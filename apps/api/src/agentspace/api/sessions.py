"""HTTP routes for sessions: create, list, read, change, archive, prompt, continue, compact, watch, steer or stop."""

import base64
import binascii
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal

from asyncer import asyncify
from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.sse import EventSourceResponse
from pydantic import BaseModel, Field

from agentspace.agent.compaction import build_context, can_compact, compact, context_tokens
from agentspace.agent.events import AgentEndEvent, AgentEvent, AgentStartEvent
from agentspace.agent.hooks import LoopConfig
from agentspace.agent.loop import Emit, can_continue, run_loop
from agentspace.agent.messages import AgentMessage, NoticeMessage, convert_to_llm
from agentspace.agent.prompt import build_system_prompt
from agentspace.agent.skills import load_skills
from agentspace.agent.tool import Tool, ToolContext
from agentspace.ai.catalog import CatalogError, ModelInfo, OpenRouterCatalog
from agentspace.ai.images import MAX_INPUT_BYTES, ImageError, base64_size, process_image
from agentspace.ai.messages import (
    AssistantMessage,
    Message,
    TextContent,
    ThinkingLevel,
    UserContent,
    UserMessage,
    strip_images,
)
from agentspace.ai.overflow import is_context_overflow
from agentspace.ai.providers import PROVIDERS
from agentspace.api.deps import CatalogDep, Run, RunDep, RunsDep, SessionRowDep, SettingsDep, StoreDep
from agentspace.api.mcp_servers import McpDep, McpServerConfigsDep
from agentspace.config import Settings
from agentspace.mcp.manager import McpManager, ServerConfig
from agentspace.runs import QueueKind, RunManager, RunSnapshot, read_events
from agentspace.session.models import SessionRow
from agentspace.tools import TOOL_SETS, ToolSetName, allows_mcp, build_tools

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


def public(session: SessionRow, runs: RunManager) -> SessionPublic:
    """The session as the API returns it, with whether it is running right now."""
    out = SessionPublic.model_validate(session, from_attributes=True)
    out.running = runs.is_running(session.id)
    return out


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
async def create_session(body: SessionCreate, store: StoreDep, settings: SettingsDep, runs: RunsDep) -> SessionPublic:
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
    return public(session, runs)


@router.get("")
async def list_sessions(store: StoreDep, runs: RunsDep) -> list[SessionPublic]:
    """All sessions, most recently active first."""
    # TS: (await store.listAll()).map((s) => public(s, runs))
    return [public(s, runs) for s in await store.list_all()]


@router.get("/{session_id}")
async def get_session(session: SessionRowDep, store: StoreDep, runs: RunsDep) -> SessionDetail:
    """One session and its saved transcript; while it runs, `GET .../events` also has the reply in progress."""
    # TS: `**dict` spreads keyword arguments: new SessionDetail({ ...public, messages }).
    messages = await store.messages(session.id)
    return SessionDetail(
        **public(session, runs).model_dump(), messages=messages, can_continue=can_continue(build_context(messages))
    )


@router.patch("/{session_id}")
async def update_session(body: SessionUpdate, session: SessionRowDep, store: StoreDep, runs: RunsDep) -> SessionPublic:
    """Switch the session's model or thinking level; the next run uses them. 409 while a run is in progress.

    A real switch also leaves a notice in the transcript for the reader; the model never sees it (ADR-008).
    """
    # A run reads these once when it starts, so a switch mid-run would look applied but wasn't.
    # It also keeps the notice from landing between a tool call and its result.
    if runs.is_running(session.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for the current run to finish before switching.")
    if body.model is not None and body.model != (previous := session.model):
        await store.set_model(session, body.model)
        await store.append(session, NoticeMessage(text=f"Switched model from {previous} to {body.model}"))
    if body.thinking_level is not None and body.thinking_level != (previous := session.thinking_level):
        await store.set_thinking_level(session, body.thinking_level)
        await store.append(session, NoticeMessage(text=f"Switched thinking from {previous} to {body.thinking_level}"))
    return public(session, runs)


@router.post("/{session_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
async def archive_session(session: SessionRowDep, store: StoreDep, runs: RunsDep) -> None:
    """Hide the session from the list; its transcript is kept. 409 while a run is in progress."""
    # A run hidden from the sidebar would keep working where nobody can see or stop it.
    if runs.is_running(session.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for the current run to finish before archiving.")
    await store.archive(session)


def no_transform(response: Response) -> None:
    """Forbid proxies from compressing the event stream: gzip holds events back until the stream ends."""
    # The Next.js server gzips proxied responses unless Cache-Control has `no-transform`.
    # Set in a dependency because the route's body only runs once the headers are already sent.
    # It goes out next to FastAPI's own `no-cache`; HTTP reads repeated headers as one list.
    response.headers["Cache-Control"] = "no-cache, no-transform"


@router.post("/{session_id}/messages", response_class=EventSourceResponse, dependencies=[Depends(no_transform)])
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
) -> AsyncIterable[AgentEvent]:
    """Start a run on a new user message and stream its events as SSE.

    Each SSE `data` field is one `AgentEvent` as JSON; its `type` names the event.
    The server owns the run (ADR-007): closing this stream only stops listening.
    Reattach with `GET .../events`; stop the run with `POST .../abort`.
    """
    # Untitled sessions take their title from the first prompt.
    if run.session.title is None:
        await store.set_title(run.session, prompt.text()[:80] or "Image")
    async for event in stream_run(run, prompt, settings, runs, model_lookup(run, catalog), mcp, mcp_servers):
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


@router.post("/{session_id}/continue", response_class=EventSourceResponse, dependencies=[Depends(no_transform)])
async def continue_run(
    run: Annotated[Run, Depends(continuable)],
    settings: SettingsDep,
    runs: RunsDep,
    catalog: CatalogDep,
    mcp: McpDep,
    mcp_servers: McpServerConfigsDep,
) -> AsyncIterable[AgentEvent]:
    """Start a run on the transcript as it is, with no new message: a retry after an error, a stop or a crash.

    Streams like `POST .../messages`. 409 if a run is going, 422 if the transcript
    is empty or ends with the agent's reply (pi's `agentLoopContinue`).
    """
    async for event in stream_run(run, None, settings, runs, model_lookup(run, catalog), mcp, mcp_servers):
        yield event


def compactable(run: RunDep, settings: SettingsDep) -> Run:
    """The claimed run, if its transcript has an older part to summarize; 422 otherwise."""
    if not can_compact(run.active.messages, settings.compaction_keep_recent_tokens):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Nothing to compact: the conversation still fits in what compaction keeps as it is.",
        )
    return run


@router.post("/{session_id}/compact", response_class=EventSourceResponse, dependencies=[Depends(no_transform)])
async def compact_session(
    run: Annotated[Run, Depends(compactable)], settings: SettingsDep, runs: RunsDep
) -> AsyncIterable[AgentEvent]:
    """Summarize the older part of the transcript now (ADR-009); streams the run's events like `.../continue`.

    409 if a run is going, 422 if the conversation is too short to compact.
    """

    async def loop(emit: Emit) -> None:
        """A run that only compacts, framed by `agent_start`/`agent_end` like any other run."""
        await emit(AgentStartEvent())
        saved = await compact(
            run.active.messages[:],
            provider=run.provider,
            model=run.session.model,
            keep_recent_tokens=settings.compaction_keep_recent_tokens,
            max_tokens=settings.max_output_tokens,
            reason="manual",
            emit=emit,
            thinking_level=run.session.thinking_level,
        )
        new = run.active.messages[-1:] if saved else []
        await emit(AgentEndEvent(reason="done" if saved else "error", new_messages=new))

    queue = runs.start(run.active, loop)
    async for event in read_events(run.active, queue):
        yield event


# Looks up the session model in the catalog when the run needs it; None when the catalog doesn't know it.
type ModelLookup = Callable[[], Awaitable[ModelInfo | None]]


def model_lookup(run: Run, catalog: OpenRouterCatalog) -> ModelLookup:
    """How the run finds what its model can do (window, images): from the catalog (ADR-005), fetched only if asked."""

    async def lookup() -> ModelInfo | None:
        """The model's catalog entry, or None if the catalog can't be reached or doesn't list it."""
        try:
            models = await catalog.models()
        except CatalogError:
            return None
        return next((m for m in models if m.id == run.session.model), None)

    return lookup


async def stream_run(
    run: Run,
    prompt: UserMessage | None,
    settings: Settings,
    runs: RunManager,
    lookup_model: ModelLookup,
    mcp: McpManager,
    mcp_servers: list[ServerConfig],
) -> AsyncIterator[AgentEvent]:
    """Start the claimed run in the background, on a prompt or continuing, and yield its events to this client.

    The tools are the session's built-in ones plus those of the enabled MCP servers that connect in time (ADR-017).

    After the loop, the run compacts if the context neared the window, or compacts and
    retries once if the provider refused it as too long (ADR-009).
    """
    session = run.session
    run.workspace.mkdir(parents=True, exist_ok=True)
    tools: list[Tool] = build_tools(session.tools, bash=settings.enable_bash)
    # Rescanned every run, so an added or edited skill shows without a restart (ADR-016).
    skills, _ = await asyncify(load_skills)(settings.resolved_skills_dir)

    async def agent(emit: Emit, prompt: UserMessage | None) -> None:
        """One pass of the agent loop over what the model reads of the transcript so far."""
        info = await lookup_model()
        # Built here, not once above, so it lists the MCP tools that joined at the start of the run.
        system_prompt = build_system_prompt(session.system_prompt, tools, skills)
        run.active.trace.set_system_prompt(system_prompt)
        # Only when the catalog says so: an unknown model gets the images, and a refusal is a normal error.
        blind = info is not None and not info.image_input

        def to_llm(messages: list[AgentMessage]) -> list[Message]:
            """The default conversion, with images swapped for a note when the model can't see them (ADR-015)."""
            converted = convert_to_llm(messages)
            return strip_images(converted) if blind else converted

        await run_loop(
            provider=run.provider,
            model=session.model,
            system_prompt=system_prompt,
            tools=tools,
            # A copy: the run appends to its own list as messages finish.
            history=build_context(run.active.messages),
            prompt=prompt,
            ctx=ToolContext(workspace=run.workspace, read_roots=tuple(skill.base_dir for skill in skills)),
            max_turns=settings.max_turns,
            max_tokens=settings.max_output_tokens,
            thinking_level=session.thinking_level,
            # Prices each reply when OpenRouter doesn't bill it; an unknown model's replies cost $0.
            price=info.price() if info else None,
            # Messages the user queues during the run (`POST .../steer`, `.../follow-up`).
            config=LoopConfig(
                get_steering_messages=run.active.take_steering,
                get_follow_up_messages=run.active.take_follow_ups,
                convert_to_llm=to_llm,
            ),
            emit=emit,
        )

    async def compact_now(emit: Emit, reason: Literal["threshold", "overflow"]) -> bool:
        """Summarize the older part of the transcript; False if it failed or there was nothing to summarize."""
        return await compact(
            run.active.messages[:],
            provider=run.provider,
            model=session.model,
            keep_recent_tokens=settings.compaction_keep_recent_tokens,
            max_tokens=settings.max_output_tokens,
            reason=reason,
            emit=emit,
            thinking_level=session.thinking_level,
        )

    async def over_threshold() -> bool:
        """Whether the context is within the reserve of the model's window, with an older part to summarize."""
        # Checked first, so a short session never needs the catalog.
        if not can_compact(run.active.messages, settings.compaction_keep_recent_tokens):
            return False
        info = await lookup_model()
        limit = info.context_length if info else None
        tokens = context_tokens(build_context(run.active.messages))
        return limit is not None and tokens > limit - settings.compaction_reserve_tokens

    # TS: an inner function closing over this request's values; the manager calls it with its own `emit`.
    async def loop(emit: Emit) -> None:
        """This run: MCP tools, the agent loop, then compaction if the context needs it."""
        # Inside the run, so the stream is already open while slow servers are waited for.
        # Never for a read-only session: an MCP tool can do anything its server does, writes included.
        if mcp_servers and allows_mcp(session.tools):
            tools.extend(await mcp.tools_for(session.id, run.workspace, mcp_servers))
        await agent(emit, prompt)
        if not settings.compaction_enabled:
            return
        last = run.active.messages[-1] if run.active.messages else None
        if isinstance(last, AssistantMessage) and is_context_overflow(last):
            # Once only: if the retry overflows too, compacting again won't make room, so the error stands.
            if await compact_now(emit, "overflow"):
                await agent(emit, None)
            return
        if await over_threshold():
            await compact_now(emit, "threshold")

    queue = runs.start(run.active, loop)
    # TS: each `yield` sends one SSE event to the client the moment it happens.
    async for event in read_events(run.active, queue):
        yield event


@router.get("/{session_id}/events", response_class=EventSourceResponse, dependencies=[Depends(no_transform)])
async def watch_session(
    session: SessionRowDep, store: StoreDep, runs: RunsDep
) -> AsyncIterable[RunSnapshot | AgentEvent]:
    """Attach to the session's run: a `run_snapshot` first, then live events until the run ends.

    With no run in progress, the stream is one snapshot of the saved transcript.
    """
    active = runs.get(session.id)
    if active is None:
        yield RunSnapshot(running=False, messages=await store.messages(session.id))
        return
    # Snapshot and subscribe with no `await` between them, so no event can fall in the gap.
    snapshot, queue = active.snapshot(), active.subscribe()
    yield snapshot
    async for event in read_events(active, queue):
        yield event


def queue_message(session: SessionRow, runs: RunManager, kind: QueueKind, content: str) -> None:
    """Queue a user message on the session's run, or 409 if no run will pick it up."""
    active = runs.get(session.id)
    if active is None or not active.queue(kind, UserMessage(content=content)):
        raise HTTPException(
            status.HTTP_409_CONFLICT, "This session has no run that can take it; send it as a new prompt."
        )


@router.post("/{session_id}/steer", status_code=status.HTTP_202_ACCEPTED)
async def steer_run(body: PromptCreate, session: SessionRowDep, runs: RunsDep) -> None:
    """Queue a message for the model to see after the current turn; it shows up as a `message_end` when it does."""
    queue_message(session, runs, "steer", body.content)


@router.post("/{session_id}/follow-up", status_code=status.HTTP_202_ACCEPTED)
async def follow_up_run(body: PromptCreate, session: SessionRowDep, runs: RunsDep) -> None:
    """Queue a message for when the run would otherwise stop; the run then goes on with it."""
    queue_message(session, runs, "follow_up", body.content)


@router.post("/{session_id}/abort", status_code=status.HTTP_204_NO_CONTENT)
async def abort_run(session: SessionRowDep, runs: RunsDep) -> None:
    """Stop the session's run; finished messages are kept. 409 if nothing is running."""
    if not await runs.abort(session.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "This session has no run in progress.")
