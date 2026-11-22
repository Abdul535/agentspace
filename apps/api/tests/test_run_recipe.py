"""The run recipe: what a prompt, continue or compact run does, driven without HTTP, Postgres or Redis."""

from pathlib import Path

import pytest
from test_compaction import BIG, overflow, used
from test_mcp import fake_config
from test_skills import make_skill

from agentspace.agent.events import AgentEndEvent, AgentEvent
from agentspace.agent.run_recipe import CompactRun, ContinueRun, PromptRun, Run, RunKind, run_recipe
from agentspace.ai.catalog import ModelInfo
from agentspace.ai.messages import (
    IMAGE_NOT_SHOWN,
    AssistantMessage,
    ImageContent,
    TextContent,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from agentspace.ai.provider import Price, prepare_transcript
from agentspace.ai.providers.fake import FakeProvider, text_reply, tool_reply
from agentspace.config import Settings
from agentspace.mcp.manager import McpManager
from agentspace.runs import ActiveRun
from agentspace.session.models import SessionRow
from agentspace.tools import TOOL_SETS
from agentspace.tracing import RunTrace

pytestmark = pytest.mark.anyio


def model(window: int | None = 1000, image_input: bool = False, price: float | None = None) -> ModelInfo:
    """The session model's catalog entry."""
    return ModelInfo(
        provider="openrouter",
        id="m",
        name="M",
        context_length=window,
        input_price=price,
        output_price=price,
        image_input=image_input,
    )


class Lookup:
    """A model lookup that answers with a fixed entry (None: the catalog doesn't know it) and counts its calls."""

    def __init__(self, info: ModelInfo | None) -> None:
        self.info = info
        self.calls = 0

    async def __call__(self) -> ModelInfo | None:
        self.calls += 1
        return self.info


class Recipe:
    """One session's runs through the recipe, each on a fresh in-memory active run over the transcript so far."""

    def __init__(self, tmp_path: Path, fake: FakeProvider, **settings: object) -> None:
        self.fake = fake
        self.settings = Settings(
            _env_file=None,
            data_dir=tmp_path,
            compaction_reserve_tokens=200,
            compaction_keep_recent_tokens=150,
            **settings,  # ty: ignore[invalid-argument-type]
        )
        self.session = SessionRow(
            id="s", user_id="u", provider="fake", model="m", system_prompt="Be brief.", tools=list(TOOL_SETS["coding"])
        )
        self.lookup = Lookup(model())
        self.mcp = McpManager()
        self.mcp_servers: list = []
        self.messages: list = []

    async def run(self, kind: RunKind, steer: str | None = None, trace: RunTrace | None = None) -> list[AgentEvent]:
        """Run one recipe to its end, saving finished messages the way the run manager does; its events in order."""
        active = ActiveRun(session_id=self.session.id, messages=self.messages[:], history_size=len(self.messages))
        if steer:
            active.queue("steer", UserMessage(content=steer))
        if trace:
            active.trace = trace
        self.active = active
        run = Run(session=self.session, provider=self.fake, workspace=self.workspace, active=active)
        events: list[AgentEvent] = []

        async def emit(event: AgentEvent) -> None:
            events.append(event)
            active.track(event)

        loop = run_recipe(
            run, kind, settings=self.settings, lookup_model=self.lookup, mcp=self.mcp, mcp_servers=self.mcp_servers
        )
        await loop(emit)
        self.messages = active.messages
        return events

    async def prompt(self, text: str = BIG) -> list[str]:
        """A prompt run; its event types."""
        return [e.type for e in await self.run(PromptRun(prompt=UserMessage(content=text)))]

    @property
    def workspace(self) -> Path:
        return self.settings.workspaces_dir / self.session.id

    def roles(self) -> list[str]:
        return [m.role for m in self.messages]


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider([])


@pytest.fixture
def recipe(tmp_path: Path, fake: FakeProvider) -> Recipe:
    return Recipe(tmp_path, fake)


# --- compaction policy (ADR-009) ------------------------------------------------------------------------------------


async def test_a_run_that_ends_near_the_window_compacts_afterwards(recipe: Recipe, fake: FakeProvider) -> None:
    fake.replies = [used(100)]
    await recipe.prompt()
    assert recipe.roles() == ["user", "assistant"]  # 100 tokens: far from the window

    # 900 > 1000 - 200: past the threshold, with older messages to summarize.
    fake.replies = [used(900), text_reply("## Goal\nkeep going")]
    events = await recipe.prompt()

    assert events[-4:] == ["compaction_start", "message_start", "message_end", "compaction_end"]
    assert recipe.roles() == ["user", "assistant", "user", "assistant", "compaction_summary"]

    # The next prompt reads the summary instead of what it replaced.
    fake.replies = [text_reply("ok")]
    await recipe.prompt("next")
    sent = fake.contexts[-1].messages
    assert sent[0].role == "user" and isinstance(sent[0].content, str) and "keep going" in sent[0].content
    assert len(sent) < 6


async def test_an_unknown_window_skips_the_threshold(recipe: Recipe, fake: FakeProvider) -> None:
    recipe.lookup.info = None
    fake.replies = [used(100), used(900_000)]
    await recipe.prompt()
    assert "compaction_start" not in await recipe.prompt()


async def test_an_overflow_compacts_and_retries_once(recipe: Recipe, fake: FakeProvider) -> None:
    fake.replies = [used(100)]
    await recipe.prompt()

    fake.replies = [overflow(), text_reply("S"), text_reply("answered")]
    events = await recipe.prompt()

    assert events.count("agent_start") == 2 and "compaction_end" in events
    assert recipe.roles()[-3:] == ["assistant", "compaction_summary", "assistant"]
    # The retry read the summary, then the prompt that overflowed; the failed reply was dropped.
    # (The real provider adapter drops the failed reply in `prepare_transcript`; the fake sees it raw.)
    retry = prepare_transcript(fake.contexts[-1].messages)
    assert retry[0].role == "user" and retry[-1].role == "user"


async def test_a_second_overflow_ends_the_run_with_the_error(recipe: Recipe, fake: FakeProvider) -> None:
    fake.replies = [used(100)]
    await recipe.prompt()

    fake.replies = [overflow(), text_reply("S"), overflow()]
    events = await recipe.prompt()

    assert events.count("compaction_start") == 1
    last = recipe.messages[-1]
    assert isinstance(last, AssistantMessage) and last.stop_reason == "error"
    assert fake.replies == []


async def test_compaction_switched_off_never_compacts_after_the_loop(tmp_path: Path, fake: FakeProvider) -> None:
    recipe = Recipe(tmp_path, fake, compaction_enabled=False)
    fake.replies = [used(100), used(900)]
    await recipe.prompt()
    assert "compaction_start" not in await recipe.prompt()
    # An overflow is left as the error too: no summary, no retry.
    fake.replies = [overflow()]
    assert "compaction_start" not in await recipe.prompt()
    assert fake.replies == []


async def test_a_short_session_reads_the_catalog_once_per_pass_and_not_for_the_threshold(
    recipe: Recipe, fake: FakeProvider
) -> None:
    fake.replies = [text_reply("hi")]
    await recipe.prompt("hello")
    assert recipe.lookup.calls == 1


# --- the compact run ------------------------------------------------------------------------------------------------


async def test_a_compact_run_is_framed_and_ends_done_when_a_summary_is_saved(
    recipe: Recipe, fake: FakeProvider
) -> None:
    fake.replies = [used(100), used(100)]
    await recipe.prompt()
    await recipe.prompt()

    fake.replies = [text_reply("S")]
    events = await recipe.run(CompactRun())

    assert [e.type for e in events] == [
        "agent_start",
        "compaction_start",
        "message_start",
        "message_end",
        "compaction_end",
        "agent_end",
    ]
    end = events[-1]
    assert isinstance(end, AgentEndEvent) and end.reason == "done"
    assert recipe.roles()[-1] == "compaction_summary" and [m.role for m in end.new_messages] == ["compaction_summary"]


async def test_a_compact_run_that_saves_no_summary_ends_as_an_error(recipe: Recipe, fake: FakeProvider) -> None:
    fake.replies = [used(100), used(100)]
    await recipe.prompt()
    await recipe.prompt()

    fake.replies = []  # the summarizer fails
    events = await recipe.run(CompactRun())

    assert events[0].type == "agent_start"
    end = events[-1]
    assert isinstance(end, AgentEndEvent) and end.reason == "error" and end.new_messages == []
    assert "compaction_summary" not in recipe.roles()


# --- continue -------------------------------------------------------------------------------------------------------


async def test_a_continue_run_answers_the_transcript_without_adding_a_message(
    recipe: Recipe, fake: FakeProvider
) -> None:
    recipe.messages = [UserMessage(content="still there?")]
    fake.replies = [text_reply("yes")]
    await recipe.run(ContinueRun())
    assert recipe.roles() == ["user", "assistant"]


# --- tools, prompt and trace ----------------------------------------------------------------------------------------


async def test_the_workspace_exists_before_the_loop_starts(recipe: Recipe, fake: FakeProvider) -> None:
    assert not recipe.workspace.exists()
    # The model's first tool call already finds the folder there.
    fake.replies = [
        tool_reply(ToolCall(id="c1", name="write", arguments={"path": "a.txt", "content": "hi"})),
        text_reply("ok"),
    ]
    await recipe.prompt("write it")
    assert (recipe.workspace / "a.txt").read_text() == "hi"


async def test_a_read_only_session_never_gets_mcp_tools(recipe: Recipe, fake: FakeProvider) -> None:
    recipe.session.tools = list(TOOL_SETS["read_only"])
    recipe.mcp_servers = [fake_config()]
    fake.replies = [text_reply("ok")]
    try:
        await recipe.prompt("hi")
    finally:
        await recipe.mcp.shutdown()
    assert [t.name for t in fake.contexts[0].tools] == ["read", "grep", "find", "ls"]


async def test_mcp_tools_join_inside_the_run_and_the_prompt_lists_them(recipe: Recipe, fake: FakeProvider) -> None:
    recipe.mcp_servers = [fake_config()]
    fake.replies = [text_reply("ok")]
    active = ActiveRun(session_id="s", messages=[], history_size=0)
    run = Run(session=recipe.session, provider=fake, workspace=recipe.workspace, active=active)
    try:
        # Building the run connects nothing: the servers are only waited for once the run is going.
        run_recipe(
            run, ContinueRun(), settings=recipe.settings, lookup_model=recipe.lookup, mcp=recipe.mcp, mcp_servers=[]
        )
        assert recipe.mcp.sessions == {}
        await recipe.prompt("hi")
    finally:
        await recipe.mcp.shutdown()
    assert "mcp__fake__echo" in [t.name for t in fake.contexts[0].tools]
    assert "- mcp__fake__echo: [fake] Echo the text back." in fake.contexts[0].system_prompt


class RecordingTrace(RunTrace):
    """A no-op trace that keeps the system prompts it was handed."""

    def __init__(self) -> None:
        self.system_prompts: list[str] = []

    def set_system_prompt(self, system_prompt: str) -> None:
        self.system_prompts.append(system_prompt)


async def test_the_system_prompt_is_handed_to_the_trace(recipe: Recipe, fake: FakeProvider) -> None:
    trace = RecordingTrace()
    fake.replies = [text_reply("ok")]
    await recipe.run(PromptRun(prompt=UserMessage(content="hi")), trace=trace)
    assert trace.system_prompts == [fake.contexts[0].system_prompt]
    assert fake.contexts[0].system_prompt.startswith("Be brief.")


# --- images (ADR-015) -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("info", "sees"),
    [(model(image_input=False), False), (model(image_input=True), True), (None, True)],
    ids=["known-blind", "known-seeing", "unknown"],
)
async def test_images_become_a_note_only_for_a_model_known_to_be_blind(
    recipe: Recipe, fake: FakeProvider, info: ModelInfo | None, sees: bool
) -> None:
    recipe.lookup.info = info
    fake.replies = [text_reply("seen")]
    image = ImageContent(data="aGk=", mime_type="image/png")
    await recipe.run(PromptRun(prompt=UserMessage(content=[TextContent(text="look"), image])))

    sent = fake.contexts[0].messages[0]
    assert isinstance(sent, UserMessage) and not isinstance(sent.content, str)
    assert (image in sent.content) is sees
    assert any(isinstance(b, TextContent) and IMAGE_NOT_SHOWN in b.text for b in sent.content) is not sees
    # The transcript keeps the image either way.
    saved = recipe.messages[0]
    assert isinstance(saved, UserMessage) and image in saved.content


# --- skills (ADR-016) -----------------------------------------------------------------------------------------------


async def test_skills_are_rescanned_each_run_and_their_folders_are_readable(recipe: Recipe, fake: FakeProvider) -> None:
    fake.replies = [text_reply("ok")]
    await recipe.prompt("hi")
    assert "pdf-tools" not in fake.contexts[-1].system_prompt

    # Added between runs, with no restart.
    skill = make_skill(recipe.settings.resolved_skills_dir / "pdf-tools", "name: pdf-tools\ndescription: Read PDFs.")
    fake.replies = [tool_reply(ToolCall(id="c1", name="read", arguments={"path": str(skill)})), text_reply("read it")]
    await recipe.prompt("use the pdf skill")

    assert "pdf-tools" in fake.contexts[-2].system_prompt
    result = next(m for m in reversed(recipe.messages) if isinstance(m, ToolResultMessage))
    assert not result.is_error and "Do the thing." in str(result.content)


# --- queues (ADR-003 step 14) ---------------------------------------------------------------------------------------


async def test_a_steer_message_queued_on_the_run_reaches_the_loop(recipe: Recipe, fake: FakeProvider) -> None:
    fake.replies = [text_reply("turning")]
    await recipe.run(PromptRun(prompt=UserMessage(content="go")), steer="turn left")
    sent = [m.content for m in fake.contexts[0].messages if isinstance(m, UserMessage)]
    assert sent == ["go", "turn left"]
    assert [m.role for m in recipe.messages] == ["user", "user", "assistant"]


# --- pricing (ADR-019) ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("info", "price"), [(model(price=1.0), Price(input=1.0, output=1.0)), (None, None)])
async def test_each_reply_is_priced_from_the_catalog_or_costs_nothing(
    recipe: Recipe, fake: FakeProvider, info: ModelInfo | None, price: Price | None
) -> None:
    # The provider adapter turns this price into the reply's cost when it isn't billed; None means $0.
    recipe.lookup.info = info
    fake.replies = [text_reply("hi")]
    await recipe.prompt("hi")
    assert fake.contexts[0].price == price
