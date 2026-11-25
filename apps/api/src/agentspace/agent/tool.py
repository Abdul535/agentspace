"""The tool interface: what a tool looks like to the agent loop and to the model."""

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Self

from pydantic import BaseModel

from agentspace.ai.messages import TextContent, UserContent
from agentspace.ai.provider import ToolSpec

# Only for the type checker: the workspace module imports ToolError from here, so a runtime import would cycle.
# TS: like `import type { Workspace } from ...`.
if TYPE_CHECKING:
    from agentspace.workspace import Workspace

# How the calls in one reply run: one after another, or concurrently (after pi's `ToolExecutionMode`).
# TS: type ExecutionMode = "sequential" | "parallel"
type ExecutionMode = Literal["sequential", "parallel"]


# Reports partial output while a tool runs, after pi's `AgentToolUpdateCallback` (ADR-003 step 10).
# TS: type OnUpdate = (partial: ToolResult) => Promise<void>
type OnUpdate = Callable[["ToolResult"], Awaitable[None]]


# TS: the @dataclass decorator writes the constructor from the fields; frozen=True makes them readonly.
@dataclass(frozen=True)
class ToolContext:
    """What every tool call gets: the session's workspace, and a way to report progress."""

    workspace: "Workspace"
    """Where the session's tools read, write and run commands (ADR-004). It refuses paths outside it,
    except reads of the loaded skills' folders (ADR-016)."""
    on_update: OnUpdate | None = None
    """Set by the loop for each call. Send a snapshot of the output so far (not a delta); the UI shows the latest.
    Calls after `execute` returns are ignored. Updates are never sent to the model or saved."""


# TS: class ToolError extends Error {}
class ToolError(Exception):
    """A failure the model should see as the tool's result, e.g. "file not found"."""


class ToolResult(BaseModel):
    """What a tool returns, after pi's `AgentToolResult`: content for the model, details for the UI."""

    content: list[UserContent]
    """Text and images for the model (ADR-015)."""
    details: dict[str, Any] | None = None
    """Structured facts for the UI and logs (paths, line ranges, sizes); never sent to the model."""
    is_error: bool = False
    """Report a failure without raising, keeping `details`; `ToolError` is the shortcut when there are none."""
    terminate: bool = False
    """Ask the run to stop after this batch instead of asking the model again (ADR-003 step 11).
    It stops only if every call in the batch asks; never sent to the model or saved."""

    # TS: a static factory; `cls` is the class itself and `Self` its instance type.
    @classmethod
    def text(cls, text: str, details: dict[str, Any] | None = None) -> Self:
        """A successful result with one text block."""
        return cls(content=[TextContent(text=text)], details=details)


# TS: abstract class Tool<P extends BaseModel>; inheriting from ABC is what makes it abstract.
class Tool[P: BaseModel](ABC):
    """A tool the model can call.

    `params` is a Pydantic model: its JSON Schema is what the model sees, and the
    model's arguments are validated against it before `execute` runs.
    """

    # TS: abstract name / description. Built-in tools set them once on the class; an MCP tool (ADR-017)
    # sets them per instance, since one class serves every server's tools.
    name: str
    description: str
    snippet: str
    """One line for the system prompt's tool list, after pi's `promptSnippet`."""
    guidelines: ClassVar[tuple[str, ...]] = ()
    """Rules this tool adds to the system prompt, after pi's `promptGuidelines`."""
    params: type[P]  # TS: the schema class itself (like `typeof Schema`), not an instance
    execution_mode: ClassVar[ExecutionMode] = "parallel"
    """"sequential" makes every call in a batch that includes this tool run one at a time."""

    # TS: abstract execute(args: P, ctx: ToolContext): Promise<ToolResult>
    @abstractmethod
    async def execute(self, args: P, ctx: ToolContext) -> ToolResult:
        """Run the call. Raise `ToolError`, or return `is_error=True`, to report a failure."""

    # TS: a method with a default body that subclasses may override, like a non-abstract method on an abstract class.
    def prepare_arguments(self, args: dict[str, Any]) -> dict[str, Any]:
        """Fix the raw arguments before validation, for models that send slightly wrong shapes. Default: no change.

        Gets a copy, so changing it in place is fine; the transcript keeps what the model sent.
        """
        return args

    def spec(self) -> ToolSpec:
        """Describe this tool to the model: name, description and argument schema."""
        return ToolSpec(name=self.name, description=self.description, parameters=self.params.model_json_schema())
