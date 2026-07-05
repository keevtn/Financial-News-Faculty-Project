"""Unit tests for the forward catalyst calendar (pure logic + faked Mongo)."""

import asyncio
from datetime import datetime, timezone

import catalyst_calendar as cc


def _grade(**kw):
    base = {
        "is_material": True, "event_type": "pdufa_date", "subtype": "PDUFA",
        "driver": "FDA decision due.", "primary_ticker": "ACME",
        "direction": "ambiguous", "magnitude": 0.7, "confidence": 0.8,
        "is_rumor": False, "is_forward_looking": True, "is_priced_in": False,
        "event_date": "2026-09-15", "deal_value_usd": None, "premium_pct": None,
        "affected_tickers": [{"ticker": "ACME", "role": "subject",
                              "direction": "ambiguous"}],
        "additional_catalysts": [], "rationale": "scheduled decision",
    }
    base.update(kw)
    return base


def _run(grades):
    return {
        "run_id": "r1",
        "generated_at": datetime(2026, 6, 30, 12, 0, tzinfo=timezone.utc),
        "deep_read": {"grades": [{"cluster_id": f"c-{i}", "grade": g}
                                 for i, g in enumerate(grades)]},
    }


class TestExtractEntries:
    def test_forward_looking_grade_becomes_entry(self):
        entries = cc.extract_entries(_run([_grade()]))
        assert len(entries) == 1
        e = entries[0]
        assert e["_id"] == "ACME:pdufa_date:2026-09-15"
        assert e["ticker"] == "ACME"
        assert e["event_date"] == "2026-09-15"

    def test_non_forward_grade_skipped(self):
        entries = cc.extract_entries(_run([_grade(is_forward_looking=False)]))
        assert entries == []

    def test_unparseable_date_skipped(self):
        assert cc.extract_entries(_run([_grade(event_date="Q3 2026")])) == []
        assert cc.extract_entries(_run([_grade(event_date=None)])) == []

    def test_datetime_event_date_normalised(self):
        dt = datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc)
        entries = cc.extract_entries(_run([_grade(event_date=dt)]))
        assert entries[0]["event_date"] == "2026-09-15"

    def test_ipo_projects_lockup_windows(self):
        ipo = _grade(event_type="ipo", subtype="priced",
                     is_forward_looking=False, event_date=None)
        entries = cc.extract_entries(_run([ipo]))
        dates = sorted(e["event_date"] for e in entries)
        assert [e["event_type"] for e in entries] == ["lockup_expiry"] * 2
        assert dates == ["2026-09-28", "2026-12-27"]   # +90d and +180d
        assert all("projected" in e["subtype"] for e in entries)

    def test_immaterial_ipo_no_lockups(self):
        ipo = _grade(event_type="ipo", is_material=False,
                     is_forward_looking=False, event_date=None)
        assert cc.extract_entries(_run([ipo])) == []

    def test_private_subject_skipped(self):
        assert cc.extract_entries(_run([_grade(primary_ticker=None)])) == []

    def test_duplicate_entries_collapse(self):
        entries = cc.extract_entries(_run([_grade(), _grade()]))
        assert len(entries) == 1

    def test_additional_catalysts_included(self):
        extra = _grade(event_type="adcom", event_date="2026-08-01")
        entries = cc.extract_entries(
            _run([_grade(additional_catalysts=[extra])]))
        assert {e["event_type"] for e in entries} == {"pdufa_date", "adcom"}

    def test_no_deep_read_yields_nothing(self):
        assert cc.extract_entries({"run_id": "r", "deep_read": None}) == []


class _FakeCalendarColl:
    def __init__(self, docs=None, fail=False):
        self.docs = docs or []
        self.fail = fail
        self.upserts = []
        self.deletes = []

    async def update_one(self, flt, update, upsert=False):
        if self.fail:
            raise RuntimeError("store down")
        self.upserts.append((flt, update))

    async def delete_many(self, flt):
        self.deletes.append(flt)

    def find(self, query, projection=None):
        if self.fail:
            raise RuntimeError("store down")
        coll = self

        class _Cur:
            def limit(self, n): return self
            async def to_list(self, length=None): return coll.docs
        return _Cur()


class TestRecordAndLookup:
    def test_record_upserts_and_prunes(self):
        coll = _FakeCalendarColl()
        n = asyncio.run(cc.record_run(coll, _run([_grade()])))
        assert n == 1
        flt, update = coll.upserts[0]
        assert flt == {"_id": "ACME:pdufa_date:2026-09-15"}
        assert update["$set"]["event_date"] == "2026-09-15"
        assert "first_seen" in update["$setOnInsert"]
        assert coll.deletes and "$lt" in coll.deletes[0]["event_date"]

    def test_record_never_raises_on_store_failure(self):
        n = asyncio.run(cc.record_run(_FakeCalendarColl(fail=True), _run([_grade()])))
        assert n == 0

    def test_lookup_groups_by_ticker(self):
        coll = _FakeCalendarColl(docs=[
            {"ticker": "ACME", "event_type": "earnings", "event_date": "2026-06-30"},
            {"ticker": "ACME", "event_type": "pdufa_date", "event_date": "2026-06-30"},
            {"ticker": "OTHR", "event_type": "lockup_expiry", "event_date": "2026-07-01"},
        ])
        out = asyncio.run(cc.lookup_scheduled(
            coll, ["ACME", "OTHR"],
            start=datetime(2026, 6, 30, tzinfo=timezone.utc),
            end=datetime(2026, 7, 1, tzinfo=timezone.utc)))
        assert len(out["ACME"]) == 2 and len(out["OTHR"]) == 1

    def test_lookup_failure_returns_empty(self):
        out = asyncio.run(cc.lookup_scheduled(
            _FakeCalendarColl(fail=True), ["ACME"],
            start=datetime(2026, 6, 30, tzinfo=timezone.utc),
            end=datetime(2026, 7, 1, tzinfo=timezone.utc)))
        assert out == {}

    def test_lookup_no_tickers_short_circuits(self):
        out = asyncio.run(cc.lookup_scheduled(
            _FakeCalendarColl(fail=True), [],
            start=datetime(2026, 6, 30, tzinfo=timezone.utc),
            end=datetime(2026, 7, 1, tzinfo=timezone.utc)))
        assert out == {}
