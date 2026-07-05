"""
sentiment_eval.py
=================
Measure sentiment-classifier quality against a labeled finance benchmark.

The precision/accuracy gap is unmeasurable without ground truth, so this
harness makes every lexicon or model change quantifiable: run it before and
after, and a change only ships if the numbers move the right way.

Benchmark
---------
``data/sentiment_benchmark.jsonl`` — one JSON object per line::

    {"text": "...", "label": "bullish|bearish|neutral", "kind": "news|social"}

Curated to include the historical failure modes: earnings-calendar headlines
that used to read bullish because of non-directional nouns ("earnings",
"revenue"), price-action verbs that used to score neutral ("plunges",
"soars"), finance bigrams ("guidance cut", "beat estimates"), negation, FDA /
SEC event language, and social slang.

Metrics
-------
Per-class precision / recall / F1, macro-F1, accuracy, and — the error that
actually costs money — the **inversion rate**: how often a bullish item is
called bearish or vice versa. Missing a signal costs opportunity; inverting
one buys the wrong side.

Usage
-----
    python sentiment_eval.py                # LM lexicon analyzer (default)
    python sentiment_eval.py --analyzer finbert
    python sentiment_eval.py --kind social  # restrict to one item kind

Pure evaluation logic (``evaluate``) is import-safe and unit-tested; only the
CLI touches the filesystem.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Optional

LABELS = ("bullish", "bearish", "neutral")
DEFAULT_BENCHMARK = Path(__file__).resolve().parent / "data" / "sentiment_benchmark.jsonl"


def load_benchmark(
    path: Path = DEFAULT_BENCHMARK, kind: Optional[str] = None
) -> list[dict[str, Any]]:
    """Load benchmark cases, optionally restricted to one kind (news/social)."""
    cases: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            case = json.loads(line)
            if case["label"] not in LABELS:
                raise ValueError(f"bad label {case['label']!r} in benchmark: {case}")
            if kind is None or case.get("kind") == kind:
                cases.append(case)
    return cases


def evaluate(
    cases: list[dict[str, Any]], predictions: list[str]
) -> dict[str, Any]:
    """
    Score predicted labels against benchmark labels.

    Returns per-class precision/recall/F1, macro-F1, accuracy, inversion rate
    (bullish<->bearish confusions / directional cases), and the confusion
    matrix as ``confusion[true][predicted]``.
    """
    if len(cases) != len(predictions):
        raise ValueError("cases and predictions length mismatch")

    confusion: dict[str, dict[str, int]] = {
        t: {p: 0 for p in LABELS} for t in LABELS
    }
    for case, pred in zip(cases, predictions):
        confusion[case["label"]][pred] += 1

    per_class: dict[str, dict[str, float]] = {}
    for label in LABELS:
        tp = confusion[label][label]
        fn = sum(confusion[label][p] for p in LABELS) - tp
        fp = sum(confusion[t][label] for t in LABELS) - tp
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = (
            2 * precision * recall / (precision + recall)
            if (precision + recall) else 0.0
        )
        per_class[label] = {
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "support": tp + fn,
        }

    n = len(cases)
    correct = sum(confusion[t][t] for t in LABELS)
    directional = sum(
        confusion[t][p] for t in ("bullish", "bearish") for p in LABELS
    )
    inversions = confusion["bullish"]["bearish"] + confusion["bearish"]["bullish"]
    macro_f1 = sum(per_class[t]["f1"] for t in LABELS) / len(LABELS)

    return {
        "n": n,
        "accuracy": round(correct / n, 4) if n else 0.0,
        "macro_f1": round(macro_f1, 4),
        "inversion_rate": round(inversions / directional, 4) if directional else 0.0,
        "per_class": per_class,
        "confusion": confusion,
    }


def run_analyzer(analyzer: Any, cases: list[dict[str, Any]]) -> list[str]:
    """Predict labels for all cases; uses batch scoring when available."""
    pairs = [(c["text"], "") for c in cases]
    if hasattr(analyzer, "analyze_text_batch"):
        return [r.label for r in analyzer.analyze_text_batch(pairs)]
    raise TypeError(f"{type(analyzer).__name__} lacks analyze_text_batch")


def format_report(result: dict[str, Any], title: str = "") -> str:
    """Human-readable report block for the CLI."""
    lines = []
    if title:
        lines.append(title)
        lines.append("=" * len(title))
    lines.append(
        f"n={result['n']}  accuracy={result['accuracy']:.1%}  "
        f"macro_f1={result['macro_f1']:.3f}  "
        f"inversion_rate={result['inversion_rate']:.1%}"
    )
    lines.append(f"{'class':<10}{'precision':>10}{'recall':>10}{'f1':>10}{'support':>10}")
    for label in LABELS:
        m = result["per_class"][label]
        lines.append(
            f"{label:<10}{m['precision']:>10.3f}{m['recall']:>10.3f}"
            f"{m['f1']:>10.3f}{m['support']:>10d}"
        )
    lines.append("confusion (rows=true, cols=predicted):")
    lines.append(f"{'':<10}" + "".join(f"{p:>10}" for p in LABELS))
    for t in LABELS:
        lines.append(
            f"{t:<10}" + "".join(f"{result['confusion'][t][p]:>10d}" for p in LABELS)
        )
    return "\n".join(lines)


def _make_analyzer(name: str) -> Any:
    if name == "lm":
        from sentiment import LoughranMcDonaldAnalyzer
        return LoughranMcDonaldAnalyzer()
    if name == "finbert":
        from sentiment import FinBERTAnalyzer
        return FinBERTAnalyzer()
    raise ValueError(f"unknown analyzer {name!r}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analyzer", choices=("lm", "finbert"), default="lm")
    parser.add_argument("--kind", choices=("news", "social"), default=None)
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument("--show-errors", action="store_true",
                        help="print each misclassified case")
    args = parser.parse_args()

    cases = load_benchmark(args.benchmark, kind=args.kind)
    analyzer = _make_analyzer(args.analyzer)
    predictions = run_analyzer(analyzer, cases)
    result = evaluate(cases, predictions)
    scope = f" [{args.kind}]" if args.kind else ""
    print(format_report(result, f"{args.analyzer}{scope} on {args.benchmark.name}"))

    if args.show_errors:
        print("\nmisclassified:")
        for case, pred in zip(cases, predictions):
            if pred != case["label"]:
                print(f"  true={case['label']:<8} pred={pred:<8} {case['text']}")


if __name__ == "__main__":
    main()
