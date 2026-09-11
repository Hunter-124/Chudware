---
name: gui-evaluate
description: Chudware Evaluate toolset for measurement, interference/fitment, and analysis — the MCP measure tool (color-coded multi-element measurement + annotated render), interference detection (overlap solids + measurements), mass props, and how agents drive them to analyze/fix parts. Use for any task about measuring faces/edges, clash/clearance checks, Evaluate ribbon analysis, or fitment diagnosis.
---

# Evaluate / Measure Specialist

Scope: analysis tools that *read* geometry without mutating the feature tree —
dimension probes, mass properties, interference/clearance, and the colour-coded
measurement view. Modeling ops stay in the engine/agent path; ribbon chrome for
the Evaluate tab is `gui-ribbon-commands`; viewport overlay prims are
`gui-viewport`.

## File map

| File | Role |
|---|---|
| `services/mcp/cad_eval_tools.py`, `cad_common.py` | MCP measurement tools, body-owned reference envelopes and annotation readbacks |
| `core/doc/session.cpp` | `doc_eval_measure`, full-document section properties, complete interference evidence |
| `host/src/ui/evaluate_dialogs.cpp/.h` | Live Measure, mass/section sessions, interference inventory, result-generation guards |
| `host/src/render/overlays.*` | Overlay prims: face/body wash, segments, triangles used by GUI measure/interference |
| `host/src/app.cpp`, `host/src/core/selection.h`, `doc_mirror.cpp` | Async pick resolution with body ownership, request tokens and explicit failures |
| `host/src/ui/layout.cpp/.h` | Resizable floating analysis windows, persisted geometry and viewport recovery |
| `host/src/ui_theme.cpp/.h` | Shared `evalEmphasisPalette` used by reference cards and overlays |

## Agent tools (MCP)

### `measure(targets, doc_id?, view="auto", annotate=True) → [image, caption]`

Colour-coded multi-element measurement matching the Evaluate ribbon:

1. Resolve each target with the same plain-English path as `inspect_part` /
   `measure_between` (`"top"`, `{"of":"edge","surf":"circle","pick":"largest"}`,
   resolved sid dicts).
2. Call engine `eval_measure` with **all refs at once** (single / pair /
   multi-distance).
   Picked refs carry `{body_id, selector}`; do not discard the owner or world-place
   the local selector twice. Unscoped queries search all bodies and refuse ambiguity.
3. Assign each element a distinct colour from the shared 10-colour
   `_EMPHASIS_PALETTE` (`per_face` emphasis passed to `cad_common._annotate_render`).
4. Return PNG + caption listing colour + reading; caption embeds
   `measure_json` with structured `{kind,value,unit,color}` per element and an
   optional `between` distance/angle block for 2+ refs.

Prefer `measure` over `measure_between` when the agent (or human) needs the
highlighted view; keep `measure_between` for cheap text-only probes.

### Interference / fitment

```
check_interference(doc_id?, include_coincident=False, min_clearance_mm=0.0)
```

- Pairwise clash/clearance over bodies (assemblies: world-placed components).
- Result: `parts[]`, `interferences[{a,b,volume,overlap_volume_mm3,overlap_bbox,overlap_mesh}]`,
  `clearances[]`, `contacts[]`, `complete`, `pairs_checked`, `geometry_failures`,
  `pair_failures`. `ok` requires completed checks with no interference; zero checked
  pairs are not clearance evidence.
- Engine also accepts optional `members: [partName, ...]` to pairwise-check a
  selected subset (absent/empty = all). Use when the user selected components
  in the interference browser.
- Intra-part feature collisions (boss swallowed by later cut) →
  `check_feature_overlap`, not body-level interference.

### Related

| Tool | Use |
|---|---|
| `measure_part` | Mass / volume / CoM / inertia |
| `measure_between` | Text-only single or pair probe |
| `inspect_feature` | Emphasis close-up + describe excerpt + dims |
| `verify_battery` | Full quality gate before declaring done |

Guide: `cad_guide("evaluate")` / `cad_guide("measure")`.

## Workflow — measure

1. `query_geometry(...)` if the selector is ambiguous.
2. `measure(targets=["top face", {"of":"edge","surf":"circle","pick":"largest"}])`.
3. Read caption / `measure_json` for values; vision models also see the colour map.
4. Fix geometry if a critical dim is off; re-measure; then `verify_battery`.

## Workflow — fitment

1. Assembly (or multi-body part) open → `check_interference(doc_id)`.
2. If interferences: read `overlap_volume_mm3` + `overlap_bbox` (world mm);
   optional `members=[...]` to re-check a subset after a partial fix.
3. Edit mates / offsets / geometry → re-check until complete with checked pairs and `interferences=[]`.
4. Tight gaps: use `min_clearance_mm` or inspect `clearances[]`.
5. Same-part feature bugs → `check_feature_overlap`.

## GUI lifecycle

- Call `prepareFrame(st)` before consuming Measure overlays. Selection/revision
  changes invalidate old readings and pending pick errors remain visible.
- Keep the full Interference member inventory separate from filtered results;
  unchecked members must remain selectable. Never paint zero-pair/incomplete
  checks as a green clearance result.
- Floating tools use `setNextCommandDialogPlacement` with metric-scaled pixel
  sizes, normal resize grips and wrapped text. Do not reintroduce auto-resize or
  fixed one-line instructions.

## Conventions

- Address geometry in plain words — never raw face indices.
- Keep existing JSON keys additive; do not rename `volume` / `distance`.
- Emphasis colours come only from `_EMPHASIS_PALETTE` / `_build_emphasis_palette`
  (same palette as `render_part` emphasis / GUI measure chips).
- Do not run project-wide pytest or the MCP server from worker slices; the
  orchestrator gates once at the end.

## Build & smoke

```bash
# syntax-only (workers): python -c "from services.mcp.cad_tools import cad_guide_text; print(cad_guide_text('measure')[:200])"
./build.sh          # if C++ engine side also changed
scripts/gate.sh     # full gate when engine/agent/mcp touched
```

Return files changed + one-line summary. User verifies in-app: open Evaluate →
Measure live mode / Interference browser matches the MCP colour-coded view.
