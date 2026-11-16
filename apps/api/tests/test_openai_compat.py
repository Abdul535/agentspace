import json

import httpx
import pytest

from agentspace.ai.messages import AssistantMessage, TextContent, ToolCall, ToolResultMessage, UserMessage
from agentspace.ai.provider import (
    INTERRUPTED_TOOL_RESULT,
    Context,
    DoneEvent,
    ErrorEvent,
    TextDeltaEvent,
    ToolCallEndEvent,
    ToolSpec,
    prepare_transcript,
)
from agentspace.ai.providers.openai_compat import OpenAICompatProvider

pytestmark = pytest.mark.anyio


def sse(*chunks: dict | str) -> bytes:
    lines = [": OPENROUTER PROCESSING\n\n"]
    for chunk in chunks:
        lines.append(f"data: {chunk if isinstance(chunk, str) else json.dumps(chunk)}\n\n")
    return "".join(lines).encode()


def provider(handler, api_key: str | None = "key") -> OpenAICompatProvider:
    return OpenAICompatProvider(
        name="openrouter", base_url="https://example.test/v1", api_key=api_key, transport=httpx.MockTransport(handler)
    )


async def collect(p: OpenAICompatProvider, context: Context | None = None) -> list:
    return [e async for e in p.stream("some/model", context or Context(messages=[UserMessage(content="hi")]))]


async def test_streams_text_and_tool_calls() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = sse(
            {"choices": [{"delta": {"content": "Let me "}}]},
            {"choices": [{"delta": {"content": "check."}}]},
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_1", "function": {"name": "read", "arguments": '{"pa'}}
                            ]
                        }
                    }
                ]
            },
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'th": "a.txt"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            "[DONE]",
        )
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

    context = Context(
        max_tokens=1000,
        system_prompt="sys",
        messages=[UserMessage(content="hi")],
        tools=[ToolSpec(name="read", description="Read a file", parameters={"type": "object"})],
    )
    events = await collect(provider(handler), context)

    assert [e.delta for e in events if isinstance(e, TextDeltaEvent)] == ["Let me ", "check."]
    tool_end = next(e for e in events if isinstance(e, ToolCallEndEvent))
    assert tool_end.tool_call == ToolCall(id="call_1", name="read", arguments={"path": "a.txt"})
    done = events[-1]
    assert isinstance(done, DoneEvent)
    assert done.message.stop_reason == "tool_use"
    assert done.message.text() == "Let me check."
    assert done.message.usage.total == 15

    sent = json.loads(requests[0].content)
    assert requests[0].url == "https://example.test/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer key"
    assert sent["stream"] is True
    assert sent["max_tokens"] == 1000
    assert sent["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert sent["tools"][0]["function"]["name"] == "read"


async def test_invalid_tool_arguments_are_reported_not_raised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=sse(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "id": "c", "function": {"name": "write", "arguments": "{oops"}}
                                ]
                            }
                        }
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
                "[DONE]",
            ),
        )

    events = await collect(provider(handler))
    call = events[-1].message.tool_calls()[0]
    assert call.arguments == {} and call.arguments_error and "{oops" in call.arguments_error


async def test_http_error_becomes_error_event() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "No auth credentials found", "code": 401}})

    events = await collect(provider(handler))
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].message.stop_reason == "error"
    assert events[-1].message.error_message == "HTTP 401: No auth credentials found"


async def test_error_inside_stream_keeps_partial_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=sse({"choices": [{"delta": {"content": "par"}}]}, {"error": {"message": "upstream died"}})
        )

    events = await collect(provider(handler))
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].message.error_message == "upstream died"
    assert events[-1].message.text() == "par"


async def test_missing_api_key_is_an_error_event() -> None:
    events = await collect(provider(lambda r: httpx.Response(200), api_key=None))
    assert len(events) == 1 and isinstance(events[0], ErrorEvent)
    assert "No API key" in events[0].message.error_message


def test_prepare_transcript_repairs_interrupted_runs() -> None:
    call = ToolCall(id="c1", name="read", arguments={"path": "a"})
    failed = AssistantMessage(provider="p", model="m", stop_reason="error", content=[TextContent(text="half")])
    transcript = [
        UserMessage(content="one"),
        AssistantMessage(provider="p", model="m", stop_reason="tool_use", content=[call]),
        # The run was cancelled here: no tool result for c1.
        UserMessage(content="two"),
        failed,
        ToolResultMessage(tool_call_id="stray", tool_name="read", content=[TextContent(text="x")]),
    ]
    repaired = prepare_transcript(transcript)

    assert [m.role for m in repaired] == ["user", "assistant", "tool_result", "user"]
    stand_in = repaired[2]
    assert isinstance(stand_in, ToolResultMessage)
    assert stand_in.tool_call_id == "c1" and stand_in.is_error
    assert stand_in.content[0].text == INTERRUPTED_TOOL_RESULT
