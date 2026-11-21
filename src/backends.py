"""Live model backends (Ollama Local 7B + Gemini Cloud) with JSONL caching and retry backoff."""

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

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
    """Load GEMINI_API_KEY from environment or sibling grounded-chart-audit/.env."""
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"].strip()
    for folder in ("grounded-chart-audit", "track3-ai4science"):
        env_file = WORKSPACE_ROOT / folder / ".env"
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


def _format_ollama_messages(
    prompt: str,
    messages: Optional[List[Dict[str, str]]],
    require_json: bool,
    required_keys: Optional[List[str]],
) -> List[Dict[str, str]]:
    if not messages:
        final_prompt = _build_json_instruction(prompt, required_keys) if require_json else prompt
        return [{"role": "user", "content": final_prompt}]
    out = [dict(m) for m in messages]
    if require_json and out:
        for i in range(len(out) - 1, -1, -1):
            if out[i].get("role") == "user":
                out[i]["content"] = _build_json_instruction(out[i]["content"], required_keys)
                break
    return out


def _format_gemini_payload(
    prompt: str,
    messages: Optional[List[Dict[str, str]]],
    require_json: bool,
    required_keys: Optional[List[str]],
    temperature: Optional[float],
    max_tokens: Optional[int],
) -> Dict:
    gen_cfg: Dict = {
        "temperature": 0.0 if temperature is None else float(temperature),
        "maxOutputTokens": 1024 if max_tokens is None else int(max_tokens),
    }
    if require_json:
        gen_cfg["responseMimeType"] = "application/json"

    if not messages:
        final_prompt = _build_json_instruction(prompt, required_keys) if require_json else prompt
        return {"contents": [{"parts": [{"text": final_prompt}]}], "generationConfig": gen_cfg}

    system_parts: List[str] = []
    contents: List[Dict] = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            system_parts.append(content)
        else:
            gem_role = "model" if role == "assistant" else "user"
            contents.append({"role": gem_role, "parts": [{"text": content}]})

    if not contents:
        contents = [{"role": "user", "parts": [{"text": prompt}]}]
    if require_json:
        for i in range(len(contents) - 1, -1, -1):
            if contents[i]["role"] == "user":
                contents[i]["parts"][0]["text"] = _build_json_instruction(
                    contents[i]["parts"][0]["text"], required_keys
                )
                break

    payload: Dict = {"contents": contents, "generationConfig": gen_cfg}
    if system_parts:
        payload["systemInstruction"] = {"parts": [{"text": "\n".join(system_parts)}]}
    return payload


def call_ollama(
    prompt: str,
    model: str = "qwen2.5-coder:7b",
    base_url: Optional[str] = None,
    require_json: bool = False,
    required_keys: Optional[List[str]] = None,
    timeout_sec: float = 30.0,
    messages: Optional[List[Dict[str, str]]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> BackendResult:
    """Call local Ollama chat API and return exact token counts and wall-clock latency."""
    resolved_url = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
    t0 = time.perf_counter()
    options: Dict = {"temperature": 0.0 if temperature is None else float(temperature)}
    if max_tokens is not None:
        options["num_predict"] = int(max_tokens)
    payload: Dict = {
        "model": model,
        "messages": _format_ollama_messages(prompt, messages, require_json, required_keys),
        "stream": False,
        "options": options,
    }
    if require_json:
        payload["format"] = "json"

    try:
        resp = requests.post(f"{resolved_url}/api/chat", json=payload, timeout=timeout_sec)
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
    messages: Optional[List[Dict[str, str]]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> BackendResult:
    """Call Gemini REST API with 429/503 exponential backoff and exact usageMetadata."""
    key = api_key or load_gemini_api_key()
    if not key:
        return BackendResult(text="", error="GEMINI_API_KEY_MISSING")

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = _format_gemini_payload(prompt, messages, require_json, required_keys, temperature, max_tokens)

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
            candidates = data.get("candidates") or []
            parts = candidates[0].get("content", {}).get("parts") or [] if candidates else []
            text = "".join(p.get("text", "") for p in parts)
            if not text:
                last_err = "EMPTY_RESPONSE"
                continue
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


def stream_ollama(
    prompt: str,
    model: str = "qwen2.5-coder:7b",
    base_url: Optional[str] = None,
    require_json: bool = False,
    required_keys: Optional[List[str]] = None,
    timeout_sec: float = 60.0,
    messages: Optional[List[Dict[str, str]]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Iterator[Tuple[str, Optional[Dict[str, int]]]]:
    """Yield incremental token chunks from Ollama's NDJSON streaming API."""
    resolved_url = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")).rstrip("/")
    options: Dict = {"temperature": 0.0 if temperature is None else float(temperature)}
    if max_tokens is not None:
        options["num_predict"] = int(max_tokens)
    payload: Dict = {
        "model": model,
        "messages": _format_ollama_messages(prompt, messages, require_json, required_keys),
        "stream": True,
        "options": options,
    }
    if require_json:
        payload["format"] = "json"
    with requests.post(f"{resolved_url}/api/chat", json=payload, stream=True, timeout=timeout_sec) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line:
                continue
            chunk = json.loads(line)
            piece = chunk.get("message", {}).get("content", "")
            usage = None
            if chunk.get("done"):
                usage = {
                    "prompt_tokens": int(chunk.get("prompt_eval_count", 0)),
                    "completion_tokens": int(chunk.get("eval_count", 0)),
                }
            if piece or usage:
                yield piece, usage


def stream_gemini(
    prompt: str,
    model: str = "gemini-3.5-flash-lite",
    api_key: Optional[str] = None,
    require_json: bool = False,
    required_keys: Optional[List[str]] = None,
    timeout_sec: float = 60.0,
    messages: Optional[List[Dict[str, str]]] = None,
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
) -> Iterator[Tuple[str, Optional[Dict[str, int]]]]:
    """Yield incremental token chunks from Gemini's SSE streaming endpoint."""
    key = api_key or load_gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY_MISSING")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse&key={key}"
    payload = _format_gemini_payload(prompt, messages, require_json, required_keys, temperature, max_tokens)
    with requests.post(url, json=payload, stream=True, timeout=timeout_sec) as resp:
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw or raw == "[DONE]":
                continue
            data = json.loads(raw)
            candidates = data.get("candidates") or []
            piece = ""
            if candidates:
                parts = candidates[0].get("content", {}).get("parts") or []
                piece = "".join(p.get("text", "") for p in parts)
            usage_meta = data.get("usageMetadata")
            usage = None
            if usage_meta:
                usage = {
                    "prompt_tokens": int(usage_meta.get("promptTokenCount", 0)),
                    "completion_tokens": int(usage_meta.get("candidatesTokenCount", 0)),
                }
            if piece or usage:
                yield piece, usage


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
        self._lock = threading.Lock()
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

    def __call__(
        self,
        prompt: str,
        tier: ModelTier,
        require_json: bool,
        required_keys: Optional[List[str]],
        messages: Optional[List[Dict[str, str]]] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> BackendResult:
        # Bypass cache when non-deterministic temperature or multi-turn history is provided
        cacheable = self.use_cache and not messages and (temperature is None or temperature == 0.0) and max_tokens is None
        key = self._make_key(prompt, tier, require_json, required_keys)
        if cacheable:
            with self._lock:
                if key in self._cache:
                    return self._cache[key]

        if tier == ModelTier.TIER_1_LOCAL_7B:
            res = call_ollama(
                prompt,
                model=self.ollama_model,
                require_json=require_json,
                required_keys=required_keys,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )
        else:
            res = call_gemini(
                prompt,
                model=self.gemini_model,
                require_json=require_json,
                required_keys=required_keys,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
            )

        if cacheable and not res.error:
            record = {
                "key": key,
                "tier": tier.value,
                "text": res.text,
                "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens,
                "latency_ms": res.latency_ms,
            }
            cached_res = BackendResult(
                text=res.text,
                prompt_tokens=res.prompt_tokens,
                completion_tokens=res.completion_tokens,
                latency_ms=res.latency_ms,
                cached=True,
            )
            with self._lock:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                with self.cache_path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._cache[key] = cached_res
        return res
