"""Unified AI Ops Gateway: Guardrails -> Router -> Live/Mock Execution + Circuit Breaker -> Telemetry."""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from fastapi import FastAPI
from pydantic import BaseModel, Field

from src.backends import BackendResult, CachedLiveBackend
from src.guardrails import inspect_input, inspect_output, strip_json_fences, validate_json_output
from src.openai_compat import GatewayMetrics, create_openai_router
from src.router import (
    TIER_BASE_LATENCY_MS,
    TIER_PRICING_PER_1K_TOKENS,
    ModelTier,
    route_request,
)


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_TRACE_PATH = ROOT / "data" / "traces.jsonl"


class ChatRequest(BaseModel):
    prompt: str = Field(..., min_length=1)
    require_json: bool = False
    required_keys: Optional[List[str]] = None
    force_tier: Optional[ModelTier] = None
    allow_escalation: bool = True


class ChatResponse(BaseModel):
    status: str  # "ok" | "blocked" | "error"
    answer: str
    tier_used: Optional[str] = None
    route_reason: Optional[str] = None
    escalated: bool = False
    overflowed: bool = False
    pii_redacted: List[str] = Field(default_factory=list)
    block_reason: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_estimated: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    cached: bool = False


def _default_simulator(prompt: str, tier: ModelTier, require_json: bool, required_keys: Optional[List[str]]) -> BackendResult:
    """Offline deterministic simulator for CI regression & demos without API keys."""
    if require_json and required_keys:
        if tier == ModelTier.TIER_1_LOCAL_7B and len(required_keys) >= 3:
            text = "{invalid_json_from_7b"
        else:
            text = json.dumps({k: f"verified_{k}" for k in required_keys}, ensure_ascii=False)
    else:
        text = f"[{tier.value}] Processed: {prompt[:80]}"

    p_tok = max(20, len(prompt) // 3)
    c_tok = max(20, len(text) // 3)
    return BackendResult(
        text=text,
        prompt_tokens=p_tok,
        completion_tokens=c_tok,
        latency_ms=TIER_BASE_LATENCY_MS[tier],
    )


def _normalize_result(raw: Union[BackendResult, str], prompt: str, tier: ModelTier) -> BackendResult:
    if isinstance(raw, BackendResult):
        return raw
    p_tok = max(20, len(prompt) // 3)
    c_tok = max(20, len(raw) // 3)
    return BackendResult(
        text=raw,
        prompt_tokens=p_tok,
        completion_tokens=c_tok,
        latency_ms=TIER_BASE_LATENCY_MS[tier],
    )


class GatewayEngine:
    """Orchestrates input guardrails, cost-aware routing, circuit-breaker fallback, and tracing."""

    def __init__(
        self,
        backend_fn: Optional[Callable[[str, ModelTier, bool, Optional[List[str]]], Any]] = None,
        live: bool = False,
        trace_path: Optional[Path] = DEFAULT_TRACE_PATH,
        max_local_concurrency: int = 2,
        queue_timeout_sec: float = 0.25,
    ):
        if backend_fn is not None:
            self.backend_fn = backend_fn
        elif live:
            self.backend_fn = CachedLiveBackend()
        else:
            self.backend_fn = _default_simulator
        self.trace_path = trace_path
        self.max_local_concurrency = max_local_concurrency
        self.queue_timeout_sec = queue_timeout_sec
        self._local_sem: Optional[asyncio.Semaphore] = None

    def _write_trace(self, resp: ChatResponse) -> None:
        if not self.trace_path:
            return
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "status": resp.status,
            "tier_used": resp.tier_used,
            "route_reason": resp.route_reason,
            "escalated": resp.escalated,
            "pii_redacted": resp.pii_redacted,
            "tokens": resp.tokens_estimated,
            "cost_usd": resp.cost_usd,
            "latency_ms": resp.latency_ms,
            "cached": resp.cached,
        }
        with self.trace_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def handle(self, req: ChatRequest) -> ChatResponse:
        t0 = time.perf_counter()
        guard = inspect_input(req.prompt)
        if guard.blocked:
            resp = ChatResponse(
                status="blocked",
                answer="Request blocked by security guardrail.",
                block_reason=guard.block_reason,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
            self._write_trace(resp)
            return resp

        decision = route_request(guard.sanitized_prompt, require_json=req.require_json)
        tier = req.force_tier or decision.tier
        reason = "FORCED_TIER" if req.force_tier else decision.reason

        if tier == ModelTier.TIER_0_RULE and decision.deterministic_answer is not None:
            resp = ChatResponse(
                status="ok",
                answer=decision.deterministic_answer,
                tier_used=tier.value,
                route_reason=reason,
                pii_redacted=guard.pii_redacted,
                latency_ms=round((time.perf_counter() - t0) * 1000, 3),
            )
            self._write_trace(resp)
            return resp

        res = _normalize_result(
            self.backend_fn(guard.sanitized_prompt, tier, req.require_json, req.required_keys),
            guard.sanitized_prompt,
            tier,
        )
        escalated = False
        total_p_tok = res.prompt_tokens
        total_c_tok = res.completion_tokens
        total_latency = res.latency_ms
        total_cost = ((total_p_tok + total_c_tok) / 1000.0) * TIER_PRICING_PER_1K_TOKENS[tier]

        # Circuit breaker: if Tier-1 fails/times out, gracefully fall back to Tier-2
        if res.error and tier == ModelTier.TIER_1_LOCAL_7B:
            escalated = True
            tier = ModelTier.TIER_2_CLOUD
            reason = f"{reason}->FALLBACK_TIER1_ERROR"
            res = _normalize_result(
                self.backend_fn(guard.sanitized_prompt, tier, req.require_json, req.required_keys),
                guard.sanitized_prompt,
                tier,
            )
            total_p_tok += res.prompt_tokens
            total_c_tok += res.completion_tokens
            total_latency += res.latency_ms
            total_cost += ((res.prompt_tokens + res.completion_tokens) / 1000.0) * TIER_PRICING_PER_1K_TOKENS[tier]

        if res.error:
            resp = ChatResponse(
                status="error",
                answer=res.error,
                tier_used=tier.value,
                route_reason=reason,
                escalated=escalated,
                pii_redacted=guard.pii_redacted,
                latency_ms=round(total_latency, 2),
            )
            self._write_trace(resp)
            return resp

        output = strip_json_fences(res.text) if req.require_json else res.text
        if req.require_json:
            valid, _ = validate_json_output(output, req.required_keys)
            if not valid and req.allow_escalation and tier == ModelTier.TIER_1_LOCAL_7B:
                escalated = True
                tier = ModelTier.TIER_2_CLOUD
                reason = f"{reason}->ESCALATED_SCHEMA_FAIL"
                esc_res = _normalize_result(
                    self.backend_fn(guard.sanitized_prompt, tier, req.require_json, req.required_keys),
                    guard.sanitized_prompt,
                    tier,
                )
                if esc_res.error:
                    resp = ChatResponse(
                        status="error",
                        answer=esc_res.error,
                        tier_used=tier.value,
                        route_reason=reason,
                        escalated=escalated,
                        pii_redacted=guard.pii_redacted,
                        latency_ms=round(total_latency + esc_res.latency_ms, 2),
                    )
                    self._write_trace(resp)
                    return resp
                output = strip_json_fences(esc_res.text)
                total_p_tok += esc_res.prompt_tokens
                total_c_tok += esc_res.completion_tokens
                total_latency += esc_res.latency_ms
                total_cost += ((esc_res.prompt_tokens + esc_res.completion_tokens) / 1000.0) * TIER_PRICING_PER_1K_TOKENS[tier]

        output, out_pii = inspect_output(output)
        all_pii = sorted(set(guard.pii_redacted + out_pii))

        resp = ChatResponse(
            status="ok",
            answer=output,
            tier_used=tier.value,
            route_reason=reason,
            escalated=escalated,
            pii_redacted=all_pii,
            prompt_tokens=total_p_tok,
            completion_tokens=total_c_tok,
            tokens_estimated=total_p_tok + total_c_tok,
            cost_usd=round(total_cost, 6),
            latency_ms=round(total_latency, 2),
            cached=res.cached,
        )
        self._write_trace(resp)
        return resp

    async def handle_async(self, req: ChatRequest) -> ChatResponse:
        """Async request handler with local GPU concurrency semaphore and cloud overflow."""
        t0 = time.perf_counter()
        guard = inspect_input(req.prompt)
        if guard.blocked:
            resp = ChatResponse(
                status="blocked",
                answer="Request blocked by security guardrail.",
                block_reason=guard.block_reason,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
            self._write_trace(resp)
            return resp

        decision = route_request(guard.sanitized_prompt, require_json=req.require_json)
        tier = req.force_tier or decision.tier
        reason = "FORCED_TIER" if req.force_tier else decision.reason

        if tier == ModelTier.TIER_0_RULE and decision.deterministic_answer is not None:
            resp = ChatResponse(
                status="ok",
                answer=decision.deterministic_answer,
                tier_used=tier.value,
                route_reason=reason,
                pii_redacted=guard.pii_redacted,
                latency_ms=round((time.perf_counter() - t0) * 1000, 3),
            )
            self._write_trace(resp)
            return resp

        overflowed = False
        acquired_local = False
        if self._local_sem is None:
            self._local_sem = asyncio.Semaphore(self.max_local_concurrency)

        if tier == ModelTier.TIER_1_LOCAL_7B:
            try:
                await asyncio.wait_for(self._local_sem.acquire(), timeout=self.queue_timeout_sec)
                acquired_local = True
            except asyncio.TimeoutError:
                if req.force_tier is None and req.allow_escalation:
                    overflowed = True
                    tier = ModelTier.TIER_2_CLOUD
                    reason = f"{reason}->OVERFLOW_CLOUD_QUEUE_FULL"
                else:
                    await self._local_sem.acquire()
                    acquired_local = True

        try:
            res = _normalize_result(
                await asyncio.to_thread(
                    self.backend_fn, guard.sanitized_prompt, tier, req.require_json, req.required_keys
                ),
                guard.sanitized_prompt,
                tier,
            )
        finally:
            if acquired_local:
                self._local_sem.release()

        escalated = False
        total_p_tok = res.prompt_tokens
        total_c_tok = res.completion_tokens
        total_cost = ((total_p_tok + total_c_tok) / 1000.0) * TIER_PRICING_PER_1K_TOKENS[tier]

        if res.error and tier == ModelTier.TIER_1_LOCAL_7B and req.allow_escalation:
            escalated = True
            tier = ModelTier.TIER_2_CLOUD
            reason = f"{reason}->FALLBACK_TIER1_ERROR"
            res = _normalize_result(
                await asyncio.to_thread(
                    self.backend_fn, guard.sanitized_prompt, tier, req.require_json, req.required_keys
                ),
                guard.sanitized_prompt,
                tier,
            )
            total_p_tok += res.prompt_tokens
            total_c_tok += res.completion_tokens
            total_cost += ((res.prompt_tokens + res.completion_tokens) / 1000.0) * TIER_PRICING_PER_1K_TOKENS[tier]

        if res.error:
            resp = ChatResponse(
                status="error",
                answer=res.error,
                tier_used=tier.value,
                route_reason=reason,
                escalated=escalated,
                overflowed=overflowed,
                pii_redacted=guard.pii_redacted,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
            self._write_trace(resp)
            return resp

        output = strip_json_fences(res.text) if req.require_json else res.text
        if req.require_json:
            valid, _ = validate_json_output(output, req.required_keys)
            if not valid and req.allow_escalation and tier == ModelTier.TIER_1_LOCAL_7B:
                escalated = True
                tier = ModelTier.TIER_2_CLOUD
                reason = f"{reason}->ESCALATED_SCHEMA_FAIL"
                esc_res = _normalize_result(
                    await asyncio.to_thread(
                        self.backend_fn, guard.sanitized_prompt, tier, req.require_json, req.required_keys
                    ),
                    guard.sanitized_prompt,
                    tier,
                )
                if not esc_res.error:
                    output = strip_json_fences(esc_res.text)
                    total_p_tok += esc_res.prompt_tokens
                    total_c_tok += esc_res.completion_tokens
                    total_cost += ((esc_res.prompt_tokens + esc_res.completion_tokens) / 1000.0) * TIER_PRICING_PER_1K_TOKENS[tier]

        output, out_pii = inspect_output(output)
        all_pii = sorted(set(guard.pii_redacted + out_pii))

        resp = ChatResponse(
            status="ok",
            answer=output,
            tier_used=tier.value,
            route_reason=reason,
            escalated=escalated,
            overflowed=overflowed,
            pii_redacted=all_pii,
            prompt_tokens=total_p_tok,
            completion_tokens=total_c_tok,
            tokens_estimated=total_p_tok + total_c_tok,
            cost_usd=round(total_cost, 6),
            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            cached=res.cached,
        )
        self._write_trace(resp)
        return resp


app = FastAPI(title="AI Ops Gateway", version="0.3.0")
_live_mode = os.environ.get("AI_OPS_LIVE", "").strip() == "1"
_max_local = int(os.environ.get("AI_OPS_MAX_LOCAL_CONCURRENCY", "2"))
_engine = GatewayEngine(live=_live_mode, max_local_concurrency=_max_local)
_metrics = GatewayMetrics()

app.include_router(create_openai_router(lambda: _engine, _metrics))


@app.get("/health")
def health() -> Dict[str, Any]:
    return {
        "status": "healthy",
        "mode": "live" if _live_mode else "simulator",
        "max_local_concurrency": _max_local,
    }


@app.post("/v1/chat", response_model=ChatResponse)
async def chat(req: ChatRequest) -> ChatResponse:
    resp = await _engine.handle_async(req)
    _metrics.record(resp)
    return resp
