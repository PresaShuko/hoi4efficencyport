#!/usr/bin/env python
"""Run the whole hoi4port test suite (stdlib unittest discovery).

Usage:
    python tests/run_all.py               # all tests
    python tests/run_all.py -k cutover    # only tests whose id contains it
Exit code: 1 when there is any failure/error, else 0.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))


def filter_suite(suite: unittest.TestSuite, pattern: str) -> unittest.TestSuite:
    """Keep only the tests whose id contains ``pattern``."""
    kept = unittest.TestSuite()
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            kept.addTest(filter_suite(test, pattern))
        elif pattern in test.id():
            kept.addTest(test)
    return kept


def main(argv: list[str]) -> int:
    pattern = None
    args = list(argv)
    if "-k" in args:
        i = args.index("-k")
        if i + 1 >= len(args):
            print("usage: run_all.py [-k substring]", file=sys.stderr)
            return 2
        pattern = args[i + 1]
        del args[i:i + 2]
    if args:
        print(f"unknown arguments: {' '.join(args)}", file=sys.stderr)
        return 2

    loader = unittest.TestLoader()
    suite = loader.discover(str(HERE), pattern="test_*.py")
    if pattern:
        suite = filter_suite(suite, pattern)
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    bad = len(result.failures) + len(result.errors)
    print(f"\nrun={result.testsRun} failures={len(result.failures)} "
          f"errors={len(result.errors)} "
          f"expectedFailures={len(result.expectedFailures)} "
          f"skipped={len(result.skipped)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
