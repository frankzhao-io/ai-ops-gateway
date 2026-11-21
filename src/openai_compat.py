"""OpenAI-compatible API adapter (/v1/chat/completions, /v1/models, /metrics) for AI Ops Gateway."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import Counter
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Union

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from src.auth_quota import TenantQuotaManager
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
    """Thread-safe/async-safe in-memory telemetry summary."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.total_requests: int = 0
        self.blocked_requests: int = 0
        self.error_requests: int = 0
        self.escalated_requests: int = 0
        self.overflowed_requests: int = 0
        self.total_cost_usd: float = 0.0
        self.total_latency_ms: float = 0.0
        self.tier_counts: Counter[str] = Counter()

    def record(self, resp: Any) -> None:
        with self._lock:
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
        with self._lock:
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
    """Extract the latest user prompt for routing while keeping full messages[] for backends."""
    for m in reversed(messages):
        if m.role == "user":
            txt = _extract_text(m.content).strip()
            if txt:
                return txt
    lines = [f"{m.role}: {_extract_text(m.content)}" for m in messages]
    return "\n".join(lines).strip()


def normalize_messages(messages: List[OpenAIMessage]) -> List[Dict[str, str]]:
    return [{"role": m.role, "content": _extract_text(m.content)} for m in messages]


def resolve_force_tier(model_name: str) -> Optional[ModelTier]:
    norm = (model_name or "auto").strip().lower()
    if norm in ("", "auto", "gateway", "ai-ops-gateway"):
        return None
    if norm in ("tier-0", "tier-0-ast", "tier_0", "tier_0_rule", "tier0_deterministic", "ast", "rule"):
        return ModelTier.TIER_0_RULE
    if norm in ("tier-1", "tier-1-local-7b", "tier_1", "tier_1_local_7b", "tier1_local_7b", "local", "qwen2.5-coder:7b"):
        return ModelTier.TIER_1_LOCAL_7B
    if norm in ("tier-2", "tier-2-cloud", "tier_2", "tier_2_cloud", "tier2_cloud_frontier", "cloud", "gemini", "gemini-3.5-flash-lite"):
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


def create_openai_router(
    get_engine_fn: Callable[[], Any],
    metrics: GatewayMetrics,
    quota_manager: Optional[TenantQuotaManager] = None,
) -> APIRouter:
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
        snap = metrics.snapshot()
        if quota_manager is not None:
            snap["tenants"] = quota_manager.snapshot()
        return snap

    @router.post("/v1/chat/completions")
    async def chat_completions(
        req: OpenAIChatCompletionRequest,
        authorization: Optional[str] = Header(default=None),
    ) -> Any:
        from src.gateway import ChatRequest

        prompt = build_prompt_from_messages(req.messages)
        if not prompt:
            return JSONResponse(
                status_code=400,
                content={"error": {"message": "Empty prompt in messages.", "type": "invalid_request_error", "code": "empty_prompt"}},
            )

        tenant_name = "anonymous"
        if quota_manager is not None:
            api_key = TenantQuotaManager.extract_bearer_key(authorization)
            q = quota_manager.check_and_reserve(api_key)
            if not q.allowed:
                return JSONResponse(
                    status_code=q.status_code,
                    content={
                        "error": {
                            "message": q.message,
                            "type": q.error_type,
                            "code": q.error_code,
                        }
                    },
                )
            tenant_name = q.tenant_name

        fmt_type = req.response_format.get("type") if req.response_format else None
        require_json = fmt_type in ("json_object", "json_schema")
        gw_req = ChatRequest(
            prompt=prompt,
            messages=normalize_messages(req.messages),
            temperature=req.temperature,
            max_tokens=req.max_tokens,
            require_json=require_json,
            required_keys=req.required_keys,
            force_tier=resolve_force_tier(req.model),
            allow_escalation=req.allow_escalation,
        )

        completion_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
        created = int(time.time())

        if req.stream:
            engine = get_engine_fn()
            stream_gen = engine.stream_async(gw_req)
            try:
                first_piece, first_resp = await stream_gen.__anext__()
            except StopAsyncIteration:
                first_piece, first_resp = "", None

            if first_resp is not None and first_resp.status == "blocked":
                metrics.record(first_resp)
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": {
                            "message": first_resp.answer,
                            "type": "guardrail_blocked",
                            "code": first_resp.block_reason,
                        }
                    },
                )

            async def sse_events() -> AsyncIterator[str]:
                first_model = (first_resp.tier_used if first_resp else None) or "ai-ops-gateway"
                first_evt = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": first_model,
                    "choices": [{"index": 0, "delta": {"role": "assistant", "content": first_piece}, "finish_reason": None}],
                }
                yield f"data: {json.dumps(first_evt, ensure_ascii=False)}\n\n"
                final_resp = first_resp
                try:
                    if final_resp is None:
                        async for piece, resp_obj in stream_gen:
                            if piece:
                                evt = {
                                    "id": completion_id,
                                    "object": "chat.completion.chunk",
                                    "created": created,
                                    "model": "ai-ops-gateway",
                                    "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}],
                                }
                                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                            if resp_obj is not None:
                                final_resp = resp_obj
                    if final_resp is not None:
                        metrics.record(final_resp)
                        if quota_manager is not None:
                            quota_manager.record_spend(tenant_name, final_resp.cost_usd)
                        if final_resp.status == "error":
                            err_evt = {"error": {"message": final_resp.answer, "type": "backend_error", "code": "upstream_error"}}
                            yield f"data: {json.dumps(err_evt, ensure_ascii=False)}\n\n"
                            yield "data: [DONE]\n\n"
                            return
                except Exception as exc:
                    err_evt = {"error": {"message": f"{type(exc).__name__}: {exc}", "type": "stream_error", "code": "stream_failed"}}
                    yield f"data: {json.dumps(err_evt, ensure_ascii=False)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                finally:
                    await stream_gen.aclose()

                end_evt = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": (final_resp.tier_used if final_resp else None) or "ai-ops-gateway",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(end_evt, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

            return StreamingResponse(sse_events(), media_type="text/event-stream")

        resp = await get_engine_fn().handle_async(gw_req)
        metrics.record(resp)
        if quota_manager is not None:
            quota_manager.record_spend(tenant_name, resp.cost_usd)

        if resp.status == "blocked":
            return JSONResponse(
                status_code=400,
                content={
                    "error": {
                        "message": resp.answer,
                        "type": "guardrail_blocked",
                        "code": resp.block_reason,
                    }
                },
            )
        if resp.status == "error":
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": resp.answer,
                        "type": "backend_error",
                        "code": "upstream_error",
                    }
                },
            )

        return to_openai_response(resp, completion_id, created)

    return router
