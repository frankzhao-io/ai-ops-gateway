"""Deterministic input/output guardrails for PII redaction, injection blocking, and schema validation."""

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


PII_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("PHONE", re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?(?:09\d{2}[-\s]?\d{3}[-\s]?\d{3}|\d{3}-\d{3}-\d{4})\b")),
    ("API_KEY", re.compile(r"\b(?:sk-[A-Za-z0-9]{12,}|AIza[0-9A-Za-z_-]{16,})\b")),
    ("TW_ID", re.compile(r"\b[A-Z][12]\d{8}\b")),
]

INJECTION_PATTERNS: List[Tuple[str, re.Pattern]] = [
    ("IGNORE_INSTRUCTIONS", re.compile(r"(?i)\b(ignore|disregard|forget)\b.{0,30}\b(previous|above|system)\b.{0,20}\b(instructions?|prompts?|rules?)\b")),
    ("SYSTEM_EXFIL", re.compile(r"(?i)(reveal|print|dump|show|output|leak)\s+(your\s+)?(hidden\s+|system\s+)?(prompt|instructions?|api\s+key|secrets?|env)")),
    ("ZH_OVERRIDE", re.compile(r"(忽略|無視|覆蓋)(?:之前|上面|系統|所有)+(?:的)?(指令|規則|設定)")),
]

BENIGN_META_CONTEXT = re.compile(
    r"(?i)(explain|how to defend|how does|distinguish|prevent|mitigate|textbook|如何防止|怎麼防禦|如何區分|解釋|什麼是)"
)
QUOTED_SPAN = re.compile(r"(['\"『「][^'\"』」]{4,120}['\"』」])")
EXFIL_PAYLOAD = re.compile(r"(?i)(dump|reveal|print|show|exfiltrate|secrets?|api\s*key|env|密碼|密鑰|環境變數)")


def _exempt_safe_quote(match: re.Match) -> str:
    span = match.group(1)
    if len(span) <= 45 and not EXFIL_PAYLOAD.search(span):
        return " "
    return span


@dataclass
class GuardrailReport:
    blocked: bool = False
    block_reason: Optional[str] = None
    pii_redacted: List[str] = field(default_factory=list)
    sanitized_prompt: str = ""


def redact_pii(text: str) -> Tuple[str, List[str]]:
    """Redact common PII and secret tokens from user input."""
    found: List[str] = []
    sanitized = text
    for label, pattern in PII_PATTERNS:
        if pattern.search(sanitized):
            found.append(label)
            sanitized = pattern.sub(f"[REDACTED_{label}]", sanitized)
    return sanitized, found


def detect_prompt_injection(text: str) -> Optional[str]:
    """Return matched rule name if prompt injection is detected, else None.

    Avoids false positives when a short injection phrase is quoted inside an
    explanatory or defensive-security question, while still blocking wrapped
    attacks containing exfiltration payloads or unquoted overrides.
    """
    is_meta_query = bool(BENIGN_META_CONTEXT.search(text))
    unquoted_text = QUOTED_SPAN.sub(_exempt_safe_quote, text) if is_meta_query else text
    if is_meta_query and EXFIL_PAYLOAD.search(unquoted_text):
        unquoted_text = text

    for rule_name, pattern in INJECTION_PATTERNS:
        if pattern.search(unquoted_text):
            return rule_name
    return None


def inspect_output(text: str) -> Tuple[str, List[str]]:
    """Scan model output and redact any accidentally leaked PII or API keys."""
    return redact_pii(text)


def inspect_input(prompt: str) -> GuardrailReport:
    """Run input guardrails: injection detection first, then PII redaction."""
    injection_rule = detect_prompt_injection(prompt)
    if injection_rule:
        return GuardrailReport(
            blocked=True,
            block_reason=f"PROMPT_INJECTION:{injection_rule}",
            sanitized_prompt="",
        )
    sanitized, pii_found = redact_pii(prompt)
    return GuardrailReport(
        blocked=False,
        pii_redacted=pii_found,
        sanitized_prompt=sanitized,
    )


def strip_json_fences(raw_text: str) -> str:
    """Strip optional ```json ... ``` markdown code fences around JSON output."""
    text = raw_text.strip()
    fence_match = re.match(r"^```(?:json)?\s*\r?\n?(.*?)\r?\n?```$", text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    return text


def validate_json_output(raw_text: str, required_keys: Optional[List[str]] = None) -> Tuple[bool, Optional[str]]:
    """Verify output is valid JSON (after stripping markdown fences) containing all required keys."""
    cleaned = strip_json_fences(raw_text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return False, f"INVALID_JSON:{exc.msg}"

    if not isinstance(parsed, dict):
        return False, "EXPECTED_JSON_OBJECT"

    if required_keys:
        missing = [k for k in required_keys if k not in parsed]
        if missing:
            return False, f"MISSING_KEYS:{','.join(missing)}"
    return True, None
