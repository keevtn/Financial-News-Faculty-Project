"""
sentiment.py
============
Financial sentiment analysis — bearish / bullish / neutral classification.

Three interchangeable analyzers all implement the ``SentimentAnalyzer``
Protocol so the storage layer (``RedisHandler``) never needs to know which
model is active.  Pick one based on your accuracy / latency requirements:

  Analyzer                    Accuracy   Latency     Extra deps
  ──────────────────────────────────────────────────────────────
  FinBERTAnalyzer             High       ~0.5–2 s    transformers, torch
  LoughranMcDonaldAnalyzer    Medium     ~1 ms       none (built-in lexicon)
  VaderSentimentAnalyzer      Low        ~1 ms       vaderSentiment

All analyzers are synchronous.  ``RedisHandler`` calls them via
``asyncio.to_thread`` so FinBERT inference never blocks the event loop.

Dependencies (install only what you use):
    pip install transformers torch   # FinBERT
    pip install vaderSentiment       # VADER fallback
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from IngestionModule import NewsItem

log = logging.getLogger("ingestion_agent.sentiment")


# ---------------------------------------------------------------------------
# Shared result type
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SentimentResult:
    """Output of every SentimentAnalyzer."""

    score: float       # continuous [-1.0, 1.0]; negative = bearish, positive = bullish
    label: str         # "bullish" | "bearish" | "neutral"
    confidence: float  # [0.0, 1.0]; how certain the model is in its label


# ---------------------------------------------------------------------------
# Protocol — the interface every analyzer must satisfy
# ---------------------------------------------------------------------------

class SentimentAnalyzer(Protocol):
    """Maps a NewsItem to a SentimentResult."""

    def analyze(self, item: NewsItem) -> SentimentResult: ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _label_from_score(
    score: float,
    pos_threshold: float = 0.05,
    neg_threshold: float = -0.05,
) -> str:
    """Map a continuous score to a directional label."""
    if score >= pos_threshold:
        return "bullish"
    if score <= neg_threshold:
        return "bearish"
    return "neutral"


def _item_text(item: NewsItem, max_chars: int = 512) -> str:
    """Combine title + description into a single string for analysis."""
    return f"{item.title}. {item.description}".strip()[:max_chars]


# ---------------------------------------------------------------------------
# FinBERT — highest accuracy, finance-tuned transformer
# ---------------------------------------------------------------------------

class FinBERTAnalyzer:
    """
    Classifies financial text using ProsusAI/finbert — a BERT model fine-tuned
    on ~10 k financial news sentences annotated as positive / negative / neutral.

    Score is computed as P(positive) − P(negative) ∈ [-1, 1] so mixed signals
    produce a score near 0 rather than false high confidence.

    Parameters
    ----------
    model_name:
        HuggingFace model ID.  Defaults to "ProsusAI/finbert".
    device:
        -1 for CPU; 0+ for a CUDA GPU index.
    batch_size:
        Passed to the transformers pipeline for ``analyze_batch``.

    Installation
    ------------
        pip install transformers torch
    """

    def __init__(
        self,
        model_name: str = "ProsusAI/finbert",
        device: int = -1,
        batch_size: int = 8,
    ) -> None:
        self._model_name = model_name
        self._device = device
        self._batch_size = batch_size
        self._pipeline: Any = None   # lazy — loaded on first call

    def _load(self) -> None:
        try:
            from transformers import pipeline as hf_pipeline
        except ImportError:
            raise RuntimeError(
                "transformers is not installed — run: pip install transformers torch"
            )
        self._pipeline = hf_pipeline(
            "text-classification",
            model=self._model_name,
            device=self._device,
            top_k=None,   # return scores for all three classes, not just the top one
        )
        log.info(
            "FinBERTAnalyzer: loaded '%s' on device=%s",
            self._model_name, self._device,
        )

    def _result_from_raw(self, raw: list[dict[str, Any]]) -> SentimentResult:
        """Convert a single pipeline output (list of class dicts) to SentimentResult."""
        probs = {r["label"].lower(): r["score"] for r in raw}
        pos = probs.get("positive", 0.0)
        neg = probs.get("negative", 0.0)
        score = float(pos - neg)
        label = _label_from_score(score)
        confidence = float(max(pos, neg, probs.get("neutral", 0.0)))
        return SentimentResult(
            score=round(score, 4),
            label=label,
            confidence=round(confidence, 4),
        )

    def analyze(self, item: NewsItem) -> SentimentResult:
        if self._pipeline is None:
            self._load()
        raw = self._pipeline(_item_text(item))
        # Newer transformers (>=4.35) wraps single-input top_k=None in a list-of-lists:
        # [[{label, score}, ...]] — unwrap to [{label, score}, ...] before scoring.
        if raw and isinstance(raw[0], list):
            raw = raw[0]
        return self._result_from_raw(raw)

    def analyze_batch(self, items: list[NewsItem]) -> list[SentimentResult]:
        """Process multiple NewsItems in one forward pass for higher throughput."""
        if self._pipeline is None:
            self._load()
        texts = [_item_text(i) for i in items]
        return [
            self._result_from_raw(raw)
            for raw in self._pipeline(texts, batch_size=self._batch_size)
        ]

    def analyze_text_batch(
        self, pairs: list[tuple[str, str]]
    ) -> list[SentimentResult]:
        """
        Score a batch of raw (title, description) string pairs.

        Used by the middleware REST endpoint where NewsItem objects are not
        available — the caller passes plain strings from the JSON request body.
        """
        if self._pipeline is None:
            self._load()
        texts = [
            f"{title}. {description}".strip()[:512]
            for title, description in pairs
        ]
        return [
            self._result_from_raw(raw)
            for raw in self._pipeline(texts, batch_size=self._batch_size)
        ]


# ---------------------------------------------------------------------------
# Loughran-McDonald lexicon — lightweight, no ML model required
# ---------------------------------------------------------------------------

# Built-in subset of the Loughran-McDonald (LM) Master Dictionary.
# Source: Loughran & McDonald, "When Is a Liability Not a Liability?", JF 2011.
#
# For full coverage (~3 500 words), download the master dictionary CSV from
#     https://sraf.nd.edu/loughranmcdonald-master-dictionary/
# and pass its path as ``csv_path`` to LoughranMcDonaldAnalyzer.

_LM_BULLISH: frozenset[str] = frozenset({
    # Earnings / revenue beats
    "beat", "exceeded", "exceeds", "surpassed", "surpasses",
    "outperformed", "outperforms", "record",
    # Growth
    "growth", "growing", "grew", "expand", "expanded", "expansion",
    "accelerated", "accelerate", "increase", "increased", "increases",
    # Profitability
    "profit", "profitable", "profitability", "margin", "margins",
    "earnings", "revenue", "revenues", "income",
    # Strength / quality
    "strong", "stronger", "strength", "robust", "solid", "healthy",
    "momentum", "leading", "dominant",
    # Guidance / outlook
    "raised", "upgrade", "upgraded", "outperform", "favorable",
    "improved", "improving", "improvement", "recovery", "recovered",
    # Corporate actions
    "acquisition", "buyback", "dividend", "dividends", "partnership",
    "launch", "launched", "innovation", "innovative",
    # Confidence language
    "confident", "confidence", "optimistic", "opportunity", "opportunities",
    "capitalize", "advantage", "upside", "breakthrough", "milestone",
    "delivered", "delivers", "commitment", "committed", "efficient",
    "sustainable", "superior", "exceptional", "outstanding", "remarkable",
})

_LM_BEARISH: frozenset[str] = frozenset({
    # Financial distress
    "bankrupt", "bankruptcy", "insolvent", "insolvency", "default",
    "defaulted", "delinquent", "impairment", "impaired",
    "writedown", "writeoff", "restatement", "restated",
    # Earnings / revenue misses
    "missed", "misses", "shortfall", "below", "disappointing",
    "disappointed", "underperformed", "underperforms",
    "declined", "declining", "decline",
    # Losses / reductions
    "loss", "losses", "deficit", "deficits", "reduction", "reduced",
    "decrease", "decreased", "drop", "dropped",
    # Guidance / outlook
    "downgrade", "downgraded", "lowered", "warn", "warning",
    "cautious", "headwinds", "challenges", "difficult",
    "uncertainty", "uncertain", "concern", "concerns", "risk", "risks",
    # Legal / regulatory
    "lawsuit", "lawsuits", "litigation", "investigated", "investigation",
    "probe", "penalty", "penalties", "fine", "fines", "violation",
    "violations", "fraud", "alleged", "allegation", "allegations",
    "misconduct", "noncompliance",
    # Operations
    "layoffs", "layoff", "restructuring", "restructure", "recall",
    "recalls", "suspended", "suspension", "terminated", "termination",
    "delayed", "delay", "disruption", "disrupted",
    # Macro / market
    "recession", "contraction", "downturn", "adverse",
    "weak", "weakness", "volatile", "volatility",
    "deteriorating", "deterioration",
})


def _tokenise(text: str) -> list[str]:
    """Lowercase and return word tokens, stripping all punctuation."""
    return re.findall(r"[a-z]+", text.lower())


class LoughranMcDonaldAnalyzer:
    """
    Scores financial text with the Loughran-McDonald (LM) financial lexicon.

    Uses the built-in word subset by default.  For full ~3 500-word coverage,
    download the LM Master Dictionary CSV and supply its path:

        analyzer = LoughranMcDonaldAnalyzer(csv_path="LM_MasterDictionary.csv")

    Scoring
    -------
    score      = (bullish_hits − bearish_hits) / total_tokens  (clamped to [-1, 1])
    confidence = min(1.0, total_sentiment_hits / 5)
                 → reaches 1.0 at 5 or more matched sentiment words

    Parameters
    ----------
    csv_path:
        Optional path to the LM Master Dictionary CSV.  If not supplied the
        built-in subset is used.
    """

    def __init__(self, csv_path: Optional[str] = None) -> None:
        self._bullish: frozenset[str] = _LM_BULLISH
        self._bearish: frozenset[str] = _LM_BEARISH
        if csv_path:
            self._load_csv(csv_path)

    def _load_csv(self, path: str) -> None:
        """Extend word sets from the full LM master dictionary CSV."""
        import csv as _csv
        bullish: set[str] = set()
        bearish: set[str] = set()
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                reader = _csv.DictReader(fh)
                for row in reader:
                    word = row.get("Word", "").lower().strip()
                    if not word:
                        continue
                    if int(row.get("Positive", 0) or 0):
                        bullish.add(word)
                    if int(row.get("Negative", 0) or 0):
                        bearish.add(word)
        except FileNotFoundError:
            log.warning(
                "LM dictionary CSV not found at '%s'; falling back to built-in subset",
                path,
            )
            return
        self._bullish = frozenset(bullish)
        self._bearish = frozenset(bearish)
        log.info(
            "LoughranMcDonaldAnalyzer: loaded %d bullish + %d bearish words from '%s'",
            len(bullish), len(bearish), path,
        )

    def analyze(self, item: NewsItem) -> SentimentResult:
        # Use a generous character limit — the LM scorer benefits from full text
        tokens = _tokenise(_item_text(item, max_chars=2048))
        if not tokens:
            return SentimentResult(score=0.0, label="neutral", confidence=0.0)

        bullish_hits = sum(1 for t in tokens if t in self._bullish)
        bearish_hits = sum(1 for t in tokens if t in self._bearish)
        total = len(tokens)

        raw_score = (bullish_hits - bearish_hits) / total
        score = max(-1.0, min(1.0, raw_score))
        label = _label_from_score(score)
        # 5 sentiment-word hits → confidence = 1.0; scales linearly below that
        confidence = min(1.0, (bullish_hits + bearish_hits) / 5.0)
        return SentimentResult(
            score=round(score, 4),
            label=label,
            confidence=round(confidence, 4),
        )


# ---------------------------------------------------------------------------
# VADER — general-purpose fallback (weakest for financial text)
# ---------------------------------------------------------------------------

# class VaderSentimentAnalyzer:
#     """
#     VADER-based analyzer ported to the new ``SentimentAnalyzer`` Protocol.
#
#     VADER is general-purpose and misreads financial jargon ("guidance cut",
#     "beat estimates") — prefer FinBERTAnalyzer or LoughranMcDonaldAnalyzer
#     for financial news.  Kept here as a zero-setup fallback.
#
#     Installation:
#         pip install vaderSentiment
#     """
#
#     def __init__(self) -> None:
#         try:
#             from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
#             self._analyzer = SentimentIntensityAnalyzer()
#         except ImportError:
#             raise RuntimeError(
#                 "vaderSentiment is not installed — run: pip install vaderSentiment"
#             )
#
#     def analyze(self, item: NewsItem) -> SentimentResult:
#         text = _item_text(item)
#         if not text:
#             return SentimentResult(score=0.0, label="neutral", confidence=0.0)
#         compound = float(self._analyzer.polarity_scores(text)["compound"])
#         label = _label_from_score(compound)
#         # VADER compound magnitude is a reasonable confidence proxy
#         return SentimentResult(
#             score=round(compound, 4),
#             label=label,
#             confidence=round(abs(compound), 4),
#         )
