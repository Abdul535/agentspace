"""Adapter for OpenAI-compatible Chat Completions APIs (OpenRouter, OpenAI, Ollama, vLLM, ...).

Plain httpx against `POST {base_url}/chat/completions` with `stream: true`;
no vendor SDK. Modeled on pi's `packages/ai/src/api/openai-completions.ts`.
"""

import json
from collections.abc import AsyncIterator
from typing import Any, TypeIs

import httpx

from agentspace.ai.messages import (
    AssistantMessage,
    Cost,
    ImageContent,
    Message,
    StopReason,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserContent,
    UserMessage,
    images_of,
    joined_text,
)
from agentspace.ai.provider import (
    AssistantEvent,
    Context,
    DoneEvent,
    ErrorEvent,
    Price,
    StartEvent,
    TextDeltaEvent,
    ThinkingDeltaEvent,
    ToolCallEndEvent,
    prepare_transcript,
)

# Long read timeout: a reply can stream for minutes; connecting should be quick.
DEFAULT_TIMEOUT = httpx.Timeout(600, connect=15)

# The API's `finish_reason` values mapped to ours; anything else counts as `stop`.
FINISH_REASONS: dict[str, StopReason] = {
    "stop": "stop",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "length": "length",
}

# Where a stream's reasoning text can arrive, in the order we look: OpenRouter sends `reasoning`,
# llama.cpp and DeepSeek send `reasoning_content`. Only the first one present is read, since some
# endpoints send the same text in two of them (pi's `reasoningFields`).
REASONING_FIELDS = ("reasoning_content", "reasoning", "reasoning_text")


class OpenAICompatProvider:
    """Streams replies from one OpenAI-compatible endpoint."""

    # TS: the constructor. The bare `*` makes every later parameter keyword-only, like one options object.
    def __init__(
        self,
        *,
        name: str,
        base_url: str,
        api_key: str | None,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # TS: fields come into existence by assigning to `self` (this); there are no field declarations.
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.headers = headers or {}
        self.timeout = timeout
        self.transport = transport

    def build_request_body(self, model: str, context: Context) -> dict[str, Any]:
        """The JSON body for `/chat/completions`, with usage reported in the last chunk."""
        body: dict[str, Any] = {
            "model": model,
            "messages": to_wire_messages(
                context.system_prompt, prepare_transcript(context.messages), provider=self.name, model=model
            ),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if context.max_tokens is not None:
            body["max_tokens"] = context.max_tokens
        # OpenRouter's one reasoning switch for every vendor. "off" sends nothing rather than
        # `effort: "none"`, which models that always think refuse (ADR-012).
        if context.thinking_level != "off":
            body["reasoning"] = {"effort": context.thinking_level}
        if context.tools:
            body["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in context.tools
            ]
        return body

    async def stream(self, model: str, context: Context) -> AsyncIterator[AssistantEvent]:
        """Send the request and translate its stream of chunks into our events."""
        output = AssistantMessage(provider=self.name, model=model)
        if not self.api_key:  # TS: !this.apiKey — None and "" are both falsy
            output.stop_reason = "error"
            output.error_message = f"No API key configured for provider '{self.name}'."
            yield ErrorEvent(message=output)
            return

        headers = {"Authorization": f"Bearer {self.api_key}", **self.headers}
        body = self.build_request_body(model, context)
        text = ""
        thinking = ""
        # The wire field the thinking text came in, so it can be sent back the same way.
        thinking_field: str | None = None
        # Structured reasoning (some of it signed or encrypted) that the model needs back after a tool call.
        details: list[dict[str, Any]] = []
        # Tool calls arrive in fragments keyed by index; their arguments JSON is concatenated.
        calls: dict[int, dict[str, str]] = {}
        finish_reason: str | None = None

        yield StartEvent()
        try:
            # TS: `async with` is like `await using`: client and response are closed on exit, even on error.
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as client:
                async with client.stream(
                    "POST", f"{self.base_url}/chat/completions", json=body, headers=headers
                ) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode(errors="replace")
                        raise ProviderError(f"HTTP {response.status_code}: {error_detail(detail)}")
                    # TS: for await (const data of sseData(response))
                    async for data in sse_data(response):
                        if data == "[DONE]":
                            break
                        chunk = json.loads(data)  # TS: JSON.parse
                        # Some providers report a failure inside an otherwise successful stream.
                        if error := chunk.get("error"):  # TS: const error = chunk.error; if (error) {...}
                            raise ProviderError(error.get("message") or json.dumps(error))
                        if usage := chunk.get("usage"):
                            output.usage = parse_usage(usage, context.price)
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            # Read before `content`: a proxy can put reasoning and answer in one chunk.
                            for field in REASONING_FIELDS:
                                piece = delta.get(field)
                                if isinstance(piece, str) and piece:
                                    thinking_field = thinking_field or field
                                    thinking += piece
                                    yield ThinkingDeltaEvent(delta=piece)
                                    break
                            for detail in delta.get("reasoning_details") or []:
                                if isinstance(detail, dict):
                                    add_reasoning_detail(details, detail)
                            if content := delta.get("content"):
                                text += content
                                yield TextDeltaEvent(delta=content)
                            for call in delta.get("tool_calls") or []:
                                slot = calls.setdefault(call.get("index", 0), {"id": "", "name": "", "arguments": ""})
                                fn = call.get("function") or {}
                                slot["id"] = call.get("id") or slot["id"]
                                slot["name"] = fn.get("name") or slot["name"]
                                slot["arguments"] += fn.get("arguments") or ""
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
        # TS: a catch that only handles these error classes; any other exception keeps propagating.
        except (httpx.HTTPError, ProviderError, json.JSONDecodeError) as exc:
            # Keep the thinking and text that arrived before the failure so they can be shown.
            output.content = thinking_blocks(thinking, thinking_field, details)
            if text:
                output.content.append(TextContent(text=text))
            output.stop_reason = "error"
            output.error_message = str(exc) or type(exc).__name__
            yield ErrorEvent(message=output)
            return

        # Thinking first, then the answer, then tool calls: the order the model produced them.
        output.content = thinking_blocks(thinking, thinking_field, details)
        if text:
            output.content.append(TextContent(text=text))
        # Arguments are only complete once the stream ends, so tool calls are parsed here.
        for index in sorted(calls):
            tool_call = parse_tool_call(calls[index])
            output.content.append(tool_call)
            yield ToolCallEndEvent(tool_call=tool_call)

        if finish_reason in ("error", "content_filter"):
            output.stop_reason = "error"
            output.error_message = f"Provider finished with '{finish_reason}'."
            yield ErrorEvent(message=output)
            return
        output.stop_reason = FINISH_REASONS.get(finish_reason or "stop", "stop")
        # Some providers say `stop` even when the reply calls tools.
        if output.tool_calls() and output.stop_reason == "stop":
            output.stop_reason = "tool_use"
        yield DoneEvent(message=output)


# TS: class ProviderError extends Error {}
class ProviderError(Exception):
    """The provider answered with an error; the message becomes the reply's `error_message`."""


# TS: `async def` + `yield` is an async generator, like `async function* sseData(response)`.
async def sse_data(response: httpx.Response) -> AsyncIterator[str]:
    """Yield the `data:` payload of each server-sent event; comments and other fields are skipped."""
    lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if lines:
                yield "\n".join(lines)
                lines = []
        elif line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
    if lines:
        yield "\n".join(lines)


def error_detail(body: str) -> str:
    """The `error.message` of a JSON error body, or the raw body cut to 500 characters."""
    try:
        error = json.loads(body).get("error")
    except (json.JSONDecodeError, AttributeError):
        return body[:500]
    if isinstance(error, dict) and error.get("message"):
        return error["message"]
    return body[:500]


def add_reasoning_detail(details: list[dict[str, Any]], detail: dict[str, Any]) -> None:
    """Add one streamed reasoning detail, joining it to the last one when both are pieces of the same text.

    OpenRouter streams `reasoning_details` in fragments: text and summary pieces are joined
    into one entry, encrypted entries stay whole (pi's `appendOpenAIReasoningDetail`).
    """
    last = details[-1] if details else None
    kind = detail.get("type")
    key = {"reasoning.text": "text", "reasoning.summary": "summary"}.get(kind or "")
    if key and last is not None and last.get("type") == kind:
        last[key] = (last.get(key) or "") + (detail.get(key) or "")
        # Fields like `signature` and `id` arrive once, often on the last fragment.
        for field, value in detail.items():
            if field != key and value is not None and last.get(field) is None:
                last[field] = value
        return
    details.append(dict(detail))  # TS: { ...detail }, a shallow copy


def thinking_blocks(
    thinking: str, field: str | None, details: list[dict[str, Any]]
) -> list[ThinkingContent | TextContent | ToolCall]:
    """The reply's thinking as a one-item content list, or empty when the model didn't think.

    The details list wins as the signature: it is the provider's own record, signed parts included.
    """
    if not thinking and not details:
        return []
    signature = json.dumps(details) if details else field
    return [ThinkingContent(thinking=thinking, signature=signature)]


def replay_thinking(message: AssistantMessage, *, provider: str, model: str) -> dict[str, Any]:
    """Fields that send a reply's thinking back to the model that wrote it; empty for any other model.

    Signed reasoning from another model is refused or meaningless, so it is never sent across.
    """
    if message.provider != provider or message.model != model:
        return {}
    for block in message.content:
        if not isinstance(block, ThinkingContent) or not block.signature:
            continue
        if block.signature in REASONING_FIELDS:
            return {block.signature: message.thinking()}
        try:
            details = json.loads(block.signature)
        except json.JSONDecodeError:
            continue
        if isinstance(details, list) and details:
            return {"reasoning_details": details}
    return {}


def parse_tool_call(raw: dict[str, str]) -> ToolCall:
    """Decode a streamed call's arguments; bad JSON is kept as `arguments_error`, not raised."""
    try:
        arguments = json.loads(raw["arguments"] or "{}")
    except json.JSONDecodeError as exc:
        return ToolCall(
            id=raw["id"], name=raw["name"], arguments={}, arguments_error=f"Invalid JSON ({exc}): {raw['arguments']}"
        )
    if not isinstance(arguments, dict):
        return ToolCall(
            id=raw["id"], name=raw["name"], arguments={}, arguments_error=f"Expected a JSON object: {raw['arguments']}"
        )
    return ToolCall(id=raw["id"], name=raw["name"], arguments=arguments)


def to_wire_messages(system_prompt: str, messages: list[Message], *, provider: str, model: str) -> list[dict[str, Any]]:
    """Convert our transcript to the Chat Completions `messages` array, for a request to `provider`'s `model`."""
    wire: list[dict[str, Any]] = []
    if system_prompt:
        wire.append({"role": "system", "content": system_prompt})
    for message in messages:
        # TS: `match` with `case Cls():` is a switch on `message instanceof Cls`.
        match message:
            case UserMessage():
                wire.append({"role": "user", "content": wire_user_content(message.content)})
            case AssistantMessage():
                entry: dict[str, Any] = {"role": "assistant", "content": message.text() or None}
                entry.update(replay_thinking(message, provider=provider, model=model))
                if calls := message.tool_calls():
                    # TS: calls.map((call) => ({ id: call.id, type: "function", function: {...} }))
                    entry["tool_calls"] = [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                        }
                        for call in calls
                    ]
                wire.append(entry)
            case ToolResultMessage():
                text = joined_text(message.content)
                images = images_of(message.content)
                if not text:
                    text = "(see attached image)" if images else ""
                wire.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": text})
                # Chat Completions takes images only from users, so a tool's images follow in a user message
                # that exists only on the wire (pi's `openai-completions.ts` does the same).
                if images:
                    parts: list[dict[str, Any]] = [{"type": "text", "text": "Attached image(s) from tool result:"}]
                    parts += [image_part(image) for image in images]
                    wire.append({"role": "user", "content": parts})
    return wire


def wire_user_content(content: str | list[UserContent]) -> str | list[dict[str, Any]]:
    """A user message's content: a plain string, or text and `image_url` parts when it holds images."""
    if not images_of(content):
        return joined_text(content)
    assert isinstance(content, list)  # images_of() found blocks, so it's the list form
    # TS: a ternary inside .map(): text blocks become text parts, images become image_url parts.
    return [{"type": "text", "text": b.text} if isinstance(b, TextContent) else image_part(b) for b in content]


def image_part(image: ImageContent) -> dict[str, Any]:
    """An image as a Chat Completions `image_url` part with a `data:` URL."""
    return {"type": "image_url", "image_url": {"url": f"data:{image.mime_type};base64,{image.data}"}}


def parse_usage(raw: dict[str, Any], price: Price | None) -> Usage:
    """The final chunk's `usage` as ours: tokens, a catalog estimate, then OpenRouter's billed cost if it sent one."""
    usage = Usage(
        input=raw.get("prompt_tokens") or 0,  # TS: `or` acts like `||`, not `??`
        output=raw.get("completion_tokens") or 0,
        total=raw.get("total_tokens") or 0,
        reasoning=(raw.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0,
    )
    if price:
        usage.cost = estimate_cost(usage, price)
    if billed := billed_cost(raw):
        usage.cost = billed
    return usage


def estimate_cost(usage: Usage, price: Price) -> Cost:
    """Tokens times the list price (pi's `calculateCost`); cached tokens aren't split out until step 36."""
    input_cost = usage.input * price.input / 1_000_000
    output_cost = usage.output * price.output / 1_000_000
    return Cost(input=input_cost, output=output_cost, total=input_cost + output_cost)


# TS: `TypeIs[float]` is a type guard, like `(value: unknown): value is number`.
def is_cost(value: object) -> TypeIs[float]:
    """A usable dollar amount: a finite number, not negative (bool is excluded, though Python counts it as an int)."""
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value < float("inf")


def billed_cost(raw: dict[str, Any]) -> Cost | None:
    """What OpenRouter charged for the reply, from `usage.cost`; None when it sent no valid figure (pi's
    `applyOpenRouterReportedCost`).

    With the user's own provider key (BYOK), `cost` is only OpenRouter's fee: the provider bills the
    tokens itself, reported as `cost_details.upstream_inference_cost`, so the two are added up.
    """
    cost = raw.get("cost")
    if not is_cost(cost):
        return None
    details = raw.get("cost_details") or {}
    prompt = details.get("upstream_inference_prompt_cost")
    completion = details.get("upstream_inference_completions_cost")
    # The split is only for display; when it's missing or doesn't add up, input/output stay 0.
    split = is_cost(prompt) and is_cost(completion)
    total = cost
    if raw.get("is_byok") is True:
        upstream = details.get("upstream_inference_cost")
        if upstream is None and split:
            upstream = prompt + completion
        if not is_cost(upstream):
            return None
        total = cost + upstream
    return Cost(
        input=prompt if split else 0,
        output=completion if split else 0,
        total=total,
        from_provider=True,
    )
