MANUFACTURING PLANNING — turn functional intent into a cited, inspectable, shop-ready route before geometry is committed.

PLAN FIRST
1. Identify function, interfaces, load paths, quantity, material/condition, available machines, finish, and inspection constraints. Record unknowns; never invent them.
2. Call `resolve_process` with the request, material, and quantity. Treat its result as a recommendation, then check stock, feature access, tolerance, finish, tooling, and quantity economics before accepting it.
3. Bind the accepted process to the source document with `set_process` when a document exists.
4. Call `mfg_plan_upsert` before modeling. Use one stable plan id (normally main); revise that plan instead of creating version-suffixed siblings.
5. Put the selected process/profile, material condition, quantity, stock, machine assumptions, ordered setups/operations, audit-native critical rows, inspection holds, work steps, references, assumptions, and open questions in the plan.
6. Keep every numeric critical row cited. Derived values need provenance plus derived_from/derivation. A remembered handbook value is not provenance.
7. After geometry changes, update the same plan revision, rerun checks, and regenerate downstream drawing/work-instruction outputs.

FUNCTIONAL FIT CHAIN
1. State the function first: sliding guidance, accurate location, bearing seat, removable hub, permanent interference, or another explicit interface behavior.
2. Call `recommend_fit` with that function. Do not select H7/g6 or another familiar class from memory.
3. Call `fit_calc` with the selected designation and governing nominal diameter. If a recommended class is unsupported, choose a supported alternative with an explicit engineering reason or block the plan.
4. Write the result directly as an audit-native critical row. Use type dimension_tolerance, a stable requirement_id/dim_ref, nominal value/unit, structured tolerance, engineering limits, critical=true, provenance naming the calculator inputs/result, and derived_from links.
5. Preserve signed engineering deviations separately from drawing-storage minus magnitudes; never silently flip a lower deviation.

Example row shape (values are illustrative only; replace them with the live calculator result):
```json
{
  "requirement_id": "DIM7",
  "type": "dimension_tolerance",
  "name": "bearing_bore",
  "value": 20.0,
  "unit": "mm",
  "tolerance": {"type": "deviation", "upper": 0.021, "lower": 0.0},
  "limits_mm": [20.0, 20.021],
  "critical": true,
  "dim_ref": "DIM7",
  "provenance": "fit_calc(nominal=20, fit=H7/g6), result <ledger/result id>",
  "derived_from": ["calc:<result id>"]
}
```

PROCESS SELECTION
- Prefer machining for low/medium quantity, tight tolerance, good finish, and accessible subtractive geometry; split mill/turn operations when the feature axes demand it.
- Prefer sheet processes for uniform-gauge parts dominated by cuts and bends; plan bend relief, inside radius, grain direction, and flat-pattern inspection.
- Prefer additive for low quantity or inaccessible internal geometry only when anisotropy, support access, finish, and dimensional capability are acceptable.
- Prefer molding only when quantity can repay tooling and the design supports draft, uniform walls, parting, ejection, shrink allowance, and secondary operations.
- Treat laser/waterjet/profile cutting as a blank-making step when holes, edges, finishes, or tolerances require secondary machining.
- Use `machining_ref_search` for cited process capability, stock/allowance, reaming/drilling, and finish ranges; retrieve the full cited row with `machining_ref_get`.
- After modeling, call `dfm_check`; distinguish not-evaluated from passed. A process profile or empty check list is not proof of manufacturability.

DATUM AND SETUP STRATEGY
- Choose primary, secondary, and tertiary datums from functional mating or locating surfaces; constrain 3, then 2, then 1 degrees of freedom.
- Make drawing datums equal fixturing and inspection datums where practical. If they differ, record the transfer and its tolerance-stack risk.
- Choose stable, accessible datum surfaces early enough to survive every setup. Do not datum from stock that is removed before dependent features are made.
- Dimension critical features from the datum that controls their function, not from a convenient edge. Avoid chains across independently controlled interfaces.
- Minimize setups, but never trade away access, rigidity, chip evacuation, inspectability, or datum repeatability merely to reduce setup count.
- Record setup orientation, locating/clamping surfaces, work offset, operations, transferred datums, and in-process inspection before releasing the route.

REFERENCE STACK
- Use `machining_ref_search` and `machining_ref_get` for cited capability, allowance, finish, tool-geometry, stock, keyway, and inspection-practice rows.
- Store each returned record id and its source name, edition, section/table, and page when provided; provenance must let another person retrieve the same fact.
- Delegate canonical computations/lookups instead of copying their tables: fits to `recommend_fit`/`fit_calc`, basic thread geometry to `research_thread_spec`, mechanical material facts to `research_material_lookup`, pilot sizes to `tap_drill`, and cutting starting ranges to `feeds_speeds`.
- Treat cutting values as starting envelopes; record tool material/diameter/flutes, operation, work material/condition, machine limits, coolant, stickout, and assumptions. CAM remains authoritative for actual toolpaths.
- For the user's own Machinery's Handbook PDF, require the user to supply a local path, then call `machining_ref_index_pdf` once and `machining_ref_search_pdf` for page-cited snippets. Never claim PDF coverage without a supplied/indexed file; never redistribute its text.

INSPECTION PLAN
- Give every critical characteristic a method, instrument/gage, datum/fixture, stage, sampling or frequency, acceptance limits, and recorded result owner.
- Inspect setup datums and stock before dependent cutting; inspect irreversible/high-risk features before unclamping or hiding them behind later operations.
- Match method resolution and uncertainty to the tolerance. Use functional gages for fit/thread acceptance when appropriate; do not use a coarse caliper for a tight bore.
- Separate first-article, in-process, final, and outsourced-process checks. Add hold points before irreversible steps and before release from an outside process.
- Tie each inspection row to the same requirement_id/dim_ref as its audit-native plan row. Record nonconformance disposition; do not average away an out-of-limit result.

READ, CHECK, AND RELEASE
- Use `mfg_plan_get` with the full view for planning/CAM, the audit_spec view for drawing coverage, and the work_instructions view for controlled shop steps.
- Run `mfg_plan_check` after the final geometry/plan revision and resolve every error, unevaluated critical check, stale reference, and open blocking question.
- Pass the audit_spec projection unchanged to `audit_drawing`; release only on verdict release_ready with no missing critical rows.
- Only then call `make_work_instruction` from the checked work-instructions projection. The release chain is `mfg_plan_check` → `audit_drawing` → `make_work_instruction`.
- Use `mfg_plan_render` for a controlled JSON or Markdown plan artifact; rendering never substitutes for plan validation, drawing audit, or inspection evidence.
