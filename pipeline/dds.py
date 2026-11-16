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


# DDS_PIXELFORMAT layout (offsets inside the 128-byte header).
_PF_FLAGS = 80
_PF_FOURCC = 84
_PF_BITCOUNT = 88
_PF_AMASK = 104
_DDPF_RGB = 0x40
_PITCH = 20


def alpha_opaque(path: Path) -> bool | None:
    """Exact answer to ``alpha_kind(load_rgba(path)) == "opaque"`` for the
    uncompressed DDS cases it can decide from the pixels, without decoding.

    Returns None when the caller must decode (compressed DDS, exotic formats,
    padded rows, truncated/malformed payload). The alpha plane is inspected
    byte by byte: "opaque" means every alpha sample is 255, exactly what
    Pillow's extrema check reports, so the BC1/BC3 choice cannot change.
    """
    try:
        with path.open("rb") as f:
            head = f.read(HEADER_SIZE)
            if len(head) < HEADER_SIZE or head[:4] != MAGIC:
                return None
            if head[_PF_FOURCC:_PF_FOURCC + 4] != b"\0\0\0\0":
                return None  # compressed (DXT*/BC*): Pillow decode
            (flags,) = struct.unpack_from("<I", head, _PF_FLAGS)
            if not flags & _DDPF_RGB:
                return None  # luminance/alpha-only/other: let Pillow decide
            (bits,) = struct.unpack_from("<I", head, _PF_BITCOUNT)
            (amask,) = struct.unpack_from("<I", head, _PF_AMASK)
            if amask == 0:
                return True  # no alpha channel -> always opaque
            if bits != 32:
                return None  # 16-bit alpha masks: Pillow decode
            low = amask & -amask          # lowest set bit of the alpha mask
            span = amask // low + 1
            if span & (span - 1):
                return None  # non-contiguous mask: Pillow decode
            offset = (low.bit_length() - 1) // 8
            if amask != 0xFF << (8 * offset):
                return None  # not a full byte: Pillow decode
            (pitch,) = struct.unpack_from("<I", head, _PITCH)
            height, width = struct.unpack_from("<II", head, 12)
            if pitch != width * 4 or width == 0 or height == 0:
                return None  # padded rows / malformed: Pillow decode
            need = pitch * height
            if len(f.read(need)) < need:
                return None
            f.seek(HEADER_SIZE)
            payload = f.read(need)
    except OSError:
        return None
    alpha = payload[offset::4]
    return alpha.count(255) == width * height
