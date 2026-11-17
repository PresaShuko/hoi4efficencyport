"""Launcher descriptors for the merged mod.

`sync` mirrors mods into the staging tree, which copies each source's own
``descriptor.mod``; the last one wins and the merged tree therefore advertises
the wrong mod. This module regenerates it *after* the merge:

- ``<staging>/descriptor.mod``  — shipped inside the mod folder
- ``<staging>.mod``             — the file the launcher reads (adds ``path``)

Field rules:

- ``name``  = the staging folder name (not the path),
- ``supported_version`` = the **oldest** supported_version among the sources,
- ``version`` = the oldest ``version`` among the sources (``1.0`` if none),
- ``tags`` = union of the sources' ``tags`` and ``categories``, deduplicated
  case-insensitively, in the order they were first seen.

Nothing here reads the game: it only parses what the sources already declare.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import fsutil

DEFAULT_VERSION = "1.0"

_SCALAR_RE = re.compile(r'(?m)^\s*([A-Za-z_]\w*)\s*=\s*"([^"]*)"')
_BLOCK_RE = re.compile(r'([A-Za-z_]\w*)\s*=\s*\{([^}]*)\}')
_QUOTED_RE = re.compile(r'"([^"]+)"')
TAG_BLOCKS = ("tags", "categories")


@dataclass(frozen=True)
class Descriptor:
    name: str = ""
    version: str = ""
    supported_version: str = ""
    tags: tuple[str, ...] = ()
    picture: str = ""
    path: str = ""
    remote_file_id: str = ""


def parse_descriptor(text: str) -> Descriptor:
    """Parse a Clausewitz descriptor (``name="x"``, ``tags={ "a" "b" }``)."""
    scalars: dict[str, str] = {}
    tags: list[str] = []
    seen: set[str] = set()
    for key, body in _BLOCK_RE.findall(text):
        if key.lower() in TAG_BLOCKS:
            for tag in _QUOTED_RE.findall(body):
                tag = tag.strip()
                if tag and tag.casefold() not in seen:
                    seen.add(tag.casefold())
                    tags.append(tag)
    for key, value in _SCALAR_RE.findall(text):
        scalars.setdefault(key.lower(), value)
    return Descriptor(
        name=scalars.get("name", ""),
        version=scalars.get("version", ""),
        supported_version=scalars.get("supported_version", ""),
        tags=tuple(tags),
        picture=scalars.get("picture", ""),
        path=scalars.get("path", ""),
        remote_file_id=scalars.get("remote_file_id", ""),
    )


def version_key(value: str) -> tuple[int, ...]:
    """Order game versions numerically: ``1.9`` < ``1.14`` < ``1.15.*``.

    A wildcard counts as zero, so ``1.14.*`` sorts before ``1.15``; anything
    unparsable sorts first (the safest "oldest" answer).
    """
    parts = [p for p in re.split(r"[^0-9]+", value or "") if p != ""]
    if not parts:
        return (0,)
    numbers = [int(p) for p in parts]
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers)


def oldest(values: Iterable[str]) -> str:
    """Oldest non-empty version string, keeping its original spelling."""
    cleaned = [v.strip() for v in values if v and v.strip()]
    if not cleaned:
        return ""
    return min(cleaned, key=version_key)


def merge_tags(descriptors: Iterable[Descriptor]) -> tuple[str, ...]:
    """Union of tags/categories, deduplicated case-insensitively, ordered."""
    seen: set[str] = set()
    out: list[str] = []
    for d in descriptors:
        for tag in d.tags:
            key = tag.casefold()
            if key not in seen:
                seen.add(key)
                out.append(tag)
    return tuple(out)


def _quote(value: str) -> str:
    """Clausewitz strings: drop characters that would break the field."""
    return value.replace('"', "'").replace("\r", " ").replace("\n", " ").strip()


def _tag_block(tags: Sequence[str]) -> str:
    if not tags:
        return ""
    body = "".join(f'\t"{_quote(t)}"\n' for t in tags)
    return f"tags={{\n{body}}}\n"


def render(name: str, *, version: str, supported_version: str,
           tags: Sequence[str] = (), path: str = "", picture: str = "") -> str:
    """Descriptor text in the same shape the game's own files use."""
    lines = [f'version="{_quote(version) or DEFAULT_VERSION}"\n',
             _tag_block(tags),
             f'name="{_quote(name)}"\n']
    if supported_version:
        lines.append(f'supported_version="{_quote(supported_version)}"\n')
    if picture:
        lines.append(f'picture="{_quote(picture)}"\n')
    if path:
        lines.append(f'path="{_quote(path)}"\n')
    return "".join(lines)


def launcher_path(staging: Path) -> str:
    """``mod/<folder>`` when staging sits in a mod folder, else the absolute path."""
    staging = staging.resolve()
    if staging.parent.name.lower() == "mod":
        return f"mod/{staging.name}"
    return staging.as_posix()


def read_source_descriptors(sources: Iterable[Path]) -> list[Descriptor]:
    found: list[Descriptor] = []
    for src in sources:
        candidate = Path(src) / "descriptor.mod"
        try:
            text, _ = fsutil.read_text(candidate)
        except (OSError, UnicodeDecodeError):
            continue
        found.append(parse_descriptor(text))
    return found


def build(staging: Path, sources: Sequence[Path]) -> tuple[str, str]:
    """(text for descriptor.mod, text for the launcher .mod)."""
    parsed = read_source_descriptors(sources)
    name = staging.resolve().name
    supported = oldest(d.supported_version for d in parsed)
    version = oldest(d.version for d in parsed) or DEFAULT_VERSION
    tags = merge_tags(parsed)
    picture = ""
    if (staging / "thumbnail.png").is_file():
        picture = "thumbnail.png"
    inner = render(name, version=version, supported_version=supported,
                   tags=tags, picture=picture)
    outer = render(name, version=version, supported_version=supported,
                   tags=tags, picture=picture, path=launcher_path(staging))
    return inner, outer


def write_for_staging(staging: Path, sources: Sequence[Path],
                      enabled: bool = True) -> dict:
    """Write ``<staging>/descriptor.mod`` and ``<staging>.mod``.

    Called after the merge: the sources' own descriptors are already copied and
    would otherwise be the ones left in place.
    """
    if not enabled:
        return {"written": False, "reason": "disabled"}
    if not staging.is_dir():
        return {"written": False, "reason": "staging missing"}
    inner, outer = build(staging, sources)
    staging_descriptor = staging / "descriptor.mod"
    launcher = staging.parent / f"{staging.name}.mod"
    staging_descriptor.write_text(inner, encoding="utf-8", newline="\n")
    launcher.write_text(outer, encoding="utf-8", newline="\n")
    parsed = parse_descriptor(outer)
    return {
        "written": True,
        "name": parsed.name,
        "version": parsed.version,
        "supported_version": parsed.supported_version,
        "tags": list(parsed.tags),
        "path": parsed.path,
        "files": [str(staging_descriptor.name), str(launcher.name)],
    }
