"""Chudware learned-translator track (Pillar 13).

Stage-0 home: the frozen `gsl-ser/v1` serialization (`gsl_ser`), the groundedness
seam shared with T-H4 (`groundedness`), and the slot-fidelity eval harness (`eval/`).
NO TRAINING lives here — training is gated (T-F11) on the eval this package builds.

See `contracts/gsl_ser.md` (the normative spec) and `docs/AI_TOOLING_MASTER_PLAN.md`
§Pillar 13 (the binding architecture boundary: the learned layer never invents a
numeral; numbers are copied from the serialization and verified post-hoc).
"""
