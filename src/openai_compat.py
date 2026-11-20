"""OpenAI-compatible API adapter (/v1/chat/completions, /v1/models, /metrics) for AI Ops Gateway."""

from __future__ import annotations

import json
import time
import uuid
from collections import Counter
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from src.router import ModelTier


class OpenAIMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]]]


class OpenAIChatCompletionRequest(BaseModel):
    model: str = "auto"
    messages: List[OpenAIMessage] = Field(..., min_length=1)
    stream: bool = False
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    response_format: Optional[Dict[str, Any]] = None
    required_keys: Optional[List[str]] = None
    allow_escalation: bool = True


class GatewayMetrics:
    """Lightweight thread-safe/async-safe in-memory telemetry summary."""

    def __init__(self) -> None:
        self.total_requests: int = 0
        self.blocked_requests: int = 0
        self.error_requests: int = 0
        self.escalated_requests: int = 0
        self.overflowed_requests: int = 0
        self.total_cost_usd: float = 0.0
        self.total_latency_ms: float = 0.0
        self.tier_counts: Counter[str] = Counter()

    def record(self, resp: Any) -> None:
        self.total_requests += 1
        if resp.status == "blocked":
            self.blocked_requests += 1
        elif resp.status == "error":
            self.error_requests += 1
        if resp.escalated:
            self.escalated_requests += 1
        if resp.overflowed:
            self.overflowed_requests += 1
        if resp.tier_used:
            self.tier_counts[resp.tier_used] += 1
        self.total_cost_usd += float(resp.cost_usd or 0.0)
        self.total_latency_ms += float(resp.latency_ms or 0.0)

    def snapshot(self) -> Dict[str, Any]:
        avg_lat = self.total_latency_ms / self.total_requests if self.total_requests else 0.0
        return {
            "total_requests": self.total_requests,
            "blocked_requests": self.blocked_requests,
            "error_requests": self.error_requests,
            "escalated_requests": self.escalated_requests,
            "overflowed_requests": self.overflowed_requests,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "avg_latency_ms": round(avg_lat, 2),
            "tier_counts": dict(self.tier_counts),
        }


def _extract_text(content: Union[str, List[Dict[str, Any]]]) -> str:
    if isinstance(content, str):
        return content
    parts: List[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text", "")))
    return "\n".join(parts)


def build_prompt_from_messages(messages: List[OpenAIMessage]) -> str:
    """Preserve single-user prompt verbatim so Tier-0 deterministic routing works cleanly."""
    if len(messages) == 1 and messages[0].role == "user":
        return _extract_text(messages[0].content).strip()
    lines = [f"{m.role}: {_extract_text(m.content)}" for m in messages]
    return "\n".join(lines).strip()


def resolve_force_tier(model_name: str) -> Optional[ModelTier]:
    norm = (model_name or "auto").strip().lower()
    if norm in ("", "auto", "gateway", "ai-ops-gateway"):
        return None
    if norm in ("tier-0", "tier_0", "tier_0_rule", "ast", "rule"):
        return ModelTier.TIER_0_RULE
    if norm in ("tier-1", "tier_1", "tier_1_local_7b", "local", "qwen2.5-coder:7b"):
        return ModelTier.TIER_1_LOCAL_7B
    if norm in ("tier-2", "tier_2", "tier_2_cloud", "cloud", "gemini", "gemini-3.5-flash-lite"):
        return ModelTier.TIER_2_CLOUD
    return None


def to_openai_response(resp: Any, completion_id: str, created: int) -> Dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": resp.tier_used or "ai-ops-gateway",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": resp.answer},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens,
            "total_tokens": resp.tokens_estimated,
        },
        "x_gateway": {
            "tier_used": resp.tier_used,
            "route_reason": resp.route_reason,
            "escalated": resp.escalated,
            "overflowed": resp.overflowed,
            "pii_redacted": resp.pii_redacted,
            "cost_usd": resp.cost_usd,
            "latency_ms": resp.latency_ms,
            "cached": resp.cached,
        },
    }


async def stream_openai_chunks(resp: Any, completion_id: str, created: int) -> AsyncIterator[str]:
    model = resp.tier_used or "ai-ops-gateway"
    text = resp.answer or ""
    chunk_size = 24

    first = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(first, ensure_ascii=False)}\n\n"

    for i in range(0, len(text), chunk_size):
        piece = text[i : i + chunk_size]
        chunk = {
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
        }
        yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

    end = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(end, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


def create_openai_router(get_engine_fn: Callable[[], Any], metrics: GatewayMetrics) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/models")
    def list_models() -> Dict[str, Any]:
        now = int(time.time())
        models = ["auto", "tier-0-ast", "tier-1-local-7b", "tier-2-cloud"]
        return {
            "object": "list",
            "data": [
                {"id": m, "object": "model", "created": now, "owned_by": "ai-ops-gateway"}
                for m in models
            ],
        }

    @router.get("/metrics")
    def get_metrics() -> Dict[str, Any]:
        return metrics.snapshot()

    @router.post("/v1/chat/completions")
    async def chat_completions(req: OpenAIChatCompletionRequest) -> Any:
        from src.gateway import ChatRequest

        prompt = build_prompt_from_messages(req.messages)
        if not prompt:
            raise HTTPException(status_code=400, detail="Empty prompt in messages.")

        require_json = bool(
            req.response_format and req.response_format.get("type") == "json_object"
        )
        gw_req = ChatRequest(
            prompt=prompt,
            require_json=require_json,
            required_keys=req.required_keys,
            force_tier=resolve_force_tier(req.model),
            allow_escalation=req.allow_escalation,
        )
        resp = await get_engine_fn().handle_async(gw_req)
        metrics.record(resp)

        if resp.status == "blocked":
            raise HTTPException(
                status_code=400,
                detail={
                    "message": resp.answer,
                    "type": "guardrail_blocked",
                    "code": resp.block_reason,
                },
            )
        if resp.status == "error":
            raise HTTPException(
                status_code=502,
                detail={"message": resp.answer, "type": "backend_error"},
            )

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        created = int(time.time())
        if req.stream:
            return StreamingResponse(
                stream_openai_chunks(resp, completion_id, created),
                media_type="text/event-stream",
            )
        return to_openai_response(resp, completion_id, created)

    return router
