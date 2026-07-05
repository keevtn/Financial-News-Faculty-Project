# Feed Sources

All sources ingested by this project, grouped by pipeline and access method.
Reddit is consumed via RSS (no credentials). StockTwits and Bluesky are consumed
via their respective public APIs (no credentials).

---

## Structured Sources

Polled by `backend/IngestionModule.py` → `RSSExtractor`, `SECExtractor`, `FDAExtractor`.

### RSS / Atom Newswires

| Label | URL | Topic Focus |
|---|---|---|
| Bloomberg Markets | https://feeds.bloomberg.com/markets/news.rss | Equities, Macro |
| Financial Times | https://www.ft.com/rss/home | Equities, Macro, Bonds |
| Wall Street Journal Markets | https://feeds.a.dj.com/rss/RSSMarketsMain.xml | Equities, Macro |
| CNBC Top News | https://www.cnbc.com/id/100003114/device/rss/rss.html | Equities, Macro |
| MarketWatch Top Stories | https://feeds.marketwatch.com/marketwatch/topstories/ | Equities |
| Seeking Alpha Market News | https://seekingalpha.com/market_currents.xml | Equities, Analysis |
| Federal Reserve Press Releases | https://www.federalreserve.gov/feeds/press_all.xml | Macro, Bonds |
| BLS Economic News | https://www.bls.gov/feed/bls_latest.rss | Macro |
| Bureau of Economic Analysis | https://apps.bea.gov/rss/rss.xml | Macro (GDP, PCE, trade) |
| EIA Today in Energy | https://www.eia.gov/rss/todayinenergy.xml | Energy (supply/demand) |
| CFTC Press Releases | https://www.cftc.gov/RSS/RSSGP/rssgp.xml | Derivatives enforcement |
| FTC Press Releases | https://www.ftc.gov/feeds/press-release.xml | Antitrust, merger challenges |
| CoinDesk | https://www.coindesk.com/arc/outboundfeeds/rss/ | Crypto |
| Cointelegraph | https://cointelegraph.com/rss | Crypto |
| Yahoo Finance | https://finance.yahoo.com/rss/topfinstories | Equities |
| PR Newswire | https://www.prnewswire.com/rss/news-releases-list.rss | All |
| Business Wire | https://feed.businesswire.com/rss/home/?rss=G1&rssid=1 | All |
| Benzinga | https://www.benzinga.com/feed | Equities, Analysis |
| GlobeNewswire (Public Companies) | https://www.globenewswire.com/RssFeed/orgclass/1/... | Press Releases, Small/Mid-cap |
| MarketWatch Real-time Headlines | https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines | Equities (intraday, faster than Top Stories) |
| MarketWatch Bulletins | https://feeds.content.dowjones.io/public/rss/mw_bulletins | Market-moving one-liners |
| Investing.com News | https://www.investing.com/rss/news.rss | Analyst ratings, company news, macro |
| DOJ Press Releases | https://www.justice.gov/news/rss?type=press_release&m=1 | Indictments, merger suits, enforcement |
| Stock Titan | https://www.stocktitan.net/rss | Ticker-tagged wire aggregate (incl. ACCESSWIRE reach) |
| Nasdaq Trade Halts | https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts | Halt/resume events w/ reason codes (T1/T12/H11) |
| Nasdaq Markets | https://www.nasdaq.com/feed/rssoutbound?category=Markets | Equities |
| Nasdaq IPOs | https://www.nasdaq.com/feed/rssoutbound?category=IPOs | New listings / pricings |
| FierceBiotech | https://www.fiercebiotech.com/rss/xml | Trial readouts, FDA catalysts |
| FiercePharma | https://www.fiercepharma.com/rss/xml | Pharma commercial / regulatory |
| Endpoints News | https://endpoints.news/feed | Biotech / FDA catalysts |
| BioPharma Dive | https://www.biopharmadive.com/feeds/news/ | Biotech / pharma industry |

The 12 feeds from "MarketWatch Real-time Headlines" down were added and verified
live (HTTP 200, RSS/XML content type) on 2026-07-05. Probed but excluded:
Treasury press RSS (dead — redirects to the OFAC homepage), CNBC Earnings feed
(empty response), Business Insider Markets (unverifiable).

> **ACCESSWIRE / ACCESS Newswire — not ingestable.** Their domains sit behind a
> Cloudflare bot challenge that returns `403 "Just a moment..."` to any
> server-side fetch, so a plain RSS poll cannot reach them. Excluded until a
> licensed/API feed becomes available.

### SEC EDGAR

Polled via the EDGAR `getcurrent` Atom feed per filing type (`SECExtractor`). The
type is URL-encoded, so multi-word forms like `SC 13D/A` work. Requires a contact
email in the User-Agent (`SEC_CONTACT_EMAIL`) per SEC fair-access policy.

| Filing Type | Description |
|---|---|
| 8-K | Current reports (material events, earnings releases) |
| 10-K / 10-Q | Annual / quarterly reports |
| S-1 | IPO registration statements |
| 6-K | Foreign private issuer reports |
| 425 / S-4 | M&A communications / registration |
| SC 13D / SC 13D/A | Activist >5% stake (and amendments) |
| SC TO-T / SC 14D9 | Tender offer / target response |
| DEFM14A | Merger-vote proxy statement |
| 424B4 | Priced offering prospectus (dilution) |

Two SEC **RSS** feeds are also polled by `RSSExtractor` and routed to the `sec`
lane: **SEC Press Releases** (`/news/pressreleases.rss`) and **SEC
Administrative Proceedings** (`/rss/litigation/admin.xml`, enforcement).

Filings carry a CIK, not a ticker, so `backend/edgar_tickers.py` loads SEC's
`company_tickers.json` (CIK→ticker) and the catalyst ranker resolves the 10-digit
CIK in each filing title to a symbol — without it the regulatory lane can't rank
filings.

### FDA

Press releases + MedWatch via RSS; recalls via the openFDA REST API. All
normalized by `FDAExtractor` (source_type `fda`).

| Source | Description |
|---|---|
| Press releases (RSS) | FDA news, approvals, safety communications |
| MedWatch (RSS) | Safety alerts / labeling changes |
| Drug enforcement | openFDA `/drug/enforcement.json` — drug recalls |
| Device enforcement | openFDA `/device/enforcement.json` — device recalls |
| Food enforcement | openFDA `/food/enforcement.json` — food recalls |
| Drug adverse events | openFDA `/drug/event.json` (FAERS); off by default (high volume) |

---

## Unstructured Sources

### Reddit — Subreddits (RSS)

Consumed via Reddit's public unauthenticated Atom feed (`/new/.rss`).
No credentials required. Polled by `RSSExtractor` alongside newswires.
Rate limit: ~30 requests per 10 minutes per IP.

| Subreddit | URL | Strength |
|---|---|---|
| r/wallstreetbets | https://www.reddit.com/r/wallstreetbets/new/.rss | Retail sentiment, options, meme stocks |
| r/investing | https://www.reddit.com/r/investing/new/.rss | Long-term fundamentals, diverse coverage |
| r/stocks | https://www.reddit.com/r/stocks/new/.rss | General equities discussion |
| r/SecurityAnalysis | https://www.reddit.com/r/SecurityAnalysis/new/.rss | Deep fundamental analysis, high quality |
| r/economics | https://www.reddit.com/r/economics/new/.rss | Macro, academic papers, policy |
| r/econmonitor | https://www.reddit.com/r/econmonitor/new/.rss | Economic data releases (CPI, GDP, jobs) |
| r/StockMarket | https://www.reddit.com/r/StockMarket/new/.rss | General market discussion |
| r/options | https://www.reddit.com/r/options/new/.rss | Derivatives, volatility, flow |
| r/algotrading | https://www.reddit.com/r/algotrading/new/.rss | Systematic strategies, signals |
| r/CryptoCurrency | https://www.reddit.com/r/CryptoCurrency/new/.rss | Broad crypto market |
| r/Bitcoin | https://www.reddit.com/r/Bitcoin/new/.rss | Bitcoin-specific news and discussion |

---

### StockTwits — Ticker Watchlist

Consumed via the public unauthenticated symbol stream endpoint:
`https://api.stocktwits.com/api/2/streams/symbol/{TICKER}.json`

No credentials required. Rate limit: 200 requests per hour.

> **Disabled (Cloudflare).** StockTwits fronts this API with Cloudflare, which
> blocks plain `aiohttp`/`requests` by TLS fingerprint (403). Reaching it would
> require impersonating a browser to defeat that block — a gray-area
> circumvention we deliberately avoid. So this source is **off by default**
> (`enable_stocktwits=False`; `RUN_STOCKTWITS` unset). Bluesky + Reddit cover
> social. The extractor is kept (plain, non-impersonating) for reference.
Crypto tickers use StockTwits' `.X` suffix convention.
Defined in `IngestionModule.py` → `STOCKTWITS_WATCHLIST`.

| Category | Tickers |
|---|---|
| Broad market ETFs | SPY, QQQ, DIA |
| Mega-cap equities | AAPL, MSFT, GOOGL, AMZN, NVDA, META, TSLA |
| Financials | JPM, BAC |
| Energy | XOM, CVX |
| Technology / Semiconductors | AMD, INTC |
| Bonds / Macro proxies | TLT, GLD |
| Crypto | BTC.X, ETH.X |
| High-sentiment / retail | GME, AMC, PLTR |

> StockTwits users self-tag messages as bullish or bearish — this provides
> human-labeled sentiment alongside our FinBERT scores.

---

### Bluesky — Keyword / Hashtag Search

Consumed via the AT Protocol AppView API (no credentials):
`https://api.bsky.app`

No API key required. Searches run via `app.bsky.feed.searchPosts`.

> **Endpoint note:** as of mid-2026 the *public* AppView host
> (`public.api.bsky.app`) returns `403` for `searchPosts` (an anti-scraping
> change), while the main AppView host `api.bsky.app` still serves the same
> search unauthenticated. The extractor points at `api.bsky.app`.
Defined in `IngestionModule.py` → `BLUESKY_SEARCH_TERMS`.

| Category | Terms |
|---|---|
| Equities & market | #stocks, #investing, #stockmarket, #wallstreetbets, #earnings, #trading, #options, #ipo, #merger |
| Macro | #economy, #inflation, #federalreserve, #gdp, #cpi |
| Crypto | #crypto, #bitcoin, #ethereum, #defi |
| Commodities / Energy | #gold, #oil, #commodities |
| Bonds | #bonds, #treasury |
| Tech / Sector | #fintech, #semiconductor, #ai |

---

## Summary

| Source | Count | Credentials Required | Status |
|---|---|---|---|
| RSS Newswires | 15 feeds | None | Live |
| SEC EDGAR | 5 filing types | None | Live |
| FDA | 3 endpoints | None | Live |
| Reddit (RSS) | 11 subreddits | None | Live |
| StockTwits | 22 tickers | n/a | Disabled, need to gain access |
| Bluesky | 27 search terms | None | Live (RUN_SOCIAL) |
