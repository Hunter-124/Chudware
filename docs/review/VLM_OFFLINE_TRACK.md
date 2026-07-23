# Small Open-Weight VLMs for the Offline Vision Track (IP-21 / T-H10)

**Status:** Research pass verified against primary sources 2026-07-02 (HF model cards,
LICENSE files, merged llama.cpp PRs). Items that could not be verified are marked
**UNVERIFIED** — re-check them in T-H10 before training decisions.
**Companions:** [CUSTOM_MODEL_INTEGRATION_SURVEY.md](CUSTOM_MODEL_INTEGRATION_SURVEY.md)
(Pillar 16, IP-21), [AI_TOOLING_MASTER_PLAN.md](../AI_TOOLING_MASTER_PLAN.md) (Pillars 10/13).

**Scope:** open-weight VLMs ≤ ~9B params, commercial-safe, runnable via llama.cpp GGUF
(vision tower included), QLoRA-tunable + preference-tunable on one 24 GB GPU.

## Headline recommendation

- **Primary candidate: Qwen3-VL-4B-Instruct** (fallback up: **Qwen3-VL-8B-Instruct**) —
  Apache-2.0, best-in-class doc/OCR at size, native multi-image, llama.cpp vision
  merged, first-class Unsloth SFT+GRPO support, and it keeps the whole Chudware ML
  stack on one tokenizer/tooling family with the planned Qwen3-4B text translator.
- **Drawing-reading specialist: ibm-granite/granite-vision-4.1-4b** (or 3.3-2b) —
  Apache-2.0, document-extraction-specialized, official IBM GGUF **with mmproj**.
- **Hedge/alternative: Gemma 4 E4B-it** (released 2026-03-31) — now **Apache-2.0**
  (unlike Gemma 3's custom terms), day-0 llama.cpp vision+audio, Unsloth QLoRA ≈10 GB.
- RLHF plan is feasible as specified: Unsloth VLM GRPO runs Qwen3-VL-8B at 4-bit on a
  16 GB T4, so 24 GB is comfortable; published CAD precedent (cadrille) says **online
  GRPO beats offline DPO when the reward is programmatic** — which ours is.

## 1. Candidate models

| Model | Params | License | llama.cpp vision | Q4 GGUF (+mmproj) | Context | DocVQA / ChartQA / OCRBench / AI2D | Fine-tune recipes |
|---|---|---|---|---|---|---|---|
| **Qwen3-VL-2B-Instruct** | 2.13B | Apache-2.0 | Yes (PR #16780, merged 2025-10-30) | 1.06 GB + 0.78 GB | 256K→1M | lower tier of family (see note) | Unsloth (vision+LLM LoRA) |
| **Qwen3-VL-4B-Instruct** | 4.44B | Apache-2.0 | Yes (same PR) | 2.38 GB + 0.80 GB | 256K→1M | 95.3 / 84.6 / 881 / 84.1 (tech report Table 4) | Unsloth SFT+GRPO, TRL, LLaMA-Factory, ms-swift |
| **Qwen3-VL-8B-Instruct** | 8.77B | Apache-2.0 | Yes (same PR) | 4.70 GB + 1.10 GB | 256K→1M | 96.1 / ≈89.6 / ≈896 / 85.7 (see note) | Unsloth SFT+GRPO (T4-proven), TRL, LLaMA-Factory |
| **Qwen2.5-VL-7B-Instruct** | 7B | Apache-2.0 | Yes (PR #12402, merged 2025-04-27) | ~4.5 GB + ~1.2 GB | 32K (YaRN ext.) | 95.7 / 87.3 / 864 / 83.9 | mature everywhere (most-cited VLM-RL model) |
| **Qwen2.5-VL-3B-Instruct** | ~3.75B | **Qwen Research License — NON-COMMERCIAL** | Yes | 1.8 GB + 0.8 GB | 32K | 93.9 / – / – / 81.5 | n/a — **ruled out** |
| **Gemma 4 E4B-it** (2026-03) | 4.5B eff / 8B total | **Apache-2.0** | Yes, day-0 (ggml-org GGUFs) | ~4–5 GB (8B raw wts) | 128K | DocVQA n/a; OmniDocBench 0.181 (**UNVERIFIED** std doc scores) | Unsloth: QLoRA ≈10 GB, vision layers trainable |
| **Gemma 3 4B-it** | 4B | Gemma ToU (commercial OK, restrictions) | Yes (mmproj; + QAT Q4_0 from Google) | ~2.4 GB + mmproj | 128K | 72.8 / 63.6 / – / 63.2 (PT card) | Unsloth vision FT; TRL GRPO-tested |
| **granite-vision-3.3-2b** | ~3B total | Apache-2.0 | Yes — official IBM GGUF + mmproj | Q4_K_M 1.55 GB + mmproj f16 | 128K | 91 / 87 / 790 / 77 | official IBM FT notebook; PEFT/TRL |
| **granite-vision-4.1-4b** (2026-04-29) | 3.4B LLM + 0.6B vision | Apache-2.0 | Yes — official GGUF + mmproj | Q4_K_M avail. | n/s | chart/table/KVP specialist (94.2% VAREX KVP) | LoRA-native architecture (rank-256 vision adapter) |
| **InternVL3.5-4B / -8B** | 4.7B / 8.5B | Apache-2.0 (Qwen3 base) | InternVL 2.5/3 supported; **3.5 UNVERIFIED** | ~2.5 / ~4.8 GB est. | 32K SFT | 92.4 / 86.0 / 822 / 82.6 (4B); 92.3 / 86.7 / 840 / 84.0 (8B) | LLaMA-Factory, ms-swift; InternVL official scripts |
| **SmolVLM2-2.2B** | 2.2B | Apache-2.0 | Yes (multimodal.md) | ~1.3 GB + mmproj | small | 80.0 / 68.8 / 729 / 70 | TRL-native (GRPO-tested model) |
| **MiniCPM-V 4.5** | 8.7B | Apache-2.0 card tag (+ registration questionnaire — **verify LICENSE file**) | Yes (llama.cpp/Ollama per card) | ~5 GB est. | 32K+ | OpenCompass 77.0; "leading OCRBench" | LLaMA-Factory, ms-swift |
| **LFM2-VL-3B / LFM2.5-VL-1.6B** | 2.6B+0.4B / 1.6B | LFM Open License v1.0 (**terms UNVERIFIED**) | GGUF per Liquid docs | small | 32K | OCRBench 822 (3B) | limited ecosystem |
| **Moondream 2** (2025-06 rev) | 2B | Apache-2.0 | Yes | ~1.2 GB | n/s | 79.3 / 77.5 / 612 / – | single-image only |
| **Moondream 3 preview** | 9B MoE (2B active) | **BUSL-1.1 + use grant** | No | n/a | 32K | not published | cloud-only FT — **ruled out** |
| **Phi-4-multimodal-instruct** | 5.6B | MIT | **No vision in llama.cpp** | n/a | 128K | 93.2 / 81.4 / 844 / 82.3 | LoRA-native — but no GGUF vision path |
| **PaliGemma 2 3B** | 3B | Gemma ToU | **No** (PR #7553 never merged) | n/a | short | 73.6 / 54.0 / – / 76.0 | deployment dead-end |

Load-bearing sources:

- Qwen3-VL sizes/params/license: https://github.com/QwenLM/Qwen3-VL ;
  https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct ,
  https://huggingface.co/Qwen/Qwen3-VL-4B-Instruct (apache-2.0, 4,437,815,808 params),
  https://huggingface.co/Qwen/Qwen3-VL-8B-Instruct (8,767,123,696).
- Qwen2.5-VL-3B non-commercial: LICENSE — "Qwen RESEARCH LICENSE AGREEMENT … FOR
  NON-COMMERCIAL PURPOSES ONLY" —
  https://huggingface.co/Qwen/Qwen2.5-VL-3B-Instruct/blob/main/LICENSE. 7B is
  apache-2.0: https://huggingface.co/Qwen/Qwen2.5-VL-7B-Instruct.
- llama.cpp vision support: Qwen3-VL https://github.com/ggml-org/llama.cpp/pull/16780
  (merged 2025-10-30, DeepStack + IM-RoPE mmproj); Qwen2.5-VL
  https://github.com/ggml-org/llama.cpp/pull/12402 (merged 2025-04-27); supported list
  https://github.com/ggml-org/llama.cpp/blob/master/docs/multimodal.md; Gemma 3n vision
  merged 2026-01-09 https://github.com/ggml-org/llama.cpp/pull/18256.
- GGUF+mmproj sizes measured from repo trees:
  https://huggingface.co/unsloth/Qwen3-VL-4B-Instruct-GGUF (Q4_K_M 2,497,282,336 B;
  mmproj-F16 836,180,640 B), https://huggingface.co/unsloth/Qwen3-VL-8B-Instruct-GGUF
  (Q4_K_M 5,027,785,568 B; mmproj-F16 1,159,030,336 B),
  https://huggingface.co/ibm-granite/granite-vision-3.3-2b-GGUF (Q4_K_M 1.55 GB),
  https://huggingface.co/ibm-granite/granite-vision-4.1-4b-GGUF (mmproj-model-f16.gguf
  verified in file list).
- Benchmarks: Qwen2.5-VL-7B card; Qwen2.5-VL-3B card; InternVL3.5 tech report Table 4
  https://arxiv.org/html/2508.18265v2; granite-vision-3.3-2b card; Gemma 3 4B card;
  Phi-4-multimodal card; SmolVLM2 card; Moondream2 card; LFM2-VL-3B card;
  MiniCPM-V 4.5 card.
- **Qwen3-VL doc scores:** tech report https://arxiv.org/abs/2511.21631 Table 4, parsed
  from the PDF and spot-checked against secondary trackers — 8B: DocVQA 96.1 / ChartQA
  89.6 / OCRBench 896 / OCRBench-v2-en 65.4; 4B: 95.3 / 84.6 / 881 / 63.7. Individual
  cells carry ±column-alignment caution (the report's CountBench is oddly non-monotonic,
  4B 84.9 > 8B 80.5). OCRBench **v1 is saturating; v2 exposes the real gap** (most LMMs
  <50/100; Qwen2.5-VL-7B 46.7 with text *spotting* at 1.2% — arXiv 2501.00321) — Qwen3-VL
  is the visible generational jump. **MiniCPM-V 4.5** is the strongest 8B alternative
  (DocVQA 94.7 / OCRBench 890, LLaVA-UHD tiling to 1.8M px) pending its LICENSE check.
  Gemma 3's fixed 896×896 encoder + pan-and-scan is poor for drawing sheets full of
  2 mm text — another reason it's dominated here.
- Gemma 4: https://ai.google.dev/gemma/docs/releases ,
  https://huggingface.co/blog/gemma4 , https://huggingface.co/google/gemma-4-E4B-it ;
  Gemma 3 terms https://ai.google.dev/gemma/terms.
- Moondream 3 license (BUSL-1.1 + Additional Use Grant; converts Apache-2.0 after 2 y):
  https://huggingface.co/moondream/moondream3-preview/blob/main/LICENSE.md ;
  fine-tuning cloud-only: https://docs.moondream.ai/finetuning.
- InternVL3.5 Apache-2.0: https://huggingface.co/OpenGVLab/InternVL3_5-4B , /-8B.
- 2026 extras: **Qwen3.5 small multimodal** series (0.8B/2B/4B/9B, 256K ctx;
  GGUF works in llama.cpp-compatible backends but not Ollama —
  https://unsloth.ai/docs/models/qwen3.5; promising successor, **benchmarks not yet
  surveyed**); **DeepSeek-OCR** 3B MIT OCR specialist
  (https://huggingface.co/deepseek-ai/DeepSeek-OCR); **Molmo2** Apache-2.0 weights but
  non-commercial training-data caveat (https://huggingface.co/allenai/Molmo2-8B);
  **Ovis2.5-2B/9B** Apache-2.0 (https://huggingface.co/AIDC-AI/Ovis2.5-2B).

## 2. RLHF / preference-tuning feasibility (mid-2026)

- **TRL — mature multimodal alignment.** DPOTrainer fully supports VLMs (worked example
  Qwen2.5-VL-3B on RLAIF-V; set `max_length=None` or image tokens truncate) —
  https://huggingface.co/docs/trl/dpo_trainer. GRPOTrainer has an official VLM section
  (tested: Gemma-3-4b-it, LLaVA-NeXT, Qwen2-VL-2B, Qwen2.5-VL-3B, SmolVLM2-2.2B) +
  `grpo_vlm.py` example — https://huggingface.co/docs/trl/grpo_trainer. MPO/GRPO/GSPO/
  DPO/RLOO/Online-DPO for VLMs since Aug 2025 —
  https://huggingface.co/blog/trl-vlm-alignment. GRPO supports **multimodal tool
  responses** (a tool can return an image mid-rollout) — directly usable for
  render-in-the-loop training.
- **Unsloth — the 24 GB path.** Vision SFT with joint `finetune_vision_layers=True` +
  `finetune_language_layers=True` (https://unsloth.ai/docs/basics/vision-fine-tuning);
  VLM GRPO/GSPO official since Aug 2025: **Qwen3-VL-8B GRPO at 4-bit on a free 16 GB
  T4** (https://www.unsloth.ai/blog/vision-rl). **Key constraint:** vLLM cannot serve
  LoRA on vision layers ⇒ vLLM-accelerated GRPO requires `finetune_vision_layers=False`
  (freeze the tower during RL; train it during SFT). No documented Unsloth VLM-DPO —
  use TRL DPOTrainer + PEFT 4-bit for the DPO leg. Known friction: GRPO VRAM
  creep/OOM (unsloth issues #3864, #3512, #3542).
- **Others:** LLaMA-Factory — DPO/KTO/ORPO/SimPO for VLMs, `freeze_vision_tower`,
  QLoRA 7B ≈ 6 GB; ms-swift — DPO/GRPO/PPO/KTO across 300+ multimodal models;
  EasyR1 — multimodal GRPO reference but **full-FT only (3B needs 40 GB)** — not our
  24 GB path.

**CAD visual-feedback precedents (verified):**

| Work | Model | Method | Reward | Result |
|---|---|---|---|---|
| CADFusion (ICML 2025) | LLaMA-3-8B LoRA r=32 | alternating SFT + visual-feedback DPO | preference pairs from rendered images | F1 85.2 sketch / 92.8 extrusion — https://arxiv.org/abs/2501.19054 |
| cadrille (ICLR 2026) | Qwen2-VL-2B | SFT → online RL (Dr. GRPO/CPPO) vs offline DPO | deterministic 10×IoU vs GT mesh, −10 invalid | IoU 67.9 vs DPO 61.3; **online RL > DPO given programmatic reward** — https://arxiv.org/abs/2505.22914 |
| CAD-Coder | Qwen2.5-7B | SFT + GRPO | Chamfer distance + format | mean CD 6.54 vs 29.29 — https://arxiv.org/html/2505.19713v3 |
| ReCAD (2025-12) | Qwen2.5-VL-7B | GRPO + off-policy guidance | best-alignment IoU + DINOv2 render-similarity | CD 72.47 vs 98.12 — https://arxiv.org/html/2512.06328v1 |

2026 successors (abstracts verified only): ToolCAD (arXiv:2604.07960, agentic CAD gym +
online GRPO), PR-CAD (arXiv:2604.19773), DreamCAD (arXiv:2603.05607).

**Feasibility verdict:** QLoRA SFT (vision+LLM LoRA) → TRL DPO (24 GB fine at 3–8B) →
Unsloth GRPO at 4-bit with frozen vision tower + our deterministic geometry rewards.
Note: all published CAD-RL used multi-GPU full-precision full-FT; the 4-bit LoRA
single-GPU variant is tool-supported but is *our* novelty — budget for VRAM-creep
debugging.

## 3. Task fit

- **(a) Render self-review:** start **Qwen3-VL-4B** (8B if VRAM allows: 4.7 GB Q4 +
  1.1 GB mmproj). It's the exact class in the Unsloth GRPO notebooks, so
  render-grounded GRPO is turnkey. Renders are in-distribution by construction —
  expect post-SFT accuracy well above the ~70–85% generic zero-shot floor.
- **(b) Drawing/dimension-callout reading:** start **Qwen3-VL-8B** with
  **granite-vision-4.1-4b** as the cheap table/callout extraction specialist.
  **Real-drawing zero-shot floor is much lower than DocVQA suggests:** MechVQA (3,281
  mechanical drawings, 20,778 QA pairs — https://arxiv.org/html/2605.30794) reports
  overall **60.2% for Qwen3-VL-4B** (60.5% MiniCPM-V-4.5, 49.8% InternVL3.5-8B); on the
  Dimension & Annotation subtask specifically, Qwen3-VL-4B hits **77.0%** — already at
  GPT-4o level (75.9%). And the benchmark's own fine-tuned **MechVL-4B-RL reaches 84.9%
  overall / 90.7% DA, beating Gemini-3-Pro (77.3%)** — direct published proof that a
  domain-tuned 4B beats frontier on this exact task. Corroborating: the Florence-2 GD&T
  study (fine-tuned 0.23B beats zero-shot GPT-4o by +52.4% F1 with only 400 drawings —
  https://arxiv.org/abs/2411.03707). With our unlimited synthetic drawings, 85–95% on
  dimension-text reading is a defensible post-SFT target; **GD&T frames (perpendicularity/
  flatness callouts, merged duplicate dims, unilateral/bilateral tolerances) are the
  documented failure mode for every model** and will lag. Deterministic fiducial code
  keeps doing anything metrological.
- **(c) Photo feature triage:** **Qwen3-VL-4B** fine-tuned on synthetic-render →
  real-photo pairs; SmolVLM2-2.2B is the budget always-loaded option. Expect a
  sim-to-real gap — augment (lighting/noise/backgrounds) + a modest real-photo set.
- **(d) Render-vs-photo compare:** requires native **multi-image** (Qwen2.5/3-VL,
  InternVL3.5, SmolVLM2 yes; **Moondream single-image — excluded**; Phi-4-mm yes but
  no llama.cpp). Start **Qwen3-VL-8B** on synthetic known-diff render pairs (free from
  our pipeline). Fine-grained pair-difference detection is a **measured** weakness of
  the whole model class: DiffSpot (arXiv 2605.29615) — best of 13 frontier VLMs recalls
  only **40.7%** of true changes, <23% on the hard tier; DiCoBench (arXiv 2606.26602) —
  humans 98.3%, all 18 tested MLLMs fail micro-scale details. So: the VLM answers coarse
  semantic questions ("same part family? flange on the correct side? hole pattern
  present?"), deterministic code (silhouette/edge-map diff, compare_parts) does the
  fine-grained verdict. ReCAD's DINOv2-similarity reward is the proven trainer for the
  comparison skill; Img-Diff (arXiv 2408.04594) is the data-synthesis recipe for
  difference-perception training pairs.

## 4. Rule-outs

| Model | Reason |
|---|---|
| Qwen2.5-VL-3B | Research license, non-commercial only — use Qwen3-VL-2B/4B instead |
| PaliGemma 2 | no llama.cpp (PR #7553 never merged); superseded by Gemma 3/4 |
| Phi-4-multimodal / Phi-4-reasoning-vision-15B | MIT + strong doc scores but llama.cpp can't run the vision tower — GGUF dead-end; 15B also over budget |
| Moondream 3 preview | BUSL use-grant conflicts with bundling in a paid app; cloud-only fine-tuning kills local render-grounded RL; single-image; no llama.cpp |
| FastVLM (Apple) | apple-amlr research license |
| Kimi-VL-A3B | MIT but 16B total weights; no verified llama.cpp vision |
| Molmo2 | training-data non-commercial caveat; no llama.cpp support found |
| DeepSeek-OCR | OCR specialist, not a general VLM; could serve as an offline drawing-text pre-extractor if vLLM acceptable |
| LFM2-VL family | license terms unverifiable (primary URL 404); thin RLHF ecosystem |
| Gemma 3 4B | not disqualified, dominated: weakest doc scores of finalists + custom ToU vs Gemma 4 E4B's Apache-2.0 |
| EasyR1 (framework) | full-FT only, 3B needs 40 GB |

## 5. Open gaps (re-verify in T-H10 before training)

1. Qwen3-VL-2B bench numbers (4B/8B now parsed from tech-report Table 4 and
   spot-checked; individual cells still carry ±column-alignment caution).
2. Gemma 4 E4B standard doc scores — run our own drawing eval.
3. InternVL3.5 llama.cpp mmproj support (2.5/3 listed; 3.5 unconfirmed).
4. LFM Open License v1.0 commercial terms.
5. MiniCPM-V 4.5 LICENSE file (card tag vs historical OpenBMB model license).
6. Qwen3.5 small multimodal series — survey before committing to Qwen3-VL.
7. TRL per-feature release versions (features verified in current stable docs).
