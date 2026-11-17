"""Running ripgrep (`rg`) for the `grep` and `find` tools, as pi does.

Searches skip `.gitignore`d files (even outside a git repo) and `.git/`, include
dotfiles, don't follow symlinks (so they stay inside the workspace), and come out
sorted by path so results and limits are the same on every run.
"""

import asyncio
import shutil
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

from agentspace.agent.tool import ToolError

# --no-config: the tools' behaviour must not depend on the server user's RIPGREP_CONFIG_PATH.
BASE_ARGS = ("--no-config", "--hidden", "--no-require-git", "--sort", "path", "--color=never", "--no-messages")


# TS: an `async function*`; the caller iterates it with `async for`, like `for await`.
async def rg_lines(flags: Sequence[str], targets: Sequence[str], cwd: Path) -> AsyncIterator[str]:
    """Run `rg` in `cwd` and yield its output lines.

    Iterate it inside `contextlib.aclosing(...)`: stopping early then kills rg at once.
    Raises `ToolError` when rg is missing or reports an error (exit code 2, e.g. a bad regex).
    """
    rg = shutil.which("rg")  # TS: like the `which` npm package; None when not on PATH
    if rg is None:
        raise ToolError("ripgrep (rg) is not installed on the server. Install it, e.g. `brew install ripgrep`.")
    # `!.git` goes last: a later glob wins, so a model glob like `*` can't pull `.git/` back in.
    args = [*BASE_ARGS, *flags, "--glob", "!.git", "--", *targets]
    proc = await asyncio.create_subprocess_exec(
        rg,
        *args,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,  # small with --no-messages: only fatal errors are printed
    )
    assert proc.stdout is not None and proc.stderr is not None  # set because of PIPE; tells the type checker
    try:
        # TS: `async for` over a StreamReader yields one line (bytes) at a time, like readline's `for await`.
        async for line in proc.stdout:
            yield line.decode("utf-8", errors="replace").rstrip("\n")
        stderr = await proc.stderr.read()
        # Exit 1 only means "nothing matched".
        if await proc.wait() not in (0, 1):
            raise ToolError(stderr.decode(errors="replace").strip() or f"ripgrep exited with code {proc.returncode}")
    finally:
        # The caller stopped early (a limit) or the run was cancelled: stop rg instead of letting it finish.
        if proc.returncode is None:
            proc.kill()
            await proc.wait()


def strip_dot_slash(glob: str) -> str:
    """Drop a leading `./` (keeping a `!`): rg never matches it, so `./src/*.ts` would find nothing."""
    negate = "!" if glob.startswith("!") else ""
    return negate + glob.removeprefix(negate).removeprefix("./")
