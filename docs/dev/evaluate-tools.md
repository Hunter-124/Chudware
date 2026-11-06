# Evaluate tools: measurement, model history, wall thickness and interference

Evaluate provides geometry measurements, mass properties, interference/clearance checks,
colour-coded views and model-history diagnostics. The same engine evidence backs the agent
(MCP) tools and the GUI Evaluate tab; explicit history cleanup is an undoable edit.

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
| `contracts/model_history.md` | Native history report, cleanup and refinement contract |

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

## Wall-thickness study

Model → Evaluate → Thickness and Simulate → Static → Wall Thickness open the same
`eval_thickness` tool. `dfm_known_processes` supplies the Process list; `dfm_get_profile`
supplies `result.params.min_wall_mm` as an editable minimum. Update sends canonical-mm
`min_wall_mm` and `resolution` along with `doc_id`. The engine measures inscribed-sphere
thickness on a refined surface rather than casting rays only at display-mesh vertices.

The renderer's per-activation `result_ramp::Display` fixes the range to `[T, 3T]` and reverses
the shared ramp: **≤ T is red**, **≥ 3T is blue**, NaN is neutral grey. The legend consumes
the renderer's direction and range; IPS labels convert mm to inches, never relabel raw mm.
Other field activations default to the original low-blue ramp; clearing the tool resets the
policy.

The results card reports minimum measured thickness, thin area, region count and
`measurement_basis.spacing_mm`, with an explicit `budget_limited` warning. Thin-region
rows show wall/taper kind, thickness and area; clicking posts the existing `zoomToBBox`
camera request using the region's world-space bounds. The result is a sampled geometry
screen, not structural or manufacturing certification; grey areas remain unmeasured.

## Model History

Open **Evaluate → Model History** to request the native report for the current part.
Select a finding to select its feature IDs, and read its evidence, suggestion and
limitations. The report is revision-scoped; refresh it after edits. The panel previews
safe fixes without mutation; **Simplify history** applies eligible fixes as one undoable
edit and is unavailable when there are no safe fixes or the report is stale.
**Refine representation** separately appends an optional normal editable feature, not a
conversion to imported geometry. Undo restores the prior history.

Agents use `check_model_history(doc_id=None)` and
`simplify_part(doc_id=None, apply=False, refine=False)` for diagnosis and preview;
`apply=True` explicitly commits eligible changes, while `refine=True` opts into appending
the refine feature. Native rebuild status also includes automatic history findings;
`add_features` can surface concise warnings. See
[the native contract](../../contracts/model_history.md) for the response and operation details.

The report analyzes the stated history rules, not engineering or geometric certification.
Automatic cleanup is deliberately restricted to provably unreferenced datums and eligible
literal zero in-place transforms. It does not delete sketches, patterns, copies, suppressed,
configuration-driven or rolled-back features, cosmetic threads, or features merely
overlapping or swallowed by others. Unknown dependencies, configurations, rollback, and
external references can block cleanup; external consumers of saved/public documents may not
be provably absent. Equal volume and face ownership do not establish equivalence or safe
deletion. Structural edits, dress-up reordering and semantic feature replacements require
human/agent judgment. Refine only unifies same-domain topology and refuses ambiguous
references, finishes or changed geometry; it is not heal, does not flatten history, and
does not promise to optimize every artifact.

History warnings do not repair failed rebuilds. A failed part is incomplete: intermediate
tool bodies must not be presented, measured, exported, or saved as its finished geometry.
Failed opens and background verification must leave the saved archive and its recoverable
solve state untouched. Any retained last-good display is explicitly unverified; repair the
failed feature or undo the edit before saving a new complete solve. Assemblies and drawing
projections must enforce the same source-completeness boundary.

Native fillets distinguish a genuine shallow C0 crease from an already-tangent edge.
A single straight, constant-section crease on a single solid can use an exact local
arc sweep from its original planar/extrusion supports, with tangent, radius, containment
and final-solid checks. Unsupported selections still fail rather than silently dropping
edges. Previously failed tiny variable blends may retry with tighter kernel construction
accuracy; geometric acceptance is never relaxed. A rejected candidate's output tolerance
is not an intrinsic precision bound for the input model. These paths do not invalidate
successful saved solves or replace an editable feature history with imported geometry.

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
scripts/testbox.py --suite geom                         # includes measure dialect and interference tests
CHUDWARE_SHOT=evalmass xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/evalmass.png
CHUDWARE_SHOT=asm      xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/asm.png       # insert, mate, interference
CHUDWARE_SHOT=multidoc xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/multidoc.png  # per-document Measure window
CHUDWARE_ENGINE_DAEMON=0 CHUDWARE_SHOT=thickness xvfb-run -a ./host/build-linux/bin/chudware --ui-screenshot scratch/thickness.png  # 0.6 mm shell, red-thin study and region evidence
```
