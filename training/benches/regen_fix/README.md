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
`tools/flywheel/bench/corpus/regen_corpus.py` — no C++ core needed. `spec.json` freezes the
content hash; a second machine regenerating with the same seed gets the same hash. The docs
themselves are not committed (regenerated on demand); only the frozen spec is.

**Baselines:** `deterministic` (kill-switch, measured offline — ~40% clean-fix rate, below
the 0.70 model bar by design) and `frontier` (pending T-H7 logging + credentials).

**Run:**
```
python -m tools.flywheel.bench.runner regen_fix          # full (500)
python -m tools.flywheel.bench.runner regen_fix --smoke  # gate-size (25)
```
Offline scoring is structural (fixture mode). With the C++ core present, the same fixer's
output is rebuilt and scored THROUGH the T-H4 battery (round_trip / is_manifold); that live
path is skipped-with-reason when the core is absent.

`results/smoke_result.json` is a committed sample report (25 examples).
