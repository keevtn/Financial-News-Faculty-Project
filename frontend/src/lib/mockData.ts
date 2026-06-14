import { NewsItem, SourceType, SentimentLabel, TopicLabel } from "@/types/news";

const ago = (minutes: number) =>
  new Date(Date.now() - minutes * 60_000).toISOString();

// Sentiment fields are intentionally omitted — FinBERT scores them at runtime
// via the middleware /api/sentiment/batch endpoint on page load.
export const MOCK_NEWS: NewsItem[] = [
  {
    id: "1",
    source: "Bloomberg Markets",
    source_type: "rss",
    title: "Federal Reserve Signals Cautious Approach to Rate Cuts Amid Persistent Inflation",
    published_at: ago(15),
    description:
      "Fed Chair Jerome Powell indicated the central bank remains data-dependent, suggesting rate cuts may be delayed if inflation does not continue its downward trajectory toward the 2% target.",
    url: "#",
    topic: "Macro",
  },
  {
    id: "2",
    source: "CNBC Top News",
    source_type: "rss",
    title: "Nvidia Smashes Q2 Earnings Estimates — Revenue Up 122% Year-Over-Year",
    published_at: ago(45),
    description:
      "Nvidia reported record quarterly revenue of $30.04 billion, driven by insatiable demand for AI chips from hyperscalers and enterprise customers. Data center revenue alone grew 154% YoY.",
    url: "#",
    topic: "Equities, Technology",
  },
  {
    id: "3",
    source: "SEC EDGAR — 8-K",
    source_type: "sec",
    title: "Apple Inc. — Form 8-K: Entry into a Material Definitive Agreement",
    published_at: ago(90),
    description:
      "Apple Inc. filed an 8-K disclosing entry into a credit agreement providing for a $5 billion unsecured revolving credit facility.",
    url: "#",
    topic: "Equities, Regulatory",
    extra: { filing_type: "8-K", accession_number: "0000320193-24-000071" },
  },
  {
    id: "4",
    source: "CoinDesk",
    source_type: "rss",
    title: "Bitcoin Surges Past $105,000 as ETF Inflows Reach Monthly Record",
    published_at: ago(120),
    description:
      "Bitcoin crossed the $105,000 mark for the first time, boosted by record spot ETF inflows totaling $1.2 billion in a single day. Analysts cite growing institutional adoption as the primary driver.",
    url: "#",
    topic: "Crypto",
  },
  {
    id: "5",
    source: "FDA Drug Enforcement",
    source_type: "fda",
    title: "[Class I] Pfizer Inc. — Contamination Risk in Cardiac Medication Batch",
    published_at: ago(180),
    description:
      "Reason: Potential microbial contamination | Status: Ongoing | Class: Class I | Voluntary/Mandated: Voluntary",
    url: "#",
    topic: "Regulatory",
    extra: { recall_number: "Z-1234-2024", classification: "Class I", status: "Ongoing" },
  },
  {
    id: "6",
    source: "MarketWatch Top Stories",
    source_type: "rss",
    title: "Oil Prices Fall 3% on OPEC+ Output Increase Announcement",
    published_at: ago(200),
    description:
      "Crude oil futures declined sharply after OPEC+ members agreed to accelerate production increases by 400,000 barrels per day starting next quarter.",
    url: "#",
    topic: "Energy, Commodities",
  },
  {
    id: "7",
    source: "Federal Reserve Press Releases",
    source_type: "rss",
    title: "Federal Reserve Board Announces Results of Annual Bank Stress Tests",
    published_at: ago(240),
    description:
      "All 31 large banks tested remained well above minimum capital requirements under a severe recession scenario, demonstrating the resilience of the U.S. banking system.",
    url: "#",
    topic: "Macro, Regulatory",
  },
  {
    id: "8",
    source: "SEC EDGAR — 10-K",
    source_type: "sec",
    title: "Tesla Inc. — Annual Report 10-K: Fiscal Year 2024",
    published_at: ago(300),
    description:
      "Tesla filed its annual 10-K. Revenue grew 8% to $97.7 billion. Operating margin declined to 6.2% from 9.2% prior year, reflecting increased competition and pricing pressure.",
    url: "#",
    topic: "Equities, Technology",
    extra: { filing_type: "10-K", accession_number: "0000950170-24-032867" },
  },
  {
    id: "9",
    source: "Yahoo Finance",
    source_type: "rss",
    title: "Gold Hits All-Time High Above $2,700 as Dollar Weakens",
    published_at: ago(360),
    description:
      "Gold futures surged to a record $2,718 per ounce as the U.S. dollar weakened on soft economic data and expectations of Fed easing. Safe-haven demand remained elevated.",
    url: "#",
    topic: "Commodities, Macro",
  },
  {
    id: "10",
    source: "FDA Press Releases",
    source_type: "fda",
    title: "FDA Approves Breakthrough Alzheimer's Drug Showing 35% Cognitive Decline Reduction",
    published_at: ago(420),
    description:
      "The FDA granted full approval to Eli Lilly's donanemab after Phase 3 trials demonstrated a 35% slowing of cognitive and functional decline in early symptomatic Alzheimer's disease.",
    url: "#",
    topic: "Regulatory",
  },
  {
    id: "11",
    source: "BLS Economic News",
    source_type: "rss",
    title: "U.S. CPI Rose 2.4% Year-Over-Year in May, Below Expectations",
    published_at: ago(480),
    description:
      "The Bureau of Labor Statistics reported headline CPI increased 2.4% YoY in May, slightly below the 2.5% consensus estimate. Core CPI rose 2.6%, a continued deceleration from 2023 peaks.",
    url: "#",
    topic: "Macro",
  },
  {
    id: "12",
    source: "Cointelegraph",
    source_type: "rss",
    title: "Ethereum Completes Major Protocol Upgrade, Reducing Transaction Fees by 80%",
    published_at: ago(600),
    description:
      "Ethereum's latest network upgrade implemented EIP-7702 and blob data improvements that dramatically reduced Layer 2 transaction costs, driving increased DeFi activity.",
    url: "#",
    topic: "Crypto, Technology",
  },
  {
    id: "13",
    source: "Wall Street Journal Markets",
    source_type: "rss",
    title: "U.S. 10-Year Treasury Yield Climbs to 4.8% on Strong Jobs Report",
    published_at: ago(720),
    description:
      "The benchmark 10-year Treasury yield rose to 4.8%, its highest since November, after nonfarm payrolls beat estimates by 80,000 jobs, reducing expectations for near-term rate cuts.",
    url: "#",
    topic: "Bonds, Macro",
  },
  {
    id: "14",
    source: "SEC EDGAR — S-1",
    source_type: "sec",
    title: "Anthropic Inc. — Form S-1: Initial Public Offering Registration",
    published_at: ago(900),
    description:
      "AI safety company Anthropic filed an S-1 registration statement with the SEC ahead of its anticipated IPO. The filing reveals $3.2 billion in annualized revenue and rapid enterprise customer growth.",
    url: "#",
    topic: "Technology, Equities",
    extra: { filing_type: "S-1", accession_number: "0001234567-24-000001" },
  },
  {
    id: "15",
    source: "PR Newswire",
    source_type: "rss",
    title: "Solar Energy Capacity Surpasses 1 Terawatt Globally — Milestone Report",
    published_at: ago(1200),
    description:
      "BloombergNEF data shows global installed solar capacity has crossed 1 terawatt for the first time, a milestone reached a decade ahead of earlier projections, driven by falling panel costs.",
    url: "#",
    topic: "Energy",
  },
  // --- Social mock items (source_type: "social") ---
  {
    id: "16",
    source: "Reddit - WallStreetBets",
    source_type: "social",
    title: "NVDA calls printing — AI demand not slowing anytime soon",
    published_at: ago(8),
    description:
      "Loaded up on NVDA $900 calls expiring Friday. Data center capex from the hyperscalers is only going up. Jensen Huang basically said supply is constrained through next year. Not financial advice.",
    url: "#",
    topic: "Equities, Technology",
    tickers: ["NVDA"],
  },
  {
    id: "17",
    source: "StockTwits — $BTC.X",
    source_type: "social",
    title: "Bitcoin holding above $100k support — next leg up incoming $BTC",
    published_at: ago(22),
    description:
      "Bitcoin holding above $100k support — next leg up incoming $BTC. Spot ETF inflows still strong. Institutions not selling.",
    url: "#",
    topic: "Crypto",
    tickers: ["BTC"],
    extra: { st_sentiment: "Bullish", ticker: "BTC.X", st_user: "cryptobull99" },
  },
  {
    id: "18",
    source: "Bluesky",
    source_type: "social",
    title: "Fed minutes confirmed what the bond market already knew — no cuts until Q3 at earliest #federalreserve #bonds",
    published_at: ago(35),
    description:
      "Fed minutes confirmed what the bond market already knew — no cuts until Q3 at earliest. 10yr yield moving back toward 4.9. #federalreserve #bonds #macro",
    url: "#",
    topic: "Macro, Bonds",
    extra: { bsky_handle: "macrowatcher.bsky.social", likes: 47, replies: 12 },
  },
];

export const ALL_TOPICS: TopicLabel[] = [
  "Crypto", "Energy", "Equities", "Macro",
  "Regulatory", "Bonds", "Commodities", "Technology", "General",
];

export const ALL_SOURCE_TYPES: SourceType[] = ["rss", "sec", "fda", "social"];
// Source types that belong to the structured pipeline (no social)
export const STRUCTURED_SOURCE_TYPES: SourceType[] = ["rss", "sec", "fda"];
export const ALL_SENTIMENTS: SentimentLabel[] = ["bullish", "bearish", "neutral"];
export const ALL_PLATFORMS: string[] = ["Reddit", "StockTwits", "Bluesky"];
