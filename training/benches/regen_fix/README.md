# regen-fix bench (IP-18)

**Measures:** can a strategy repair a corrupted `aicad.part/1` document so it rebuilds,
without drifting design intent? Success bar (survey §6): **≥70% auto-fixed** (rebuild
passes + unaffected bodies compare ≈100%) with **0 fixes that silently change intent
dims**.

**Dataset** — corruption corpus, 500 docs, 5 mutation classes × 12 held-out structures:

| class | what it does | deterministic baseline can fix cleanly? |
|---|---|---|
| `param_out_of_range` | drives a feature-local numeric param to an impossible value | ✅ clamp |
| `suppressed_parent`   | suppresses a feature its children still depend on            | ✅ un-suppress |
| `circular_equation`   | two global equations reference each other                   | ❌ can break cycle only by guessing an intent value |
| `dangling_ref`        | a selector/profile/pattern points at a deleted feature id    | ❌ can't invent the target |
| `bad_selector`        | corrupts a selector's `kind`                                 | ❌ can't infer the kind |

The last three are the measured **RECOGNITION_RESIDUAL** a trained IP-18 model must beat
before its training task is greenlit.

**Reproducibility:** the corpus is generated deterministically from
`contracts/fixtures/part_bracket.json` + a fixed RNG seed by
`tools/flywheel/bench/corpus/regen_corpus.py` — no C++ core needed. Each example dir holds
`document.json` (the corrupted doc) and `good.json` (the pre-mutation known-good doc — the
reference the live verifier compares a fix against). `spec.json` freezes the content hash; a
second machine regenerating with the same seed gets the same hash. The docs themselves are
not committed (regenerated on demand); only the frozen spec is.

**Verifier (`verify_fix`):**
- *fixture mode* (offline, default): structural — the strategy claimed a fix, the anomaly is
  cleared, and no intent dim drifted.
- *live mode* (C++ core present, auto-detected): the **authoritative binary verifier** — the
  fix is rebuilt through the engine and compared (`eval_compare` / `compare_parts`) against
  `good.json`; **pass iff it rebuilds AND matches ≥99.5%**. A fix that rebuilds but altered
  geometry scores **0** — zero tolerance for silent intent changes (T-H11 driver note). Falls
  back to fixture scoring with a recorded reason when the core is absent.

**Baselines:** `deterministic` (kill-switch, measured offline — ~40% clean-fix rate, below the
0.70 model bar by design) and `frontier` (the IP-18 diagnosis strategy in
`services/agent/regen_diagnosis.py`, scored through the same verifier and logged as an IP-18
correction trace via T-H7). The frontier row is recorded **pending** — never a fabricated
number — until real provider credentials are present; wire it with
`regen_fix.make_frontier_baseline(provider, available=<real-creds?>)`.

**Migration criteria (FROZEN before any training):** `migration_criteria.json` +
`tools/flywheel/bench/migration.py`. Ship the local (Qwen3-4B) IP-18 model only if, on the
frozen bench, it is within 5 pts of the frontier fix-rate, at <1/50th cost, never below the
deterministic kill-switch, with zero intent drift — **and** a human signs off (T-H9). The
frontier target is pre-registered at ≥70% auto-fix / zero drift. No local model is trained or
wired in this task (frontier-first ship + logging seam only).

**Run:**
```
python -m tools.flywheel.bench.runner regen_fix          # full (500)
python -m tools.flywheel.bench.runner regen_fix --smoke  # gate-size (25)
```
`results/smoke_result.json` is a committed sample report (25 examples, fixture mode).
