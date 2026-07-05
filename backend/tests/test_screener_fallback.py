"""Unit tests for the screener source-fallback policy (pure; no web/yfinance)."""

import asyncio

import screener_fallback as sf


def _fetch(result, calls=None, tag=None):
    """A fake async fetcher returning a fixed result; records calls if given."""
    async def f(*, preset, filters, limit):
        if calls is not None:
            calls.append(tag)
        return dict(result)
    return f


def _run(**kw):
    return asyncio.run(sf.run_with_fallback(**kw))


def test_finviz_success_serves_finviz():
    name, res = _run(
        preset="top_gainers", filters=None, limit=30, primary_name="finviz_elite",
        primary_fetch=_fetch({"rows": [{"ticker": "X"}], "source": "finviz_elite", "status": None}),
        fallback_fetch=_fetch({"rows": [{"ticker": "Y"}], "source": "yahoo"}),
    )
    assert name == "finviz_elite"
    assert res["rows"][0]["ticker"] == "X"


def test_finviz_empty_falls_back_to_yahoo():
    name, res = _run(
        preset="top_gainers", filters=None, limit=30, primary_name="finviz_elite",
        primary_fetch=_fetch({"rows": [], "source": "finviz_elite", "status": "finviz elite HTTP 401"}),
        fallback_fetch=_fetch({"rows": [{"ticker": "Y"}], "source": "yahoo", "status": None}),
    )
    assert name == "yahoo"
    assert res["rows"][0]["ticker"] == "Y"


def test_yahoo_primary_never_attempts_fallback():
    calls = []
    name, _ = _run(
        preset="top_gainers", filters=None, limit=30, primary_name="yahoo",
        primary_fetch=_fetch({"rows": [], "source": "yahoo", "status": "down"}, calls, "primary"),
        fallback_fetch=_fetch({"rows": [{"ticker": "Z"}]}, calls, "fallback"),
    )
    assert name == "yahoo"
    assert "fallback" not in calls   # fallback only fires for a failing finviz_elite


def test_both_empty_keeps_finviz_failure_status():
    name, res = _run(
        preset="top_gainers", filters=None, limit=30, primary_name="finviz_elite",
        primary_fetch=_fetch({"rows": [], "source": "finviz_elite", "status": "finviz elite HTTP 401"}),
        fallback_fetch=_fetch({"rows": [], "source": "yahoo", "status": "yahoo unreachable"}),
    )
    assert name == "finviz_elite"
    assert res["status"] == "finviz elite HTTP 401"   # primary status surfaces
