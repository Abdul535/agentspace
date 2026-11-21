"""Background runs (ADR-007): runs outlive their request, late clients catch up, and runs can be stopped."""

import asyncio
import json
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from pathlib import Path
from typing import cast

import fastapi.routing
import pytest
from anyio.from_thread import BlockingPortal
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agentspace.runs
from agentspace.agent.events import AgentEndEvent, MessageEndEvent, MessageStartEvent, TurnStartEvent
from agentspace.agent.loop import Emit
from agentspace.agent.messages import AgentMessage
from agentspace.ai.messages import AssistantMessage, TextContent, UserMessage
from agentspace.ai.provider import AssistantEvent, Context, DoneEvent, StartEvent, TextDeltaEvent
from agentspace.api.deps import provider_lookup
from agentspace.config import Settings
from agentspace.main import create_app
from agentspace.runs import ActiveRun, MessageQueue, RunManager, read_events


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


def sse_ids(text: str) -> list[str]:
    """The `id` field of each SSE event that has one, in order."""
    return [line.removeprefix("id: ") for line in text.splitlines() if line.startswith("id: ")]


def in_background(fn: Callable[[], object]) -> dict:
    """Run a blocking request on a thread; the dict gets its result. TestClient buffers a stream until it ends."""
    out: dict = {}
    thread = threading.Thread(target=lambda: out.setdefault("result", fn()))
    thread.start()
    out["thread"] = thread
    return out


def portal(client: TestClient) -> BlockingPortal:
    """The client's portal into the app's event loop; it exists while the client is open (`with TestClient(...)`)."""
    assert client.portal is not None  # narrows `BlockingPortal | None` for the type checker
    return client.portal


def user_texts(messages: list[AgentMessage]) -> list[str]:
    """The text of each message, all of which must be user messages."""
    assert all(isinstance(m, UserMessage) for m in messages)
    return [m.text() for m in messages if isinstance(m, UserMessage)]


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
    portal(client).start_task_soon(open_gate_soon)
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
    # TS: cast() is `as FastAPI`: TestClient types `app` as any ASGI app, ours is a FastAPI one.
    runs: RunManager = cast(FastAPI, client.app).state.runs

    async def scenario() -> None:
        async def no_history() -> list[AgentMessage]:
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
        assert (await anext(events)).event.type == "message_start"
        await events.aclose()  # the client went away
        assert not active.subscribers

        gate.set()
        assert active.task is not None
        await active.task

    portal(client).call(scenario)
    detail = client.get(f"/sessions/{session_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"] and detail["running"] is False


def test_a_crashed_loop_ends_its_subscribers_with_an_error_and_frees_the_slot(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    runs: RunManager = cast(FastAPI, client.app).state.runs

    async def scenario() -> list:
        async def no_history() -> list[AgentMessage]:
            return []

        active = await runs.claim(session_id, no_history)

        async def loop(emit: Emit) -> None:
            await emit(TurnStartEvent(turn=1))
            raise RuntimeError("boom")

        queue = runs.start(active, loop)
        return [numbered.event async for numbered in read_events(active, queue)]

    events = portal(client).call(scenario)
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


def test_a_message_steered_in_mid_run_gets_its_own_reply(client: TestClient, provider: GatedProvider) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    assert client.post(f"/sessions/{session_id}/steer", json={"content": "x"}).status_code == 409  # nothing running

    post = in_background(lambda: client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    assert client.post(f"/sessions/{session_id}/steer", json={"content": "shorter please"}).status_code == 202
    portal(client).call(provider.gate.set)
    post["thread"].join(5)

    events = sse_events(post["result"].text)
    assert events[-1]["reason"] == "done"
    saved = client.get(f"/sessions/{session_id}").json()["messages"]
    assert [(m["role"], m["content"] if m["role"] == "user" else "…") for m in saved] == [
        ("user", "hi"),
        ("assistant", "…"),
        ("user", "shorter please"),
        ("assistant", "…"),
    ]


def test_queues_hand_over_one_message_or_all_of_them() -> None:
    one, everything = MessageQueue(), MessageQueue("all")
    for queue in (one, everything):
        queue.messages = [UserMessage(content="a"), UserMessage(content="b")]

    assert user_texts(one.drain()) == ["a"] and user_texts(one.drain()) == ["b"]
    assert user_texts(everything.drain()) == ["a", "b"] and everything.drain() == []


def test_after_the_last_empty_follow_up_poll_the_run_refuses_new_messages() -> None:
    run = ActiveRun(session_id="s", messages=[], history_size=0)
    assert run.queue("follow_up", UserMessage(content="a"))
    assert asyncio.run(run.take_follow_ups()) != []
    assert run.accepting  # the run goes on with "a"

    assert asyncio.run(run.take_follow_ups()) == []
    # The loop is about to end, so a message queued now would never be sent: it gets a 409 instead.
    assert not run.queue("steer", UserMessage(content="too late"))


def test_continue_after_a_stop_answers_the_prompt_without_adding_one(
    client: TestClient, provider: GatedProvider
) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    url = f"/sessions/{session_id}"
    assert client.post(f"{url}/continue").status_code == 422  # empty: nothing to answer
    assert client.get(url).json()["can_continue"] is False

    # Stopped mid-reply: the transcript ends with the prompt, still waiting for an answer.
    post = in_background(lambda: client.post(f"{url}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    assert client.post(f"{url}/continue").status_code == 409  # a run is going
    assert client.post(f"{url}/abort").status_code == 204
    post["thread"].join(5)
    assert client.get(url).json()["can_continue"] is True

    provider.waiting.clear()
    cont = in_background(lambda: client.post(f"{url}/continue"))
    assert provider.waiting.wait(5)
    portal(client).call(provider.gate.set)
    cont["thread"].join(5)

    events = sse_events(cont["result"].text)
    assert events[-1]["reason"] == "done"
    assert [m["role"] for m in events[-1]["new_messages"]] == ["assistant"]  # no second "hi"
    detail = client.get(url).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    # Answered now: continuing again would have the model reply to itself.
    assert detail["can_continue"] is False
    assert client.post(f"{url}/continue").status_code == 422
    assert client.get(url).json()["running"] is False  # the refused claim gave its slot back


def test_a_streaming_run_holds_no_database_connection_while_the_model_works(
    client: TestClient, provider: GatedProvider
) -> None:
    # Postgres connections are few (ADR-027): a stream that kept one for the whole run would starve other requests.
    app = cast(FastAPI, client.app)
    # Titled already, so the prompt commits no title before the stream starts.
    session_id = client.post("/sessions", json={"title": "Named"}).json()["id"]
    post = in_background(lambda: client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    watch = in_background(lambda: client.get(f"/sessions/{session_id}/events"))
    run = app.state.runs.get(session_id)
    deadline = time.monotonic() + 5
    while len(run.subscribers) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(run.subscribers) == 2

    assert app.state.db_factory.kw["bind"].pool.checkedout() == 0
    portal(client).call(provider.gate.set)
    post["thread"].join(5)
    watch["thread"].join(5)


def test_every_event_of_a_run_has_the_next_id_of_that_run(client: TestClient, provider: GatedProvider) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    portal(client).call(provider.gate.set)
    body = client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}).text

    ids = sse_ids(body)
    assert len(ids) == len(sse_events(body))
    run_ids = {i.split(".")[0] for i in ids}
    assert len(run_ids) == 1
    assert [int(i.split(".")[1]) for i in ids] == list(range(1, len(ids) + 1))


def test_reconnecting_with_last_event_id_sends_only_the_missed_events(
    client: TestClient, provider: GatedProvider
) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    runs: RunManager = cast(FastAPI, client.app).state.runs
    post = in_background(lambda: client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    active = runs.get(session_id)
    assert active is not None
    # The client saw up to the "Hel" delta, then lost its connection.
    seen = next(e for e in active.recent if e.event.type == "message_update")

    async def open_gate_soon() -> None:
        await asyncio.sleep(0.2)
        provider.gate.set()

    portal(client).start_task_soon(open_gate_soon)
    response = client.get(f"/sessions/{session_id}/events", headers={"Last-Event-ID": active.event_id(seen.seq)})
    post["thread"].join(5)

    events = sse_events(response.text)
    # No snapshot: the client keeps what it has and adds the rest, so "Hel" isn't shown twice.
    assert events[0]["type"] != "run_snapshot"
    assert [e["event"]["delta"] for e in events if e["type"] == "message_update"] == ["lo"]
    assert events[-1]["type"] == "agent_end"
    # Together with what the client saw, the ids cover the run once each.
    full = sse_ids(post["result"].text)
    assert full[: seen.seq] + sse_ids(response.text) == full


def test_an_id_from_another_run_gets_a_snapshot_instead(client: TestClient, provider: GatedProvider) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    post = in_background(lambda: client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    portal(client).start_task_soon(provider.gate.set)
    events = sse_events(client.get(f"/sessions/{session_id}/events", headers={"Last-Event-ID": "oldrun.7"}).text)
    post["thread"].join(5)
    assert events[0]["type"] == "run_snapshot"


def test_resume_replays_only_when_every_missed_event_is_still_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agentspace.runs, "REPLAY_SIZE", 3)
    run = ActiveRun(session_id="s", messages=[], history_size=0)
    for turn in range(1, 6):
        run.publish(TurnStartEvent(turn=turn))  # events 1..5; only 3..5 are kept

    resumed = run.resume(run.event_id(2))
    assert resumed is not None
    assert [e.seq for e in resumed[0]] == [3, 4, 5]
    up_to_date = run.resume(run.event_id(5))
    assert up_to_date is not None and up_to_date[0] == []
    assert run.resume(run.event_id(1)) is None  # event 2 is gone
    assert run.resume(run.event_id(6)) is None  # from the future
    assert run.resume("someotherrun.3") is None
    assert run.resume(f"{run.run_id}.x") is None
    assert run.resume("garbage") is None


def test_a_quiet_stream_gets_a_heartbeat_comment(
    client: TestClient, provider: GatedProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FastAPI sends `: ping` after 15 s of silence; shortened here so the test is quick.
    monkeypatch.setattr(fastapi.routing, "_PING_INTERVAL", 0.05)
    session_id = client.post("/sessions", json={}).json()["id"]

    async def open_gate_soon() -> None:
        await asyncio.sleep(0.3)
        provider.gate.set()

    portal(client).start_task_soon(open_gate_soon)
    body = client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}).text
    assert ": ping" in body.splitlines()
    assert sse_events(body)[-1]["reason"] == "done"
