"""Shared output shaping for the listing tools (grep, find, ls): a byte cap and limit notices."""

from agentspace.tools.read import MAX_BYTES


def head_within_cap(lines: list[str]) -> tuple[str, bool]:
    """The first `lines` that fit in `MAX_BYTES`, joined; and whether any were dropped."""
    kept: list[str] = []
    size = 0
    for line in lines:
        size += len(line.encode()) + 1
        if size > MAX_BYTES:
            return "\n".join(kept), True
        kept.append(line)
    return "\n".join(kept), False


def with_notices(text: str, notices: list[str]) -> str:
    """`text` followed by a bracketed line telling the model what was cut and how to get more."""
    return f"{text}\n\n[{'. '.join(notices)}]" if notices else text
