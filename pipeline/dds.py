"""DDS header parsing and format policy shared by analyze/validate/texfix."""
from __future__ import annotations

import struct
from pathlib import Path

HEADER_SIZE = 128
MAGIC = b"DDS "
# Formats that need no re-encode for 2D UI assets.
UI_FOURCC = frozenset({"DXT1", "DXT3", "DXT5"})
# Model textures are BC1/BC3 only: texfix re-encodes anything else.
MODEL_FOURCC = frozenset({"DXT1", "DXT5"})


def read_header(path: Path) -> dict | None:
    """Header fields of a DDS file, or None if the file is not a valid DDS."""
    try:
        with path.open("rb") as f:
            head = f.read(HEADER_SIZE)
    except OSError:
        return None
    if len(head) < HEADER_SIZE or head[:4] != MAGIC:
        return None
    height, width = struct.unpack_from("<II", head, 12)
    (mips,) = struct.unpack_from("<I", head, 28)
    return {"w": width, "h": height, "mips": mips,
            "fourcc": head[84:88].decode("ascii", "replace")}


def fourcc(data: bytes) -> str:
    """FourCC of an in-memory DDS ('' when the header is not a DDS)."""
    if len(data) < HEADER_SIZE or data[:4] != MAGIC:
        return "?"
    return data[84:88].decode("ascii", "replace")


def fourcc_file(path: Path) -> str:
    """FourCC read from disk ('?' when unreadable or not a DDS)."""
    try:
        with path.open("rb") as f:
            return fourcc(f.read(HEADER_SIZE))
    except OSError:
        return "?"
