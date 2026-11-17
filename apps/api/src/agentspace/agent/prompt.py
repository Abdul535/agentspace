"""Builds the system prompt from a preamble and the tools a run has, after pi's `core/system-prompt.ts`.

Built on every run rather than stored, so the tool list and rules always match the tools the model gets.
"""

from collections.abc import Sequence

from agentspace.agent.tool import Tool

# Rules for every run, after the tools' own; pi ends its rules the same way.
GENERAL_RULES = (
    "Be concise in your responses",
    "Show file paths clearly when working with files",
)

WORKSPACE = (
    "You work in a workspace folder. Tool paths are relative to it: use '.' (or leave the path out) for its root. "
    "'/' is the server's filesystem root, outside the workspace, and is refused."
)


def build_system_prompt(preamble: str, tools: Sequence[Tool]) -> str:
    """The preamble, then `<tools>`, `<rules>` and `<workspace>` sections, tagged so the model can tell them apart."""
    names = {tool.name for tool in tools}  # TS: a set comprehension, like new Set(tools.map((t) => t.name))
    rules: list[str] = []
    # Without the dedicated file tools, point the model at bash for them, as pi does.
    if "bash" in names and not names & {"grep", "find", "ls"}:  # TS: `&` on sets is their intersection
        rules.append("Use bash for file operations like ls, rg, find")
    for tool in tools:
        rules.extend(tool.guidelines)
    rules.extend(GENERAL_RULES)

    sections = {
        "tools": "\n".join(f"- {tool.name}: {tool.snippet}" for tool in tools) or "(none)",
        "rules": "\n".join(f"- {rule}" for rule in dict.fromkeys(rules)),  # dict.fromkeys de-duplicates, keeping order
        "workspace": WORKSPACE,
    }
    tagged = (f"<{name}>\n{body}\n</{name}>" for name, body in sections.items())
    return "\n\n".join([preamble.strip(), *tagged])
