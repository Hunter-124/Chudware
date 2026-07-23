# Offline VLM track v0 (IP-21 / T-H10)

Gives the offline / limited-context copilot **eyes**: render self-review, drawing/dimension
reading, part-photo feature triage, render-vs-photo compare. **Locked division of labor**
(never invert): the VLM proposes COARSE, confidence-tiered claims about an image;
deterministic code verifies. The VLM never measures — fiducial/metrology code owns that
(T-F5).

**v0 scope (RATIFIED, fenced):** bench + synthetic corpus + zero-shot baselines only. **No
training runs.** The `recipes/` files may land but never execute in CI (`recipes/guard.py`).

## Modules

| module | what it does | runs offline/CI? |
|---|---|---|
| `candidates.py` | registry of the 3 RATIFIED local models (Qwen3-VL-4B primary, granite-vision-4.1-4b, Gemma 4 E4B — all Apache-2.0) + provisioning commands | yes (registry only) |
| `corpus_gen.py` | synthetic corpus v1: labeled renders, drawings w/ known callouts, known-diff render pairs; geometry KNOWN; provenance=synthetic; frozen hash | yes |
| `candidate_eval.py` | zero-shot residual harness — candidate claims → T-H8 bench scoring → residual vs deterministic ceiling → GO/NO-GO. Mock-proven end-to-end. | yes (mock); real when provisioned |
| `serving.py` | `experimental.vision_review` seam: render self-review via providers/1, loopback-guarded | yes (guards); real when served |
| `residual_report.py` | emits the go/no-go section into `docs/RECOGNITION_RESIDUAL.md` | yes |
| `dvc_track.py` | DVC-version the corpus (reuses T-H5 helpers) | yes (round-trip test) |
| `recipes/` | SFT/DPO/GRPO scaffold + deterministic reward + CI guard | **guarded — never trains in CI** |

## Quick start

```bash
py=~/.local/share/Chudware/mcp-venv/bin/python
# 1. generate the synthetic corpus
$py -m tools.flywheel.vlm.corpus_gen training/corpora/vlm/v1 --seeds 3
# 2. record the residual (offline -> deterministic ceiling MEASURED, candidates pending)
$py -m tools.flywheel.vlm.residual_report
# 3. tests (all real, no skips beyond dvc-absent)
$py -m pytest tools/flywheel/vlm/tests -q
```

## Measuring the real candidate residual (foreman/Erik-authorized, NOT in CI)

The 3 candidates are LOCAL and run OFFLINE once weights are on disk — the residual is
measurable on an air-gapped box. Provisioning (multi-GB download + llama.cpp-with-vision
build) is out-of-band:

```bash
python -c "from tools.flywheel.vlm.candidates import provision_instructions as p; print(p('all'))"
# ...download + `llama-server --mmproj ...` on 127.0.0.1:1234, then:
python -m tools.flywheel.vlm.residual_report --base-url http://127.0.0.1:1234/v1
```

`residual_report` fills the MEASURED rows and re-emits the go/no-go verdict. **GO** = zero-
shot bench F1 beats the deterministic ceiling AND precision == 1.0 (never lose to the
fallback). The second kill-switch (beat the frontier VLM per dollar) needs the T-H8 API
`frontier` row and is pending offline.

## The training path (scaffold only — see `recipes/README.md`)

`QLoRA SFT (vision+LLM) → TRL DPO → Unsloth GRPO (frozen vision tower, deterministic
render-grounded reward)`. Gated on a real RECOGNITION_RESIDUAL GO; never starts in CI.
