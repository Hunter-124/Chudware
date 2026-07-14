# Findings — grok45-2 (C1 × xai:grok-4-5, 3 seeds, post specify-fix-1)

Source: real gym run + static monitor scan of `reports/runs/grok45-2/`.
Result: **0/3 pass**, taxonomy `incoherent_geometry=3`, `grind=3`.
Missions: s1/s2 `failed` phase=`plan`; s3 `failed` phase=`specify`.

Monitor: 3 cells, 164 events, 15 findings
(`missing_tool=6`, `retry_storm=3`, `repeated_tool_error=1`, `provider_error=2`,
`cycle_failure_class=3`).

## What the model struggled with

1. **Still no geometry.** All three seeds fail ACs. SPECIFY records some dimensions
   (bore H7, process) but BUILD never creates a part document. s3 reaches DELIVER with
   reviewer note: "No current part in engine; ledger empty of build…" (seq **100**).
2. **Short s1/s2 runs die at plan** after partial SPECIFY (34 / 23 events) — pipeline
   stops before BUILD; stop_reason on mission failed at plan.
3. **s3 longer thrash (107 events)** with:
   - `buildgraph_status` "run not found" ×3 (seqs **25, 36, 62**) — retry storm
   - `ledger_get|{}` ×3 (seqs **10, 17, 76**) and `ledger_get|budget_tokens:2000` ×3
     (seqs **21, 66, 92**)
   - missing tools: `dag_status`, `channel_send`, `request_resolve`, `council_convene`,
     `dag_run`, `dag_replan` (seqs **20, 33, 49**)
   - `describe_part` / `fit_report` / `measure_part` without part
   - invalid ledger sections: `manifest`, `specs`, `acceptance_criteria`
   - invalid ledger_note kinds: `review_verdict`, `audit`
4. **Transport:** xAI 429 (seq **19**), read timeout (seq **64**).
5. **Offline research footguns:** `research_material_lookup` / `research_web_search`
   return `offline` mid-SPECIFY (s1), burning turns.

## Tooling improvements (with evidence)

### T1 — Bootstrap part creation (still #1)

Same as grok45-1: offer `new_part` or accept `op:new_part` in `add_features`.
s3 still hits "No current part…" on describe/fit/measure; DELIVER blocked (seq **100**).

### T2 — `buildgraph_status` / workspace session honesty

- **Symptom:** `buildgraph_status` "Build graph run '…' not found" ×3 identical retries.
- **Evidence:** s3 seqs **25, 36, 62**.
- **Fix:** On missing run, return a structured hint: "no build graph yet — call
  buildgraph_plan / add_features first" and include next-step tools. Do not return a
  naked not-found that invites identical retries.

### T3 — tools_slot vs role-card unresolved set (recurring)

- **Evidence:** s3 seqs **20, 33, 49** — `dag_status`, `council_convene`, etc.
- **Fix:** Offer `dag_status` to PLAN/SPECIFY-complete, or remove from prompts.

### T4 — ledger_note allowed-kinds + aliases

- **Evidence:** `unknown kind 'deliverables_manifest'` (s1), `review_verdict` / `audit`
  (s3).
- **Fix:** `allowed: [...]` in error payload; alias intake kinds to pipeline schema.

### T5 — Research tools offline messaging

- **Evidence:** s1 offline `research_material_lookup` / `research_web_search`.
- **Fix:** When offline, surface bundled/catalog fallbacks immediately and say
  "offline — using bundled only; do not retry web".

## Monitor note

Live `--follow` initially quiet-exited after first cell pause (fixed: follow now waits
for all cells complete or `gym.cycle_summary`). Static rescan after run is authoritative
(`monitor.json` / `monitor.md`).
