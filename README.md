# hoi4port

Asset porting and optimization pipeline for **Hearts of Iron IV** mods:
safe mod mirroring, reference scanning, normalization, DDS conversion
(DXT1/DXT5 + mips), dedup, cutover, mesh/entity audit and validation.
One CLI, idempotent, dry-run by default.

## Install

With Python 3.11+ (any OS):

```bash
pip install hoi4port-<version>-py3-none-any.whl
```

or, without Python, download `hoi4port.exe` (Windows) from the repository
*Releases* page.

Windows is the fully supported platform: the texture stages use
`texconv.exe` (DirectXTex, bundled with the package). On Linux/macOS all
pure-Python commands work; `convert`/`texfix`/`cutover --apply` require
Wine (invoked automatically, disable with `--no-wine`).

## Requirements

- **None** for sync/scan/normalize(dry-run)/analyze/meshscan/entityfix/validate.
- Installed Workshop mod (for `sync`) — autodetected from Steam libraries,
  otherwise pass `--mod <ID>`.
- HoI4 install (for vanilla baseline/dedup) — autodetected; without it,
  commands degrade gracefully (no baseline).

## Quickstart

```bash
hoi4port sync --mod 3350890356     # Workshop ID or path -> ./mod_tfr staging
hoi4port scan                      # references, missing sprites, baseline
hoi4port meshscan                  # mesh metrics (vertices, materials, collision)
hoi4port texfix --apply            # full mip chain + DXT1/DXT5 model textures
hoi4port validate                  # final checks (exit != 0 on problems)
```

Persistent configuration: `hoi4port.toml` (see `hoi4port.example.toml`) in
the current directory or in `%APPDATA%\hoi4port\` (Windows) /
`~/.config/hoi4port/` (Linux/macOS).

Resolution order for every path:
**CLI flags -> env `HOI4PORT_MOD/STAGING/VANILLA/OUT` -> `hoi4port.toml` -> Steam autodetect.**

## Selecting the mod

Four ways to choose which mod to optimize (the first that is set wins):

```bash
hoi4port sync --mod 3350890356        # 1. flag: numeric Workshop ID...
hoi4port sync --mod "D:\path\to\mod"  #    ...or a direct folder path
set HOI4PORT_MOD=3350890356           # 2. environment variable (session)
```

```toml
# 3. hoi4port.toml — persistent default (copy from hoi4port.example.toml)
mod = "3350890356"
```

4. **No selection**: with exactly one HoI4 Workshop mod installed it is
   picked automatically; with several, `sync` lists them and asks you to
   choose with `--mod`. A numeric ID is resolved across **all** Steam
   libraries (Windows registry + `libraryfolders.vdf`, Linux, macOS).

## Commands

| Command | What it does | Modifies files? |
|---|---|---|
| `sync` | Mirror Workshop mod -> staging (`--mod <ID\|path>`) | staging only |
| `scan` | Asset references, missing GFX_*, vanilla hash baseline | no |
| `normalize` | Vanilla-style asset sorting + reference repair | `--apply` |
| `analyze` | Asset plan: alpha, cap, dedup, unused | no |
| `convert` | Per-class DDS conversion (BC1/BC3, cap, %4 padding) | `--apply` |
| `cutover` | Reference rewrite + dedup + original removal | `--apply` |
| `meshscan` | `.mesh` analysis: vertices, triangles, materials, collision | no |
| `texfix` | Model textures: full mip chain + DXT1/DXT5 | `--apply` |
| `entityfix` | Entity audit + `cull_radius` for ambient objects | `--apply` |
| `validate` | Post-cutover validation: DDS, scan, disk sizes | no |
| `check-log` | Texture/sprite errors from the game `error.log` | no |

Every stage writes JSON manifests/reports into `build/`; a second run is a
no-op (idempotent).

## Troubleshooting

- **"texconv not found"**: the package bundles it in `pipeline/bin/`;
  if you have your own build, set `HOI4PORT_TEXCONV=<path>`.
- **"Source mod not determined"**: pass `--mod <Workshop ID>` (the error
  lists the mods it found).
- **Vanilla not found** (non-standard Steam library): `--vanilla <path>`.
- **Game log**: `check-log` looks for `error.log` in the standard Windows
  (including OneDrive), Linux (Proton and native) and macOS locations.

## License

MIT. `texconv.exe` is [DirectXTex](https://github.com/microsoft/DirectXTex) (MIT).
