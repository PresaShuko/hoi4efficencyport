"""Shared hoi4port configuration: one place for path resolution.

Resolution order (first hit wins):
1. CLI flags (`--mod`, `--staging`, `--vanilla`, `--out`);
2. environment variables `HOI4PORT_MOD`, `HOI4PORT_STAGING`,
   `HOI4PORT_VANILLA`, `HOI4PORT_OUT`;
3. `hoi4port.toml` file (current directory, then user config directory),
   keys: `mod`, `staging`, `vanilla`, `out`;
4. Steam library autodetect on all platforms.

`--mod` accepts a numeric Workshop ID (resolved to
`<library>/workshop/content/394360/<id>`) or a direct path, and may be
repeated: several mods are merged into the staging tree in load order, later
entries overriding earlier ones (exactly what the game does when mods are
enabled together). `HOI4PORT_MOD` and the `mods` TOML key accept the same
list (`os.pathsep`-separated for the environment, an array in the file).

No path is hardcoded: on a clean machine the pure-Python commands work with
flags or a config file alone; `workshop_mod` may stay None (commands that
require it say so with precise instructions).
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

HOI4_APPID = "394360"
VANILLA_DIRNAME = "Hearts of Iron IV"
DEFAULT_STAGING_NAME = "mod_tfr"
DEFAULT_OUT_NAME = "build"

ENV = {"mod": "HOI4PORT_MOD", "staging": "HOI4PORT_STAGING",
       "vanilla": "HOI4PORT_VANILLA", "out": "HOI4PORT_OUT"}


@dataclass(frozen=True)
class Config:
    workshop_mods: tuple[Path, ...]
    staging: Path
    vanilla: Path | None
    out: Path

    @property
    def workshop_mod(self) -> Path | None:
        """First source mod: what single-mod commands and messages refer to."""
        return self.workshop_mods[0] if self.workshop_mods else None


# --- settings from file -------------------------------------------------------------

def _config_files() -> list[Path]:
    """hoi4port.toml in the cwd, then in the user config directory."""
    paths = [Path.cwd() / "hoi4port.toml"]
    if os.name == "nt":
        base = os.environ.get("APPDATA")
        if base:
            paths.append(Path(base) / "hoi4port" / "hoi4port.toml")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
        paths.append(Path(xdg) / "hoi4port" / "hoi4port.toml")
    return paths


def _settings() -> dict[str, str]:
    for f in _config_files():
        if f.is_file():
            try:
                with f.open("rb") as fh:
                    data = tomllib.load(fh)
            except tomllib.TOMLDecodeError as e:
                raise SystemExit(f"Invalid hoi4port.toml ({f}): {e}") from None
            out: dict[str, str] = {}
            for k, v in data.items():
                if isinstance(v, bool):
                    continue
                if isinstance(v, (str, int)):
                    if v != "":
                        out[k] = str(v)
                elif isinstance(v, list):
                    items = [str(i) for i in v if isinstance(i, (str, int))
                             and not isinstance(i, bool)]
                    if items:
                        out[k] = os.pathsep.join(items)
            return out
    return {}


class _Settings:
    """Lazy, single-read hoi4port.toml for the duration of one command."""

    __slots__ = ("_data",)

    def __init__(self) -> None:
        self._data: dict[str, str] | None = None

    def get(self, key: str) -> str | None:
        if self._data is None:
            self._data = _settings()
        return self._data.get(key)


# --- Steam autodetect -----------------------------------------------------------------

def _steam_roots() -> list[Path]:
    home = Path.home()
    roots: list[Path] = []
    if sys.platform == "win32":
        try:
            import winreg
            for hive, key in (
                (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            ):
                try:
                    with winreg.OpenKey(hive, key) as k:
                        val, _ = winreg.QueryValueEx(k, "SteamPath")
                    roots.append(Path(val))
                except OSError:
                    pass
        except ImportError:
            pass
        roots += [Path(r"C:\Program Files (x86)\Steam"),
                  Path(r"C:\Program Files\Steam")]
    elif sys.platform == "darwin":
        roots.append(home / "Library/Application Support/Steam")
    else:
        roots += [home / ".steam/steam", home / ".local/share/Steam",
                  home / ".steam/root",
                  home / ".var/app/com.valvesoftware.Steam/.steam/steam"]
    return list(dict.fromkeys(roots))


_VDF_PATH_RE = re.compile(r'"path"\s+"([^"]+)"')


def _steam_libraries() -> list[Path]:
    """Known Steam libraries: candidate roots + those listed in libraryfolders.vdf."""
    libs: list[Path] = []
    for root in _steam_roots():
        if root not in libs and (root / "steamapps").is_dir():
            libs.append(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.is_file():
            try:
                text = vdf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for m in _VDF_PATH_RE.finditer(text):
                p = Path(m.group(1).replace("\\\\", "\\"))
                if p not in libs and p.is_dir():
                    libs.append(p)
    return libs


def workshop_candidates() -> list[Path]:
    """HoI4 Workshop mods installed across all known libraries."""
    out: set[Path] = set()
    for lib in _steam_libraries():
        base = lib / "steamapps" / "workshop" / "content" / HOI4_APPID
        if base.is_dir():
            out.update(d for d in base.iterdir() if d.is_dir())
    return sorted(out)


# --- individual value resolution --------------------------------------------------------

def _pick(cli: str | None, key: str, settings: _Settings | None = None) -> str | None:
    if cli:
        return cli
    env = os.environ.get(ENV[key])
    if env:
        return env
    return (settings or _Settings()).get(key)


def _resolve_one(value: str) -> Path:
    p = Path(value).expanduser()
    if p.is_dir():
        return p.resolve()
    if p.name.isdigit():
        for cand in workshop_candidates():
            if cand.name == p.name:
                return cand.resolve()
        libs = ", ".join(str(lib) for lib in _steam_libraries()) \
            or "no Steam library found"
        raise SystemExit(
            f"--mod: Workshop mod {p.name} not found. Libraries searched: {libs}")
    raise SystemExit(
        f"--mod: unrecognized value: {value} — pass a numeric Workshop "
        f"ID or the mod folder path.")


def _split_list(value: str) -> list[str]:
    """Split an env/config list: os.pathsep, and commas for convenience."""
    parts = [v.strip() for v in value.replace(",", os.pathsep).split(os.pathsep)]
    return [v for v in parts if v]


def resolve_mods(values: list[str] | None,
                 settings: _Settings | None = None) -> tuple[Path, ...]:
    """Source mods in load order; empty when nothing can be determined.

    Order matters: later mods override earlier ones, like HoI4 does.
    """
    settings = settings or _Settings()
    entries: list[str] = []
    if values:
        entries = [v for v in values if v and v.strip()]
    if not entries:
        env = os.environ.get(ENV["mod"])
        if env:
            entries = _split_list(env)
    if not entries:
        from_file = (settings.get("mods") or "").strip()
        if from_file:
            entries = _split_list(from_file)
        elif (single := settings.get("mod")):
            entries = [single]
    if entries:
        return tuple(_resolve_one(v) for v in entries)
    found = workshop_candidates()
    return (found[0].resolve(),) if len(found) == 1 else ()


def resolve_workshop(value: str | None, settings: _Settings | None = None) -> Path | None:
    """Single Workshop mod path (compatibility wrapper around resolve_mods)."""
    mods = resolve_mods([value] if value else None, settings)
    return mods[0] if mods else None


def resolve_vanilla(value: str | None, settings: _Settings | None = None) -> Path | None:
    value = _pick(value, "vanilla", settings)
    if value:
        p = Path(value).expanduser()
        if p.is_dir():
            return p.resolve()
        print(f"Warning: vanilla install given but not found: {p}", file=sys.stderr)
        return None
    for lib in _steam_libraries():
        cand = lib / "steamapps" / "common" / VANILLA_DIRNAME
        if cand.is_dir():
            return cand.resolve()
    return None


def load_config(
    mod: list[str] | None = None,
    staging: str | None = None,
    vanilla: str | None = None,
    out: str | None = None,
) -> Config:
    settings = _Settings()  # at most one TOML read per command (C1)
    st = _pick(staging, "staging", settings) or DEFAULT_STAGING_NAME
    o = _pick(out, "out", settings) or DEFAULT_OUT_NAME
    return Config(
        workshop_mods=resolve_mods(mod if isinstance(mod, list) else
                                   ([mod] if mod else None), settings),
        staging=Path(st).expanduser().resolve(),
        vanilla=resolve_vanilla(vanilla, settings),
        out=Path(o).expanduser().resolve(),
    )
