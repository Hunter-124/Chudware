# VLM training recipes — SCAFFOLD ONLY (T-H10 v0)

**These recipes never execute in CI and must not start until the T-H8 VLM bench shows a
real, measured positive residual** (RECOGNITION_RESIDUAL discipline). They are the
capture-now-train-later plan made concrete, not a training run. `guard.py` enforces this:
every runner calls `assert_training_allowed()`, which refuses under CI and without an
explicit `CHUDWARE_ALLOW_VLM_TRAINING=1` opt-in.

## The recipe (VLM_OFFLINE_TRACK.md §2 feasibility verdict)

```
QLoRA SFT (vision + LLM LoRA)  →  TRL DPO (24 GB)  →  Unsloth GRPO (4-bit, FROZEN vision tower)
  sft_qlora.yaml                   dpo.yaml            grpo.yaml  + reward.py (deterministic)
```

- **SFT** trains BOTH vision and language LoRA layers on the synthetic corpus (renders +
  drawings → their known-geometry claim targets). Unsloth, 4-bit, ≥16 GB.
- **DPO** preference-tunes on grounded-vs-hallucinated claim pairs (TRL DPOTrainer + PEFT;
  `max_length=null` or image tokens truncate). The cheaper warm-start.
- **GRPO** is the winner leg: online RL with a **deterministic render-grounded reward**
  (`reward.py` — `F1(claims vs known_geometry) − penalty·hallucinations`, the same scoring
  the bench uses, so training optimizes exactly what we measure). **The vision tower is
  FROZEN** here because vLLM cannot serve vision-layer LoRA — it was trained in SFT.

## Why online RL (not just DPO)

cadrille (ICLR 2026): online RL > offline DPO **given a programmatic reward** (IoU 67.9 vs
61.3). We have that reward for free — we know the geometry behind every synthetic render.
CAD-Coder, ReCAD corroborate (GRPO + geometric reward).

## The gate (do NOT start training before this)

`docs/RECOGNITION_RESIDUAL.md` "VLM candidate go/no-go" must read **GO** for a candidate
(its zero-shot bench F1 beats the deterministic ceiling AND precision==1.0). Kill switch:
the trained model must beat the frontier-VLM-via-API **per dollar** on the frozen bench AND
never lose to the deterministic/claims-protocol fallback. If either fails, we ship the
deterministic fallback and do not deploy the model.

## Provisioning

Weights are downloaded out-of-band (never in CI): see
`tools.flywheel.vlm.candidates.provision_instructions("all")`.
