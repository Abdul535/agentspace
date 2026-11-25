"""Images (ADR-015): resizing, `read`, the wire format, blind models, compaction and the prompt route."""

import base64
import io
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from agentspace.agent.compaction import IMAGE_CHARS, estimate_tokens, serialize
from agentspace.agent.tool import ToolContext
from agentspace.ai.catalog import ModelInfo, OpenRouterCatalog
from agentspace.ai.images import MAX_SIDE, ImageError, process_image, sniff_image
from agentspace.ai.messages import (
    IMAGE_NOT_SHOWN,
    ImageContent,
    TextContent,
    ToolResultMessage,
    UserMessage,
    joined_text,
    strip_images,
)
from agentspace.ai.providers.fake import FakeProvider, text_reply
from agentspace.ai.providers.openai_compat import to_wire_messages
from agentspace.api.deps import get_catalog, provider_lookup
from agentspace.config import Settings
from agentspace.main import create_app
from agentspace.tools.read import ReadParams, ReadTool
from agentspace.workspace.local import LocalWorkspace


def png(width: int = 4, height: int = 3, color: str = "red") -> bytes:
    """A small solid PNG."""
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, "PNG")
    return out.getvalue()


def size_of(image: ImageContent) -> tuple[int, int]:
    """The pixel size of an encoded image block."""
    return Image.open(io.BytesIO(base64.b64decode(image.data))).size


IMAGE = ImageContent(data=base64.b64encode(png()).decode(), mime_type="image/png")


def test_sniff_reads_the_bytes_not_the_name() -> None:
    assert sniff_image(png()) == "image/png"
    assert sniff_image(b"GIF89a....") == "image/gif"
    assert sniff_image(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert sniff_image(b"hello") is None


def test_a_small_image_is_kept_as_it_is() -> None:
    data = png()
    processed = process_image(data)
    assert base64.b64decode(processed.image.data) == data
    assert processed.note() is None


def test_a_big_image_is_shrunk_and_the_note_gives_the_scale() -> None:
    processed = process_image(png(4000, 1000))
    assert size_of(processed.image) == (MAX_SIDE, 500)
    assert (processed.original_width, processed.width) == (4000, MAX_SIDE)
    assert "Multiply coordinates by 2.00" in (processed.note() or "")


def test_an_animated_gif_keeps_its_first_frame() -> None:
    frames = [Image.new("RGB", (5, 5), color) for color in ("red", "blue")]
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:])
    processed = process_image(out.getvalue())
    image = Image.open(io.BytesIO(base64.b64decode(processed.image.data)))
    assert getattr(image, "n_frames", 1) == 1
    assert image.convert("RGB").getpixel((0, 0)) == (255, 0, 0)


def test_not_an_image_or_a_broken_one_is_refused() -> None:
    with pytest.raises(ImageError):
        process_image(b"just text")
    with pytest.raises(ImageError):
        process_image(png()[:20])  # a PNG header with the pixels cut off


@pytest.mark.anyio
async def test_read_returns_an_image_with_a_note(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(png())
    result = await ReadTool().execute(ReadParams(path="shot.png"), ToolContext(workspace=LocalWorkspace(tmp_path)))
    note, image = result.content
    assert isinstance(note, TextContent) and note.text == "Read image file [image/png]"
    assert isinstance(image, ImageContent) and image.mime_type == "image/png"
    assert result.details is not None and result.details["image"]["width"] == 4


@pytest.mark.anyio
async def test_read_judges_by_content_so_a_fake_png_is_text(tmp_path: Path) -> None:
    (tmp_path / "fake.png").write_text("not really an image")
    result = await ReadTool().execute(ReadParams(path="fake.png"), ToolContext(workspace=LocalWorkspace(tmp_path)))
    assert joined_text(result.content) == "not really an image"


def test_wire_sends_user_images_as_data_url_parts() -> None:
    wire = to_wire_messages(
        "", [UserMessage(content=[TextContent(text="what is this?"), IMAGE])], provider="p", model="m"
    )
    assert wire == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{IMAGE.data}"}},
            ],
        }
    ]
    # Text only stays a plain string, as before images existed.
    assert to_wire_messages("", [UserMessage(content="hi")], provider="p", model="m")[0]["content"] == "hi"


def test_wire_moves_tool_result_images_into_a_user_message() -> None:
    result = ToolResultMessage(tool_call_id="c1", tool_name="read", content=[IMAGE])
    tool, extra = to_wire_messages("", [result], provider="p", model="m")
    # Chat Completions takes no images in a tool message, so they follow from the "user".
    assert tool == {"role": "tool", "tool_call_id": "c1", "content": "(see attached image)"}
    assert extra["role"] == "user" and extra["content"][1]["type"] == "image_url"


def test_strip_images_swaps_each_image_for_a_note_without_touching_the_original() -> None:
    message = UserMessage(content=[TextContent(text="look"), IMAGE])
    [stripped] = strip_images([message])
    assert isinstance(stripped, UserMessage)
    assert joined_text(stripped.content) == "look" + IMAGE_NOT_SHOWN
    assert message.content[1] is IMAGE  # the stored message keeps its image


def test_compaction_counts_an_image_and_marks_it_in_the_summary_text() -> None:
    message = UserMessage(content=[TextContent(text="abcd"), IMAGE])
    assert estimate_tokens(message) == (4 + IMAGE_CHARS) // 4
    assert serialize([message]) == "[User]: abcd [image]"


class BlindCatalog(OpenRouterCatalog):
    """Lists the test model as one that can't see images."""

    async def _fetch(self) -> list[ModelInfo]:
        return [
            ModelInfo(
                provider="openrouter",
                id="blind/model",
                name="Blind",
                context_length=None,
                input_price=None,
                output_price=None,
                image_input=False,
            )
        ]


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider([])


@pytest.fixture
def client(tmp_path: Path, fake: FakeProvider) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    app.dependency_overrides[provider_lookup] = lambda: lambda name: fake
    with TestClient(app) as client:
        yield client


def b64_png(width: int = 4, height: int = 3) -> str:
    """A PNG as the base64 the composer sends."""
    return base64.b64encode(png(width, height)).decode()


def test_a_prompt_with_an_image_is_saved_shrunk_and_sent(client: TestClient, fake: FakeProvider) -> None:
    fake.replies = [text_reply("A red square.")]
    session_id = client.post("/sessions", json={}).json()["id"]

    response = client.post(
        f"/sessions/{session_id}/messages",
        json={"content": "what is it?", "images": [{"data": "data:image/png;base64," + b64_png(3000, 30)}]},
    )
    assert response.status_code == 200

    [prompt, _] = client.get(f"/sessions/{session_id}").json()["messages"]
    assert prompt["content"][0] == {"type": "text", "text": "what is it?"}
    assert prompt["content"][1]["mime_type"] == "image/png"
    sent = fake.contexts[0].messages[0]
    assert isinstance(sent, UserMessage) and not isinstance(sent.content, str)
    image = sent.content[1]
    assert isinstance(image, ImageContent) and size_of(image) == (MAX_SIDE, 20)


def test_an_image_alone_is_a_prompt_and_titles_the_session(client: TestClient, fake: FakeProvider) -> None:
    fake.replies = [text_reply("ok")]
    session_id = client.post("/sessions", json={}).json()["id"]
    assert client.post(f"/sessions/{session_id}/messages", json={"images": [{"data": b64_png()}]}).status_code == 200
    assert client.get(f"/sessions/{session_id}").json()["title"] == "Image"


def test_bad_images_and_empty_prompts_are_refused_before_a_run(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    url = f"/sessions/{session_id}/messages"
    assert client.post(url, json={"content": "  "}).status_code == 422
    assert client.post(url, json={"images": [{"data": "%%% not base64"}]}).status_code == 422
    assert client.post(url, json={"images": [{"data": base64.b64encode(b"text").decode()}]}).status_code == 422
    assert client.post(url, json={"images": [{"data": b64_png()}] * 6}).status_code == 422
    # The refused prompts never claimed the run's slot.
    assert client.get(f"/sessions/{session_id}").json()["running"] is False


def test_a_model_that_cant_see_images_gets_a_note_instead(client: TestClient, fake: FakeProvider) -> None:
    # TS: cast() is `as FastAPI`: TestClient types `app` as any ASGI app, ours is a FastAPI one.
    cast(FastAPI, client.app).dependency_overrides[get_catalog] = lambda: BlindCatalog("https://openrouter.test")
    fake.replies = [text_reply("I can't see it.")]
    session_id = client.post("/sessions", json={"model": "blind/model"}).json()["id"]

    client.post(f"/sessions/{session_id}/messages", json={"content": "look", "images": [{"data": b64_png()}]})

    sent = fake.contexts[0].messages[0]
    assert isinstance(sent, UserMessage) and joined_text(sent.content) == "look" + IMAGE_NOT_SHOWN
    # The transcript keeps the image for a model that can see it.
    saved = client.get(f"/sessions/{session_id}").json()["messages"][0]
    assert saved["content"][1]["type"] == "image"
