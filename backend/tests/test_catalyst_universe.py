"""Unit tests for the candidate-universe accumulation + weight auto-tune logic.

Pure helpers are tested directly; the async orchestrators (`accumulate`,
`auto_tune`) run against a tiny in-memory fake collection so the suite stays
fully offline (no Mongo, no network), matching the existing catalyst tests.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import catalyst_universe as cu


# --------------------------------------------------------------------------- #
# Minimal async Mongo fake (only the surface accumulate/auto_tune use)
# --------------------------------------------------------------------------- #

class _Cursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def sort(self, key, direction=-1):
        self._docs.sort(key=lambda d: d.get(key) or 0, reverse=direction < 0)
        return self

    def limit(self, n):
        self._docs = self._docs[:n]
        return self

    async def to_list(self, length=None):
        return list(self._docs[: length if length is not None else len(self._docs)])


def _matches(doc, flt):
    for k, cond in (flt or {}).items():
        if k == "metrics.graded":
            graded = (doc.get("metrics") or {}).get("graded")
            if not (isinstance(graded, (int, float)) and graded > cond["$gt"]):
                return False
        elif isinstance(cond, dict) and ("$gte" in cond or "$lte" in cond):
            v = doc.get(k)
            if v is None:
                return False
            if "$gte" in cond and v < cond["$gte"]:
                return False
            if "$lte" in cond and v > cond["$lte"]:
                return False
        elif isinstance(cond, dict) and "$in" in cond:
            if doc.get(k) not in cond["$in"]:
                return False
        else:
            if doc.get(k) != cond:
                return False
    return True


class FakeCollection:
    def __init__(self, docs=None):
        # keyed by _id when present, else appended
        self._store = {}
        self._auto = 0
        for d in docs or []:
            self._put(d)

    def _put(self, doc):
        _id = doc.get("_id")
        if _id is None:
            self._auto += 1
            _id = f"_auto{self._auto}"
            doc = {**doc, "_id": _id}
        self._store[_id] = doc

    def find(self, flt=None, projection=None):
        return _Cursor([d for d in self._store.values() if _matches(d, flt or {})])

    async def find_one(self, flt, projection=None):
        for d in self._store.values():
            if _matches(d, flt):
                return dict(d)
        return None

    async def replace_one(self, flt, doc, upsert=False):
        self._store[doc["_id"]] = dict(doc)

    # test helper
    def get(self, _id):
        return self._store.get(_id)


def _doc(ticker, *, source, h, when, score=None):
    return {
        "content_hash": h,
        "source": source,
        "source_type": "rss",
        "title": ticker,            # TitleExtractor maps title -> ticker
        "description": f"{ticker} news",
        "url": f"https://x/{h}",
        "published_at": when,
        "tickers": [ticker],
        "sentiment": ({"score": score} if score is not None else None),
    }


class TitleExtractor:
    """Test extractor: the document title *is* the ticker (deterministic)."""

    def extract(self, title, description):
        return (title,) if title else ()


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #

class TestSelectNewDocs:
    def test_filters_seen_hashes(self):
        docs = [{"content_hash": "a"}, {"content_hash": "b"}, {"content_hash": "c"}]
        out = cu.select_new_docs(docs, {"a": "x", "c": "y"})
        assert [d["content_hash"] for d in out] == ["b"]

    def test_doc_without_hash_is_new(self):
        assert cu.select_new_docs([{"title": "x"}], {"a": "1"}) == [{"title": "x"}]

    def test_empty_seen_keeps_all(self):
        docs = [{"content_hash": "a"}, {"content_hash": "b"}]
        assert cu.select_new_docs(docs, {}) == docs


class TestBuildUniverseFeatures:
    def test_groups_and_counts(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        docs = [
            _doc("AAA", source="Reuters", h="1", when=now, score=0.4),
            _doc("AAA", source="Bloomberg", h="2", when=now, score=0.2),
            _doc("BBB", source="CNBC", h="3", when=now),
        ]
        feats = cu.build_universe_features(docs, ticker_extractor=TitleExtractor())
        assert feats["AAA"]["n_docs"] == 2
        assert feats["AAA"]["sources"] == ["Bloomberg", "Reuters"]  # sorted union
        assert feats["AAA"]["sent_n"] == 2
        assert round(feats["AAA"]["sent_sum"], 4) == 0.6
        assert feats["BBB"]["sent_n"] == 0             # no scored docs


class TestMergeCandidate:
    def test_accumulates_and_unions_sources(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        first = cu.merge_candidate(
            None,
            {"n_docs": 1, "n_stories": 1, "sources": ["Reuters"], "source_types": ["rss"],
             "sent_sum": 0.4, "sent_n": 1, "sample_articles": []},
            now=now,
        )
        assert first["n_sources"] == 1
        assert first["promoted"] is False              # 1 source < floor
        assert first["first_seen"] == now

        later = now + timedelta(hours=12)
        second = cu.merge_candidate(
            first,
            {"n_docs": 2, "n_stories": 2, "sources": ["Bloomberg"], "source_types": ["rss"],
             "sent_sum": 0.2, "sent_n": 1, "sample_articles": []},
            now=later,
        )
        assert second["n_docs"] == 3
        assert second["n_stories"] == 3
        assert second["n_sources"] == 2                # union Reuters+Bloomberg
        assert second["sent_n"] == 2
        assert round(second["mean_sentiment"], 2) == 0.30
        assert second["cycles"] == 2
        assert second["first_seen"] == now             # preserved
        assert second["last_seen"] == later
        assert second["promoted"] is True              # 2 sources & 3 stories


class TestBlendWeights:
    def test_nudges_toward_suggested_and_normalizes(self):
        current = {"attention": 0.30, "abnormal": 0.25, "sentiment": 0.25, "materiality": 0.20}
        suggested = {"attention": 0.70, "abnormal": 0.10, "sentiment": 0.10, "materiality": 0.10}
        out = cu.blend_weights(current, suggested, blend=0.2)
        assert abs(sum(out.values()) - 1.0) < 1e-6      # renormalized
        assert out["attention"] > current["attention"]  # moved toward suggestion
        assert out["attention"] < suggested["attention"]  # but only part-way (a nudge)


class TestPruneSeen:
    def test_drops_old_entries(self):
        now = datetime(2026, 1, 2, tzinfo=timezone.utc)
        old = (now - timedelta(hours=100)).isoformat()
        fresh = (now - timedelta(hours=1)).isoformat()
        pruned = cu.prune_seen({"old": old, "fresh": fresh}, now=now, retention_hours=72)
        assert "fresh" in pruned and "old" not in pruned


# --------------------------------------------------------------------------- #
# Async orchestrators
# --------------------------------------------------------------------------- #

class TestAccumulate:
    def test_only_new_data_no_double_count(self):
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        recent = now - timedelta(hours=1)
        news = FakeCollection([
            _doc("AAA", source="Reuters", h="h1", when=recent, score=0.5),
        ])
        universe = FakeCollection()
        meta = FakeCollection()

        # Cycle 1 — first run picks up the doc.
        s1 = asyncio.run(cu.accumulate(
            news, universe, meta, now=now, ticker_extractor=TitleExtractor()))
        assert s1["new_docs"] == 1
        assert universe.get("AAA")["n_docs"] == 1
        assert universe.get("AAA")["promoted"] is False  # single source

        # Cycle 2 — same doc, slightly later. Hash already seen → nothing new.
        s2 = asyncio.run(cu.accumulate(
            news, universe, meta, now=now + timedelta(hours=12),
            ticker_extractor=TitleExtractor()))
        assert s2["new_docs"] == 0
        assert universe.get("AAA")["n_docs"] == 1        # NOT doubled

    def test_promotion_on_second_independent_source(self):
        now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
        news = FakeCollection([
            _doc("AAA", source="Reuters", h="h1", when=now - timedelta(hours=1), score=0.5),
        ])
        universe = FakeCollection()
        meta = FakeCollection()
        asyncio.run(cu.accumulate(news, universe, meta, now=now,
                                  ticker_extractor=TitleExtractor()))
        assert universe.get("AAA")["promoted"] is False

        # A genuinely new doc (new hash) from a second source, next cycle.
        later = now + timedelta(hours=12)
        news._put(_doc("AAA", source="Bloomberg", h="h2",
                       when=later - timedelta(hours=1), score=0.3))
        asyncio.run(cu.accumulate(news, universe, meta, now=later,
                                  ticker_extractor=TitleExtractor()))
        doc = universe.get("AAA")
        assert doc["n_sources"] == 2
        assert doc["promoted"] is True                   # crossed the floor


def _graded_run(i):
    return {
        "run_id": f"r{i}",
        "generated_at": i,  # sortable
        "items": [
            {"ticker": "AAA", "components": {
                "attention": 0.8, "abnormal": 0.5, "sentiment": 0.3, "materiality": 0.6,
                "credibility_factor": 1.0, "size_factor": 1.0, "confirmation_factor": 1.0}},
            {"ticker": "BBB", "components": {
                "attention": 0.2, "abnormal": 0.1, "sentiment": 0.1, "materiality": 0.2,
                "credibility_factor": 1.0, "size_factor": 1.0, "confirmation_factor": 1.0}},
        ],
        "metrics": {"graded": 2, "per_ticker": [
            {"ticker": "AAA", "abs_move": 0.08, "rank": 1, "direction_hit": True},
            {"ticker": "BBB", "abs_move": 0.01, "rank": 2, "direction_hit": False},
        ]},
    }


class TestAutoTune:
    def test_noop_below_min_graded(self):
        rankings = FakeCollection([_graded_run(i) for i in range(5)])
        meta = FakeCollection()
        result = asyncio.run(cu.auto_tune(rankings, meta, min_graded=10))
        assert result["tuned"] is False
        assert meta.get("weights") is None

    def test_tunes_above_min_graded(self):
        rankings = FakeCollection([_graded_run(i) for i in range(12)])
        meta = FakeCollection()
        result = asyncio.run(cu.auto_tune(rankings, meta, min_graded=10, blend=0.2))
        assert result["tuned"] is True
        weights = meta.get("weights")["weights"]
        assert abs(sum(weights.values()) - 1.0) < 1e-6
        assert set(weights) == set(cu.DEFAULT_WEIGHTS)
