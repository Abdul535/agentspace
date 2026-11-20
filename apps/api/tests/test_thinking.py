"""Thinking (ADR-012): reasoning is streamed, saved, sent back to the same model, and set per session."""

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from agentspace.agent.compaction import estimate_tokens
from agentspace.agent.events import MessageStartEvent, MessageUpdateEvent
from agentspace.ai.catalog import to_model_info
from agentspace.ai.messages import AssistantMessage, TextContent, ThinkingContent, ToolCall, UserMessage
from agentspace.ai.provider import Context, DoneEvent, ErrorEvent, TextDeltaEvent, ThinkingDeltaEvent
from agentspace.ai.providers.fake import FakeProvider, text_reply
from agentspace.ai.providers.openai_compat import OpenAICompatProvider
from agentspace.api.deps import provider_lookup
from agentspace.config import Settings
from agentspace.main import create_app
from agentspace.runs import ActiveRun

pytestmark = pytest.mark.anyio


def sse(*chunks: dict | str) -> bytes:
    """A streamed Chat Completions body made of these chunks."""
    return "".join(f"data: {c if isinstance(c, str) else json.dumps(c)}\n\n" for c in chunks).encode()


def delta(**fields: object) -> dict:
    """One chunk whose only choice carries this delta."""
    return {"choices": [{"delta": fields}]}


def recording_provider(body: bytes, requests: list[httpx.Request]) -> OpenAICompatProvider:
    """A provider whose endpoint records each request and answers with `body`."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=body)

    return OpenAICompatProvider(
        name="openrouter", base_url="https://example.test/v1", api_key="key", transport=httpx.MockTransport(handler)
    )


async def test_reasoning_streams_first_and_is_saved_before_the_answer() -> None:
    requests: list[httpx.Request] = []
    body = sse(
        delta(reasoning="Two plus "),
        # Some endpoints send the same text in two fields; it must not show up twice.
        delta(reasoning="two.", reasoning_content="two."),
        delta(content="4"),
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {
            "choices": [],
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 9,
                "total_tokens": 12,
                "completion_tokens_details": {"reasoning_tokens": 7},
            },
        },
        "[DONE]",
    )
    provider = recording_provider(body, requests)
    context = Context(messages=[UserMessage(content="2+2?")], thinking_level="high")
    events = [e async for e in provider.stream("some/model", context)]

    streamed = [(type(e).__name__, e.delta) for e in events if isinstance(e, ThinkingDeltaEvent | TextDeltaEvent)]
    assert streamed == [("ThinkingDeltaEvent", "Two plus "), ("ThinkingDeltaEvent", "two."), ("TextDeltaEvent", "4")]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    thought = ThinkingContent(thinking="Two plus two.", signature="reasoning")
    assert done.message.content == [thought, TextContent(text="4")]
    assert done.message.text() == "4" and done.message.thinking() == "Two plus two."
    assert done.message.usage.reasoning == 7

    assert json.loads(requests[0].content)["reasoning"] == {"effort": "high"}


async def test_thinking_off_sends_no_reasoning_field() -> None:
    requests: list[httpx.Request] = []
    provider = recording_provider(sse(delta(content="hi"), "[DONE]"), requests)
    [e async for e in provider.stream("some/model", Context(messages=[UserMessage(content="hi")]))]
    assert "reasoning" not in json.loads(requests[0].content)


async def test_reasoning_details_are_joined_and_win_as_the_signature() -> None:
    body = sse(
        delta(reasoning="Let me", reasoning_details=[{"type": "reasoning.text", "text": "Let me", "index": 0}]),
        delta(
            reasoning=" check.",
            reasoning_details=[{"type": "reasoning.text", "text": " check.", "signature": "sig", "index": 0}],
        ),
        delta(reasoning_details=[{"type": "reasoning.encrypted", "data": "opaque", "index": 1}]),
        delta(content="Done."),
        "[DONE]",
    )
    events = [e async for e in recording_provider(body, []).stream("m", Context())]
    done = events[-1]
    assert isinstance(done, DoneEvent)
    block = done.message.content[0]
    assert isinstance(block, ThinkingContent) and block.thinking == "Let me check."
    assert json.loads(block.signature or "") == [
        {"type": "reasoning.text", "text": "Let me check.", "index": 0, "signature": "sig"},
        {"type": "reasoning.encrypted", "data": "opaque", "index": 1},
    ]


async def test_an_error_keeps_the_thinking_that_arrived() -> None:
    body = sse(delta(reasoning="Hmm"), {"error": {"message": "upstream died"}})
    events = [e async for e in recording_provider(body, []).stream("m", Context())]
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].message.thinking() == "Hmm"


def reply_with(signature: str | None, *, model: str = "some/model") -> AssistantMessage:
    """A reply that thought, then called a tool."""
    return AssistantMessage(
        provider="openrouter",
        model=model,
        content=[ThinkingContent(thinking="plan", signature=signature), ToolCall(id="c1", name="ls")],
        stop_reason="tool_use",
    )


def sent_assistant(provider: OpenAICompatProvider, reply: AssistantMessage) -> dict:
    """The assistant message as the next request to `some/model` would send it."""
    body = provider.build_request_body("some/model", Context(messages=[UserMessage(content="hi"), reply]))
    return next(m for m in body["messages"] if m["role"] == "assistant")


def test_thinking_goes_back_to_the_model_that_wrote_it_and_no_other() -> None:
    provider = recording_provider(b"", [])
    details = [{"type": "reasoning.encrypted", "data": "opaque"}]

    assert sent_assistant(provider, reply_with(json.dumps(details)))["reasoning_details"] == details
    assert sent_assistant(provider, reply_with("reasoning_content"))["reasoning_content"] == "plan"

    # Another model's signed reasoning would be refused, and thinking without a signature has no way back.
    other = sent_assistant(provider, reply_with(json.dumps(details), model="other/model"))
    unsigned = sent_assistant(provider, reply_with(None))
    for sent in (other, unsigned):
        assert not {"reasoning", "reasoning_content", "reasoning_details"} & sent.keys()


def test_a_late_client_sees_the_thinking_streamed_so_far() -> None:
    run = ActiveRun(session_id="s", messages=[], history_size=0)
    run.track(MessageStartEvent(message=AssistantMessage(provider="p", model="m")))
    for event in (ThinkingDeltaEvent(delta="Hm"), ThinkingDeltaEvent(delta="m."), TextDeltaEvent(delta="Hi")):
        run.track(MessageUpdateEvent(event=event))
    assert run.streaming is not None
    assert run.streaming.content == [ThinkingContent(thinking="Hmm."), TextContent(text="Hi")]


def test_thinking_counts_toward_the_token_estimate() -> None:
    plain = AssistantMessage(provider="p", model="m", content=[TextContent(text="x" * 40)])
    thought = AssistantMessage(
        provider="p", model="m", content=[ThinkingContent(thinking="y" * 400), TextContent(text="x" * 40)]
    )
    assert estimate_tokens(thought) == estimate_tokens(plain) + 100


def test_the_catalog_says_which_models_can_think() -> None:
    raw = {"id": "a/b", "pricing": {}, "supported_parameters": ["tools", "reasoning"]}
    assert to_model_info(raw).reasoning is True
    assert to_model_info({**raw, "supported_parameters": ["tools"]}).reasoning is False


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider([])


@pytest.fixture
def client(tmp_path: Path, fake: FakeProvider) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    app.dependency_overrides[provider_lookup] = lambda: lambda name: fake
    with TestClient(app) as client:
        yield client


def test_the_thinking_level_is_stored_switched_and_sent_with_each_run(client: TestClient, fake: FakeProvider) -> None:
    session = client.post("/sessions", json={"thinking_level": "low"}).json()
    assert session["thinking_level"] == "low"
    url = f"/sessions/{session['id']}"
    assert client.patch(url, json={"thinking_level": "extreme"}).status_code == 422

    updated = client.patch(url, json={"thinking_level": "high"})
    client.patch(url, json={"thinking_level": "high"})  # not a switch: no second notice
    assert updated.json()["thinking_level"] == "high"
    assert updated.json()["model"] == session["model"]  # a field left out stays as it was

    fake.replies = [text_reply("Hi.")]
    client.post(f"{url}/messages", json={"content": "hi"})
    assert fake.contexts[-1].thinking_level == "high"
    notices = [m["text"] for m in client.get(url).json()["messages"] if m["role"] == "notice"]
    assert notices == ["Switched thinking from low to high"]
