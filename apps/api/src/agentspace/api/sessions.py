"""HTTP routes for sessions: create, list, read, change, archive, prompt, continue, compact, watch, steer or stop."""

import base64
import binascii
from collections.abc import AsyncIterable, AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from asyncer import asyncify
from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel, Field

from agentspace.agent.compaction import build_context, can_compact
from agentspace.agent.events import AgentEvent
from agentspace.agent.loop import can_continue
from agentspace.agent.messages import AgentMessage, NoticeMessage
from agentspace.agent.run_recipe import CompactRun, ContinueRun, PromptRun, Run, RunKind, model_lookup, run_recipe
from agentspace.ai.catalog import OpenRouterCatalog
from agentspace.ai.images import MAX_INPUT_BYTES, ImageError, base64_size, process_image
from agentspace.ai.messages import (
    TextContent,
    ThinkingLevel,
    UserContent,
    UserMessage,
)
from agentspace.ai.providers import PROVIDERS
from agentspace.api.deps import (
    CatalogDep,
    DBDep,
    RunDep,
    RunsDep,
    SandboxesDep,
    SessionRowDep,
    SettingsDep,
    StoreDep,
)
from agentspace.api.mcp_servers import McpDep, McpServerConfigsDep
from agentspace.config import Settings
from agentspace.mcp.manager import McpManager, ServerConfig
from agentspace.runs import LoopFn, QueueKind, RunManager, RunSnapshot
from agentspace.session.db import release
from agentspace.session.models import SessionRow
from agentspace.session.store import SessionStore
from agentspace.tools import TOOL_SETS, ToolSetName
from agentspace.workspace.sandboxes import Sandboxes

router = APIRouter(prefix="/sessions", tags=["sessions"])  # TS: a sub-router, like `new Hono().basePath("/sessions")`


class SessionCreate(BaseModel):
    """Fields for a new session; anything left out uses the app's defaults."""

    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    """The prompt's preamble; the tool list, rules and workspace sections are added on every run."""
    tool_set: ToolSetName | None = None
    """Which preset of tools the session gets; stored as the list of tool names."""
    thinking_level: ThinkingLevel | None = None
    """How hard the model thinks; "off" (the model's own default) when left out."""
    title: str | None = None


class SessionPublic(BaseModel):
    """A session as the API returns it."""

    id: str
    title: str | None
    provider: str
    model: str
    system_prompt: str
    tools: list[str]
    thinking_level: ThinkingLevel
    created_at: datetime
    updated_at: datetime
    running: bool = False
    """Whether a run is in progress; a client that just loaded can attach to it at `GET .../events`."""


# TS: interface SessionDetail extends SessionPublic
class SessionDetail(SessionPublic):
    """A session with its full transcript."""

    messages: list[AgentMessage]
    can_continue: bool
    """Whether `POST .../continue` can run: the transcript ends with the model still owing a reply."""


class SessionUpdate(BaseModel):
    """Fields to change on a session: the model (ADR-005) and how hard it thinks (ADR-012); unset ones stay."""

    model: str | None = Field(default=None, min_length=1)
    thinking_level: ThinkingLevel | None = None


def public(session: SessionRow, running: bool) -> SessionPublic:
    """The session as the API returns it, with whether it is running right now (on any server)."""
    out = SessionPublic.model_validate(session, from_attributes=True)
    out.running = running
    return out


async def is_running(runs: RunManager, store: SessionStore, session_id: str) -> bool:
    """Whether the session has a run going, on whichever server drives it (ADR-029)."""
    return await runs.active(store.db, session_id) is not None


class PromptCreate(BaseModel):
    """A text message for a run: steering or a follow-up."""

    content: str = Field(min_length=1)  # TS: z.string().min(1)


# Base64 of the largest image accepted, plus room for a `data:image/...;base64,` prefix.
MAX_IMAGE_BASE64 = base64_size(MAX_INPUT_BYTES) + 64


class ImageInput(BaseModel):
    """An image attached to a prompt, as base64; its type is read from the bytes, not trusted from the client."""

    data: str = Field(max_length=MAX_IMAGE_BASE64)
    """Base64 of the image file, with or without a `data:<type>;base64,` prefix."""


class MessageCreate(BaseModel):
    """A new user message to run the agent on: text, images (ADR-015), or both."""

    content: str = ""
    images: list[ImageInput] = Field(default=[], max_length=5)


async def prompt_message(body: MessageCreate) -> UserMessage:
    """The body as the user message to save: text first, then each image shrunk to send. 422 if it is unusable."""
    if not body.content.strip() and not body.images:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "Send some text or an image.")
    if not body.images:
        return UserMessage(content=body.content)
    blocks: list[UserContent] = [TextContent(text=body.content)] if body.content else []
    for i, image in enumerate(body.images, start=1):
        # TS: str.partition is like split(",", 2) that always returns three parts; it drops a data-URL prefix.
        data = image.data.partition(",")[2] if image.data.startswith("data:") else image.data
        try:
            raw = base64.b64decode(data, validate=True)
            # Decoding and resizing are CPU work, so they run on a worker thread, off the event loop.
            blocks.append((await asyncify(process_image)(raw)).image)
        except (binascii.Error, ImageError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Image {i}: {exc}") from exc
    return UserMessage(content=blocks)


# TS: the decorator registers the handler, like router.post("/", handler). The parameter and return types
# become the validated request body and response schema (and the OpenAPI spec).
@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionCreate, store: StoreDep, settings: SettingsDep) -> SessionPublic:
    """Create a session; the provider must be registered."""
    provider = body.provider or settings.default_provider
    if provider not in PROVIDERS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Unknown provider '{provider}'. Known: {', '.join(sorted(PROVIDERS))}.",
        )
    session = await store.create(
        provider=provider,
        model=body.model or settings.default_model,
        system_prompt=body.system_prompt or settings.default_system_prompt,
        tools=list(TOOL_SETS[body.tool_set or settings.default_tool_set]),
        thinking_level=body.thinking_level or "off",
        title=body.title,
    )
    return public(session, running=False)


@router.get("")
async def list_sessions(store: StoreDep, runs: RunsDep) -> list[SessionPublic]:
    """All sessions, most recently active first."""
    running = await runs.running_sessions(store.db, store.user_id)
    # TS: (await store.listAll()).map((s) => public(s, running.has(s.id)))
    return [public(s, s.id in running) for s in await store.list_all()]


@router.get("/{session_id}")
async def get_session(session: SessionRowDep, store: StoreDep, runs: RunsDep) -> SessionDetail:
    """One session and its saved transcript; while it runs, `GET .../events` also has the reply in progress."""
    # TS: `**dict` spreads keyword arguments: new SessionDetail({ ...public, messages }).
    messages = await store.messages(session.id)
    out = public(session, await is_running(runs, store, session.id))
    return SessionDetail(**out.model_dump(), messages=messages, can_continue=can_continue(build_context(messages)))


@router.patch("/{session_id}")
async def update_session(body: SessionUpdate, session: SessionRowDep, store: StoreDep, runs: RunsDep) -> SessionPublic:
    """Switch the session's model or thinking level; the next run uses them. 409 while a run is in progress.

    A real switch also leaves a notice in the transcript for the reader; the model never sees it (ADR-008).
    """
    # A run reads these once when it starts, so a switch mid-run would look applied but wasn't.
    # It also keeps the notice from landing between a tool call and its result.
    if await is_running(runs, store, session.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for the current run to finish before switching.")
    if body.model is not None and body.model != (previous := session.model):
        await store.set_model(session, body.model)
        await store.append(session, NoticeMessage(text=f"Switched model from {previous} to {body.model}"))
    if body.thinking_level is not None and body.thinking_level != (previous := session.thinking_level):
        await store.set_thinking_level(session, body.thinking_level)
        await store.append(session, NoticeMessage(text=f"Switched thinking from {previous} to {body.thinking_level}"))
    return public(session, running=False)


@router.post("/{session_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_session(session: SessionRowDep, store: StoreDep, runs: RunsDep) -> None:
    """Hide the session from the list; its transcript is kept. 409 while a run is in progress."""
    # A run hidden from the sidebar would keep working where nobody can see or stop it.
    if await is_running(runs, store, session.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for the current run to finish before archiving.")
    await store.archive(session)


# The routes yield `ServerSentEvent`s (for their ids), which FastAPI leaves out of OpenAPI; this puts the
# event types back, so `pnpm gen:api` still gives the web app `AgentEvent` and `RunSnapshot`.
RUN_EVENTS: dict[int | str, dict[str, Any]] = {200: {"model": AgentEvent, "description": "The run's events, as SSE."}}
WATCH_EVENTS: dict[int | str, dict[str, Any]] = {
    200: {"model": RunSnapshot | AgentEvent, "description": "A snapshot, or the missed events, then live ones."}
}


def no_transform(response: Response) -> None:
    """Forbid proxies from compressing the event stream: gzip holds events back until the stream ends."""
    # The Next.js server gzips proxied responses unless Cache-Control has `no-transform`.
    # Set in a dependency because the route's body only runs once the headers are already sent.
    # It goes out next to FastAPI's own `no-cache`; HTTP reads repeated headers as one list.
    response.headers["Cache-Control"] = "no-cache, no-transform"


@router.post(
    "/{session_id}/messages",
    response_class=EventSourceResponse,
    responses=RUN_EVENTS,
    dependencies=[Depends(no_transform)],
)
async def send_message(
    # Listed before `run`, so a bad image is refused before the run's slot is claimed.
    prompt: Annotated[UserMessage, Depends(prompt_message)],
    run: RunDep,
    store: StoreDep,
    settings: SettingsDep,
    runs: RunsDep,
    catalog: CatalogDep,
    mcp: McpDep,
    mcp_servers: McpServerConfigsDep,
    sandboxes: SandboxesDep,
) -> AsyncIterable[ServerSentEvent]:
    """Start a run on a new user message and stream its events as SSE.

    Each SSE `data` field is one `AgentEvent` as JSON; its `type` names the event.
    Its `id` is the event's entry in the session's Redis Stream (ADR-029): send the last one
    as `Last-Event-ID` to `GET .../events` after a dropped connection to get only what was missed.
    The server owns the run (ADR-007): closing this stream only stops listening.
    Reattach with `GET .../events`; stop the run with `POST .../abort`.
    """
    # Untitled sessions take their title from the first prompt.
    if run.session.title is None:
        await store.set_title(run.session, prompt.text()[:80] or "Image")
    # The stream lasts the whole run; its connection serves other requests meanwhile (ADR-027).
    await release(store.db)
    loop = agent_recipe(run, PromptRun(prompt), settings, catalog, mcp, mcp_servers, sandboxes)
    # TS: each `yield` sends one SSE event to the client the moment it happens.
    async for event in start_and_stream(runs, run, loop):
        yield event


def continuable(run: RunDep) -> Run:
    """The claimed run, if its transcript can be continued; 422 otherwise (the slot is then given back)."""
    # Judged on what the model reads: a compacted transcript starts at its latest summary.
    if not can_continue(build_context(run.active.messages)):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Nothing to continue: the transcript is empty or already ends with the agent's reply.",
        )
    return run


@router.post(
    "/{session_id}/continue",
    response_class=EventSourceResponse,
    responses=RUN_EVENTS,
    dependencies=[Depends(no_transform)],
)
async def continue_run(
    run: Annotated[Run, Depends(continuable)],
    db: DBDep,
    settings: SettingsDep,
    runs: RunsDep,
    catalog: CatalogDep,
    mcp: McpDep,
    mcp_servers: McpServerConfigsDep,
    sandboxes: SandboxesDep,
) -> AsyncIterable[ServerSentEvent]:
    """Start a run on the transcript as it is, with no new message: a retry after an error, a stop or a crash.

    Streams like `POST .../messages`. 409 if a run is going, 422 if the transcript
    is empty or ends with the agent's reply (pi's `agentLoopContinue`).
    """
    await release(db)
    loop = agent_recipe(run, ContinueRun(), settings, catalog, mcp, mcp_servers, sandboxes)
    async for event in start_and_stream(runs, run, loop):
        yield event


def compactable(run: RunDep, settings: SettingsDep) -> Run:
    """The claimed run, if its transcript has an older part to summarize; 422 otherwise."""
    if not can_compact(run.active.messages, settings.compaction_keep_recent_tokens):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Nothing to compact: the conversation still fits in what compaction keeps as it is.",
        )
    return run


@router.post(
    "/{session_id}/compact",
    response_class=EventSourceResponse,
    responses=RUN_EVENTS,
    dependencies=[Depends(no_transform)],
)
async def compact_session(
    run: Annotated[Run, Depends(compactable)],
    db: DBDep,
    settings: SettingsDep,
    runs: RunsDep,
) -> AsyncIterable[ServerSentEvent]:
    """Summarize the older part of the transcript now (ADR-009); streams the run's events like `.../continue`.

    409 if a run is going, 422 if the conversation is too short to compact.
    """
    await release(db)
    # No catalog or MCP: a compact run reads no model entry and calls no tools, so they could only make it fail.
    loop = run_recipe(run, CompactRun(), settings=settings)
    async for event in start_and_stream(runs, run, loop):
        yield event


async def stream(runs: RunManager, session_id: str, run_id: str, after: str) -> AsyncIterator[ServerSentEvent]:
    """The run's events after Stream entry `after` as SSE, each with its id so the client can resume after it."""
    async for entry in runs.follow(session_id, run_id, after):
        yield ServerSentEvent(data=entry.event, id=entry.id)


def agent_recipe(
    run: Run,
    kind: RunKind,
    settings: Settings,
    catalog: OpenRouterCatalog,
    mcp: McpManager,
    mcp_servers: list[ServerConfig],
    sandboxes: Sandboxes | None,
) -> LoopFn:
    """The loop of a prompt or continue run: the recipe with the catalog entry, MCP tools and sandbox vendor."""
    lookup = model_lookup(run.session.model, catalog)
    return run_recipe(
        run, kind, settings=settings, lookup_model=lookup, mcp=mcp, mcp_servers=mcp_servers, sandboxes=sandboxes
    )


def start_and_stream(runs: RunManager, run: Run, loop: LoopFn) -> AsyncIterator[ServerSentEvent]:
    """Start the claimed run in the background with its recipe's loop, and stream its events to this client."""
    runs.start(run.active, loop)
    return stream(runs, run.active.session_id, run.active.run_id, run.active.start_id)


@router.get(
    "/{session_id}/events",
    response_class=EventSourceResponse,
    responses=WATCH_EVENTS,
    dependencies=[Depends(no_transform)],
)
async def watch_session(
    session: SessionRowDep,
    store: StoreDep,
    runs: RunsDep,
    last_event_id: Annotated[str | None, Header()] = None,
) -> AsyncIterable[ServerSentEvent]:
    """Attach to the session's run, on whichever server drives it: a `run_snapshot`, then live events until it ends.

    With `Last-Event-ID` (ADR-028), the stream skips the snapshot and sends only the events
    after that id, also when the run has ended since: then they finish with its `agent_end`,
    so a client cut off by a server that stopped still learns why. If they can't be replayed
    (another run, or too old), it falls back to a snapshot. With no run in progress, the
    stream is one snapshot of the saved transcript.
    """
    row = await runs.active(store.db, session.id)
    # TS: `:=` assigns inside the condition, like `if (last && (resumed = await runOf(last)))`.
    if last_event_id and (resumed := await runs.log.run_of(session.id, last_event_id)):
        # A newer run cleared the Stream, so an entry still in it is from this run or the one just ended.
        if row is None or row.id == resumed:
            await release(store.db)
            async for event in stream(runs, session.id, resumed, last_event_id):
                yield event
            return
    if row is not None and (built := await runs.snapshot(session.id, row.id, lambda: store.messages(session.id))):
        snapshot, after = built
        # The snapshot covers every entry up to `after`, so resuming after it starts at the next one.
        yield ServerSentEvent(data=snapshot, id=after)
        # The stream can last the whole run; its connection serves other requests meanwhile (ADR-027).
        await release(store.db)
        async for event in stream(runs, session.id, row.id, after):
            yield event
        return
    # Nothing running, or it ended while we looked: the saved transcript is the whole story.
    yield ServerSentEvent(data=RunSnapshot(running=False, messages=await store.messages(session.id)))


async def queue_message(session: SessionRow, runs: RunManager, kind: QueueKind, content: str) -> None:
    """Queue a user message on the session's run, wherever it runs, or 409 if nothing is running."""
    if not await runs.send(session.id, kind, content):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This session has no run that can take it; send it as a new prompt."
        )


@router.post("/{session_id}/steer", status_code=status.HTTP_202_ACCEPTED)
async def steer_run(body: PromptCreate, session: SessionRowDep, runs: RunsDep) -> None:
    """Queue a message for the model to see after the current turn; it shows up as a `message_end` when it does.

    A run that is already ending drops it: the client gets back what the run never picked up.
    """
    await queue_message(session, runs, "steer", body.content)


@router.post("/{session_id}/follow-up", status_code=status.HTTP_202_ACCEPTED)
async def follow_up_run(body: PromptCreate, session: SessionRowDep, runs: RunsDep) -> None:
    """Queue a message for when the run would otherwise stop; the run then goes on with it."""
    await queue_message(session, runs, "follow_up", body.content)


@router.post(
    "/{session_id}/abort",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={202: {"description": "The stop is on its way; the run's stream ends when it lands."}},
)
async def abort_run(session: SessionRowDep, runs: RunsDep, response: Response) -> None:
    """Stop the session's run; finished messages are kept. 409 if nothing is running.

    204 once it has stopped; 202 if the server driving it hasn't confirmed within a few seconds (ADR-029).
    """
    # TS: `match` is a switch that compares values here: `case None` is `case null`.
    match await runs.abort(session.id):
        case None:
            raise HTTPException(status.HTTP_409_CONFLICT, "This session has no run in progress.")
        case "stopping":
            response.status_code = status.HTTP_202_ACCEPTED
