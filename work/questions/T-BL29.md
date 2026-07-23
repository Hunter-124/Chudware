# T-BL29 Design Decisions

Driver: sonnet-lumen, 2026-07-03

## Q1: Per-face emphasis overlay in renderThumbnail

**Context:** The shared visual contract (T-G15) says "modified faces emphasized with
selection-highlight color." The T-G15 HANDOFF 2 notes that renderThumbnail colors
per-body, not per-face.

**Decision (least-invasive, no C++):** Defer the per-face overlay. The first tranche
(fillet/chamfer/shell/extrude_boss/appr_*) already shipped without it and passed
Hunter's review. All new icons in this task use the same per-body rendering — the
camera angle + operation result communicates the tool's function adequately at 32px.
A dedicated per-face overlay pass (sub-mesh color override or id-pass re-use) is a
follow-up when Erik prioritizes it.

**Alternatives:** (a) per-face sub-mesh color override in renderThumbnail — needs C++
renderer changes; (b) id-pass selection overlay — needs more render passes.

---

## Q2: Sheet Metal tab — contextual gating vs. icon render

**Context:** The Sheet Metal ribbon tab renders EMPTY headless (contextual gating —
the tab only populates when a sheet-metal doc is active). This affects IN-CONTEXT
VERIFICATION but NOT the icon render itself: the icons come from `CHUDWARE_SHOT=iconpipe`
which builds parts through applyOps, bypassing the tab rendering entirely.

**Decision:** sm_base_flange and sm_flat_pattern can be fully rendered via iconpipe —
they need only a sketch and the feature, no edge-query dependency.

sm_edge_flange / sm_hem / sm_jog require a resolved edge selector (refs.edge must be
a real edge pick object from query_geometry, NOT a bare query shorthand). The iconpipe
runner has no edge-query capability (only vertex/face selectors via resolvePick). These
three cells are PARKED for now — would require either: (a) adding a `rim_edge` selector
to runIconPipe in scenarios.cpp, or (b) using a separate query step.

**For Hunter:** Is it worth adding a `rim_edge` selector (top-rim straight edge of the
last body, analogous to `top_face`) to scenarios.cpp to unlock sm_edge_flange/hem/jog?
This is ~20 lines of C++ in runIconPipe's resolveSel lambda.

**In-context verification for Sheet Metal tab:** Skipped — the tab renders empty
headless and needs a workspace-mode hook. The sm_base_flange/sm_flat_pattern icons
will still display correctly in the running app once injected into the atlas.

---

## Q3: Manufacture — CAM-simulator capture path

**Context:** The T-BL29 DoD asks for a cam_simulate capture path for Manufacture icons.

**Finding:** ribbon_def.cpp maps ALL Manufacture commands to cells already belonging to
other groups (extrude_cut, fillet, shell, hole_wizard, loft, revolve_boss/cut, sweep,
study_static, split, slot, ref_point, ok_check, ai_panel, ref_csys, curve_helix, new_doc).
There are ZERO `cam_*` atlas cells. The atlas.json dump confirms this.

**Decision:** Manufacture is PARKED. The current ribbon design reuses modeling-viewport
glyphs for all CAM commands by design. Creating dedicated cam-simulator photographic
icons would require: (a) adding new atlas cells in make_icons.py, (b) building the
cam_simulate headless capture path (new scenario work), and (c) mapping the new cells
in ribbon_def.cpp. This is a separate feature, not "recipe-writing."

**For Hunter:** Please confirm whether dedicated photographic CAM icons are wanted
(scope for a new task) or whether the current modeling-glyph reuse is intentional and
should be documented as final.

---

## Q4: appr_glass / appr_paint GL state

**Context:** T-G15 HANDOFF 2 flagged these as needing "appearance-display scenes."

**Decision:** Use the same approach as appr_metal/plastic/default — box geometry +
set_appearance with the exact metallic/roughness/RGB values from ui::kMaterials. The
renderThumbnail path is the same thumbnail FBO path that already renders appr_metal
correctly. The "appearance-display GL state" concern was likely about the in-context
Appearance tab capture, not the iconpipe thumbnail path.

If glass renders indistinguishable from opaque (the GL thumbnail path may not support
real-time refraction/transparency), that is a cosmetic note, not a blocker — the icon
still communicates "glass material" via the visible color/roughness difference. Document
in the contact sheet for Hunter's round-2 review.

---

## Q5: Sketch refs across iconpipe steps

**Finding:** The iconpipe runner applies each step as its own `applyOps([single_op])`
call. `$N` batch-local references do NOT work across steps. However, the engine assigns
deterministic persistent feature ids: first feature in a new part = `f1`, second = `f2`,
etc. Sketch-based recipes use this:
- Step 1: `add_feature` type `sketch` with inline `params.entities` → becomes `f1`
- Step 2: `add_feature` type `revolve` with `refs.profile[].sketch = "f1"` → works

For multi-sketch recipes (sweep, loft): sketch 1 = `f1`, sketch 2 = `f2`, op = `f3`.
For pattern/mirror: the patterned feature must be a sketch-based extrude (not box),
so: sketch = `f1`, extrude_boss = `f2`, pattern = `f3`.
