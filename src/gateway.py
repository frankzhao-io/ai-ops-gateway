"""Unified AI Ops Gateway: Guardrails -> Router -> Live/Mock Execution + Circuit Breaker -> Telemetry."""

import asyncio
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

from fastapi import FastAPI
from pydantic import BaseModel, Field

from src.auth_quota import TenantQuotaManager
from src.backends import BackendResult, CachedLiveBackend, stream_gemini, stream_ollama
from src.guardrails import GuardrailReport, inspect_input, inspect_output, strip_json_fences, validate_json_output
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
    messages: Optional[List[Dict[str, str]]] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
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
        self._sem_loop: Optional[asyncio.AbstractEventLoop] = None
        self._trace_lock = threading.Lock()

    def _inspect_request(self, req: ChatRequest) -> tuple:
        guard = inspect_input(req.prompt)
        if guard.blocked:
            return guard, None
        pii = list(guard.pii_redacted)
        sanitized_messages = None
        if req.messages:
            sanitized_messages = []
            for m in req.messages:
                content = m.get("content", "")
                mg = inspect_input(content)
                if mg.blocked:
                    return GuardrailReport(blocked=True, block_reason=mg.block_reason), None
                pii.extend(mg.pii_redacted)
                sanitized_messages.append({"role": m.get("role", "user"), "content": mg.sanitized_prompt})
        return (
            GuardrailReport(
                blocked=False,
                pii_redacted=sorted(set(pii)),
                sanitized_prompt=guard.sanitized_prompt,
            ),
            sanitized_messages,
        )

    def _call_backend(
        self,
        prompt: str,
        tier: ModelTier,
        req: ChatRequest,
        sanitized_messages: Optional[List[Dict[str, str]]] = None,
    ) -> Any:
        if isinstance(self.backend_fn, CachedLiveBackend):
            return self.backend_fn(
                prompt,
                tier,
                req.require_json,
                req.required_keys,
                messages=sanitized_messages if sanitized_messages is not None else req.messages,
                temperature=req.temperature,
                max_tokens=req.max_tokens,
            )
        return self.backend_fn(prompt, tier, req.require_json, req.required_keys)

    def _write_trace(self, resp: ChatResponse) -> None:
        if not self.trace_path:
            return
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
        with self._trace_lock:
            self.trace_path.parent.mkdir(parents=True, exist_ok=True)
            with self.trace_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def handle(self, req: ChatRequest) -> ChatResponse:
        t0 = time.perf_counter()
        guard, sanitized_messages = self._inspect_request(req)
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
            self._call_backend(guard.sanitized_prompt, tier, req, sanitized_messages),
            guard.sanitized_prompt,
            tier,
        )
        escalated = False
        final_cached = res.cached
        total_p_tok = res.prompt_tokens
        total_c_tok = res.completion_tokens
        total_latency = res.latency_ms
        total_cost = ((total_p_tok + total_c_tok) / 1000.0) * TIER_PRICING_PER_1K_TOKENS[tier]

        # Circuit breaker: if Tier-1 fails/times out, gracefully fall back to Tier-2
        if res.error and tier == ModelTier.TIER_1_LOCAL_7B and req.allow_escalation:
            escalated = True
            tier = ModelTier.TIER_2_CLOUD
            reason = f"{reason}->FALLBACK_TIER1_ERROR"
            res = _normalize_result(
                self._call_backend(guard.sanitized_prompt, tier, req, sanitized_messages),
                guard.sanitized_prompt,
                tier,
            )
            final_cached = res.cached
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
                    self._call_backend(guard.sanitized_prompt, tier, req, sanitized_messages),
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
                final_cached = esc_res.cached
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
            cached=final_cached,
        )
        self._write_trace(resp)
        return resp

    async def handle_async(self, req: ChatRequest) -> ChatResponse:
        """Async request handler with local GPU concurrency semaphore and cloud overflow."""
        t0 = time.perf_counter()
        guard, sanitized_messages = self._inspect_request(req)
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
        cur_loop = asyncio.get_running_loop()
        if self._local_sem is None or self._sem_loop is not cur_loop:
            self._local_sem = asyncio.Semaphore(self.max_local_concurrency)
            self._sem_loop = cur_loop

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
                await asyncio.to_thread(self._call_backend, guard.sanitized_prompt, tier, req, sanitized_messages),
                guard.sanitized_prompt,
                tier,
            )
        finally:
            if acquired_local:
                self._local_sem.release()

        escalated = False
        final_cached = res.cached
        total_p_tok = res.prompt_tokens
        total_c_tok = res.completion_tokens
        total_cost = ((total_p_tok + total_c_tok) / 1000.0) * TIER_PRICING_PER_1K_TOKENS[tier]

        if res.error and tier == ModelTier.TIER_1_LOCAL_7B and req.allow_escalation:
            escalated = True
            tier = ModelTier.TIER_2_CLOUD
            reason = f"{reason}->FALLBACK_TIER1_ERROR"
            res = _normalize_result(
                await asyncio.to_thread(self._call_backend, guard.sanitized_prompt, tier, req, sanitized_messages),
                guard.sanitized_prompt,
                tier,
            )
            final_cached = res.cached
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
                    await asyncio.to_thread(self._call_backend, guard.sanitized_prompt, tier, req, sanitized_messages),
                    guard.sanitized_prompt,
                    tier,
                )
                total_p_tok += esc_res.prompt_tokens
                total_c_tok += esc_res.completion_tokens
                total_cost += ((esc_res.prompt_tokens + esc_res.completion_tokens) / 1000.0) * TIER_PRICING_PER_1K_TOKENS[tier]
                if esc_res.error:
                    resp = ChatResponse(
                        status="error",
                        answer=esc_res.error,
                        tier_used=tier.value,
                        route_reason=reason,
                        escalated=escalated,
                        overflowed=overflowed,
                        pii_redacted=guard.pii_redacted,
                        prompt_tokens=total_p_tok,
                        completion_tokens=total_c_tok,
                        tokens_estimated=total_p_tok + total_c_tok,
                        cost_usd=round(total_cost, 6),
                        latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                    )
                    self._write_trace(resp)
                    return resp
                final_cached = esc_res.cached
                output = strip_json_fences(esc_res.text)

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
            cached=final_cached,
        )
        self._write_trace(resp)
        return resp

    async def stream_async(self, req: ChatRequest):
        """Async generator yielding (delta_text, final_ChatResponse_or_None) with true upstream streaming."""
        t0 = time.perf_counter()
        guard, sanitized_messages = self._inspect_request(req)
        if guard.blocked:
            resp = ChatResponse(
                status="blocked",
                answer="Request blocked by security guardrail.",
                block_reason=guard.block_reason,
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
            self._write_trace(resp)
            yield "", resp
            return

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
            yield decision.deterministic_answer, resp
            return

        if isinstance(self.backend_fn, CachedLiveBackend):
            overflowed = False
            acquired_local = False
            cur_loop = asyncio.get_running_loop()
            if self._local_sem is None or self._sem_loop is not cur_loop:
                self._local_sem = asyncio.Semaphore(self.max_local_concurrency)
                self._sem_loop = cur_loop

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

            q: asyncio.Queue = asyncio.Queue()
            collected: List[str] = []
            usage_totals = {"prompt_tokens": 0, "completion_tokens": 0}
            active_messages = sanitized_messages if sanitized_messages is not None else req.messages

            def _worker() -> None:
                try:
                    if tier == ModelTier.TIER_1_LOCAL_7B:
                        it = stream_ollama(
                            guard.sanitized_prompt,
                            model=self.backend_fn.ollama_model,
                            require_json=req.require_json,
                            required_keys=req.required_keys,
                            messages=active_messages,
                            temperature=req.temperature,
                            max_tokens=req.max_tokens,
                        )
                    else:
                        it = stream_gemini(
                            guard.sanitized_prompt,
                            model=self.backend_fn.gemini_model,
                            require_json=req.require_json,
                            required_keys=req.required_keys,
                            messages=active_messages,
                            temperature=req.temperature,
                            max_tokens=req.max_tokens,
                        )
                    for piece, u in it:
                        if piece:
                            cur_loop.call_soon_threadsafe(q.put_nowait, ("chunk", piece))
                        if u:
                            usage_totals["prompt_tokens"] = u.get("prompt_tokens", usage_totals["prompt_tokens"])
                            usage_totals["completion_tokens"] = u.get("completion_tokens", usage_totals["completion_tokens"])
                    cur_loop.call_soon_threadsafe(q.put_nowait, ("done", None))
                except Exception as exc:
                    cur_loop.call_soon_threadsafe(q.put_nowait, ("err", f"{type(exc).__name__}:{exc}"))

            threading.Thread(target=_worker, daemon=True).start()
            pending = ""
            redacted_pii = list(guard.pii_redacted)
            try:
                while True:
                    kind, payload = await q.get()
                    if kind == "chunk":
                        collected.append(payload)
                        pending += payload
                        if len(pending) > 48:
                            safe_prefix, out_pii = inspect_output(pending[:-48])
                            redacted_pii.extend(out_pii)
                            pending = pending[-48:]
                            if safe_prefix:
                                yield safe_prefix, None
                    elif kind == "err":
                        resp = ChatResponse(
                            status="error",
                            answer=payload,
                            tier_used=tier.value,
                            route_reason=reason,
                            overflowed=overflowed,
                            pii_redacted=sorted(set(redacted_pii)),
                            latency_ms=round((time.perf_counter() - t0) * 1000, 2),
                        )
                        self._write_trace(resp)
                        yield "", resp
                        return
                    else:
                        if pending:
                            tail_clean, out_pii = inspect_output(pending)
                            redacted_pii.extend(out_pii)
                            if tail_clean:
                                yield tail_clean, None
                        break
            finally:
                if acquired_local:
                    self._local_sem.release()

            full_text, out_pii = inspect_output("".join(collected))
            redacted_pii.extend(out_pii)
            p_tok = usage_totals["prompt_tokens"] or max(20, len(guard.sanitized_prompt) // 3)
            c_tok = usage_totals["completion_tokens"] or max(20, len(full_text) // 3)
            cost = ((p_tok + c_tok) / 1000.0) * TIER_PRICING_PER_1K_TOKENS[tier]
            resp = ChatResponse(
                status="ok",
                answer=full_text,
                tier_used=tier.value,
                route_reason=reason,
                overflowed=overflowed,
                pii_redacted=sorted(set(redacted_pii)),
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                tokens_estimated=p_tok + c_tok,
                cost_usd=round(cost, 6),
                latency_ms=round((time.perf_counter() - t0) * 1000, 2),
            )
            self._write_trace(resp)
            yield "", resp
            return

        # Simulator / custom non-streaming backend: execute & stream incremental word chunks
        resp = await self.handle_async(req)
        if resp.status != "ok":
            yield "", resp
            return
        text = resp.answer or ""
        step = 24
        for i in range(0, len(text), step):
            yield text[i : i + step], None
            await asyncio.sleep(0)
        yield "", resp


app = FastAPI(title="AI Ops Gateway", version="0.4.0")
_live_mode = os.environ.get("AI_OPS_LIVE", "").strip() == "1"
_max_local = int(os.environ.get("AI_OPS_MAX_LOCAL_CONCURRENCY", "2"))
_engine = GatewayEngine(live=_live_mode, max_local_concurrency=_max_local)
_metrics = GatewayMetrics()
_quota = TenantQuotaManager()

app.include_router(create_openai_router(lambda: _engine, _metrics, quota_manager=_quota))


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
