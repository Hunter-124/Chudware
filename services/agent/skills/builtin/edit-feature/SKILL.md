---
name: edit-feature
description: Add or edit individual solid features in build order — preflight, mutate, verify, diagnose rebuilds; fillets last in their region.
inject: auto
triggers:
  keywords: [fillet, chamfer, hole, pattern, mirror, shell, extrude, revolve, loft, sweep, feature]
  tools: [add_features, preflight_ops, check_feature_overlap, regen_diagnose, undo]
  phases: [feature]
---

# Edit Feature

Per-feature discipline for solid modeling.

## Per-feature loop

1. **Read intent** — which feature, which dims (from ledger / measure / user). No improvising critical numbers.
2. **Preflight** — `preflight_ops` with the planned op when available (dangling refs, insane fillet vs wall, downstream break risk).
3. **Apply** — `add_features([...])` with plain-word face/edge queries.
4. **Verify** — `describe_part` / `measure_part` on the affected region; `check_feature_overlap` after booleans.
5. **Rebuild failure** — `regen_diagnose` first; `undo` when the approach is wrong. Two identical failures ⇒ change approach or escalate, never a third blind retry.

## Ordering

- Bosses before the bores that pierce them.
- Patterns after the seed feature is verified.
- Fillets/chamfers last within their region; radius < adjacent wall (preflight checks this).

## Rules

- Verify-before-done on every feature, not only at the end.
- Standard hardware via `part_search` / `part_place`, never hand-modeled approximations.
- Mating holes: diameter from `fit_calc` / callout, not the nominal shaft size.

## Deeper docs

- Part workflow: `cad_skill("edit-part")`
- `cad_guide()` op vocabulary
