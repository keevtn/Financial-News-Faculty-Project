"""Unit tests for squeeze_stream — fake Redis publisher, no network."""

import asyncio
import json
from datetime import datetime, timezone

import squeeze_stream as st


class FakeRedis:
    def __init__(self):
        self.published = []   # (channel, payload)

    async def publish(self, channel, payload):
        self.published.append((channel, payload))
        return 1


class BrokenRedis:
    async def publish(self, channel, payload):
        raise ConnectionError("redis down")


class TestRunSummaryEvent:
    _RESULT = {
        "run_id": "abc123",
        "generated_at": datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc),
        "fueled_count": 12,
        "universe_count": 40,
        "items": [
            {"ticker": "NOVA", "rank": 1, "squeeze_score": 71.2,
             "ignition_score": 0.61, "news_ignition": 0.55, "direction": "bullish",
             "thesis_broken": False, "halted": None},
            {"ticker": "BADD", "rank": 2, "squeeze_score": 23.9,
             "ignition_score": 0.0, "news_ignition": 0.0, "direction": "neutral",
             "thesis_broken": True,
             "halted": {"code": "T1", "resumed": False}},
        ],
    }

    def test_compact_shape(self):
        ev = st.run_summary_event(self._RESULT)
        assert ev["type"] == "squeeze_run"
        assert ev["run_id"] == "abc123"
        assert ev["generated_at"].startswith("2026-07-02T18:00")
        assert len(ev["top"]) == 2

    def test_flags_ride_along(self):
        ev = st.run_summary_event(self._RESULT)
        nova, badd = ev["top"]
        assert nova["thesis_broken"] is False and nova["halt_code"] is None
        assert badd["thesis_broken"] is True and badd["halt_code"] == "T1"

    def test_top_slice_capped(self):
        result = dict(self._RESULT)
        result["items"] = [{"ticker": f"T{i}", "rank": i} for i in range(30)]
        ev = st.run_summary_event(result)
        assert len(ev["top"]) == st._TOP_SLICE

    def test_json_serializable(self):
        json.dumps(st.run_summary_event(self._RESULT))   # must not raise


class TestDocEvent:
    def test_tagged_item(self):
        ev = st.doc_event(("nova", " abc "), "Reuters", "rss",
                          "Nova wins FDA approval",
                          datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc))
        assert ev["type"] == "doc"
        assert ev["tickers"] == ["NOVA", "ABC"]
        assert ev["published_at"].startswith("2026-07-02T15:00")

    def test_untagged_item_is_none(self):
        assert st.doc_event((), "Reuters", "rss", "Markets wrap") is None

    def test_title_capped(self):
        ev = st.doc_event(("NOVA",), "Reuters", "rss", "x" * 500)
        assert len(ev["title"]) == st._TITLE_CAP


class TestPublish:
    def test_publish_squeeze_run_hits_channel(self):
        fake = FakeRedis()
        pub = st.StreamPublisher(client=fake)
        ok = asyncio.run(st.publish_squeeze_run(
            {"run_id": "r1", "items": []}, publisher=pub))
        assert ok is True
        channel, payload = fake.published[0]
        assert channel == st.CHANNEL
        assert json.loads(payload)["type"] == "squeeze_run"

    def test_broken_redis_returns_false_never_raises(self):
        pub = st.StreamPublisher(client=BrokenRedis())
        ok = asyncio.run(st.publish_squeeze_run({"run_id": "r1", "items": []},
                                                publisher=pub))
        assert ok is False
        assert pub._down_until > 0        # backoff armed

    def test_no_uri_is_silent_noop(self, monkeypatch):
        monkeypatch.delenv("REDIS_URI", raising=False)
        pub = st.StreamPublisher()
        ok = asyncio.run(pub.publish({"type": "doc"}))
        assert ok is False


class _Item:
    """Duck-typed NewsItem stand-in (what the dispatcher hands handlers)."""

    def __init__(self, tickers=("NOVA",), title="Nova wins FDA approval"):
        self.tickers = tickers
        self.source = "Reuters"
        self.source_type = "rss"
        self.title = title
        self.published_at = datetime(2026, 7, 2, 15, 0, tzinfo=timezone.utc)


class TestIngestionHandler:
    def test_publishes_tagged_items(self):
        fake = FakeRedis()
        h = st.IngestionStreamHandler(publisher=st.StreamPublisher(client=fake))
        asyncio.run(h(_Item()))
        assert len(fake.published) == 1
        assert json.loads(fake.published[0][1])["tickers"] == ["NOVA"]

    def test_skips_untagged_items(self):
        fake = FakeRedis()
        h = st.IngestionStreamHandler(publisher=st.StreamPublisher(client=fake))
        asyncio.run(h(_Item(tickers=())))
        assert fake.published == []

    def test_disabled_handler_is_inert(self):
        fake = FakeRedis()
        h = st.IngestionStreamHandler(publisher=st.StreamPublisher(client=fake),
                                      enabled=False)
        asyncio.run(h(_Item()))
        assert fake.published == []

    def test_broken_redis_never_disturbs_ingestion(self):
        h = st.IngestionStreamHandler(publisher=st.StreamPublisher(client=BrokenRedis()))
        asyncio.run(h(_Item()))          # must not raise
        asyncio.run(h.close())           # lifecycle contract


class TestDatabaseSafety:
    def test_stream_module_never_persists(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[1] / "squeeze_stream.py").read_text(
            encoding="utf-8")
        for writer in ("insert_one", "insert_many", "update_one", "update_many",
                       "replace_one", "delete_one", "delete_many", "bulk_write",
                       ".set(", "setex", "hset"):
            assert writer not in src, writer
