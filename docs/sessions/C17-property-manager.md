# C17 — PropertyManager + core feature forms + M1 integration scenario

Model: **opus** · Wave 2 (last) · Depends: C14, C16 · Follow `docs/sessions/README.md`.
BUILD_DIR: `host/build`.

## Mission
Close the M1 loop: SolidWorks-style PropertyManager forms driven by ribbon commands, selector
wells fed by viewport picks, dimension fields with units + expressions, live preview, and the
full M1 integration scenario passing.

## Read first
1. `docs/contracts/feature_params.md` — field order IS the form; PM conventions at the bottom
2. `docs/contracts/doc_ops.md` (add_feature/edit_feature you emit)
3. C14's ribbon callback registration, C16's doc_mirror + selection APIs, C15's PickHit flow
4. `docs/PLAN.md` §5-M1 "Integration"

## Owned paths
`host/src/ui/property_manager.{h,cpp}` · `host/src/ui/pm_forms.cpp` ·
wiring in `host/src/app.cpp` (ribbon dispatch → PM open; PM panel swaps over the tree panel
while active, SolidWorks-style) · `host/CMakeLists.txt` (append)

## Deliverables
1. PM host: title (feature name) + OK (accent ✓) / Cancel (✗) header; collapsible groups;
   occupies the tree panel's dock slot while a command is active (tree returns on close).
2. Field widgets:
   - `DimField`: numeric input accepting `25`, `25mm`, `1.5in`, `3/8in` (parse to canonical mm;
     IPS display when Settings says so) and `=expr` to bind an expression (shows resolved value
     + tooltip; validation error inline red).
   - `SelectorWell`: labeled pick target; when active (clicked), viewport picks route into it
     (filtered by allowed kinds from feature_params); shows chips "Edge×3"; right-click clears;
     red tint when a dangling ref comes back in feature_status.
   - Combos/checkboxes for enums/bools per feature_params.
3. `pm_forms.cpp`: forms for `box`, `cylinder`, `extrude`, `revolve`, `fillet`, `chamfer`,
   `shell` — generated as data-driven field lists (one table per type matching
   feature_params.md order), not bespoke code per form, so later features are table rows.
4. Flow: ribbon command → PM opens with defaults → user picks/types → **live preview**: debounce
   400 ms → `doc_apply_ops` add/edit → viewport updates; Cancel → worker `doc_undo` for each
   preview batch applied (track count); OK → leave as committed + tree refresh. Edit path:
   tree context "Edit" reopens the form populated from the feature's params/refs.
5. **M1 integration scenario** — make it executable headlessly as `AI_CAD_SHOT=m1` (scripted
   ops through the real worker, screenshots at each step into `scratch/m1_shots/`):
   New Part → Box 60×40×10 → Fillet r=4 on two picked vertical edges → Shell t=2 removing top
   face → edit Fillet r=8 (cached rebuild: only fillet+shell recompiled — assert via
   feature_status ms/rebuild counters if exposed) → save part.step+ → reopen → tree intact.
6. Run the full regression gate at the end: all engine/doc suites + the 5 legacy suites +
   capture default/light/ribbon/tree/m1 screenshots both themes where modes support it.

## Verification
Build green; `AI_CAD_SHOT=m1` completes with screenshots; regression gate green; report the
scenario's feature_status timings and every deviation.
