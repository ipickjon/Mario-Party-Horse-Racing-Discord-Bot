"""Rules for community ads.

Anything that passes here can end up on stream, so the checks are strict:
text is length-limited and stripped of anything that isn't ordinary text,
and an upload has to actually be an image, judged by its bytes rather than
by the filename or what Discord says it is.
"""

from __future__ import annotations

import re

MAX_HEADLINE = 60
MAX_BODY = 120
MAX_TAG = 40
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_PENDING_PER_PERSON = 3
DEFAULT_ACCENTS = ["#ffa81e", "#5865f2", "#e5453a", "#3fae5a", "#f4a43c", "#b36ad6"]

# The slot is about 1170 x 170 pixels; other shapes are cropped to fit.
IDEAL_SIZE = "1170 × 170"

_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def clean_text(value: str | None, limit: int) -> str:
    """One line of plain text, trimmed to the limit. Line breaks and control
    characters are removed so nobody can wreck the layout."""
    if not value:
        return ""
    # Line breaks and tabs become spaces first, then anything else that isn't
    # printable is dropped, so "one<newline>two" reads "one two", not "onetwo".
    text = "".join(" " if ch.isspace() else ch for ch in value)
    text = "".join(ch for ch in text if ch.isprintable())
    text = re.sub(r" +", " ", text).strip()
    return text[:limit].strip()


def clean_accent(value: str | None, fallback_index: int = 0) -> str | None:
    """A #rrggbb colour, or None if what was typed isn't one."""
    if not value:
        return DEFAULT_ACCENTS[fallback_index % len(DEFAULT_ACCENTS)]
    value = value.strip()
    if not value.startswith("#"):
        value = "#" + value
    return value.lower() if _HEX.match(value) else None


def sniff_image(data: bytes) -> str | None:
    """The image type, read from the file's own bytes. None if it isn't a
    PNG, JPEG, GIF or WebP, whatever the filename claims."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
