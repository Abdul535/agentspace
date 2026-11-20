# ADR-015 — Images: in user messages and from `read`, stored inline as base64

- **Status:** Accepted
- **Date:** 2026-11-20
- **Deciders:** project owner

## Context
ADR-003 step 21. The agent only sees text:

- `UserMessage.content` is a `str` (`ai/messages.py:76`), and
  `POST /sessions/{id}/messages` takes text only.
- `ToolResult.content` and `ToolResultMessage.content` are `list[TextContent]`.
- `read` refuses binary files (`tools/read.py`: a NUL byte in the first 8 KiB),
  so a screenshot or diagram in the workspace can't be looked at.
- The catalog (ADR-005) doesn't say which models take images. OpenRouter's
  `/models` lists them in `architecture.input_modalities` (`"image"`).
- `to_wire_messages` (`openai_compat.py:303`) sends user content as a string
  and tool results as a `role: "tool"` string. Chat Completions takes images
  only in user messages, as `image_url` parts with a `data:` URL.

How pi does it:

- **A content block.** `ImageContent { type: "image", data, mimeType }`, with
  `data` as base64 (`ai/src/types.ts:446`). User messages and tool results hold
  `(TextContent | ImageContent)[]`. Images live inline in the session file;
  there is no separate file store.
- **`read` returns images** (`coding-agent/src/core/tools/read.ts:272`): it
  detects jpg/png/gif/webp/bmp by content, resizes, and returns a text note plus
  the image. When the model can't take images it still returns them, and sets
  `details.imageUnsupportedByModel` for the UI.
- **Resizing** (`utils/image-resize-core.ts`): fit within 2000×2000 and 4.5 MB of
  base64 (under Anthropic's 5 MB), JPEG quality 80. When it shrank the image,
  the note says the original size and the scale, so the model can map
  coordinates back (`formatDimensionNote`, `image-resize.ts:117`).
- **Blocking images** (`core/sdk.ts:297`): with the `blockImages` setting, its
  `convertToLlm` wrapper swaps every image for the text "Image reading is
  disabled." right before each call. The session keeps the image.
- **Tool-result images over Chat Completions**
  (`ai/src/api/openai-completions.ts:1402`): the tool message gets the text (or
  "(see attached image)"), and the images follow in an extra user message
  "Attached image(s) from tool result:", only when the model takes images.
- **Compaction** counts an image as 4800 characters (~1200 tokens,
  `compaction.ts:276`) and leaves it out of the summarized text.

## Decision
**1. An image block.** `ai/messages.py` gains:

```python
class ImageContent(BaseModel):
    type: Literal["image"] = "image"
    data: str          # base64, no "data:" prefix
    mime_type: str     # "image/png" | "image/jpeg" | "image/gif" | "image/webp"
```

- `UserMessage.content` becomes `str | list[TextContent | ImageContent]`, as in
  pi. A plain string stays valid, so old rows and text-only prompts don't
  change. A `text()` method joins the text parts, for titles and compaction.
- `ToolResult.content`, `ToolResultMessage.content` and
  `AfterToolCallResult.content` become `list[TextContent | ImageContent]`.
- Images are stored inline in the message row, as in pi. No file store, no
  upload route: one image is one field of one message.

**2. One resize function.** `ai/images.py` (Pillow, added with `uv add
pillow`) turns raw bytes into an `ImageContent`, after pi's defaults:

- Detect the format from the bytes, not the file name or the client's claim;
  anything other than png/jpeg/gif/webp is refused.
- Fit within 2000×2000, keeping the aspect ratio. If the base64 is still over
  4.5 MB, re-encode as JPEG at quality 80, then step the size down until it fits.
  An animated GIF keeps its first frame.
- Return the image plus a dimension note when it was shrunk ("original
  3024×1964, shown at 2000×1299; multiply coordinates by 1.51").
- It is CPU work, so callers run it with `asyncify`, as `read` does with file I/O.

**3. Images in the prompt.** `POST /sessions/{id}/messages` takes
`images: list[ImageInput]` next to `content` (`data` as base64, optional
`mime_type`). Each goes through the resize function before the message is
saved. Limits: 5 images per message, 10 MB each before resizing; over that is
a 422. The saved `UserMessage` holds the text first, then the images. Steering
and follow-up messages stay text-only for now.

**4. `read` returns images.** If the file's bytes are an image, `read` resizes
it and returns `[TextContent("Read image file [image/png]" + note),
ImageContent]` instead of refusing it as binary. `details` gains `image:
{mime_type, width, height, original_width, original_height}`. `offset` and
`limit` are ignored for images. The tool description says it reads images.
The same 10 MB limit applies, so a huge file in the workspace is refused, not
decoded.

**5. Which models see images.** `ModelInfo` gains `image_input: bool` from
`architecture.input_modalities`. Before each run, `stream_run` looks up the
session's model in the catalog (`context_window` becomes `model_lookup`, which
returns the whole `ModelInfo`, so compaction reads the window from it too). When the catalog says the model
can't take images, the run's `convert_to_llm` is wrapped to swap every image
for the text "[Image not shown: this model can't see images.]", as pi's
`blockImages` does. A model the catalog doesn't know gets the images; a
refusal then comes back as a normal provider error. The transcript keeps the
image, so switching back to a vision model shows it again.

**6. The wire.** `to_wire_messages`:

- A user message with images is sent as content parts: `{"type": "text", ...}`
  and `{"type": "image_url", "image_url": {"url": "data:<mime>;base64,<data>"}}`.
- A tool result with images sends its text as the `role: "tool"` message (or
  "(see attached image)" if it has no text), then one extra user message
  "Attached image(s) from tool result:" with the images, as pi does. That
  extra message exists only on the wire; it is never saved.

**7. Compaction.** `estimate_tokens` counts each image as 4800 characters, as pi
does. `serialize` writes `[image]` in its place, so the summary model is told
an image was there without being sent it.

**8. The web app.**

- The composer takes images by paste, drag-and-drop and an attach button, shows
  them as thumbnails with a remove button, and sends them as base64. The attach
  button is disabled, with a tooltip, when the catalog says the model can't see
  images.
- The chat shows images in user messages and in `read` results, as thumbnails
  that open full size.
- `pnpm gen:api` after the model and route changes.

**Not built:** images in steering/follow-up messages, a file store with
references instead of inline base64, pi's per-model resize limits, bmp, image
output from the model (ADR-014 step 43), and a session setting to block images
on a vision model.

## Alternatives considered
- **Save images as files and keep a reference in the message** — the database
  and every `GET /sessions/{id}` stay small, and an image is sent to the browser
  only when shown. But it needs an upload route, file cleanup when a session is
  deleted, and a lookup before each model call. pi keeps them inline; so do we,
  until size hurts.
- **Don't resize** — simpler and exact, but a phone screenshot can pass 5 MB,
  which some providers refuse, and every later turn resends it.
- **Drop images entirely for models that can't see them** — the model would not
  know the user attached anything. A short text placeholder tells it.
- **Refuse to switch to a non-vision model when the session has images** — safe,
  but blocks a common move (switch to a cheaper model for a text question). The
  placeholder covers it.

## Consequences
- Every place that reads `UserMessage.content` as a string must handle the list
  form: titles, compaction, the web chat. Code that walks tool-result content
  (`after_tool_call` hooks, compaction, the adapter) gets a second block type.
- The database grows by up to ~4.5 MB per image, and `GET /sessions/{id}` and
  late-join snapshots carry the images too. Fine for a single user; a file store
  is the fix if it starts to hurt.
- Every turn resends every image still in context, so images cost tokens until
  compaction summarizes them away.
- Pillow becomes a backend dependency. Image detection reads bytes, so a file
  named `.png` that isn't one is read as text or refused as binary, as today.
