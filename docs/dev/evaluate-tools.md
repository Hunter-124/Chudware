# Evaluate tools: measurement and interference

Analysis tools that read geometry without mutating the feature tree: dimension probes, mass
properties, interference/clearance checks and the colour-coded measurement view. The same
engine evidence backs the agent (MCP) tools and the GUI Evaluate tab.

## File map

| File | Role |
|---|---|
| `services/mcp/cad_eval_tools.py`, `services/mcp/cad_common.py` | MCP measurement tools, reference envelopes, annotation readbacks |
| `services/mcp/cad_guide.py` | `cad_guide("evaluate")` / `cad_guide("measure")` topic text |
| `core/doc/session.cpp` | `doc_eval_measure`, full-document section properties, complete interference evidence |
| `host/src/ui/evaluate_dialogs.cpp/.h` | Live Measure, mass/section sessions, interference inventory |
| `host/src/render/overlays.cpp/.h` | Face/body washes, segments and triangles used by measure and interference |
| `host/src/core/selection.h`, `host/src/core/doc_mirror.cpp` | Async pick resolution with body ownership and explicit failures |
| `host/src/ui_theme.cpp/.h` | Shared `evalEmphasisPalette` for reference cards and overlays |

## `measure`

`measure(targets, doc_id?, view="auto", annotate=True)` returns an image plus caption:

1. Each target resolves through the same plain-English path as `inspect_part`,
   `measure_between` and `query_geometry` (`"top"`,
   `{"of":"edge","surf":"circle","pick":"largest"}`, or resolved selector dicts).
2. Engine `eval_measure` runs with **all refs at once** (single, pair or multi-distance).
   Picked refs carry `{body_id, selector}`; the owner is never discarded and a local selector
   is never world-placed twice. Unscoped queries search all bodies and refuse ambiguity.
3. Each element gets a distinct colour from the shared 10-colour emphasis palette.
4. The caption lists colour and reading per element and embeds `measure_json`
   (`{kind, value, unit, color}` per element, plus a `between` block for two or more refs).

Use `measure` when the highlighted view is needed and `measure_between` for cheap text-only
probes. Related tools: `measure_part` (mass, volume, centre of mass, inertia),
`inspect_feature` (emphasis close-up plus dimensions), `verify_battery` (full quality gate).

## `check_interference`

`check_interference(doc_id?, include_coincident?, min_clearance_mm?, members?, clearance_mode="exact")`

- Pairwise clash/clearance over bodies; assemblies use world-placed components.
- Result keys: `ok`, `complete`, `clearance_mode`, `pairs_checked`, `pairs_total`, `parts`,
  `interferences` (`volume`, `overlap_volume_mm3`, `overlap_bbox`, `overlap_mesh`),
  `clearances`, `contacts`, `geometry_failures`, `pair_failures`.
- `ok` requires a completed check with no interference; clearance violations are warnings.
  Failures mean incomplete, never collision-free. An empty document is refused
  (`insufficient_bodies`); a single body returns `pairs_checked=0`, which is not clearance
  evidence.
- `clearance_mode="threshold"` classifies against the requested clearance without exact gap
  work; its clearances carry `distance_status="lower_bound"`, never a measured gap.
- `members=[...]` restricts checking to pairs within an exact-name selection (unknown names are
  refused) and says nothing about the rest of the document.
- Collisions between features of one part belong to `check_feature_overlap`, not body-level
  interference.

## GUI lifecycle

- `prepareFrame(st)` runs before Measure overlays are consumed; selection or revision changes
  invalidate old readings, while pending pick errors stay visible.
- The Interference member inventory is kept separate from filtered results; unchecked members
  stay selectable. Zero-pair or incomplete checks are never painted as a green result.
- Floating analysis windows use `setNextCommandDialogPlacement` with metric-scaled sizes,
  normal resize grips and wrapped text (see [gui-theme-layout.md](gui-theme-layout.md)).

## Conventions

- Address geometry in plain words, never raw face indices.
- JSON keys are additive; `volume` and `distance` are never renamed.
- Emphasis colours come only from the shared palette (`_EMPHASIS_PALETTE` in Python,
  `evalEmphasisPalette` in the GUI), the same palette `render_part` emphasis uses.

## Verification

```bash
python -m pytest services/mcp/test_eval_tool_dialect.py   # measure dialect; interference exact vs threshold
CHUDWARE_SHOT=evalmass xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/evalmass.png
CHUDWARE_SHOT=asm      xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/asm.png       # insert, mate, interference
CHUDWARE_SHOT=multidoc xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/multidoc.png  # per-document Measure window
```
