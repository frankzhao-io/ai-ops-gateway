"""Per-tenant API key authentication, sliding-window RPM rate limiting, and USD budget caps."""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional


@dataclass
class TenantPolicy:
    key: str
    name: str
    rpm_limit: int = 60
    budget_usd: float = 10.0


@dataclass
class QuotaCheckResult:
    allowed: bool
    status_code: int = 200
    error_type: Optional[str] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    tenant_name: str = "anonymous"


def load_policies_from_env() -> List[TenantPolicy]:
    raw = os.environ.get("AI_OPS_API_KEYS", "").strip()
    if not raw:
        return []
    items = json.loads(raw)
    return [
        TenantPolicy(
            key=str(item["key"]),
            name=str(item.get("name", f"tenant-{idx}")),
            rpm_limit=int(item.get("rpm_limit", 60)),
            budget_usd=float(item.get("budget_usd", 10.0)),
        )
        for idx, item in enumerate(items)
    ]


class TenantQuotaManager:
    """Thread-safe sliding-window RPM limiter and cumulative USD budget enforcer."""

    def __init__(
        self,
        policies: Optional[List[TenantPolicy]] = None,
        enforce_auth: Optional[bool] = None,
        window_seconds: float = 60.0,
    ) -> None:
        if policies is None:
            policies = load_policies_from_env()
        if enforce_auth is None:
            enforce_auth = os.environ.get("AI_OPS_AUTH_ENABLED", "").strip() == "1"

        self.enforce_auth = enforce_auth
        self.window_seconds = window_seconds
        self._policies: Dict[str, TenantPolicy] = {p.key: p for p in policies}
        self._lock = threading.Lock()
        self._windows: Dict[str, Deque[float]] = defaultdict(deque)
        self._spent_usd: Dict[str, float] = defaultdict(float)

    @staticmethod
    def extract_bearer_key(authorization: Optional[str]) -> str:
        if not authorization:
            return ""
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return authorization.strip()

    def check_and_reserve(self, api_key: Optional[str], now: Optional[float] = None) -> QuotaCheckResult:
        token = (api_key or "").strip()
        ts = time.monotonic() if now is None else now

        with self._lock:
            policy = self._policies.get(token)
            if self.enforce_auth and policy is None:
                return QuotaCheckResult(
                    allowed=False,
                    status_code=401,
                    error_type="authentication_error",
                    error_code="invalid_api_key",
                    message="Invalid or missing API key.",
                )

            if policy is None:
                return QuotaCheckResult(allowed=True, tenant_name="anonymous")

            spent = self._spent_usd[policy.name]
            if spent >= policy.budget_usd:
                return QuotaCheckResult(
                    allowed=False,
                    status_code=429,
                    error_type="insufficient_quota",
                    error_code="budget_exceeded",
                    message=f"Tenant '{policy.name}' exceeded USD budget (${spent:.4f} >= ${policy.budget_usd:.4f}).",
                    tenant_name=policy.name,
                )

            win = self._windows[policy.name]
            cutoff = ts - self.window_seconds
            while win and win[0] <= cutoff:
                win.popleft()

            if len(win) >= policy.rpm_limit:
                return QuotaCheckResult(
                    allowed=False,
                    status_code=429,
                    error_type="rate_limit_error",
                    error_code="rpm_limit_exceeded",
                    message=f"Tenant '{policy.name}' exceeded {policy.rpm_limit} RPM limit.",
                    tenant_name=policy.name,
                )

            win.append(ts)
            return QuotaCheckResult(allowed=True, tenant_name=policy.name)

    def record_spend(self, tenant_name: str, cost_usd: float) -> None:
        if not tenant_name or tenant_name == "anonymous" or cost_usd <= 0:
            return
        with self._lock:
            self._spent_usd[tenant_name] += float(cost_usd)

    def snapshot(self) -> Dict[str, Any]:
        ts = time.monotonic()
        cutoff = ts - self.window_seconds
        with self._lock:
            out: Dict[str, Any] = {}
            for p in self._policies.values():
                win = self._windows[p.name]
                while win and win[0] <= cutoff:
                    win.popleft()
                out[p.name] = {
                    "rpm_used": len(win),
                    "rpm_limit": p.rpm_limit,
                    "spent_usd": round(self._spent_usd[p.name], 6),
                    "budget_usd": p.budget_usd,
                }
            return out
