"""Windowed entry point for the standalone ``hoi4port-gui.exe`` build.

Two jobs, both decided by argv:

* ``--cli ...``: the frozen GUI re-invokes itself to run one pipeline stage
  (stdout piped back to the window); delegate to the CLI entry point, which
  strips the switch.
* anything else: open the window. A double click must never fall into the
  argparse "missing command" path, which a windowed build cannot display.
"""
from __future__ import annotations

import sys

import hoi4port

from pipeline.gui import main as gui_main


def main() -> int:
    if sys.argv[1:2] == ["--cli"]:
        return hoi4port.main()
    return gui_main()


if __name__ == "__main__":
    raise SystemExit(main())
