"""
edgar_tickers.py
================
SEC **CIK → ticker** mapping from the official ``company_tickers.json``.

EDGAR filings carry a CIK (a filer ID), not a ticker — an 8-K from Genco shows up
as ``"8-K - GENCO SHIPPING & TRADING LTD (0001326200) (Filer)"``. Without a CIK
map the catalyst ranker's text extractor can't turn that into ``GNK``, so the
regulatory lane (SEC + FDA) sees filings but produces no candidates. This module
loads SEC's authoritative ~10k-row CIK↔ticker file once per process (cached,
refreshed daily) and hands the ranker a ``{cik:int -> ticker:str}`` map.

Pure parsing (``parse_company_tickers``) is unit-tested; ``load_cik_map`` adds
the cached network fetch and never raises — a fetch failure just yields the last
good map (or an empty one), so ranking degrades gracefully rather than breaking.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Optional

log = logging.getLogger("edgar_tickers")

_URL = "https://www.sec.gov/files/company_tickers.json"
# SEC fair-access asks for a contact in the UA — same env var the ingestion
# client uses (set SEC_CONTACT_EMAIL on the deployment).
_CONTACT = os.environ.get("SEC_CONTACT_EMAIL", "set-SEC_CONTACT_EMAIL@example.com")
_HEADERS = {
    "User-Agent": f"FinancialNewsDashboard/1.0 ({_CONTACT})",
    "Accept": "application/json",
}
_TTL_SECONDS = 24 * 3600.0  # the file changes slowly; refresh daily
_TIMEOUT = 20

# Module-level cache so the ~10k-row file is fetched at most once a day per
# process, shared across every ranking run.
_cache: dict[str, Any] = {"fetched_at": 0.0, "cik_map": {}}
_lock = asyncio.Lock()


def parse_company_tickers(raw: Any) -> dict[int, str]:
    """
    ``{cik:int -> TICKER}`` from SEC's company_tickers.json payload.

    The file is a dict of rows ``{"0": {"cik_str": 320193, "ticker": "AAPL",
    "title": "Apple Inc."}, ...}``. Rows missing a CIK or ticker are skipped.
    On a duplicate CIK the first ticker wins (the file lists one row per CIK).
    """
    rows = raw.values() if isinstance(raw, dict) else (raw or [])
    cik_map: dict[int, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            cik = int(row["cik_str"])
        except (KeyError, ValueError, TypeError):
            continue
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker:
            continue
        cik_map.setdefault(cik, ticker)
    return cik_map


async def _fetch_raw(session: Any = None) -> Optional[Any]:
    """GET company_tickers.json; None on any failure (never raises)."""
    try:
        import aiohttp
    except ImportError:
        return None
    owns = session is None
    if owns:
        session = aiohttp.ClientSession(headers=_HEADERS)
    try:
        async with session.get(_URL, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                log.warning("company_tickers.json HTTP %s", resp.status)
                return None
            return await resp.json(content_type=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("company_tickers.json fetch failed: %s", type(exc).__name__)
        return None
    finally:
        if owns:
            await session.close()


async def load_cik_map(
    *, session: Any = None, force: bool = False
) -> dict[int, str]:
    """
    Cached ``{cik -> ticker}``. Fetches at most once per ``_TTL_SECONDS``; on a
    fetch failure returns the last good map (possibly empty). Safe to call on
    every ranking run.
    """
    now = time.monotonic()
    if not force and _cache["cik_map"] and (now - _cache["fetched_at"]) < _TTL_SECONDS:
        return _cache["cik_map"]

    async with _lock:
        # Re-check inside the lock — another task may have refreshed it.
        now = time.monotonic()
        if not force and _cache["cik_map"] and (now - _cache["fetched_at"]) < _TTL_SECONDS:
            return _cache["cik_map"]

        raw = await _fetch_raw(session=session)
        if raw is not None:
            parsed = parse_company_tickers(raw)
            if parsed:
                _cache["cik_map"] = parsed
                _cache["fetched_at"] = time.monotonic()
                log.info("loaded %d CIK→ticker mappings", len(parsed))
        return _cache["cik_map"]
