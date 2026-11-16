"""The tool interface: what a tool looks like to the agent loop and to the model."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel

from agentspace.ai.provider import ToolSpec


# TS: the @dataclass decorator writes the constructor from the fields; frozen=True makes them readonly.
@dataclass(frozen=True)
class ToolContext:
    """Per-session facts every tool call gets, such as the workspace folder."""

    workspace: Path
    """The folder the session's tools act in. File tools refuse paths outside it."""


# TS: class ToolError extends Error {}
class ToolError(Exception):
    """A failure the model should see as the tool's result, e.g. "file not found"."""


# TS: abstract class Tool<P extends BaseModel>; inheriting from ABC is what makes it abstract.
class Tool[P: BaseModel](ABC):
    """A tool the model can call.

    `params` is a Pydantic model: its JSON Schema is what the model sees, and the
    model's arguments are validated against it before `execute` runs.
    """

    # TS: abstract readonly name / description — each subclass sets them as class-level constants.
    name: ClassVar[str]
    description: ClassVar[str]
    params: type[P]  # TS: the schema class itself (like `typeof Schema`), not an instance

    # TS: abstract execute(args: P, ctx: ToolContext): Promise<string>
    @abstractmethod
    async def execute(self, args: P, ctx: ToolContext) -> str:
        """Return the text the model receives. Raise `ToolError` to report a failure."""

    def spec(self) -> ToolSpec:
        """Describe this tool to the model: name, description and argument schema."""
        return ToolSpec(name=self.name, description=self.description, parameters=self.params.model_json_schema())
