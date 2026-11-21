"""Run events and control requests as Redis Streams, so any API server can serve any run (ADR-029).

Each session has one Stream for its run. Only the server driving the run writes to
it, and every SSE stream reads from it, so a client can watch from any server and
resume from the last entry id it saw. Each server also has a control Stream, where
other servers leave stop, steer and follow-up requests for the runs it drives.
"""

import json
import re
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from pydantic import TypeAdapter
from redis.asyncio import Redis
from redis.typing import EncodableT, FieldT

from agentspace.agent.events import AgentEvent

REPLAY_SIZE = 10_000
"""Entries a session's Stream keeps at least; a client that missed more gets a snapshot instead."""
KEEP_SECONDS = 3600
"""How long a Stream outlives the run that wrote it, or the server that read it."""
BLOCK_MS = 5_000
"""How long one read waits for new entries before it asks again."""
SOCKET_TIMEOUT_SECONDS = BLOCK_MS / 1000 + 10
"""How long the client waits on Redis for a reply; well over `BLOCK_MS`, or a quiet read fails as a timeout."""
PAGE = 200
"""Entries read per round when looking back for a checkpoint."""

# Stream entry ids: milliseconds, a dash, then a counter, e.g. `1764000000000-0`.
ENTRY_ID = re.compile(r"\d+-\d+")

# TS: a TypeAdapter validates a type that isn't a model class, like `AgentEvent.parse(...)` on a zod union.
event_adapter: TypeAdapter[AgentEvent] = TypeAdapter(AgentEvent)

type ControlKind = Literal["abort", "steer", "follow_up"]


@dataclass(frozen=True)
class Entry:
    """One entry of a session's Stream: an event, a checkpoint, the end marker, or an event that is a checkpoint."""

    id: str
    run: str
    event: AgentEvent | None = None
    saved: int | None = None
    """Set on checkpoints: how many transcript messages were in Postgres once this entry was written."""
    pending: tuple[str, ...] = ()
    """On checkpoints: the tool calls still running."""
    end: bool = False
    """The run is over: nothing more comes for it."""

    @classmethod
    def parse(cls, entry_id: str, fields: dict[str, str]) -> "Entry":
        """An entry as Redis returns it (all strings) back into its parts."""
        # TS: `"x" in d` is `"x" in obj` / `d.has("x")`.
        return cls(
            id=entry_id,
            run=fields.get("run", ""),
            event=event_adapter.validate_json(fields["event"]) if "event" in fields else None,
            saved=int(fields["saved"]) if "saved" in fields else None,
            pending=tuple(json.loads(fields.get("pending", "[]"))),
            end="end" in fields,
        )


@dataclass(frozen=True)
class Control:
    """A request for a run, left in its server's control Stream by whichever server got it."""

    id: str
    run: str
    kind: ControlKind
    content: str


# Stream entries as the client returns them: `decode_responses=True` makes every value a `str`.
type StreamEntries = list[tuple[str, dict[str, str]]]


def entries(reply: Any) -> StreamEntries:
    """The entries of a one-key `XREAD` reply (`[[key, [(id, fields), ...]]]`), or none."""
    # TS: cast() is `as`: redis-py types replies as `bytes | str`, since it can't see `decode_responses`.
    return cast(StreamEntries, reply[0][1]) if reply else []


class RunLog:
    """Reads and writes the run Streams of one app, under its key prefix."""

    def __init__(self, redis: Redis, prefix: str) -> None:
        self.redis = redis
        self.prefix = prefix

    def events_key(self, session_id: str) -> str:
        """The session's event Stream."""
        return f"{self.prefix}session:{session_id}:events"

    def control_key(self, owner: str) -> str:
        """The control Stream of the server `owner`."""
        return f"{self.prefix}server:{owner}:control"

    async def begin(self, session_id: str, run_id: str, saved: int) -> str:
        """Clear the session's Stream for a new run and write the run's first checkpoint; returns its entry id."""
        key = self.events_key(session_id)
        # TS: `async with` here is like `await using`: the pipeline is reset when the block ends.
        async with self.redis.pipeline(transaction=True) as pipe:
            # The earlier run is over (the `runs` row says so), so only this one's entries are kept from here.
            pipe.xtrim(key, maxlen=0, approximate=False)
            pipe.persist(key)
            pipe.xadd(key, {"run": run_id, "saved": str(saved), "pending": "[]"})
            *_, entry_id = await pipe.execute()
        return cast(str, entry_id)

    async def append(
        self, session_id: str, run_id: str, event: AgentEvent, *, saved: int | None = None, pending: Sequence[str] = ()
    ) -> str:
        """Add one event (a checkpoint too when `saved` is given); returns its entry id, the event's SSE id."""
        fields: dict[FieldT, EncodableT] = {"run": run_id, "event": event.model_dump_json()}
        if saved is not None:
            fields |= {"saved": str(saved), "pending": json.dumps(list(pending))}  # TS: Object.assign(fields, {...})
        # `approximate` trims in whole blocks, which is much cheaper and keeps at least `REPLAY_SIZE`.
        entry_id = await self.redis.xadd(self.events_key(session_id), fields, maxlen=REPLAY_SIZE, approximate=True)
        return cast(str, entry_id)

    async def end(self, session_id: str, run_id: str, closing: AgentEvent | None = None) -> None:
        """Mark the run over, after a closing event if the loop never sent its own; the Stream then expires."""
        key = self.events_key(session_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            if closing is not None:
                closing_fields: dict[FieldT, EncodableT] = {"run": run_id, "event": closing.model_dump_json()}
                pipe.xadd(key, closing_fields, maxlen=REPLAY_SIZE, approximate=True)
            pipe.xadd(key, {"run": run_id, "end": "1"})
            pipe.expire(key, KEEP_SECONDS)
            await pipe.execute()

    async def run_of(self, session_id: str, entry_id: str) -> str | None:
        """The run whose entry `entry_id` is, while the session's Stream still holds it; None otherwise.

        Trimming only drops the oldest entries, so while it is there, every entry after it is too.
        """
        if not ENTRY_ID.fullmatch(entry_id):
            return None
        found = cast(StreamEntries, await self.redis.xrange(self.events_key(session_id), entry_id, entry_id))
        return found[0][1].get("run") if found else None

    async def last_id(self, session_id: str) -> str:
        """The id of the session's newest entry; `0-0` if the Stream is empty."""
        newest = cast(StreamEntries, await self.redis.xrevrange(self.events_key(session_id), count=1))
        return newest[0][0] if newest else "0-0"

    async def follow(
        self, session_id: str, run_id: str, after: str, alive: Callable[[], Awaitable[bool]]
    ) -> AsyncGenerator[Entry]:
        """The run's events after entry `after`, as they are written, until its end marker.

        `alive` is asked whenever a read comes back empty, so a run whose end marker never
        made it to Redis (lost in a failover, say) still ends its streams.
        """
        key = self.events_key(session_id)
        while True:
            # Blocks until something comes or `BLOCK_MS` passes; a client that leaves cancels it.
            found = entries(await self.redis.xread({key: after}, count=500, block=BLOCK_MS))
            if not found and not await alive():
                return
            for entry_id, fields in found:
                after = entry_id
                entry = Entry.parse(entry_id, fields)
                # Another run's entry means a newer run cleared the Stream: ours is over too.
                if entry.run != run_id or entry.end:
                    return
                if entry.event is not None:
                    yield entry

    async def since_checkpoint(self, session_id: str, run_id: str) -> list[Entry]:
        """The run's newest checkpoint and every entry after it, oldest first.

        Starts with a non-checkpoint only if trimming dropped them all; empty if the Stream has nothing of the run.
        """
        key = self.events_key(session_id)
        newer: list[Entry] = []
        top = "+"
        while page := cast(StreamEntries, await self.redis.xrevrange(key, top, "-", count=PAGE)):
            for entry_id, fields in page:
                entry = Entry.parse(entry_id, fields)
                if entry.run != run_id:
                    return newer[::-1]  # TS: [...newer].reverse()
                newer.append(entry)
                if entry.saved is not None:
                    return newer[::-1]
            top = f"({page[-1][0]}"  # `(` makes the bound exclusive, so the next page starts below this one
        return newer[::-1]

    async def send(self, owner: str, run_id: str, kind: ControlKind, content: str = "") -> None:
        """Leave a request for the run in its server's control Stream."""
        key = self.control_key(owner)
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.xadd(key, {"run": run_id, "kind": kind, "content": content})
            # If the server is gone, nobody reads it: it disappears with the server's last renewal.
            pipe.expire(key, KEEP_SECONDS)
            await pipe.execute()

    async def requests(self, owner: str, after: str) -> list[Control]:
        """Requests for `owner`'s runs after entry `after`, waiting up to `BLOCK_MS` for one."""
        reply = await self.redis.xread({self.control_key(owner): after}, count=100, block=BLOCK_MS)
        return [
            Control(id=entry_id, run=fields["run"], kind=cast(ControlKind, fields["kind"]), content=fields["content"])
            for entry_id, fields in entries(reply)
        ]

    async def keep(self, owner: str) -> None:
        """Push back the expiry of `owner`'s control Stream; the server calls it while it is alive."""
        await self.redis.expire(self.control_key(owner), KEEP_SECONDS)
