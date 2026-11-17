"""The `bash` tool, after pi's `packages/coding-agent/src/core/tools/bash.ts`.

Commands run on the host as the API's user, with the workspace as working directory.
That is not a sandbox: a command can reach anything the API's user can (ADR-003 step 3).
"""

import asyncio
import contextlib
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import Annotated, Any

from asyncer import asyncify
from pydantic import BaseModel, Field

from agentspace.agent.tool import OnUpdate, Tool, ToolContext, ToolResult
from agentspace.ai.messages import TextContent
from agentspace.tools.read import MAX_BYTES, MAX_LINES

# Environment variables a command inherits; everything else (API keys included) stays with the server.
ENV_ALLOWLIST = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TERM", "TMPDIR")
# Bytes read from the end of the output; twice the cap so a partial first line can be dropped.
TAIL_BYTES = 2 * MAX_BYTES
CHUNK_BYTES = 64 * 1024
# Live output for the UI while a command runs: checked this often, at most this much from the end.
UPDATE_INTERVAL_SECONDS = 0.1
UPDATE_TAIL_BYTES = 8 * 1024


class BashParams(BaseModel):
    """Arguments of the `bash` tool."""

    command: Annotated[str, Field(description="Shell command to execute")]
    timeout: Annotated[float | None, Field(gt=0, description="Timeout in seconds (optional, no default timeout)")] = (
        None
    )


# TS: class BashTool extends Tool<BashParams>
class BashTool(Tool[BashParams]):
    """Runs a shell command in the workspace and returns the end of its output."""

    name = "bash"
    description = (
        "Execute a bash command in the workspace folder. Returns stdout and stderr. Output is truncated to the "
        f"last {MAX_LINES} lines or {MAX_BYTES // 1024}KB, whichever is hit first; if truncated, the full output is "
        "saved to a temp file. Background processes the command starts are killed when it exits. "
        "Optionally provide a timeout in seconds."
    )
    params = BashParams
    snippet = "Execute bash commands (ls, grep, find, etc.)"

    async def execute(self, args: BashParams, ctx: ToolContext) -> ToolResult:
        """Run the command; a non-zero exit or a timeout becomes an error result that still carries the output."""
        log = new_log_file()
        started = time.monotonic()  # TS: performance.now(), in seconds
        exit_code: int | None = None
        watcher = asyncio.create_task(watch_output(log, ctx.on_update)) if ctx.on_update else None
        try:
            exit_code = await run_command(args.command, ctx.workspace, args.timeout, log)
        except TimeoutError:
            pass  # `exit_code` stays None; reported below with the output so far
        except asyncio.CancelledError:
            log.unlink(missing_ok=True)  # the run is gone; nobody will read this output
            raise
        finally:
            # Stop live updates before the final read; the end event carries the full result.
            if watcher is not None:
                watcher.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await watcher

        # TS: render_output reads a possibly large file (blocking); asyncify runs it on a worker thread.
        text, details = await asyncify(render_output)(log)
        # TS: `|=` merges into the dict in place, like Object.assign(details, {...}).
        details |= {"exit_code": exit_code, "wall_time_seconds": round(time.monotonic() - started, 1)}
        if exit_code is None:
            return error(text, f"Command timed out after {args.timeout:g} seconds", details)
        if exit_code != 0:
            return error(text, f"Command exited with code {exit_code}", details)
        return ToolResult.text(text or "(no output)", details)


async def watch_output(log: Path, on_update: OnUpdate) -> None:
    """Until cancelled, send the end of the output every `UPDATE_INTERVAL_SECONDS` if it grew."""
    sent_size = 0
    while True:
        await asyncio.sleep(UPDATE_INTERVAL_SECONDS)
        size = log.stat().st_size
        if size != sent_size:
            sent_size = size
            await on_update(ToolResult.text(await asyncify(read_tail)(log, UPDATE_TAIL_BYTES)))


def read_tail(log: Path, max_bytes: int) -> str:
    """The last `max_bytes` of the output, starting at a line break when it had to cut."""
    with log.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - max_bytes))
        text = f.read().decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]  # the cut landed mid-line; drop that partial line
    return text


def error(text: str, status: str, details: dict[str, Any]) -> ToolResult:
    """A failed result: the output so far, then why it failed."""
    message = f"{text}\n\n{status}" if text else status
    return ToolResult(content=[TextContent(text=message)], details=details, is_error=True)


def new_log_file() -> Path:
    """An empty temp file for one command's full output; kept only if the shown output gets truncated."""
    fd, name = tempfile.mkstemp(prefix="agentspace-bash-", suffix=".log")
    os.close(fd)
    return Path(name)


async def run_command(command: str, cwd: Path, timeout: float | None, log: Path) -> int:
    """Run `command` to completion with its output written to `log`; returns the exit code.

    Raises `TimeoutError` after `timeout` seconds. Whatever happens, the whole
    process group is killed at the end, so no child of the command keeps running.
    """
    # The command writes straight to the file: no pipe for a background child to hold open, which
    # would make `proc.wait()` hang (asyncio waits for pipes to close), and no writes on the event loop.
    # TS: `with` closes the file when the block ends, like `using` / try-finally.
    with log.open("wb") as out:
        # TS: like child_process.spawn("bash", ["-c", command], { detached: true, ... }).
        proc = await asyncio.create_subprocess_exec(
            "bash",
            "-c",
            command,
            cwd=cwd,
            env={key: os.environ[key] for key in ENV_ALLOWLIST if key in os.environ},  # TS: a dict comprehension
            stdin=asyncio.subprocess.DEVNULL,
            stdout=out,
            stderr=asyncio.subprocess.STDOUT,  # one stream, in the order the command wrote it
            start_new_session=True,  # own process group, so killpg reaches every child
        )
    try:
        # TS: asyncio.timeout(None) never fires, like skipping the setTimeout.
        async with asyncio.timeout(timeout):
            await proc.wait()
    finally:
        # Background children outlive the shell; on timeout or cancellation the shell is still running too.
        kill_group(proc.pid)
        if proc.returncode is None:
            await proc.wait()
    # A signal-killed shell reports -N; report 128 + N as shells do, so it never reads as success.
    return proc.returncode if proc.returncode >= 0 else 128 - proc.returncode


def kill_group(pid: int) -> None:
    """SIGKILL every process in the command's group; fine if they are already gone."""
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):  # TS: a tuple catches either class, like two instanceof checks
        pass


def render_output(log: Path) -> tuple[str, dict[str, Any]]:
    """The text the model sees (the end of the output, cut at the caps) and the details for the UI.

    Deletes `log` when nothing was cut, since the text already has everything.
    """
    size = log.stat().st_size
    newlines = 0
    with log.open("rb") as f:
        last_byte = b""
        while chunk := f.read(CHUNK_BYTES):  # TS: `:=` assigns and tests, like while ((chunk = read()).length)
            newlines += chunk.count(b"\n")
            last_byte = chunk[-1:]
        f.seek(max(0, size - TAIL_BYTES))
        tail = f.read()
    total_lines = newlines + (1 if last_byte not in (b"", b"\n") else 0)

    text = tail.decode("utf-8", errors="replace").removesuffix("\n")
    if size > len(tail) and "\n" in text:
        text = text.split("\n", 1)[1]  # the tail starts mid-line; drop that partial line

    # Take lines from the end until either cap would be crossed.
    lines = text.split("\n") if text else []
    shown: list[str] = []
    used = 0
    for line in reversed(lines):
        line_bytes = len(line.encode()) + 1
        if len(shown) >= MAX_LINES or used + line_bytes > MAX_BYTES:
            break
        shown.append(line)
        used += line_bytes
    shown.reverse()

    partial_line = not shown and bool(lines)
    if partial_line:
        # The last line alone is over the cap: show its end.
        shown = [lines[-1].encode()[-MAX_BYTES:].decode(errors="ignore")]
    elif len(shown) == total_lines:
        log.unlink()
        return "\n".join(shown), {"truncated": False}

    if partial_line:
        note = f"[Showing the last {MAX_BYTES // 1024}KB of line {total_lines}. Full output: {log}]"
    else:
        note = f"[Showing lines {total_lines - len(shown) + 1}-{total_lines} of {total_lines}. Full output: {log}]"
    return "\n".join(shown) + f"\n\n{note}", {"truncated": True, "full_output_path": str(log)}
