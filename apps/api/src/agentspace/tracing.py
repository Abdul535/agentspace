"""Traces of each run for Langfuse (ADR-018), built from the loop's events so the loop never knows about them.

One run is one Langfuse trace, grouped by session:

    run (agent)                       the whole run: first prompt in, last answer out
      model (generation)              one per assistant reply: system prompt + transcript in, reply out, tokens,
                                      latency, time to first token
      <tool name> (tool)              one per tool call: arguments in, result out
      compaction (span)               when the run summarizes older messages (ADR-009)

Tracing is a side channel, after pi's telemetry (`packages/ai/src/telemetry.ts`): with
no keys set it does nothing, the client sends in the background, and a tracing error
is logged and dropped, never raised into the run.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Self

from langfuse import Langfuse, propagate_attributes

from agentspace.agent.events import (
    AgentEndEvent,
    AgentEvent,
    CompactionEndEvent,
    CompactionStartEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from agentspace.agent.messages import AgentMessage, convert_to_llm
from agentspace.ai.messages import AssistantMessage, UserMessage, joined_text, strip_images
from agentspace.config import Settings

log = logging.getLogger(__name__)

# An open Langfuse observation; the SDK types `start_observation` as a wide union, but each kind has update/end.
type Observation = Any


class RunTrace:
    """A run's trace that records nothing: what runs get while tracing is off."""

    def observe(self, event: AgentEvent) -> None:
        """Record one loop event."""

    def set_system_prompt(self, system_prompt: str) -> None:
        """The system prompt the next model calls are sent; the run builds it per loop pass, so it arrives late."""

    def end(self) -> None:
        """Close the trace once the run is over."""


# TS: `class LangfuseRunTrace extends RunTrace`; it overrides both methods.
class LangfuseRunTrace(RunTrace):
    """One run's Langfuse trace: opens and closes an observation as each model call, tool call or compaction does."""

    def __init__(self, client: Langfuse, session_id: str, transcript: list[AgentMessage]) -> None:
        self.transcript = transcript
        """The run's live transcript (`ActiveRun.messages`), read as each model call starts to record its input."""
        # TS: `with` scopes the session id to the observations created inside it, like a try/finally that resets it.
        with propagate_attributes(session_id=session_id, trace_name="agent run"):
            self.root = client.start_observation(name="run", as_type="agent")
        self.generation: Observation | None = None
        self.first_token = False
        """Whether the current reply has streamed anything yet; its first piece marks time to first token."""
        self.tools: dict[str, Observation] = {}
        """Open tool observations by tool call id; several run at once with parallel tools."""
        self.compaction: Observation | None = None
        self.has_input = False
        self.system_prompt: str | None = None

    def observe(self, event: AgentEvent) -> None:
        """Record one event; a tracing failure is logged, never raised into the run."""
        try:
            self.record(event)
        except Exception:
            log.warning("Tracing failed on a %s event", event.type, exc_info=True)

    def set_system_prompt(self, system_prompt: str) -> None:
        """Keep the system prompt to put first in each model call's input, as the provider sends it."""
        self.system_prompt = system_prompt

    def record(self, event: AgentEvent) -> None:
        """Turn one event into the matching observation change."""
        match event:
            case MessageStartEvent(message=AssistantMessage() as message):
                # Sent as the model would see it, with images swapped for a note to keep traces small.
                sent = [m.model_dump() for m in strip_images(convert_to_llm(self.transcript))]
                if self.system_prompt is not None:
                    # A `system` message first, so Langfuse's chat view shows it like the provider request does.
                    sent.insert(0, {"role": "system", "content": self.system_prompt})
                self.generation = self.root.start_observation(
                    name="model", as_type="generation", model=message.model, input=sent
                )
                self.first_token = False
            case MessageUpdateEvent() if self.generation and not self.first_token:
                self.first_token = True
                self.generation.update(completion_start_time=datetime.now(UTC))
            case MessageEndEvent(message=AssistantMessage() as message) if self.generation:
                usage = message.usage
                self.generation.update(
                    output=[block.model_dump() for block in message.content],
                    usage_details={"input": usage.input, "output": usage.output, "total": usage.total},
                    metadata={
                        "provider": message.provider,
                        "stop_reason": message.stop_reason,
                        "reasoning_tokens": usage.reasoning,
                    },
                    **error_level(message.stop_reason == "error", message.error_message),
                )
                self.generation.end()
                self.generation = None
            case MessageEndEvent(message=UserMessage() as message) if not self.has_input:
                # The prompt that started the run; steering messages later in the run show in the model inputs.
                self.has_input = True
                self.root.update(input=message.text())
            case ToolExecutionStartEvent(tool_call_id=call_id, tool_name=name, arguments=arguments):
                self.tools[call_id] = self.root.start_observation(name=name, as_type="tool", input=arguments)
            case ToolExecutionEndEvent(result=result):
                # TS: `dict.pop(key, None)` is like reading the key and deleting it, `undefined` if absent.
                tool = self.tools.pop(result.tool_call_id, None)
                if tool:
                    tool.update(output=joined_text(result.content), **error_level(result.is_error, "Tool error"))
                    tool.end()
            case CompactionStartEvent(reason=reason):
                self.compaction = self.root.start_observation(
                    name="compaction", as_type="span", metadata={"reason": reason}
                )
            case CompactionEndEvent(error=error) if self.compaction:
                self.compaction.update(**error_level(error is not None, error))
                self.compaction.end()
                self.compaction = None
            case AgentEndEvent(reason=reason, new_messages=new_messages):
                # An overflow retry ends the loop twice (ADR-009); the last `agent_end` wins.
                answers = [m for m in new_messages if isinstance(m, AssistantMessage)]
                self.root.update(
                    output=answers[-1].text() if answers else None,
                    metadata={"end_reason": reason},
                    **error_level(reason == "error", "The run failed"),
                )

    def end(self) -> None:
        """Close what the run left open (it was cancelled or crashed mid-call), then the run itself."""
        try:
            leftovers = [self.generation, self.compaction, *self.tools.values()]
            for observation in leftovers:
                if observation:
                    observation.update(level="WARNING", status_message="The run stopped before this finished")
                    observation.end()
            self.root.end()
        except Exception:
            log.warning("Tracing failed closing a run", exc_info=True)


def error_level(failed: bool, message: str | None) -> dict[str, Any]:
    """The `level` and `status_message` arguments that mark an observation failed, or none when it went fine."""
    return {"level": "ERROR", "status_message": message} if failed else {}


class Tracer:
    """Starts a trace per run: Langfuse when its keys are set, a no-op otherwise."""

    def __init__(self, client: Langfuse | None) -> None:
        self.client = client

    # TS: @classmethod is like a static factory method; `cls` is the class itself.
    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Langfuse with the configured keys, or off when either key is missing."""
        if not (settings.langfuse_public_key and settings.langfuse_secret_key):
            return cls(None)
        client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url,
        )
        return cls(client)

    def start_run(self, session_id: str, transcript: list[AgentMessage]) -> RunTrace:
        """A trace for a run that is starting; tracing that fails to start leaves the run untraced."""
        if self.client is None:
            return RunTrace()
        try:
            return LangfuseRunTrace(self.client, session_id, transcript)
        except Exception:
            log.warning("Tracing failed to start a run", exc_info=True)
            return RunTrace()

    def shutdown(self) -> None:
        """Send what is still buffered; blocks on the network, so call it off the event loop."""
        if self.client is not None:
            self.client.shutdown()
