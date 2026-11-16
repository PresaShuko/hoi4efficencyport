"""Thread-pool execution shared by the texconv stages (convert, texfix)."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any


def run_pool(items: Sequence[Any], worker: Callable[[Any], dict],
             *, workers: int, every: int) -> list[dict]:
    """Run ``worker`` over ``items`` and collect one record per item.

    ``worker`` must return a dict with an ``ok`` flag (both texconv workers
    catch their own exceptions, so no record is ever lost).
    """
    manifest: list[dict] = []
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(worker, item) for item in items]
        for done, future in enumerate(as_completed(futures), 1):
            rec = future.result()
            manifest.append(rec)
            ok += 1 if rec.get("ok") else 0
            if done % every == 0:
                print(f"  … {done}/{len(futures)} (ok: {ok})")
    return manifest


def split(manifest: list[dict]) -> tuple[list[dict], list[dict]]:
    """(ok, failed) split of a worker manifest."""
    ok = [rec for rec in manifest if rec.get("ok")]
    return ok, [rec for rec in manifest if not rec.get("ok")]
