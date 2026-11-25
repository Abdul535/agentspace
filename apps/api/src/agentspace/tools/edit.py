"""The `edit` tool: exact-text replacements, after pi's `packages/coding-agent/src/core/tools/edit.ts`.

pi also retries a failed match with fuzzy normalization (smart quotes, trailing spaces); we don't yet.
"""

import difflib
import json
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, Field

from agentspace.agent.tool import Tool, ToolContext, ToolError, ToolResult

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
    snippet = "Make precise file edits with exact text replacement, including multiple disjoint edits in one call"
    guidelines = (
        "Use edit for targeted changes. Batch changes to one file in a single call.",
        "Each edits[].old_text must exactly and uniquely match the original file. "
        "Keep matches short and non-overlapping; merge nearby changes without including large unchanged regions.",
    )

    def prepare_arguments(self, args: dict[str, Any]) -> dict[str, Any]:
        """Repair the shapes models often send for `edits`, after pi's `prepareEditArguments`.

        Handles `edits` as a JSON string or as a single object, the old single-edit shape
        (`old_text`/`new_text` at the top level), and camelCase keys (`oldText`/`newText`).
        Anything else is left for validation to report.
        """
        edits = args.get("edits")
        if isinstance(edits, str):
            try:
                edits = json.loads(edits)
            except json.JSONDecodeError:
                pass
        if isinstance(edits, dict):
            edits = [edits]
        legacy = snake_case_edit(args)
        old, new = legacy.get("old_text"), legacy.get("new_text")
        if isinstance(old, str) and isinstance(new, str):
            for key in ("old_text", "new_text", "oldText", "newText"):
                args.pop(key, None)
            # TS: `[...(Array.isArray(edits) ? edits : []), { old_text, new_text }]`
            edits = [*(edits if isinstance(edits, list) else []), {"old_text": old, "new_text": new}]
        if isinstance(edits, list):
            edits = [snake_case_edit(edit) if isinstance(edit, dict) else edit for edit in edits]
        if edits is not None:
            args["edits"] = edits
        return args

    async def execute(self, args: EditParams, ctx: ToolContext) -> ToolResult:
        """Apply the edits off the event loop; `details` carries a unified diff of the change."""
        # The workspace holds the file's lock for the whole read-modify-write, so a parallel edit can't
        # overwrite it in between.
        return await ctx.workspace.update_file(args.path, lambda raw: edit_bytes(raw, args.path, args.edits))


def snake_case_edit(edit: dict[str, Any]) -> dict[str, Any]:
    """`old_text`/`new_text` from an edit object, also accepting `oldText`/`newText`; other keys are kept."""
    fixed = {key: value for key, value in edit.items() if key not in ("oldText", "newText")}
    for camel, snake in (("oldText", "old_text"), ("newText", "new_text")):
        if snake not in fixed and camel in edit:
            fixed[snake] = edit[camel]
    return fixed


@dataclass(frozen=True)
class Match:
    """Where one edit's `old_text` sits in the original text."""

    index: int  # position in `edits`, for error messages
    start: int
    end: int
    new_text: str


def edit_bytes(data: bytes | None, shown_path: str, edits: list[Replacement]) -> tuple[bytes, ToolResult]:
    """The file's new bytes with every edit applied against the original text, and a result describing the change."""
    if data is None:
        raise ToolError(f"File not found: {shown_path}")
    try:
        raw = data.decode("utf-8")  # TS: unlike TextDecoder's default, this throws on invalid bytes
    except UnicodeDecodeError:
        raise ToolError(f"{shown_path} is not UTF-8 text; only text files can be edited.") from None

    # The model never writes the invisible BOM or CRs, so match on LF text and restore both on write.
    bom = BOM if raw.startswith(BOM) else ""
    eol = "\r\n" if "\r\n" in raw else "\n"
    old = raw.removeprefix(BOM).replace("\r\n", "\n")

    new = apply_edits(old, edits, shown_path)

    patch = "".join(
        difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), shown_path, shown_path, n=4)
    )
    details: dict[str, Any] = {"path": shown_path, "patch": patch, "first_changed_line": first_changed_line(old, new)}
    result = ToolResult.text(f"Successfully replaced {len(edits)} block(s) in {shown_path}.", details)
    return (bom + new.replace("\n", eol)).encode(), result


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
