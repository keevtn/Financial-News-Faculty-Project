"""Unit tests for squeeze_cache — fake Redis client, no network.

The invariant under test everywhere: the cache may only ever change *when* a
source is called, never *what* the pipeline answers (beyond declared TTL
staleness) — and any cache failure must leave the run indistinguishable from
an uncached one.
"""

import asyncio
import json
from datetime import datetime, timezone

import squeeze_cache as sc


class FakePipe:
    def __init__(self, store):
        self._store = store
        self._ops = []

    def set(self, k, v, ex=None):
        self._ops.append((k, v, ex))

    async def execute(self):
        for k, v, ex in self._ops:
            self._store[k] = (v, ex)


class FakeRedis:
    """Minimal redis.asyncio stand-in: mget + pipeline(set ex)."""

    def __init__(self):
        self.store = {}          # key -> (json_str, ttl)
        self.mget_calls = 0

    async def mget(self, keys):
        self.mget_calls += 1
        return [self.store[k][0] if k in self.store else None for k in keys]

    def pipeline(self):
        return FakePipe(self.store)


class BrokenRedis:
    """Every operation raises — the cache must degrade, not propagate."""

    async def mget(self, keys):
        raise ConnectionError("redis down")

    def pipeline(self):
        raise ConnectionError("redis down")


def _cache(client):
    return sc.SqueezeCache(client=client)


class _Fetcher:
    """Records calls; returns a canned {ticker: value} mapping."""

    def __init__(self, data):
        self.data = data
        self.calls = []

    async def __call__(self, tickers):
        self.calls.append(list(tickers))
        return {t: self.data[t] for t in tickers if t in self.data}


class TestReadThrough:
    def test_miss_fetches_and_caches_with_ttl(self):
        fake = FakeRedis()
        fetch = _Fetcher({"NOVA": {"short_pct_float": 0.35}})
        out = asyncio.run(sc.cached_short_metrics(
            ["NOVA"], fetch, ttl=1234, cache=_cache(fake)))
        assert out == {"NOVA": {"short_pct_float": 0.35}}
        assert fetch.calls == [["NOVA"]]
        stored, ttl = fake.store["squeeze:short:NOVA"]
        assert json.loads(stored) == {"short_pct_float": 0.35}
        assert ttl == 1234

    def test_hit_skips_the_source(self):
        fake = FakeRedis()
        fake.store["squeeze:short:NOVA"] = (json.dumps({"short_ratio": 7.0}), 99)
        fetch = _Fetcher({"NOVA": {"short_ratio": -1.0}})   # must NOT be used
        out = asyncio.run(sc.cached_short_metrics(
            ["NOVA"], fetch, ttl=99, cache=_cache(fake)))
        assert out == {"NOVA": {"short_ratio": 7.0}}
        assert fetch.calls == []

    def test_partial_hit_fetches_only_misses(self):
        fake = FakeRedis()
        fake.store["squeeze:short:AAA"] = (json.dumps({"v": 1}), 99)
        fetch = _Fetcher({"BBB": {"v": 2}})
        out = asyncio.run(sc.cached_short_metrics(
            ["AAA", "BBB"], fetch, ttl=99, cache=_cache(fake)))
        assert out == {"AAA": {"v": 1}, "BBB": {"v": 2}}
        assert fetch.calls == [["BBB"]]

    def test_absent_ticker_negative_cached(self):
        # source has nothing on QUIET -> sentinel cached -> second call
        # neither returns it nor re-asks the source for it
        fake = FakeRedis()
        fetch = _Fetcher({"LOUD": {"posts": 5}})
        c = _cache(fake)
        out1 = asyncio.run(sc.cached_social(["LOUD", "QUIET"], fetch, ttl=60, cache=c))
        assert out1 == {"LOUD": {"posts": 5}}
        assert json.loads(fake.store["squeeze:social:QUIET"][0]) == sc._SENTINEL
        out2 = asyncio.run(sc.cached_social(["LOUD", "QUIET"], fetch, ttl=60, cache=c))
        assert out2 == {"LOUD": {"posts": 5}}
        assert fetch.calls == [["LOUD", "QUIET"]]   # exactly one source call ever

    def test_dedup_and_normalization(self):
        fake = FakeRedis()
        fetch = _Fetcher({"NOVA": {"v": 1}})
        out = asyncio.run(sc.cached_short_metrics(
            [" nova ", "NOVA", "", None], fetch, ttl=9, cache=_cache(fake)))
        assert out == {"NOVA": {"v": 1}}
        assert fetch.calls == [["NOVA"]]

    def test_corrupt_entry_is_a_miss(self):
        fake = FakeRedis()
        fake.store["squeeze:short:NOVA"] = ("{not json", 99)
        fetch = _Fetcher({"NOVA": {"v": 2}})
        out = asyncio.run(sc.cached_short_metrics(
            ["NOVA"], fetch, ttl=99, cache=_cache(fake)))
        assert out == {"NOVA": {"v": 2}}
        assert fetch.calls == [["NOVA"]]

    def test_empty_ticker_list(self):
        fetch = _Fetcher({})
        out = asyncio.run(sc.cached_short_metrics([], fetch, ttl=9,
                                                  cache=_cache(FakeRedis())))
        assert out == {} and fetch.calls == []


class TestDegradation:
    def test_broken_redis_falls_through_to_source(self):
        fetch = _Fetcher({"NOVA": {"v": 1}})
        out = asyncio.run(sc.cached_short_metrics(
            ["NOVA"], fetch, ttl=9, cache=_cache(BrokenRedis())))
        assert out == {"NOVA": {"v": 1}}          # answer identical to uncached
        assert fetch.calls == [["NOVA"]]

    def test_broken_redis_marks_down_no_retry_storm(self):
        c = _cache(BrokenRedis())
        fetch = _Fetcher({"NOVA": {"v": 1}})
        asyncio.run(sc.cached_short_metrics(["NOVA"], fetch, ttl=9, cache=c))
        assert c._down_until > 0                  # backoff armed after failure

    def test_no_uri_no_client_is_direct(self, monkeypatch):
        monkeypatch.delenv("REDIS_URI", raising=False)
        c = sc.SqueezeCache()                     # nothing to connect to
        fetch = _Fetcher({"NOVA": {"v": 1}})
        out = asyncio.run(sc.cached_short_metrics(["NOVA"], fetch, ttl=9, cache=c))
        assert out == {"NOVA": {"v": 1}}
        assert fetch.calls == [["NOVA"]]


class TestNewsRoundTrip:
    _PUB = datetime(2026, 7, 2, 15, 30, tzinfo=timezone.utc)

    def _doc(self):
        return {"title": "Nova wins FDA approval", "source": "Reuters",
                "source_type": "rss", "published_at": self._PUB,
                "tickers": ["NOVA"], "sentiment": {"score": 0.5}}

    def test_datetime_revived_after_cache(self):
        fake = FakeRedis()
        fetch = _Fetcher({"NOVA": [self._doc()]})
        c = _cache(fake)
        asyncio.run(sc.cached_ticker_news(["NOVA"], fetch, ttl=60, cache=c))
        out = asyncio.run(sc.cached_ticker_news(["NOVA"], fetch, ttl=60, cache=c))
        assert fetch.calls == [["NOVA"]]           # second call served from cache
        pub = out["NOVA"][0]["published_at"]
        assert isinstance(pub, datetime) and pub == self._PUB   # aware + exact

    def test_revived_docs_feed_news_signal(self):
        # end-to-end: cached slice -> news_signal decay still works
        from news_signal import news_ignition
        fake = FakeRedis()
        fetch = _Fetcher({"NOVA": [self._doc()]})
        c = _cache(fake)
        asyncio.run(sc.cached_ticker_news(["NOVA"], fetch, ttl=60, cache=c))
        out = asyncio.run(sc.cached_ticker_news(["NOVA"], fetch, ttl=60, cache=c))
        now = datetime(2026, 7, 2, 18, 0, tzinfo=timezone.utc)
        ign, comp = news_ignition(out["NOVA"], now=now)
        assert ign > 0.5 and comp["n_docs"] == 1

    def test_unparseable_timestamp_is_a_miss(self):
        fake = FakeRedis()
        fake.store["squeeze:news:NOVA"] = (
            json.dumps([{"title": "x", "published_at": "not-a-date"}]), 60)
        fetch = _Fetcher({"NOVA": [self._doc()]})
        out = asyncio.run(sc.cached_ticker_news(["NOVA"], fetch, ttl=60,
                                                cache=_cache(fake)))
        assert fetch.calls == [["NOVA"]]           # bad entry -> refetch
        assert out["NOVA"][0]["published_at"] == self._PUB

    def test_empty_news_slice_cached_not_sentineled(self):
        # fetch_ticker_news returns [] for quiet names — a real, cacheable answer
        fake = FakeRedis()
        fetch = _Fetcher({"NOVA": []})
        c = _cache(fake)
        out1 = asyncio.run(sc.cached_ticker_news(["NOVA"], fetch, ttl=60, cache=c))
        out2 = asyncio.run(sc.cached_ticker_news(["NOVA"], fetch, ttl=60, cache=c))
        assert out1 == {"NOVA": []} and out2 == {"NOVA": []}
        assert fetch.calls == [["NOVA"]]


class TestTTLDefaults:
    def test_env_ttl_used_when_not_passed(self, monkeypatch):
        monkeypatch.setenv("SQUEEZE_SOCIAL_TTL_S", "77")
        fake = FakeRedis()
        fetch = _Fetcher({"NOVA": {"posts": 1}})
        asyncio.run(sc.cached_social(["NOVA"], fetch, cache=_cache(fake)))
        assert fake.store["squeeze:social:NOVA"][1] == 77

    def test_default_ttls(self):
        assert sc.DEFAULT_SHORT_TTL_S == 12 * 3600
        assert sc.DEFAULT_SOCIAL_TTL_S == 15 * 60
        assert sc.DEFAULT_NEWS_TTL_S == 10 * 60
