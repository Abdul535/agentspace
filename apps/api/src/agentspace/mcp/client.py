"""A minimal MCP client over stdio, after pi's `packages/mcp/src/client.ts` and `transports/stdio.ts` (ADR-017).

The server is a child process. Each message is one line of JSON-RPC 2.0 on its
stdin (to it) or stdout (from it); its stderr is kept only as a short tail for
error messages. A reader task matches replies to waiting requests by id.
"""

import asyncio
import contextlib
import itertools
import json
import logging
import os
import signal
from collections import deque
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2025-06-18"
"""The MCP version we ask for in `initialize`; a server may answer with an older one it supports."""
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
CLIENT_INFO = {"name": "agentspace", "version": "0.1.0"}

# One JSON-RPC line may hold a large tool result or an image; asyncio's default line limit is 64 KB.
MAX_LINE_BYTES = 32 * 1024 * 1024
STDERR_TAIL_CHARS = 2000
# How long a server gets to exit on its own after stdin closes, then after SIGTERM, before the next step.
STDIN_CLOSE_GRACE_S = 0.5
SIGTERM_GRACE_S = 2.0

# Variables a child server inherits from the API's environment, like the official SDKs' default.
# The rest (API keys among them) stay out unless the server's own `env` sets them.
INHERITED_ENV = ("HOME", "PATH", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TMPDIR")


# TS: class McpError extends Error {}
class McpError(Exception):
    """A request failed: the server answered with an error, it exited, or the reply was unusable."""


class McpToolInfo(BaseModel):
    """One tool as `tools/list` describes it."""

    # TS: like zod's .passthrough(): unknown fields from newer servers are kept, not rejected.
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    title: str | None = None
    description: str | None = None
    # TS: `alias` maps the wire name `inputSchema` to the Python-style `input_schema`.
    input_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"}, alias="inputSchema")


class CallToolResult(BaseModel):
    """What `tools/call` returns: content blocks for the model, and whether the tool failed."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    content: list[dict[str, Any]] = []
    structured_content: Any = Field(default=None, alias="structuredContent")
    is_error: bool = Field(default=False, alias="isError")


def child_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment a server starts with: a few safe variables from ours, then its own `env`."""
    # TS: a dict comprehension, like Object.fromEntries(KEYS.filter((k) => k in env).map((k) => [k, env[k]])).
    base = {key: os.environ[key] for key in INHERITED_ENV if key in os.environ}
    return base | dict(env)  # TS: `|` merges dicts, right side wins, like { ...base, ...env }


class StdioClient:
    """One running MCP server process and the JSON-RPC session with it."""

    def __init__(self, command: str, args: Sequence[str], env: Mapping[str, str], cwd: Path) -> None:
        self.command, self.args, self.env, self.cwd = command, list(args), dict(env), cwd
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future[Any]] = {}
        self.ids = itertools.count(1)  # TS: a generator of 1, 2, 3, ...; next(self.ids) takes the next one
        self.write_lock = asyncio.Lock()
        self.stderr_tail: deque[str] = deque(maxlen=50)
        self.tasks: list[asyncio.Task[None]] = []
        self.closed = False
        self.instructions: str | None = None
        """What the server says about itself in `initialize`; shown on the settings page's test result."""
        self.server_info: dict[str, Any] = {}

    @property
    def alive(self) -> bool:
        """Whether the process is running and the session is usable."""
        return not self.closed and self.process is not None and self.process.returncode is None

    def stderr_text(self) -> str:
        """The last lines the server wrote to stderr, for error messages."""
        return "".join(self.stderr_tail)[-STDERR_TAIL_CHARS:]

    async def start(self) -> None:
        """Start the process and run the `initialize` handshake. Closes the process again if any step fails."""
        try:
            self.process = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=child_env(self.env),
                limit=MAX_LINE_BYTES,
                # Its own process group, so stopping it also stops what `npx` / `uvx` started (pi does the same).
                start_new_session=True,
            )
        except OSError as exc:
            raise McpError(f"Could not start '{self.command}': {exc}") from exc
        self.tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]
        try:
            result = await self.request(
                "initialize",
                {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
            )
            version = result.get("protocolVersion")
            if version not in SUPPORTED_VERSIONS:
                raise McpError(f"The server chose an unsupported MCP version: {version}")
            self.instructions = result.get("instructions")
            self.server_info = result.get("serverInfo") or {}
            await self.notify("notifications/initialized")
        except BaseException:
            # TS: like `catch (e) { await this.close(); throw e }`; BaseException also covers cancellation.
            await self.close()
            raise

    async def list_tools(self) -> list[McpToolInfo]:
        """Every tool the server offers, following `nextCursor` across pages."""
        tools: list[McpToolInfo] = []
        cursor: str | None = None
        for _ in range(100):  # A guard against a server that pages forever.
            result = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(McpToolInfo.model_validate(tool) for tool in result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                return tools
        raise McpError("tools/list returned more than 100 pages")

    async def call_tool(self, name: str, arguments: dict[str, Any], timeout: float) -> CallToolResult:
        """Call a tool and wait for its result; `TimeoutError` after `timeout` seconds."""
        result = await self.request("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)
        return CallToolResult.model_validate(result)

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 60) -> dict[str, Any]:
        """Send a request and wait for its reply; on timeout or cancellation, tell the server to stop working on it."""
        if not self.alive:
            raise McpError(self._exit_message())
        request_id = next(self.ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})
            # TS: `async with` runs cleanup on exit, like try/finally; this one raises TimeoutError when time runs out.
            async with asyncio.timeout(timeout):
                result = await future
        except (TimeoutError, asyncio.CancelledError):
            # The server may still be working on it; tell it to stop (best effort, never awaited for long).
            if self.alive:
                with contextlib.suppress(Exception):  # TS: try { ... } catch {} — any failure here is ignored
                    await self.notify("notifications/cancelled", {"requestId": request_id, "reason": "cancelled"})
            raise
        finally:
            self.pending.pop(request_id, None)
        if not isinstance(result, dict):
            raise McpError(f"{method} returned {type(result).__name__}, not an object")
        return result

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a notification: a message that gets no reply."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._send(message)

    async def _send(self, message: dict[str, Any]) -> None:
        """Write one message as a line on the server's stdin."""
        if self.process is None or self.process.stdin is None or self.closed:
            raise McpError(self._exit_message())
        line = json.dumps(message, separators=(",", ":")) + "\n"
        # One writer at a time, so two concurrent calls can't interleave their bytes.
        async with self.write_lock:
            try:
                self.process.stdin.write(line.encode())
                await self.process.stdin.drain()
            except (ConnectionError, RuntimeError) as exc:
                raise McpError(self._exit_message()) from exc

    async def _read_stdout(self) -> None:
        """Read messages until the server closes stdout, then fail whatever is still waiting."""
        assert self.process is not None and self.process.stdout is not None  # TS: like `!`, but checked at runtime
        try:
            while line := await self.process.stdout.readline():  # TS: `:=` assigns inside the condition
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    # Servers sometimes print logs to stdout; skip anything that isn't JSON.
                    log.debug("MCP %s: non-JSON stdout line: %r", self.command, line[:200])
                    continue
                if isinstance(message, dict):
                    await self._dispatch(message)
        except (ValueError, ConnectionError) as exc:  # ValueError: a line over MAX_LINE_BYTES
            log.warning("MCP %s: stdout closed: %s", self.command, exc)
        finally:
            self.closed = True
            error = McpError(self._exit_message())
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)

    async def _dispatch(self, message: dict[str, Any]) -> None:
        """Route one message: a reply to us, a request from the server, or a notification (ignored for now)."""
        method, request_id = message.get("method"), message.get("id")
        if method is None:
            future = self.pending.get(request_id) if isinstance(request_id, int) else None
            if future is None or future.done():
                return
            if (error := message.get("error")) is not None:
                future.set_exception(McpError(f"{error.get('message', 'error')} (code {error.get('code')})"))
            else:
                future.set_result(message.get("result"))
        elif request_id is not None:
            # The server asks us something. We answer `ping` and refuse the rest (sampling, roots, elicitation).
            if method == "ping":
                reply: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "result": {}}
            else:
                error = {"code": -32601, "message": f"Method not found: {method}"}
                reply = {"jsonrpc": "2.0", "id": request_id, "error": error}
            with contextlib.suppress(McpError):
                await self._send(reply)

    async def _read_stderr(self) -> None:
        """Keep the last lines the server logs, so a crash can say why."""
        assert self.process is not None and self.process.stderr is not None
        with contextlib.suppress(ValueError, ConnectionError):
            while line := await self.process.stderr.readline():
                self.stderr_tail.append(line.decode(errors="replace"))

    def _exit_message(self) -> str:
        """Why the server can't be reached, with its exit code and the end of its stderr when known."""
        code = self.process.returncode if self.process else None
        text = "The MCP server is not running" if code is None else f"The MCP server exited with code {code}"
        tail = self.stderr_text().strip()
        return f"{text}. Its last output:\n{tail}" if tail else f"{text}."

    async def close(self) -> None:
        """Stop the server: close stdin, then SIGTERM, then SIGKILL its process group, waiting a little between."""
        self.closed = True
        process = self.process
        if process is not None and process.returncode is None:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    process.stdin.close()
            for sig, grace in ((None, STDIN_CLOSE_GRACE_S), (signal.SIGTERM, SIGTERM_GRACE_S), (signal.SIGKILL, 5.0)):
                if sig is not None:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(process.pid, sig)  # the whole group: the server and anything it started
                try:
                    async with asyncio.timeout(grace):
                        await process.wait()
                    break
                except TimeoutError:
                    continue
        for task in self.tasks:
            task.cancel()
        # TS: like Promise.allSettled: wait for the readers to stop, ignoring their errors.
        await asyncio.gather(*self.tasks, return_exceptions=True)
