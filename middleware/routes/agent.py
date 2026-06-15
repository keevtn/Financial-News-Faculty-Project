from __future__ import annotations

import json
import logging
import os
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.security.api_key import APIKeyHeader
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from middleware.agent import run
from middleware.limiter import limiter

log = logging.getLogger("middleware.routes.agent")
router = APIRouter()

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_agent_key(key: str | None = Security(_api_key_header)) -> None:
    """Reject requests that don't carry a valid X-API-Key header."""
    expected = os.environ.get("AGENT_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=503, detail="Agent key not configured")
    if not key or not secrets.compare_digest(key, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class AgentRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)


@router.post("/", dependencies=[Depends(_require_agent_key)])
@limiter.limit("10/minute")
async def agent_chat(body: AgentRequest, request: Request) -> StreamingResponse:
    """
    POST /api/agent
    Body: {"message": "..."}
    Returns a text/event-stream (SSE) of JSON payloads:
      data: {"type": "tool_call", "name": "query_news", "input": {...}}
      data: {"type": "text", "text": "..."}
      data: [DONE]
    """
    collection = request.app.state.news_collection

    async def event_stream():
        try:
            async for chunk in run(body.message, collection):
                yield f"data: {chunk}\n\n"
        except (ValueError, RuntimeError) as exc:
            log.error("Agent error: %s", exc)
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")
