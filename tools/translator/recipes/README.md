# Translator training recipes — SCAFFOLD ONLY (T-F11 v0)

**These recipes never execute in CI** and must not start until the eval harness shows the
ship gates are reachable. `guard.py` enforces it (`assert_training_allowed()` refuses under
CI and without `CHUDWARE_ALLOW_TRANSLATOR_TRAINING=1`). Same discipline as T-H6/T-H10.

## The recipe (master plan §Pillar 13)

```
QLoRA SFT (2 candidates, winner picked by harness)  →  TRL GRPO (deterministic multi-term reward)
  sft_qlora.yaml                                        grpo.yaml  + ../reward.py
```

- **SFT** distills the deterministic teacher: `(gsl-ser/v1 state → render_caption)` pairs,
  QLoRA r=16–32, on **Qwen3-4B-Instruct-2507** (primary) and **Gemma 3 4B** in parallel; the
  eval harness picks the winner. The template is a *correct, free* label source
  (Snorkel/DryBell distill-the-teacher); round-trip + groundedness filters keep the set clean.
- **GRPO** polishes groundedness with a **deterministic, verifiable** reward
  (`../reward.py`: groundedness + slot coverage + format − hard hallucination penalty). No
  critic, no learned reward model. 4B LoRA GRPO ≈18–20 GB → one 24 GB card.

## Ship gates (master plan §Pillar 13 — each stage gates the next AND deployment)

| gate | threshold | measured by |
|---|---|---|
| numeric fidelity | ≥ 99.5% | `tools.translator.eval.harness` / `candidate_eval` |
| unsupported-claim rate | < 0.1% on held-out **structures** | same |
| slot coverage | ≥ 98% | same |
| round-trip re-extraction | exact | `gsl_ser.roundtrip_diff == []` |
| **kill-switch** | **beat raw GSL templates on downstream task success, never lose on fidelity** | downstream-task harness (see `docs/RECOGNITION_RESIDUAL.md`) |

**Inversion caveat:** `render_caption` scores ~1.0 fidelity BY CONSTRUCTION — it is the
ceiling. A learned model can at best TIE on fidelity; its reason to ship is the kill-switch
WIN axis (downstream task success), a different metric. If it doesn't beat templates there,
**don't ship — templates are free** (a documented no-ship verdict is a valid completion).

## Provisioning

Out-of-band, never CI: `tools.translator.candidates.provision_instructions("all")`.
