# elicitation bench (IP-12)

**Measures:** does a conversation turn into a schema-valid `aicad.req/1` design brief that
covers what the human actually asked for? Scored as **slot coverage + precision** against a
human-audited gold brief (survey §6). Success bar: **>=90% coverage** and **100%
schema-valid** across the set.

## Slot model

Each requirement in a brief exposes typed "slots": `kind`, `quantity.param`, `quantity.op`,
`quantity.value`, `quantity.unit`, `quantity.value_max` (only when `op == "range"`), and one
`verify_by:<verifier-name>` membership slot per attached verifier.

- **Coverage** = fraction of GOLD slots (summed across all gold requirements) present with
  an equal value in the candidate brief. A gold requirement with no candidate counterpart
  contributes zero covered slots.
- **Precision** = fraction of CANDIDATE slots that match a gold slot. A spurious
  requirement, or a matched requirement with a wrong param/value/unit/verifier, lowers
  precision without moving coverage.
- **`score`** per example = `mean(coverage, precision)`; `passed` = `coverage >= 0.90 AND
  schema_valid`.

Gold requirements are paired to candidate requirements by (1) exact `quantity.param` match,
then (2) same `kind` in first-seen order for anything left over — `id`/`need` strings are
opaque per-document tokens, not shared vocabulary between two independently produced
briefs, so matching never uses them. See the docstring in
`tools/flywheel/bench/benches/elicitation.py` for the full rule and `match_requirements()`
for the implementation.

## Schema-valid check

A candidate brief is schema-valid iff every `kind`/`status`/`verify_by[].verifier` value is
a member of `VALID_KINDS` / `VALID_STATUSES` / `VALID_VERIFIERS` (imported from
`engine_oracle/requirements/requirements.py`, with a local fallback copy if that import path
isn't wired), every requirement names >=1 verifier, and every `need` resolves to a declared
stakeholder need. An invalid brief scores `0.0` outright — coverage/precision are not
computed for it. This is narrower than the full `RequirementsDoc.validate()` (it does not
enforce the "no numerals in prose text" style convention from `contracts/requirements.md
§0`), matching the T-H8 spec for this bench.

## Deterministic baseline (kill-switch)

`deterministic_extract()` is a regex/keyword extractor: it finds `NUMBER UNIT` tokens in the
raw conversation text, guesses a `quantity.param` from nearby keywords, and guesses a
`kind`/`verifier` pair from keyword hits in the sentence. It never reads the gold brief. On
the frozen gold set it measures **coverage ~0.60, precision ~0.68, schema_valid_rate =
1.0, 0% examples pass the 0.90 coverage bar** — a deliberately imperfect kill-switch that
leaves a clear residual (purely qualitative asks with no adjacent number — symmetry,
interface fit, standards compliance stated without a number — are the main gap; see
`bracket_shelf`/`enclosure_ip65`/`plate_gusset` for cases where it under-covers).

## Dataset

10 hand-authored `(conversation, gold_brief)` pairs under `gold/*.json`, spanning 4 part
families (`bracket` x3, `shaft` x2, `enclosure` x3, `plate` x2) used as the held-out
**structure** key (P13: split on structures, not samples) — so a held-out evaluation can
hold out an entire family. Every gold brief was validated with the real
`RequirementsDoc.from_dict(...).validate()` at authoring time. `spec.json` freezes the
content hash over `gold/` (OS-independent, EOL-normalized per `tools/flywheel/bench/dataset.py`).

**The real >=50 real-team-session gold set is PENDING** (fed by T-H1 telemetry / the T-H5
data store, per the T-H8 spec). This hand-authored 10-example set ships the scorer +
matching rule + deterministic baseline now so the framework, the two-baseline-row
convention, and the residual reporting are exercised end-to-end; it is not a substitute for
the audited real-session corpus.

## Baselines

- `deterministic` — the kill-switch extractor above, measured offline (~0.64 mean score).
- `frontier` — pending T-H7 frontier logging + provider credentials (`status='pending'`,
  no fabricated scores).

Residual is `status='pending'` until the frontier row lands; once measured, it is
`frontier_mean - deterministic_mean` on this frozen set, same convention as `regen_fix`.

## Run

```
python -m tools.flywheel.bench.runner elicitation          # full (10 examples)
python -m tools.flywheel.bench.runner elicitation --smoke  # held sample (4 examples)
```

(`elicitation` is registered into the shared `REGISTRY` alongside the other T-H8 benches;
this module only exposes `DRIVER = BenchDriver("elicitation", _run)` per the T-H8 bench
contract — wiring it into `tools/flywheel/bench/runner.py`'s `REGISTRY` is done where the
other benches are wired in.)

`results/smoke_result.json` is a committed sample report (4 held examples, `--smoke`).
Offline-only: no C++ core is used anywhere in this bench.
