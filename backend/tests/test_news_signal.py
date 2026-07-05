"""Unit tests for news_signal — pure logic, no network/Mongo.

Fixed clock: Thu 2026-07-02 18:00 UTC (2026-07-03 is a market holiday, so the
5-trading-day veto walk spans a weekend AND a holiday — the boundary cases the
flat window must get right).
"""

from datetime import datetime, timedelta, timezone

import news_signal as ns

NOW = datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc)


def _doc(title, hours_ago, *, source="Reuters", stype="rss", desc="",
         tickers=("NOVA",), sent=None):
    return {
        "title": title, "description": desc, "source": source,
        "source_type": stype, "published_at": NOW - timedelta(hours=hours_ago),
        "tickers": list(tickers),
        "sentiment": {"score": sent} if sent is not None else None,
    }


def _halt_doc(sym, hours_ago, code="T1", resumed=False):
    """Real Nasdaq Trade Halts item shape: bare-symbol title, code in the
    description table, resumption columns trailing the code."""
    resume_cell = '<td valign="top">07/02/2026</td>' if resumed else '<td valign="top"></td>'
    desc = (
        '<table><tr><th>Halt Date</th><th>Reason Code</th></tr>'
        f'<tr><td valign="top">07/02/2026</td><td valign="top">13:50:00.000</td>'
        f'<td valign="top">{sym}</td><td valign="top">Some Corp</td>'
        f'<td valign="top">NASDAQ</td><td valign="top">{code}</td>'
        f'<td valign="top"></td>{resume_cell}<td valign="top"></td></tr></table>'
    )
    return {"title": sym, "description": desc, "source": ns.HALT_SOURCE,
            "source_type": "rss", "published_at": NOW - timedelta(hours=hours_ago),
            "tickers": []}


class TestDecay:
    def test_fresh_is_full_strength(self):
        assert ns._decay(0.0, 6.0) == 1.0

    def test_half_life(self):
        assert abs(ns._decay(6.0, 6.0) - 0.5) < 1e-9
        assert abs(ns._decay(12.0, 6.0) - 0.25) < 1e-9

    def test_future_stamp_clamps(self):
        # wire clock skew must not produce weight > 1
        assert ns._decay(-2.0, 6.0) == 1.0


class TestClassifyBullish:
    def test_fda_approval_is_strongest(self):
        name, w = ns.classify_bullish("Nova receives FDA approval for lead drug")
        assert name == "fda_approval" and w == 1.0

    def test_beat_and_raise(self):
        name, _ = ns.classify_bullish("Nova beats estimates and raises guidance")
        assert name == "beat_and_raise"

    def test_strongest_class_wins(self):
        # approval + buyback in one headline -> approval (higher weight)
        name, w = ns.classify_bullish(
            "Nova wins FDA approval, expands buyback program")
        assert name == "fda_approval" and w == 1.0

    def test_case_insensitive(self):
        assert ns.classify_bullish("NOVA WINS FDA APPROVAL") is not None

    def test_plain_headline_is_none(self):
        assert ns.classify_bullish("Nova to present at industry conference") is None


class TestClassifyVeto:
    def test_offering(self):
        assert ns.classify_veto("Nova announces $50 million public offering") == "dilutive_offering"

    def test_going_concern(self):
        assert ns.classify_veto("Auditor raises going concern doubt") == "going_concern"

    def test_chapter_11(self):
        assert ns.classify_veto("Nova files for chapter 11 bankruptcy protection") == "chapter_11"

    def test_withdrawal_suppresses(self):
        # a cancelled offering removes the dilution — must NOT veto
        assert ns.classify_veto("Nova withdraws proposed public offering") is None

    def test_offering_needs_finance_qualifier(self):
        # "offering" as a plain verb must not fire
        assert ns.classify_veto("Nova begins offering customers free trials") is None


class TestNewsIgnition:
    def test_empty_docs_zero(self):
        ign, comp = ns.news_ignition([], now=NOW)
        assert ign == 0.0 and comp["n_docs"] == 0

    def test_single_fresh_approval_exact(self):
        ign, comp = ns.news_ignition(
            [_doc("Nova wins FDA approval", 3.0)], now=NOW, halflife_h=6.0)
        expect = min(1.0, (1.0 * 0.5 ** (3.0 / 6.0)) / 1.25)
        assert abs(ign - round(expect, 4)) < 1e-9
        assert "fda_approval" in comp["classes"]

    def test_decay_orders_fresh_over_stale(self):
        fresh, _ = ns.news_ignition([_doc("Nova wins FDA approval", 2.0)], now=NOW)
        stale, _ = ns.news_ignition([_doc("Nova wins FDA approval", 20.0)], now=NOW)
        assert fresh > stale > 0.0

    def test_window_excludes_old_docs(self):
        ign, comp = ns.news_ignition([_doc("Nova wins FDA approval", 30.0)], now=NOW)
        assert ign == 0.0 and comp["n_docs"] == 0

    def test_syndication_capped_not_multiplied(self):
        solo, _ = ns.news_ignition([_doc("Nova wins FDA approval", 3.0)], now=NOW)
        reprints = [
            _doc("Nova wins FDA approval", 3.0, source="Reuters"),
            _doc("Nova wins FDA approval for drug", 3.1, source="MarketWatch"),
            _doc("Nova granted FDA approval, shares poised to rally", 3.2, source="Benzinga"),
            _doc("Nova wins FDA approval", 3.4, source="GlobeNewswire"),
        ]
        synd, _ = ns.news_ignition(reprints, now=NOW)
        assert synd > solo                     # confirmation helps a little…
        assert synd <= solo * 1.35             # …but never multiplies the catalyst

    def test_two_catalyst_classes_stack(self):
        one, _ = ns.news_ignition([_doc("Nova wins FDA approval", 2.0)], now=NOW)
        two, comp = ns.news_ignition(
            [_doc("Nova wins FDA approval", 2.0),
             _doc("Nova beats estimates and raises guidance", 2.5)], now=NOW)
        assert two > one and len(comp["classes"]) == 2

    def test_social_and_halt_docs_ignored(self):
        docs = [_doc("Nova wins FDA approval $NOVA to the moon", 1.0, stype="social"),
                _halt_doc("NOVA", 1.0)]
        ign, comp = ns.news_ignition(docs, now=NOW)
        assert ign == 0.0 and comp["n_docs"] == 0

    def test_generic_bullish_small_and_capped(self):
        docs = [_doc(f"Nova shares climb on momentum {i}", 1.0, sent=0.9)
                for i in range(10)]
        ign, comp = ns.news_ignition(docs, now=NOW)
        assert 0.0 < ign <= ns._GENERIC_W / ns._IGNITION_SAT + 1e-9
        assert comp["classes"] == {}

    def test_bearish_wire_no_ignition(self):
        ign, _ = ns.news_ignition(
            [_doc("Nova misses estimates, announces restructuring", 1.0, sent=-0.7)],
            now=NOW)
        assert ign == 0.0

    def test_halflife_env_tunable(self, monkeypatch):
        monkeypatch.setenv("SQUEEZE_NEWS_HALFLIFE_H", "2")
        short_hl, comp = ns.news_ignition([_doc("Nova wins FDA approval", 8.0)], now=NOW)
        monkeypatch.setenv("SQUEEZE_NEWS_HALFLIFE_H", "12")
        long_hl, _ = ns.news_ignition([_doc("Nova wins FDA approval", 8.0)], now=NOW)
        assert long_hl > short_hl
        assert comp["halflife_h"] == 2.0

    def test_evidence_reported(self):
        _, comp = ns.news_ignition([_doc("Nova wins FDA approval", 3.0)], now=NOW)
        assert comp["evidence"][0]["event"] == "fda_approval"
        assert comp["evidence"][0]["source"] == "Reuters"


class TestFuelVeto:
    def test_fresh_offering_vetoes(self):
        v = ns.fuel_veto([_doc("Nova announces $50 million public offering", 5.0)], now=NOW)
        assert v is not None and v["reason"] == "dilutive_offering"

    def test_flat_memory_no_decay(self):
        # 30h old (well past the ignition half-life) must still hard-veto
        v = ns.fuel_veto([_doc("Nova prices public offering", 30.0)], now=NOW)
        assert v is not None

    def test_trading_day_boundary(self):
        # NOW = Thu Jul 2. Five trading days back: Jul 1, Jun 30, 29, 26, 25
        # -> cutoff = Jun 25 00:00 ET. Jun 25 14:00 UTC inside; Jun 24 outside.
        inside = _doc("Nova prices public offering", 0.0)
        inside["published_at"] = datetime(2026, 6, 25, 14, 0, tzinfo=timezone.utc)
        outside = _doc("Nova prices public offering", 0.0)
        outside["published_at"] = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc)
        assert ns.fuel_veto([inside], now=NOW) is not None
        assert ns.fuel_veto([outside], now=NOW) is None

    def test_most_recent_veto_wins(self):
        v = ns.fuel_veto(
            [_doc("Nova prices public offering", 40.0),
             _doc("Nova files for chapter 11 bankruptcy protection", 4.0)], now=NOW)
        assert v["reason"] == "chapter_11"

    def test_social_chatter_cannot_veto(self):
        v = ns.fuel_veto(
            [_doc("heard NOVA doing a public offering lol", 2.0, stype="social")], now=NOW)
        assert v is None

    def test_lookback_env_tunable(self, monkeypatch):
        doc = _doc("Nova prices public offering", 0.0)
        doc["published_at"] = datetime(2026, 6, 29, 14, 0, tzinfo=timezone.utc)  # 3 TD back
        monkeypatch.setenv("SQUEEZE_VETO_LOOKBACK_TD", "2")
        assert ns.fuel_veto([doc], now=NOW) is None      # 2-day memory forgot it
        monkeypatch.setenv("SQUEEZE_VETO_LOOKBACK_TD", "5")
        assert ns.fuel_veto([doc], now=NOW) is not None


class TestHaltStatus:
    def test_halt_parsed_from_feed_shape(self):
        h = ns.halt_status([_halt_doc("NOVA", 2.0)], "NOVA", now=NOW)
        assert h is not None and h["code"] == "T1" and h["resumed"] is False

    def test_bare_title_matches_without_ticker_tags(self):
        # halt items carry no ticker tags — bare-symbol title must match
        h = ns.halt_status([_halt_doc("NOVA", 2.0)], "nova", now=NOW)
        assert h is not None

    def test_resumption_detected(self):
        h = ns.halt_status([_halt_doc("NOVA", 2.0, resumed=True)], "NOVA", now=NOW)
        assert h["resumed"] is True

    def test_t12_not_misread_as_t1(self):
        h = ns.halt_status([_halt_doc("NOVA", 2.0, code="T12")], "NOVA", now=NOW)
        assert h["code"] == "T12"

    def test_stale_halt_ignored(self):
        # the halts RSS keeps months of rows — recency window must apply
        assert ns.halt_status([_halt_doc("NOVA", 30.0)], "NOVA", now=NOW) is None

    def test_other_symbol_ignored(self):
        assert ns.halt_status([_halt_doc("OTHR", 2.0)], "NOVA", now=NOW) is None

    def test_non_halt_source_ignored(self):
        d = _doc("NOVA", 2.0)  # bare-symbol title but a normal wire
        assert ns.halt_status([d], "NOVA", now=NOW) is None


class TestEvaluateTickerNews:
    def test_veto_zeroes_ignition_keeps_raw(self):
        docs = [_doc("Nova wins FDA approval", 2.0),
                _doc("Nova announces $50 million public offering", 6.0)]
        out = ns.evaluate_ticker_news(docs, "NOVA", now=NOW)
        assert out["veto"] is not None
        assert out["news_ignition"] == 0.0
        assert out["components"]["raw_ignition"] > 0.0   # visible, not silent

    def test_clean_read(self):
        out = ns.evaluate_ticker_news([_doc("Nova wins FDA approval", 2.0)], "NOVA", now=NOW)
        assert out["news_ignition"] > 0.5
        assert out["veto"] is None and out["halt"] is None and out["n_news"] == 1

    def test_halt_reported(self):
        out = ns.evaluate_ticker_news([_halt_doc("NOVA", 1.0)], "NOVA", now=NOW)
        assert out["halt"]["code"] == "T1"
        assert out["news_ignition"] == 0.0   # a halt row is not a bullish catalyst


class TestDatabaseSafety:
    """news_signal must be structurally unable to write to storage."""

    def test_no_storage_imports(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1] / "news_signal.py").read_text(
            encoding="utf-8")
        for name in ("storage_handlers", "motor", "redis", "pymongo"):
            assert f"import {name}" not in src, name
            assert f"from {name}" not in src, name

    def test_fetch_is_read_only(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1] / "news_signal.py").read_text(
            encoding="utf-8")
        for writer in ("insert_one", "insert_many", "update_one", "update_many",
                       "replace_one", "delete_one", "delete_many", "bulk_write"):
            assert writer not in src, writer
