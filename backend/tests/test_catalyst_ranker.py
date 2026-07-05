"""Unit tests for the catalyst ranker's pure scoring + feature logic."""

import asyncio

import catalyst_ranker as cr
from catalyst_ranker import CandidateFeatures


def _cand(**kw):
    base = dict(ticker="T", n_docs=3, n_stories=3, n_sources=3, source_types=["rss"],
                mean_sentiment=0.0, abnormal_attention=1.0, best_source_weight=1.0,
                credibility=1.0)
    base.update(kw)
    return CandidateFeatures(**base)


# --- size factor ----------------------------------------------------------- #

class TestSizeFactor:
    def test_unknown_is_neutral(self):
        assert cr._size_factor(None) == 1.0

    def test_mega_cap_downweighted(self):
        assert cr._size_factor(500e9) == 0.82

    def test_small_cap_favoured(self):
        assert cr._size_factor(1e9) == 1.10

    def test_micro_cap_not_rewarded(self):
        assert cr._size_factor(100e6) == 1.00

    def test_threshold_is_inclusive(self):
        assert cr._size_factor(200e9) == 0.82   # exactly the mega threshold
        assert cr._size_factor(2e9) == 1.00      # exactly the mid threshold


# --- pre-market confirmation factor ---------------------------------------- #

class TestConfirmationFactor:
    def test_no_gap_is_neutral(self):
        assert cr._confirmation_factor(None, 5.0) == 1.0

    def test_full_boost_and_saturation(self):
        assert cr._confirmation_factor(8.0, 2.0) == 1.20
        assert cr._confirmation_factor(-20.0, 9.0) == 1.20  # |gap| & vol saturate

    def test_partial(self):
        assert cr._confirmation_factor(4.0, 1.0) == 1.05

    def test_unknown_volume_gets_half_credit(self):
        assert cr._confirmation_factor(8.0, None) == 1.10

    def test_boost_only_floor(self):
        assert cr._confirmation_factor(0.0, 0.0) == 1.0
        assert cr._confirmation_factor(0.1, 0.0) == 1.0  # never below 1.0


# --- direction + credibility ----------------------------------------------- #

class TestDirection:
    def test_bullish(self):
        assert cr._direction_from_sentiment(0.15) == "bullish"

    def test_bearish(self):
        assert cr._direction_from_sentiment(-0.15) == "bearish"

    def test_neutral_band(self):
        assert cr._direction_from_sentiment(0.14) == "neutral"
        assert cr._direction_from_sentiment(0.0) == "neutral"


class TestCredibility:
    def test_known_wire(self):
        assert cr._credibility_for("Reuters Business") == 1.15

    def test_case_insensitive(self):
        assert cr._credibility_for("BLOOMBERG") == 1.15

    def test_unknown_is_one(self):
        assert cr._credibility_for("Some Random Blog") == 1.0


# --- clustering ------------------------------------------------------------ #

class TestClustering:
    def test_title_tokens_drop_stopwords(self):
        toks = cr._title_tokens("The Apple and Microsoft Deal")
        assert "apple" in toks and "microsoft" in toks
        assert "the" not in toks and "and" not in toks

    def test_same_story_dedups_identical(self):
        a = {"title": "Acme reports record quarterly earnings beat"}
        b = {"title": "Acme reports record quarterly earnings beat"}
        assert cr._same_story(a, b)

    def test_distinct_stories_not_merged(self):
        a = {"title": "Acme wins FDA approval for new cancer drug"}
        b = {"title": "Globex announces surprise CEO resignation"}
        assert not cr._same_story(a, b)

    def test_cluster_collapses_reprints(self):
        docs = [{"title": "Acme beats earnings expectations handily"},
                {"title": "Acme beats earnings expectations handily"},
                {"title": "Unrelated tuesday market wrap recap"}]
        assert len(cr._cluster(docs)) == 2


# --- feature engineering --------------------------------------------------- #

def test_build_candidates_aggregates_features():
    docs = [
        {"title": "Acme wins approval", "description": "", "tickers": ["ACME"],
         "source": "Reuters", "source_type": "rss", "sentiment": {"score": 0.5},
         "content_hash": "h1"},
        {"title": "Acme wins approval", "description": "", "tickers": ["ACME"],
         "source": "Bloomberg", "source_type": "rss", "sentiment": {"score": 0.3},
         "content_hash": "h2"},
    ]
    cands = cr.build_candidates(docs, baseline_daily={"ACME": 1.0})
    assert len(cands) == 1
    c = cands[0]
    assert c.ticker == "ACME"
    assert c.n_docs == 2
    assert c.n_stories == 1           # same headline -> one story
    assert c.n_sources == 2           # two independent outlets
    # magnitude-weighted mean: (0.5*0.5 + 0.3*0.3) / (0.5+0.3) = 0.425 —
    # same-direction docs land between, tilted toward conviction.
    assert abs(c.mean_sentiment - 0.425) < 1e-9
    assert abs(c.abnormal_attention - 2.0) < 1e-9   # 2 docs / baseline 1.0


# --- prominence (headline vs incidental mention) ---------------------------- #

class _TitleAwareExtractor:
    """Extracts ACME from anywhere, PEER only where it's actually written."""
    def extract(self, title, description):
        text = f"{title} {description}".lower()
        out = []
        if "acme" in text:
            out.append("ACME")
        if "peer" in text:
            out.append("PEER")
        return tuple(out)


class TestProminence:
    def test_factor_bounds(self):
        assert cr._prominence_factor(None) == 1.0    # unknown -> neutral
        assert cr._prominence_factor(0.0) == 0.7     # never in a headline
        assert cr._prominence_factor(1.0) == 1.0
        assert cr._prominence_factor(0.5) == 0.85

    def test_title_mention_share_computed(self):
        docs = [
            {"title": "Acme wins approval", "description": "PEER also trades",
             "source": "A", "source_type": "rss", "content_hash": "h1"},
            {"title": "Acme up big", "description": "watchers cite peer weakness",
             "source": "B", "source_type": "rss", "content_hash": "h2"},
        ]
        cands = {c.ticker: c for c in cr.build_candidates(
            docs, baseline_daily={}, ticker_extractor=_TitleAwareExtractor())}
        assert cands["ACME"].title_mention_share == 1.0   # headline subject
        assert cands["PEER"].title_mention_share == 0.0   # body-only mention

    def test_incidental_mention_scored_lower(self):
        subject = _cand(ticker="SUBJ", title_mention_share=1.0)
        incidental = _cand(ticker="INCD", title_mention_share=0.0)
        out = {c.ticker: c for c in cr.score_candidates([subject, incidental],
                                                        min_sources=2)}
        assert abs(out["INCD"].pre_score - out["SUBJ"].pre_score * 0.7) < 0.05
        assert out["INCD"].components["prominence_factor"] == 0.7
        assert out["SUBJ"].components["prominence_factor"] == 1.0

    def test_no_extractor_is_neutral(self):
        docs = [{"title": "Acme wins", "description": "", "tickers": ["ACME"],
                 "source": "A", "source_type": "rss", "content_hash": "h1"}]
        c = cr.build_candidates(docs, baseline_daily={})[0]
        assert c.title_mention_share is None
        scored = cr.score_candidates([_cand(ticker="X")], min_sources=2)[0]
        assert scored.components["prominence_factor"] == 1.0


# --- scoring --------------------------------------------------------------- #

class TestScoreCandidates:
    def test_volume_floor_filters_thin_names(self):
        out = cr.score_candidates([_cand(ticker="LOW", n_sources=1),
                                   _cand(ticker="OK", n_sources=2)], min_sources=2)
        assert [c.ticker for c in out] == ["OK"]

    def test_sorted_by_pre_score_desc(self):
        out = cr.score_candidates([_cand(ticker="A", n_stories=1),
                                   _cand(ticker="B", n_stories=6)], min_sources=2)
        assert out[0].ticker == "B"

    def test_premarket_boost_applied_and_recorded(self):
        base = cr.score_candidates([_cand(ticker="X", mean_sentiment=0.5)],
                                   min_sources=2)[0].pre_score
        boosted = cr.score_candidates(
            [_cand(ticker="X", mean_sentiment=0.5)], min_sources=2,
            premarket={"X": {"gap_pct": 9.0, "rel_volume": 3.0}})[0]
        assert boosted.confirmation_factor == 1.20
        assert abs(boosted.pre_score - round(base * 1.2, 2)) < 0.05
        assert "confirmation_factor" in boosted.components

    def test_size_factor_applied(self):
        c = cr.score_candidates([_cand(ticker="MEGA")], min_sources=2,
                                market_caps={"MEGA": 500e9})[0]
        assert c.size_factor == 0.82
        assert c.market_cap == 500e9

    def test_no_premarket_is_neutral(self):
        c = cr.score_candidates([_cand(ticker="X")], min_sources=2)[0]
        assert c.confirmation_factor == 1.0
        assert c.premarket is None


# --- grading --------------------------------------------------------------- #

def test_grade_ranking_separation_and_hit_rate():
    result = {"items": [
        {"ticker": "BIG", "rank": 1, "direction": "bullish"},
        {"ticker": "SMALL", "rank": 2, "direction": "bearish"},
    ]}
    prices = {"BIG": {"open": 100.0, "close": 110.0},    # +10%, bullish call -> hit
              "SMALL": {"open": 100.0, "close": 99.0}}     # -1%, bearish call -> hit
    m = cr.grade_ranking(result, prices)
    assert m["graded"] == 2
    assert m["reaction_separation"] > 0      # top-ranked moved more
    assert m["direction_hit_rate"] == 1.0


def test_grade_ranking_no_price_data():
    result = {"items": [{"ticker": "X", "rank": 1, "direction": "bullish"}]}
    assert cr.grade_ranking(result, {})["graded"] == 0


def test_grade_ranking_gap_inclusive_basis():
    # Overnight approval: +12% gap, then -2% intraday drift. The old
    # open->close basis graded this bullish call a MISS (-2%); the
    # prev_close basis captures the gap the catalyst expressed in.
    result = {"items": [{"ticker": "GAP", "rank": 1, "direction": "bullish"}]}
    prices = {"GAP": {"open": 112.0, "close": 109.76, "prev_close": 100.0}}
    m = cr.grade_ranking(result, prices)
    row = m["per_ticker"][0]
    assert m["entry_basis"] == "prev_close"
    assert row["entry_basis"] == "prev_close"
    assert abs(row["return"] - 0.0976) < 1e-9
    assert row["direction_hit"] is True            # gap made the call right
    assert abs(row["gap_return"] - 0.12) < 1e-9
    assert abs(row["drift_return"] - (-0.02)) < 1e-9


def test_grade_ranking_open_fallback_and_mixed_basis():
    result = {"items": [
        {"ticker": "A", "rank": 1, "direction": "bullish"},
        {"ticker": "B", "rank": 2, "direction": "bullish"},
    ]}
    prices = {"A": {"open": 100.0, "close": 105.0, "prev_close": 98.0},
              "B": {"open": 50.0, "close": 51.0, "prev_close": None}}  # day-one listing
    m = cr.grade_ranking(result, prices)
    assert m["entry_basis"] == "mixed"
    by = {r["ticker"]: r for r in m["per_ticker"]}
    assert by["A"]["entry_basis"] == "prev_close"
    assert by["B"]["entry_basis"] == "open"
    assert by["B"]["gap_return"] is None


# --- sentiment aggregation (audit fix: dilution) ----------------------------- #

def test_neutral_recaps_do_not_dilute_strong_catalyst():
    # The audit case: five neutral recaps + one -0.8 CRL. A plain mean
    # (-0.8/6 = -0.133) sat under the +/-0.15 direction floor -> "neutral"
    # on exactly the best-covered catalysts.
    docs = [
        {"title": f"Acme recap {i}", "description": "", "tickers": ["ACME"],
         "source": f"S{i}", "source_type": "rss", "sentiment": {"score": 0.0},
         "content_hash": f"n{i}"} for i in range(5)
    ] + [
        {"title": "FDA issues complete response letter for Acme", "description": "",
         "tickers": ["ACME"], "source": "Reuters", "source_type": "rss",
         "sentiment": {"score": -0.8}, "content_hash": "crl"},
    ]
    c = cr.build_candidates(docs, baseline_daily={"ACME": 1.0})[0]
    # floor-weighted: (0.8*-0.8) / (5*0.15 + 0.8) = -0.4129 — direction survives
    assert abs(c.mean_sentiment - (-0.4129)) < 1e-4
    assert cr._direction_from_sentiment(c.mean_sentiment) == "bearish"


def test_routine_tape_stays_under_the_floor():
    # A lone mild-positive item (dividend declaration ~+0.2) among quiet tape
    # must NOT get promoted to a direction call by the weighting.
    docs = [
        {"title": "Harbor declares quarterly dividend", "description": "",
         "tickers": ["HRBB"], "source": "PR Newswire", "source_type": "rss",
         "sentiment": {"score": 0.2}, "content_hash": "d1"},
        {"title": "Harbor to speak at banking forum", "description": "",
         "tickers": ["HRBB"], "source": "GlobeNewswire", "source_type": "rss",
         "sentiment": {"score": 0.0}, "content_hash": "d2"},
    ]
    c = cr.build_candidates(docs, baseline_daily={"HRBB": 1.0})[0]
    assert cr._direction_from_sentiment(c.mean_sentiment) == "neutral"


def test_opposing_strong_docs_still_cancel():
    docs = [
        {"title": "Acme surges on results", "description": "", "tickers": ["ACME"],
         "source": "A", "source_type": "rss", "sentiment": {"score": 0.8},
         "content_hash": "p"},
        {"title": "Acme faces major lawsuit", "description": "", "tickers": ["ACME"],
         "source": "B", "source_type": "rss", "sentiment": {"score": -0.8},
         "content_hash": "q"},
    ]
    c = cr.build_candidates(docs, baseline_daily={"ACME": 1.0})[0]
    assert abs(c.mean_sentiment) < 1e-9   # honest ambiguity stays neutral


class TestResolveMarketCaps:
    def test_finviz_preferred_yahoo_fills_rest(self, monkeypatch):
        async def fake_yahoo(tickers):
            return {t: 1.0e9 for t in tickers}   # Yahoo answers 1B for whatever it's asked
        monkeypatch.setattr(cr, "_fetch_market_caps_safe", fake_yahoo)
        # A has a Finviz cap; B's is None; C isn't in the pre-market result at all
        premarket = {"A": {"market_cap": 5.0e9}, "B": {"market_cap": None}}
        caps = asyncio.run(cr._resolve_market_caps(["A", "B", "C"], premarket))
        assert caps["A"] == 5.0e9   # Finviz preferred
        assert caps["B"] == 1.0e9   # Yahoo fallback (Finviz had None)
        assert caps["C"] == 1.0e9   # Yahoo fallback (not in pre-market)

    def test_all_yahoo_when_no_finviz(self, monkeypatch):
        called = {}
        async def fake_yahoo(tickers):
            called["tickers"] = list(tickers)
            return {t: 2.0e9 for t in tickers}
        monkeypatch.setattr(cr, "_fetch_market_caps_safe", fake_yahoo)
        caps = asyncio.run(cr._resolve_market_caps(["X", "Y"], {}))   # no Finviz data
        assert caps == {"X": 2.0e9, "Y": 2.0e9}
        assert called["tickers"] == ["X", "Y"]   # Yahoo asked for all of them


# --- catalyst profiles ----------------------------------------------------- #

class _RecordingCursor:
    def __init__(self, query):
        self.query = query
    def sort(self, *a, **k): return self
    def limit(self, *a, **k): return self
    async def to_list(self, length=None): return []


class _RecordingColl:
    """Captures the query each find() receives so we can assert source-type scope."""
    def __init__(self):
        self.queries = []
    def find(self, query, projection=None):
        self.queries.append(query)
        return _RecordingCursor(query)


class TestProfiles:
    def test_resolve_known(self):
        assert cr.resolve_profile("combined") == ("combined", ["rss", "sec", "fda"])
        assert cr.resolve_profile("regulatory") == ("regulatory", ["sec", "fda"])

    def test_resolve_unknown_falls_back_to_default(self):
        assert cr.resolve_profile("nope") == (cr.DEFAULT_PROFILE, ["rss", "sec", "fda"])
        assert cr.resolve_profile(None)[0] == cr.DEFAULT_PROFILE

    def test_window_query_scoped_to_source_types(self):
        from datetime import datetime, timezone
        coll = _RecordingColl()
        now = datetime(2026, 6, 30, tzinfo=timezone.utc)
        asyncio.run(cr._fetch_window_docs(coll, now, now, source_types=["sec", "fda"]))
        assert coll.queries[0]["source_type"] == {"$in": ["sec", "fda"]}

    def test_baseline_query_scoped_to_source_types(self):
        from datetime import datetime, timezone
        coll = _RecordingColl()
        now = datetime(2026, 6, 30, tzinfo=timezone.utc)
        asyncio.run(cr._compute_baseline(coll, now, source_types=["sec", "fda"]))
        assert coll.queries[0]["source_type"] == {"$in": ["sec", "fda"]}

    def test_window_defaults_to_all_three(self):
        from datetime import datetime, timezone
        coll = _RecordingColl()
        now = datetime(2026, 6, 30, tzinfo=timezone.utc)
        asyncio.run(cr._fetch_window_docs(coll, now, now))
        assert coll.queries[0]["source_type"] == {"$in": ["rss", "sec", "fda"]}
