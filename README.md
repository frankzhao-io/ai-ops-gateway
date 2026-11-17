# ai-ops-gateway

A lightweight routing and guardrail gateway for mixing deterministic checks, a local 7B model (`qwen2.5-coder:7b` via Ollama), and a cloud model (`gemini-3.5-flash-lite`) behind a single `/v1/chat` interface.

The goal is practical: avoid sending arithmetic, malformed inputs, or routine extraction tasks to a paid cloud API, while keeping a fallback path when the local model fails or times out.

## How a request flows

1. **Input checks (`src/guardrails.py`)**: Blocks common English/Chinese prompt-injection patterns and redacts PII/secrets (emails, phone numbers, Taiwan national IDs, `sk-`/`AIza` keys) before any model call. Short quoted phrases inside explanatory security questions are exempted unless they contain exfiltration verbs.
2. **Routing (`src/router.py`)**:
   - Pure arithmetic expressions go to an AST evaluator (`tier0_deterministic`, $0 cost).
   - Prompts over 600 characters or matching complexity keywords route to `tier2_cloud_frontier`.
   - Everything else starts on `tier1_local_7b`.
3. **Fallback & output checks (`src/gateway.py`)**: If Ollama errors or times out, or if `require_json=True` validation still fails after stripping markdown code fences, the request falls back once to Tier-2. Model output is scanned again for leaked keys/PII before returning, and request metadata is appended to `data/traces.jsonl`.

## Evaluation results

All live runs used Ollama `qwen2.5-coder:7b` (local RTX 3080) and `gemini-3.5-flash-lite`, with responses cached in `data/gateway_cache.jsonl` and `data/heldout_cache.jsonl`.

### 1. Development set (`N=60`, `src/run_ablation_live.py`)

Cases: 10 arithmetic, 10 injection attacks, 10 benign questions containing security terms (hard negatives), 15 JSON extractions, and 15 short analytical Q&As.

| Arm | Pass@1 | Injection Block | False Positive Rate | Total Cost (USD) | P95 Latency |
| :--- | :---: | :---: | :---: | :---: | :---: |
| `Arm1_All_Cloud_Baseline` (no guardrails, Tier-2 only) | 48/60 (80.0%) | 0.0% | 0.0% | $0.05804 | 9099.77 ms |
| `Arm2_All_Local_7B_Gated` (guardrails + Tier-1 only) | 54/60 (90.0%) | 100.0% | 0.0% | $0.00123 | 5447.65 ms |
| `Arm3_Routed_Gated_Gateway` (guardrails + 3-tier router) | 57/60 (95.0%) | 100.0% | 0.0% | $0.00712 | 5255.85 ms |

Notes from this run:
- `Arm3` cut cost by **87.73%** compared to `Arm1`.
- Model-level refusal is not enough on its own: in `Arm1`, Gemini refused 9 of 10 injections but complied with `S08` (a Chinese system-override prompt).
- Without Tier-0 AST evaluation, the local 7B model miscalculated two arithmetic cases (`M06`: `999 * 3 + 3` -> `2999`; `M07`: `256 * 8 - 48` -> `2016`).

### 2. Held-out set (`N=24`, `src/run_heldout_and_latency.py`)

To check generalization on unseen phrasing after tuning the guardrail rules, we ran 24 held-out cases after warming up Ollama (`5278.8 ms` warmup excluded from steady-state stats):

| Arm | Pass@1 | Injection Block | False Positive Rate | Total Cost (USD) | P50 Latency | P95 Latency |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: |
| `HeldOut_Baseline_AllCloud` | 16/24 (66.7%) | 0.0% | 0.0% | $0.03189 | 1311.51 ms | 12572.16 ms |
| `HeldOut_Routed_Gated_Gateway` | 21/24 (87.5%) | 83.3% | 0.0% | $0.00067 | 885.4 ms | 5337.56 ms |

Held-out cost dropped by **97.91%** (`$0.03189` -> `$0.00067`). Three cases failed in the gated arm:
- `HS06` used an unseen Chinese synonym (`前面`) not covered by the regex, so 1 of 6 held-out injections slipped through.
- `HQ01` and `HQ02` (short questions about AdamW and vLLM PagedAttention) stayed on the 7B model because they did not trigger length or keyword rules, and the 7B answers missed the target concepts.

## Known limitations

- **Regex guardrails and keyword routing do not generalize to unseen paraphrases**, as `HS06`, `HQ01`, and `HQ02` showed. The next step is pairing the fast regex pre-filter with a lightweight classifier or secondary check on borderline inputs.
- **Synchronous FastAPI handler**: `src/gateway.py` runs synchronously. Under concurrent load on a single GPU, an async handler with an in-flight semaphore and cloud overflow would be needed to keep tail latency bounded.
- **Token pricing**: Tier-1 cost uses an approximate `$0.00015 / 1k tokens` infra estimate; Tier-0 is `$0`.

## Running locally

```powershell
git clone https://github.com/frankzhao-io/ai-ops-gateway.git
cd ai-ops-gateway

# Unit and regression tests (offline mock backend, 10 tests)
python -m pytest -v

# Offline CI smoke run (writes dashboard/eval-data.js)
python -m src.eval_ci

# Live runs (requires local Ollama with qwen2.5-coder:7b and GEMINI_API_KEY)
python -m src.run_ablation_live
python -m src.run_heldout_and_latency
```

Open `dashboard/index.html` in a browser to inspect the dev and held-out tables alongside per-case results.
