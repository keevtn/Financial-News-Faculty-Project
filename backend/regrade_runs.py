"""
regrade_runs.py
===============
Re-grade historical catalyst runs onto the **gap-inclusive** basis
(prev_close -> close).

Why: grading previously measured next-session open->close, which excludes the
overnight gap — the return overnight catalysts actually express in. Every
downstream consumer (track record, expected-move calibration, weight tuner,
signal_eval) was optimizing against post-gap drift. New runs are graded on the
fixed basis automatically; this tool rewrites history so calibration doesn't
train on a mix of bases.

Run (from the project root, venv Python):
    python backend/regrade_runs.py --dry-run    # show what would change
    python backend/regrade_runs.py              # regrade all graded runs
    python backend/regrade_runs.py --limit 10   # bounded pass (newest first)

Reads prices from yfinance per run; writes real metrics onto real run docs
(``grade_run`` upserts ``metrics``). Runs whose basis is already
``prev_close`` are skipped, so the pass is idempotent.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

_BACKEND = os.path.dirname(os.path.abspath(__file__))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


def _load_env() -> None:
    env = os.path.join(os.path.dirname(_BACKEND), ".env")
    if os.path.isfile(env):
        for ln in open(env, encoding="utf-8-sig"):
            if "=" in ln and not ln.strip().startswith("#"):
                k, _, v = ln.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


async def main(dry_run: bool, limit: int | None) -> None:
    _load_env()
    uri = os.environ.get("MONGODB_URI", "").strip()
    if not uri:
        sys.exit("MONGODB_URI is not set (checked environment and .env)")

    from motor.motor_asyncio import AsyncIOMotorClient
    from catalyst_ranker import grade_run

    client = AsyncIOMotorClient(uri, tz_aware=True)
    coll = client["financial_news"]["catalyst_rankings"]

    query = {"metrics.graded": {"$gt": 0}}
    docs = await (
        coll.find(query, {"_id": 0})
        .sort("generated_at", -1)
        .limit(limit or 1000)
        .to_list(length=limit or 1000)
    )
    print(f"graded catalyst runs found: {len(docs)}")

    regraded = skipped = failed = 0
    for run_doc in docs:
        old = run_doc.get("metrics") or {}
        if old.get("entry_basis") == "prev_close":
            skipped += 1
            continue
        rid = run_doc.get("run_id", "?")[:12]
        if dry_run:
            print(f"  would regrade {rid} (old basis: {old.get('entry_basis', 'open')}, "
                  f"sep={old.get('reaction_separation')})")
            regraded += 1
            continue
        try:
            new = await grade_run(coll, run_doc)
        except Exception as exc:  # noqa: BLE001
            print(f"  FAILED {rid}: {type(exc).__name__}: {exc}")
            failed += 1
            continue
        if new is None:
            print(f"  ungradeable {rid} (window not closed / no timestamp)")
            failed += 1
            continue
        print(f"  regraded {rid}: separation {old.get('reaction_separation')} "
              f"-> {new.get('reaction_separation')}  "
              f"hit_rate {old.get('direction_hit_rate')} -> {new.get('direction_hit_rate')}  "
              f"[basis {new.get('entry_basis')}]")
        regraded += 1

    print(f"\n{'would regrade' if dry_run else 'regraded'}: {regraded}  "
          f"already-new-basis: {skipped}  failed: {failed}")
    client.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dry-run", action="store_true", help="report, write nothing")
    p.add_argument("--limit", type=int, default=None, help="newest N runs only")
    a = p.parse_args()
    asyncio.run(main(a.dry_run, a.limit))
