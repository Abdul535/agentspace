"""Spike for ADR-004: does a Daytona sandbox do what `SandboxWorkspace` needs?

Creates one throwaway sandbox, checks each spike question, prints a report, and
deletes the sandbox (even on failure). Not part of the app: the SDK is pulled in
only for this run, so it is not a project dependency yet.

    cd apps/api
    uv run --with daytona==0.232.0 python scripts/spike_daytona.py

Needs DAYTONA_API_KEY in the environment or in apps/api/.env.
"""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from daytona import AsyncDaytona, AsyncSandbox, CreateSandboxFromSnapshotParams, DaytonaConfig, ListSandboxesQuery

LABEL = {"agentspace-spike": "1"}
results: list[tuple[str, str]] = []


def load_key() -> str:
    """DAYTONA_API_KEY from the environment, else from apps/api/.env."""
    if key := os.environ.get("DAYTONA_API_KEY"):  # TS: `:=` assigns inside the condition
        return key
    env_file = Path(__file__).resolve().parents[1] / ".env"
    for line in env_file.read_text().splitlines() if env_file.exists() else []:
        name, _, value = line.partition("=")  # TS: like line.split("=", 2), but never throws
        if name.strip() == "DAYTONA_API_KEY":
            return value.strip()
    raise SystemExit("Set DAYTONA_API_KEY (environment or apps/api/.env) to run the spike.")


def record(question: str, answer: str) -> None:
    """Keep one finding for the report, and show it as it happens."""
    results.append((question, answer))
    print(f"- {question}: {answer}", flush=True)


async def timed[T](make: Callable[[], Awaitable[T]]) -> tuple[T, float]:
    """Await `make()` and return its result with the seconds it took."""
    started = time.monotonic()
    value = await make()
    return value, time.monotonic() - started


async def sh(sandbox: AsyncSandbox, command: str, **kwargs: Any) -> tuple[int, str]:
    """Run a shell command in the sandbox; exit code and output."""
    response = await sandbox.process.exec(command, **kwargs)
    return response.exit_code, response.result


async def spike(daytona: AsyncDaytona, sandbox: AsyncSandbox) -> None:
    """Answer ADR-004's spike questions against a running sandbox."""
    work = await sandbox.get_work_dir()
    record("work dir", work)

    # Q2: exit code, merged output, cwd.
    await sh(sandbox, "mkdir -p sub")
    code, out = await sh(sandbox, "pwd; echo out; echo err >&2; exit 3", cwd=f"{work}/sub")
    record("Q2 exec exit code / output / cwd", f"exit={code} output={out!r}")

    # Q2: timeout. Does it return, raise, and is the command killed?
    try:
        (code, out), took = await timed(lambda: sh(sandbox, "echo $$ > /tmp/fg.pid; echo started; sleep 30", timeout=3))
        record("Q2 timeout result", f"returned after {took:.1f}s, exit={code} output={out!r}")
    except Exception as exc:  # the spike wants to see whichever error the SDK raises
        record("Q2 timeout result", f"raised {type(exc).__name__}: {exc}")
    _, alive = await sh(sandbox, "kill -0 $(cat /tmp/fg.pid) 2>/dev/null && echo ALIVE || echo DEAD")
    record("Q2 timed-out command afterwards", alive.strip())

    # Q2: background children: does exec return at once, and do they outlive it?
    (code, out), took = await timed(lambda: sh(sandbox, "(sleep 300; echo late) & echo $! > /tmp/bg.pid; echo done"))
    _, alive = await sh(sandbox, "kill -0 $(cat /tmp/bg.pid) 2>/dev/null && echo ALIVE || echo DEAD")
    record("Q2 background child", f"exec returned in {took:.1f}s ({out.strip()!r}); child afterwards: {alive.strip()}")

    # Q3: bytes in and out, parent folders, listing, info.
    payload = bytes(range(256)) * 4
    try:
        await sandbox.fs.upload_file(payload, f"{work}/deep/nested/blob.bin")
        record("Q3 upload creates parent folders", "yes")
    except Exception as exc:
        record("Q3 upload creates parent folders", f"no ({type(exc).__name__}: {exc})")
        await sandbox.fs.create_folder(f"{work}/deep/nested", "755")
        await sandbox.fs.upload_file(payload, f"{work}/deep/nested/blob.bin")
    back = await sandbox.fs.download_file(f"{work}/deep/nested/blob.bin")
    record("Q3 binary round trip", "identical" if back == payload else f"DIFFERENT ({len(back or b'')} bytes)")
    listing = await sandbox.fs.list_files(f"{work}/deep")
    record("Q3 list_files", ", ".join(f"{f.name}{'/' if f.is_dir else ''}" for f in listing))
    info = await sandbox.fs.get_file_info(f"{work}/deep/nested/blob.bin")
    record("Q3 file info", f"size={info.size} is_dir={info.is_dir}")
    try:
        await sandbox.fs.download_file(f"{work}/missing.txt")
        record("Q3 missing file", "no error")
    except Exception as exc:
        record("Q3 missing file", f"{type(exc).__name__}: {str(exc)[:120]}")

    # Q4: ripgrep in the default image.
    code, out = await sh(sandbox, "command -v rg && rg --version | head -1 || echo MISSING")
    record("Q4 ripgrep", out.strip().replace("\n", " | "))

    # Q5: network blocked (the sandbox was created with network_block_all).
    code, out = await sh(sandbox, "curl -sS -m 5 -o /dev/null -w '%{http_code}' https://example.com 2>&1 || true")
    record("Q5 outbound HTTPS with network_block_all", out.strip()[:150])

    # Q6: find the sandbox again by label.
    # TS: an async comprehension, like `for await (const s of daytona.list(...))` collecting ids.
    found = [s.id async for s in daytona.list(ListSandboxesQuery(labels=LABEL))]
    record("Q6 found by label", f"{sandbox.id in found} ({len(found)} sandbox(es) with the label)")

    # Q1: files survive stop -> start; how long each takes.
    await sh(sandbox, "echo kept > persist.txt")
    _, stop_s = await timed(lambda: daytona.stop(sandbox))
    _, start_s = await timed(lambda: daytona.start(sandbox))
    _, out = await sh(sandbox, "cat persist.txt 2>&1")
    record("Q1 stop / start time", f"stop {stop_s:.1f}s, start {start_s:.1f}s")
    record("Q1 file after stop/start", out.strip())


async def main() -> None:
    """Create the sandbox, run the checks, always delete it, then print the report."""
    # TS: `async with` closes the client at the end, like `await using`.
    async with AsyncDaytona(DaytonaConfig(api_key=load_key())) as daytona:
        params = CreateSandboxFromSnapshotParams(labels=LABEL, auto_stop_interval=15, network_block_all=True)
        sandbox, create_s = await timed(lambda: daytona.create(params))
        record("create time", f"{create_s:.1f}s (id {sandbox.id})")
        try:
            await spike(daytona, sandbox)
        finally:
            await daytona.delete(sandbox)
            print("sandbox deleted", flush=True)

    print("\n## Report\n")
    for question, answer in results:
        print(f"- **{question}:** {answer}")


if __name__ == "__main__":
    asyncio.run(main())
