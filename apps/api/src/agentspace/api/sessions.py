"""HTTP routes for sessions: create, list, read, switch model, send a prompt, watch, steer or stop its run."""

from collections.abc import AsyncIterable
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.sse import EventSourceResponse
from pydantic import BaseModel, Field

from agentspace.agent.events import AgentEvent
from agentspace.agent.hooks import LoopConfig
from agentspace.agent.loop import Emit, run_loop
from agentspace.agent.prompt import build_system_prompt
from agentspace.agent.tool import ToolContext
from agentspace.ai.messages import Message, UserMessage
from agentspace.ai.providers import PROVIDERS
from agentspace.api.deps import RunDep, RunsDep, SessionRowDep, SettingsDep, StoreDep
from agentspace.runs import QueueKind, RunManager, RunSnapshot, read_events
from agentspace.session.models import SessionRow
from agentspace.tools import TOOL_SETS, ToolSetName, build_tools

router = APIRouter(prefix="/sessions", tags=["sessions"])  # TS: a sub-router, like `new Hono().basePath("/sessions")`


class SessionCreate(BaseModel):
    """Fields for a new session; anything left out uses the app's defaults."""

    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    """The prompt's preamble; the tool list, rules and workspace sections are added on every run."""
    tool_set: ToolSetName | None = None
    """Which preset of tools the session gets; stored as the list of tool names."""
    title: str | None = None


class SessionPublic(BaseModel):
    """A session as the API returns it."""

    id: str
    title: str | None
    provider: str
    model: str
    system_prompt: str
    tools: list[str]
    created_at: datetime
    updated_at: datetime
    running: bool = False
    """Whether a run is in progress; a client that just loaded can attach to it at `GET .../events`."""


# TS: interface SessionDetail extends SessionPublic
class SessionDetail(SessionPublic):
    """A session with its full transcript."""

    messages: list[Message]


class SessionUpdate(BaseModel):
    """Fields to change on a session; only `model` for now (ADR-005)."""

    model: str = Field(min_length=1)


def public(session: SessionRow, runs: RunManager) -> SessionPublic:
    """The session as the API returns it, with whether it is running right now."""
    out = SessionPublic.model_validate(session, from_attributes=True)
    out.running = runs.is_running(session.id)
    return out


class PromptCreate(BaseModel):
    """A new user message to run the agent on."""

    content: str = Field(min_length=1)  # TS: z.string().min(1)


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
    return SessionDetail(**public(session, runs).model_dump(), messages=await store.messages(session.id))


@router.patch("/{session_id}")
async def update_session(body: SessionUpdate, session: SessionRowDep, store: StoreDep, runs: RunsDep) -> SessionPublic:
    """Switch the session's model; the next run uses it. 409 while a run is in progress."""
    # A run reads the model once when it starts, so a switch mid-run would look applied but wasn't.
    if runs.is_running(session.id):
        raise HTTPException(status.HTTP_409_CONFLICT, "Wait for the current run to finish before switching models.")
    return public(await store.set_model(session, body.model), runs)


def no_transform(response: Response) -> None:
    """Forbid proxies from compressing the event stream: gzip holds events back until the stream ends."""
    # The Next.js server gzips proxied responses unless Cache-Control has `no-transform`.
    # Set in a dependency because the route's body only runs once the headers are already sent.
    # It goes out next to FastAPI's own `no-cache`; HTTP reads repeated headers as one list.
    response.headers["Cache-Control"] = "no-cache, no-transform"


@router.post("/{session_id}/messages", response_class=EventSourceResponse, dependencies=[Depends(no_transform)])
async def send_message(
    body: PromptCreate, run: RunDep, store: StoreDep, settings: SettingsDep, runs: RunsDep
) -> AsyncIterable[AgentEvent]:
    """Start a run on a new user message and stream its events as SSE.

    Each SSE `data` field is one `AgentEvent` as JSON; its `type` names the event.
    The server owns the run (ADR-007): closing this stream only stops listening.
    Reattach with `GET .../events`; stop the run with `POST .../abort`.
    """
    session = run.session
    # Untitled sessions take their title from the first prompt.
    if session.title is None:
        await store.set_title(session, body.content[:80])
    run.workspace.mkdir(parents=True, exist_ok=True)

    tools = build_tools(session.tools, bash=settings.enable_bash)
    system_prompt = build_system_prompt(session.system_prompt, tools)
    history = run.active.messages[:]  # a copy: the run appends to its own list as messages finish
    prompt = UserMessage(content=body.content)

    # TS: an inner function closing over this request's values; the manager calls it with its own `emit`.
    async def loop(emit: Emit) -> None:
        """This prompt's agent loop."""
        await run_loop(
            provider=run.provider,
            model=session.model,
            system_prompt=system_prompt,
            tools=tools,
            history=history,
            prompt=prompt,
            ctx=ToolContext(workspace=run.workspace),
            max_turns=settings.max_turns,
            max_tokens=settings.max_output_tokens,
            # Messages the user queues during the run (`POST .../steer`, `.../follow-up`).
            config=LoopConfig(
                get_steering_messages=run.active.take_steering,
                get_follow_up_messages=run.active.take_follow_ups,
            ),
            emit=emit,
        )

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
