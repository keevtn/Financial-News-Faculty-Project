"""
Financial news analysis agent powered by the Anthropic SDK.

Uses claude-opus-4-8 with adaptive thinking and an agentic tool-use loop.
The agent has one tool — query_news — which searches MongoDB for articles
matching keyword, topic, sentiment, or ticker filters.

Entry point: run(user_message, collection) — async generator yielding
JSON-encoded SSE payload strings.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Any, AsyncIterator

log = logging.getLogger("middleware.agent")

MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = """\
You are a financial news analysis assistant with access to a live database
of financial news articles.  Your role is to help users:

- Summarise and synthesise recent financial news
- Identify trends across tickers, sectors, and sentiment
- Answer questions about specific companies or market topics
- Highlight bullish / bearish signals derived from news sentiment

When querying news, filter by the most relevant parameters and prioritise
recency.  Keep analysis concise and grounded in the articles you retrieve.\
"""

TOOLS: list[dict[str, Any]] = [
    {
        "name": "query_news",
        "description": (
            "Search the financial news database for articles. "
            "Returns recent articles with title, description, sentiment, tickers, and topic."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Keyword to match in article title or description.",
                },
                "topic": {
                    "type": "string",
                    "description": (
                        "Filter by topic label: Crypto, Energy, Equities, Macro, "
                        "Regulatory, Bonds, Commodities, Technology, General."
                    ),
                },
                "sentiment": {
                    "type": "string",
                    "enum": ["bullish", "bearish", "neutral"],
                    "description": "Filter by FinBERT sentiment label.",
                },
                "ticker": {
                    "type": "string",
                    "description": "Filter by stock ticker symbol (e.g. AAPL).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max articles to return (1–20). Defaults to 10.",
                },
            },
            "required": [],
        },
    }
]


def _build_client():
    """Return an AsyncAnthropic client from ANTHROPIC_API_KEY."""
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "anthropic package is not installed — run: pip install anthropic"
        ) from exc

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set in environment")

    return anthropic.AsyncAnthropic(api_key=api_key)


async def _query_news(
    collection: Any,
    *,
    search: str | None = None,
    topic: str | None = None,
    sentiment: str | None = None,
    ticker: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Execute a MongoDB news query and return a trimmed list of article dicts."""
    if collection is None:
        return []

    limit = max(1, min(limit, 20))
    query: dict[str, Any] = {}

    if search:
        safe = re.escape(search)
        query["$or"] = [
            {"title": {"$regex": safe, "$options": "i"}},
            {"description": {"$regex": safe, "$options": "i"}},
        ]
    if topic:
        query["topic"] = {"$regex": re.escape(topic), "$options": "i"}
    if sentiment:
        query["sentiment.label"] = sentiment
    if ticker:
        query["tickers"] = {"$in": [ticker.upper()]}

    cursor = collection.find(query, {"_id": 0}).sort("published_at", -1).limit(limit)
    docs = await cursor.to_list(length=limit)

    results = []
    for d in docs:
        published = d.get("published_at")
        if isinstance(published, datetime):
            published = published.isoformat()
        results.append(
            {
                "title": d.get("title", ""),
                "description": (d.get("description") or "")[:400],
                "source": d.get("source", ""),
                "published_at": published or "",
                "topic": d.get("topic", ""),
                "tickers": d.get("tickers") or [],
                "sentiment": d.get("sentiment") or None,
                "url": d.get("url", ""),
            }
        )
    return results


async def run(
    user_message: str,
    collection: Any,
) -> AsyncIterator[str]:
    """
    Agentic loop: calls Claude with tool use until no more tool calls remain,
    then yields the final assistant text as a JSON-encoded SSE payload.

    Each yielded string is a JSON object:
      {"type": "tool_call", "name": "...", "input": {...}}   — emitted per tool invocation
      {"type": "text", "text": "..."}                        — emitted when done
    """
    client = _build_client()
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]

    while True:
        async with client.messages.stream(
            model=MODEL,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        ) as stream:
            response = await stream.get_final_message()

        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if not tool_uses:
            for block in response.content:
                if block.type == "text":
                    yield json.dumps({"type": "text", "text": block.text})
            break

        # Execute each tool call and collect results
        tool_results = []
        for tool_use in tool_uses:
            args = tool_use.input or {}
            log.debug("Tool call: %s %s", tool_use.name, args)

            if tool_use.name == "query_news":
                articles = await _query_news(
                    collection,
                    search=args.get("search"),
                    topic=args.get("topic"),
                    sentiment=args.get("sentiment"),
                    ticker=args.get("ticker"),
                    limit=int(args.get("limit", 10)),
                )
                content = json.dumps(articles)
            else:
                content = json.dumps({"error": f"Unknown tool: {tool_use.name}"})

            yield json.dumps({"type": "tool_call", "name": tool_use.name, "input": args})

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.id,
                    "content": content,
                }
            )

        # Feed tool results back into the conversation and loop
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
