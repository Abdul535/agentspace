"""HTTP routes for the skills on the shared shelf: list them, read one, or read a file inside one (ADR-016).

Each rescans `skills_dir` on every request, as runs do, so the web app is never staler than its last fetch.
"""

from asyncer import asyncify
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from agentspace.agent.skills import (
    Skill,
    format_skill_entry,
    format_skills_for_prompt,
    list_skill_files,
    load_skills,
    read_skill_body,
    read_skill_file,
)
from agentspace.api.deps import SettingsDep
from agentspace.config import Settings

router = APIRouter(prefix="/skills", tags=["skills"])

# Rough characters per token, as compaction estimates (ADR-009); good enough for "about how big".
CHARS_PER_TOKEN = 4


# TS: a Pydantic model is a zod schema and its type in one; FastAPI validates and documents responses with it.
class SkillPublic(BaseModel):
    """What the model sees of a skill in its system prompt, plus its size."""

    name: str
    description: str
    location: str
    """The absolute path of its `SKILL.md`."""
    tokens: int
    """About how many tokens reading its `SKILL.md` adds to the model's context."""


class SkillList(BaseModel):
    """Every skill a run would list, plus problems with the ones it skipped or would warn about."""

    skills_dir: str
    """Where the skills live; shown when there are none, so the user knows where to put one."""
    skills: list[SkillPublic]
    diagnostics: list[str]
    listing_tokens: int
    """About how many tokens the `<skills>` section costs on every model call."""


class SkillDetail(SkillPublic):
    """One skill with its instructions and what else its folder holds."""

    content: str
    """`SKILL.md` without its frontmatter, as Markdown."""
    prompt_entry: str
    """Its `<skill>` element, exactly as the system prompt lists it."""
    files: list[str]
    """Files in its folder, relative to it, `SKILL.md` first."""


class SkillFile(BaseModel):
    """A text file inside a skill's folder."""

    path: str
    content: str


def public(skill: Skill) -> SkillPublic:
    """The listing fields of a skill."""
    return SkillPublic(
        name=skill.name,
        description=skill.description,
        location=str(skill.file_path),
        tokens=skill.size // CHARS_PER_TOKEN,
    )


async def find_skill(name: str, settings: Settings) -> Skill:
    """The loaded skill with this name, or 404. Looked up by name only, so a request can't name an arbitrary path."""
    skills, _ = await asyncify(load_skills)(settings.resolved_skills_dir)
    skill = next((s for s in skills if s.name == name), None)
    if skill is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No skill named '{name}'.")
    return skill


@router.get("")
async def list_skills(settings: SettingsDep) -> SkillList:
    """The skills a run started now would see, sorted by name."""
    root = settings.resolved_skills_dir
    skills, diagnostics = await asyncify(load_skills)(root)
    skills.sort(key=lambda s: s.name)
    listing = format_skills_for_prompt(skills, "read") if skills else ""
    return SkillList(
        skills_dir=str(root.resolve()),
        # TS: a list comprehension, like skills.map(public)
        skills=[public(skill) for skill in skills],
        diagnostics=diagnostics,
        listing_tokens=len(listing) // CHARS_PER_TOKEN,
    )


@router.get("/{name}")
async def get_skill(name: str, settings: SettingsDep) -> SkillDetail:
    """One skill, read fresh: its instructions, prompt entry and files."""
    skill = await find_skill(name, settings)
    try:
        content = await asyncify(read_skill_body)(skill)
        files = await asyncify(list_skill_files)(skill)
    except (OSError, UnicodeDecodeError) as exc:
        # Removed or broken between the scan and the read.
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Can't read skill '{name}': {exc}") from exc
    return SkillDetail(
        **public(skill).model_dump(), content=content, prompt_entry=format_skill_entry(skill), files=files
    )


# TS: `{path:path}` is a catch-all segment, like Next's [...path]: it keeps the slashes.
@router.get("/{name}/files/{path:path}")
async def get_skill_file(name: str, path: str, settings: SettingsDep) -> SkillFile:
    """A text file inside the skill's folder; 404 for anything outside it, missing, binary or over 1 MB."""
    skill = await find_skill(name, settings)
    try:
        content = await asyncify(read_skill_file)(skill, path)
    except (OSError, ValueError) as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return SkillFile(path=path, content=content)
