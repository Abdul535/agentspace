# ADR-034 — The `Workspace` protocol, built with a local backend only

- **Status:** Accepted
- **Date:** 2026-11-25
- **Deciders:** project owner

## Context
ADR-022 step 59, first step of Milestone C (ADR-033's order): every tool goes
through a `Workspace`, with `LocalWorkspace` as the only backend and no change
users can see. ADR-004 (still Proposed until the Daytona spike) sketched the
interface as `read_bytes`, `write_bytes`, `stat`, `list_dir` and `exec`.

Building it showed three gaps in that sketch:

- **`grep` and `find` start `rg` on our server.** In a sandbox, rg has to run
  where the files are. `exec` returns output only at the end, but these tools
  stop rg early at their result limit, so they need lines as they come.
- **`write` and `edit` hold a per-file lock** (`file_lock`) around their
  read-modify-write, keyed by the host path with symlinks resolved. A tool
  can't build that key for a sandbox, and `write_bytes` alone can't make an
  edit atomic.
- **`bash` writes its output to a local log file** while it runs, for live
  updates and for the "full output" file when the shown output is cut.

How the references do it: pi gives each tool its own small `*Operations`
interface (`read.ts`, `bash.ts`, …) and says to override `grep`'s whole
`execute()` for remote search. opencode's `WorkspaceDriver` (`packages/core/src/workspace/driver.ts`)
handles the sandbox lifecycle (create, connect, suspend, destroy), which
step 28 needs and this step doesn't.

## Decision
**In one line:** one `Workspace` protocol in `agentspace/workspace/`, taking the
model's path strings, with six methods; `ToolContext.workspace` is a `Workspace`.

```python
class Workspace(Protocol):
    async def stat(self, path: str) -> Literal["file", "dir"] | None
    async def read_bytes(self, path: str) -> bytes
    async def list_dir(self, path: str) -> list[DirEntry]
    async def update_file[T](self, path: str, change: Callable[[bytes | None], tuple[bytes, T]]) -> T
    def ripgrep(self, args: Sequence[str], *, cwd: str) -> AsyncGenerator[str]
    async def exec(self, command: str, *, timeout: float | None, output: Path) -> int
```

- **Paths are the model's strings.** The workspace resolves them and refuses
  anything outside (`ToolError "… is outside the workspace."`), so each backend
  checks paths where the files are. The read methods also reach the read-only
  roots (skill folders, ADR-016); `ToolContext.read_roots` moved into
  `LocalWorkspace`.
- **`update_file` replaces `write_bytes` and the lock.** It reads the file,
  calls `change(old_bytes_or_None)`, writes what it returns, and holds the
  file's lock throughout. `write` ignores the old bytes; `edit` applies its
  replacements. A `change` that raises writes nothing.
- **`ripgrep` streams lines.** The tools still own rg's arguments
  (`tools/ripgrep.py`) and the output shaping; the backend owns finding rg,
  running it in `cwd`, killing it when the caller stops early, and mapping
  exit codes (1 = no match, others = `ToolError`).
- **`exec` writes to a local file.** `bash` keeps its log, live updates and
  truncation; the backend owns the process (env allowlist, process group kill,
  timeout). A sandbox backend copies output into the file as it arrives.
- **Error messages live in the backend** ("File not found: …", "Not a
  directory: …"), worded the same for every backend, so `read` and `ls` need one
  call, not a `stat` first.
- **MCP keeps a `Path`** (`McpManager.tools_for`) until step 60 moves it.

## Alternatives considered
- **ADR-004's `write_bytes` plus a `lock(path)` method** — closer to the sketch,
  but every writer has to remember to take the lock, and the lock leaks into the
  interface. `update_file` makes the safe way the only way.
- **pi's per-tool operations** — each tool gets exactly what it needs, but that
  is seven interfaces a sandbox backend must implement instead of one.
- **`grep`/`find` through `exec`** — no `ripgrep` method, but they would lose
  early stopping and parse a shell-quoted command line, and every backend would
  build that quoting.
- **Paths as `Path` objects resolved by the tools** — what we had; it ties path
  safety to the host's filesystem, which a sandbox doesn't share.

## Consequences
- A sandbox backend (step 28) implements six methods and no tool changes. Its
  tests can be the same as `tests/test_workspace.py`, which only uses the
  protocol.
- The per-file lock is still in-process. That is enough while one server owns
  a session's run (ADR-029); a sandbox backend keeps the same lock on the
  server that owns the run.
- `read` and `edit` now read the whole file into memory, as before; a size cap
  is still to do.
- `bash`'s "full output" file is still on our server, outside the workspace,
  so the model can't `read` it. Unchanged from before; step 28 should put it
  in the sandbox.
- ADR-004 stays Proposed for the sandbox parts. Its interface sketch is
  replaced by this one.
