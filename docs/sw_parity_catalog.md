# SolidWorks 2025 reverse-engineering catalog (C25 recon)

Captured live from SolidWorks 2025 (Student Edition, Simplified Interface) on 2026-06-11.
Feeds the C25+ UI waves. Source of truth for visual/UX parity decisions.

## Viewport visuals
- Background: near-white vertical gradient, soft cool mid-band, lighter floor glow at bottom
  (~#F4F6F9 top / ~#D9DDE4 mid / ~#EEF0F3 bottom — already in `ui::palette` C25).
- Default part appearance: grey-blue ~#C6CAD8 lit / ~#7E828E shadowed; soft top-dominant light,
  gentle specular. Thin near-black edges on all feature/silhouette edges.
- Selection highlight: bright cyan-blue (whole feature's faces/edges tint, e.g. on tree click).
- Live feature preview: translucent YELLOW body + dashed yellow edge highlight (fillet),
  with in-viewport parameter callouts ("Radius: 0.2in") attached by leader line to the geometry.
- Reference planes: translucent blue quads with script-style corner labels; hidden by default
  in normal work (shown during sketch plane pick / when selected).
- Save reminder toast bottom-right (info icon, message, [Save] button).

## Space / view interaction
- Space → View Selector "glass box": translucent corner-truncated cube around model, dark thin
  outlines; hover face = blue outline + preview thumbnail panel top-right with caption
  "Click on a plane to switch views"; click = ~0.4s animated camera transition; Esc aborts
  (status bar: "Hit Esc key to abort animation").
- Heads-up toolbar (top-center): zoom-fit, select, pan, rotate, zoom, zoom-area,
  view-orientation ▾, display-style ▾, hide/show-items ▾, view-settings ▾.
- Display style flyout (5, vertical icon list): Shaded With Edges, Shaded, Hidden Lines Removed,
  Hidden Lines Visible, Wireframe. Tooltip/status: "Changes the display style for the active view."
- Hotkeys: Ctrl+1..8 view presets, Space selector, F zoom-fit (our mapping).

## Sketch tool flyouts (user screenshots, all icon+label rows)
- Rectangle: Corner Rectangle, Center Rectangle, 3 Point Corner Rectangle, 3 Point Center
  Rectangle, Parallelogram
- Circle: Circle, Perimeter Circle
- Slot: Straight Slot, Centerpoint Straight Slot, 3 Point Arc Slot, Centerpoint Arc Slot
- Spline: Spline, Style Spline, Spline on Surface, Equation Driven Curve
- Ellipse group: Ellipse, Partial Ellipse, Parabola, Conic
- Parent button fires last-used variant; arrow opens flyout.

## Features ribbon (Features tab, Simplified Interface)
Groups observed: [Extruded Boss/Base | Revolved Boss/Base | Swept/Lofted/Boundary Boss] ·
[Extruded Cut | Hole Wizard | Revolved Cut | Swept/Lofted/Boundary Cut] ·
[Fillet | Linear Pattern | Rib | Draft | Shell | Wrap | Intersect | Mirror] ·
[Reference Geometry ▾ | Curves ▾ | Instant3D].

## PropertyManager patterns (the "definition types" to replicate)
Common chrome: header (feature icon + name + ?), commit row (green ✓, red ✗, eye=preview),
collapsible groups with chevrons, selector wells (blue when active), spinner number fields with
unit suffixes ("2.50in"), reverse-direction arrow buttons next to direction dropdowns.

### Extrude (Boss-Extrude PM)
- Group "From": Sketch Plane (default) | Surface/Face/Plane | Vertex | Offset.
- Group "Direction 1": end-condition dropdown = Blind, Up To Vertex, Up To Surface,
  Offset From Surface, Up To Body, Mid Plane (+ Through All on cuts);
  reverse button, direction-ref well, depth field, draft field + "Draft outward" checkbox.
- Group "Direction 2" (collapsed when unused), "Selected Contours" well.
- Status bar shows sketch DOF state ("Fully Defined").

### Fillet (Fillet PM)
- "Feature Type" icon tabs: Constant Size (+ Variable Size, Face Fillet, Full Round in full UI).
- "Items To Fillet": multi-edge well (Edge<1>, Edge<2>...), checkboxes Show selection toolbar,
  Tangent propagation; radios Full / Partial / No preview.
- "Fillet Parameters": Symmetric|Asymmetric dropdown, radius spinner, Multi Radius Fillet
  checkbox, Profile dropdown: Circular | Conic Rho | Conic Radius | Curvature Continuous.
- Collapsed groups: Setback Parameters, Partial Edge Parameters, Fillet Options.

### Section View tool (heads-up)
- Section Method: Planar | Zonal. Options: offset perpendicular to Reference/Selected plane,
  Show section cap (ON default), Keep cap color, Graphics-only section.
- Section 1/2/3 (up to three planes), each: plane preset buttons (Front/Top/Right/picked),
  reference well, offset distance, 2 rotation angles, Edit Color.
- "Section by Body" with exclude list.
- Viewport manipulator: translucent blue plane + ORANGE drag arrow (offset) and orange
  rotation ring.

## Context menus
- Tree feature right-click: icon quick-row (Edit Feature, Edit Sketch, suppress, rollback bar,
  hide, zoom-to, appearance ▾) + menu: Comment, Parent/Child…, Configure Feature, Delete…,
  Add to Favorites, Save Selection, Add to New Folder, Feature Properties…, Change
  Transparency, Go To…, Create New Folder, Rename tree item, Hide/Show Tree Items…,
  Collapse Items. Fillet adds "Convert Fillet to Chamfer".
- Viewport right-click (feature selected): Selection Tools ▸, Zoom/Pan/Rotate ▸,
  Recent Commands ▸, then the feature section (Edit Feature, Suppress, …).
- Menus have a "»" expander at bottom (collapsed rarely-used items).

## Not yet captured (future recon)
- Variable-size fillet PM, chamfer PM, shell/draft PMs, Hole Wizard tabs, pattern PMs,
  appearance editor, scene/lighting editor, RealView toggles, mate PMs (assembly),
  drawing mode anything, Instant3D drag handles.

## Status (C25–C30)

| Catalog item | Status | Notes |
|---|---|---|
| **Viewport visuals** | SHIPPED C25 | Background gradient, part appearance, selection highlight, preview, reference planes |
| **Space / view interaction** | SHIPPED C25 | View Selector glass box with animated camera; heads-up toolbar; display style flyout (5 styles); hotkeys Ctrl+1..8 |
| **Sketch tool flyouts** | SHIPPED C25 | Rectangle, Circle, Slot, Spline variants; Ellipse group base (full set un-greyed C28) |
| **Features ribbon** | SHIPPED C25 | Button groups and organization; PropertyManager chrome (header, commit row, groups, selector wells) |
| **Extrude end conditions** | SHIPPED C26 | Blind, Mid Plane, Through All, Up To Next; Direction 2; draft taper; reverse buttons in PM forms |
| **Extrude draft + direction2** | SHIPPED C26 | Draft outward checkbox, direction field per Direction 1/2; engine test test_extrude_ends.py |
| **Face-normal Normal-To (Ctrl+8)** | SHIPPED C26 | True face-normal camera lock; clickable orientation triad in viewport |
| **Per-body appearance colors** | SHIPPED C26 | set_appearance op; AICM color encoding; tree icon + viewport tint; PM appearance dropdown; context menu color picker; engine test test_appearance.py |
| **In-viewport dimension callouts** | SHIPPED C27 | Dimension values with leader lines to geometry during PM edit; theme-aware rendering |
| **View-selector hover preview** | SHIPPED C27 | Hover face on View Selector glass box → thumbnail preview panel top-right |
| **Asymmetric fillet** | SHIPPED C27 | Symmetric|Asymmetric dropdown in Fillet PM; engine support for per-side radius |
| **Convert Fillet↔Chamfer** | SHIPPED C27 | Context menu option to toggle feature type; tree/icon update |
| **Conic sketch entities** | SHIPPED C28 | Ellipse Arc, Parabola, Conic in Ellipse flyout (entity creation + constraint/solve); test_sketch_conics.py |
| **Sketch input variants** | SHIPPED C28 | 3-point corner/center rectangle, parallelogram, perimeter circle, centerpoint slot, arc slots enabled |
| **Zonal/multi-plane section** | SHIPPED C28 | Section Method dropdown (Planar | Zonal); up to 3 independent planes; per-plane offsets/rotations/caps; AI_CAD_SECTION=2/3 debug knobs (AI_CAD_SECTION_PANEL=1 forces panel open) |
| **Trim / Extend / Offset** | SHIPPED C29 | Sketch-tab Trim, Extend, Offset entity tools |
| **3-plane zonal section** | SHIPPED C29 | Corner-octant cut (stencil-masked caps per plane, cross-plane clipping); AI_CAD_SECTION=3 |
| **Variable-size fillet** | SHIPPED C30 | Fillet PM "Fillet Type" combo Constant Size | Variable Size; End Radius field gated on Variable; engine per-side radius |
| **Arc slot (3-pt + centerpoint)** | SHIPPED C30 | 3 Point Arc Slot, Centerpoint Arc Slot enabled in Slot flyout |
| **RealView reflections** | SHIPPED C30 | RealView sheen/reflections in viewport (theme-aware); hide/show toggle |
| **Fillet type tabs (Face/Full Round)** | OPEN | Constant + Variable shipped; Face Fillet and Full Round remain "(coming soon)" rows in PM |
| **Hole Wizard advanced tabs** | OPEN | Callout, Positions, Thread, Cosmetic (simple/cbore/csink core shipped, advanced deferred) |
| **RealView material library** | OPEN | Material picker / photorealistic finish presets (reflections shipped C30; qualitative CFD model is baseline) |
