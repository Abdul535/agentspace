"""Runs the server owns (ADR-007): one background task per running session, watched by subscribers that come and go.

The HTTP request that starts a run is only its first subscriber: closing it stops
the listening, not the run. A client that (re)connects gets a `RunSnapshot`
(transcript so far + the reply being written), then live events, like pi's
`agent.state` followed by `agent.subscribe()`. Every event is numbered (ADR-028),
so a client that lost its connection resumes from the last id it saw, with nothing
missed and nothing twice. Runs live in this process's memory, so the API runs as
one worker.

While a run is going, the user can queue messages (ADR-003 step 14): steering goes
in after the current turn, follow-ups once the run would otherwise stop.
"""

import asyncio
import contextlib
import logging
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

from agentspace.agent.events import (
    AgentEndEvent,
    AgentEvent,
    MessageEndEvent,
    MessageStartEvent,
    MessageUpdateEvent,
    ToolExecutionEndEvent,
    ToolExecutionStartEvent,
)
from agentspace.agent.loop import Emit
from agentspace.agent.messages import AgentMessage
from agentspace.ai.messages import AssistantMessage, TextContent, ThinkingContent, UserMessage
from agentspace.ai.provider import TextDeltaEvent, ThinkingDeltaEvent, ToolCallEndEvent
from agentspace.session.db import release
from agentspace.session.models import LOCAL_USER_ID, SessionRow
from agentspace.session.store import SessionStore
from agentspace.tracing import RunTrace, Tracer

log = logging.getLogger(__name__)

# Events a subscriber may fall behind by before it is dropped; it reconnects and catches up from a snapshot.
SUBSCRIBER_QUEUE_SIZE = 10_000
# Recent events a run keeps for clients that reconnect; one that missed more gets a snapshot instead.
REPLAY_SIZE = 10_000


class RunSnapshot(BaseModel):
    """First event of `GET /sessions/{id}/events`: where the run is, so a late client can catch up."""

    type: Literal["run_snapshot"] = "run_snapshot"
    running: bool
    messages: list[AgentMessage]
    """The whole transcript so far, saved messages included."""
    streaming: AssistantMessage | None = None
    """The assistant reply being written right now, as far as it got."""
    pending_tools: list[str] = []
    """Ids of tool calls still executing."""


# TS: frozen=True makes instances read-only, like `Readonly<{ seq: number; event: AgentEvent }>`.
@dataclass(frozen=True)
class RunEvent:
    """An event and its number in the run: 1 for the first, counting up with no gaps."""

    seq: int
    event: AgentEvent


# What a subscriber's queue carries; None marks the end of the stream.
type Subscriber = asyncio.Queue[RunEvent | None]

# The loop with everything bound except `emit`, which the manager supplies.
# TS: type LoopFn = (emit: Emit) => Promise<void>
type LoopFn = Callable[[Emit], Awaitable[None]]


# How many queued messages one poll hands over: all of them, or the oldest one (pi's default).
type QueueMode = Literal["all", "one_at_a_time"]
type QueueKind = Literal["steer", "follow_up"]


class MessageQueue:
    """Messages waiting for the loop to pick them up, after pi's `PendingMessageQueue`."""

    def __init__(self, mode: QueueMode = "one_at_a_time") -> None:
        self.mode: QueueMode = mode
        self.messages: list[UserMessage] = []

    def drain(self) -> list[AgentMessage]:
        """Take what this poll delivers, oldest first."""
        count = len(self.messages) if self.mode == "all" else min(1, len(self.messages))
        taken: list[AgentMessage] = list(self.messages[:count])
        del self.messages[:count]  # TS: this.messages.splice(0, count)
        return taken


class SessionBusyError(Exception):
    """The session already has a run."""


class TooManyRunsError(Exception):
    """The user already has as many runs going as they may (ADR-026)."""

    def __init__(self, limit: int) -> None:
        super().__init__(limit)
        self.limit = limit


# TS: @dataclass writes the constructor from the fields; eq=False keeps identity equality, so it can sit in a set.
@dataclass(eq=False)
class ActiveRun:
    """One session's run: its task, the live state a late subscriber needs, and its subscribers."""

    session_id: str
    messages: list[AgentMessage]
    """History at start plus every message the run has finished."""
    history_size: int
    user_id: str = LOCAL_USER_ID
    """Who owns the session; their runs at once are capped (ADR-026)."""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    """Names this run in event ids, so an id from an earlier run of the session is never resumed in this one."""
    seq: int = 0
    """Number of the last event published."""
    recent: deque[RunEvent] = field(default_factory=lambda: deque(maxlen=REPLAY_SIZE))
    """The latest events, oldest first, for clients that reconnect (`resume`)."""
    # TS: `field(default_factory=...)` gives each instance a fresh value; a plain `= set()` would be shared.
    subscribers: set[Subscriber] = field(default_factory=set)
    streaming: AssistantMessage | None = None
    pending_tools: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    ended: bool = False
    """Whether subscribers already got an `agent_end`."""
    steering: MessageQueue = field(default_factory=MessageQueue)
    follow_ups: MessageQueue = field(default_factory=MessageQueue)
    accepting: bool = True
    """Whether queued messages can still reach the model; False once the run has made its last poll."""
    trace: RunTrace = field(default_factory=RunTrace)
    """The run's Langfuse trace (ADR-018); a no-op until `RunManager.start` sets it, or when tracing is off."""

    def queue(self, kind: QueueKind, message: UserMessage) -> bool:
        """Queue a message for the loop; False if the run will never pick it up."""
        if not self.accepting:
            return False
        (self.steering if kind == "steer" else self.follow_ups).messages.append(message)
        return True

    async def take_steering(self) -> list[AgentMessage]:
        """`LoopConfig.get_steering_messages` for this run."""
        return self.steering.drain()

    async def take_follow_ups(self) -> list[AgentMessage]:
        """`LoopConfig.get_follow_up_messages` for this run."""
        taken = self.follow_ups.drain()
        # The loop polls follow-ups only after an empty steering poll, and ends when this is empty too.
        # Neither poll awaits anything, so no message can slip in between: from here, refuse new ones.
        if not taken:
            self.accepting = False
        return taken

    def event_id(self, seq: int) -> str:
        """The SSE id of event `seq`, which a client sends back as `Last-Event-ID` to resume after it."""
        return f"{self.run_id}.{seq}"

    def snapshot(self) -> RunSnapshot:
        """The run's state now; copied, because the run keeps changing it after this returns."""
        return RunSnapshot(
            running=True,
            messages=list(self.messages),
            streaming=self.streaming.model_copy(deep=True) if self.streaming else None,
            pending_tools=list(self.pending_tools),
        )

    def subscribe(self) -> Subscriber:
        """A new queue that receives every event from now on."""
        queue: Subscriber = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self.subscribers.add(queue)
        return queue

    def resume(self, last_event_id: str) -> tuple[list[RunEvent], Subscriber] | None:
        """The events after `last_event_id` and a queue for the rest; None if they can't be replayed exactly.

        None when the id is from another run, malformed, or older than what `recent` still holds:
        the client then needs a snapshot. No `await` here, so no event falls between the two.
        """
        run_id, _, seq_text = last_event_id.partition(".")
        if run_id != self.run_id or not seq_text.isdigit():
            return None
        seq = int(seq_text)
        oldest = self.recent[0].seq if self.recent else self.seq + 1
        # Replayable only if every event after `seq` is still in `recent`.
        if seq > self.seq or seq < oldest - 1:
            return None
        # TS: a comprehension, like `recent.filter((e) => e.seq > seq)`.
        return [e for e in self.recent if e.seq > seq], self.subscribe()

    def track(self, event: AgentEvent) -> None:
        """Keep the snapshot state current: finished messages, the reply in progress, running tools."""
        # TS: `match` on classes is like a switch over `instanceof` checks; `case X(message=m)` also reads a field.
        match event:
            case MessageStartEvent(message=AssistantMessage() as message):
                self.streaming = message.model_copy(deep=True)
            case MessageUpdateEvent(event=TextDeltaEvent(delta=delta)) if self.streaming:
                content = self.streaming.content
                # Deltas extend the last text block, as the provider streamed them.
                if content and isinstance(content[-1], TextContent):
                    content[-1].text += delta
                else:
                    content.append(TextContent(text=delta))
            case MessageUpdateEvent(event=ThinkingDeltaEvent(delta=delta)) if self.streaming:
                content = self.streaming.content
                # Thinking comes before the answer, so its block is the first one or not there yet.
                if content and isinstance(content[0], ThinkingContent):
                    content[0].thinking += delta
                else:
                    content.insert(0, ThinkingContent(thinking=delta))
            case MessageUpdateEvent(event=ToolCallEndEvent(tool_call=call)) if self.streaming:
                self.streaming.content.append(call)
            case MessageEndEvent(message=message):
                self.messages.append(message)
                if isinstance(message, AssistantMessage):
                    self.streaming = None
            case ToolExecutionStartEvent(tool_call_id=call_id):
                self.pending_tools.append(call_id)
            case ToolExecutionEndEvent(result=result):
                with contextlib.suppress(ValueError):
                    self.pending_tools.remove(result.tool_call_id)
            case AgentEndEvent():
                self.ended = True

    def publish(self, event: AgentEvent) -> None:
        """Number the event, keep it for replay and hand it to every subscriber; one too far behind is dropped."""
        self.seq += 1
        numbered = RunEvent(self.seq, event)
        self.recent.append(numbered)
        # A copy, because dropping a subscriber changes the set while we loop over it.
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(numbered)
            except asyncio.QueueFull:
                log.warning("Dropping a slow subscriber of session %s", self.session_id)
                self.subscribers.discard(queue)
                end_stream(queue)

    def close(self) -> None:
        """End every subscriber's stream."""
        for queue in self.subscribers:
            end_stream(queue)
        self.subscribers.clear()


def end_stream(queue: Subscriber) -> None:
    """Put the end marker on a queue, making room first if it is full (the client catches up from a snapshot)."""
    while queue.full():
        queue.get_nowait()
    queue.put_nowait(None)


async def read_events(run: ActiveRun, queue: Subscriber) -> AsyncGenerator[RunEvent]:
    """Yield a subscriber's events until the run ends; leaving early (client gone) only unsubscribes."""
    try:
        # TS: `:=` assigns inside the condition, like `while ((event = await queue.get()) !== null)`.
        while (event := await queue.get()) is not None:
            yield event
    finally:
        run.subscribers.discard(queue)


class RunManager:
    """Owns every running session's task; at most one run per session, and `max_per_user` per user."""

    def __init__(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        tracer: Tracer | None = None,
        max_per_user: int | None = None,
    ) -> None:
        self.db_factory = db_factory
        self.tracer = tracer or Tracer(None)
        self.max_per_user = max_per_user
        """`None` = no cap."""
        self.runs: dict[str, ActiveRun] = {}
        # Held from "is it free?" through loading history to registering the run, so two prompts can't both pass.
        self.claim_lock = asyncio.Lock()

    def get(self, session_id: str) -> ActiveRun | None:
        """The session's run, if it has one."""
        return self.runs.get(session_id)

    def is_running(self, session_id: str) -> bool:
        """Whether the session has a run (claimed or started)."""
        return session_id in self.runs

    async def claim(
        self,
        session_id: str,
        load_history: Callable[[], Awaitable[list[AgentMessage]]],
        user_id: str = LOCAL_USER_ID,
    ) -> ActiveRun:
        """Take the session's run slot and load its history.

        Raises `SessionBusyError` if the slot is taken, `TooManyRunsError` if the user is at their cap.
        """
        # TS: `async with lock` is acquire + try/finally release, like `await using`.
        async with self.claim_lock:
            if session_id in self.runs:
                raise SessionBusyError(session_id)
            # Counted under the lock, so two prompts sent together can't both take the last slot.
            mine = sum(1 for run in self.runs.values() if run.user_id == user_id)
            if self.max_per_user is not None and mine >= self.max_per_user:
                raise TooManyRunsError(self.max_per_user)
            # Loaded under the lock: no run can be active now, so this is the full transcript.
            history = await load_history()
            run = ActiveRun(session_id=session_id, messages=list(history), history_size=len(history), user_id=user_id)
            self.runs[session_id] = run
            return run

    def release(self, run: ActiveRun) -> None:
        """Give back a claimed slot whose run never started."""
        if run.task is None and self.runs.get(run.session_id) is run:
            del self.runs[run.session_id]
            run.close()

    def start(self, run: ActiveRun, loop: LoopFn) -> Subscriber:
        """Start the claimed run in the background and return the caller's subscription to it."""
        # Subscribed before the task gets to run, so the caller sees every event from `agent_start` on.
        queue = run.subscribe()
        run.trace = self.tracer.start_run(run.session_id, run.messages)
        # TS: like starting a promise without awaiting it; the task runs on its own until done or cancelled.
        run.task = asyncio.create_task(self.drive(run, loop), name=f"run:{run.session_id}")
        # A done-callback, not a `finally` in `drive`: it runs even if the task is cancelled before it starts.
        run.task.add_done_callback(lambda task: self.finish(run, task))
        return queue

    async def drive(self, run: ActiveRun, loop: LoopFn) -> None:
        """Run the loop with an `emit` that saves finished messages, tracks state and fans events out."""
        # The run outlives the request, so it can't use the request's DB session: it opens its own.
        async with self.db_factory() as db:
            # By id alone: the route that claimed the run already checked who owns the session.
            session = await db.get(SessionRow, run.session_id)
            if session is None:
                raise LookupError(f"Session {run.session_id} disappeared before its run started.")
            store = SessionStore(db, session.user_id)
            # Not held while the model works: each saved message takes a connection only for its write.
            await release(db)

            async def emit(event: AgentEvent) -> None:
                """Save, update snapshot and trace, then publish: a client never sees a message that isn't saved."""
                if isinstance(event, MessageEndEvent):
                    await store.append(session, event.message)
                run.track(event)
                run.trace.observe(event)
                run.publish(event)

            await loop(emit)

    def finish(self, run: ActiveRun, task: asyncio.Task[None]) -> None:
        """Free the slot and end every subscriber's stream, with a closing `agent_end` if the loop didn't send one."""
        # A run that ended early (max_turns, error, abort) leaves queued messages unsent; the client still has them.
        run.accepting = False
        if self.runs.get(run.session_id) is run:
            del self.runs[run.session_id]
        if not run.ended:
            # A cancelled or crashed loop never reached its own `agent_end`.
            if task.cancelled():
                reason: Literal["aborted", "error"] = "aborted"
            else:
                reason = "error"
                log.error("Run for session %s failed", run.session_id, exc_info=task.exception())
            end = AgentEndEvent(reason=reason, new_messages=run.messages[run.history_size :])
            run.trace.observe(end)
            run.publish(end)
        elif not task.cancelled() and (exc := task.exception()):
            log.error("Run for session %s failed after it ended", run.session_id, exc_info=exc)
        run.trace.end()
        run.close()

    async def abort(self, session_id: str) -> bool:
        """Cancel the session's run and wait until it has stopped; False if nothing was running."""
        run = self.runs.get(session_id)
        if run is None or run.task is None:
            return False
        run.task.cancel()
        # Waits without raising the CancelledError, so the slot is free when this returns.
        await asyncio.wait([run.task])
        return True

    async def shutdown(self) -> None:
        """Cancel every run and wait for them, so none is cut off mid-write when the DB closes."""
        tasks = [run.task for run in self.runs.values() if run.task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.wait(tasks)
