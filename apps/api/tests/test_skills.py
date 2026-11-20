from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentspace.agent.prompt import build_system_prompt
from agentspace.agent.skills import Skill, load_skills
from agentspace.agent.tool import ToolContext, ToolError
from agentspace.ai.messages import ToolCall, joined_text
from agentspace.ai.providers.fake import FakeProvider, text_reply, tool_reply
from agentspace.api.deps import provider_lookup
from agentspace.config import Settings
from agentspace.main import create_app
from agentspace.tools import TOOL_SETS, build_tools
from agentspace.tools.ls import LsParams, LsTool
from agentspace.tools.read import ReadParams, ReadTool
from agentspace.tools.write import WriteParams, WriteTool


def make_skill(folder: Path, frontmatter: str, body: str = "Do the thing.\n") -> Path:
    """Write a SKILL.md with the given frontmatter lines into `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}")
    return path


def test_load_skills_reads_frontmatter_and_skips_bad_ones(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    pdf = make_skill(root / "pdf-tools", "name: pdf-tools\ndescription: Read PDFs. Use for any .pdf file.")
    # Nested one level down, no name: the folder name stands in.
    make_skill(root / "group" / "review", "description: Review code.")
    make_skill(root / "no-description", "name: no-description")
    (root / "broken").mkdir()
    (root / "broken" / "SKILL.md").write_text("no frontmatter here")
    make_skill(root / "zz-dupe", "name: pdf-tools\ndescription: A second pdf-tools.")
    make_skill(root / ".hidden", "description: Hidden.")
    make_skill(root / "node_modules" / "dep", "description: A dependency's skill.")
    # A skill inside a skill's folder is part of it, not a second skill.
    make_skill(root / "pdf-tools" / "inner", "description: Inner.")

    skills, diagnostics = load_skills(root)

    assert [(s.name, s.description) for s in skills] == [
        ("review", "Review code."),
        ("pdf-tools", "Read PDFs. Use for any .pdf file."),
    ]
    assert skills[1].file_path == pdf.resolve() and skills[1].base_dir == pdf.parent.resolve()
    assert any("no-description" in d and "no description" in d for d in diagnostics)
    assert any("broken" in d and "frontmatter" in d for d in diagnostics)
    assert any("already taken" in d for d in diagnostics)


def test_load_skills_warns_about_bad_names_but_keeps_the_skill(tmp_path: Path) -> None:
    make_skill(tmp_path / "x", "name: Bad_Name\ndescription: Still loads.")
    skills, diagnostics = load_skills(tmp_path)
    assert [s.name for s in skills] == ["Bad_Name"]
    assert any("lowercase" in d for d in diagnostics)


def test_load_skills_follows_symlinked_folders_and_survives_loops(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere" / "linked"
    make_skill(real, "description: Linked in.")
    root = tmp_path / "skills"
    root.mkdir()
    (root / "linked").symlink_to(real)
    (root / "loop").symlink_to(root)
    skills, _ = load_skills(root)
    # The location is the real file, so the read-only root matches what `read` resolves to.
    assert [(s.name, s.base_dir) for s in skills] == [("linked", real.resolve())]


def test_missing_skills_folder_means_no_skills(tmp_path: Path) -> None:
    assert load_skills(tmp_path / "nope") == ([], [])


def skill(tmp_path: Path, description: str = "Use <this> & 'that'.") -> Skill:
    """One skill, for prompt tests."""
    path = (tmp_path / "s" / "SKILL.md").resolve()
    return Skill(name="s", description=description, file_path=path, base_dir=path.parent)


def test_prompt_lists_skills_with_escaped_values(tmp_path: Path) -> None:
    prompt = build_system_prompt("p", build_tools(TOOL_SETS["read_only"], bash=False), [skill(tmp_path)])
    body = prompt.split("<skills>\n")[1].split("\n</skills>")[0]
    assert "Use the read tool to load a skill's file" in body
    assert "<description>Use &lt;this&gt; &amp; &apos;that&apos;.</description>" in body
    assert f"<location>{tmp_path.resolve() / 's' / 'SKILL.md'}</location>" in body
    assert "Skill folders listed in <skills> can be read, but not changed." in prompt
    # The skill's body never goes into the prompt.
    assert "Do the thing" not in prompt


def test_prompt_skills_need_a_tool_that_can_read_them(tmp_path: Path) -> None:
    only_bash = build_system_prompt("p", build_tools(["bash"], bash=True), [skill(tmp_path)])
    assert "Use the bash tool to load" in only_bash
    for tools, skills in (([], [skill(tmp_path)]), (build_tools(["read"], bash=False), [])):
        prompt = build_system_prompt("p", tools, skills)
        assert "<skills>" not in prompt and "Skill folders" not in prompt


@pytest.fixture
def skill_ctx(tmp_path: Path) -> ToolContext:
    """A workspace, plus one skill folder outside it that tools may read."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    make_skill(tmp_path / "skills" / "pdf", "description: PDFs.", body="Step one.\n")
    (tmp_path / "skills" / "pdf" / "notes.md").write_text("ref")
    (tmp_path / "skills" / "secret.txt").write_text("not a skill")
    return ToolContext(workspace=workspace, read_roots=((tmp_path / "skills" / "pdf").resolve(),))


@pytest.mark.anyio
async def test_skill_folders_are_readable_but_not_writable(skill_ctx: ToolContext, tmp_path: Path) -> None:
    folder = tmp_path / "skills" / "pdf"
    result = await ReadTool().execute(ReadParams(path=str(folder / "SKILL.md")), skill_ctx)
    assert "Step one." in joined_text(result.content)
    listing = await LsTool().execute(LsParams(path=str(folder)), skill_ctx)
    assert "notes.md" in joined_text(listing.content)

    with pytest.raises(ToolError, match="outside the workspace"):
        await WriteTool().execute(WriteParams(path=str(folder / "SKILL.md"), content="x"), skill_ctx)
    # Only the skill's own folder opens: not files beside it, nor relative paths climbing out.
    with pytest.raises(ToolError, match="outside the workspace"):
        await ReadTool().execute(ReadParams(path=str(tmp_path / "skills" / "secret.txt")), skill_ctx)
    with pytest.raises(ToolError, match="outside the workspace"):
        await ReadTool().execute(ReadParams(path="../skills/pdf/SKILL.md"), skill_ctx)
    with pytest.raises(ToolError, match="outside the workspace"):
        await ReadTool().execute(ReadParams(path=str(folder / ".." / "secret.txt")), skill_ctx)


def test_a_run_lists_skills_and_the_model_reads_one(tmp_path: Path) -> None:
    location = make_skill(tmp_path / "skills" / "pdf-tools", "description: Read PDFs.", body="Use pdftotext.\n")
    fake = FakeProvider([])
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    app.dependency_overrides[provider_lookup] = lambda: lambda name: fake
    with TestClient(app) as client:
        session_id = client.post("/sessions", json={}).json()["id"]
        fake.replies = [
            tool_reply(ToolCall(id="c1", name="read", arguments={"path": str(location.resolve())})),
            text_reply("Done."),
        ]
        client.post(f"/sessions/{session_id}/messages", json={"content": "Read my pdf"})

        assert f"<location>{location.resolve()}</location>" in fake.contexts[0].system_prompt
        tool_result = client.get(f"/sessions/{session_id}").json()["messages"][2]
        assert tool_result["is_error"] is False and "Use pdftotext." in tool_result["content"][0]["text"]


def test_skill_routes_list_and_read_fresh(tmp_path: Path) -> None:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    with TestClient(app) as client:
        empty = client.get("/skills").json()
        assert empty == {
            "skills_dir": str((tmp_path / "skills").resolve()),
            "skills": [],
            "diagnostics": [],
            "listing_tokens": 0,
        }

        # Added while the app runs: the next request sees it, with no restart.
        location = make_skill(tmp_path / "skills" / "pdf-tools", "description: Read PDFs.", body="# PDF\n\nStep.\n")
        make_skill(tmp_path / "skills" / "broken", "name: broken")
        listing = client.get("/skills").json()
        [entry] = listing["skills"]
        assert entry["name"] == "pdf-tools" and entry["location"] == str(location.resolve())
        assert entry["tokens"] == len(location.read_text()) // 4
        assert listing["listing_tokens"] > 0
        assert any("no description" in d for d in listing["diagnostics"])

        detail = client.get("/skills/pdf-tools").json()
        assert detail["content"] == "# PDF\n\nStep.\n" and detail["name"] == "pdf-tools"
        assert detail["prompt_entry"].startswith("  <skill>\n    <name>pdf-tools</name>")
        assert detail["files"] == ["SKILL.md"]
        # Only loaded skills by name: no paths, no skipped skills.
        assert client.get("/skills/broken").status_code == 404
        assert client.get("/skills/..%2F..%2Fagentspace.db").status_code == 404


def test_skill_file_route_serves_only_text_files_inside_the_skill(tmp_path: Path) -> None:
    folder = tmp_path / "skills" / "pdf-tools"
    make_skill(folder, "description: Read PDFs.")
    (folder / "references").mkdir()
    (folder / "references" / "formats.md").write_text("# Formats")
    (folder / "logo.png").write_bytes(b"\x89PNG\0\0")
    (folder / ".secret").write_text("hidden")
    (tmp_path / "agentspace-secret.txt").write_text("outside")
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    with TestClient(app) as client:
        assert client.get("/skills/pdf-tools").json()["files"] == ["SKILL.md", "logo.png", "references/formats.md"]
        file = client.get("/skills/pdf-tools/files/references/formats.md").json()
        assert file == {"path": "references/formats.md", "content": "# Formats"}
        assert client.get("/skills/pdf-tools/files/logo.png").status_code == 404
        assert client.get("/skills/pdf-tools/files/missing.md").status_code == 404
        assert client.get("/skills/pdf-tools/files/..%2F..%2Fagentspace-secret.txt").status_code == 404
