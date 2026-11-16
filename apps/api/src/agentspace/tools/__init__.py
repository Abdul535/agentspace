"""The built-in tools every session gets."""

from agentspace.agent.tool import Tool
from agentspace.tools.bash import BashTool
from agentspace.tools.edit import EditTool
from agentspace.tools.read import ReadTool
from agentspace.tools.write import WriteTool


def default_tools() -> list[Tool]:
    """The tools a session's agent can call."""
    return [ReadTool(), BashTool(), EditTool(), WriteTool()]
