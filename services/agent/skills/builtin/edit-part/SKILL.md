---
name: edit-part
description: Build or edit a single solid part with human-editable features, targeted verification, and a history review before completion.
inject: auto
triggers:
  keywords: [part, solid, extrude, revolve, fillet, boss, pocket, shell, edit part, modify part, existing part, model a, model an, model me a, design a, design an, design me a, make a, make an, make me a, build a, build an, build me a, create a, create an, create me a, 3d print a, machine a]
  tools: [new_part, list_parts, import_part, get_part, add_features, save_part, render_part, describe_part]
  phases: [feature, part_verify]
---

# Edit Part

How to model one solid part in Chudware. Prefer standard editable features over freeform code.

## Sequence

1. **Orient** — load `cad_guide("edit_part")` for existing parts and a narrow feature topic as needed. Use `list_parts()` for open documents and find the requested file in the selected workspace. Prefer its name in narration; use its opaque `doc_id` in tool calls.
2. **Open or create** — for an existing native file use `import_part(path)` to reuse its live document or bind the saved file, preserving feature IDs and history. Relative paths stay in the selected workspace. Use `copy=True` only for an independent document; use `new_part(name)` for a fresh part.
3. **Inspect before editing** — `get_part(doc_id=..., summary=True)` orients; features are in `result["tree"]["features"]`, not at the top level. Use `get_part(doc_id=..., feature_ids=[...])` for complete selected features instead of dumping the whole tree. Recover any spilled full result before using truncated parameters.
4. **Edit in place** — repair the existing sketch or feature rather than appending compensating patches. Use `add_features` with `{"op":"edit_feature","id":"fN","params":{...}}` and/or `refs` containing only intended changes, rather than recreating features. Preserve untouched dimensions and relations; when replacing a list, include the existing entries to keep. Requery topology after geometry changes before selecting faces or edges.
5. **Interface first** — read mating dimensions from the existing mating part via `measure_part` / `query_geometry`; never parallel-guess both sides. Add human-editable features when needed.
6. **Verify changes** — inspect the completed reply and describe or measure affected geometry as needed; use `check_feature_overlap` when boolean-heavy work warrants it.
7. **Standard parts** — fasteners, bearings, inserts: `part_search(query)` then `part_place(part_id)`. Never approximate a catalog part.
8. **Done gate** — inspect `check_model_history(doc_id)` and address its findings or explain why they remain, including its limitations. `verify_battery` must show no FAILs before reporting done. Agent edits persist automatically unless human edits are pending; never ask the user to save AI work. Present a finished artifact only when ready, or leave a user-opened document live. See `cad_guide('verify')`.

## Rules

- **Editable intent.** Name each feature for one functional intent, not a minimum
  feature count. Use semantic hole csink/cbore rather than cylinder-plus-cone
  chains. Share repeated sizes via globals/expressions; use patterns only for
  genuinely repeated placements, not irregular hole locations.
- **Shared references.** For cuts sharing an angle, prefer a shared angled datum
  and direct cuts over duplicate transformed tool solids. Use geometric relations
  and explicit critical dimensions, not a dimension for every coordinate or
  precision-derived coordinate literals. Group finishing after structural
  changes in the region unless a real dependency requires it earlier.
- **Conservative cleanup.** History findings are tree-level advice, not geometry
  certification. Equal volume or no surviving owned faces does not prove a
  feature unused. Preview `simplify_part(doc_id, apply=False)` before cleanup;
  apply only eligible, understood changes that preserve editable history. Never
  automatically rewrite topology or use refine without an explicit need.

- **Verify-before-done.** Inspect mutation results and check affected geometry as needed; verify the requested outcome before completion.
- **Single source of truth for mates.** The part that already exists owns the interface numbers.
- **Process-aware fits.** Use `fit_calc` before sizing a bore/shaft; do not copy nominal diameters.
- **History recovery.** A valid edit that breaks existing downstream features
  returns `ok:true` with `auto_rollback.after`, `broken`, and `hidden_features`.
  The suffix is retained but unbuilt. Repair the listed features and use
  `{"op":"set_rollback","after":null}`; repeat if further failures are reported.
  A feature authored by the batch that fails or was not compiled in the
  attempted rebuild is rejected atomically. Partial repairs to an already-broken
  tree are kept if they fix a failure without creating another; an earlier
  unresolved failure can leave the repaired feature hidden behind the bar.
  Never present a rolled-back model as complete. If an incomplete handoff is
  intentional, state the rollback and every hidden feature ID in the summary.
- **Rejected rebuild** → inspect the failure and `regen_diagnosis` if supplied;
  undo when the diagnosis says the approach is wrong.

## Deeper docs

- Op vocabulary: call `cad_guide(topic)` for the particular operation; `cad_guide('feature_history')` for insertion, reorder and rollback; `cad_guide('review')` for review.
- Verify battery: `cad_guide('verify')`
- Parts library phrasing: `cad_skill("source-parts")`


## Eval calls

For the checked helper and recovery instructions, call `cad_guide("eval-helper")`.
