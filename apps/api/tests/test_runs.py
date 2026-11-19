"""Background runs (ADR-007): runs outlive their request, late clients catch up, and runs can be stopped."""

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import agentspace.runs
from agentspace.agent.events import AgentEndEvent, MessageEndEvent, MessageStartEvent, TurnStartEvent
from agentspace.agent.loop import Emit
from agentspace.ai.messages import AssistantMessage, Message, TextContent, UserMessage
from agentspace.ai.provider import AssistantEvent, Context, DoneEvent, StartEvent, TextDeltaEvent
from agentspace.api.deps import provider_lookup
from agentspace.config import Settings
from agentspace.main import create_app
from agentspace.runs import ActiveRun, RunManager, read_events


class GatedProvider:
    """Streams "Hel", then holds the reply until `gate` is set, then finishes with "lo"."""

    name = "gated"

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.waiting = threading.Event()
        """Set (for the test thread) once the stream is parked at the gate."""

    async def stream(self, model: str, context: Context) -> AsyncIterator[AssistantEvent]:
        """One text reply, paused halfway."""
        yield StartEvent()
        yield TextDeltaEvent(delta="Hel")
        self.waiting.set()
        await self.gate.wait()
        yield TextDeltaEvent(delta="lo")
        yield DoneEvent(message=AssistantMessage(provider=self.name, model=model, content=[TextContent(text="Hello")]))


@pytest.fixture
def provider() -> GatedProvider:
    return GatedProvider()


@pytest.fixture
def client(tmp_path: Path, provider: GatedProvider) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    app.dependency_overrides[provider_lookup] = lambda: lambda name: provider
    with TestClient(app) as client:
        yield client


def sse_events(text: str) -> list[dict]:
    """Parse an SSE body into the JSON object of each event's `data` field."""
    return [json.loads(line.removeprefix("data: ")) for line in text.splitlines() if line.startswith("data: ")]


def in_background(fn: Callable[[], object]) -> dict:
    """Run a blocking request on a thread; the dict gets its result. TestClient buffers a stream until it ends."""
    out: dict = {}
    thread = threading.Thread(target=lambda: out.setdefault("result", fn()))
    thread.start()
    out["thread"] = thread
    return out


def wait_for(condition: Callable[[], bool]) -> None:
    """Poll until the condition holds, failing after 5 s."""
    deadline = time.monotonic() + 5
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_a_late_client_gets_a_snapshot_with_the_partial_reply_then_live_events(
    client: TestClient, provider: GatedProvider
) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    post = in_background(lambda: client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    assert client.get(f"/sessions/{session_id}").json()["running"] is True

    async def open_gate_soon() -> None:
        await asyncio.sleep(0.2)
        provider.gate.set()

    # The GET below blocks until the run ends, so the gate opens from the app's event loop.
    client.portal.start_task_soon(open_gate_soon)
    events = sse_events(client.get(f"/sessions/{session_id}/events").text)
    post["thread"].join(5)

    snapshot = events[0]
    assert snapshot["type"] == "run_snapshot" and snapshot["running"] is True
    assert [m["role"] for m in snapshot["messages"]] == ["user"]
    assert snapshot["streaming"]["content"] == [{"type": "text", "text": "Hel"}]
    # Snapshot + live events add up to the whole reply: nothing missed, nothing twice.
    deltas = [e["event"]["delta"] for e in events if e["type"] == "message_update"]
    assert deltas == ["lo"] and events[-1]["type"] == "agent_end"

    # The POST client saw the whole run too, and the transcript was saved.
    assert sse_events(post["result"].text)[-1]["reason"] == "done"
    detail = client.get(f"/sessions/{session_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"] and detail["running"] is False


def test_watching_an_idle_session_returns_one_snapshot_of_the_transcript(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    events = sse_events(client.get(f"/sessions/{session_id}/events").text)
    empty = {"type": "run_snapshot", "running": False, "messages": [], "streaming": None, "pending_tools": []}
    assert events == [empty]


def test_abort_stops_the_run_keeps_finished_messages_and_frees_the_slot(
    client: TestClient, provider: GatedProvider
) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    assert client.post(f"/sessions/{session_id}/abort").status_code == 409

    post = in_background(lambda: client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    assert client.post(f"/sessions/{session_id}/abort").status_code == 204
    post["thread"].join(5)

    end = sse_events(post["result"].text)[-1]
    assert end["type"] == "agent_end" and end["reason"] == "aborted"
    assert [(m["role"], m["content"]) for m in end["new_messages"]] == [("user", "hi")]
    detail = client.get(f"/sessions/{session_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user"] and detail["running"] is False


def test_closing_a_subscription_does_not_stop_the_run(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    runs: RunManager = client.app.state.runs

    async def scenario() -> None:
        async def no_history() -> list[Message]:
            return []

        active = await runs.claim(session_id, no_history)
        gate = asyncio.Event()
        prompt = UserMessage(content="hi")
        reply = AssistantMessage(provider="test", model="test", content=[TextContent(text="Hi.")])

        async def loop(emit: Emit) -> None:
            await emit(MessageStartEvent(message=prompt))
            await emit(MessageEndEvent(message=prompt))
            await gate.wait()
            await emit(MessageStartEvent(message=reply))
            await emit(MessageEndEvent(message=reply))
            await emit(AgentEndEvent(reason="done", new_messages=[prompt, reply]))

        queue = runs.start(active, loop)
        events = read_events(active, queue)
        assert (await anext(events)).type == "message_start"
        await events.aclose()  # the client went away
        assert not active.subscribers

        gate.set()
        assert active.task is not None
        await active.task

    client.portal.call(scenario)
    detail = client.get(f"/sessions/{session_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"] and detail["running"] is False


def test_a_crashed_loop_ends_its_subscribers_with_an_error_and_frees_the_slot(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    runs: RunManager = client.app.state.runs

    async def scenario() -> list:
        async def no_history() -> list[Message]:
            return []

        active = await runs.claim(session_id, no_history)

        async def loop(emit: Emit) -> None:
            await emit(TurnStartEvent(turn=1))
            raise RuntimeError("boom")

        queue = runs.start(active, loop)
        return [event async for event in read_events(active, queue)]

    events = client.portal.call(scenario)
    assert [e.type for e in events] == ["turn_start", "agent_end"] and events[-1].reason == "error"
    assert not runs.is_running(session_id)


def test_a_subscriber_that_falls_behind_is_dropped_without_holding_the_run_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agentspace.runs, "SUBSCRIBER_QUEUE_SIZE", 2)
    run = ActiveRun(session_id="s", messages=[], history_size=0)
    slow, fast = run.subscribe(), run.subscribe()

    run.publish(TurnStartEvent(turn=1))
    fast.get_nowait()  # this one keeps up
    run.publish(TurnStartEvent(turn=2))
    run.publish(TurnStartEvent(turn=3))  # `slow` is full now

    assert run.subscribers == {fast}
    # The dropped queue ends with the end marker, so its stream closes and the client can reconnect.
    drained = []
    while not slow.empty():
        drained.append(slow.get_nowait())
    assert drained[-1] is None
