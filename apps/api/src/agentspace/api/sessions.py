"""HTTP routes for sessions: create, list, read, and send a prompt."""

from collections.abc import AsyncIterable
from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from fastapi.sse import EventSourceResponse
from pydantic import BaseModel, Field

from agentspace.agent.events import AgentEvent, MessageEndEvent
from agentspace.agent.loop import run_agent
from agentspace.agent.tool import ToolContext
from agentspace.ai.messages import Message, UserMessage
from agentspace.ai.providers import PROVIDERS
from agentspace.api.deps import RunDep, SessionRowDep, SettingsDep, StoreDep
from agentspace.tools import default_tools

router = APIRouter(prefix="/sessions", tags=["sessions"])  # TS: a sub-router, like `new Hono().basePath("/sessions")`


class SessionCreate(BaseModel):
    """Fields for a new session; anything left out uses the app's defaults."""

    provider: str | None = None
    model: str | None = None
    system_prompt: str | None = None
    title: str | None = None


class SessionPublic(BaseModel):
    """A session as the API returns it."""

    id: str
    title: str | None
    provider: str
    model: str
    system_prompt: str
    created_at: datetime
    updated_at: datetime


# TS: interface SessionDetail extends SessionPublic
class SessionDetail(SessionPublic):
    """A session with its full transcript."""

    messages: list[Message]


class PromptCreate(BaseModel):
    """A new user message to run the agent on."""

    content: str = Field(min_length=1)  # TS: z.string().min(1)


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
        title=body.title,
    )
    return SessionPublic.model_validate(session, from_attributes=True)


@router.get("")
async def list_sessions(store: StoreDep) -> list[SessionPublic]:
    """All sessions, most recently active first."""
    # TS: (await store.listAll()).map((s) => SessionPublic.parse(s)); from_attributes reads an object's fields.
    return [SessionPublic.model_validate(s, from_attributes=True) for s in await store.list_all()]


@router.get("/{session_id}")
async def get_session(session: SessionRowDep, store: StoreDep) -> SessionDetail:
    """One session and its transcript."""
    public = SessionPublic.model_validate(session, from_attributes=True)
    # TS: `**dict` spreads keyword arguments: new SessionDetail({ ...public, messages }).
    return SessionDetail(**public.model_dump(), messages=await store.messages(session.id))


@router.post("/{session_id}/messages", response_class=EventSourceResponse)
async def send_message(
    body: PromptCreate, run: RunDep, store: StoreDep, settings: SettingsDep
) -> AsyncIterable[AgentEvent]:
    """Run the agent on a new user message and stream its events as SSE.

    Each SSE `data` field is one `AgentEvent` as JSON; its `type` names the event.
    Messages are saved as each one ends, so a dropped connection keeps everything
    finished before it, and the run stops with the connection.
    """
    session = run.session
    history = await store.messages(session.id)
    # Untitled sessions take their title from the first prompt; it is saved with the first message.
    if session.title is None:
        session.title = body.content[:80]
    run.workspace.mkdir(parents=True, exist_ok=True)

    events = run_agent(
        provider=run.provider,
        model=session.model,
        system_prompt=session.system_prompt,
        tools=default_tools(),
        history=history,
        prompt=UserMessage(content=body.content),
        ctx=ToolContext(workspace=run.workspace),
        max_turns=settings.max_turns,
        max_tokens=settings.max_output_tokens,
    )
    # TS: each `yield` sends one SSE event to the client the moment it happens.
    async for event in events:
        if isinstance(event, MessageEndEvent):
            await store.append(session, event.message)
        yield event
