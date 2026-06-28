"""
alerts.py
=========
Alert rules engine — turns the dashboard's *signals* into *triggers*.

Everything else produces signals you have to go look at (open the Squeeze tab,
the Gossip tab, …). Alerts invert that: one ticker, one row, fired because a
threshold was crossed, ranked by signal confluence. The strongest alert is a
ticker that lights up on **multiple** signals at once (e.g. a short squeeze that
is *also* spiking in social chatter).

``evaluate_alerts`` is pure — it takes the latest squeeze / catalyst items and
the live gossip items and returns one alert per ticker, severity by confluence.
The endpoint just gathers those three inputs. Unit-tested without any I/O.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

# Thresholds at/above which each signal "fires". Override per request if needed.
DEFAULT_THRESHOLDS: dict[str, float] = {
    "squeeze_score": 45.0,     # squeeze_score to flag
    "squeeze_ignition": 0.40,  # AND ignition (actually firing, not just primed)
    "gossip_velocity": 3.0,    # mentions >= 3x trailing baseline
    "gossip_score": 65.0,      # OR a high gossip score
    "catalyst_score": 65.0,    # strong news catalyst
}

_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2}


def _pct(n: Optional[float]) -> str:
    return "—" if n is None else f"{n * 100:.0f}%"


def evaluate_alerts(
    *,
    squeeze: Optional[list[dict[str, Any]]] = None,
    catalyst: Optional[list[dict[str, Any]]] = None,
    gossip: Optional[list[dict[str, Any]]] = None,
    thresholds: Optional[dict[str, float]] = None,
) -> list[dict[str, Any]]:
    """
    One alert per ticker that crossed a threshold. Severity by confluence:
      critical — squeeze firing AND social spiking (the strongest setup)
      high     — squeeze firing, or a strong news catalyst
      medium   — social chatter spike alone
    Sorted by severity, then by the strongest contributing score.
    """
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    hits: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)

    for it in squeeze or []:
        if (it.get("squeeze_score", 0) >= th["squeeze_score"]
                and it.get("ignition_score", 0) >= th["squeeze_ignition"]):
            hits[it["ticker"]]["squeeze"] = {
                "score": it.get("squeeze_score", 0),
                "short_pct_float": it.get("short_pct_float"),
                "ignition": it.get("ignition_score", 0),
            }

    for it in gossip or []:
        if (it.get("velocity", 0) >= th["gossip_velocity"]
                or it.get("gossip_score", 0) >= th["gossip_score"]):
            hits[it["ticker"]]["gossip"] = {
                "velocity": it.get("velocity", 0),
                "recent": it.get("recent_count"),
                "score": it.get("gossip_score", 0),
                "direction": it.get("direction", "neutral"),
            }

    for it in catalyst or []:
        if it.get("catalyst_score", 0) >= th["catalyst_score"]:
            hits[it["ticker"]]["catalyst"] = {
                "score": it.get("catalyst_score", 0),
                "rationale": (it.get("rationale") or "")[:140],
            }

    alerts: list[dict[str, Any]] = []
    for ticker, sig in hits.items():
        kinds = set(sig)
        if "squeeze" in kinds and "gossip" in kinds:
            severity = "critical"
        elif kinds & {"squeeze", "catalyst"}:
            severity = "high"
        else:
            severity = "medium"

        title, detail = _summarize(sig)
        value = max(s.get("score", 0) for s in sig.values())
        tab = "squeeze" if "squeeze" in kinds else ("catalysts" if "catalyst" in kinds else "gossip")

        alerts.append({
            "ticker": ticker,
            "severity": severity,
            "title": title,
            "detail": detail,
            "signals": sorted(kinds),
            "value": round(value, 2),
            "tab": tab,
        })

    alerts.sort(key=lambda a: (_SEV_ORDER.get(a["severity"], 9), -a["value"]))
    return alerts


def _summarize(sig: dict[str, dict[str, Any]]) -> tuple[str, str]:
    """Human title + detail from whichever signals fired for a ticker."""
    kinds = set(sig)
    parts: list[str] = []
    if "squeeze" in kinds:
        s = sig["squeeze"]
        parts.append(f"squeeze {s['score']:.0f} ({_pct(s.get('short_pct_float'))} short)")
    if "gossip" in kinds:
        g = sig["gossip"]
        parts.append(f"chatter {g['velocity']:.1f}× ({g.get('recent')} recent)")
    if "catalyst" in kinds:
        parts.append(f"catalyst {sig['catalyst']['score']:.0f}")

    if "squeeze" in kinds and "gossip" in kinds:
        title = "Short squeeze firing + social spike"
    elif "squeeze" in kinds and "catalyst" in kinds:
        title = "Short squeeze + news catalyst"
    elif "squeeze" in kinds:
        title = "Short squeeze firing"
    elif "catalyst" in kinds:
        title = "Strong news catalyst"
    else:
        title = "Social chatter spike"
    return title, " · ".join(parts)


def severity_counts(alerts: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"critical": 0, "high": 0, "medium": 0}
    for a in alerts:
        counts[a["severity"]] = counts.get(a["severity"], 0) + 1
    return counts
