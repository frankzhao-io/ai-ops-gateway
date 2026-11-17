"""Offline/Live Eval CI runner comparing Baseline (All-Cloud) vs Routed+Gated Gateway."""

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List

from src.gateway import ChatRequest, GatewayEngine
from src.guardrails import validate_json_output
from src.router import TIER_BASE_LATENCY_MS, TIER_PRICING_PER_1K_TOKENS, ModelTier


ROOT = Path(__file__).resolve().parent.parent
GOLDEN_PATH = ROOT / "data" / "golden_set.json"
DASHBOARD_DATA_PATH = ROOT / "dashboard" / "eval-data.js"


def percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(p * len(ordered)) - 1))
    return round(ordered[idx], 2)


def evaluate_case(case: Dict[str, Any], engine: GatewayEngine, baseline: bool = False) -> Dict[str, Any]:
    req = ChatRequest(
        prompt=case["prompt"],
        require_json=bool(case.get("require_json", False)),
        required_keys=case.get("required_keys"),
    )

    if baseline:
        # Unprotected all-cloud baseline: no guardrails, always calls Tier-2
        est_tokens = max(75, len(case["prompt"]) // 2)
        cost = round((est_tokens / 1000.0) * TIER_PRICING_PER_1K_TOKENS[ModelTier.TIER_2_CLOUD], 6)
        latency = TIER_BASE_LATENCY_MS[ModelTier.TIER_2_CLOUD]
        # Baseline fails security injection cases because it doesn't block them
        passed = case["expected_status"] != "blocked"
        return {
            "id": case["id"],
            "category": case["category"],
            "passed": passed,
            "tier_used": ModelTier.TIER_2_CLOUD.value,
            "cost_usd": cost,
            "latency_ms": latency,
            "status": "ok",
            "escalated": False,
        }

    resp = engine.handle(req)
    passed = resp.status == case["expected_status"]
    if passed and "expected_exact" in case:
        passed = resp.answer == case["expected_exact"]
    if passed and "expect_pii" in case:
        passed = all(p in resp.pii_redacted for p in case["expect_pii"])
    if passed and req.require_json:
        ok, _ = validate_json_output(resp.answer, req.required_keys)
        passed = ok

    return {
        "id": case["id"],
        "category": case["category"],
        "passed": passed,
        "tier_used": resp.tier_used or "blocked",
        "cost_usd": resp.cost_usd,
        "latency_ms": resp.latency_ms,
        "status": resp.status,
        "escalated": resp.escalated,
    }


def summarize_arm(name: str, rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    pass_count = sum(1 for r in rows if r["passed"])
    total_cost = round(sum(r["cost_usd"] for r in rows), 6)
    latencies = [r["latency_ms"] for r in rows]
    tier_counts: Dict[str, int] = {}
    for r in rows:
        tier_counts[r["tier_used"]] = tier_counts.get(r["tier_used"], 0) + 1

    return {
        "arm": name,
        "total_cases": n,
        "pass_count": pass_count,
        "pass_rate": round(pass_count / n, 4) if n else 0.0,
        "total_cost_usd": total_cost,
        "avg_latency_ms": round(sum(latencies) / n, 2) if n else 0.0,
        "p95_latency_ms": percentile(latencies, 0.95),
        "tier_counts": tier_counts,
        "escalations": sum(1 for r in rows if r["escalated"]),
    }


def run_eval(live: bool = False) -> Dict[str, Any]:
    cases = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    engine = GatewayEngine(live=live, trace_path=None)

    baseline_rows = [evaluate_case(c, engine, baseline=True) for c in cases]
    gated_rows = [evaluate_case(c, engine, baseline=False) for c in cases]

    base_summary = summarize_arm("Baseline (All-Cloud, Unprotected)", baseline_rows)
    gated_summary = summarize_arm("Routed + Gated Gateway (Ours)", gated_rows)

    cost_saved_pct = 0.0
    if base_summary["total_cost_usd"] > 0:
        cost_saved_pct = round(
            (1.0 - gated_summary["total_cost_usd"] / base_summary["total_cost_usd"]) * 100.0,
            2,
        )

    report = {
        "mode": "live" if live else "mock",
        "baseline": base_summary,
        "gated": gated_summary,
        "cost_saved_pct": cost_saved_pct,
        "cases": gated_rows,
    }

    DASHBOARD_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DATA_PATH.write_text(
        f"window.EVAL_REPORT = {json.dumps(report, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run AI Ops Gateway Eval CI")
    parser.add_argument("--live", action="store_true", help="Use live Ollama + Gemini backends with JSONL cache")
    args = parser.parse_args()
    rep = run_eval(live=args.live)
    print(f"=== AI Ops Gateway Eval CI ({rep['mode']}) ===")
    print(f"Baseline : Pass={rep['baseline']['pass_count']}/{rep['baseline']['total_cases']} "
          f"({rep['baseline']['pass_rate']*100:.1f}%), Cost=${rep['baseline']['total_cost_usd']:.5f}, "
          f"P95={rep['baseline']['p95_latency_ms']}ms")
    print(f"Gateway  : Pass={rep['gated']['pass_count']}/{rep['gated']['total_cases']} "
          f"({rep['gated']['pass_rate']*100:.1f}%), Cost=${rep['gated']['total_cost_usd']:.5f}, "
          f"P95={rep['gated']['p95_latency_ms']}ms, Saved={rep['cost_saved_pct']}%")
