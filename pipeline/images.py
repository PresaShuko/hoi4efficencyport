"""Pillow helpers shared by the texture stages (analyze, convert, texfix)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image

ALPHA_MODES = ("RGBA", "LA", "PA")


def alpha_kind(img: Image.Image) -> str:
    """'opaque', 'binary' or 'gradient' — decides BC1 vs BC3."""
    if img.mode not in ALPHA_MODES:
        return "opaque"
    a = img.getchannel("A")
    lo, hi = a.getextrema()
    if lo == 255:
        return "opaque"
    if lo == 0 and hi == 255 and not any(
        0 < v < 255 for v in a.histogram()[1:255]
    ):
        return "binary"
    return "gradient"


def load_rgba(path: Path) -> Image.Image:
    """Fully decoded image, detached from the file handle (RGB or RGBA)."""
    with Image.open(path) as img:
        img.load()
        return img.copy() if img.mode in ("RGB", "RGBA") else img.convert("RGBA")


def resize_cap(img: Image.Image, cap: int | None) -> tuple[Image.Image, bool]:
    """Downscale so the largest side is at most ``cap`` (None = no cap)."""
    if cap is None:
        return img, False
    w, h = img.size
    if max(w, h) <= cap:
        return img, False
    scale = cap / max(w, h)
    return img.resize(
        (max(1, int(w * scale)), max(1, int(h * scale))), Image.Resampling.LANCZOS
    ), True


def pad_to_4(img: Image.Image) -> tuple[Image.Image, int, int]:
    """Pad to multiples of 4 by replicating the last row/column."""
    w, h = img.size
    pw, ph = (-w) % 4, (-h) % 4
    if pw == ph == 0:
        return img, 0, 0
    out = Image.new(img.mode, (w + pw, h + ph))
    out.paste(img, (0, 0))
    if pw:
        col = img.crop((w - 1, 0, w, h)).resize((pw, h), Image.Resampling.NEAREST)
        out.paste(col, (w, 0))
    if ph:
        row = out.crop((0, h - 1, w + pw, h)).resize((w + pw, ph), Image.Resampling.NEAREST)
        out.paste(row, (0, h))
    return out, pw, ph
