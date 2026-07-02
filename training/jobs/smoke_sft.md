---
id: smoke-sft-001
title: Smoke SFT — Qwen3-4B LoRA on IP-13 v1 (small)
dataset_hash: corpus/IP-13/v1
recipe: training/recipes/sft_lora.yaml
base_model: Qwen/Qwen3-4B
adapter_out: training/adapters/smoke-sft-001
eval_bench: aicad_basic
requires_os: any
requires_vram_gb: 8
requires_cuda: true
owner: unassigned
status: todo
---

## Smoke SFT job

Smallest possible SFT run to validate the full pipeline end-to-end:
dataset pull → LoRA fine-tune → adapter push → MLflow run logged.

- **Base model:** Qwen/Qwen3-4B (Pillar 13 ratified choice)
- **Recipe:** `training/recipes/sft_lora.yaml` — 50 steps, batch 1, grad accum 4
- **Dataset:** `corpus/IP-13/v1` — small IP-13 export (placeholder; swap for real shard)
- **Adapter out:** `training/adapters/smoke-sft-001/`
- **Eval:** `aicad_basic` benchmark (basic CAD prompt correctness)
- **VRAM:** 8 GB minimum (4-bit quant via Unsloth)

### Definition of done for this job

- [ ] Adapter written to `training/adapters/smoke-sft-001/`
- [ ] `provenance.json` present in adapter dir
- [ ] MLflow run logged (or `./mlruns` fallback)
- [ ] Job status set to `review` by runner
