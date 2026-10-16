"""Size limits for images the studio reads, and smaller copies for uploads that must fit a cap.

Provider stills at 2K are RGBA PNGs of about 8.5 MB, and 4K stills are larger. They stay on
disk unchanged; only the bytes sent to KIE are re-encoded when they exceed the upload cap.
"""

from __future__ import annotations

import io

from PIL import Image

UPLOAD_LIMIT = 8_000_000  # bytes sent through KIE's base64 file upload
SOURCE_LIMIT = 64_000_000  # largest local image the studio reads, displays or plans with
_JPEG_QUALITIES = (95, 90, 85)
_MIN_SIDE = 512


def header_kind(header: bytes) -> str | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp"
    return None


def _encode(image: Image.Image, kind: str, **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, {".png": "PNG", ".jpg": "JPEG"}[kind], **options)
    return buffer.getvalue()


def fit_image(data: bytes, limit: int = UPLOAD_LIMIT) -> tuple[bytes, str]:
    """Return image bytes of at most `limit` and their kind (".png", ".jpg" or ".webp").

    Small images pass through untouched. A fully opaque alpha channel is dropped first, which
    keeps every pixel. If that PNG is still too large, the highest JPEG quality that fits is
    used, and only then a smaller size. Transparent areas are flattened onto white.
    """
    kind = header_kind(data[:12])
    if kind is None:
        raise ValueError("Image must be a PNG, JPEG, or WebP.")
    if len(data) <= limit:
        return data, kind
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        image = source if source.mode in ("RGB", "RGBA", "L", "LA") else source.convert("RGBA")
        if "A" in image.getbands():
            if image.getchannel("A").getextrema()[0] == 255:
                image = image.convert("RGB")
            else:
                flat = Image.new("RGB", image.size, "white")
                flat.paste(image, mask=image.getchannel("A"))
                image = flat
        if kind == ".png" and image.mode in ("RGB", "L"):
            lossless = _encode(image, ".png")
            if len(lossless) <= limit:
                return lossless, ".png"
        image = image.convert("RGB")
        while True:
            for quality in _JPEG_QUALITIES:
                encoded = _encode(image, ".jpg", quality=quality)
                if len(encoded) <= limit:
                    return encoded, ".jpg"
            if min(image.size) * 3 // 4 < _MIN_SIDE:
                raise ValueError("Image cannot be reduced to the upload size limit.")
            image = image.resize((image.width * 3 // 4, image.height * 3 // 4), Image.Resampling.LANCZOS)


def thumbnail(data: bytes, max_side: int = 320) -> bytes:
    """A small JPEG preview for lists of pictures; the original file is untouched."""
    with Image.open(io.BytesIO(data)) as source:
        source.draft("RGB", (max_side, max_side))
        image = source.convert("RGBA") if "A" in source.getbands() else source.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        if image.mode == "RGBA":
            flat = Image.new("RGB", image.size, "white")
            flat.paste(image, mask=image.getchannel("A"))
            image = flat
        return _encode(image, ".jpg", quality=82)
