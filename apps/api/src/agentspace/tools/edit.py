"""The `edit` tool: exact-text replacements, after pi's `packages/coding-agent/src/core/tools/edit.ts`.

pi also retries a failed match with fuzzy normalization (smart quotes, trailing spaces); we don't yet.
"""

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from asyncer import asyncify
from pydantic import BaseModel, Field

from agentspace.agent.tool import Tool, ToolContext, ToolError, ToolResult
from agentspace.tools.paths import resolve_in_workspace

BOM = "﻿"


class Replacement(BaseModel):
    """One targeted replacement inside the file."""

    old_text: Annotated[
        str,
        Field(
            description="Exact text for one targeted replacement. It must be unique in the original file and must "
            "not overlap with any other edits[].old_text in the same call."
        ),
    ]
    new_text: Annotated[str, Field(description="Replacement text for this targeted edit.")]


class EditParams(BaseModel):
    """Arguments of the `edit` tool."""

    path: Annotated[str, Field(description="Path to the file to edit, relative to the workspace")]
    # TS: z.array(Replacement).min(1)
    edits: Annotated[
        list[Replacement],
        Field(
            min_length=1,
            description="One or more targeted replacements. Each edit is matched against the original file, not "
            "incrementally. Do not include overlapping or nested edits. If two changes touch the same block or "
            "nearby lines, merge them into one edit instead.",
        ),
    ]


# TS: class EditTool extends Tool<EditParams>
class EditTool(Tool[EditParams]):
    """Replaces exact, unique snippets of an existing file in the workspace."""

    name = "edit"
    description = (
        "Edit a single file using exact text replacement. Every edits[].old_text must match a unique, "
        "non-overlapping region of the original file. If two changes affect the same block or nearby lines, "
        "merge them into one edit instead of emitting overlapping edits. Do not include large unchanged regions "
        "just to connect distant changes."
    )
    params = EditParams

    async def execute(self, args: EditParams, ctx: ToolContext) -> ToolResult:
        """Apply the edits off the event loop; `details` carries a unified diff of the change."""
        path = resolve_in_workspace(ctx.workspace, args.path)
        # TS: edit_file blocks (fs.readFileSync/writeFileSync); asyncify runs it on a worker thread and awaits it.
        return await asyncify(edit_file)(path, args.path, args.edits)


@dataclass(frozen=True)
class Match:
    """Where one edit's `old_text` sits in the original text."""

    index: int  # position in `edits`, for error messages
    start: int
    end: int
    new_text: str


def edit_file(path: Path, shown_path: str, edits: list[Replacement]) -> ToolResult:
    """Read the file, apply every edit against the original text, write it back, and describe the change."""
    if not path.exists():
        raise ToolError(f"File not found: {shown_path}")
    if path.is_dir():
        raise ToolError(f"{shown_path} is a directory, not a file.")
    try:
        raw = path.read_bytes().decode("utf-8")  # TS: unlike TextDecoder's default, this throws on invalid bytes
    except UnicodeDecodeError:
        raise ToolError(f"{shown_path} is not UTF-8 text; only text files can be edited.") from None

    # The model never writes the invisible BOM or CRs, so match on LF text and restore both on write.
    bom = BOM if raw.startswith(BOM) else ""
    eol = "\r\n" if "\r\n" in raw else "\n"
    old = raw.removeprefix(BOM).replace("\r\n", "\n")

    new = apply_edits(old, edits, shown_path)
    path.write_bytes((bom + new.replace("\n", eol)).encode())

    patch = "".join(
        difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), shown_path, shown_path, n=4)
    )
    details: dict[str, Any] = {"path": shown_path, "patch": patch, "first_changed_line": first_changed_line(old, new)}
    return ToolResult.text(f"Successfully replaced {len(edits)} block(s) in {shown_path}.", details)


def apply_edits(text: str, edits: list[Replacement], shown_path: str) -> str:
    """`text` with every edit applied; raises `ToolError` if any edit is empty, missing, ambiguous or overlapping."""
    # Error messages name the edit only when there are several, as pi's do.
    many = len(edits) > 1

    def label(i: int) -> str:
        """How an error message refers to edit `i`."""
        return f"edits[{i}]" if many else "the text"

    matches: list[Match] = []
    # TS: enumerate gives (index, item) pairs, like edits.entries()
    for i, edit in enumerate(edits):
        old_text = edit.old_text.replace("\r\n", "\n")
        if not old_text:
            field = f"edits[{i}].old_text" if many else "old_text"
            raise ToolError(f"{field} must not be empty in {shown_path}.")
        count = text.count(old_text)
        if count == 0:
            raise ToolError(
                f"Could not find {label(i)} in {shown_path}. "
                "The old_text must match exactly, including all whitespace and newlines."
            )
        if count > 1:
            raise ToolError(
                f"Found {count} occurrences of {label(i)} in {shown_path}. "
                "Each old_text must be unique; include more surrounding context to make it unique."
            )
        start = text.index(old_text)
        matches.append(Match(i, start, start + len(old_text), edit.new_text.replace("\r\n", "\n")))

    matches.sort(key=lambda m: m.start)  # TS: matches.sort((a, b) => a.start - b.start)
    # TS: zip(xs, xs[1:]) walks neighbouring pairs, like xs.slice(1).map((b, i) => [xs[i], b])
    for prev, cur in zip(matches, matches[1:], strict=False):
        if prev.end > cur.start:
            raise ToolError(
                f"edits[{prev.index}] and edits[{cur.index}] overlap in {shown_path}. "
                "Merge them into one edit or target disjoint regions."
            )

    # Splice back to front so earlier offsets stay valid.
    result = text
    for m in reversed(matches):
        result = result[: m.start] + m.new_text + result[m.end :]
    if result == text:
        raise ToolError(f"No changes made to {shown_path}: the replacements produced identical content.")
    return result


def first_changed_line(old: str, new: str) -> int:
    """1-based line number of the first line that differs, so the UI can jump to it."""
    # TS: next(...) takes the first item of a lazy sequence, like a for-of loop that returns early.
    first = next((i for i, (a, b) in enumerate(zip(old, new, strict=False)) if a != b), min(len(old), len(new)))
    return new.count("\n", 0, first) + 1
