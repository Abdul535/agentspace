"""The built-in tools and the named sets a session can start with."""

from collections.abc import Sequence
from typing import Literal

from agentspace.agent.tool import Tool
from agentspace.tools.bash import BashTool
from agentspace.tools.edit import EditTool
from agentspace.tools.find import FindTool
from agentspace.tools.grep import GrepTool
from agentspace.tools.ls import LsTool
from agentspace.tools.read import ReadTool
from agentspace.tools.write import WriteTool

# Every built-in tool class by its name, the name the model calls it by.
# TS: a dict comprehension, like Object.fromEntries(classes.map((c) => [c.name, c])).
TOOL_CLASSES: dict[str, type[Tool]] = {
    cls.name: cls for cls in (ReadTool, BashTool, EditTool, WriteTool, GrepTool, FindTool, LsTool)
}

# TS: type ToolSetName = "all" | "coding" | "read_only"
type ToolSetName = Literal["all", "coding", "read_only"]

# Presets for a new session, after pi's `createCodingTools` / `createReadOnlyTools`. A session stores the
# expanded names, so changing a preset here never changes existing sessions.
TOOL_SETS: dict[ToolSetName, tuple[str, ...]] = {
    "all": tuple(TOOL_CLASSES),  # TS: Object.keys(TOOL_CLASSES); dicts keep insertion order
    "coding": ("read", "bash", "edit", "write"),
    "read_only": ("read", "grep", "find", "ls"),
}


def build_tools(names: Sequence[str], *, bash: bool) -> list[Tool]:
    """Instances of the named tools, in order.

    `bash` is dropped unless enabled: the server setting overrides what a session stores,
    since the tool runs unsandboxed. Unknown names (a tool removed since) are skipped.
    """
    return [TOOL_CLASSES[name]() for name in names if name in TOOL_CLASSES and (bash or name != "bash")]
