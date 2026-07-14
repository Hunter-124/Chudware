# Findings — grok45-1 (baseline C1 × xai:grok-4-5, 3 seeds)

Source: static monitor scan of `reports/runs/grok45-1/` (monitor.py WS5).
Monitor summary: 3 cells, 144 events, 17 findings
(`missing_tool=10`, `retry_storm=2`, `repeated_tool_error=1`, `guardrail_block=1`,
`provider_error=1`, `cycle_failure_class=2`).

## What the model struggled with

1. **SPECIFY never produced a usable geometry handoff.** Seeds reach BUILD/VERIFY with
   empty dimensions/requirements sections and no part document. Acceptance criteria all
   fail (`incoherent_geometry` on every seed).
2. **`new_part` is not reachable as a first-class tool call.** BUILD tries
   `add_features` with `op: new_part` and gets `No current part. Call new_part(...) first`
   (s3 seqs **58, 60**). Then repeatedly calls `describe_part` / `fit_report` against the
   empty doc (s3 **54, 56, 67, 74**).
3. **Overseer / PLAN thrash on ledger sections.** Models probe invalid
   `ledger_get` sections (`manifest`, `Artifacts`, `all`) and empty real sections
   (`requirements`, `dimensions`, `geometry_specs`) without a recovery strategy
   (s1 **26–32**, s3 **44, 63**).
4. **Identical retries (grind / retry storm).** `ledger_get|{}` ×4 (s3 **16, 26, 51, 62**)
   and `describe_part|{}` ×4 (s3 **54, 56, 67, 74**). Taxonomy also records grind on s3.
5. **Hard guardrail block.** `verify_battery` blocked at VERIFY (s3 seq **70**) because
   geometry never existed — battery never PASS/WAIVED.
6. **Transport.** One xAI 429 `provider_error` mid-SPECIFY complete (s3 seq **29**).
7. **tools_slot unresolved (role cards advertise tools not in the slot):**
   `dag_status`, `channel_send`, `request_resolve`, `council_convene`, `dag_run`,
   `dag_replan` (s1 **24, 33**; s3 **25, 30, 41**).

## Tooling improvements (priority order)

### T1 — Make `new_part` a first-class offered tool (or accept `op:new_part` in add_features)

- **Symptom:** BUILD cannot create a document; every geometry tool returns the same
  error; retry storm on `describe_part`.
- **Evidence:** s3 seqs **54, 56, 58, 60, 67, 74**; repeated_tool_error count=4.
- **Fix:** Offer `new_part` in the BUILD tools_slot **or** teach `add_features` to
  bootstrap a part when the first op is `new_part` / when no current part exists.
  Error text already names the missing tool — the surface must include it.

### T2 — Honest / actionable `ledger_get` section errors + prompt the allowed list once

- **Symptom:** Models invent section names (`manifest`, `Artifacts`, `all`) and burn
  turns; empty sections return success with `data: []` so the model re-queries.
- **Evidence:** s1 seq **26** (`unknown section 'manifest'`); s3 seq **44**
  (`Artifacts`), **63** (`all`).
- **Fix:** (a) alias common mistakes (`manifest`→`artifacts`, case-fold);
  (b) on empty `requirements`/`dimensions` during SPECIFY, return a hint:
  "no dimensions recorded — call ledger_note(kind=dimension|…)" rather than a silent
  empty list; (c) put the allowed section list in the SPECIFY role card once.

### T3 — Close tools_slot / role-card gap for unresolved tools

- **Symptom:** `tools_slot.unresolved` lists tools the role prompt implies exist
  (`dag_status`, `council_convene`, `channel_send`, `request_resolve`, `dag_run`,
  `dag_replan`) but the slot omits them → models either invent calls or thrash.
- **Evidence:** s1 seqs **24, 33**; s3 seqs **25, 30, 41** (missing_tool findings).
- **Fix:** Either offer the tools those roles need, or strip the prompt mentions and
  replace with the concrete substitute (`ledger_get` / `describe_project` / etc.).
  Prefer offering `dag_status` to PLAN/SPECIFY-complete roles — it is the natural
  progress query and currently unresolved.

### T4 — Surface geometry-bootstrap failures before VERIFY guardrail

- **Symptom:** VERIFY hits `verify_battery` **blocked** with no part; late, expensive.
- **Evidence:** s3 seq **70**.
- **Fix:** Phase gate or preflight: if BUILD exits with zero successful mutating
  geometry tools, fail closed with a structured "no_part_created" handoff instead of
  advancing to VERIFY.

### T5 — `ledger_note` kind validation with allowed-kinds in the error

- **Symptom:** unknown kinds `deliverables_manifest`, `org_structure`, `ruling`
  (intake/organize) waste turns; error does not list allowed kinds.
- **Evidence:** tool-error scan across cells (ledger_note unknown kind × several).
- **Fix:** Error payload should include `allowed: [...]` (as `ledger_get` already does
  for sections) and alias a small set of intake kinds onto the schema the pipeline
  actually reads.

## Monitor artifacts

- `monitor.json` / `monitor.md` written beside this file by
  `python3 -m tools.flywheel.bench.gym.monitor grok45-1 --write`.
