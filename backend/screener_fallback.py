"""
screener_fallback.py
====================
Source-fallback orchestration for the screener route, kept as a pure async
function with the fetchers injected so it's unit-testable without importing
FastAPI, slowapi, or yfinance.

Policy: Finviz Elite is preferred (real-time) but must not be a hard dependency.
Its export ``auth`` token is session-tied and Finviz rotates it, so a stale token
401s every call. When the Elite source is configured-but-failing (or simply
returns no rows), drop to the Yahoo source (delayed) so a dead token degrades to
delayed data instead of blanking the screener.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable

FetchFn = Callable[..., Awaitable[dict[str, Any]]]


async def run_with_fallback(
    *,
    preset: str,
    filters: str | None,
    limit: int,
    primary_name: str,
    primary_fetch: FetchFn,
    fallback_fetch: FetchFn,
) -> tuple[str, dict[str, Any]]:
    """
    Run ``primary_fetch``; if the primary is Finviz Elite and it yields no rows,
    retry with ``fallback_fetch`` (Yahoo). Returns ``(source_name, result)`` —
    ``source_name`` is "yahoo" only when the fallback actually produced rows, so
    the caller can label the response truthfully. If the fallback also comes back
    empty, the original (primary) result is kept so its failure status surfaces.
    """
    result = await primary_fetch(preset=preset, filters=filters, limit=limit)
    if primary_name == "finviz_elite" and not result.get("rows"):
        fb = await fallback_fetch(preset=preset, filters=filters, limit=limit)
        if fb.get("rows"):
            return "yahoo", fb
    return primary_name, result
