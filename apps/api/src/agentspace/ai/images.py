"""Turn raw image bytes into an `ImageContent` small enough to send, after pi's `image-resize-core.ts` (ADR-015).

Images are kept inline as base64, so every one is shrunk once, before it is saved:
within 2000×2000 pixels and 4.5 MB of base64, under the 5 MB some providers allow.
"""

import base64
import io
import math
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from agentspace.ai.messages import ImageContent

MAX_INPUT_BYTES = 10 * 1024 * 1024
"""Bigger files are refused before decoding, so one upload or `read` can't tie up the server."""
MAX_SIDE = 2000
MAX_BASE64_BYTES = 4_500_000
JPEG_QUALITY = 80
# Sizes tried in turn when the image is still too big after fitting it within MAX_SIDE.
SCALE_STEPS = (1.0, 0.75, 0.5, 0.35, 0.25)


class ImageError(ValueError):
    """The bytes aren't a supported image, are too big, or couldn't be made small enough."""


@dataclass(frozen=True)
class ProcessedImage:
    """An image ready to save, with its size before and after resizing."""

    image: ImageContent
    width: int
    height: int
    original_width: int
    original_height: int

    def note(self) -> str | None:
        """Tells the model the image was shrunk and how to map its coordinates back; None when it wasn't."""
        if (self.width, self.height) == (self.original_width, self.original_height):
            return None
        scale = self.original_width / self.width
        return (
            f"[Image: original {self.original_width}x{self.original_height}, shown at {self.width}x{self.height}. "
            f"Multiply coordinates by {scale:.2f} to map to the original image.]"
        )


def sniff_image(data: bytes) -> str | None:
    """The image's MIME type from its first bytes, or None if it isn't PNG, JPEG, GIF or WebP."""
    # TS: bytes.startswith is like buf.subarray(0, n).equals(Buffer.from(...)).
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def process_image(data: bytes) -> ProcessedImage:
    """Check, decode and shrink an image. CPU-bound: call it through `asyncify` from async code."""
    mime_type = sniff_image(data)
    if mime_type is None:
        raise ImageError("Not a PNG, JPEG, GIF or WebP image.")
    if len(data) > MAX_INPUT_BYTES:
        raise ImageError(
            f"The image is {len(data) / 1024 / 1024:.1f} MB; the limit is {MAX_INPUT_BYTES // 1024 // 1024} MB."
        )
    try:
        # TS: `with` closes the file handle on exit, like try/finally around img.close().
        with Image.open(io.BytesIO(data)) as img:
            img.load()  # Pillow decodes lazily; do it here so a broken file fails inside this `try`
            return shrink(img, data, mime_type)
    # A DecompressionBombError is a huge pixel count hidden in a small file.
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise ImageError(f"Couldn't read the image: {exc}") from exc


def shrink(img: Image.Image, data: bytes, mime_type: str) -> ProcessedImage:
    """The image as it is when it already fits, otherwise resized and re-encoded until it does."""
    width, height = img.size
    animated = getattr(img, "n_frames", 1) > 1  # TS: like `img.n_frames ?? 1`
    if width <= MAX_SIDE and height <= MAX_SIDE and base64_size(len(data)) <= MAX_BASE64_BYTES and not animated:
        return ProcessedImage(ImageContent(data=b64(data), mime_type=mime_type), width, height, width, height)

    # An animated GIF keeps its first frame, which `Image.open` already points at.
    fit = min(1.0, MAX_SIDE / width, MAX_SIDE / height)
    # Palette images (GIFs, some PNGs) can only be resized pixel by pixel; full color resizes smoothly.
    if img.mode not in ("RGB", "RGBA", "L", "LA"):
        img = img.convert("RGBA")
    # PNG keeps sharp edges and transparency (screenshots); JPEG is the fallback that always gets small.
    formats = ("PNG", "JPEG") if mime_type in ("image/png", "image/gif") else ("JPEG",)
    for step in SCALE_STEPS:
        size = (max(1, round(width * fit * step)), max(1, round(height * fit * step)))
        resized = img.resize(size, Image.Resampling.LANCZOS)
        for fmt in formats:
            encoded = encode(resized, fmt)
            if base64_size(len(encoded)) <= MAX_BASE64_BYTES:
                image = ImageContent(data=b64(encoded), mime_type=f"image/{fmt.lower()}")
                return ProcessedImage(image, size[0], size[1], width, height)
    raise ImageError("The image couldn't be made small enough to send.")


def encode(img: Image.Image, fmt: str) -> bytes:
    """`img` saved as PNG or JPEG; JPEG has no transparency, so it is laid on white first."""
    out = io.BytesIO()
    if fmt == "JPEG":
        rgba = img.convert("RGBA")
        flat = Image.new("RGB", rgba.size, "white")
        flat.paste(rgba, mask=rgba.getchannel("A"))
        flat.save(out, "JPEG", quality=JPEG_QUALITY)
    else:
        img.save(out, "PNG", optimize=True)
    return out.getvalue()


def base64_size(byte_count: int) -> int:
    """How long `byte_count` bytes are once base64-encoded (4 characters per 3 bytes)."""
    return 4 * math.ceil(byte_count / 3)


def b64(data: bytes) -> str:
    """Bytes as a base64 string."""
    return base64.b64encode(data).decode("ascii")
