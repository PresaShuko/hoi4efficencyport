"""Shared helpers for the hoi4port test suite (stdlib only).

- Workspace: isolated temp dir per test (staging/ + build/ + modsrc/).
- make_png / make_raw_dds: real Pillow-generated assets.
- crafted_dds: hand-built DDS header with arbitrary fourcc/mips/dimensions.
- MeshBuilder: synthetic .mesh binary in the format _parse_mesh expects.
- make_scan: production scanner over a Workspace source tree.
- TEXCONV / CMD: bundled texconv availability and argv (empty off Windows).
- run_cli: subprocess invocation of `python -m hoi4port` with a clean env.
"""
from __future__ import annotations

import io
import os
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from pipeline import scanner, tools

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "mini_mod"
WINDOWS = sys.platform == "win32"

TEXCONV = tools._bundled().is_file() and WINDOWS
CMD = tools.texconv_command() if TEXCONV else []


class Workspace:
    """Temp tree: modsrc/ (a fake workshop mod), staging/, build/."""

    def __init__(self, root: Path):
        self.root = root
        self.modsrc = root / "modsrc"
        self.staging = root / "staging"
        self.build = root / "build"
        self.modsrc.mkdir(parents=True, exist_ok=True)

    # -- tree helpers ------------------------------------------------------
    def mkdirs(self, *rel: str) -> Path:
        p = self.modsrc
        for r in rel:
            p = p / r
        p.mkdir(parents=True, exist_ok=True)
        return p

    def write(self, rel: str, text: str, encoding: str = "utf-8") -> Path:
        p = self.modsrc / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding=encoding, newline="")
        return p

    def make_png(self, rel: str, w: int = 8, h: int = 8,
                 color=(0, 128, 255, 255), mode: str = "RGBA") -> Path:
        p = self.modsrc / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new(mode, (w, h), color).save(p)
        return p

    def make_raw_dds(self, rel: str, w: int = 8, h: int = 8,
                     color=(0, 128, 255, 255), mode: str = "RGBA") -> Path:
        """Uncompressed DDS, fourcc 0 — what 'analyze' classifies as reencode."""
        p = self.modsrc / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        Image.new(mode, (w, h), color).save(p, format="DDS")
        return p

    def sync(self) -> Path:
        """modsrc -> staging via the production sync function."""
        from pipeline import fsutil
        fsutil.sync_staging(self.modsrc, self.staging)
        return self.staging

    # -- path objects -------------------------------------------------------
    def sp(self, rel: str) -> Path:       # path inside staging
        return self.staging / rel

    def bp(self, rel: str) -> Path:       # path inside build
        return self.build / rel


class BaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hoi4port_test_")
        self.ws = Workspace(Path(self._tmp.name))

    def tearDown(self) -> None:
        self._tmp.cleanup()


def make_scan(ws: Workspace) -> dict:
    """Production scanner over the workspace source tree."""
    return scanner.scan(ws.modsrc)


# --- DDS builders -----------------------------------------------------------

DDS_MAGIC = b"DDS "


def crafted_dds(w: int, h: int, fourcc: bytes = b"DXT1", mips: int = 1) -> bytes:
    """Minimal but structurally valid DDS: header + zero payload."""
    flags = 0x1 | 0x2 | 0x4 | 0x1000          # CAPS|HEIGHT|WIDTH|PIXELFORMAT
    caps = 0x1000                              # TEXTURE
    if mips > 1:
        flags |= 0x20000 | 0x10000             # MIPMAPCOUNT | COMPLEX
        caps |= 0x400008                       # COMPLEX | MIPMAP
    if fourcc in (b"DXT1", b"DXT2", b"DXT3", b"DXT4", b"DXT5"):
        flags |= 0x80000                       # LINEARSIZE
        bpb = 8 if fourcc == b"DXT1" else 16
        pitch = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * bpb
        pf_flags = 0x4                          # DDPF_FOURCC
        rgb_bits, masks = 0, b""
    else:                                       # uncompressed RGBA
        flags |= 0x8                            # PITCH
        pitch = w * 4
        pf_flags = 0x41                         # RGB | ALPHA
        rgb_bits = 32
        masks = struct.pack("<IIII", 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    buf = io.BytesIO()
    buf.write(DDS_MAGIC)
    buf.write(struct.pack("<7I", 124, flags, h, w, pitch, 0, mips))
    buf.write(struct.pack("<11I", *([0] * 11)))
    buf.write(struct.pack("<2I", 32, pf_flags))
    buf.write(fourcc.ljust(4, b"\0"))
    masks4 = struct.unpack("<4I", masks) if masks else (0, 0, 0, 0)
    buf.write(struct.pack("<IIIII", rgb_bits, *masks4))
    buf.write(struct.pack("<5I", caps, 0, 0, 0, 0))
    assert buf.tell() == 128, buf.tell()
    payload_size = max(1, (w + 3) // 4) * max(1, (h + 3) // 4) * (8 if fourcc == b"DXT1" else 16 if fourcc else 4)
    return buf.getvalue() + b"\0" * payload_size * (mips if mips > 1 else 1)


# --- synthetic .mesh builder (format consumed by meshscan._parse_mesh) ------

class MeshBuilder:
    """Emit the PDX-style binary layout expected by pipeline.meshscan:
    b'@@b@' then objects b'[<name>\\0' followed by properties
    b'!' + len8(name) + name + type + int32 count + payload.
    """

    def __init__(self) -> None:
        self.buf = io.BytesIO()
        self.buf.write(b"@@b@")

    def obj(self, name: str) -> "MeshBuilder":
        self.buf.write(b"[" + name.encode("latin-1") + b"\0")
        return self

    def _prop(self, name: str, typ: bytes, count: int, payload: bytes) -> None:
        self.buf.write(b"!")
        self.buf.write(bytes([len(name.encode("latin-1"))]))
        self.buf.write(name.encode("latin-1"))
        self.buf.write(typ)
        self.buf.write(struct.pack("<i", count))
        self.buf.write(payload)

    def string(self, name: str, value: str) -> "MeshBuilder":
        raw = value.encode("latin-1")
        self._prop(name, b"s", 1, struct.pack("<i", len(raw)) + raw)
        return self

    def floats(self, name: str, values: list[float]) -> "MeshBuilder":
        self._prop(name, b"f", len(values), struct.pack(f"<{len(values)}f", *values))
        return self

    def ints(self, name: str, values: list[int]) -> "MeshBuilder":
        self._prop(name, b"i", len(values), struct.pack(f"<{len(values)}i", *values))
        return self

    def bytes_(self) -> bytes:
        return self.buf.getvalue()


def simple_mesh(verts: int = 3, tris: int = 1, shader: str = "Standard",
                diff: str = "models/t.dds", bones: int = 0,
                collision: bool = False) -> bytes:
    b = MeshBuilder()
    sh = "collisionShader" if collision else shader
    b.obj("mesh").floats("p", [0.0] * (verts * 3)).ints("tri", list(range(tris * 3)))
    b.obj("material").string("shader", sh).string("diff", diff)
    if bones:
        b.obj("skin").ints("bones", [bones] + [0] * 7)
    return b.bytes_()


# --- CLI runner ---------------------------------------------------------------

def clean_env(extra: dict[str, str] | None = None,
              appdata: Path | None = None) -> dict[str, str]:
    """Environment without HOI4PORT_*/XDG, with REPO importable.

    ``appdata`` pins the Windows user-config lookup (hermetic toml tests).
    """
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("HOI4PORT_")}
    env.pop("XDG_CONFIG_HOME", None)
    if appdata is not None:
        env["APPDATA"] = str(appdata)
    # make `python -m hoi4port` importable regardless of the subprocess cwd
    py_path = env.get("PYTHONPATH")
    env["PYTHONPATH"] = f"{REPO}{os.pathsep}{py_path}" if py_path else str(REPO)
    if extra:
        env.update(extra)
    return env


def run_cli(*args: str, cwd: Path | None = None,
            env: dict[str, str] | None = None) -> tuple[int, str, str]:
    r = subprocess.run(
        [sys.executable, "-m", "hoi4port", *args],
        capture_output=True, text=True, errors="replace",
        cwd=str(cwd) if cwd else None,
        env=env or clean_env(), timeout=300,
    )
    return r.returncode, r.stdout, r.stderr
