"""A sandbox per session: get-or-create by name, wake when asleep, copy the skills in (ADR-035).

The vendor sits behind the `Sandboxes` port (Daytona's adapter: `workspace/daytona.py`), so the
lifecycle rules (one sandbox per session even when two servers race, a crash halfway is adopted
on the next try) are tested with a fake.
"""

import hashlib
import shlex
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from asyncer import asyncify

from agentspace.agent.skills import Skill
from agentspace.agent.tool import ToolError
from agentspace.workspace.sandbox import Box, OpenBox

# Written next to a copied skill: the hash of the server's copy, so an unchanged skill isn't copied again.
SKILL_HASH_FILE = ".agentspace-hash"


# Outbound network of a sandbox (ADR-035):
# - "vendor_default": the vendor's own policy. Daytona's Tiers 1–2 allow only essential services (package
#   registries, git hosts, model providers) and refuse any override; Tiers 3–4 allow everything.
# - "allow_list": only `allowed_domains` (Daytona Tier 3+).
# - "blocked": nothing.
type SandboxNetwork = Literal["vendor_default", "allow_list", "blocked"]


@dataclass(frozen=True)
class SandboxSpec:
    """How new sandboxes are made: idle timers and the network policy."""

    auto_stop_minutes: int
    auto_archive_minutes: int
    network: SandboxNetwork
    allowed_domains: tuple[str, ...] = ()
    """With `allow_list`, the only sites a sandbox can reach."""


@dataclass(frozen=True)
class Found:
    """A sandbox that exists, and whether it is running right now."""

    box: Box
    running: bool


class SandboxNameTakenError(Exception):
    """A sandbox with this name already exists (another server, or an earlier attempt, made it)."""


class Sandboxes(Protocol):
    """The vendor's sandboxes, by the name we give them."""

    home: str
    """The sandbox user's home folder; the workspace and the skill copies live under it."""

    async def find(self, name: str) -> Found | None:
        """The sandbox called `name`, or None."""
        ...

    async def create(self, name: str, labels: Mapping[str, str]) -> Box:
        """A new running sandbox; raises `SandboxNameTakenError` if the name is taken."""
        ...

    async def start(self, name: str) -> Box:
        """Wake the sandbox called `name` (stopped, archived, or already waking) and return it running."""
        ...


# TS: type WorkspaceStatus = "starting" | "ready" | "failed"
type WorkspaceStatus = Literal["starting", "ready", "failed"]
# Told when the sandbox has to be created or woken, and how that ended, with a reason on failure.
type OnStatus = Callable[[WorkspaceStatus, str | None], Awaitable[None]]


def sandbox_name(prefix: str, session_id: str) -> str:
    """The sandbox's name, the one key that finds it again (ADR-035)."""
    return f"{prefix}-{session_id}"


def workspace_folder(home: str) -> str:
    """The session's files, inside its sandbox."""
    return f"{home}/workspace"


def skills_folder(home: str) -> str:
    """Where the skills are copied, inside the sandbox; outside the workspace, so the model can't change them there."""
    return f"{home}/.agentspace/skills"


async def ensure(
    sandboxes: Sandboxes, name: str, labels: Mapping[str, str], on_starting: Callable[[], Awaitable[None]]
) -> Box:
    """The running sandbox called `name`, created or woken if needed; safe to call again after any failure.

    `on_starting` runs once, before a create or a wake, so the user knows why they wait.
    """
    found = await sandboxes.find(name)
    if found is not None and found.running:
        return found.box
    await on_starting()
    if found is None:
        try:
            return await sandboxes.create(name, labels)
        except SandboxNameTakenError:
            # Two prompts raced, or a crash left one behind: adopt it rather than make a second.
            found = await sandboxes.find(name)
            if found is None:
                raise ToolError("The sandbox was being created and then disappeared. Try again.") from None
            if found.running:
                return found.box
    return await sandboxes.start(name)


def session_box(
    sandboxes: Sandboxes,
    *,
    name: str,
    labels: Mapping[str, str],
    skills: Sequence[Skill],
    on_status: OnStatus,
) -> OpenBox:
    """How a run opens its session's sandbox: ensure it runs, make the folders, copy the skills in."""
    root, skills_root = workspace_folder(sandboxes.home), skills_folder(sandboxes.home)

    async def open_box() -> Box:
        """The ready sandbox; reports `starting` / `ready` / `failed` only when it had to wait."""
        started = False

        async def starting() -> None:
            """Note that this call waits on a create or wake, and say so."""
            nonlocal started  # TS: assigns the outer `let started`, which Python otherwise shadows
            started = True
            await on_status("starting", None)

        try:
            box = await ensure(sandboxes, name, labels, starting)
            await _check(box, f"mkdir -p {shlex.quote(root)} {shlex.quote(skills_root)}", "make the workspace folder")
            await sync_skills(box, skills, skills_root)
        except ToolError as exc:
            await on_status("failed", str(exc))
            raise
        if started:
            await on_status("ready", None)
        return box

    return open_box


def sandbox_skill(skill: Skill, skills_root: str) -> Skill:
    """The skill as the model sees it in a sandbox: the same files, at their copied location."""
    base = Path(skills_root) / skill.name
    # TS: `replace` copies a frozen dataclass with some fields changed, like `{ ...skill, base_dir, file_path }`.
    return replace(skill, file_path=base / skill.file_path.relative_to(skill.base_dir), base_dir=base)


async def sync_skills(box: Box, skills: Sequence[Skill], skills_root: str) -> None:
    """Copy each skill folder into `skills_root` in the sandbox, skipping one whose copy is already current."""
    for skill in skills:
        files, digest = await asyncify(_snapshot)(skill.base_dir)
        target = f"{skills_root}/{skill.name}"
        marker = f"{target}/{SKILL_HASH_FILE}"
        if await box.kind(marker) == "file" and (await box.download(marker)).decode() == digest:
            continue
        # Start clean, so a file removed from the skill doesn't linger in the copy.
        await _check(box, f"rm -rf {shlex.quote(target)}", "replace a skill")
        for relative, data in files:
            await box.upload(f"{target}/{relative}", data)
        await box.upload(marker, digest.encode())  # last: a copy cut off halfway is redone next run


def _snapshot(folder: Path) -> tuple[list[tuple[str, bytes]], str]:
    """Every file under `folder` (relative path, bytes) and one hash over all of them."""
    # shortcut: reads whole skill folders into memory each run; fine for text skills, cap if big assets appear.
    files = sorted((p.relative_to(folder).as_posix(), p.read_bytes()) for p in folder.rglob("*") if p.is_file())
    digest = hashlib.sha256()
    for relative, data in files:
        digest.update(relative.encode() + b"\0" + hashlib.sha256(data).digest())
    return files, digest.hexdigest()


async def _check(box: Box, command: str, what: str) -> None:
    """Run a housekeeping command; a non-zero exit becomes a `ToolError` naming what failed."""
    errors: list[str] = []
    code = await box.run(command, cwd="/", on_stdout=lambda _chunk: None, on_stderr=errors.append)
    if code != 0:
        raise ToolError(f"Couldn't {what} in the sandbox: {''.join(errors).strip() or f'exit {code}'}")
