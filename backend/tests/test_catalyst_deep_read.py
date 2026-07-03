"""Unit tests for the per-cluster deep-read stage. Fully offline: no API key,
no network, no Redis — the LLM and stores are faked where needed."""

import asyncio
import json
import sys
import time
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import catalyst_deep_read as dr
import catalyst_ranker as cr


def _feat(ticker, **kw):
    base = dict(ticker=ticker, pre_score=62.0, abnormal_attention=2.0,
                best_source_weight=1.0, premarket=None, size_factor=1.0)
    base.update(kw)
    return SimpleNamespace(**base)


def _doc(title, *, tickers=(), source="BusinessWire", source_type="rss",
         description="", sentiment=None, content_hash=None, extra=None):
    return {
        "title": title, "description": description, "tickers": list(tickers),
        "source": source, "source_type": source_type,
        "sentiment": sentiment, "content_hash": content_hash or f"h-{title[:24]}",
        "extra": extra or {},
    }


_GRADE = {
    "is_material": True, "event_type": "merger_acquisition", "subtype": "all-cash",
    "driver": "BigCo acquires Target2 at $58 cash.", "primary_ticker": "TGT2",
    "direction": "bullish", "magnitude": 0.9, "confidence": 0.94,
    "is_rumor": False, "is_forward_looking": False, "is_priced_in": False,
    "event_date": None, "deal_value_usd": 9.2e9, "premium_pct": 42.0,
    "affected_tickers": [
        {"ticker": "TGT2", "role": "target", "direction": "bullish"},
        {"ticker": "BIGCO", "role": "acquirer", "direction": "ambiguous"},
    ],
    "additional_catalysts": [], "rationale": "Definitive takeover at a 42% premium.",
}


# --- story-cluster construction ---------------------------------------------- #

class TestBuildStoryClusters:
    def test_two_sided_story_is_one_cluster_with_both_candidates(self):
        docs = [
            _doc("BigCo to acquire Target2 for $58 per share in cash",
                 tickers=("BIGCO", "TGT2"), source="PRNewswire"),
            _doc("BigCo to acquire Target2 for $58 per share in cash",
                 tickers=("BIGCO", "TGT2"), source="Reuters", content_hash="h-2"),
        ]
        clusters = dr.build_story_clusters(docs, {"BIGCO"})
        assert len(clusters) == 1
        assert clusters[0]["candidates"] == ["BIGCO", "TGT2"]
        assert len(clusters[0]["members"]) == 2

    def test_docs_without_shortlist_ticker_excluded(self):
        docs = [_doc("Some other company update entirely", tickers=("OTHR",))]
        assert dr.build_story_clusters(docs, {"BIGCO"}) == []

    def test_cluster_id_stable_and_content_derived(self):
        docs = [_doc("Acme wins massive defense contract award", tickers=("ACME",))]
        a = dr.build_story_clusters(docs, {"ACME"})[0]["cluster_id"]
        b = dr.build_story_clusters(list(reversed(docs)), {"ACME"})[0]["cluster_id"]
        assert a == b and a.startswith("c-")

    def test_extractor_is_authoritative_when_given(self):
        class Ext:
            def extract(self, title, description):
                return ("ACME",) if "acme" in title.lower() else ()
        docs = [_doc("Acme surges on approval", tickers=())]  # no stored tickers
        clusters = dr.build_story_clusters(docs, {"ACME"}, ticker_extractor=Ext())
        assert clusters and clusters[0]["candidates"] == ["ACME"]


# --- input rendering ---------------------------------------------------------- #

class TestRenderClusterInput:
    def _cluster_of(self, docs):
        return {"cluster_id": "c-test123456", "members": docs,
                "candidates": sorted({t for d in docs for t in d["tickers"]})}

    def test_prescore_sentiment_and_candidate_lines(self):
        docs = [_doc("Acme prices upsized offering", tickers=("ACME",),
                     sentiment={"score": 0.55})]
        text = dr.render_cluster_input(
            self._cluster_of(docs), profile="combined",
            features_by_ticker={"ACME": _feat("ACME", pre_score=62.0,
                                              abnormal_attention=2.0)},
            name_map={"ACME": "Acme Corp"},
        )
        assert "PROFILE: combined" in text
        assert "pre_score=0.62" in text
        assert "abnormal_attention=med" in text
        assert "source_weight=0.62" in text          # rss 1.0 / max 1.6 (half-even)
        assert "pre_market_confirmation=none" in text
        assert "SENTIMENT: score=0.55 label=positive" in text
        assert "CANDIDATES: ACME — Acme Corp" in text
        assert "CLUSTER c-test123456 (1 report)" in text
        assert "[1] source=businesswire type=newswire" in text
        assert "    HEADLINE: Acme prices upsized offering" in text

    def test_sec_member_carries_form_items_and_cik(self):
        docs = [_doc("8-K - MNOP CORP (0000111222) (Filer)",
                     tickers=("MNOP",), source="SEC EDGAR — 8-K", source_type="sec",
                     description="Item 5.02: Departure of Directors or Certain Officers",
                     extra={"filing_type": "8-K"})]
        text = dr.render_cluster_input(
            self._cluster_of(docs), profile="regulatory",
            features_by_ticker={"MNOP": _feat("MNOP", best_source_weight=1.6)},
        )
        assert "source=sec_edgar type=sec form=8-K" in text
        assert 'items=["5.02"]' in text
        assert "cik=0000111222" in text
        assert "source_weight=1.00" in text

    def test_gap_up_confirmation_and_body_truncation(self):
        docs = [_doc("Acme gaps on news", tickers=("ACME",), description="x" * 900)]
        text = dr.render_cluster_input(
            self._cluster_of(docs), profile="combined",
            features_by_ticker={"ACME": _feat(
                "ACME", premarket={"gap_pct": 6.0, "rel_volume": 2.0})},
        )
        assert "pre_market_confirmation=gap_up" in text
        body_line = next(l for l in text.splitlines() if l.startswith("    BODY:"))
        assert len(body_line) <= len("    BODY: ") + 500

    def test_member_cap_prefers_distinct_sources(self):
        docs = [_doc("Same story headline about acme corp", tickers=("ACME",),
                     source=f"Outlet {i % 3}", content_hash=f"h{i}") for i in range(9)]
        text = dr.render_cluster_input(
            self._cluster_of(docs), profile="combined",
            features_by_ticker={"ACME": _feat("ACME")},
        )
        assert "(9 reports)" in text
        assert "[5]" in text and "[6]" not in text   # capped at 5 members
        assert "source=outlet_0" in text and "source=outlet_2" in text


# --- output parsing ------------------------------------------------------------ #

class TestParseGrade:
    def test_clean_json(self):
        assert dr.parse_grade(json.dumps(_GRADE))["event_type"] == "merger_acquisition"

    def test_fenced_json(self):
        assert dr.parse_grade("```json\n" + json.dumps(_GRADE) + "\n```") is not None

    def test_prose_wrapped_json(self):
        text = "Here is the grade:\n" + json.dumps(_GRADE) + "\nHope that helps!"
        assert dr.parse_grade(text)["primary_ticker"] == "TGT2"

    def test_garbage_returns_none(self):
        assert dr.parse_grade("no json here") is None
        assert dr.parse_grade("") is None
        assert dr.parse_grade('["a","list"]') is None


# --- validation ----------------------------------------------------------------- #

class TestValidateGrade:
    def test_invented_primary_ticker_nulled(self):
        g = dr.validate_grade({**_GRADE, "primary_ticker": "FAKE"}, ["TGT2", "BIGCO"])
        assert g["primary_ticker"] is None

    def test_unknown_event_type_becomes_other(self):
        g = dr.validate_grade({**_GRADE, "event_type": "weird_event", "subtype": ""},
                              ["TGT2", "BIGCO"])
        assert g["event_type"] == "other"
        assert g["subtype"] == "weird_event"     # original preserved for debugging

    def test_numbers_clamped(self):
        g = dr.validate_grade({**_GRADE, "magnitude": 3.7, "confidence": -1}, ["TGT2"])
        assert g["magnitude"] == 1.0 and g["confidence"] == 0.0

    def test_affected_filtered_to_candidates(self):
        g = dr.validate_grade(_GRADE, ["TGT2"])   # BIGCO not a candidate
        assert [e["ticker"] for e in g["affected_tickers"]] == ["TGT2"]

    def test_subject_entry_synthesised_when_affected_missing(self):
        g = dr.validate_grade({**_GRADE, "affected_tickers": []}, ["TGT2"])
        assert g["affected_tickers"] == [
            {"ticker": "TGT2", "role": "subject", "direction": "bullish"}]

    def test_additional_catalysts_validated_and_capped(self):
        extra = {**_GRADE, "additional_catalysts": []}
        g = dr.validate_grade({**_GRADE, "additional_catalysts": [extra] * 5},
                              ["TGT2", "BIGCO"])
        assert len(g["additional_catalysts"]) == 3
        assert g["additional_catalysts"][0]["additional_catalysts"] == []  # depth 1

    def test_non_dict_is_none(self):
        assert dr.validate_grade(None, []) is None
        assert dr.validate_grade("{}", []) is None


# --- effective score -------------------------------------------------------------- #

class TestEffectiveScore:
    def _g(self, **kw):
        g = dr.validate_grade(dict(_GRADE), ["TGT2", "BIGCO"])
        g.update(kw)
        return g

    def test_immaterial_is_zero(self):
        assert dr.effective_score(self._g(is_material=False)) == 0.0

    def test_base_is_magnitude_times_confidence(self):
        assert dr.effective_score(self._g()) == round(100 * 0.9 * 0.94, 2)

    def test_discounts_stack(self):
        eff = dr.effective_score(self._g(is_rumor=True, is_priced_in=True,
                                         is_forward_looking=True))
        assert eff == round(100 * 0.9 * 0.94 * 0.75 * 0.5 * 0.85, 2)

    def test_role_and_size_scaling(self):
        assert dr.effective_score(self._g(), role="acquirer") < dr.effective_score(self._g())
        assert dr.effective_score(self._g(), size_factor=0.82) < dr.effective_score(self._g())


# --- merge into items ----------------------------------------------------------------- #

class TestApplyGradesToItems:
    def _items(self):
        return [
            {"ticker": "TGT2", "pre_score": 70.0, "catalyst_score": 70.0,
             "direction": "neutral", "confidence": 0.4, "rationale": "quant",
             "size_factor": 1.0},
            {"ticker": "BIGCO", "pre_score": 50.0, "catalyst_score": 50.0,
             "direction": "neutral", "confidence": 0.4, "rationale": "quant",
             "size_factor": 0.82},
            {"ticker": "UNTOUCHED", "pre_score": 40.0, "catalyst_score": 40.0,
             "direction": "bullish", "confidence": 0.4, "rationale": "quant",
             "size_factor": 1.0},
        ]

    def _graded(self, grade):
        return [{"cluster_id": "c-1", "grade": dr.validate_grade(grade, ["TGT2", "BIGCO"])}]

    def test_two_sided_merge_directions_and_roles(self):
        kept, dropped = dr.apply_grades_to_items(self._items(), self._graded(_GRADE))
        by = {i["ticker"]: i for i in kept}
        assert dropped == []
        assert by["TGT2"]["direction"] == "bullish"
        assert by["TGT2"]["deep_read"]["role"] == "target"
        assert by["BIGCO"]["direction"] == "neutral"          # ambiguous -> neutral
        assert by["BIGCO"]["deep_read"]["role"] == "acquirer"
        assert by["TGT2"]["catalyst_score"] > by["BIGCO"]["catalyst_score"]
        assert by["UNTOUCHED"]["rationale"] == "quant"        # no grade touched it

    def test_immaterial_grade_drops_item(self):
        immaterial = {**_GRADE, "is_material": False,
                      "affected_tickers": [{"ticker": "TGT2", "role": "subject",
                                            "direction": "ambiguous"}]}
        kept, dropped = dr.apply_grades_to_items(self._items(), self._graded(immaterial))
        assert "TGT2" not in {i["ticker"] for i in kept}
        assert dropped[0]["ticker"] == "TGT2"
        assert dropped[0]["cluster_id"] == "c-1"

    def test_ungraded_cluster_entries_ignored(self):
        kept, dropped = dr.apply_grades_to_items(
            self._items(), [{"cluster_id": "c-err", "grade": None}])
        assert len(kept) == 3 and dropped == []


# --- degradation + cache keys ------------------------------------------------------------ #

class TestDeepReadDegradation:
    def test_no_api_key_returns_status(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        docs = [_doc("Acme wins approval today", tickers=("ACME",)),
                _doc("Acme wins approval today", tickers=("ACME",),
                     source="Reuters", content_hash="h-2")]
        out = asyncio.run(dr.deep_read(
            docs, [_feat("ACME")], profile="combined", name_map={}))
        assert out["grades"] == []
        assert "ANTHROPIC_API_KEY" in out["status"]
        assert out["clusters_considered"] == 1

    def test_no_clusters_returns_status(self):
        out = asyncio.run(dr.deep_read([], [_feat("ACME")], profile="combined",
                                       name_map={}))
        assert out["grades"] == [] and "no story clusters" in out["status"]

    def test_cache_key_scoped_by_model(self):
        assert dr._cache_key("m1", "c-abc") != dr._cache_key("m2", "c-abc")


# --- offline end-to-end through rank_catalysts (cluster mode, fake LLM) ------------------- #

class _Cursor:
    def __init__(self, docs):
        self._docs = docs
    def sort(self, *a, **k): return self
    def limit(self, *a, **k): return self
    async def to_list(self, length=None): return self._docs


class _FakeNewsColl:
    """Window query gets the docs; the trailing-baseline query gets nothing."""
    def __init__(self, docs):
        self._docs = docs
    def find(self, query, projection=None):
        is_window = "$lte" in query.get("published_at", {})
        return _Cursor(self._docs if is_window else [])


class _Ext:
    def extract(self, title, description):
        return ("ACME",) if "acme" in f"{title} {description}".lower() else ()


def _fake_anthropic(payload: str):
    class _Messages:
        async def create(self, **kw):
            _fake_anthropic.last_request = kw
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=payload)])
    mod = types.ModuleType("anthropic")
    mod.AsyncAnthropic = lambda api_key=None: SimpleNamespace(messages=_Messages())
    return mod


def _window_docs():
    ts = datetime(2026, 6, 30, 10, 0, tzinfo=timezone.utc)
    return [
        {**_doc("Acme wins FDA approval for lead drug", tickers=("ACME",),
                source="BusinessWire", sentiment={"score": 0.4}), "published_at": ts},
        {**_doc("Acme wins FDA approval for lead drug", tickers=("ACME",),
                source="Reuters", sentiment={"score": 0.3}, content_hash="h-2"),
         "published_at": ts},
    ]


def _rank(monkeypatch, *, use_llm, payload=None):
    async def _empty(*a, **k):
        return {}
    monkeypatch.setattr(cr, "_fetch_premarket_safe", _empty)
    monkeypatch.setattr(cr, "_fetch_market_caps_safe", _empty)
    monkeypatch.delenv("REDIS_URI", raising=False)
    if payload is not None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setitem(sys.modules, "anthropic", _fake_anthropic(payload))
        import edgar_tickers
        edgar_tickers._cache.update({
            "fetched_at": time.monotonic(), "cik_map": {1: "ACME"},
            "name_map": {"ACME": "Acme Corp"},
        })
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return asyncio.run(cr.rank_catalysts(
        _FakeNewsColl(_window_docs()), use_llm=use_llm, ticker_extractor=_Ext(),
        now=datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
    ))


class TestRankCatalystsClusterMode:
    def test_quantitative_run_unchanged(self, monkeypatch):
        result = _rank(monkeypatch, use_llm=False)
        assert result["used_llm"] is False
        assert result["deep_read"] is None
        item = result["items"][0]
        assert item["ticker"] == "ACME"
        assert item["catalyst_score"] == item["pre_score"]
        assert "use_llm=false" in result["llm_status"]

    def test_cluster_grade_overlays_item(self, monkeypatch):
        grade = {**_GRADE, "event_type": "fda_approval", "subtype": "approval",
                 "primary_ticker": "ACME", "direction": "bullish",
                 "magnitude": 0.85, "confidence": 0.9,
                 "affected_tickers": [{"ticker": "ACME", "role": "subject",
                                       "direction": "bullish"}]}
        result = _rank(monkeypatch, use_llm=True, payload=json.dumps(grade))
        assert result["used_llm"] is True
        assert result["llm_status"] is None
        assert result["deep_read"]["graded"] == 1
        item = result["items"][0]
        assert item["deep_read"]["event_type"] == "fda_approval"
        assert item["direction"] == "bullish"
        assert item["catalyst_score"] == round(100 * 0.85 * 0.9, 2)
        # The rendered input reached the fake API with the right shape
        req = _fake_anthropic.last_request
        assert "CANDIDATES: ACME — Acme Corp" in req["messages"][0]["content"]
        assert req["system"].startswith("You are the deep-read stage")

    def test_immaterial_grade_drops_from_ranking(self, monkeypatch):
        grade = {**_GRADE, "is_material": False, "primary_ticker": "ACME",
                 "affected_tickers": [{"ticker": "ACME", "role": "subject",
                                       "direction": "ambiguous"}]}
        result = _rank(monkeypatch, use_llm=True, payload=json.dumps(grade))
        assert result["items"] == []
        assert result["deep_read"]["dropped_items"][0]["ticker"] == "ACME"

    def test_unparseable_output_falls_back_to_quantitative(self, monkeypatch):
        result = _rank(monkeypatch, use_llm=True, payload="I cannot comply.")
        assert result["used_llm"] is False
        assert result["items"][0]["catalyst_score"] == result["items"][0]["pre_score"]
        assert result["llm_status"] == "unparseable model output"
