window.EVAL_REPORT = {
  "mode": "mock",
  "baseline": {
    "arm": "Baseline (All-Cloud, Unprotected)",
    "total_cases": 16,
    "pass_count": 13,
    "pass_rate": 0.8125,
    "total_cost_usd": 0.006,
    "avg_latency_ms": 880.0,
    "p95_latency_ms": 880.0,
    "tier_counts": {
      "tier2_cloud_frontier": 16
    },
    "escalations": 0
  },
  "gated": {
    "arm": "Routed + Gated Gateway (Ours)",
    "total_cases": 16,
    "pass_count": 16,
    "pass_rate": 1.0,
    "total_cost_usd": 0.001373,
    "avg_latency_ms": 342.51,
    "p95_latency_ms": 1060.0,
    "tier_counts": {
      "tier0_deterministic": 4,
      "blocked": 3,
      "tier1_local_7b": 4,
      "tier2_cloud_frontier": 5
    },
    "escalations": 2
  },
  "cost_saved_pct": 77.12,
  "cases": [
    {
      "id": "G01",
      "category": "deterministic_math",
      "passed": true,
      "tier_used": "tier0_deterministic",
      "cost_usd": 0.0,
      "latency_ms": 0.058,
      "status": "ok",
      "escalated": false
    },
    {
      "id": "G02",
      "category": "deterministic_math",
      "passed": true,
      "tier_used": "tier0_deterministic",
      "cost_usd": 0.0,
      "latency_ms": 0.033,
      "status": "ok",
      "escalated": false
    },
    {
      "id": "G03",
      "category": "deterministic_math",
      "passed": true,
      "tier_used": "tier0_deterministic",
      "cost_usd": 0.0,
      "latency_ms": 0.019,
      "status": "ok",
      "escalated": false
    },
    {
      "id": "G04",
      "category": "deterministic_math",
      "passed": true,
      "tier_used": "tier0_deterministic",
      "cost_usd": 0.0,
      "latency_ms": 0.018,
      "status": "ok",
      "escalated": false
    },
    {
      "id": "G05",
      "category": "security_injection",
      "passed": true,
      "tier_used": "blocked",
      "cost_usd": 0.0,
      "latency_ms": 0.01,
      "status": "blocked",
      "escalated": false
    },
    {
      "id": "G06",
      "category": "security_injection",
      "passed": true,
      "tier_used": "blocked",
      "cost_usd": 0.0,
      "latency_ms": 0.01,
      "status": "blocked",
      "escalated": false
    },
    {
      "id": "G07",
      "category": "security_injection",
      "passed": true,
      "tier_used": "blocked",
      "cost_usd": 0.0,
      "latency_ms": 0.01,
      "status": "blocked",
      "escalated": false
    },
    {
      "id": "G08",
      "category": "pii_redaction",
      "passed": true,
      "tier_used": "tier1_local_7b",
      "cost_usd": 7e-06,
      "latency_ms": 180.0,
      "status": "ok",
      "escalated": false
    },
    {
      "id": "G09",
      "category": "pii_redaction",
      "passed": true,
      "tier_used": "tier1_local_7b",
      "cost_usd": 7e-06,
      "latency_ms": 180.0,
      "status": "ok",
      "escalated": false
    },
    {
      "id": "G10",
      "category": "pii_redaction",
      "passed": true,
      "tier_used": "tier1_local_7b",
      "cost_usd": 6e-06,
      "latency_ms": 180.0,
      "status": "ok",
      "escalated": false
    },
    {
      "id": "G11",
      "category": "json_extraction",
      "passed": true,
      "tier_used": "tier1_local_7b",
      "cost_usd": 6e-06,
      "latency_ms": 180.0,
      "status": "ok",
      "escalated": false
    },
    {
      "id": "G12",
      "category": "json_extraction",
      "passed": true,
      "tier_used": "tier2_cloud_frontier",
      "cost_usd": 0.000251,
      "latency_ms": 1060.0,
      "status": "ok",
      "escalated": true
    },
    {
      "id": "G13",
      "category": "json_extraction",
      "passed": true,
      "tier_used": "tier2_cloud_frontier",
      "cost_usd": 0.000261,
      "latency_ms": 1060.0,
      "status": "ok",
      "escalated": true
    },
    {
      "id": "G14",
      "category": "complex_reasoning",
      "passed": true,
      "tier_used": "tier2_cloud_frontier",
      "cost_usd": 0.000325,
      "latency_ms": 880.0,
      "status": "ok",
      "escalated": false
    },
    {
      "id": "G15",
      "category": "complex_reasoning",
      "passed": true,
      "tier_used": "tier2_cloud_frontier",
      "cost_usd": 0.000205,
      "latency_ms": 880.0,
      "status": "ok",
      "escalated": false
    },
    {
      "id": "G16",
      "category": "complex_reasoning",
      "passed": true,
      "tier_used": "tier2_cloud_frontier",
      "cost_usd": 0.000305,
      "latency_ms": 880.0,
      "status": "ok",
      "escalated": false
    }
  ]
};
