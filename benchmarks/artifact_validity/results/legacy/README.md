# Legacy artifact_validity results (2026-07-29) — not comparable to current trials

`baseline.*` and `smoke.*` were produced by the retired prompt-set benchmark (12 prompts /
31 criteria, `turn_driver.py` + `probe.py` + the old `scoring.py`). They are kept unchanged
as historical evidence. Do not compare them with trials from the current task suite, and
do not quote them as current agent quality:

- Criteria were satisfied per-criterion by the "best" of all documents a turn touched
  (closest volume, closest bbox, largest health), not by one declared deliverable.
- Volume-only hole counting accepted a 15-hole plate as the required 16-hole grid
  (difference 0.296 % inside a 0.5 % tolerance).
- Drawing, ECAD, simulation and mass criteria read numbers from the turn's recorded tool
  payloads (ledger), not from a reopened saved artifact or saved study.
- One run per prompt; `daemon_mode: true` in their metadata differs from the retired
  driver's documented in-process engine; the artifacts they reference lived under
  machine-local `results/runs-*` paths that were never bundled.
- The retired driver's response decoder expected a structured `turn.result`; the current
  worker returns terminal text, so the old pipeline cannot reproduce these runs.

Current trials live under `benchmarks/artifact_validity/runs/<tag>/` (see ../../README.md).
