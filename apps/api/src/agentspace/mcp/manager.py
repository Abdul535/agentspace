"""Per-session MCP connections, after pi's `extensions/mcp/runtime.ts` and `index.ts` (ADR-017).

Each session gets its own server processes, started on its first run with the
session's workspace as their working folder, and closed after a while idle.
A run waits a short time for servers to connect; a server that is slow keeps
connecting in the background and joins a later run. A dead connection is
made again on the next call.
"""

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agentspace.agent.tool import Tool
from agentspace.mcp.client import McpError, McpToolInfo, StdioClient
from agentspace.mcp.tools import GetClient, McpTool, mcp_tool_name

log = logging.getLogger(__name__)


# TS: frozen=True makes the fields readonly; `==` compares field by field, which spots a changed config.
@dataclass(frozen=True)
class ServerConfig:
    """What starting one stdio server needs, as saved on the settings page."""

    id: str
    name: str
    command: str
    args: tuple[str, ...]
    env: tuple[tuple[str, str], ...]


class Connection:
    """One server for one session: its client, connected on demand and again after it dies."""

    def __init__(self, config: ServerConfig, cwd: Path, connect_timeout: float) -> None:
        self.config = config
        self.cwd = cwd
        self.connect_timeout = connect_timeout
        self.client: StdioClient | None = None
        self.connecting: asyncio.Task[StdioClient] | None = None
        self.closed = False
        """Set once the server was stopped for good, so a tool still holding it can't start an untracked process."""

    async def get_client(self) -> StdioClient:
        """The live client, connecting first if there is none or it died."""
        if self.closed:
            raise McpError("This server was changed or removed; the next message will use its new settings.")
        if self.client is not None and self.client.alive:
            return self.client
        if self.connecting is None or self.connecting.done():
            self.connecting = asyncio.create_task(self._connect())
            self.connecting.add_done_callback(self._log_failure)
        # TS: shield means a caller that gives up (timeout, abort) doesn't cancel the connect for everyone else.
        return await asyncio.shield(self.connecting)

    async def _connect(self) -> StdioClient:
        """Start the process and handshake, within the connect timeout (long, for `npx` downloads)."""
        config = self.config
        client = StdioClient(config.command, config.args, dict(config.env), self.cwd)
        async with asyncio.timeout(self.connect_timeout):
            await client.start()
        self.client = client
        return client

    def _log_failure(self, task: asyncio.Task[StdioClient]) -> None:
        """Log a failed connect; reading the error here also stops asyncio warning that nobody did."""
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.warning("MCP server %s failed to connect: %s", self.config.name, exc)

    async def close(self) -> None:
        """Stop connecting and stop the process."""
        self.closed = True
        if self.connecting is not None and not self.connecting.done():
            self.connecting.cancel()
            with contextlib.suppress(BaseException):
                await self.connecting
        if self.client is not None:
            await self.client.close()


@dataclass
class SessionConnections:
    """The servers of one session, and when it last used them."""

    servers: dict[str, Connection] = field(default_factory=dict)
    last_used: float = field(default_factory=time.monotonic)


class McpManager:
    """Every session's MCP connections, in this process's memory, like runs (ADR-007)."""

    def __init__(
        self,
        *,
        wait_timeout: float = 10,
        connect_timeout: float = 120,
        call_timeout: float = 60,
        idle_timeout: float = 600,
        is_busy: Callable[[str], bool] = lambda _session_id: False,
    ) -> None:
        self.wait_timeout = wait_timeout
        """How long a run waits for its servers before going on without the slow ones."""
        self.connect_timeout = connect_timeout
        """How long one connect may take in the background before it counts as failed."""
        self.call_timeout = call_timeout
        self.idle_timeout = idle_timeout
        self.is_busy = is_busy
        """Whether a session is running; its servers are never reaped mid-run, however long since a call."""
        self.sessions: dict[str, SessionConnections] = {}
        self.reaper: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start the task that closes idle sessions' servers."""
        self.reaper = asyncio.create_task(self._reap_idle())

    async def tools_for(self, session_id: str, workspace: Path, configs: list[ServerConfig]) -> list[Tool]:
        """The tools of the session's servers that connect within the wait, connecting or reconnecting as needed."""
        session = self.sessions.setdefault(session_id, SessionConnections())
        session.last_used = time.monotonic()
        wanted = {config.id: config for config in configs}
        # A server removed, disabled or edited since the last run is stopped; an edited one starts again below.
        for server_id, conn in list(session.servers.items()):
            if wanted.get(server_id) != conn.config:
                del session.servers[server_id]
                await conn.close()
        for config in configs:
            if config.id not in session.servers:
                session.servers[config.id] = Connection(config, workspace, self.connect_timeout)
        if not session.servers:
            return []

        conns = list(session.servers.values())
        listing = [asyncio.create_task(self._list(conn)) for conn in conns]
        done, slow = await asyncio.wait(listing, timeout=self.wait_timeout)
        for task in slow:
            task.cancel()  # Only the listing; the connect itself goes on (shielded) for the next run.
        tools: list[Tool] = []
        taken: set[str] = set()
        for conn, task in zip(conns, listing, strict=True):
            if task not in done or task.exception() is not None:
                continue
            for info in task.result():
                name = mcp_tool_name(conn.config.name, info.name, taken)
                taken.add(name)
                tools.append(
                    McpTool(
                        name=name,
                        server=conn.config.name,
                        info=info,
                        get_client=self._client_getter(session, conn),
                        timeout=self.call_timeout,
                    )
                )
        return tools

    async def _list(self, conn: Connection) -> list[McpToolInfo]:
        """The server's tools, connecting first if needed."""
        client = await conn.get_client()
        return await client.list_tools()

    def _client_getter(self, session: SessionConnections, conn: Connection) -> GetClient:
        """For a tool: its server's client, marking the session as in use."""

        async def get_client() -> StdioClient:
            """Mark the session used, then return the live client (reconnecting if it died)."""
            session.last_used = time.monotonic()
            return await conn.get_client()

        return get_client

    async def close_server(self, server_id: str) -> None:
        """Stop one server in every session, after it was edited, disabled or deleted."""
        for session in self.sessions.values():
            if (conn := session.servers.pop(server_id, None)) is not None:
                await conn.close()

    async def close_session(self, session_id: str) -> None:
        """Stop every server of one session."""
        if (session := self.sessions.pop(session_id, None)) is not None:
            await asyncio.gather(*(conn.close() for conn in session.servers.values()))

    async def _reap_idle(self) -> None:
        """Every minute, stop the servers of sessions unused for `idle_timeout`."""
        while True:
            await asyncio.sleep(min(60, self.idle_timeout))
            now = time.monotonic()
            for session_id, session in list(self.sessions.items()):
                if now - session.last_used > self.idle_timeout and not self.is_busy(session_id):
                    await self.close_session(session_id)

    async def shutdown(self) -> None:
        """Stop the reaper and every server, when the API shuts down."""
        if self.reaper is not None:
            self.reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reaper
        await asyncio.gather(*(self.close_session(sid) for sid in list(self.sessions)))
