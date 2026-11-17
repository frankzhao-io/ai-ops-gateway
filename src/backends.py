"""Live model backends (Ollama Local 7B + Gemini Cloud) with JSONL caching and retry backoff."""

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import requests

from src.router import ModelTier


ROOT = Path(__file__).resolve().parent.parent
WORKSPACE_ROOT = ROOT.parent
DEFAULT_CACHE_PATH = ROOT / "data" / "gateway_cache.jsonl"


@dataclass
class BackendResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cached: bool = False
    error: Optional[str] = None


def load_gemini_api_key() -> Optional[str]:
    """Load GEMINI_API_KEY from environment or sibling track3-ai4science/.env."""
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    env_file = WORKSPACE_ROOT / "track3-ai4science" / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith("GEMINI_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return None


def _build_json_instruction(prompt: str, required_keys: Optional[List[str]]) -> str:
    if not required_keys:
        return prompt
    keys_str = ", ".join(required_keys)
    return f"{prompt}\n\nReturn ONLY a valid JSON object containing these keys: {keys_str}."


def call_ollama(
    prompt: str,
    model: str = "qwen2.5-coder:7b",
    base_url: str = "http://localhost:11434",
    require_json: bool = False,
    required_keys: Optional[List[str]] = None,
    timeout_sec: float = 30.0,
) -> BackendResult:
    """Call local Ollama chat API and return exact token counts and wall-clock latency."""
    t0 = time.perf_counter()
    final_prompt = _build_json_instruction(prompt, required_keys) if require_json else prompt
    payload: Dict = {
        "model": model,
        "messages": [{"role": "user", "content": final_prompt}],
        "stream": False,
        "options": {"temperature": 0.0},
    }
    if require_json:
        payload["format"] = "json"

    try:
        resp = requests.post(f"{base_url.rstrip('/')}/api/chat", json=payload, timeout=timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        return BackendResult(
            text=data.get("message", {}).get("content", ""),
            prompt_tokens=int(data.get("prompt_eval_count", 0)),
            completion_tokens=int(data.get("eval_count", 0)),
            latency_ms=elapsed_ms,
        )
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        return BackendResult(text="", latency_ms=elapsed_ms, error=f"OLLAMA_ERROR:{type(exc).__name__}:{exc}")


def call_gemini(
    prompt: str,
    model: str = "gemini-3.5-flash-lite",
    api_key: Optional[str] = None,
    require_json: bool = False,
    required_keys: Optional[List[str]] = None,
    timeout_sec: float = 30.0,
    max_retries: int = 3,
) -> BackendResult:
    """Call Gemini REST API with 429/503 exponential backoff and exact usageMetadata."""
    key = api_key or load_gemini_api_key()
    if not key:
        return BackendResult(text="", error="GEMINI_API_KEY_MISSING")

    final_prompt = _build_json_instruction(prompt, required_keys) if require_json else prompt
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    gen_cfg: Dict = {"temperature": 0.0, "maxOutputTokens": 1024}
    if require_json:
        gen_cfg["responseMimeType"] = "application/json"
    payload = {
        "contents": [{"parts": [{"text": final_prompt}]}],
        "generationConfig": gen_cfg,
    }

    t0 = time.perf_counter()
    last_err = "UNKNOWN"
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, json=payload, timeout=timeout_sec)
            if resp.status_code in (429, 503):
                last_err = f"HTTP_{resp.status_code}"
                time.sleep(1.5 * (2 ** attempt))
                continue
            resp.raise_for_status()
            data = resp.json()
            elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            usage = data.get("usageMetadata", {})
            return BackendResult(
                text=text,
                prompt_tokens=int(usage.get("promptTokenCount", 0)),
                completion_tokens=int(usage.get("candidatesTokenCount", 0)),
                latency_ms=elapsed_ms,
            )
        except Exception as exc:
            last_err = f"{type(exc).__name__}:{exc}"
            if attempt < max_retries - 1:
                time.sleep(1.0 * (2 ** attempt))

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return BackendResult(text="", latency_ms=elapsed_ms, error=f"GEMINI_ERROR:{last_err}")


class CachedLiveBackend:
    """Routes Tier-1 to Ollama and Tier-2 to Gemini with persistent JSONL caching."""

    def __init__(
        self,
        cache_path: Path = DEFAULT_CACHE_PATH,
        ollama_model: str = "qwen2.5-coder:7b",
        gemini_model: str = "gemini-3.5-flash-lite",
        use_cache: bool = True,
    ):
        self.cache_path = cache_path
        self.ollama_model = ollama_model
        self.gemini_model = gemini_model
        self.use_cache = use_cache
        self._cache: Dict[str, BackendResult] = {}
        if self.use_cache and self.cache_path.exists():
            for line in self.cache_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                self._cache[row["key"]] = BackendResult(
                    text=row["text"],
                    prompt_tokens=row.get("prompt_tokens", 0),
                    completion_tokens=row.get("completion_tokens", 0),
                    latency_ms=row.get("latency_ms", 0.0),
                    cached=True,
                )

    def _make_key(self, prompt: str, tier: ModelTier, require_json: bool, required_keys: Optional[List[str]]) -> str:
        raw = json.dumps(
            {
                "tier": tier.value,
                "model": self.ollama_model if tier == ModelTier.TIER_1_LOCAL_7B else self.gemini_model,
                "require_json": require_json,
                "required_keys": required_keys or [],
                "prompt": prompt,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def __call__(self, prompt: str, tier: ModelTier, require_json: bool, required_keys: Optional[List[str]]) -> BackendResult:
        key = self._make_key(prompt, tier, require_json, required_keys)
        if self.use_cache and key in self._cache:
            return self._cache[key]

        if tier == ModelTier.TIER_1_LOCAL_7B:
            res = call_ollama(prompt, model=self.ollama_model, require_json=require_json, required_keys=required_keys)
        else:
            res = call_gemini(prompt, model=self.gemini_model, require_json=require_json, required_keys=required_keys)

        if self.use_cache and not res.error:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "key": key,
                "tier": tier.value,
                "text": res.text,
                "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens,
                "latency_ms": res.latency_ms,
            }
            with self.cache_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._cache[key] = BackendResult(
                text=res.text,
                prompt_tokens=res.prompt_tokens,
                completion_tokens=res.completion_tokens,
                latency_ms=res.latency_ms,
                cached=True,
            )
        return res
