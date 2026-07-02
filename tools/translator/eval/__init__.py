"""Slot-fidelity eval harness (Pillar 13 — eval-harness-first gate).

Primary metric: SER-style slot accuracy (unit-normalised) over the ground-truth slots
(`gsl_ser.enumerate_slots`) — reference-free and exact because we hold the state.
Intrinsic (wrong value for a real slot) and extrinsic (invented feature) hallucination
are reported SEPARATELY; numeric accuracy is stratified by magnitude/unit. Extrinsic
detection consults the groundedness seam (T-H4-owned impl).

`slot_accuracy` is the scorer; `harness` runs it over a gold set with a held-out
STRUCTURES split and emits the baseline numbers appended to docs/RECOGNITION_RESIDUAL.md.
"""
