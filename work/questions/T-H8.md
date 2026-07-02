# T-H8 questions (opus-hex) — non-blocking, proceeding with recommended answers

## Q1 — CAM bench: score through the battery, or read `cam_simulate` raw?

**Context.** The task says every bench scores THROUGH the T-H4 verifier battery
(`services/verify/battery.py`), never raw tools. But T-H4's `_extract_score`
(battery.py ~L342-362) only recognizes scalar keys
`match_pct/score/min_fos/cd/max_von_mises_mpa/coverage_ratio`. `cam_simulate`'s real
result fields are `machined_fraction` (0..1) and `gouges` (int count) — neither is in
that list, so the battery's generic scoring silently degrades the `cam_simulate` check
to a bare pass/fail `ok`, discarding the two numbers the CAM bench needs (gouge-free +
machined fraction).

**Recommended answer (proceeding on this).** The CAM bench scorer reads `cam_simulate`'s
raw result directly (`gouges == 0` for gouge-free, `machined_fraction` for the fraction
metric) and records the battery's `cam_simulate` pass/fail alongside as the through-
battery signal. I do **NOT** edit `battery.py` — it is T-H4's merged, reviewed deliverable
and adding CAM keys to `_extract_score` is a scope violation for T-H8. The clean long-term
fix belongs in a T-H4 follow-up: extend `_extract_score` to recognize `machined_fraction`
(scalar) and treat `gouges` as a binary-safety key. Filed here so the battery owner rules
on it; blocks nothing.

**Alternative considered.** Have the CAM bench call only the battery and accept the
degraded bare-ok signal — rejected: it throws away the machined-fraction metric the survey
§6 success bar is defined on.

## Q2 — Corpus scale: which DoD numbers are met offline vs gated on infra/data?

Recording the honest split (proceeding, not blocking):
- **≥500 mutated regen-fix docs — MET offline.** The corruption mutator operates on
  document JSON (no C++ core needed to mutate), so a seeded deterministic mutator over the
  committed seed docs produces ≥500 labeled corruptions, reproducible from the frozen hash.
- **Frontier baseline rows — PENDING (gated).** No network/credentials offline; every
  bench records the frontier row as `status: pending` (honest stub, T-F13 precedent) to be
  filled when T-H7 logging + provider credentials exist.
- **Elicitation ≥50 real team sessions — PENDING (consent-gated).** Ships the scorer + a
  small hand-authored aicad.req/1 gold set now; the real ≥50-session gold set is fed by
  T-H1 telemetry / T-H5 store when consented data lands.
- **spec→sketch / CAM / VLM live scoring — runs when the C++ core is present** (gate stage
  6, env-gated like OCCT); offline they score committed fixtures with recorded expected
  outcomes. Tests skip-if-no-core.

## Q3 — req_join ↔ contract-constants alignment (post-merge note, NOT actioned here)

The T-H8 driver notes ask to align `req_join` with the T-H3 contract constants + the
unit_ranges import "if nobody has yet". Findings after the H3+H4 merge:
- **unit_ranges: already wired** — `battery.py` `_gap_unit_sanity` imports
  `services.verify.unit_ranges.check_unit_ranges` directly. Nothing to do.
- **req_join met-statuses: intentionally looser** — battery's `_gap_req_join` hardcodes
  `met_statuses = ("pass","verified","ok")` rather than importing T-H3's
  `VALID_STATUSES` ladder (`proposed|user_confirmed|verified|failed|waived`), per its own
  docstring. Reconciling them means editing `services/verify/battery.py`, which is T-H4's
  merged deliverable — **out of scope for T-H8** (hard rule: no edits outside my file
  list; contracts/verifier semantics are the owner's call). **Recommended:** a T-H4
  follow-up maps req_join's met-status set onto the contract ladder. The elicitation bench
  here consumes `VALID_VERIFIERS/STATUSES/KINDS` from `engine_oracle/requirements` for its
  schema-valid check, so the bench side is contract-aligned regardless.
Non-blocking; recorded so the owner rules on it.
