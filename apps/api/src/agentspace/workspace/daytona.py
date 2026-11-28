"""The Daytona adapters: `DaytonaBox` (one running sandbox) and `DaytonaSandboxes` (find, create, wake) (ADR-035).

Everything Daytona-specific lives here; the spike's findings (ADR-004) shape it:
- Plain `exec` returns output only at the end, so commands run as session commands, whose log streams.
- A background child holding the output keeps that log open, and Daytona reports no exit code until it
  closes. So the command's shell prints its exit code into the stream as it exits, then kills its group.
- Auto-stop counts API calls, not running processes, so a long command pings the sandbox now and then.
- Every call is a round trip (0.3–1 s from a laptop), so a command makes as few as it can.
"""

import asyncio
import contextlib
import logging
import shlex
import uuid
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager

from daytona import (
    AsyncDaytona,
    AsyncSandbox,
    CreateSandboxFromSnapshotParams,
    DaytonaConflictError,
    DaytonaError,
    DaytonaFileNotFoundError,
    DaytonaNotFoundError,
    SandboxState,
    SessionExecuteRequest,
)

from agentspace.agent.tool import ToolError
from agentspace.workspace import DirEntry, PathKind
from agentspace.workspace.sandbox import OnOutput
from agentspace.workspace.sandboxes import Found, SandboxNameTakenError, SandboxSpec

log = logging.getLogger(__name__)

# One API call (file info, a small download, a status poll) may take this long before it counts as failed.
CALL_TIMEOUT_SECONDS = 30
# Uploads and downloads move whole files.
TRANSFER_TIMEOUT_SECONDS = 300
# Creating, starting or waking a sandbox, including from archive.
LIFECYCLE_TIMEOUT_SECONDS = 300
# A long command pings the sandbox this often, so auto-stop (15 min without API calls) doesn't stop it.
KEEPALIVE_SECONDS = 300
# After the exit code arrives, how long stderr's last chunk may take; usually the log closes at once.
DRAIN_SECONDS = 0.5

# States a sandbox passes through on its way to running or stopped; we wait them out.
_WAKING = {SandboxState.CREATING, SandboxState.STARTING, SandboxState.RESTORING, SandboxState.RESUMING}
_SLEEPING = {SandboxState.STOPPING, SandboxState.ARCHIVING, SandboxState.PAUSING}
_ASLEEP = {SandboxState.STOPPED, SandboxState.ARCHIVED, SandboxState.PAUSED}


@asynccontextmanager
async def _vendor_call(what: str, timeout: float = CALL_TIMEOUT_SECONDS) -> AsyncGenerator[None]:
    """Bound one Daytona call in time, and turn its failures into a `ToolError` the model can read."""
    try:
        async with asyncio.timeout(timeout):
            yield
    except TimeoutError as exc:
        raise ToolError(f"The sandbox didn't answer in time ({what}). Try again.") from exc
    except DaytonaError as exc:
        # The detail goes to the log; the model gets the kind of failure, never request internals.
        log.warning("Daytona %s failed: %s", what, exc)
        raise ToolError(f"The sandbox failed ({what}): {type(exc).__name__}. Try again.") from exc


class DaytonaBox:
    """A running Daytona sandbox, behind the `Box` port."""

    def __init__(self, sandbox: AsyncSandbox) -> None:
        """Wraps an SDK sandbox that is already started."""
        self._sandbox = sandbox

    @property
    def id(self) -> str:
        """Daytona's sandbox id."""
        return self._sandbox.id

    async def kind(self, path: str) -> PathKind | None:
        """From the file's info; a missing path is None, not an error."""
        async with _vendor_call("file info"):
            try:
                info = await self._sandbox.fs.get_file_info(path)
            except (DaytonaFileNotFoundError, DaytonaNotFoundError):
                return None  # the normal answer for a missing path, so not logged as a failure
        return "dir" if info.is_dir else "file"

    async def download(self, path: str) -> bytes:
        """The file's bytes."""
        async with _vendor_call("download", TRANSFER_TIMEOUT_SECONDS):
            data = await self._sandbox.fs.download_file(path)
        return data or b""  # the SDK types an empty file's body as None

    async def upload(self, path: str, data: bytes) -> None:
        """Daytona creates the parent folders (spike question 3)."""
        async with _vendor_call("upload", TRANSFER_TIMEOUT_SECONDS):
            await self._sandbox.fs.upload_file(data, path)

    async def entries(self, path: str) -> list[DirEntry]:
        """The folder's entries."""
        async with _vendor_call("list folder"):
            files = await self._sandbox.fs.list_files(path)
        return [DirEntry(f.name, bool(f.is_dir)) for f in files]

    async def run(self, command: str, *, cwd: str, on_stdout: OnOutput, on_stderr: OnOutput) -> int:
        """A session command in its own process group, streamed; the group is killed when it exits."""
        process = self._sandbox.process
        session = f"agentspace-{uuid.uuid4().hex}"
        pidfile = f"/tmp/{session}.pid"
        marker = ExitMarker(uuid.uuid4().hex, on_stdout)
        # setsid gives the command its own process group, whose id the leader writes down (for a cancel).
        # `eval` keeps a bare `exit` from ending the session's shell (spike Q2b). On exit the trap prints
        # the code with a one-time nonce, then kills the group: the code arrives even while a background
        # child holds the output open, and no extra call is needed to clean up. It goes to stdout, after
        # everything the command wrote there, so seeing it means stdout is complete.
        trap = f"c=$?; printf '\\n{marker.nonce}:%d\\n' $c; kill -KILL -- -$$"
        inner = f'trap {shlex.quote(trap)} EXIT; echo $$ > "$0"; eval "$1"'
        setsid = shlex.join(["setsid", "-w", "bash", "-c", inner, pidfile, command])
        script = f"cd {shlex.quote(cwd)} && {setsid} < /dev/null"
        async with _vendor_call("start command"):
            await process.create_session(session)
            response = await process.execute_session_command(
                session, SessionExecuteRequest(command=script, run_async=True)
            )
        command_id = response.cmd_id
        assert command_id is not None  # always set for run_async; tells the type checker
        logs = asyncio.create_task(process.get_session_command_logs_async(session, command_id, marker.feed, on_stderr))
        seen = asyncio.create_task(marker.seen.wait())
        finished = False
        try:
            while marker.code is None and not logs.done():
                await asyncio.wait({logs, seen}, timeout=KEEPALIVE_SECONDS, return_when=asyncio.FIRST_COMPLETED)
                if marker.code is None and not logs.done():
                    async with _vendor_call("keep the sandbox awake"):
                        await self._sandbox.refresh_activity()
            # stderr has its own pipe, so its last chunk may trail the marker by a moment.
            with contextlib.suppress(TimeoutError, DaytonaError):
                await asyncio.wait_for(asyncio.shield(logs), DRAIN_SECONDS)
            marker.flush()
            if marker.code is not None:
                finished = True
                return marker.code
            # The log ended without the code (a dropped connection): ask Daytona.
            async with _vendor_call("command status"):
                status = await process.get_session_command(session, command_id)
            if status.exit_code is None:
                raise ToolError("Lost the connection to the sandbox while the command ran. Try again.")
            finished = True
            return status.exit_code
        finally:
            seen.cancel()
            logs.cancel()
            with contextlib.suppress(asyncio.CancelledError, DaytonaError):
                await logs
            # Cancelled or failed before the trap ran: the group may still be running, so kill it now.
            if not finished:
                await self._kill(pidfile)
            _forget_session(self._sandbox, session)

    async def _kill(self, pidfile: str) -> None:
        """Kill a command's process group by its pid file; best effort, since the run goes on either way."""
        quoted = shlex.quote(pidfile)
        try:
            async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                await self._sandbox.process.exec(
                    f'kill -KILL -- -"$(cat {quoted})" 2>/dev/null; rm -f {quoted}', timeout=10
                )
        except (TimeoutError, DaytonaError) as exc:
            # A leftover process dies with the sandbox's next stop; nothing the user can act on.
            log.warning("Killing a Daytona command in %s failed: %s", self.id, exc)


class ExitMarker:
    """Takes a command's stdout, passes it on, and picks out the `<nonce>:<code>` line its exit trap prints."""

    def __init__(self, nonce: str, on_output: OnOutput) -> None:
        """`nonce` is unique to the command, so its output can't fake the line."""
        self.nonce = nonce
        self._line = f"\n{nonce}:"
        self._on_output = on_output
        self._pending = ""
        self.code: int | None = None
        self.seen = asyncio.Event()

    def feed(self, chunk: str) -> None:
        """One stdout chunk; text that might be the start of the marker is held back until the next one."""
        if self.code is not None:
            return
        text = self._pending + chunk
        start = text.find(self._line)
        if start != -1:
            end = text.find("\n", start + len(self._line))
            if end != -1:
                self._pending = ""
                if start:
                    self._on_output(text[:start])
                self.code = int(text[start + len(self._line) : end])
                self.seen.set()
                return
            self._pending = text[start:]  # the marker's line hasn't ended yet
            if start:
                self._on_output(text[:start])
            return
        # Hold back a tail that could be the marker's beginning, cut across two chunks.
        keep = max((n for n in range(1, len(self._line)) if text.endswith(self._line[:n])), default=0)
        self._pending = text[len(text) - keep :] if keep else ""
        if passed := text[: len(text) - keep]:
            self._on_output(passed)

    def flush(self) -> None:
        """Pass on what was held back once the output has ended."""
        if self._pending and self.code is None:
            self._on_output(self._pending)
        self._pending = ""


# Session deletions still in flight; kept here so they aren't garbage-collected mid-call.
_forgetting: set[asyncio.Task[None]] = set()


def _forget_session(sandbox: AsyncSandbox, session: str) -> None:
    """Delete a finished command's Daytona session without making the tool call wait for it."""

    async def forget() -> None:
        """One bounded delete; a leftover session goes when the sandbox stops."""
        try:
            async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                await sandbox.process.delete_session(session)
        except (TimeoutError, DaytonaError) as exc:
            log.warning("Deleting Daytona session %s in %s failed: %s", session, sandbox.id, exc)

    task = asyncio.create_task(forget())
    _forgetting.add(task)
    task.add_done_callback(_forgetting.discard)


class DaytonaSandboxes:
    """Finds, creates and wakes Daytona sandboxes by name, behind the `Sandboxes` port."""

    home = "/home/daytona"
    """The default snapshot's user home (spike: the work dir)."""

    def __init__(self, client: AsyncDaytona, spec: SandboxSpec) -> None:
        """`client` is shared by every session; `spec` says how new sandboxes are made."""
        self._client = client
        self._spec = spec

    async def find(self, name: str) -> Found | None:
        """The sandbox called `name` and whether it is running, or None when there is none."""
        async with _vendor_call("find sandbox"):
            try:
                sandbox = await self._client.get(name)
            except DaytonaNotFoundError:
                return None
        return Found(DaytonaBox(sandbox), running=sandbox.state == SandboxState.STARTED)

    async def create(self, name: str, labels: Mapping[str, str]) -> DaytonaBox:
        """A new started sandbox; `SandboxNameTakenError` if one with this name already exists."""
        params = CreateSandboxFromSnapshotParams(
            name=name,
            labels=dict(labels),
            auto_stop_interval=self._spec.auto_stop_minutes,
            auto_archive_interval=self._spec.auto_archive_minutes,
            domain_allow_list=self._allow_list(),
            network_block_all=self._spec.network == "blocked" or None,
        )
        try:
            async with _vendor_call("create sandbox", LIFECYCLE_TIMEOUT_SECONDS):
                sandbox = await self._client.create(params, timeout=LIFECYCLE_TIMEOUT_SECONDS)
        except ToolError as exc:
            if isinstance(exc.__cause__, DaytonaConflictError):
                raise SandboxNameTakenError(name) from exc
            raise
        return DaytonaBox(sandbox)

    async def start(self, name: str) -> DaytonaBox:
        """Wake the sandbox called `name`, waiting out a stop or start already under way."""
        async with _vendor_call("start sandbox", LIFECYCLE_TIMEOUT_SECONDS):
            sandbox = await self._client.get(name)
            if sandbox.state in _SLEEPING:
                await sandbox.wait_for_sandbox_stop(timeout=LIFECYCLE_TIMEOUT_SECONDS)
                await sandbox.refresh_data()
            if sandbox.state in _ASLEEP:
                try:
                    await self._client.start(sandbox, timeout=LIFECYCLE_TIMEOUT_SECONDS)
                except DaytonaConflictError:
                    # Another server woke it at the same moment; wait for theirs instead.
                    await sandbox.wait_for_sandbox_start(timeout=LIFECYCLE_TIMEOUT_SECONDS)
            elif sandbox.state in _WAKING:
                await sandbox.wait_for_sandbox_start(timeout=LIFECYCLE_TIMEOUT_SECONDS)
            elif sandbox.state != SandboxState.STARTED:
                raise ToolError(f"The sandbox is in state '{sandbox.state}' and can't be started.")
            # A sandbox made before the network policy changed gets the current one. Not for the vendor's
            # default: Daytona's lower tiers refuse any per-sandbox network call.
            if self._spec.network == "allow_list":
                await sandbox.update_network_settings(domain_allow_list=self._allow_list())
            elif self._spec.network == "blocked":
                await sandbox.update_network_settings(network_block_all=True)
        return DaytonaBox(sandbox)

    def _allow_list(self) -> str | None:
        """Daytona's comma-separated domain allow list, when the spec asks for one."""
        return ",".join(self._spec.allowed_domains) if self._spec.network == "allow_list" else None
