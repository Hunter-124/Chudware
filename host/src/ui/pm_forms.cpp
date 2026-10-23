// PropertyManager form table (C17).
//
// ONE data-driven table per feature type, each row in contracts/feature_params.md field
// order. Adding a feature later is a table edit here — no bespoke draw code. Consumed by
// property_manager.cpp (which renders the rows) and the ribbon dispatcher (cmd_id -> form).
#include <cstring>
#include <string>
#include <vector>

#include "property_manager.h"

namespace ui {

// Shorthand constructors for the table below.
static PMFieldDef Dim(const char* key, const char* label, const char* group, double def) {
    PMFieldDef f; f.kind = PMFieldKind::Dim; f.key = key; f.label = label;
    f.group = group; f.dimDefault = def; return f;
}
// Metric defaults only — IPS uses clean native imperial seeds (0.25 in, 0.125 in, …)
// applied at form open by property_manager.cpp (applyIpsNativeDefaults). Do NOT convert
// these mm values for display; the dual-default path picks a separate imperial number.

// Dim that is enabled only when a sibling Combo (named enableKey) holds one of enableVals.
// omit=true => the param is dropped from the op entirely while disabled (so the engine's
// table/defaults drive it) rather than emitted-and-ignored.
static PMFieldDef DimIf(const char* key, const char* label, const char* group, double def,
                        const char* enableKey, const char* enableVals, bool omit = false) {
    PMFieldDef f = Dim(key, label, group, def);
    f.enableKey = enableKey; f.enableVals = enableVals; f.omitWhenDisabled = omit; return f;
}
static PMFieldDef Well(const char* key, const char* label, const char* group,
                       PMSelKinds kinds, bool multi) {
    PMFieldDef f; f.kind = PMFieldKind::SelectorWell; f.key = key; f.label = label;
    f.group = group; f.selKinds = kinds; f.selMulti = multi; return f;
}
// SelectorWell enabled only when a sibling Combo (named enableKey) holds one of enableVals.
static PMFieldDef WellIf(const char* key, const char* label, const char* group,
                         PMSelKinds kinds, bool multi,
                         const char* enableKey, const char* enableVals) {
    PMFieldDef f = Well(key, label, group, kinds, multi);
    f.enableKey = enableKey; f.enableVals = enableVals; return f;
}
static PMFieldDef Combo(const char* key, const char* label, const char* group,
                        const char* options) {
    PMFieldDef f; f.kind = PMFieldKind::Combo; f.key = key; f.label = label;
    f.group = group; f.comboOptions = options; return f;
}
// Combo with separate display labels (options = param values, labels = UI strings).
static PMFieldDef ComboL(const char* key, const char* label, const char* group,
                         const char* options, const char* labels) {
    PMFieldDef f = Combo(key, label, group, options);
    f.comboLabels = labels; return f;
}
// ComboL enabled only when a sibling field (named enableKey) holds one of enableVals — works
// against a sibling combo OR a checkbox ("true"/"false"), via fieldEnabled/enableSourceValue.
static PMFieldDef ComboLIf(const char* key, const char* label, const char* group,
                           const char* options, const char* labels,
                           const char* enableKey, const char* enableVals) {
    PMFieldDef f = ComboL(key, label, group, options, labels);
    f.enableKey = enableKey; f.enableVals = enableVals; return f;
}
static PMFieldDef Check(const char* key, const char* label, const char* group, bool def) {
    PMFieldDef f; f.kind = PMFieldKind::Check; f.key = key; f.label = label;
    f.group = group; f.checkDefault = def; return f;
}
static PMFieldDef CheckIf(const char* key, const char* label, const char* group, bool def,
                          const char* enableKey, const char* enableVals) {
    PMFieldDef f = Check(key, label, group, def);
    f.enableKey = enableKey; f.enableVals = enableVals; return f;
}
// Integer param (pattern instance counts). No unit suffix.
static PMFieldDef Int(const char* key, const char* label, const char* group, double def) {
    PMFieldDef f; f.kind = PMFieldKind::Int; f.key = key; f.label = label;
    f.group = group; f.dimDefault = def; f.unitLabel = ""; return f;
}
// Free-form text param (e.g. pattern instances-to-skip "2:1, 3:0"). Emitted only when non-empty.
static PMFieldDef Text(const char* key, const char* label, const char* group,
                       const char* placeholder = nullptr) {
    PMFieldDef f; f.kind = PMFieldKind::Text; f.key = key; f.label = label;
    f.group = group; f.placeholder = placeholder; return f;
}
// Unitless Dim (real-valued, no "[mm]" suffix) — e.g. sheet-metal k-factor in 0..1.
static PMFieldDef DimU(const char* key, const char* label, const char* group, double def) {
    PMFieldDef f = Dim(key, label, group, def); f.unitLabel = ""; return f;
}
// Angle dim (shows "[deg]" instead of "[mm]"; value is plain degrees, like revolve's angle).
static PMFieldDef Angle(const char* key, const char* label, const char* group, double def) {
    PMFieldDef f = Dim(key, label, group, def); f.unitLabel = " [deg]"; return f;
}
// Angle dim enabled only when a sibling field (named enableKey) holds one of enableVals.
// omit=true drops the param entirely while disabled (so it can't drive an engine code path —
// e.g. a stale chamfer angle must NOT force the distance-angle path on a symmetric chamfer).
static PMFieldDef AngleIf(const char* key, const char* label, const char* group, double def,
                          const char* enableKey, const char* enableVals, bool omit = false) {
    PMFieldDef f = Angle(key, label, group, def);
    f.enableKey = enableKey; f.enableVals = enableVals; f.omitWhenDisabled = omit; return f;
}
// Attach a bounded range (canonical mm/deg) to a Dim/Angle/DimU — the PM renders it as a slider
// with the unit in the value text (CTRL+click still types an exact number). step 0 = kind default.
static PMFieldDef Range(PMFieldDef f, double mn, double mx, double step = 0.0) {
    f.dimMin = mn; f.dimMax = mx; f.dimStep = step; return f;
}
// Allow negative values on a drag Dim (offsets that reverse direction). Default drags clamp at 0.
// IMPORTANT: dimMax must stay <= dimMin so the field is UNBOUNDED (DragFloat). Setting only dimMin
// to -1e9 while dimMax stays 0 made dimMax > dimMin → a broken SliderFloat from -1e9..0 that
// AlwaysClamp'd any positive default (e.g. plane Offset Distance 10) down to 0.
static PMFieldDef Signed(PMFieldDef f) {
    f.dimMin = -1.0e9;
    f.dimMax = -1.0e9;  // dimMax <= dimMin => unbounded above (see PMFieldDef)
    return f;
}
// Render a 2-option combo as a segmented pill pair (one click to flip) instead of a dropdown.
static PMFieldDef Seg(PMFieldDef f) { f.segmented = true; return f; }
// A datum-reference combo whose chosen value is emitted as a refs string ("axis.x"/"plane.right").
// For _SEL_OR_DATUM slots (pattern direction, mirror plane, draft neutral plane).
static PMFieldDef DatumRef(const char* key, const char* label, const char* group,
                           const char* options, const char* labels) {
    PMFieldDef f; f.kind = PMFieldKind::DatumRef; f.key = key; f.label = label;
    f.group = group; f.comboOptions = options; f.comboLabels = labels; return f;
}
// A feature-id list (refs _FEAT_LIST), seeded from the FeatureManager selection — the features a
// pattern/mirror replicates. selMulti defaults true => emitted as refs[key] = [ids].
static PMFieldDef FeatWell(const char* key, const char* label, const char* group) {
    PMFieldDef f; f.kind = PMFieldKind::FeatureWell; f.key = key; f.label = label;
    f.group = group; return f;
}
// SINGLE-feature-id FeatureWell: selMulti=false => buildOpFeature emits refs[key] = "<one id>"
// (the bare string, not an array). For refs that take exactly one feature id, e.g.
// weld_member's path sketch. The user fills it by selecting the sketch in the FeatureManager
// tree and clicking "Add selected" (the well replaces any prior pick when single).
static PMFieldDef FeatWell1(const char* key, const char* label, const char* group) {
    PMFieldDef f = FeatWell(key, label, group); f.selMulti = false; return f;
}
static const PMSelKinds K_FACE   {true,  false, false};
static const PMSelKinds K_EDGE   {false, true,  false};
static const PMSelKinds K_VERTEX {false, false, true};
static const PMSelKinds K_FEV    {true,  true,  true };   // face/edge/vertex (datum a / point target)
static const PMSelKinds K_FACEEDGE {true, true,  false};  // face or edge (datum plane hinge / midplane #2)
static const PMSelKinds K_FACEVERT {true, false, true};   // face or vertex (datum axis b)
static const PMSelKinds K_BODY   {false, false, false, true};  // solid body (boolean targets/tools)
// Planar reference: an origin datum plane (clicked in the viewport via the plane picker) OR a
// planar model face (id-buffer pick). face=true routes a planar face through the normal pick path;
// plane=true arms SketchMode::drawPlanePicker so the 3 origin planes become clickable. Used for the
// datum-axis "Two Planes" refs and the datum-plane "Mid Plane" second reference.
static const PMSelKinds K_PLANE  {true, false, false, false, true};
// Planar reference target: a planar model FACE (id-buffer pick) OR a datum PLANE
// (plane picker). Same flags as K_PLANE but named for the extrude "Up To" / "From"
// references so a reference PLANE (origin or created datum) is a valid pick, not just
// a model face. The engine resolves either form (resolve_plane_ref / planar face).
static const PMSelKinds K_FACEPLANE     {true, false, false, false, true};
// Face, vertex, OR datum plane — the extrude "Up To" target (up_to_surface picks a
// plane/planar face; up_to_vertex picks a vertex). plane=true arms the plane picker.
static const PMSelKinds K_FACEVERTPLANE {true, false, true,  false, true};
// Axis reference: a straight model edge (id-buffer pick) OR a created datum AXIS (clicked in the
// viewport via the axis picker, routed as a {kind:datum,feature} ref). edge=true keeps model-edge
// picking; axis=true arms SketchMode::drawAxisPicker. Used for revolve / circular-pattern axes.
static const PMSelKinds K_EDGEAXIS     {false, true, false, false, false, true};  // edge or datum axis
static const PMSelKinds K_FACEEDGEAXIS {true,  true, false, false, false, true};  // face/edge or datum axis
static const PMSelKinds K_FACEEDGEVERTEX {true, true, true};  // face/edge/vertex (thread start reference)

// ----------------------------------------------------------------------------
// The forms. Field order == feature_params.md order == PM tab order.
// ----------------------------------------------------------------------------
static const std::vector<PMFormDef>& forms() {
    static const std::vector<PMFormDef> kForms = {
        // --- box ---  length/width/height/centered ; refs at? (origin default)
        { "box", "Box", "box_feature", {
            Dim   ("length",   "Length",   "Box",  20.0),
            Dim   ("width",    "Width",    "Box",  20.0),
            Dim   ("height",   "Height",   "Box",  20.0),
            Check ("centered", "Centered", "Box",  true),
        }},
        // --- cylinder ---  radius/height/centered
        { "cylinder", "Cylinder", "cylinder_feature", {
            Dim   ("radius",   "Radius",   "Cylinder", 10.0),
            Dim   ("height",   "Height",   "Cylinder", 20.0),
            Check ("centered", "Centered", "Cylinder", true),
        }},
        // --- extrude ---  mode/end/depth/flip/draft/draft_outward/merge ; profile[]
        //   Direction 2: end2 / depth2. Geometry-driven ends (up_to_surface/offset_from_surface/
        //   up_to_body/up_to_vertex) read a face/vertex/body target from refs.up_to(2) — the
        //   "Up To" well (K_FACEVERT) gated on those ends; offset_from_surface adds an Offset dim.
        //   Thin Feature (params.thin/thin_thickness/thin_side) extrudes a wall instead of a solid.
        { "extrude", "Extrude", "extrude_boss", {
            Well      ("profile",  "Profile",         "From",        K_FACE, true),
            ComboL    ("start",    "Start Condition", "From",
                       "sketch_plane|offset|from_reference|offset_from_reference",
                       "Sketch Plane|Offset|From Ref.|Offset Ref."),
            WellIf    ("start_ref","Start Reference", "From",        K_FACEPLANE, false,
                       "start", "from_reference|offset_from_reference"),
            Signed    (DimIf ("start_offset", "Start Offset","From", 0.0,
                       "start", "offset|offset_from_reference", true)),
            Seg       (ComboL("mode",     "Type",            "Direction 1", "boss|cut",
                                                                     "Boss|Cut")),
            ComboL    ("end",      "End Condition",   "Direction 1",
                       "blind|mid_plane|through_all|up_to_next|up_to_surface|offset_from_surface|up_to_body|up_to_vertex|up_to_reference|offset_from_reference",
                       "Blind|Mid Plane|Through All|Up To Next|Up To Plane/Surface|Offset Plane/Surf.|Up To Body|Up To Vertex|Up To Ref.|Offset Ref."),
            DimIf     ("depth",    "Depth",           "Direction 1", 10.0,
                       "end", "blind|mid_plane", true),
            WellIf    ("up_to",    "Up To Reference", "Direction 1", K_FACEVERTPLANE, false,
                       "end", "up_to_surface|offset_from_surface|up_to_body|up_to_vertex"),
            Signed    (DimIf ("offset",   "Offset",          "Direction 1", 0.0,
                       "end", "offset_from_surface", true)),
            WellIf    ("end_ref",  "End Reference",   "Direction 1", K_FACEPLANE, false,
                       "end", "up_to_reference|offset_from_reference"),
            Signed    (DimIf ("end_offset","End Offset",      "Direction 1", 0.0,
                       "end", "offset_from_reference", true)),
            Check     ("flip",     "Reverse direction","Direction 1", false),
            // Issue #32: "Draft outward" is the enable toggle. The angle field (and its in-viewport
            // handle/callout, which follow fieldVisible) stay hidden until it's checked, so a plain
            // extrude isn't cluttered by a draft control the user didn't ask for. The checkbox is
            // only offered for blind/mid-plane ends where draft is meaningful. (omitWhenDisabled on
            // the angle => it's hidden, not greyed.) Inward draft remains available via the
            // standalone Draft feature.
            CheckIf   ("draft_outward", "Draft outward","Direction 1", false,
                       "end", "blind|mid_plane"),
            Range     (AngleIf("draft",    "Draft angle",     "Direction 1", 0.0,
                       "draft_outward", "true", true), 0.0, 89.0),
            ComboL    ("end2",     "End Condition",   "Direction 2",
                       "none|blind|through_all|up_to_next|up_to_surface|offset_from_surface|up_to_body|up_to_vertex|up_to_reference|offset_from_reference",
                       "None|Blind|Through All|Up To Next|Up To Plane/Surface|Offset Plane/Surf.|Up To Body|Up To Vertex|Up To Ref.|Offset Ref."),
            WellIf    ("up_to2",   "Up To Reference", "Direction 2", K_FACEVERTPLANE, false,
                       "end2", "up_to_surface|offset_from_surface|up_to_body|up_to_vertex"),
            Signed    (DimIf ("offset2",  "Offset",          "Direction 2", 0.0,
                       "end2", "offset_from_surface", true)),
            WellIf    ("end2_ref", "End Reference",   "Direction 2", K_FACEPLANE, false,
                       "end2", "up_to_reference|offset_from_reference"),
            Signed    (DimIf ("end2_offset","End Offset",     "Direction 2", 0.0,
                       "end2", "offset_from_reference", true)),
            DimIf     ("depth2",   "Depth",           "Direction 2", 10.0,
                       "end2", "blind", true),
            Check     ("thin",         "Thin Feature",  "Thin Feature", false),
            DimIf     ("thin_thickness","Wall thickness","Thin Feature", 2.0,
                       "thin", "true"),
            ComboLIf  ("thin_side", "Wall side",       "Thin Feature",
                       "one|two|mid", "One Direction|Two Directions|Mid-Plane",
                       "thin", "true"),
            Check     ("merge",    "Merge result",    "Options",      true),
        }},
        // --- revolve ---  mode/end/angle/flip/dir2/angle2/thin ; profile[]/axis
        //   Engine (revolve.py): end one_direction|mid_plane|up_to_surface (up_to auto-trims to the
        //   body, no target ref); Dir2 (params.dir2 + angle2 — angle2 omitWhenDisabled so it can't
        //   force dir2 on); Thin Feature (thin/thin_thickness/thin_side inside|outside|both).
        { "revolve", "Revolve", "revolve_boss", {
            Well   ("profile",  "Profile",        "From",        K_FACE, true),
            Well   ("axis",     "Axis",           "From",        K_EDGEAXIS, false),
            Seg    (ComboL("mode",     "Type",           "Direction 1", "boss|cut", "Boss|Cut")),
            ComboL ("end",      "End Condition",  "Direction 1",
                    "one_direction|mid_plane|up_to_surface",
                    "One Direction|Mid Plane|Up To Surface"),
            Range  (AngleIf("angle",    "Angle",          "Direction 1", 360.0,
                    "end", "one_direction|mid_plane"), 0.0, 360.0),
            Check  ("flip",     "Reverse direction", "Direction 1", false),
            Check  ("dir2",     "Direction 2",    "Direction 2", false),
            Range  (AngleIf("angle2",   "Angle",          "Direction 2", 360.0,
                    "dir2", "true", /*omit=*/true), 0.0, 360.0),
            Check  ("thin",         "Thin Feature",  "Thin Feature", false),
            DimIf  ("thin_thickness","Wall thickness","Thin Feature", 2.0,
                    "thin", "true"),
            ComboLIf("thin_side", "Wall side",      "Thin Feature",
                    "inside|outside|both", "Inside|Outside|Both", "thin", "true"),
            Check  ("merge",    "Merge result",   "Options",     true),
        }},
        // --- sweep ---  mode/merge ; profile[] (region) + path (a SKETCH or a CURVE/helix feature)
        //   Engine: profile resolves like extrude (whole-sketch/index/centroid region or model face);
        //   path = a whole sketch OR a curve feature (helix/spiral) whose wire becomes the spine. The
        //   viewport region picker fills profile; the path well is filled by clicking a sketch/helix in
        //   the FeatureManager tree, or by clicking a drawn helix in the viewport (drawCurvePathPicker).
        { "sweep", "Sweep", "sweep", {
            Well      ("profile", "Profile",      "Profile and Path", K_FACE, true),
            Well      ("path",    "Path",         "Profile and Path", K_EDGE, false),
            Well      ("guides",  "Guide Curves", "Guide Curves",     K_EDGE, true),
            ComboL    ("orientation", "Profile Orientation", "Options",
                       "follow_path|frenet|keep_normal",
                       "Follow Path|Frenet|Keep Normal"),
            Signed    (DimU ("twist_turns",   "Twist (turns)",   "Options", 0.0)),
            Signed    (Angle("twist_degrees", "Twist (degrees)", "Options", 0.0)),
            Seg       (ComboL("mode",    "Type",         "Options", "boss|cut", "Boss|Cut")),
            Check     ("merge",   "Merge result", "Options", true),
        }},
        // --- loft ---  mode/ruled/merge ; sections[] (>=2 ordered region selectors)
        //   Engine (loft.py): ThruSections through each section's outer wire, in list order.
        { "loft", "Loft", "loft", {
            Well      ("sections",  "Profiles",     "Profiles", K_FACE, true),
            Well      ("centerline","Centerline",   "Centerline", K_EDGE, false),
            Well      ("guides",    "Guide Curves", "Guide Curves", K_EDGE, true),
            ComboL    ("tangency_start", "Start Constraint", "Tangency",
                       "none|normal|tangent|curvature",
                       "None|Normal To Profile|Tangent To Section|Curvature"),
            ComboL    ("tangency_end",   "End Constraint",   "Tangency",
                       "none|normal|tangent|curvature",
                       "None|Normal To Profile|Tangent To Section|Curvature"),
            Seg       (ComboL("mode",    "Type",         "Options", "boss|cut", "Boss|Cut")),
            Check     ("ruled",   "Ruled (straight transitions)", "Options", false),
            Check     ("merge",   "Merge result", "Options", true),
        }},
        // --- fillet --- constant or variable radius on picked edges.
        // The engine selects the variable path when radius2 differs from radius.
        { "fillet", "Fillet", "fillet", {
            WellIf    ("edges",      "Edges",              "Items to Fillet",    K_EDGE, true,
                       "type", "constant|variable"),
            ComboL    ("type",       "Fillet Type",        "Fillet Parameters",
                       "constant|variable", "Constant Size|Variable Size"),
            DimIf     ("radius",     "Radius",             "Fillet Parameters",  2.0,
                       "type", "constant|variable"),
            DimIf     ("radius2",    "End Radius",         "Fillet Parameters",  2.0,
                       "type", "variable"),
        }},
        // --- chamfer --- distance / [second distance | angle] on edges or vertices.
        // The selected type determines whether to bevel the picked edges or edges
        // incident to picked vertices, and which leg definition applies.
        { "chamfer", "Chamfer", "chamfer", {
            WellIf  ("edges",    "Edges",            "Items to Chamfer", K_EDGE, true,
                     "type", "symmetric|asymmetric|distance_angle"),
            WellIf  ("vertices", "Vertices",         "Items to Chamfer", K_VERTEX, true,
                     "type", "vertex"),
            ComboL  ("type",     "Chamfer Type",     "Chamfer Parameters",
                     "symmetric|asymmetric|distance_angle|vertex",
                     "Distance (Symmetric)|Distance (Asymmetric)|Angle-Distance|Vertex"),
            Dim     ("length",   "Distance",         "Chamfer Parameters", 1.0),
            DimIf   ("length2",  "Distance 2",       "Chamfer Parameters", 1.0,
                     "type", "asymmetric", /*omit=*/true),
            Range   (AngleIf("angle",    "Angle",            "Chamfer Parameters", 45.0,
                     "type", "distance_angle", /*omit=*/true), 1.0, 89.0),
            WellIf  ("reference_face","Reference Face","Chamfer Parameters", K_FACE, false,
                     "type", "distance_angle"),
        }},
        // --- shell ---  thickness/outward ; remove[]
        { "shell", "Shell", "shell", {
            Well  ("remove",   "Faces to Remove", "Parameters", K_FACE, true),
            Dim   ("thickness","Thickness", "Parameters", 2.0),
            Check ("outward",  "Shell outward",   "Parameters", false),
        }},
        // --- hole (Hole Wizard) ---  full SolidWorks Hole Wizard over features/hole.py.
        //   style simple|cbore|csink|tap; standard M2..M12 drives diameter + cbore/csink dims from
        //   the ISO table (features/hole.py _STD_TABLE), "custom" reveals an explicit Diameter.
        //   CRITICAL: diameter is OMITTED while a non-custom standard is chosen (omitWhenDisabled),
        //   so the table — not a form default — sets it. cbore/csink dims are left to the engine
        //   table + proportional defaults (exposing them as overrides would shadow the table).
        //   Multi-position (SolidWorks Hole Wizard's Positions tab): a rectangular grid (nx×ny at
        //   dx,dy pitch, centred on the face) and/or picked vertices (refs.positions). No positions
        //   => one hole at the face centre. end through_all/blind; blind uses depth.
        { "hole", "Hole Wizard", "hole_wizard", {
            Well    ("face",     "Face",             "Hole Position", K_FACE, false),
            ComboL  ("style",    "Type",             "Hole Type",
                     "simple|cbore|csink|tap", "Simple|Counterbore|Countersink|Tapped"),
            ComboL  ("standard", "Size",             "Hole Type",
                     "M2|M2.5|M3|M4|M5|M6|M8|M10|M12|"
                     "#4-40|#6-32|#8-32|#10-24|#10-32|1/4-20|1/4-28|5/16-18|3/8-16|1/2-13|"
                     "NPT1/8|NPT1/4|NPT1/2|custom",
                     "M2|M2.5|M3|M4|M5|M6|M8|M10|M12|"
                     "#4-40 UNC|#6-32 UNC|#8-32 UNC|#10-24 UNC|#10-32 UNF|1/4-20 UNC|1/4-28 UNF|"
                     "5/16-18 UNC|3/8-16 UNC|1/2-13 UNC|NPT 1/8|NPT 1/4|NPT 1/2|Custom"),
            DimIf   ("diameter", "Diameter",         "Hole Type", 6.6, "standard", "custom", true),
            Range   (AngleIf("csink_angle", "C'Sink Angle",  "Hole Type", 90.0,
                     "style", "csink"), 60.0, 150.0),
            ComboL  ("end",      "End Condition",    "End Condition",
                     "through_all|blind", "Through All|Blind"),
            DimIf   ("depth",    "Depth",            "End Condition", 10.0, "end", "blind"),
            Well    ("positions","Position Vertices","Positions", K_VERTEX, true),
            Int     ("nx",       "Count X",          "Positions", 1),
            Int     ("ny",       "Count Y",          "Positions", 1),
            Dim     ("dx",       "Pitch X",          "Positions", 20.0),
            Dim     ("dy",       "Pitch Y",          "Positions", 20.0),
        }},
        // --- thread (Thread Wizard) ---  ISO/Unified threads cut/raised on a picked CYLINDRICAL
        //   face (core/geom/thread.cpp, compiled in core/doc/document.cpp). The engine reads params
        //   top-level after document.cpp's params/refs flatten; target_face passes straight through
        //   (like hole.face). `size` drives major+pitch from the standard table — but a size that
        //   is NOT in the table THROWS, so "custom" can't be a dropdown value: a "Custom size" check
        //   HIDES the Size dropdown (→ omitted from the op) and reveals Major Dia. + Pitch instead.
        //   mode has 3 options (Cut/Raised/Cosmetic) so it's a dropdown, not a 2-way segmented pill.
        //   turns is a Text field: blank ⇒ omitted ⇒ engine's default cap (24); a typed number caps
        //   the helix (num_or_expr parses it). Helical booleans can fall back to the plain cylinder
        //   plus a non-fatal FeatureStatus.warning, which the PM surfaces amber automatically.
        { "thread", "Thread", "thread", {
            Well    ("target_face", "Cylindrical Face", "Thread", K_FACE, false),
            Check   ("custom_size", "Custom size",      "Thread", false),
            ComboLIf("size",  "Size",  "Thread",
                     "M3|M4|M5|M6|M8|M10|M12|M16|M20|M24|M8x1|M10x1.25|M12x1.5|"
                     "#10-24|#10-32|1/4-20|5/16-18|3/8-16|1/2-13",
                     "M3|M4|M5|M6|M8|M10|M12|M16|M20|M24|M8x1 (fine)|M10x1.25 (fine)|M12x1.5 (fine)|"
                     "#10-24 UNC|#10-32 UNF|1/4-20 UNC|5/16-18 UNC|3/8-16 UNC|1/2-13 UNC",
                     "custom_size", "false"),
            DimIf   ("major_dia", "Major Dia.", "Thread", 12.0,  "custom_size", "true", /*omit=*/true),
            DimIf   ("pitch",     "Pitch",      "Thread", 1.75,  "custom_size", "true", /*omit=*/true),
            // Full length by default: `full_length` checked OMITS `length` so the engine
            // spans the whole cylindrical face. Uncheck to reveal an explicit Length field.
            Check   ("full_length", "Full length", "Thread", true),
            DimIf   ("length",    "Length",     "Thread", 12.0,  "full_length", "false", /*omit=*/true),
            // Start reference: a vertex / edge / face the thread starts FROM (default: the
            // lower face end). Flip runs it toward the other end — use it when the picked
            // reference isn't already an auto-termination end of the thread.
            Well    ("from",  "Start From (edge/face/vertex)", "Thread", K_FACEEDGEVERTEX, false),
            Check   ("flip",  "Flip direction", "Thread", false),
            ComboL  ("form",  "Profile", "Thread",
                     "v|acme|trap|square", "V (60 deg)|ACME|Trapezoidal|Square"),
            ComboL  ("mode",  "Mode", "Thread",
                     "cut|raised|cosmetic", "Cut|Raised|Cosmetic"),
            ComboL  ("style", "Style", "Advanced",
                     "external|internal", "External (shaft)|Internal (bore)"),
            Int     ("starts",     "Starts",     "Advanced", 1),
            ComboL  ("handedness", "Handedness", "Advanced", "right|left", "Right|Left"),
            Text    ("turns",      "Turns cap",  "Advanced", "(auto)"),
        }},
        // --- mirror ---  plane (datum/face) + features[] (seeded from selection)
        //   Engine (pattern.py compile_mirror): reflects each seed feature's tool body across the
        //   plane with the same boolean; empty features => whole-body mirror + union.
        { "mirror", "Mirror", "mirror", {
            DatumRef ("plane",    "Mirror Face/Plane", "Mirror Plane",
                      "plane.right|plane.front|plane.top",
                      "Right plane|Front plane|Top plane"),
            // Override well (shares the "plane" ref key, last-wins in buildOpFeature): click an
            // origin plane (routed as a datum string) or a planar model face — same dropdown+pick
            // pattern as linear/circular pattern. K_PLANE arms drawPlanePicker; the engine's
            // mirror branch already resolves both (normalize_feature: datum->plane_point/normal,
            // face selector passes through).
            Well     ("plane",    "…or click a face/plane", "Mirror Plane", K_PLANE, false),
            FeatWell ("features", "Features to Mirror", "Features to Mirror"),
            Well     ("bodies",   "Bodies to Mirror",   "Bodies to Mirror", K_BODY, true),
            Check    ("merge",    "Merge result",       "Options", true),
        }},
        // --- linear_pattern ---  dir1(+count/spacing), optional dir2, features[]
        //   Engine (pattern.py compile_linear_pattern): lays down a count1×count2 grid of the seed
        //   features' tool bodies (re-applying each boolean). count2=1 disables direction 2.
        //   W2: an optional edge/planar-face Well shares each direction's ref key with the DatumRef.
        //   Both emit refs[dir]; the Well is LATER in the form so a pick OVERRIDES the global-axis
        //   datum (last-wins in buildOpFeature); empty Well => the axis combo's datum is used. The
        //   engine (_resolve_direction) already resolves a model edge / planar face to a direction.
        { "linear_pattern", "Linear Pattern", "linear_pattern", {
            DatumRef ("dir1",     "Direction 1",  "Direction 1",
                      "axis.x|axis.y|axis.z", "X axis|Y axis|Z axis"),
            Well     ("dir1",     "…or pick an edge", "Direction 1", K_FACEEDGE, false),
            Int      ("count1",   "Instances",    "Direction 1", 3),
            Dim      ("spacing1", "Spacing",      "Direction 1", 20.0),
            DatumRef ("dir2",     "Direction 2",  "Direction 2",
                      "axis.y|axis.x|axis.z", "Y axis|X axis|Z axis"),
            Well     ("dir2",     "…or pick an edge", "Direction 2", K_FACEEDGE, false),
            Int      ("count2",   "Instances",    "Direction 2", 1),
            Dim      ("spacing2", "Spacing",      "Direction 2", 20.0),
            FeatWell ("features", "Features to Pattern", "Features to Pattern"),
            Text     ("skip",     "Instances to skip", "Instances to Skip",
                      "grid cells i:j or indices, e.g. 2:1, 3:0"),
        }},
        // --- circular_pattern ---  axis + count/angle + equal_spacing, features[]
        //   Engine (pattern.py compile_circular_pattern): count instances about the axis; equal
        //   spacing fills the angle (step = angle/count), else angle is the per-step increment.
        { "circular_pattern", "Circular Pattern", "circular_pattern", {
            DatumRef ("axis",     "Pattern Axis", "Parameters",
                      "axis.z|axis.x|axis.y", "Z axis|X axis|Y axis"),
            Well     ("axis",     "…or pick edge/cyl face/datum axis", "Parameters", K_FACEEDGEAXIS, false),
            Int      ("count",    "Instances",    "Parameters", 4),
            Range    (Angle("angle",    "Angle",        "Parameters", 360.0), 0.0, 360.0),
            Check    ("equal_spacing", "Equal spacing", "Parameters", true),
            FeatWell ("features", "Features to Pattern", "Features to Pattern"),
            Text     ("skip",     "Instances to skip", "Instances to Skip",
                      "ring indices (0=seed), e.g. 1, 3, 5"),
        }},
        // --- draft --- neutral-plane taper on the selected faces.
        { "draft", "Draft", "draft", {
            ComboL   ("mode",    "Draft Type",    "Draft",
                      "neutral_plane", "Neutral Plane"),
            Range    (Angle("angle",   "Draft Angle",   "Draft", 3.0), 0.0, 89.0),
            DatumRef ("neutral", "Neutral Plane", "Draft",
                      "plane.top|plane.front|plane.right",
                      "Top plane|Front plane|Right plane"),
            // Override well (shares "neutral", last-wins): click an origin plane or a planar face
            // for the neutral plane — the common SolidWorks draft workflow. Engine accepts both
            // (normalize_feature neutral slot: datum->plane_point/normal, face passes through).
            Well     ("neutral", "…or click a face/plane", "Draft", K_PLANE, false),
            Well     ("faces",   "Faces to Draft", "Draft", K_FACE, true),
        }},
        // --- helix (Curve) --- engine features/curves.py. Produces a sweep PATH (a Sweep can then
        //   reference this helix feature). v1 is "Height & Pitch"-defined; the engine derives turns
        //   = height/pitch. radius defaults the coil radius when base is a datum plane.
        { "helix", "Helix / Spiral", "curve_helix", {
            DatumRef ("base",   "Axis Plane",       "Helix",
                      "plane.top|plane.front|plane.right", "Top plane|Front plane|Right plane"),
            // Override well (shares "base", last-wins): click an origin plane or a planar face as
            // the helix axis-plane. Engine helix base now resolves via plane_of (datum/csys OR a
            // planar face), same as the ref-plane offset base.
            Well     ("base",   "…or click a face/plane", "Helix", K_PLANE, false),
            Dim      ("radius", "Radius",           "Helix", 10.0),
            Dim      ("pitch",  "Pitch",            "Helix", 5.0),
            Dim      ("height", "Height",           "Helix", 20.0),
            Range    (Angle("taper",  "Taper Angle",      "Helix", 0.0), -89.0, 89.0),
            Range    (Angle("start_angle", "Start Angle", "Helix", 0.0), 0.0, 360.0),
            Check    ("ccw",    "Counterclockwise", "Helix", true),
        }},
        // --- ref_plane (datum) --- engine datum.py compile_plane. method gates which refs apply:
        //   offset/angle use base (+ second hinge for angle); three_point uses 3 vertices; midplane
        //   uses base + second. base is a K_PLANE well: SolidWorks-style, the user CLICKS an origin
        //   datum plane (routed as "plane.top"/… via the plane picker) or a planar model face in the
        //   viewport — not a dropdown. Arming the well force-shows the three origin planes so the
        //   pick target is visible. base is hidden for three_point (which is defined by 3 vertices).
        { "plane", "Plane", "ref_plane", {
            ComboL   ("method", "Type",            "First Reference",
                      "offset|angle|three_point|midplane",
                      "Offset|At Angle|Three Points|Mid Plane"),
            WellIf   ("base",   "Reference",       "First Reference", K_PLANE, false,
                      "method", "offset|angle|midplane"),
            Signed   (DimIf("offset", "Offset Distance", "Parameters", 10.0, "method", "offset")),
            Range    (AngleIf("angle",  "Angle",           "Parameters", 45.0,
                      "method", "angle"), -90.0, 90.0),
            WellIf   ("second", "Hinge (edge/axis)","Second Reference", K_FACEEDGE, false,
                      "method", "angle"),
            WellIf   ("second", "Second Plane",     "Second Reference", K_PLANE, false,
                      "method", "midplane"),
            WellIf   ("points", "Three Vertices",  "Points", K_VERTEX, true,
                      "method", "three_point"),
            Check    ("flip",   "Flip",            "Parameters", false),
        }},
        // --- ref_axis (datum) --- engine datum.py compile_axis. a = the primary reference (cyl
        //   face / edge / vertex); b = the second vertex (two_points) or plane (plane_intersect).
        // ref_axis wells are method-gated (duplicate keys; buildOpFeature emits only the visible
        //   one): cylinder/edge/two_points resolve a face/edge/vertex selector; plane_intersect
        //   takes two PLANAR references (origin datum planes — clickable via the plane picker — or
        //   planar faces). The engine's compile_axis plane_intersect already accepts a datum-plane
        //   ref string for refs.a / refs.b (datum.py _plane_of).
        { "axis", "Axis", "ref_axis", {
            ComboL ("method", "Type",        "Reference",
                    "cylinder|two_points|edge|plane_intersect",
                    "Cylindrical Face|Two Points|Edge|Two Planes"),
            WellIf ("a",      "Reference 1", "Reference", K_FEV,   false,
                    "method", "cylinder|two_points|edge"),
            WellIf ("a",      "Plane 1",     "Reference", K_PLANE, false,
                    "method", "plane_intersect"),
            WellIf ("b",      "Reference 2", "Reference", K_FACEVERT, false,
                    "method", "two_points"),
            WellIf ("b",      "Plane 2",     "Reference", K_PLANE, false,
                    "method", "plane_intersect"),
        }},
        // --- ref_point (datum) --- engine datum.py compile_point. vertex/arc-center/on-face.
        { "point", "Point", "ref_point", {
            ComboL ("method", "Type",      "Reference",
                    "vertex|center|on_face", "At Vertex|Arc/Circle Center|On Face"),
            Well   ("target", "Reference", "Reference", K_FEV, false),
        }},
        // --- ref_csys (datum) --- engine datum.py compile_csys. origin + optional X/Y direction
        //   refs (orthonormalized engine-side). x_dir/y_dir take a model edge OR a datum AXIS — the
        //   engine's _dir_from (document.cpp) already resolves resolve_axis_ref, so the wells are
        //   K_EDGEAXIS (arm the datum-axis picker too) for parity with revolve / circular-pattern.
        { "csys", "Coordinate System", "ref_csys", {
            Well ("origin", "Origin",            "Coordinate System", K_VERTEX,   false),
            Well ("x_dir",  "X Axis (edge/axis)", "Coordinate System", K_EDGEAXIS, false),
            Well ("y_dir",  "Y Axis (edge/axis)", "Coordinate System", K_EDGEAXIS, false),
        }},
        // --- boolean (Multi-body) --- engine features/boolean.py. op is preset from the ribbon
        //   command (Combine/Subtract/Intersect), like extrude boss/cut. Body wells (K_BODY): a
        //   pick resolves the clicked element's OWNING body (selection observer -> resolvePick body).
        { "boolean", "Combine", "combine", {
            ComboL ("op",         "Operation",          "Operation",
                    "combine|subtract|intersect", "Add|Subtract|Common"),
            Well   ("targets",    "Main / Target Bodies","Bodies", K_BODY, true),
            Well   ("tools",      "Tool Bodies",        "Bodies", K_BODY, true),
            Check  ("keep_tools", "Keep tool bodies",   "Options", false),
        }},
        // --- sm_base_flange (Sheet Metal) --- engine features/sheet_metal sm_base_flange. Profile is
        //   a sketch region, resolved exactly like extrude's profile well (K_FACE, multi). thickness
        //   drives the wall; k_factor (0..1, unitless) sets the bend-allowance neutral axis;
        //   default_bend_radius seeds the inner radius (defaults to thickness engine-side).
        { "sm_base_flange", "Base Flange", "sm_base_flange", {
            Well ("profile",            "Profile",            "Profile",          K_FACE, true),
            Dim  ("thickness",          "Thickness",          "Sheet Metal Gauges", 2.0),
            Range(DimU("k_factor",      "K-Factor",           "Sheet Metal Gauges", 0.45),
                  0.0, 1.0, 0.05),
            Dim  ("default_bend_radius","Bend Radius",        "Sheet Metal Gauges", 2.0),
        }},
        // --- sm_edge_flange (Sheet Metal) --- engine features/sheet_metal sm_edge_flange. The bend
        //   edge is a single straight thickness-face edge (K_EDGE). angle 0..180 deg; length is the
        //   flange wall; radius is the inner bend radius (defaults to thickness). position picks how
        //   the flange aligns to the picked edge; flip mirrors the bend direction. k_factor is
        //   inherited from the base flange — intentionally NOT on this form.
        { "sm_edge_flange", "Edge Flange", "sm_edge_flange", {
            Well   ("edge",     "Bend Edge",      "Flange",   K_EDGE, false),
            Range  (Angle("angle",    "Angle",          "Flange",   90.0), 0.0, 180.0),
            Dim    ("length",   "Length",         "Flange",   20.0),
            Dim    ("radius",   "Bend Radius",    "Flange",   2.0),
            ComboL ("position", "Flange Position","Flange",
                    "material_inside|material_outside|bend_outside",
                    "Material Inside|Material Outside|Bend Outside"),
            Check  ("flip",     "Reverse direction","Flange", false),
        }},
        // --- sm_hem (Sheet Metal) --- engine features/sheet_metal sm_hem. Fold a straight thickness-
        //   face edge back on itself. type picks closed (180° flat fold, tiny gap) / open (180° at a
        //   real radius, visible standoff) / teardrop (curls past 180°). length is the hem return
        //   wall; radius is the inner bend radius (defaults engine-side: ~T/2 for closed, T else).
        //   flip mirrors the fold side. k_factor inherited from the base flange (not on this form).
        { "sm_hem", "Hem", "sm_hem", {
            Well   ("edge",   "Bend Edge",      "Hem",   K_EDGE, false),
            ComboL ("type",   "Hem Type",       "Hem",
                    "closed|open|teardrop", "Closed|Open|Teardrop"),
            Dim    ("length", "Length",         "Hem",   10.0),
            Dim    ("radius", "Bend Radius",    "Hem",   2.0),
            Check  ("flip",   "Reverse direction","Hem", false),
        }},
        // --- sm_jog (Sheet Metal) --- engine features/sheet_metal sm_jog. Two opposing bends that
        //   step the flange to a parallel plane offset by the Z-step. angle is the jog bend angle
        //   (0..90]; offset is the step height; length is the flat end flange; radius is the inner
        //   bend radius (defaults to thickness). position aligns the first bend to the picked edge;
        //   flip mirrors the bend direction. k_factor inherited from the base flange.
        { "sm_jog", "Jog", "sm_jog", {
            Well   ("edge",     "Bend Edge",      "Jog",   K_EDGE, false),
            Range  (Angle("angle",    "Angle",          "Jog",   90.0), 0.0, 90.0),
            Dim    ("offset",   "Offset",         "Jog",   10.0),
            Dim    ("length",   "Length",         "Jog",   20.0),
            Dim    ("radius",   "Bend Radius",    "Jog",   2.0),
            ComboL ("position", "Flange Position","Jog",
                    "material_inside|material_outside|bend_outside",
                    "Material Inside|Material Outside|Bend Outside"),
            Check  ("flip",     "Reverse direction","Jog", false),
        }},
        // --- weld_member (Weldments / Structural Member) --- engine features weld_member. Sweeps a
        //   library profile along the segments of ONE path sketch (refs.sketch = a single sketch
        //   feature-id STRING, not an array — FeatWell1 single-emit). segments are OMITTED here so the
        //   engine defaults to ALL sketch segments. The profile combo picks one of six library shapes;
        //   each shape's dim keys are gated on that combo with omit=true so only the active profile's
        //   dims are emitted (the engine's per-profile defaults drive the rest). thickness applies to
        //   every profile. angle rotates the section; justify centres vs corner-aligns it to the path;
        //   trim_corners miters where members meet.
        { "weld_member", "Structural Member", "weld_member", {
            FeatWell1 ("sketch",  "Path Sketch",   "Selections"),
            ComboL    ("profile", "Profile",       "Profile",
                       "square_tube|rect_tube|round_tube|angle_L|flat_bar|c_channel",
                       "Square Tube|Rect Tube|Round Tube|Angle (L)|Flat Bar|C-Channel"),
            DimIf     ("size",    "Size",          "Profile", 20.0, "profile", "square_tube",          true),
            DimIf     ("w",       "Width",         "Profile", 40.0, "profile", "rect_tube|c_channel|flat_bar", true),
            DimIf     ("h",       "Height",        "Profile", 20.0, "profile", "rect_tube|c_channel",  true),
            DimIf     ("od",      "Outer Dia.",    "Profile", 25.0, "profile", "round_tube",           true),
            DimIf     ("leg",     "Leg",           "Profile", 25.0, "profile", "angle_L",              true),
            Dim       ("thickness","Thickness",    "Profile", 2.0),
            Range     (Angle("angle",   "Rotation",      "Settings", 0.0), 0.0, 360.0),
            Seg       (ComboL("justify", "Justify",       "Settings", "center|corner",
                       "Center|Corner")),
            Check     ("trim_corners", "Trim corners", "Settings", true),
        }},
        // --- weld_gusset (Weldments / Gusset) --- engine features/weldment weld_gusset. A triangular
        //   (or polygonal cut-corner) reinforcing plate welded into the L-corner of two members. The
        //   faces well takes TWO planar faces (one on each member at the corner — e.g. the two
        //   members' top faces); the engine recovers each member axis from the pick and builds the
        //   plate in the faces' shared plane. d1/d2 are the legs along each member; type picks the
        //   plate shape; location shifts the plate ±½·thickness off the shared plane.
        { "weld_gusset", "Gusset", "weld_gusset", {
            Well   ("faces",     "Supporting Faces", "Selections", K_FACE, true),
            Dim    ("thickness", "Thickness",        "Gusset",  5.0),
            Dim    ("d1",        "Leg 1 (d1)",       "Gusset",  30.0),
            Dim    ("d2",        "Leg 2 (d2)",       "Gusset",  30.0),
            ComboL ("type",      "Profile",          "Gusset",
                    "triangular|polygonal", "Triangular|Polygonal"),
            ComboL ("location",  "Location",         "Settings",
                    "centered|inner|outer", "Centered|Inner|Outer"),
            Signed (Dim("offset",    "Offset",           "Settings", 0.0)),
        }},
        // --- weld_end_cap (Weldments / End Cap) --- engine features/weldment weld_end_cap. A plate
        //   closing the open END of a hollow member. The face well takes ONE planar member end face.
        //   type: outer mounts a plate covering the whole tube end + overhang on the end face; inner
        //   insets a plate matching the bore into the tube. offset shifts along the member axis.
        { "weld_end_cap", "End Cap", "weld_end_cap", {
            Well   ("face",      "End Face",         "Selections", K_FACE, false),
            Dim    ("thickness", "Thickness",        "End Cap", 5.0),
            Seg    (ComboL("type",      "Type",             "End Cap",
                    "outer|inner", "Outer (on end)|Inner (inset)")),
            DimU   ("overhang",  "Overhang",         "End Cap", 0.0),
            Signed (Dim("offset",    "Offset",           "End Cap", 0.0)),
        }},
        // --- rib (Dress-up / Stretch) --- engine features/rib.py. A thin support wall grown from an
        //   OPEN sketch contour (a line/chain bridging the gap). The profile well is a region well
        //   (key "profile" -> the viewport region picker fills it, like extrude), but the engine reads
        //   the sketch's OPEN contour, not a closed region. thickness is the wall width; side centres
        //   it (both) or grows to one side (one); draft tapers the sides; flip flips the one-sided dir.
        { "rib", "Rib", "rib", {
            Well   ("profile",   "Profile (open contour)", "Profile", K_FACE, true),
            Dim    ("thickness", "Thickness",              "Rib",     3.0),
            Seg    (ComboL("side",      "Thickness Side",         "Rib",
                    "both|one", "Both (centered)|One Side")),
            Range  (Angle("draft",     "Draft Angle",            "Rib",     0.0), 0.0, 89.0),
            Check  ("flip",      "Reverse direction",      "Rib",     false),
        }},
        // --- split (Curves / Stretch) --- engine features/split.py. Divide the running body(ies) by a
        //   planar tool into above/below pieces. The tool is a standard datum plane (DatumRef) OR a
        //   picked planar model face (the Well shares the "tool" key and is LATER, so a pick overrides
        //   the datum — last-wins in buildOpFeature). keep chooses which resulting piece(s) to keep.
        { "split", "Split", "split", {
            DatumRef ("tool", "Tool Plane",          "Trim/Split Tool",
                      "plane.top|plane.front|plane.right", "Top plane|Front plane|Right plane"),
            Well     ("tool", "…or pick a planar face", "Trim/Split Tool", K_FACE, false),
            ComboL   ("keep", "Resulting Bodies",    "Resulting Bodies",
                      "both|above|below", "Keep Both|Keep Above|Keep Below"),
        }},

        // =====================================================================
        // SURFACING — open-surface (kind="surface") features. cmd_id == feature
        // type, so each routes straight through pmFormForCommand. Wells/params
        // mirror engine/doc/schema.py FEATURE_TYPES surf_* exactly. _SEL_OR_DATUM
        // slots (axis/path/surface/tool) use single-pick SelectorWells (the engine
        // resolves a picked edge/face/datum); _SEL_LIST slots use multi wells.
        // =====================================================================

        // --- surf_extrude --- refs.profile[]; params end/depth. Mirrors the solid
        //   extrude's profile well + end-condition combo + gated depth.
        { "surf_extrude", "Extruded Surface", "extrude_boss", {
            Well   ("profile", "Profile",       "Profile",   K_FACE, true),
            ComboL ("end",     "End Condition", "Direction",
                    "blind|mid_plane|through_all|up_to_next",
                    "Blind|Mid Plane|Through All|Up To Next"),
            DimIf  ("depth",   "Distance",      "Direction", 10.0, "end", "blind|mid_plane"),
        }},
        // --- surf_revolve --- refs.profile[]/axis; params angle.
        { "surf_revolve", "Revolved Surface", "revolve_boss", {
            Well  ("profile", "Profile", "Revolve", K_FACE, true),
            Well  ("axis",    "Axis",    "Revolve", K_EDGEAXIS, false),  // K_EDGEAXIS (not K_EDGE) so a datum axis is accepted — mirrors solid revolve
            Range (Angle("angle",   "Angle",   "Revolve", 360.0), 0.0, 360.0),
        }},
        // --- surf_sweep --- refs.profile[]/path/guides[]; no params.
        { "surf_sweep", "Swept Surface", "sweep", {
            Well ("profile", "Profile",      "Profile and Path", K_FACE, true),
            Well ("path",    "Path",         "Profile and Path", K_EDGE, false),
            Well ("guides",  "Guide Curves", "Guide Curves",     K_EDGE, true),
        }},
        // --- surf_loft --- refs.sections[]/guides[]/centerline; no params.
        { "surf_loft", "Lofted Surface", "loft", {
            Well ("sections",   "Profiles",     "Profiles",     K_FACE, true),
            Well ("guides",     "Guide Curves", "Guide Curves", K_EDGE, true),
            Well ("centerline", "Centerline",   "Centerline",   K_EDGE, false),
        }},
        // --- surf_planar --- refs.profile[]; no params. A planar surface filling a
        //   closed sketch contour or bounded by closed edges.
        { "surf_planar", "Planar Surface", "ref_plane", {
            Well ("profile", "Bounding Entities", "Planar Surface", K_FACE, true),
        }},
        // --- surf_offset --- refs.faces[]; params distance.
        { "surf_offset", "Offset Surface", "offset_ent", {
            Well ("faces",    "Faces to Offset", "Offset Parameters", K_FACE, true),
            Signed(Dim("distance", "Offset Distance", "Offset Parameters", 10.0)),
        }},
        // --- surf_ruled --- refs.edge_a/edge_b (each one edge/curve); no params.
        { "surf_ruled", "Ruled Surface", "shell", {
            Well ("edge_a", "Edge 1", "Edge Selection", K_EDGE, false),
            Well ("edge_b", "Edge 2", "Edge Selection", K_EDGE, false),
        }},
        // --- surf_boundary --- refs.dir1[] (required) / dir2[] (optional); no params.
        //   Two direction curve sets blended into a boundary patch.
        { "surf_boundary", "Boundary Surface", "boundary", {
            Well ("dir1", "Direction 1 Curves", "Direction 1", K_EDGE, true),
            Well ("dir2", "Direction 2 Curves", "Direction 2", K_EDGE, true),
        }},
        // --- surf_fill --- refs.edges[]; no params. Patch fills a bounded edge loop.
        { "surf_fill", "Filled Surface", "boundary", {
            Well ("edges", "Patch Boundary", "Fill Boundary", K_EDGE, true),
        }},
        // --- surf_knit --- refs.surfaces[] (multi surface bodies); no params.
        //   Knit adjacent surface bodies into one. K_FACE so the user picks faces of
        //   the surface bodies to knit (the engine resolves the owning surface bodies).
        { "surf_knit", "Knit Surface", "combine", {
            Well ("surfaces", "Surfaces to Knit", "Selections", K_FACE, true),
        }},
        // --- surf_thicken --- refs.surface (one surface body); params thickness.
        { "surf_thicken", "Thicken", "extrude_boss", {
            Well ("surface",   "Surface",   "Thicken Parameters", K_FACE, false),
            Dim  ("thickness", "Thickness", "Thicken Parameters", 2.0),
        }},
        // --- surf_trim --- refs.surface/tool; params keep. Trim a surface with a
        //   tool surface, keeping the inside or outside piece.
        { "surf_trim", "Trim Surface", "trim", {
            Well   ("surface", "Surface to Trim", "Trim", K_FACE, false),
            Well   ("tool",    "Trim Tool",       "Trim", K_FACE, false),
            Seg    (ComboL("keep",    "Keep Selection",  "Trim",
                    "inside|outside", "Keep Inside|Keep Outside")),
        }},
        // --- surf_untrim --- refs.faces[]; no params. Restore a face's natural
        //   (untrimmed) underlying surface.
        { "surf_untrim", "Untrim Surface", "convert_ent", {
            Well ("faces", "Faces to Untrim", "Untrim", K_FACE, true),
        }},
        // --- surf_delete_face --- refs.faces[]; no params. Remove faces from a body.
        { "surf_delete_face", "Delete Face", "subtract", {
            Well ("faces", "Faces to Delete", "Selections", K_FACE, true),
        }},
        // --- surf_replace_face --- refs.faces[]/surface; no params. Replace target
        //   faces with a replacement surface body.
        { "surf_replace_face", "Replace Face", "wrap", {
            Well ("faces",   "Faces to Replace",  "Replace Face", K_FACE, true),
            Well ("surface", "Replacement Surface","Replace Face", K_FACE, false),
        }},
    };
    return kForms;
}

const PMFormDef* pmFindForm(const std::string& type) {
    for (const auto& f : forms())
        if (type == f.type) return &f;
    return nullptr;
}

const PMFormDef* pmFormForCommand(const std::string& cmdId, std::string& mode) {
    mode.clear();
    // Direct feature commands.
    if (cmdId == "fillet")        return pmFindForm("fillet");
    if (cmdId == "chamfer")       return pmFindForm("chamfer");
    if (cmdId == "shell")         return pmFindForm("shell");
    // Boss / cut split buttons -> extrude / revolve with a mode hint.
    if (cmdId == "extrude_boss")  { mode = "boss"; return pmFindForm("extrude"); }
    if (cmdId == "extrude_cut")   { mode = "cut";  return pmFindForm("extrude"); }
    if (cmdId == "revolve_boss")  { mode = "boss"; return pmFindForm("revolve"); }
    if (cmdId == "revolve_cut")   { mode = "cut";  return pmFindForm("revolve"); }
    if (cmdId == "sweep_boss")    { mode = "boss"; return pmFindForm("sweep"); }
    if (cmdId == "sweep_cut")     { mode = "cut";  return pmFindForm("sweep"); }
    if (cmdId == "loft_boss")     { mode = "boss"; return pmFindForm("loft"); }
    if (cmdId == "loft_cut")      { mode = "cut";  return pmFindForm("loft"); }
    // Hole wizard, draft, and the feature pattern/mirror family (no boss/cut mode).
    if (cmdId == "hole_wizard")      return pmFindForm("hole");
    if (cmdId == "thread")           return pmFindForm("thread");
    if (cmdId == "draft")            return pmFindForm("draft");
    if (cmdId == "mirror_feature")   return pmFindForm("mirror");
    if (cmdId == "linear_pattern")   return pmFindForm("linear_pattern");
    if (cmdId == "circular_pattern") return pmFindForm("circular_pattern");
    // Curves + reference geometry + multi-body booleans (W3/W4 — engine compilers now exist).
    if (cmdId == "curve_helix")      return pmFindForm("helix");
    if (cmdId == "ref_plane")        return pmFindForm("plane");
    if (cmdId == "ref_axis")         return pmFindForm("axis");
    if (cmdId == "ref_point")        return pmFindForm("point");
    if (cmdId == "ref_csys")         return pmFindForm("csys");
    if (cmdId == "boolean_combine")   { mode = "combine";   return pmFindForm("boolean"); }
    if (cmdId == "boolean_subtract")  { mode = "subtract";  return pmFindForm("boolean"); }
    if (cmdId == "boolean_intersect") { mode = "intersect"; return pmFindForm("boolean"); }
    // Sheet Metal — ribbon cmd id == the engine feature type, so route straight through.
    if (cmdId == "sm_base_flange")    return pmFindForm("sm_base_flange");
    if (cmdId == "sm_edge_flange")    return pmFindForm("sm_edge_flange");
    if (cmdId == "sm_hem")            return pmFindForm("sm_hem");
    if (cmdId == "sm_jog")            return pmFindForm("sm_jog");
    // Weldments — ribbon cmd id == the engine feature type.
    if (cmdId == "weld_member")       return pmFindForm("weld_member");
    if (cmdId == "weld_gusset")       return pmFindForm("weld_gusset");
    if (cmdId == "weld_end_cap")      return pmFindForm("weld_end_cap");
    // Stretch — rib (thin wall from an open contour) + split (divide by a planar tool). cmd id ==
    // the engine feature type.
    if (cmdId == "rib")               return pmFindForm("rib");
    if (cmdId == "split")             return pmFindForm("split");
    // Surfacing — open-surface (kind="surface") features. ribbon cmd id == the engine feature
    // type, so each routes straight through. Listed per-id so the feature-surface audit sees an
    // explicit handler for every surf_ command.
    if (cmdId == "surf_extrude")      return pmFindForm("surf_extrude");
    if (cmdId == "surf_revolve")      return pmFindForm("surf_revolve");
    if (cmdId == "surf_sweep")        return pmFindForm("surf_sweep");
    if (cmdId == "surf_loft")         return pmFindForm("surf_loft");
    if (cmdId == "surf_planar")       return pmFindForm("surf_planar");
    if (cmdId == "surf_offset")       return pmFindForm("surf_offset");
    if (cmdId == "surf_ruled")        return pmFindForm("surf_ruled");
    if (cmdId == "surf_boundary")     return pmFindForm("surf_boundary");
    if (cmdId == "surf_fill")         return pmFindForm("surf_fill");
    if (cmdId == "surf_knit")         return pmFindForm("surf_knit");
    if (cmdId == "surf_thicken")      return pmFindForm("surf_thicken");
    if (cmdId == "surf_trim")         return pmFindForm("surf_trim");
    if (cmdId == "surf_untrim")       return pmFindForm("surf_untrim");
    if (cmdId == "surf_delete_face")  return pmFindForm("surf_delete_face");
    if (cmdId == "surf_replace_face") return pmFindForm("surf_replace_face");
    // Primitive quick-start commands (Chudware extensions; not in the C14 ribbon but reachable
    // via the M1 scenario / future "Insert > Primitive" entries).
    if (cmdId == "box")           return pmFindForm("box");
    if (cmdId == "cylinder")      return pmFindForm("cylinder");
    return nullptr;
}

}  // namespace ui
