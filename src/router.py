"""Cost-aware 3-tier model router: Tier-0 Deterministic -> Tier-1 Local 7B -> Tier-2 Cloud Frontier."""

import ast
import operator
import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ModelTier(str, Enum):
    TIER_0_RULE = "tier0_deterministic"
    TIER_1_LOCAL_7B = "tier1_local_7b"
    TIER_2_CLOUD = "tier2_cloud_frontier"


TIER_PRICING_PER_1K_TOKENS = {
    ModelTier.TIER_0_RULE: 0.0,
    ModelTier.TIER_1_LOCAL_7B: 0.00015,
    ModelTier.TIER_2_CLOUD: 0.0050,
}

TIER_BASE_LATENCY_MS = {
    ModelTier.TIER_0_RULE: 2.0,
    ModelTier.TIER_1_LOCAL_7B: 180.0,
    ModelTier.TIER_2_CLOUD: 880.0,
}

COMPLEX_KEYWORDS = re.compile(
    r"(?i)(architecture|trade-?off|multi-step|proof|refactor|root cause|compare|設計架構|權衡|多步驟|根因分析|重構)"
)
MATH_EXPR = re.compile(r"^\s*(?:calculate|compute|what is|計算)?\s*([\d\s+\-*/().]+)\s*\??\s*$", re.I)

_SAFE_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
}


def _eval_ast(node: ast.AST) -> float:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_eval_ast(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _SAFE_OPS:
        return _SAFE_OPS[type(node.op)](_eval_ast(node.left), _eval_ast(node.right))
    raise ValueError("Unsupported expression")


def try_deterministic_solve(prompt: str) -> Optional[str]:
    """Solve pure arithmetic expressions deterministically without calling an LLM."""
    match = MATH_EXPR.match(prompt)
    if not match:
        return None
    expr = match.group(1).strip()
    if not any(op in expr for op in "+-*/"):
        return None
    try:
        tree = ast.parse(expr, mode="eval")
        result = _eval_ast(tree.body)
        return str(int(result)) if result.is_integer() else f"{result:.4f}"
    except Exception:
        return None


@dataclass
class RouteDecision:
    tier: ModelTier
    reason: str
    deterministic_answer: Optional[str] = None


def route_request(prompt: str, require_json: bool = False) -> RouteDecision:
    """Select the cheapest tier capable of handling the prompt reliably."""
    det_answer = try_deterministic_solve(prompt)
    if det_answer is not None and not require_json:
        return RouteDecision(
            tier=ModelTier.TIER_0_RULE,
            reason="PURE_ARITHMETIC_DETERMINISTIC",
            deterministic_answer=det_answer,
        )

    if len(prompt) > 600 or COMPLEX_KEYWORDS.search(prompt):
        return RouteDecision(
            tier=ModelTier.TIER_2_CLOUD,
            reason="HIGH_COMPLEXITY_OR_LONG_CONTEXT",
        )

    return RouteDecision(
        tier=ModelTier.TIER_1_LOCAL_7B,
        reason="STANDARD_TASK_LOCAL_7B",
    )
