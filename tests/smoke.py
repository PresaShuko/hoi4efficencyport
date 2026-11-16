"""End-to-end smoke test on a minimal fixture — no game, no vanilla install.

Exercises install/config/CLI/IO: sync -> scan -> analyze -> texfix (dry-run)
-> meshscan -> validate; on Windows also convert --apply (bundled texconv).
Run locally with `python tests/smoke.py`; the full suite lives in
`tests/run_all.py`.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIXTURE = REPO / "tests" / "mini_mod"
WINDOWS = sys.platform == "win32"


def run(*args: str, expected_rc: int = 0) -> str:
    print(f"$ hoi4port {' '.join(args)}")
    r = subprocess.run([sys.executable, "-m", "hoi4port", *args],
                       capture_output=True, text=True, errors="replace")
    if r.stdout:
        print(r.stdout.rstrip())
    if r.returncode != expected_rc:
        print(r.stderr, file=sys.stderr)
        raise SystemExit(f"EXIT {r.returncode} (expected {expected_rc}): "
                         f"hoi4port {' '.join(args)}")
    return r.stdout


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hoi4port_smoke_") as td:
        tdp = Path(td)
        staging = tdp / "staging"
        out = tdp / "build"
        common = ["--mod", str(FIXTURE), "--staging", str(staging),
                  "--out", str(out)]

        assert "hoi4port " in subprocess.run(
            [sys.executable, "-m", "hoi4port", "--version"],
            capture_output=True, text=True).stdout

        run("sync", "--mod", str(FIXTURE), "--staging", str(staging))
        assert staging.is_dir() and (staging / "gfx" / "fixture" / "test.dds").is_file()

        scan_out = run("scan", *common, "--skip-vanilla")
        assert "true: 0" in scan_out
        assert (out / "scan.json").is_file()

        run("meshscan", *common, "--skip-vanilla")
        assert (out / "mesh_stats.json").is_file()

        run("analyze", *common, "--skip-vanilla")
        assert (out / "asset_plan.json").is_file()

        # fixture has no model textures: texfix exits 2 ("no targets")
        texfix_out = run("texfix", *common, expected_rc=2)
        assert "No model textures in" in texfix_out

        if WINDOWS:  # bundled texconv: real conversion
            run("convert", *common, "--apply")
            conv = json.loads((out / "convert_manifest.json").read_text("utf-8"))
            assert len(conv["ok"]) == 1 and not conv["failed"], conv
            hdr = (staging / "gfx" / "fixture" / "test.dds").read_bytes()[:88]
            assert hdr[84:88] in (b"DXT1", b"DXT5"), hdr[84:88]

        run("validate", *common)
    print("SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
