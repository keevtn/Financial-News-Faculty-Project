"""
backfill_tickers.py
===================
One-time (repeatable) maintenance pass that re-validates the ``tickers`` field on
social documents already stored in MongoDB.

Why this is needed
------------------
Social ingestion used to tag any ``$XXXX`` cashtag as a ticker, so Reddit/Bluesky
posts stuck fake symbols ($YOLO, $MOON, $PUMP …) onto documents that are now
persisted. Fixing the live extractor only affects *new* items — the dedup cache
keeps already-seen posts from being re-dispatched, so historical docs keep their
bogus tickers (and surface them in the frontend ticker filter) forever. This
script repairs them in place, independent of the live ingestion loop.

For each social document it re-runs the *exact same* extraction the live social
agent now uses (``extract_social_tickers`` with the SEC + crypto universe): the
curated company-name and SEC-CIK passes are trusted, while cashtags and
platform-provided symbols are validated against the real-ticker universe. If the
recomputed set differs from what's stored, it writes the clean set back.

Idempotent: a second run over a repaired DB changes nothing. Fails closed — if the
real-ticker universe can't be loaded, it writes nothing (so a transient SEC fetch
failure can't blank out every document's tickers).

Run
---
    python backend/backfill_tickers.py             # re-validate docs that have tickers
    python backend/backfill_tickers.py --all       # re-extract every social doc
    python backend/backfill_tickers.py --dry-run   # report, write nothing
"""

from __future__ import annotations
import argparse
import asyncio
import os
import sys

# Ensure this script's directory (backend/) is importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load .env from the project root so MONGODB_URI / SEC_CONTACT_EMAIL are available.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
except ImportError:
    pass

from listed_symbols import load_valid_tickers
from ticker_extractor import TickerExtractor, extract_social_tickers


async def main(do_all: bool, dry_run: bool) -> None:
    uri = os.environ.get("MONGODB_URI")
    if not uri:
        raise SystemExit("MONGODB_URI not set — add it to .env or the environment.")

    # Load the real-ticker universe first. Fail closed: without it we cannot tell
    # a real ticker from a fake one, so writing anything risks corrupting good data.
    universe = await load_valid_tickers()
    if not universe:
        raise SystemExit(
            "Could not load the real-ticker universe (Nasdaq directory + SEC). "
            "Aborting so we don't rewrite tickers without a validation set."
        )
    extractor = TickerExtractor(valid_tickers=universe)
    print(f"real-ticker universe loaded: {len(universe)} symbols "
          f"(US-listed + SEC + crypto)")

    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(uri, tz_aware=True)
    collection = client["financial_news"]["news_items"]

    # Default: only docs that already carry tickers (the ones that can have fakes
    # to strip). --all re-extracts every social doc, which can also *add* tickers
    # newly resolvable via the company-name map.
    if do_all:
        query: dict = {"source_type": "social"}
    else:
        query = {"source_type": "social", "tickers": {"$exists": True, "$ne": []}}

    total = await collection.count_documents(query)
    print(f"social docs to process: {total}  (mode={'all' if do_all else 'has-tickers'}, "
          f"dry_run={dry_run})")

    scanned = changed = fakes_removed = tickers_added = 0
    cursor = collection.find(query)
    async for doc in cursor:
        scanned += 1
        title = doc.get("title", "") or ""
        desc = doc.get("description", "") or ""
        extra = doc.get("extra") or {}
        old = tuple(doc.get("tickers") or ())

        new = extract_social_tickers(extractor, title, desc, extra)

        if tuple(sorted(old)) == new:
            continue

        removed = set(old) - set(new)
        added = set(new) - set(old)
        fakes_removed += len(removed)
        tickers_added += len(added)
        changed += 1

        if not dry_run:
            await collection.update_one(
                {"content_hash": doc["content_hash"]},
                {"$set": {"tickers": list(new)}},
            )

    print(f"scanned={scanned}  docs_changed={changed}  "
          f"fake_tickers_removed={fakes_removed}  tickers_added={tickers_added}"
          f"{'  (DRY RUN — nothing written)' if dry_run else ''}")
    client.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Re-validate tickers on stored social docs against the real-ticker universe"
    )
    ap.add_argument("--all", action="store_true",
                    help="Re-extract every social doc, not just ones that already have tickers")
    ap.add_argument("--dry-run", action="store_true",
                    help="Report what would change without writing")
    args = ap.parse_args()
    asyncio.run(main(args.all, args.dry_run))
