"""The ripgrep (`rg`) arguments the `grep` and `find` tools share, as pi's.

Searches skip `.gitignore`d files (even outside a git repo) and `.git/`, include
dotfiles, don't follow symlinks (so they stay inside the workspace), and come out
sorted by path so results and limits are the same on every run. The workspace runs
rg wherever its files are (ADR-004).
"""

from collections.abc import AsyncGenerator, Sequence

from agentspace.workspace import Workspace

# --no-config: the tools' behaviour must not depend on the server user's RIPGREP_CONFIG_PATH.
BASE_ARGS = ("--no-config", "--hidden", "--no-require-git", "--sort", "path", "--color=never", "--no-messages")


def rg_lines(workspace: Workspace, flags: Sequence[str], targets: Sequence[str], cwd: str) -> AsyncGenerator[str]:
    """`rg` with the shared arguments, run by `workspace` in `cwd`; iterate it inside `contextlib.aclosing(...)`."""
    # `!.git` goes last: a later glob wins, so a model glob like `*` can't pull `.git/` back in.
    return workspace.ripgrep([*BASE_ARGS, *flags, "--glob", "!.git", "--", *targets], cwd=cwd)


def strip_dot_slash(glob: str) -> str:
    """Drop a leading `./` (keeping a `!`): rg never matches it, so `./src/*.ts` would find nothing."""
    negate = "!" if glob.startswith("!") else ""
    return negate + glob.removeprefix(negate).removeprefix("./")
