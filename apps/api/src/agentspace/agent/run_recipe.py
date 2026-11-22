"""The run recipe: what a run of a given kind does, built from a claimed run; driven by the run manager.

Everything that decides what a run does lives here, not in the HTTP routes: the workspace folder,
which tools it gets (built-in plus MCP, ADR-017), the skills rescan (ADR-016), the system prompt and
its trace (ADR-018), the image rule (ADR-015), the steer and follow-up queues, pricing (ADR-019) and
the compaction policy (ADR-009). It takes plain values and callables, not FastAPI dependencies, so a
test or a later worker process (ADR-022) can build a run without a request.

ADR-009 places the threshold and overflow checks in `stream_run` in the sessions routes; they moved here.
"""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from asyncer import asyncify

from agentspace.agent.compaction import build_context, can_compact, compact, context_tokens
from agentspace.agent.events import AgentEndEvent, AgentStartEvent, CompactionReason
from agentspace.agent.hooks import LoopConfig
from agentspace.agent.loop import Emit, run_loop
from agentspace.agent.messages import AgentMessage, convert_to_llm
from agentspace.agent.prompt import build_system_prompt
from agentspace.agent.skills import Skill, load_skills
from agentspace.agent.tool import Tool, ToolContext
from agentspace.ai.catalog import CatalogError, ModelInfo, OpenRouterCatalog
from agentspace.ai.messages import AssistantMessage, Message, UserMessage, strip_images
from agentspace.ai.overflow import is_context_overflow
from agentspace.ai.provider import Provider
from agentspace.config import Settings
from agentspace.mcp.manager import McpManager, ServerConfig
from agentspace.runs import ActiveRun, LoopFn
from agentspace.session.models import SessionRow
from agentspace.tools import allows_mcp, build_tools


# TS: `frozen=True` makes instances read-only, like `Readonly<{...}>` enforced at runtime.
@dataclass(frozen=True)
class Run:
    """A claimed run: the session, its provider, its workspace and its claimed run slot."""

    session: SessionRow
    provider: Provider
    workspace: Path
    active: ActiveRun


@dataclass(frozen=True)
class PromptRun:
    """A run on a new user message."""

    prompt: UserMessage


@dataclass(frozen=True)
class ContinueRun:
    """A run on the transcript as it is: a retry after an error, a stop or a crash."""


@dataclass(frozen=True)
class CompactRun:
    """A run that only summarizes the older part of the transcript; it needs no catalog and no MCP."""


# TS: a union of classes, narrowed with `match`/`isinstance` like a discriminated union on `kind`.
type RunKind = PromptRun | ContinueRun | CompactRun

# Looks up the session model in the catalog when the run needs it; None when the catalog doesn't know it.
type ModelLookup = Callable[[], Awaitable[ModelInfo | None]]


def model_lookup(model: str, catalog: OpenRouterCatalog) -> ModelLookup:
    """How a run finds what its model can do (window, images, prices) in the catalog (ADR-005), fetched if asked."""

    async def lookup() -> ModelInfo | None:
        """The model's catalog entry, or None if the catalog can't be reached or doesn't list it."""
        try:
            models = await catalog.models()
        except CatalogError:
            return None
        # TS: `next(<generator>, None)` is like `models.find((m) => m.id === model) ?? null`, stopping at the first.
        return next((m for m in models if m.id == model), None)

    return lookup


def run_recipe(
    run: Run,
    kind: RunKind,
    *,  # TS: everything after `*` must be passed by name, like a single options object
    settings: Settings,
    lookup_model: ModelLookup | None = None,
    mcp: McpManager | None = None,
    mcp_servers: Sequence[ServerConfig] = (),
) -> LoopFn:
    """The loop function for this kind of run, for the run manager to drive with its own `emit`.

    A prompt or continue run looks its model up with `lookup_model` (none: treated as unknown) and gets the
    tools of `mcp_servers` from `mcp`; a compact run uses neither, so its route needn't provide them.
    """
    session = run.session
    active = run.active

    async def compact_now(emit: Emit, reason: CompactionReason) -> bool:
        """Summarize the older part of the transcript; False if it failed or there was nothing to summarize."""
        # The one compaction call every kind of run makes, so their arguments can't drift apart.
        return await compact(
            active.messages[:],
            provider=run.provider,
            model=session.model,
            keep_recent_tokens=settings.compaction_keep_recent_tokens,
            max_tokens=settings.max_output_tokens,
            reason=reason,
            emit=emit,
            thinking_level=session.thinking_level,
        )

    # TS: an inner function closing over this run's values; the manager calls it with its own `emit`.
    async def compact_only(emit: Emit) -> None:
        """A run that only compacts, framed by `agent_start`/`agent_end` like any other run."""
        await emit(AgentStartEvent())
        saved = await compact_now(emit, "manual")
        new = active.messages[-1:] if saved else []
        await emit(AgentEndEvent(reason="done" if saved else "error", new_messages=new))

    if isinstance(kind, CompactRun):
        return compact_only
    prompt = kind.prompt if isinstance(kind, PromptRun) else None

    async def find_model() -> ModelInfo | None:
        """The model's catalog entry, or None when there's no catalog or it doesn't know the model."""
        return await lookup_model() if lookup_model else None

    async def agent(emit: Emit, message: UserMessage | None, tools: list[Tool], skills: list[Skill]) -> None:
        """One pass of the agent loop over the transcript so far, with `message` added first if given."""
        info = await find_model()
        # Built per pass, after MCP joined, so the prompt lists exactly the tools the model gets.
        system_prompt = build_system_prompt(session.system_prompt, tools, skills)
        active.trace.set_system_prompt(system_prompt)
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
            history=build_context(active.messages),
            prompt=message,
            ctx=ToolContext(workspace=run.workspace, read_roots=tuple(skill.base_dir for skill in skills)),
            max_turns=settings.max_turns,
            max_tokens=settings.max_output_tokens,
            thinking_level=session.thinking_level,
            # Prices each reply when OpenRouter doesn't bill it; an unknown model's replies cost $0.
            price=info.price() if info else None,
            # Messages the user queues during the run (`POST .../steer`, `.../follow-up`).
            config=LoopConfig(
                get_steering_messages=active.take_steering,
                get_follow_up_messages=active.take_follow_ups,
                convert_to_llm=to_llm,
            ),
            emit=emit,
        )

    async def over_threshold() -> bool:
        """Whether the context is within the reserve of the model's window, with an older part to summarize."""
        # Checked first, so a short session never needs the catalog.
        if not can_compact(active.messages, settings.compaction_keep_recent_tokens):
            return False
        info = await find_model()
        limit = info.context_length if info else None
        tokens = context_tokens(build_context(active.messages))
        return limit is not None and tokens > limit - settings.compaction_reserve_tokens

    async def prompt_or_continue(emit: Emit) -> None:
        """This run: workspace, tools and skills, the agent loop, then compaction if the context needs it (ADR-009)."""
        run.workspace.mkdir(parents=True, exist_ok=True)
        tools: list[Tool] = build_tools(session.tools, bash=settings.enable_bash)
        # Rescanned every run, so an added or edited skill shows without a restart (ADR-016).
        # TS: asyncify runs the blocking folder scan on a worker thread and gives back a promise of its result.
        skills, _ = await asyncify(load_skills)(settings.resolved_skills_dir)
        # Inside the run, so the stream is already open while slow servers are waited for.
        # Never for a read-only session: an MCP tool can do anything its server does, writes included.
        if mcp and mcp_servers and allows_mcp(session.tools):
            tools.extend(await mcp.tools_for(session.id, run.workspace, list(mcp_servers)))
        await agent(emit, prompt, tools, skills)
        if not settings.compaction_enabled:
            return
        last = active.messages[-1] if active.messages else None
        if isinstance(last, AssistantMessage) and is_context_overflow(last):
            # Once only: if the retry overflows too, compacting again won't make room, so the error stands.
            if await compact_now(emit, "overflow"):
                await agent(emit, None, tools, skills)
            return
        if await over_threshold():
            await compact_now(emit, "threshold")

    return prompt_or_continue
