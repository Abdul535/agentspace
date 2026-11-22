"""Runs the server owns (ADR-007), shared by every API server (ADR-029).

The HTTP request that starts a run is only its first watcher: closing it stops the
listening, not the run. A `runs` row in Postgres says which server drives each
session's run, and that server checks in every few seconds; a run whose server goes
silent is marked interrupted by whichever server notices. The driving server writes
the run's events to the session's Redis Stream (`run_log.py`), so any server can
stream them, resume a dropped stream from its last id (ADR-028), or rebuild the run
so far as a `RunSnapshot`. Stop, steer and follow-up requests that land on another
server reach the driving one through its control Stream.

While a run is going, the user can queue messages (ADR-003 step 14): steering goes
in after the current turn, follow-ups once the run would otherwise stop.
"""

import asyncio
import contextlib
import logging
import os
import socket
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import ColumnElement, func, insert, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import col, delete, select
from sqlmodel.ext.asyncio.session import AsyncSession

from agentspace.agent.events import (
    AgentEndEvent,
    AgentEvent,
    EndReason,
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
from agentspace.run_log import Control, Entry, RunLog
from agentspace.session.db import release
from agentspace.session.models import LOCAL_USER_ID, RunRow, RunStatus, SessionRow, UserRow
from agentspace.session.store import SessionStore
from agentspace.tracing import RunTrace, Tracer

log = logging.getLogger(__name__)

ABORT_WAIT_SECONDS = 10.0
"""How long a stop request for another server's run waits for it to end before answering "on its way"."""

# How each way a run can end is recorded on its row.
STATUS: dict[EndReason, RunStatus] = {
    "done": "done",
    "max_turns": "done",
    "finish_turn": "done",
    "terminated": "done",
    "aborted": "aborted",
    "error": "error",
    "interrupted": "interrupted",
}


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


# The loop with everything bound except `emit`, which the manager supplies.
# TS: type LoopFn = (emit: Emit) => Promise<void>
type LoopFn = Callable[[Emit], Awaitable[None]]

# Loads a session's saved transcript, oldest message first.
type LoadMessages = Callable[[], Awaitable[list[AgentMessage]]]


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


# TS: @dataclass writes the constructor from the fields; eq=False keeps identity equality.
@dataclass(eq=False)
class ActiveRun:
    """One session's run on the server driving it: its task, its queues and the state a snapshot shows."""

    session_id: str
    messages: list[AgentMessage]
    """History at start plus every message the run has finished."""
    history_size: int
    user_id: str = LOCAL_USER_ID
    """Who owns the session; their runs at once are capped (ADR-026)."""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    """The run's `runs` row; every entry it writes to the session's Stream carries it."""
    start_id: str = "0-0"
    """The Stream entry the run began with; the stream of the request that started it reads from here."""
    # TS: `field(default_factory=...)` gives each instance a fresh value; a plain `= []` would be shared.
    streaming: AssistantMessage | None = None
    pending_tools: list[str] = field(default_factory=list)
    task: asyncio.Task[None] | None = None
    reason: EndReason | None = None
    """Why the loop said it ended, from its latest `agent_end`; None while it hasn't."""
    stop_reason: EndReason = "aborted"
    """What a cancelled run ends as: stopped by the user, or interrupted by the server (shutdown, lost row)."""
    done: asyncio.Event = field(default_factory=asyncio.Event)
    """Set once the run's row and Stream say it is over."""
    steering: MessageQueue = field(default_factory=MessageQueue)
    follow_ups: MessageQueue = field(default_factory=MessageQueue)
    accepting: bool = True
    """Whether queued messages can still reach the model; False once the run has made its last poll."""
    trace: RunTrace = field(default_factory=RunTrace)
    """The run's Langfuse trace (ADR-018); a no-op until `RunManager.start` sets it, or when tracing is off."""

    @property
    def ended(self) -> bool:
        """Whether the loop already sent an `agent_end`."""
        return self.reason is not None

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

    def snapshot(self) -> RunSnapshot:
        """The run's state now; copied, because the run keeps changing it after this returns."""
        return RunSnapshot(
            running=True,
            messages=list(self.messages),
            streaming=self.streaming.model_copy(deep=True) if self.streaming else None,
            pending_tools=list(self.pending_tools),
        )

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
            case AgentEndEvent(reason=reason):
                self.reason = reason


# The filter for runs still going.
RUNNING = col(RunRow.status) == "running"


class RunManager:
    """Claims, drives and watches runs; at most one per session and `max_per_user` per user, across every server."""

    def __init__(
        self,
        db_factory: async_sessionmaker[AsyncSession],
        redis: Redis,
        *,
        prefix: str = "agentspace:",
        tracer: Tracer | None = None,
        max_per_user: int | None = None,
        heartbeat_seconds: float = 5.0,
        stale_seconds: float = 30.0,
    ) -> None:
        self.db_factory = db_factory
        self.log = RunLog(redis, prefix)
        self.tracer = tracer or Tracer(None)
        self.max_per_user = max_per_user
        """`None` = no cap."""
        self.heartbeat_seconds = heartbeat_seconds
        self.stale_seconds = stale_seconds
        self.owner = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        """This server, as its runs' rows name it; new on every start, so a restarted server never inherits runs."""
        self.runs: dict[str, ActiveRun] = {}
        """The runs this server drives, by session."""
        self.background: list[asyncio.Task[None]] = []
        self.finishing: set[asyncio.Task[None]] = set()

    def open(self) -> None:
        """Start checking in for this server's runs and reading the requests other servers send it."""
        self.background = [
            asyncio.create_task(self.keep_alive(), name="runs:heartbeat"),
            asyncio.create_task(self.listen(), name="runs:control"),
        ]

    def owns(self, session_id: str) -> bool:
        """Whether this server drives the session's run (claimed or started)."""
        return session_id in self.runs

    async def active(self, db: AsyncSession, session_id: str) -> RunRow | None:
        """The session's running run, on whichever server drives it."""
        return (await db.exec(select(RunRow).where(col(RunRow.session_id) == session_id, RUNNING))).first()

    async def running_sessions(self, db: AsyncSession, user_id: str) -> set[str]:
        """Ids of the user's sessions that have a run going."""
        result = await db.exec(select(RunRow.session_id).where(col(RunRow.user_id) == user_id, RUNNING))
        return set(result.all())

    async def sweep(self, db: AsyncSession, *where: ColumnElement[bool], status: RunStatus = "interrupted") -> bool:
        """End the running runs whose server has gone silent (among `where`); True if there were any.

        Every open stream of each gets an `agent_end` and the end marker, so its client knows what happened.
        """
        # The database's clock decides, so servers whose clocks disagree still agree on what is stale.
        stale = col(RunRow.heartbeat_at) < func.now() - timedelta(seconds=self.stale_seconds)
        result = await db.exec(
            update(RunRow)
            .where(RUNNING, stale, *where)
            .values(status=status, ended_at=func.now())
            .returning(col(RunRow.id), col(RunRow.session_id))
        )
        # Row locks hand each stale row to one sweeper only, so each run gets one closing event.
        swept = result.all()
        await db.commit()
        for run_id, session_id in swept:
            log.warning("Run %s of session %s lost its server; ended as %s", run_id, session_id, status)
            reason: EndReason = "aborted" if status == "aborted" else "interrupted"
            await self.log.end(session_id, run_id, AgentEndEvent(reason=reason, new_messages=[]))
        return bool(swept)

    async def claim(self, session_id: str, load_history: LoadMessages, user_id: str = LOCAL_USER_ID) -> ActiveRun:
        """Take the session's run slot and load its history.

        Raises `SessionBusyError` if the slot is taken, `TooManyRunsError` if the user is at their cap.
        """
        run_id = uuid.uuid4().hex
        # TS: `async with` opens the DB session and closes it when the block ends, like `await using`.
        async with self.db_factory() as db:
            # A dead server's run must not block this prompt, or count against the cap, any longer than it has to.
            await self.sweep(db, or_(col(RunRow.session_id) == session_id, col(RunRow.user_id) == user_id))
            # Lines up this user's claims on every server, so two prompts can't both take the last slot.
            await db.exec(select(UserRow).where(col(UserRow.id) == user_id).with_for_update())
            if await self.active(db, session_id) is not None:
                raise SessionBusyError(session_id)
            if self.max_per_user is not None:
                mine = (
                    await db.exec(
                        select(func.count()).select_from(RunRow).where(col(RunRow.user_id) == user_id, RUNNING)
                    )
                ).one()
                if mine >= self.max_per_user:
                    raise TooManyRunsError(self.max_per_user)
            try:
                await db.exec(
                    insert(RunRow).values(
                        id=run_id,
                        session_id=session_id,
                        user_id=user_id,
                        status="running",
                        owner=self.owner,
                        heartbeat_at=func.now(),
                        started_at=func.now(),
                    )
                )
                await db.commit()
            except IntegrityError as exc:
                # The unique index on running rows: a claim we couldn't see yet got there first.
                raise SessionBusyError(session_id) from exc
        try:
            # No other run can append now, so this is the full transcript.
            history = await load_history()
            run = ActiveRun(
                session_id=session_id, messages=list(history), history_size=len(history), user_id=user_id, run_id=run_id
            )
            run.start_id = await self.log.begin(session_id, run_id, len(history))
        except BaseException:
            await self.forget(run_id)
            raise
        self.runs[session_id] = run
        return run

    async def forget(self, run_id: str) -> None:
        """Delete the row of a run that never started."""
        async with self.db_factory() as db:
            await db.exec(delete(RunRow).where(col(RunRow.id) == run_id))
            await db.commit()

    async def release(self, run: ActiveRun) -> None:
        """Give back a claimed slot whose run never started."""
        if run.task is None and self.runs.get(run.session_id) is run:
            del self.runs[run.session_id]
            await self.forget(run.run_id)
            # Someone may already be watching it; end their stream.
            await self.log.end(run.session_id, run.run_id)

    def start(self, run: ActiveRun, loop: LoopFn) -> None:
        """Start the claimed run in the background; its events go to the session's Stream from `run.start_id` on."""
        run.trace = self.tracer.start_run(run.session_id, run.messages)
        # TS: like starting a promise without awaiting it; the task runs on its own until done or cancelled.
        run.task = asyncio.create_task(self.drive(run, loop), name=f"run:{run.session_id}")
        # A done-callback, not a `finally` in `drive`: it runs even if the task is cancelled before it starts.
        run.task.add_done_callback(lambda task: self.finished(run, task))

    async def drive(self, run: ActiveRun, loop: LoopFn) -> None:
        """Run the loop with an `emit` that saves finished messages, tracks state and writes each event to Redis."""
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
                """Save, track and trace, then write: a client never sees a message that isn't saved."""
                saved = isinstance(event, MessageEndEvent)
                if saved:
                    await store.append(session, event.message)
                run.track(event)
                run.trace.observe(event)
                # A finished message is a checkpoint: a snapshot starts from Postgres up to it, then replays the rest.
                await self.log.append(
                    run.session_id,
                    run.run_id,
                    event,
                    saved=len(run.messages) if saved else None,
                    pending=run.pending_tools,
                )

            await loop(emit)

    def finished(self, run: ActiveRun, task: asyncio.Task[None]) -> None:
        """The run's task is done: record that in the background (a done-callback can't await)."""
        closing = asyncio.create_task(self.finish(run, task), name=f"run-end:{run.session_id}")
        # Kept until done: the event loop only holds weak references to tasks.
        self.finishing.add(closing)
        closing.add_done_callback(self.finishing.discard)

    async def finish(self, run: ActiveRun, task: asyncio.Task[None]) -> None:
        """End the run's row, then its Stream (with a closing `agent_end` if the loop didn't send one)."""
        # A run that ended early (max_turns, error, abort) leaves queued messages unsent; the client still has them.
        run.accepting = False
        closing: AgentEndEvent | None = None
        failed = None if task.cancelled() else task.exception()
        if not run.ended:
            # A cancelled or crashed loop never reached its own `agent_end`.
            reason: EndReason = run.stop_reason if task.cancelled() else "error"
            closing = AgentEndEvent(reason=reason, new_messages=run.messages[run.history_size :])
            run.trace.observe(closing)
        if failed:
            log.error("Run for session %s failed", run.session_id, exc_info=failed)
        if task.cancelled():
            status = STATUS[run.stop_reason]
        else:
            status = "error" if failed else STATUS[run.reason or "error"]
        run.trace.end()
        try:
            async with self.db_factory() as db:
                result = await db.exec(
                    update(RunRow)
                    .where(col(RunRow.id) == run.run_id, RUNNING)
                    .values(status=status, ended_at=func.now())
                    .returning(col(RunRow.id))
                )
                ours = result.first() is not None
                await db.commit()
            # The row first, so a client that sees the stream end and reloads finds the session idle.
            # Not ours any more means a sweeper already ended it, closing event and all.
            if ours:
                await self.log.end(run.session_id, run.run_id, closing)
        except Exception:
            log.exception("Couldn't record the end of run %s of session %s", run.run_id, run.session_id)
        finally:
            if self.runs.get(run.session_id) is run:
                del self.runs[run.session_id]
            run.done.set()

    async def snapshot(self, session_id: str, run_id: str, load: LoadMessages) -> tuple[RunSnapshot, str] | None:
        """The run so far and the id of the last Stream entry it covers; None if the run is over.

        Built by any server, from the run's newest checkpoint: the transcript Postgres held then,
        plus the entries after it replayed, which gives the reply being written and the tools running.
        """
        entries = await self.log.since_checkpoint(session_id, run_id)
        if not entries or any(entry.end for entry in entries):
            return None
        checkpoint, *rest = entries  # TS: const [checkpoint, ...rest] = entries
        messages = await load()
        state = ActiveRun(session_id=session_id, messages=messages, history_size=len(messages))
        if checkpoint.saved is not None:
            # Messages are append-only, so the first `saved` ones are the transcript at the checkpoint,
            # its own message included.
            del state.messages[checkpoint.saved :]  # TS: messages.splice(saved)
            state.pending_tools = list(checkpoint.pending)
        else:
            # Trimming took every checkpoint (one reply of over `REPLAY_SIZE` events): replay what's left.
            rest = entries
        for entry in rest:
            if entry.event is not None:
                state.track(entry.event)
        return state.snapshot(), entries[-1].id

    def follow(self, session_id: str, run_id: str, after: str) -> AsyncGenerator[Entry]:
        """The run's events after entry `after`, as they come, until it ends."""

        async def alive() -> bool:
            """Whether the run's row still says it is going."""
            async with self.db_factory() as db:
                return (await db.exec(select(RunRow.id).where(col(RunRow.id) == run_id, RUNNING))).first() is not None

        return self.log.follow(session_id, run_id, after, alive)

    async def send(self, session_id: str, kind: QueueKind, content: str) -> bool:
        """Queue a message on the session's run, on whichever server drives it; False if nothing is running.

        A run that has already made its last poll drops it; the client gets it back when the run ends.
        """
        # TS: `:=` assigns inside the condition, like `if ((run = this.runs.get(id)) != null)`.
        if (run := self.runs.get(session_id)) is not None:
            run.queue(kind, UserMessage(content=content))
            return True
        async with self.db_factory() as db:
            row = await self.active(db, session_id)
            if row is None or await self.sweep(db, col(RunRow.id) == row.id):
                return False
        await self.log.send(row.owner, row.id, kind, content)
        return True

    async def abort(self, session_id: str) -> Literal["stopped", "stopping"] | None:
        """Stop the session's run: "stopped" once it has, "stopping" if its server hasn't confirmed in time.

        None if nothing is running. Finished messages are kept.
        """
        run = self.runs.get(session_id)
        if run is not None:
            if run.task is None:
                return None  # claimed but not started: there is nothing to stop yet
            run.stop_reason = "aborted"
            run.task.cancel()
            # Waits for the row and the Stream too, so the slot is free when this returns.
            await run.done.wait()
            return "stopped"
        async with self.db_factory() as db:
            row = await self.active(db, session_id)
            if row is None:
                return None
            # Its server is gone: nobody would read the request, so end the run here, as the user asked.
            if await self.sweep(db, col(RunRow.id) == row.id, status="aborted"):
                return "stopped"
        after = await self.log.last_id(session_id)
        await self.log.send(row.owner, row.id, "abort")
        try:
            async with asyncio.timeout(ABORT_WAIT_SECONDS):
                async for _ in self.follow(session_id, row.id, after):
                    pass
        except TimeoutError:
            return "stopping"
        return "stopped"

    async def keep_alive(self) -> None:
        """Every `heartbeat_seconds`: check in for this server's runs, and end runs whose server went silent."""
        while True:
            try:
                await self.beat()
            except Exception:
                log.exception("Run heartbeat failed")
            await asyncio.sleep(self.heartbeat_seconds)

    async def beat(self) -> None:
        """One check-in and sweep."""
        # Only runs registered before the update: a claim that lands meanwhile may not be in its result yet.
        before = {run.run_id: run for run in self.runs.values()}
        async with self.db_factory() as db:
            result = await db.exec(
                update(RunRow)
                .where(col(RunRow.owner) == self.owner, RUNNING)
                .values(heartbeat_at=func.now())
                .returning(col(RunRow.id))
            )
            alive = set(result.scalars().all())
            await db.commit()
            await self.sweep(db)
        for run_id, run in before.items():
            # Swept while we were silent (paused, cut off): another server already ended it, so stop driving it.
            if run_id not in alive and run.task is not None and not run.task.done():
                log.warning("Run %s of session %s was ended elsewhere; stopping it", run_id, run.session_id)
                run.stop_reason = "interrupted"
                run.task.cancel()

    async def listen(self) -> None:
        """Carry out the requests other servers leave in this server's control Stream."""
        # From the start: the Stream is new with this server's id, so nothing in it is old.
        after = "0-0"
        while True:
            try:
                for request in await self.log.requests(self.owner, after):
                    after = request.id
                    self.handle(request)
                # Once per read, not per heartbeat: at most every `CONTROL_BLOCK_MS`, well inside the Stream's hour.
                await self.log.keep(self.owner)
            except Exception:
                log.exception("Reading this server's control Stream failed")
                await asyncio.sleep(1)

    def handle(self, request: Control) -> None:
        """Stop the run, or queue a message on it; a request for a run that has ended is dropped."""
        # TS: next(...) with a default is `find(...) ?? null`.
        run = next((run for run in self.runs.values() if run.run_id == request.run), None)
        if run is None:
            return
        if request.kind == "abort":
            if run.task is not None:
                run.stop_reason = "aborted"
                run.task.cancel()
        else:
            run.queue(request.kind, UserMessage(content=request.content))

    async def shutdown(self, grace_seconds: float = 0) -> None:
        """Let runs finish for up to `grace_seconds`, then stop the rest as interrupted (the user stopped nothing)."""
        running = [run for run in self.runs.values() if run.task is not None]
        # Check-ins and stop/steer requests keep working during the grace period, so a finishing run behaves as usual.
        if running and grace_seconds > 0:
            log.info("Shutting down: waiting up to %ss for %d run(s) to finish", grace_seconds, len(running))
            # TS: like Promise.race of all runs settling against a timeout; returns instead of raising on the timeout.
            waits = [asyncio.create_task(run.done.wait()) for run in running]
            _, late = await asyncio.wait(waits, timeout=grace_seconds)
            for wait in late:
                wait.cancel()
        for task in self.background:
            task.cancel()
        running = [run for run in self.runs.values() if run.task is not None and not run.done.is_set()]
        for run in running:
            run.stop_reason = "interrupted"
            assert run.task is not None  # narrowed above, but not across the loop for the type checker
            run.task.cancel()
        await asyncio.gather(*(run.done.wait() for run in running))
        await asyncio.gather(*self.background, return_exceptions=True)
