"""Live concurrency load test: Local-Only Queueing vs. Semaphore + Cloud Overflow."""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from src.backends import CachedLiveBackend, call_ollama
from src.gateway import ChatRequest, GatewayEngine
from src.run_ablation_live import percentile


ROOT = Path(__file__).resolve().parent.parent
OUT_JSON = ROOT / "data" / "concurrency_results.json"
DASHBOARD_JS = ROOT / "dashboard" / "concurrency-data.js"

BURST_PROMPTS = [
    "Summarize in one sentence why unit tests matter.",
    "Summarize in one sentence why API rate limiting matters.",
    "Summarize in one sentence why structured logging matters.",
    "Summarize in one sentence why schema validation matters.",
    "Summarize in one sentence why database indexing matters.",
    "Summarize in one sentence why caching reduces latency.",
]


async def run_burst(mode_name: str, n_concurrent: int, queue_timeout_sec: float, backend: CachedLiveBackend) -> Dict[str, Any]:
    engine = GatewayEngine(
        backend_fn=backend,
        trace_path=None,
        max_local_concurrency=1,
        queue_timeout_sec=queue_timeout_sec,
    )
    prompts = [BURST_PROMPTS[i % len(BURST_PROMPTS)] for i in range(n_concurrent)]
    t0 = time.perf_counter()
    resps = await asyncio.gather(*[engine.handle_async(ChatRequest(prompt=p)) for p in prompts])
    wall_ms = round((time.perf_counter() - t0) * 1000, 2)

    lats = [r.latency_ms for r in resps]
    overflows = sum(1 for r in resps if r.overflowed)
    errors = sum(1 for r in resps if r.status != "ok")
    cost = round(sum(r.cost_usd for r in resps), 6)

    return {
        "mode": mode_name,
        "concurrency": n_concurrent,
        "wall_ms": wall_ms,
        "throughput_rps": round(n_concurrent / (wall_ms / 1000.0), 2),
        "p50_ms": percentile(lats, 0.50),
        "p95_ms": percentile(lats, 0.95),
        "overflow_count": overflows,
        "errors": errors,
        "total_cost_usd": cost,
    }


async def main_async() -> None:
    print("Warming up local Ollama (qwen2.5-coder:7b)...")
    warm = call_ollama("Say ok.", model="qwen2.5-coder:7b", timeout_sec=60.0)
    print(f"Warmup: {warm.latency_ms} ms (error={warm.error})")

    live_backend = CachedLiveBackend(use_cache=False)
    results: List[Dict[str, Any]] = []

    for n in (4, 6):
        q_res = await run_burst("Local_Queue_NoOverflow", n, queue_timeout_sec=60.0, backend=live_backend)
        o_res = await run_burst("Semaphore_Cloud_Overflow", n, queue_timeout_sec=0.3, backend=live_backend)
        results.extend([q_res, o_res])

    OUT_JSON.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    DASHBOARD_JS.write_text(f"window.CONCURRENCY_REPORT = {json.dumps(results, ensure_ascii=False, indent=2)};\n", encoding="utf-8")

    for r in results:
        print(
            f"N={r['concurrency']} | {r['mode']:24s} | Wall={r['wall_ms']:7.1f}ms "
            f"| P50={r['p50_ms']:7.1f}ms | P95={r['p95_ms']:7.1f}ms "
            f"| RPS={r['throughput_rps']:.2f} | Overflows={r['overflow_count']} | Cost=${r['total_cost_usd']:.5f}"
        )


if __name__ == "__main__":
    asyncio.run(main_async())
