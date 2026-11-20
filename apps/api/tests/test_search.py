import shutil
from pathlib import Path

import pytest

from agentspace.agent.tool import ToolContext, ToolError, ToolResult
from agentspace.ai.messages import joined_text
from agentspace.tools.find import FindParams, FindTool
from agentspace.tools.grep import MAX_LINE_CHARS, GrepParams, GrepTool
from agentspace.tools.ls import LsParams, LsTool

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) is not installed"),
]


@pytest.fixture
def ctx(tmp_path: Path) -> ToolContext:
    """A workspace with nested, hidden, ignored and git-internal files."""
    ws = tmp_path / "ws"
    for rel, content in {
        "src/a.ts": "hello world\nbye\n",
        "src/sub/b.ts": "one\nhello there\nthree\n",
        "Notes.md": "HELLO caps\n",
        ".hidden/c.ts": "hello hidden\n",
        "node_modules/x.ts": "hello ignored\n",
        ".git/config": "hello git\n",
        ".gitignore": "node_modules/\n",
    }.items():
        path = ws / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (ws / "outlink").symlink_to(tmp_path)
    (tmp_path / "secret.txt").write_text("hello secret\n")
    return ToolContext(workspace=ws)


def text(result: ToolResult) -> str:
    return joined_text(result.content)


async def grep(ctx: ToolContext, pattern: str, **kwargs: object) -> ToolResult:
    return await GrepTool().execute(GrepParams.model_validate({"pattern": pattern, **kwargs}), ctx)


async def find(ctx: ToolContext, pattern: str, **kwargs: object) -> ToolResult:
    return await FindTool().execute(FindParams.model_validate({"pattern": pattern, **kwargs}), ctx)


async def test_grep_skips_ignored_git_and_symlinked_files(ctx: ToolContext) -> None:
    result = await grep(ctx, "hello")
    assert text(result) == ".hidden/c.ts:1: hello hidden\nsrc/a.ts:1: hello world\nsrc/sub/b.ts:2: hello there"
    assert result.details == {"matches": 3, "limit_reached": False, "truncated": False}


async def test_grep_options(ctx: ToolContext) -> None:
    assert text(await grep(ctx, "hello", ignore_case=True, glob="*.md")) == "Notes.md:1: HELLO caps"
    with_context = await grep(ctx, "hello", path="src/sub", context=1)
    assert text(with_context) == "b.ts-1- one\nb.ts:2: hello there\nb.ts-3- three"
    assert text(await grep(ctx, "hello", path="src/a.ts")) == "a.ts:1: hello world"
    assert text(await grep(ctx, "./src/**", glob="./src/**")) == "No matches found"
    assert text(await grep(ctx, "h.llo w", literal=True)) == "No matches found"


async def test_grep_limit_keeps_trailing_context(ctx: ToolContext) -> None:
    result = await grep(ctx, "hello", path="src", limit=1, context=1)
    assert text(result) == (
        "a.ts:1: hello world\na.ts-2- bye\n\n[1 matches limit reached. Use limit=2 for more, or refine pattern]"
    )
    assert result.details is not None and result.details["limit_reached"]


async def test_grep_cuts_long_lines(ctx: ToolContext) -> None:
    (ctx.workspace / "long.txt").write_text("needle" + "x" * 1000 + "\n")
    out = text(await grep(ctx, "needle"))
    assert out.startswith("long.txt:1: needle") and f"truncated to {MAX_LINE_CHARS} chars" in out


async def test_grep_errors(ctx: ToolContext) -> None:
    with pytest.raises(ToolError, match="regex parse error"):
        await grep(ctx, "(")
    with pytest.raises(ToolError, match="Path not found"):
        await grep(ctx, "x", path="nope")
    with pytest.raises(ToolError, match="outside the workspace"):
        await grep(ctx, "x", path="..")


async def test_find_globs(ctx: ToolContext) -> None:
    assert text(await find(ctx, "*.ts")) == ".hidden/c.ts\nsrc/a.ts\nsrc/sub/b.ts"
    assert text(await find(ctx, "./src/**/*.ts")) == "src/a.ts\nsrc/sub/b.ts"
    assert text(await find(ctx, "*.ts", path="src")) == "a.ts\nsub/b.ts"
    # A catch-all glob still keeps .git/ out.
    assert ".git/config" not in text(await find(ctx, "*"))
    assert text(await find(ctx, "*.py")) == "No files found matching pattern"


async def test_find_limit(ctx: ToolContext) -> None:
    result = await find(ctx, "*.ts", limit=2)
    assert text(result).startswith(".hidden/c.ts\nsrc/a.ts\n\n[2 results limit reached.")
    assert result.details == {"count": 2, "limit_reached": True, "truncated": False}
    with pytest.raises(ToolError, match="Not a directory"):
        await find(ctx, "*", path="src/a.ts")


async def test_ls(ctx: ToolContext) -> None:
    out = text(await LsTool().execute(LsParams(), ctx))
    assert out == ".git/\n.gitignore\n.hidden/\nnode_modules/\nNotes.md\noutlink/\nsrc/"
    limited = await LsTool().execute(LsParams(limit=2), ctx)
    assert text(limited) == ".git/\n.gitignore\n\n[2 entries limit reached. Use limit=4 for more]"
    (ctx.workspace / "empty").mkdir()
    assert text(await LsTool().execute(LsParams(path="empty"), ctx)) == "(empty directory)"
    with pytest.raises(ToolError, match="outside the workspace"):
        await LsTool().execute(LsParams(path="outlink"), ctx)
