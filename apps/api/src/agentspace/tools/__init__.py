"""The built-in tools every session gets."""

from agentspace.agent.tool import Tool
from agentspace.tools.bash import BashTool
from agentspace.tools.edit import EditTool
from agentspace.tools.find import FindTool
from agentspace.tools.grep import GrepTool
from agentspace.tools.ls import LsTool
from agentspace.tools.read import ReadTool
from agentspace.tools.write import WriteTool


def default_tools(*, bash: bool = False) -> list[Tool]:
    """The tools a session's agent can call; `bash` only when enabled, since it runs unsandboxed."""
    shell: list[Tool] = [BashTool()] if bash else []
    # TS: `*shell` spreads the list, like ...shell
    return [ReadTool(), *shell, EditTool(), WriteTool(), GrepTool(), FindTool(), LsTool()]
