# T-H4 questions / coordination notes (sonnet-tomo)

## 1. Groundedness checker ownership (T-H4 ∩ T-F10) — FYI, no blocker

T-F10 was **unclaimed** at claim time (checked `work.py board` 2026-07-02), so per
the driver notes T-H4 landed the shared implementation:

- Module: `tools/translator/groundedness.py`, `check_groundedness(model_text, state_dict)`
  → `{score, matched, spurious, unmentioned_state}`.
- Semantics (locked here, T-F10 should consume as-is): **spurious = numerals in the
  TEXT with no state match** (hallucination risk; the only thing that costs reward);
  `unmentioned_state` = state slots the text never cites (informational, never
  penalized). score = matched / total-text-numerals; no numerals → vacuously 1.0.
- Unit-normalized compare (mm/cm/m/in/ft, MPa/GPa/ksi/psi, N/kN/lbf, g/kg/lb,
  deg/rad), ±5% relative tolerance.
- DoD says "groundedness validated against T-F10's harness fixtures" — those fixtures
  don't exist yet (T-F10 todo). Unit tests in `services/mcp/test_verify_battery.py`
  cover exact/cross-unit/ksi-MPa/attached-unit/hallucination/vacuous cases. When
  T-F10 lands its fixtures it should point them at this module (one implementation,
  two callers) and extend the test matrix rather than reimplement.

**Recommended answer:** accept as-is; T-F10 consumes. Alternative: T-F10 renames
fields — if so it must update `services/verify/battery.py:_gap_groundedness` and the
tests in the same PR.

## 2. req_join vs T-H3's aicad.req/1 (T-H3 is `doing`, sonnet-riko) — stub contract

`req_join` in `services/verify/battery.py` implements the §3.5 sketch directly from
the survey (T-H3's contract wasn't frozen when this landed):

- Statuses honored: proposed | user_confirmed | verified | failed | waived
  (plus generic pass/ok). `waived` excluded from the denominator.
- The join itself: a `verified` claim with explicitly empty `trace.evidence` is
  **inconsistent** and counts unmet (this is the failure mode the checker exists for).
- Input: the battery takes `requirements=[...]` (the `requirements` array of an
  aicad.req/1 doc). It does NOT yet load a requirements doc by id — T-H3 owns
  storage/loading; when that lands, `_gap_req_join` grows a doc-id path.

**Recommended answer:** T-H3 reviews the met/unmet mapping above when freezing the
schema; any status renames need a one-line update in `_gap_req_join` + tests.
Alternative: battery waits for T-H3 — rejected, DoD requires the checker now and the
stub is schema-shaped.

## 3. Unit-sanity range table location — FYI

Driver notes say the table "lives in the contract-adjacent constants, cited by T-H3's
quantity validation too". There is no contracts/ python package, so it lives at
`services/verify/unit_ranges.py` (importable standalone, no engine deps). T-H3:
import `RANGES` / `check_unit_ranges` from there rather than duplicating.
