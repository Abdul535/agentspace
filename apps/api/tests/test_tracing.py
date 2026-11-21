"""Langfuse tracing (ADR-018): a run becomes one trace of model and tool observations, and never fails it."""

import json
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langfuse import Langfuse
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agentspace.agent.events import AgentEndEvent, MessageEndEvent, MessageStartEvent, ToolExecutionStartEvent
from agentspace.ai.messages import AssistantMessage, Cost, TextContent, ToolCall, Usage, UserMessage
from agentspace.ai.provider import AssistantEvent, Context, DoneEvent, StartEvent, TextDeltaEvent
from agentspace.api.deps import provider_lookup
from agentspace.config import Settings
from agentspace.main import create_app
from agentspace.runs import RunManager
from agentspace.tracing import LangfuseRunTrace, RunTrace, Tracer


class ToolThenAnswer:
    """Asks for `ls` on the first request, then answers "done"."""

    name = "scripted"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, model: str, context: Context) -> AsyncIterator[AssistantEvent]:
        """One reply per request, with token counts so the trace has usage to record."""
        self.calls += 1
        yield StartEvent()
        if self.calls == 1:
            content: list[Any] = [ToolCall(id="call-1", name="ls", arguments={})]
            stop = "tool_use"
        else:
            yield TextDeltaEvent(delta="done")
            content, stop = [TextContent(text="done")], "stop"
        usage = Usage(input=10, output=4, total=14, cost=Cost(input=0.001, output=0.002, total=0.003))
        yield DoneEvent(
            message=AssistantMessage(provider=self.name, model=model, content=content, usage=usage, stop_reason=stop)
        )


@pytest.fixture
def exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def langfuse(exporter: InMemorySpanExporter) -> Iterator[Langfuse]:
    """A Langfuse client that hands spans to `exporter` instead of the network."""
    # A fresh key per test: the SDK shares one span pipeline per public key.
    client = Langfuse(
        public_key=f"pk-{uuid.uuid4()}", secret_key="sk", base_url="http://127.0.0.1:9", span_exporter=exporter
    )
    yield client
    client.shutdown()


def spans(client: Langfuse, exporter: InMemorySpanExporter) -> dict[str, ReadableSpan]:
    """The finished spans by name, after pushing out what the client buffered."""
    client.flush()
    return {span.name: span for span in exporter.get_finished_spans()}


def attr(span: ReadableSpan, key: str) -> Any:
    """One Langfuse attribute of a span; the SDK stores structured values as JSON strings."""
    value = (span.attributes or {})[f"langfuse.observation.{key}"]
    return json.loads(value) if isinstance(value, str) and value[:1] in '[{"0123456789' else value


def test_a_run_traces_its_model_calls_and_tool_calls_under_one_root(
    tmp_path: Path, langfuse: Langfuse, exporter: InMemorySpanExporter
) -> None:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    provider = ToolThenAnswer()
    app.dependency_overrides[provider_lookup] = lambda: lambda name: provider
    with TestClient(app) as client:
        cast(RunManager, cast(FastAPI, client.app).state.runs).tracer = Tracer(langfuse)
        session_id = client.post("/sessions", json={}).json()["id"]
        client.post(f"/sessions/{session_id}/messages", json={"content": "list files"})

    langfuse.flush()
    finished = exporter.get_finished_spans()
    by_type = [(s.name, attr(s, "type")) for s in finished]
    assert sorted(by_type) == [("ls", "tool"), ("model", "generation"), ("model", "generation"), ("run", "agent")]

    root = next(s for s in finished if s.name == "run")
    assert root.attributes and root.attributes["session.id"] == session_id
    # Every observation hangs off the run, in one trace.
    assert root.context and all(
        s.parent and s.parent.span_id == root.context.span_id for s in finished if s is not root
    )
    assert attr(root, "input") == "list files" and attr(root, "output") == "done"

    first, second = sorted((s for s in finished if s.name == "model"), key=lambda s: s.start_time or 0)
    assert attr(first, "usage_details") == {"input": 10, "output": 4, "total": 14}
    # Our own cost, so Langfuse doesn't guess one from its price table.
    assert attr(first, "cost_details") == {"total": 0.003, "input": 0.001, "output": 0.002}
    assert attr(first, "metadata.stop_reason") == "tool_use"
    # The second call was sent the system prompt and the tool result; its streamed text marks time to first token.
    sent = attr(second, "input")
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "tool_result"]
    assert sent[0]["content"].startswith("You are a coding assistant") and "<tools>" in sent[0]["content"]
    assert "langfuse.observation.completion_start_time" in (second.attributes or {})


def test_ending_a_cut_off_run_closes_its_open_observations_as_warnings(
    langfuse: Langfuse, exporter: InMemorySpanExporter
) -> None:
    trace = LangfuseRunTrace(langfuse, "s", [UserMessage(content="hi")])
    trace.observe(MessageStartEvent(message=AssistantMessage(provider="p", model="m")))
    trace.observe(ToolExecutionStartEvent(tool_call_id="c", tool_name="bash", arguments={"command": "sleep 9"}))
    trace.observe(AgentEndEvent(reason="aborted", new_messages=[]))
    trace.end()

    finished = spans(langfuse, exporter)
    assert set(finished) == {"run", "model", "bash"}
    assert attr(finished["model"], "level") == "WARNING" and attr(finished["bash"], "level") == "WARNING"


def test_a_failed_reply_is_marked_as_an_error(langfuse: Langfuse, exporter: InMemorySpanExporter) -> None:
    trace = LangfuseRunTrace(langfuse, "s", [])
    failed = AssistantMessage(provider="p", model="m", stop_reason="error", error_message="rate limited")
    trace.observe(MessageStartEvent(message=failed))
    trace.observe(MessageEndEvent(message=failed))
    trace.end()

    model = spans(langfuse, exporter)["model"]
    assert attr(model, "level") == "ERROR" and attr(model, "status_message") == "rate limited"


def test_tracing_is_off_without_both_keys() -> None:
    tracer = Tracer.from_settings(Settings(_env_file=None).model_copy(update={"langfuse_public_key": "pk"}))
    assert tracer.client is None
    assert type(tracer.start_run("s", [])) is RunTrace


class BrokenClient:
    """A Langfuse stand-in whose every call fails."""

    def start_observation(self, **kwargs: Any) -> Any:
        raise RuntimeError("langfuse is down")


def test_a_broken_tracer_never_fails_the_run(tmp_path: Path) -> None:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    provider = ToolThenAnswer()
    app.dependency_overrides[provider_lookup] = lambda: lambda name: provider
    with TestClient(app) as client:
        cast(FastAPI, client.app).state.runs.tracer = Tracer(cast(Langfuse, BrokenClient()))
        session_id = client.post("/sessions", json={}).json()["id"]
        body = client.post(f"/sessions/{session_id}/messages", json={"content": "list files"}).text

    assert '"reason":"done"' in body.replace(" ", "")
