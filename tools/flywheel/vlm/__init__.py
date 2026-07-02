"""Offline VLM track v0 (IP-21 / T-H10).

Gives the offline/limited-context mode *eyes*: render self-review, drawing/dimension
reading, part-photo feature triage, and render-vs-photo compare. The LOCKED division of
labor (survey Appendix A — never invert): a VLM proposes COARSE, confidence-tiered claims
about what an image shows; deterministic code verifies/measures those claims against known
geometry. The VLM never measures — fiducial/metrology code owns that (T-F5).

v0 SCOPE FENCE (Driver notes, RATIFIED): bench + synthetic corpus + zero-shot baselines
ONLY. NO training runs. The SFT/DPO/GRPO recipe FILES under `recipes/` may land but must
NEVER execute in CI (see `recipes/guard.py`). Training waits for the T-H8 VLM bench
residual (RECOGNITION_RESIDUAL discipline).

Modules:
  * `candidates`     — the 3 RATIFIED local candidate models (registry only, no download).
  * `candidate_eval` — zero-shot residual harness: run a candidate over the corpus, score
                       its claims through the T-H8 VLM bench, measure residual over the
                       deterministic ceiling. Proven end-to-end offline with a mock
                       candidate; runs the real models when llama.cpp + weights provision.
  * `corpus_gen`     — synthetic vision corpus v1 (labeled renders, drawings with known
                       callouts, known-diff render pairs); geometry behind every image is
                       KNOWN (that ground truth is the later reward signal); provenance
                       stamped "synthetic".
  * `serving`        — the `experimental.vision_review` serving seam: an OpenAI-compatible
                       loopback vision endpoint (llama.cpp --mmproj), conforming to the
                       T-F5 providers/1 image part.
"""
