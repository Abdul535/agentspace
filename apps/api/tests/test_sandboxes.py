"""A sandbox per session: get-or-create, races, wake-ups, failures, status and skill copies (ADR-035).

Against a fake vendor (`FakeSandboxes`) whose boxes are `HostBox`es, so it runs offline.
"""

import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest
from host_box import HostBox

from agentspace.agent.skills import Skill
from agentspace.agent.tool import ToolError
from agentspace.workspace.sandbox import Box, SandboxWorkspace
from agentspace.workspace.sandboxes import (
    SKILL_HASH_FILE,
    Found,
    SandboxNameTakenError,
    WorkspaceStatus,
    ensure,
    sandbox_skill,
    session_box,
    sync_skills,
)

pytestmark = pytest.mark.anyio


class FakeSandboxes:
    """In-memory vendor: names to boxes, with a running flag, and counters for what was asked."""

    def __init__(self, home: str = "/nonexistent-home") -> None:
        """No sandboxes yet; `home` holds the workspace of every box (a host folder, since boxes are `HostBox`es)."""
        self.home = home
        self.boxes: dict[str, HostBox] = {}
        self.running: dict[str, bool] = {}
        self.labels: dict[str, Mapping[str, str]] = {}
        self.creates = 0
        self.starts = 0
        self.fail_create: Exception | None = None
        """Raised by the next create, once."""
        self.race_on_create = False
        """Another server creates the sandbox just before ours, so ours is refused."""
        self.create_gate: asyncio.Event | None = None
        """When set, creates wait for it: two calls can then be in flight at once."""

    async def find(self, name: str) -> Found | None:
        """The box called `name`."""
        if name not in self.boxes:
            return None
        return Found(self.boxes[name], running=self.running[name])

    async def create(self, name: str, labels: Mapping[str, str]) -> Box:
        """A new running box, unless the name is taken (or a failure was asked for)."""
        self.creates += 1
        if self.create_gate is not None:
            await self.create_gate.wait()
        if self.fail_create is not None:
            error, self.fail_create = self.fail_create, None
            raise error
        if self.race_on_create:
            self.race_on_create = False
            self._add(name, labels, running=True)
        if name in self.boxes:
            raise SandboxNameTakenError(name)
        return self._add(name, labels, running=True)

    async def start(self, name: str) -> Box:
        """Wake the box."""
        self.starts += 1
        self.running[name] = True
        return self.boxes[name]

    def _add(self, name: str, labels: Mapping[str, str], *, running: bool) -> HostBox:
        """Record a box."""
        self.boxes[name] = HostBox(name)
        self.running[name] = running
        self.labels[name] = labels
        return self.boxes[name]


class Statuses:
    """Collects what `on_status` was told."""

    def __init__(self) -> None:
        """Nothing yet."""
        self.seen: list[tuple[WorkspaceStatus, str | None]] = []

    async def __call__(self, status: WorkspaceStatus, error: str | None) -> None:
        """Record one report."""
        self.seen.append((status, error))


async def nothing() -> None:
    """An `on_starting` that does nothing."""


async def test_a_new_session_gets_a_sandbox_created_once() -> None:
    vendor = FakeSandboxes()
    first = await ensure(vendor, "agentspace-s1", {"agentspace.session": "s1"}, nothing)
    second = await ensure(vendor, "agentspace-s1", {"agentspace.session": "s1"}, nothing)
    assert first is second
    assert vendor.creates == 1
    assert vendor.labels["agentspace-s1"] == {"agentspace.session": "s1"}


async def test_a_stopped_sandbox_is_woken_not_replaced() -> None:
    vendor = FakeSandboxes()
    box = await ensure(vendor, "n", {}, nothing)
    vendor.running["n"] = False  # Daytona's auto-stop
    assert await ensure(vendor, "n", {}, nothing) is box
    assert (vendor.creates, vendor.starts) == (1, 1)


async def test_a_name_taken_by_another_server_is_adopted() -> None:
    vendor = FakeSandboxes()
    vendor.race_on_create = True
    box = await ensure(vendor, "n", {}, nothing)
    assert box is vendor.boxes["n"]
    assert vendor.creates == 1  # ours was refused, and none was made after it


async def test_two_prompts_at_once_share_one_sandbox() -> None:
    vendor = FakeSandboxes()
    vendor.create_gate = asyncio.Event()
    first = asyncio.create_task(ensure(vendor, "n", {}, nothing))
    second = asyncio.create_task(ensure(vendor, "n", {}, nothing))
    await asyncio.sleep(0.01)  # both have looked, found nothing, and are creating
    vendor.create_gate.set()
    boxes = await asyncio.gather(first, second)
    assert boxes[0] is boxes[1]
    assert len(vendor.boxes) == 1


async def test_on_starting_is_told_only_when_there_is_a_wait() -> None:
    vendor = FakeSandboxes()
    calls: list[str] = []

    async def starting() -> None:
        calls.append("starting")

    await ensure(vendor, "n", {}, starting)  # create
    await ensure(vendor, "n", {}, starting)  # already running: no wait
    vendor.running["n"] = False
    await ensure(vendor, "n", {}, starting)  # wake
    assert calls == ["starting", "starting"]


def workspace_on(vendor: FakeSandboxes, statuses: Statuses, tmp_path: Path, skills: list[Skill]) -> SandboxWorkspace:
    """A session's `SandboxWorkspace` over the fake vendor, its folders under `tmp_path`."""
    vendor.home = str(tmp_path)
    open_box = session_box(vendor, name="n", labels={}, skills=skills, on_status=statuses)
    return SandboxWorkspace(open_box, root=str(tmp_path), default_timeout=5)


async def test_the_sandbox_opens_on_the_first_tool_call_and_reports_starting_then_ready(tmp_path: Path) -> None:
    vendor, statuses = FakeSandboxes(), Statuses()
    ws = workspace_on(vendor, statuses, tmp_path, [])
    assert vendor.creates == 0  # nothing until a tool needs it: a chat-only run never pays for a sandbox
    await asyncio.gather(ws.stat("."), ws.stat("."))  # parallel tool calls wait for one opening
    assert vendor.creates == 1
    assert statuses.seen == [("starting", None), ("ready", None)]
    await ws.stat(".")
    assert len(statuses.seen) == 2  # opened once per run


async def test_a_failed_start_is_reported_and_the_next_tool_call_tries_again(tmp_path: Path) -> None:
    vendor, statuses = FakeSandboxes(), Statuses()
    vendor.fail_create = ToolError("The sandbox failed (create sandbox): DaytonaRateLimitError. Try again.")
    ws = workspace_on(vendor, statuses, tmp_path, [])
    with pytest.raises(ToolError, match="RateLimit"):
        await ws.stat(".")
    assert statuses.seen[-1] == ("failed", "The sandbox failed (create sandbox): DaytonaRateLimitError. Try again.")
    assert await ws.stat(".") == "dir"  # not stuck on the failure
    assert statuses.seen[-1] == ("ready", None)


async def test_a_command_without_a_timeout_gets_the_default_limit(tmp_path: Path) -> None:
    vendor, statuses = FakeSandboxes(str(tmp_path)), Statuses()
    open_box = session_box(vendor, name="n", labels={}, skills=[], on_status=statuses)
    ws = SandboxWorkspace(open_box, root=str(tmp_path), default_timeout=0.5)
    with pytest.raises(TimeoutError):
        await ws.exec("sleep 5", timeout=None, output=tmp_path / "out.log")


def make_skill(root: Path, name: str, files: dict[str, str]) -> Skill:
    """A skill folder on the server with these files (SKILL.md among them)."""
    folder = root / name
    for relative, text in files.items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_text(text)
    return Skill(name=name, description="d", file_path=folder / "SKILL.md", base_dir=folder)


async def test_skills_are_copied_in_and_an_unchanged_one_is_not_copied_again(tmp_path: Path) -> None:
    sandbox_skills = tmp_path / "box-skills"
    skill = make_skill(tmp_path / "server", "pdf", {"SKILL.md": "how", "scripts/run.sh": "echo hi"})
    box = HostBox()
    await sync_skills(box, [skill], str(sandbox_skills))
    assert (sandbox_skills / "pdf/SKILL.md").read_text() == "how"
    assert (sandbox_skills / "pdf/scripts/run.sh").read_text() == "echo hi"
    assert (sandbox_skills / "pdf" / SKILL_HASH_FILE).exists()

    box.commands.clear()
    await sync_skills(box, [skill], str(sandbox_skills))
    assert box.commands == []  # unchanged: nothing removed or copied

    (skill.base_dir / "scripts/run.sh").unlink()
    (skill.base_dir / "SKILL.md").write_text("how, v2")
    await sync_skills(box, [skill], str(sandbox_skills))
    assert (sandbox_skills / "pdf/SKILL.md").read_text() == "how, v2"
    assert not (sandbox_skills / "pdf/scripts/run.sh").exists()  # a removed file doesn't linger


async def test_the_prompt_points_at_the_skills_copied_location(tmp_path: Path) -> None:
    skill = make_skill(tmp_path, "pdf", {"SKILL.md": "how"})
    copied = sandbox_skill(skill, "/home/daytona/.agentspace/skills")
    assert str(copied.file_path) == "/home/daytona/.agentspace/skills/pdf/SKILL.md"
    assert str(copied.base_dir) == "/home/daytona/.agentspace/skills/pdf"
    assert (copied.name, copied.description) == (skill.name, skill.description)
