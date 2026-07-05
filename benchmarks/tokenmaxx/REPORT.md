# Local tokenmaxx bench — T-W29

**Machine:** RTX 3080 Ti, 12 GB VRAM (shared with the user's desktop).
**Date:** 2026-07-05. **Owner:** opus-sable.
**Question (Hunter, MAIN GOAL):** best way to tokenmaxx with local models — diffusion
Gemma's high single-stream TPS vs Gemma 4 E4B / qwen3-4b parallel slots under llama.cpp?

All numbers below come from `results/matrix.json`, produced by `bench.py` on a
**CUDA-enabled** llama-server (see Runtime provenance). Re-run recipe at the bottom.

---

## TL;DR recommendation

1. **Diffusion Gemma — NO-GO on this hardware.** DiffusionGemma shipped 2026-06-10
   (`google/diffusiongemma-26B-A4B-it`, Apache-2.0) but it is a **26B-A4B MoE**:
   smallest GGUF quant (Q4_K_M) = **16 GB**, and the MoE keeps all experts resident so
   "4B active" does NOT shrink the footprint. It does not fit 12 GB. It also needs the
   **draft** `llama-diffusion-cli` (PR #24423) — no OpenAI-compat server, no GBNF
   grammar, no parallel slots. Every production lever we rely on is absent. This
   *extends* T-W26's "not llama.cpp-runnable" with the now-released weights: still
   NO-GO, now for a concrete VRAM reason too. **Do not bundle; bench-only at best.**

2. **The parallel-slots path decisively wins on this box.** On the CUDA build, a single
   4B model scales from ~120-175 tok/s single-stream to **324-476 tok/s aggregate at
   N=8 slots**, at ≤5.3 GB VRAM — ~2.8x throughput for free. Diffusion's single-stream
   TPS advantage (which we can't even realize here) would not beat this for the harness's
   dominant token-spend shape (parallel fan-out).

3. **Two workers, split by role shape:**
   - **GBNF worker roles (feature_modeler, reviewer, all grammar-constrained handoffs):
     use `gemma3-4b-qat`.** It follows `roles.schema_to_gbnf` grammars at **100%**,
     smallest footprint (3.1 GB), zero retry tax. `gemma4-e4b` also does GBNF 100% but is
     slower and larger with no upside — **drop E4B**.
   - **Raw-throughput / non-GBNF turns: `qwen3-4b`** is fastest single-stream (175) and
     best parallel scaler (**476 tok/s at N=8**), zero JSON retries — BUT its GBNF path is
     **broken** (see below), so it must use prompted-JSON + validator, not grammars.

---

## Runtime provenance (two facts for the lead to action)

1. **The bundled llama.cpp b9860 is CPU-only** (`GGML_CUDA=OFF` in its CMakeCache).
   `--n-gpu-layers` is silently ignored ("no usable GPU found" warning only) — so
   production's "parallel slots under llama.cpp" path **currently runs on CPU**. For this
   bench I built a **CUDA llama-server at the exact same commit** (`fdb1db8`, sm_86, via
   `-allow-unsupported-compiler` for gcc 16 vs CUDA 13.3) into
   `~/.local/share/AI-CAD/llamacpp/build-cuda/` — **no pin change, bundled binary
   untouched.** GPU offload verified (E4B load: VRAM 264→3706 MB, no fallback warning).
   **Recommendation: production should ship a CUDA build.**
2. **Version mismatch:** `local_runtime.py` / `model_manager` claim pin **b9871**; the
   installed source + binary are **b9860** (`fdb1db8`). b9860 ≥ b8637 so Gemma 4 runs
   fine — no functional bump needed — but the pin string is wrong. Lead should reconcile
   the constant or the install.

## Model provenance

| model_id | repo / file | quant | license | present |
|----------|-------------|-------|---------|---------|
| qwen3-4b | Qwen/Qwen3-4B-GGUF / Qwen3-4B-Q4_K_M.gguf | Q4_K_M | Apache-2.0 | yes |
| gemma3-4b-qat | bartowski/google_gemma-3-4b-it-qat-GGUF / …-Q4_K_M.gguf | Q4_K_M | Gemma-ToU | yes |
| gemma4-e4b | unsloth/gemma-4-E4B-it-GGUF / gemma-4-E4B-it-Q4_K_M.gguf | Q4_K_M | Gemma-ToU | yes |
| diffusiongemma-26b-a4b | google/diffusiongemma-26B-A4B-it | Q4=16 GB | Apache-2.0 | **NOT downloaded — NO-GO** |

Naming: the task said "Gemma 3n E4B". Gemma 3n was superseded by the **Gemma 4 E-series**
(E2B/E4B, April 2026); `gemma-4-E4B-it` is the current equivalent. It **loads and
generates on b9860** — verified before wiring the matrix (MatFormer edge models sometimes
hit loader bugs; this one is clean).

---

## Bench matrix (llama.cpp CUDA build, sm_86; server-authoritative decode TPS)

| model | VRAM load | single-stream tok/s | TTFT | 24k-ctx decode tok/s (prompt-eval ms) | GBNF success | JSON first-pass / eventual / mean-retries |
|-------|-----------|---------------------|------|----------------------------------------|--------------|-------------------------------------------|
| qwen3-4b | 3910 MB | **175** | — | 97 (5856 ms) | **0.0 (broken)** | 1.00 / 1.00 / 0.0 |
| gemma3-4b-qat | **3086 MB** | 162 | 0.04 s | **121** (3431 ms) | **1.0** | 0.00 / 1.00 / 1.0 |
| gemma4-e4b | 3464 MB | 115 | 1.72 s | 109 (4404 ms) | **1.0** | 0.00 / 1.00 / 1.0 |

All three: `gpu_offloaded=True`. 24k context loads and decodes fine on all three (~23k
prompt tokens); latency there is prompt-eval-bound (3.4–5.9 s one-time), decode stays fast.

### N-parallel aggregate throughput (tok/s across N concurrent slots)

| model | N=1 | N=2 | N=4 | N=8 | VRAM @ N=8 | scaling N1→N8 |
|-------|-----|-----|-----|-----|-----------|---------------|
| qwen3-4b | 167 | 279 | 379 | **476** | 5284 MB | 2.85x |
| gemma3-4b-qat | 117 | 193 | 279 | 332 | 4690 MB | 2.84x |
| gemma4-e4b | 105 | 192 | 254 | 324 | 4113 MB | 3.09x |

**VRAM ceiling:** none of the three hit it — even N=8 stays ≤5.3 GB (weights load once,
shared across slots; only KV scales with N). At 12 GB there is headroom for N≥16 or for
running two models concurrently. Aggregate throughput was still climbing at N=8 for all
three, so the practical slot count is bounded by scheduling/latency goals, not VRAM here.

### Methodology note (measurement corrected mid-task)

Decode TPS uses llama-server's authoritative `timings.predicted_per_second` (excludes
HTTP/queue/prompt-eval), plus a warmup generation after each load to pay CUDA-graph
capture. A self-check asserts single-stream ≈ per-stream@N=1: qwen3 ratio 1.04 (ok),
gemma4 0.94 (ok), gemma3 1.30 (a dedicated single-slot server runs slightly faster than
one slot of an 8-slot server — explainable, not an error). An earlier client-wall-time
method gave contradictory numbers (e.g. qwen3 single-stream 187 vs N=1 59) and was
discarded.

---

## Harness-shaped micro-evals (effective TPS after validate/retry tax)

Three turns against **real role handoff schemas** (`roles.load_role` +
`roles.validate_handoff`): feature_modeler geometry-expansion (serial deep-think shape),
reviewer critique (council seat), worker tool-call (fan-out shape). See per-model JSON
`micro_evals`. Headlines: the JSON+retry path costs qwen3 ~2.9 s/handoff (0 retries),
gemma3 ~4.8 s (1 retry), gemma4 ~8.6 s (1 retry) — E4B is the slowest end-to-end. Where
GBNF works (gemma3/gemma4) the grammar path is one-shot with no retry tax.

---

## Structured-output adherence — the key risk, and the qwen3 surprise

GBNF grammar (llama.cpp `grammar` field from `roles.schema_to_gbnf`) is the production
path for worker roles. Head-to-head with prompted-JSON + `validate_handoff` retry:

- **gemma3-4b-qat / gemma4-e4b: GBNF success 1.0** — grammar-constrained, one-shot, ~0
  retry tax. These are the safe GBNF workers.
- **qwen3-4b: GBNF success 0.0 — REPRODUCED root cause.** Under grammar constraint qwen3
  emits only whitespace and never commits to the object. qwen3 is a *reasoning* model: it
  wants a `<think>` block first; the grammar forbids those tokens, so the constrained
  sampler is trapped in the only legal filler (`ws`). Non-reasoning instruct models don't
  have this problem. qwen3 aces prompted-JSON instead (first-pass 1.0, 0 retries) because
  `<think>` is allowed there and the trailing object parses.
  - **Untested** recovery hypotheses (do not assume they work): prefix-inject an empty
    `<think></think>` then constrain; disable thinking (`/no_think` or a non-reasoning
    qwen variant); grammar that permits a leading think span.
  - **Verified conclusion:** qwen3 cannot use the production GBNF path as-is.

**Why diffusion loses even before VRAM:** DiffusionGemma has no grammar engine, so it
would run the prompted-JSON+retry path (the slower, retry-taxed one) — and it can't fit
on-GPU here anyway.

---

## Transformers GPU backend seam (DoD)

`services/agent/local_runtime.py` gained an **extras-guarded** `TransformersBackend` +
`select_backend()` (env `AICAD_LOCAL_BACKEND` / cfg `local_backend`). Proven to actually
run on the 3080 Ti (`results/transformers_backend_seam.json`): loaded Qwen2.5-0.5B fp16,
988 MB VRAM, emitted valid JSON. In the **gate venv** (no torch) it degrades honestly —
`transformers_available()=False`, `TransformersBackend(...)` raises `BackendUnavailable`;
6 gate-safe tests (`TestBackendSeam`) cover this. torch/transformers live in a **separate
venv**: `~/.local/share/AI-CAD/torch-venv` (torch 2.6.0+cu124, transformers 5.13.0) —
never the gate venv. This is the seam T-W27 gym cycle 2 selects by config, not a fork.
Note: the transformers path has **no GBNF engine**, so structured output there is
prompted-JSON + `validate_handoff` — the same asymmetry the diffusion NO-GO hinges on.

---

## RECOMMENDATION — role × stage mapping

> Proposed only. Do **not** apply tiers.yaml/routing changes here — lead applies after
> gym cycle 1 corroborates.

| harness stage | token-spend shape | proposed engine | why |
|---------------|-------------------|-----------------|-----|
| ladder rungs / ponder / council monologues | serial deep-think, latency-bound | `qwen3-4b` (non-GBNF) for raw reasoning speed; `gemma3-4b-qat` if the turn is grammar-constrained | diffusion would win single-stream TPS *if it fit* — it doesn't. qwen3 is the fastest fitting AR model (175 tok/s) and reasons natively; use gemma3 when GBNF is required |
| departments / workers / council fan-out / multi-doc | parallel throughput-bound | `qwen3-4b` under llama.cpp `--parallel` (476 tok/s @ N=8) for non-GBNF; `gemma3-4b-qat` for GBNF-constrained workers (332 tok/s @ N=8, 100% grammar) | weights shared across slots, KV scales with N; 12 GB has headroom past N=8 |

**GO/NO-GO on diffusion for production bundling:** **NO-GO.** Revisit only when BOTH
(a) a ≥18 GB VRAM target *or* a ≤12 GB diffusion variant exists, AND (b) a production
diffusion **server + grammar** path lands (not the draft `llama-diffusion-cli`). Until
then, bundle the CUDA llama.cpp 4B stack.

**Proposed tiers.yaml/routing (for lead, post-corroboration):**
- Keep `qwen3-4b` as the tool-calling / reasoning primary (Apache-2.0, fastest, best
  parallel), but route its handoffs through prompted-JSON + `validate_handoff`, NOT GBNF,
  until the reasoning×GBNF issue is fixed.
- Make `gemma3-4b-qat` the default **GBNF-constrained worker** engine (100% grammar,
  smallest footprint). **Drop `gemma4-e4b` from the default set** — it is slower and
  larger than gemma3-qat with no measured advantage.
- Enable `--parallel 8` (or higher) on the bundled server; there is VRAM headroom.

---

## What was NOT run, and why (honest-open)

- **DiffusionGemma not downloaded/benched:** does not fit 12 GB (16 GB min quant) and
  needs a draft runtime with no server/grammar/parallel. Recorded NO-GO with evidence
  rather than substituting a different diffusion LM silently. A sub-2B diffusion proxy
  (SEDD/SMDM-class) was considered per the task's "check smaller options" but skipped: it
  cannot move the recommendation (can't beat a 4B AR model on this hardware) and its
  structured-output behavior wouldn't transfer to the 26B target. Available on request as
  a "paradigm TPS reference only."
- **qwen3 GBNF recovery fixes:** listed as hypotheses, not tested — out of scope for the
  bench; flagged for follow-up.
- **scripts/gate.sh not run to completion:** it timed out at 7 min on the OCCT C++
  build leg (unrelated to these Python-only changes). The Python legs were run directly
  instead: full `pytest services/agent/` = **885 passed, 2 failed, 3 skipped**; the 2
  failures (`test_h7_shadow`, `test_providers_conformance`) are the known issue #16
  pre-existing failures in files this task never touched (confirmed: they fail in
  isolation without any of my code in the import path). `test_local_runtime.py` (incl.
  the 6 new `TestBackendSeam` tests) = 24 passed, 2 skipped. `test_loop_mock.py` was
  uncollectable due to a pre-existing missing sample file (`services/engine/samples/
  mounting_plate.py`), also unrelated. `graphify update .` was run (32450 nodes).

## Caveat on qwen3's single-stream number

qwen3-4b's 175 tok/s **includes its `<think>` reasoning tokens**, so its rate of
*useful output* is lower than the raw figure — relevant to the serial-deep-think row
where qwen3 is proposed for reasoning speed. The instruct models (gemma3/gemma4) emit no
hidden reasoning, so their tok/s is closer to useful-output rate. This does not change
the recommendation but is worth weighing when comparing the two families directly.

---

## Reproduce

```bash
# CUDA llama-server built at same commit as bundled b9860 (fdb1db8), sm_86:
export TOKENMAXX_LLAMA_SERVER=~/.local/share/AI-CAD/llamacpp/build-cuda/bin/llama-server
~/.local/share/AI-CAD/mcp-venv/bin/python benchmarks/tokenmaxx/bench.py \
    --models qwen3-4b gemma3-4b-qat gemma4-e4b --parallel 1 2 4 8 --trials 6
# results -> benchmarks/tokenmaxx/results/{matrix.json,<model>.json}
# transformers seam proof (separate torch venv):
~/.local/share/AI-CAD/torch-venv/bin/python -c "import sys; sys.path.insert(0,'.'); \
    from services.agent import local_runtime as lr; print(lr.transformers_available())"
```
