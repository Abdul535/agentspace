"""Background runs (ADR-007) across servers (ADR-029): runs outlive their request, late clients catch up, and runs
can be stopped, from any server."""

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import cast

import fastapi.routing
import pg
import pytest
import redis
from anyio.from_thread import BlockingPortal
from fastapi import FastAPI
from fastapi.testclient import TestClient

import agentspace.run_log
from agentspace.agent.events import AgentEndEvent, MessageEndEvent, MessageStartEvent, TurnStartEvent
from agentspace.agent.loop import Emit
from agentspace.agent.messages import AgentMessage
from agentspace.ai.messages import AssistantMessage, TextContent, UserMessage
from agentspace.ai.provider import AssistantEvent, Context, DoneEvent, StartEvent, TextDeltaEvent
from agentspace.api.deps import provider_lookup
from agentspace.config import Settings
from agentspace.main import create_app
from agentspace.run_log import ENTRY_ID, Entry, RunLog
from agentspace.runs import ActiveRun, MessageQueue, RunManager


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


def make_client(tmp_path: Path, provider: GatedProvider, **settings: object) -> TestClient:
    """An API server on this test's database and Redis; two of them share every run."""
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, **settings))  # ty: ignore[invalid-argument-type]
    app.dependency_overrides[provider_lookup] = lambda: lambda name: provider
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path, provider: GatedProvider) -> Iterator[TestClient]:
    with make_client(tmp_path, provider) as client:
        yield client


@pytest.fixture
def servers(tmp_path: Path, provider: GatedProvider) -> Iterator[tuple[TestClient, TestClient]]:
    """Two API servers, A and B, as two tasks behind a load balancer would be."""
    # TS: ExitStack enters several `with` blocks and exits them all, like a list of disposables.
    with ExitStack() as stack:
        yield (
            stack.enter_context(make_client(tmp_path, provider)),
            stack.enter_context(make_client(tmp_path, provider)),
        )


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


def runs_of(client: TestClient) -> RunManager:
    """The app's run manager."""
    # TS: cast() is `as FastAPI`: TestClient types `app` as any ASGI app, ours is a FastAPI one.
    return cast(FastAPI, client.app).state.runs


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


def stream_entries(session_id: str) -> list[tuple[str, dict[str, str]]]:
    """The session's Redis Stream as a client of Redis sees it: entry ids and their fields."""
    client = redis.Redis.from_url(os.environ["AGENTSPACE_REDIS_URL"], decode_responses=True)
    key = f"{os.environ['AGENTSPACE_REDIS_PREFIX']}session:{session_id}:events"
    return cast(list[tuple[str, dict[str, str]]], client.xrange(key))


def run_statuses(session_id: str) -> list[str]:
    """The status of each of the session's `runs` rows, oldest first."""
    return [status for (status,) in pg.db_rows("SELECT status FROM runs WHERE session_id = $1", session_id)]


def open_gate_soon(provider: GatedProvider) -> Callable[[], object]:
    """A task for the app's loop that lets the reply finish shortly, while the test blocks on a stream."""

    async def later() -> None:
        await asyncio.sleep(0.2)
        provider.gate.set()

    return later


def test_a_late_client_gets_a_snapshot_with_the_partial_reply_then_live_events(
    client: TestClient, provider: GatedProvider
) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    post = in_background(lambda: client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    assert client.get(f"/sessions/{session_id}").json()["running"] is True

    # The GET below blocks until the run ends, so the gate opens from the app's event loop.
    portal(client).start_task_soon(open_gate_soon(provider))
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
    assert run_statuses(session_id) == ["done"]


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
    assert run_statuses(session_id) == ["aborted"]


def test_closing_a_stream_does_not_stop_the_run(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    runs = runs_of(client)

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

        runs.start(active, loop)
        events = runs.follow(session_id, active.run_id, active.start_id)
        first = await anext(events)
        assert first.event is not None and first.event.type == "message_start"
        await events.aclose()  # the client went away

        gate.set()
        await active.done.wait()

    portal(client).call(scenario)
    detail = client.get(f"/sessions/{session_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"] and detail["running"] is False


def test_a_crashed_loop_ends_its_streams_with_an_error_and_frees_the_slot(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    runs = runs_of(client)

    async def scenario() -> list:
        async def no_history() -> list[AgentMessage]:
            return []

        active = await runs.claim(session_id, no_history)

        async def loop(emit: Emit) -> None:
            await emit(TurnStartEvent(turn=1))
            raise RuntimeError("boom")

        runs.start(active, loop)
        events = [entry.event async for entry in runs.follow(session_id, active.run_id, active.start_id)]
        await active.done.wait()
        return events

    events = portal(client).call(scenario)
    assert [e.type for e in events] == ["turn_start", "agent_end"] and events[-1].reason == "error"
    assert not runs.owns(session_id)
    assert run_statuses(session_id) == ["error"]


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
    # The loop is about to end, so a message queued now would never be sent: the run drops it,
    # and the client gets it back when the run ends.
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
    # The refused claim left no row behind; the stopped and the continued run did.
    assert run_statuses(session_id) == ["aborted", "done"]


def test_a_streaming_run_holds_no_database_connection_while_the_model_works(
    client: TestClient, provider: GatedProvider
) -> None:
    # Postgres connections are few (ADR-027): a stream that kept one for the whole run would starve other requests.
    app = cast(FastAPI, client.app)
    # Titled already, so the prompt commits no title before the stream starts.
    session_id = client.post("/sessions", json={"title": "Named"}).json()["id"]
    post = in_background(lambda: client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    redis_client = redis.Redis.from_url(os.environ["AGENTSPACE_REDIS_URL"])

    def blocked_readers() -> int:
        """Redis clients parked on `XREAD`: the streams, this server's control reader, and any other app's."""
        return sum(1 for c in redis_client.client_list() if c["cmd"] == "xread" and c["flags"] == "b")

    before = blocked_readers()
    watch = in_background(lambda: client.get(f"/sessions/{session_id}/events"))
    # The watcher is parked on Redis too, waiting for the next event.
    wait_for(lambda: blocked_readers() > before)

    assert app.state.db_factory.kw["bind"].pool.checkedout() == 0
    portal(client).call(provider.gate.set)
    post["thread"].join(5)
    watch["thread"].join(5)


def test_every_event_of_a_run_has_a_later_stream_id(client: TestClient, provider: GatedProvider) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    portal(client).call(provider.gate.set)
    body = client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}).text

    ids = sse_ids(body)
    assert len(ids) == len(sse_events(body))
    assert all(ENTRY_ID.fullmatch(i) for i in ids)
    # Stream ids order by time, then counter: each is later than the one before.
    keys = [tuple(int(part) for part in i.split("-")) for i in ids]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)


def test_reconnecting_with_last_event_id_sends_only_the_missed_events(
    client: TestClient, provider: GatedProvider
) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    post = in_background(lambda: client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    # The client saw up to the "Hel" delta, then lost its connection.
    updates = [entry_id for entry_id, fields in stream_entries(session_id) if "message_update" in str(fields)]
    seen = updates[0]

    portal(client).start_task_soon(open_gate_soon(provider))
    response = client.get(f"/sessions/{session_id}/events", headers={"Last-Event-ID": seen})
    post["thread"].join(5)

    events = sse_events(response.text)
    # No snapshot: the client keeps what it has and adds the rest, so "Hel" isn't shown twice.
    assert events[0]["type"] != "run_snapshot"
    assert [e["event"]["delta"] for e in events if e["type"] == "message_update"] == ["lo"]
    assert events[-1]["type"] == "agent_end"
    # Together with what the client saw, the ids cover the run once each.
    full = sse_ids(post["result"].text)
    assert full[: full.index(seen) + 1] + sse_ids(response.text) == full


@pytest.mark.parametrize("last_id", ["oldrun.7", "garbage", "1-0"])
def test_an_id_from_another_run_or_before_the_deploy_gets_a_snapshot_instead(
    client: TestClient, provider: GatedProvider, last_id: str
) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    post = in_background(lambda: client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)
    portal(client).start_task_soon(open_gate_soon(provider))
    events = sse_events(client.get(f"/sessions/{session_id}/events", headers={"Last-Event-ID": last_id}).text)
    post["thread"].join(5)
    assert events[0]["type"] == "run_snapshot"


def test_an_id_resumes_only_while_its_entry_is_kept(client: TestClient) -> None:
    runs = runs_of(client)

    async def scenario() -> None:
        log: RunLog = runs.log
        start = await log.begin("s", "run1", saved=0)
        ids = [await log.append("s", "run1", TurnStartEvent(turn=turn)) for turn in range(1, 5)]
        assert await log.run_of("s", start) == "run1" and await log.run_of("s", ids[0]) == "run1"
        assert await log.run_of("s", "run1.3") is None  # an id from before Redis (ADR-028)
        assert await log.run_of("s", "garbage") is None
        await log.redis.xtrim(log.events_key("s"), maxlen=2, approximate=False)
        assert await log.run_of("s", ids[1]) is None  # trimmed: the events after it may be gone too
        assert await log.run_of("s", ids[2]) == "run1"

    portal(client).call(scenario)


def test_a_stream_ends_when_its_run_ended_without_telling_redis(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agentspace.run_log, "BLOCK_MS", 50)  # reads give up quickly, so `alive` is asked soon
    session_id = client.post("/sessions", json={}).json()["id"]
    runs = runs_of(client)

    async def no_history() -> list[AgentMessage]:
        return []

    active = portal(client).call(runs.claim, session_id, no_history)
    # Its row ended, but no end marker reached the Stream (as if Redis lost it in a failover).
    pg.db_rows("UPDATE runs SET status = 'done' WHERE id = $1", active.run_id)

    async def watch() -> list[Entry]:
        return [entry async for entry in runs.follow(session_id, active.run_id, active.start_id)]

    assert portal(client).call(watch) == []


def test_redis_waits_longer_than_a_blocking_read(client: TestClient) -> None:
    # Reads block up to `BLOCK_MS` (events) or `CONTROL_BLOCK_MS` (requests); a shorter socket timeout would fail them.
    timeout = runs_of(client).log.redis.connection_pool.connection_kwargs["socket_timeout"]
    assert timeout > agentspace.run_log.BLOCK_MS / 1000
    assert timeout > agentspace.run_log.CONTROL_BLOCK_MS / 1000


def test_a_quiet_stream_gets_a_heartbeat_comment(
    client: TestClient, provider: GatedProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    # FastAPI sends `: ping` after 15 s of silence; shortened here so the test is quick.
    monkeypatch.setattr(fastapi.routing, "_PING_INTERVAL", 0.05)
    session_id = client.post("/sessions", json={}).json()["id"]
    portal(client).start_task_soon(open_gate_soon(provider))
    body = client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}).text
    assert ": ping" in body.splitlines()
    assert sse_events(body)[-1]["reason"] == "done"


def test_another_server_can_watch_steer_and_report_a_run(
    servers: tuple[TestClient, TestClient], provider: GatedProvider
) -> None:
    a, b = servers
    session_id = a.post("/sessions", json={}).json()["id"]
    post = in_background(lambda: a.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)

    # B never saw this run, yet knows it is going and refuses a second one.
    assert b.get(f"/sessions/{session_id}").json()["running"] is True
    assert b.post(f"/sessions/{session_id}/messages", json={"content": "again"}).status_code == 409
    # B's steer reaches A through A's control Stream.
    assert b.post(f"/sessions/{session_id}/steer", json={"content": "shorter please"}).status_code == 202

    portal(a).start_task_soon(open_gate_soon(provider))
    events = sse_events(b.get(f"/sessions/{session_id}/events").text)
    post["thread"].join(5)

    # B's snapshot has A's reply in progress, rebuilt from Postgres and the Stream alone.
    assert events[0]["type"] == "run_snapshot" and events[0]["streaming"]["content"][0]["text"] == "Hel"
    assert events[-1]["type"] == "agent_end" and events[-1]["reason"] == "done"
    saved = b.get(f"/sessions/{session_id}").json()["messages"]
    assert [m["content"] for m in saved if m["role"] == "user"] == ["hi", "shorter please"]
    assert b.get(f"/sessions/{session_id}").json()["running"] is False


def test_another_server_can_stop_a_run(servers: tuple[TestClient, TestClient], provider: GatedProvider) -> None:
    a, b = servers
    session_id = a.post("/sessions", json={}).json()["id"]
    post = in_background(lambda: a.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
    assert provider.waiting.wait(5)

    # 204 only once A has stopped it and said so in the Stream.
    assert b.post(f"/sessions/{session_id}/abort").status_code == 204
    post["thread"].join(5)
    assert sse_events(post["result"].text)[-1]["reason"] == "aborted"
    assert run_statuses(session_id) == ["aborted"]
    assert not runs_of(a).owns(session_id)


def test_the_cap_on_runs_counts_every_server(tmp_path: Path, provider: GatedProvider) -> None:
    with make_client(tmp_path, provider, max_runs_per_user=1) as a, make_client(tmp_path, provider) as b:
        first, second = (a.post("/sessions", json={}).json()["id"] for _ in range(2))
        post = in_background(lambda: b.post(f"/sessions/{first}/messages", json={"content": "hi"}))
        assert provider.waiting.wait(5)
        # The run is on B, but A counts it.
        response = a.post(f"/sessions/{second}/messages", json={"content": "hi"})
        assert response.status_code == 429 and response.json()["detail"]["code"] == "too_many_runs"
        portal(b).call(provider.gate.set)
        post["thread"].join(5)


def test_a_run_whose_server_went_silent_ends_as_interrupted(tmp_path: Path, provider: GatedProvider) -> None:
    # B checks often and gives up on a silent server quickly, so the test doesn't wait 30 s.
    with (
        make_client(tmp_path, provider) as a,
        make_client(tmp_path, provider, run_heartbeat_seconds=0.05, run_stale_seconds=0.3) as b,
    ):
        session_id = a.post("/sessions", json={}).json()["id"]
        post = in_background(lambda: a.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
        assert provider.waiting.wait(5)
        # A stops checking in, as if it froze or lost its network; its run stays parked at the gate.
        portal(a).call(lambda: runs_of(a).background[0].cancel())

        events = sse_events(b.get(f"/sessions/{session_id}/events").text)
        # Every open stream ends with a cue, A's own too, and the slot is free again.
        assert events[-1]["type"] == "agent_end" and events[-1]["reason"] == "interrupted"
        post["thread"].join(5)
        assert sse_events(post["result"].text)[-1]["reason"] == "interrupted"
        assert run_statuses(session_id) == ["interrupted"]
        assert b.get(f"/sessions/{session_id}").json()["running"] is False

        # When A checks in again, it finds the run ended elsewhere and stops driving it.
        portal(a).call(runs_of(a).beat)
        wait_for(lambda: not runs_of(a).owns(session_id))
        assert run_statuses(session_id) == ["interrupted"]


def test_a_server_that_shuts_down_lets_its_runs_finish_within_the_grace_period(
    tmp_path: Path, provider: GatedProvider
) -> None:
    with make_client(tmp_path, provider) as a:
        session_id = a.post("/sessions", json={}).json()["id"]
        post = in_background(lambda: a.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
        assert provider.waiting.wait(5)
        # The reply finishes while the server is shutting down, as one does during a deploy's drain.
        threading.Timer(0.2, lambda: portal(a).call(provider.gate.set)).start()
        portal(a).call(runs_of(a).shutdown, 5)
        post["thread"].join(5)
        assert sse_events(post["result"].text)[-1]["reason"] == "done"
        assert run_statuses(session_id) == ["done"]


def test_a_run_still_going_when_the_grace_period_ends_is_interrupted(tmp_path: Path, provider: GatedProvider) -> None:
    with make_client(tmp_path, provider) as a:
        session_id = a.post("/sessions", json={}).json()["id"]
        post = in_background(lambda: a.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
        assert provider.waiting.wait(5)
        portal(a).call(runs_of(a).shutdown, 0.2)
        post["thread"].join(5)
        assert run_statuses(session_id) == ["interrupted"]


def test_a_server_that_shuts_down_ends_its_runs_as_interrupted(tmp_path: Path, provider: GatedProvider) -> None:
    with make_client(tmp_path, provider) as a:
        session_id = a.post("/sessions", json={}).json()["id"]
        post = in_background(lambda: a.post(f"/sessions/{session_id}/messages", json={"content": "hi"}))
        assert provider.waiting.wait(5)
    # Leaving the `with` ran the app's shutdown, as a deploy's SIGTERM does; that also cut A's own stream.
    post["thread"].join(5)
    assert run_statuses(session_id) == ["interrupted"]
    *_, (_, closing), (_, end) = stream_entries(session_id)
    assert json.loads(closing["event"])["reason"] == "interrupted" and "end" in end

    # The client reconnects to another server from the last event it saw, and learns why the reply stopped.
    seen = next(entry_id for entry_id, fields in stream_entries(session_id) if "message_update" in str(fields))
    with make_client(tmp_path, provider) as b:
        events = sse_events(b.get(f"/sessions/{session_id}/events", headers={"Last-Event-ID": seen}).text)
        assert events[-1]["type"] == "agent_end" and events[-1]["reason"] == "interrupted"
        assert b.get(f"/sessions/{session_id}").json()["running"] is False
