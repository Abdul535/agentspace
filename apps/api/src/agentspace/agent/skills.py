"""Finds skill folders (`SKILL.md`) and lists them for the system prompt, after pi's `core/skills.ts` (ADR-016)."""

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml

logger = logging.getLogger(__name__)

SKILL_FILE = "SKILL.md"
MAX_NAME_LENGTH = 64
MAX_DESCRIPTION_LENGTH = 1024
# Lowercase letters and digits, joined by single hyphens: the Agent Skills naming rule.
NAME_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")
# The YAML between the opening `---` line and the next one.
FRONTMATTER = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    """One skill: what the prompt shows of it, and where its files are."""

    name: str
    description: str
    file_path: Path
    """The resolved, absolute path of its `SKILL.md`."""
    base_dir: Path
    """The resolved folder holding `SKILL.md`; the model may read anything inside it."""
    size: int = 0
    """Characters in `SKILL.md`: what loading it costs the model's context (≈ size / 4 tokens)."""


def load_skills(root: Path) -> tuple[list[Skill], list[str]]:
    """Every valid skill under `root`, plus diagnostics for the ones skipped or questionable.

    A folder holding `SKILL.md` is one skill and is not searched further. Dot-folders and
    `node_modules` are skipped; symlinked folders are followed. On a name clash the first wins.
    """
    skills: list[Skill] = []
    diagnostics: list[str] = []
    if root.is_dir():
        _walk(root, skills, diagnostics, seen=set())
    names: set[str] = set()
    unique: list[Skill] = []
    for skill in skills:
        if skill.name in names:
            diagnostics.append(f"{skill.file_path}: skill name '{skill.name}' is already taken; skipped")
            continue
        names.add(skill.name)
        unique.append(skill)
    for message in diagnostics:
        logger.warning("skills: %s", message)
    return unique, diagnostics


def _walk(folder: Path, skills: list[Skill], diagnostics: list[str], seen: set[Path]) -> None:
    """Add the skill in `folder`, or search its subfolders in name order."""
    # A symlink loop would otherwise recurse forever.
    real = folder.resolve()
    if real in seen:
        return
    seen.add(real)

    skill_file = folder / SKILL_FILE
    if skill_file.is_file():
        # TS: `:=` assigns and tests in one go, like `if ((skill = load()))`
        if skill := _load_skill(skill_file, diagnostics):
            skills.append(skill)
        return
    try:
        children = sorted(folder.iterdir(), key=lambda p: p.name)
    except OSError as exc:
        diagnostics.append(f"{folder}: can't list folder ({exc})")
        return
    for child in children:
        # is_dir() follows symlinks, so a linked skill folder is searched too.
        if child.name.startswith(".") or child.name == "node_modules" or not child.is_dir():
            continue
        _walk(child, skills, diagnostics, seen)


def _load_skill(skill_file: Path, diagnostics: list[str]) -> Skill | None:
    """The skill described by one `SKILL.md`, or None (with a diagnostic) if it can't be used."""
    try:
        text = skill_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        diagnostics.append(f"{skill_file}: can't read ({exc})")
        return None
    meta = _frontmatter(text)
    if meta is None:
        diagnostics.append(f"{skill_file}: missing or invalid frontmatter; skipped")
        return None

    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        diagnostics.append(f"{skill_file}: no description; skipped")
        return None
    description = description.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        diagnostics.append(f"{skill_file}: description exceeds {MAX_DESCRIPTION_LENGTH} characters")

    # The folder name stands in when the frontmatter has no usable name, as in pi.
    raw_name = meta.get("name")
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else skill_file.parent.name
    if len(name) > MAX_NAME_LENGTH:
        diagnostics.append(f"{skill_file}: name exceeds {MAX_NAME_LENGTH} characters")
    if not NAME_PATTERN.fullmatch(name):
        diagnostics.append(f"{skill_file}: name '{name}' should be lowercase letters, digits and single hyphens")

    file_path = skill_file.resolve()
    return Skill(name=name, description=description, file_path=file_path, base_dir=file_path.parent, size=len(text))


def _frontmatter(text: str) -> dict[str, Any] | None:
    """The YAML frontmatter as a dict, or None if there is none or it doesn't parse to a mapping."""
    match = FRONTMATTER.match(text.removeprefix("﻿"))
    if not match:
        return None
    try:
        # safe_load builds only plain data (dicts, lists, strings), never arbitrary objects.
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    return meta if isinstance(meta, dict) else None


def format_skills_for_prompt(skills: list[Skill], reader: str) -> str:
    """The `<skills>` section body: how to load a skill, then `<available_skills>` with name, description, location.

    `reader` names the tool the model loads files with (`read` or `bash`).
    """
    lines = [
        "The following skills give specialized instructions for specific tasks.",
        f"Use the {reader} tool to load a skill's file when the task matches its description.",
        "Resolve relative paths in a skill against its folder (the parent of SKILL.md). "
        "Use absolute paths in tool calls.",
        "<available_skills>",
    ]
    lines += [format_skill_entry(skill) for skill in skills]
    lines.append("</available_skills>")
    return "\n".join(lines)


def format_skill_entry(skill: Skill) -> str:
    """One skill's `<skill>` element in `<available_skills>`: exactly what the model sees of it before reading it."""
    return "\n".join(
        [
            "  <skill>",
            f"    <name>{_xml(skill.name)}</name>",
            f"    <description>{_xml(skill.description)}</description>",
            f"    <location>{_xml(str(skill.file_path))}</location>",
            "  </skill>",
        ]
    )


def _xml(value: str) -> str:
    """Escape a value for an XML element, quotes included, as pi does."""
    return escape(value, {'"': "&quot;", "'": "&apos;"})


def read_skill_body(skill: Skill) -> str:
    """The skill's instructions: its `SKILL.md` without the frontmatter."""
    text = skill.file_path.read_text(encoding="utf-8").removeprefix("﻿")
    return FRONTMATTER.sub("", text, count=1).lstrip("\n")


# Caps for the web app's file list, so a skill holding a whole dataset can't flood the page.
MAX_LISTED_FILES = 200
MAX_FILE_BYTES = 1024 * 1024


def list_skill_files(skill: Skill) -> list[str]:
    """Files in the skill's folder as POSIX paths relative to it, `SKILL.md` first; dot-entries skipped."""
    files: list[str] = []
    for path in sorted(skill.base_dir.rglob("*")):
        relative = path.relative_to(skill.base_dir)
        if any(part.startswith(".") for part in relative.parts) or not path.is_file():
            continue
        files.append(relative.as_posix())
        if len(files) >= MAX_LISTED_FILES:
            break
    # TS: sorted(..., key=...) with a tuple key sorts by SKILL.md-first, then by path.
    return sorted(files, key=lambda f: (f != SKILL_FILE, f))


def read_skill_file(skill: Skill, path: str) -> str:
    """A text file inside the skill's folder; ValueError if it's outside, missing, too big or not text."""
    target = (skill.base_dir / path).resolve()
    if not target.is_relative_to(skill.base_dir) or not target.is_file():
        raise ValueError(f"No file '{path}' in skill '{skill.name}'.")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise ValueError(f"'{path}' is over {MAX_FILE_BYTES // 1024} KB.")
    data = target.read_bytes()
    if b"\0" in data[:8192]:
        raise ValueError(f"'{path}' is a binary file.")
    return data.decode("utf-8", errors="replace")
