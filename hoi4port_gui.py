"""Windowed entry point for the standalone ``hoi4port-gui.exe`` build.

Keeping the GUI behind its own script means a double click opens the window
instead of the CLI parser complaining about a missing command.
"""
from __future__ import annotations

from pipeline.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
