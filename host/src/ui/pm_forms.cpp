// PropertyManager form table (C17).
//
// ONE data-driven table per feature type, each row in docs/contracts/feature_params.md field
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
static PMFieldDef Check(const char* key, const char* label, const char* group, bool def) {
    PMFieldDef f; f.kind = PMFieldKind::Check; f.key = key; f.label = label;
    f.group = group; f.checkDefault = def; return f;
}
// Integer param (pattern instance counts). No unit suffix.
static PMFieldDef Int(const char* key, const char* label, const char* group, double def) {
    PMFieldDef f; f.kind = PMFieldKind::Int; f.key = key; f.label = label;
    f.group = group; f.dimDefault = def; f.unitLabel = ""; return f;
}
// Unitless Dim (real-valued, no "[mm]" suffix) — e.g. sheet-metal k-factor in 0..1.
static PMFieldDef DimU(const char* key, const char* label, const char* group, double def) {
    PMFieldDef f = Dim(key, label, group, def); f.unitLabel = ""; return f;
}
// Angle dim (shows "[deg]" instead of "[mm]"; value is plain degrees, like revolve's angle).
static PMFieldDef Angle(const char* key, const char* label, const char* group, double def) {
    PMFieldDef f = Dim(key, label, group, def); f.unitLabel = " [deg]"; return f;
}
// Angle dim enabled only when a sibling Combo (named enableKey) holds one of enableVals.
static PMFieldDef AngleIf(const char* key, const char* label, const char* group, double def,
                          const char* enableKey, const char* enableVals) {
    PMFieldDef f = Angle(key, label, group, def);
    f.enableKey = enableKey; f.enableVals = enableVals; return f;
}
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
// Check/Dim that is disabled and shows "(coming soon)" — discoverability only, never serialised.
static PMFieldDef CheckSoon(const char* key, const char* label, const char* group) {
    PMFieldDef f = Check(key, label, group, false); f.comingSoon = true; return f;
}
static PMFieldDef DimSoon(const char* key, const char* label, const char* group, double def) {
    PMFieldDef f = Dim(key, label, group, def); f.comingSoon = true; return f;
}
static const PMSelKinds K_FACE   {true,  false, false};
static const PMSelKinds K_EDGE   {false, true,  false};
static const PMSelKinds K_VERTEX {false, false, true};
static const PMSelKinds K_FEV    {true,  true,  true };   // face/edge/vertex (datum a / point target)
static const PMSelKinds K_FACEEDGE {true, true,  false};  // face or edge (datum plane hinge / midplane #2)
static const PMSelKinds K_FACEVERT {true, false, true};   // face or vertex (datum axis b)
static const PMSelKinds K_BODY   {false, false, false, true};  // solid body (boolean targets/tools)

// ----------------------------------------------------------------------------
// The forms. Field order == feature_params.md order == PM tab order.
// ----------------------------------------------------------------------------
static const std::vector<PMFormDef>& forms() {
    static const std::vector<PMFormDef> kForms = {
        // --- box ---  length/width/height/centered ; refs at? (origin default)
        { "box", "Box", "extrude_boss", {
            Dim   ("length",   "Length",   "Box",  20.0),
            Dim   ("width",    "Width",    "Box",  20.0),
            Dim   ("height",   "Height",   "Box",  20.0),
            Check ("centered", "Centered", "Box",  true),
        }},
        // --- cylinder ---  radius/height/centered
        { "cylinder", "Cylinder", "extrude_boss", {
            Dim   ("radius",   "Radius",   "Cylinder", 10.0),
            Dim   ("height",   "Height",   "Cylinder", 20.0),
            Check ("centered", "Centered", "Cylinder", true),
        }},
        // --- extrude ---  mode/end/depth/flip/draft/draft_outward/merge ; profile[]
        //   Direction 2: end2 / depth2 (C26-B engine; UI ready, errors gracefully before merge)
        { "extrude", "Extrude", "extrude_boss", {
            Well      ("profile",  "Profile",         "From",        K_FACE, true),
            ComboL    ("mode",     "Type",            "Direction 1", "boss|cut",
                                                                     "Boss|Cut"),
            ComboL    ("end",      "End Condition",   "Direction 1",
                       "blind|mid_plane|through_all|up_to_next",
                       "Blind|Mid Plane|Through All|Up To Next"),
            DimIf     ("depth",    "Depth",           "Direction 1", 10.0,
                       "end", "blind|mid_plane"),
            Check     ("flip",     "Reverse direction","Direction 1", false),
            Dim       ("draft",    "Draft angle",     "Direction 1", 0.0),
            Check     ("draft_outward", "Draft outward","Direction 1", false),
            ComboL    ("end2",     "End Condition",   "Direction 2",
                       "none|blind|through_all|up_to_next",
                       "None|Blind|Through All|Up To Next"),
            DimIf     ("depth2",   "Depth",           "Direction 2", 10.0,
                       "end2", "blind"),
            Check     ("merge",    "Merge result",    "Options",      true),
        }},
        // --- revolve ---  mode/angle/flip/merge ; profile[]/axis
        //   Engine (revolve.py): mode, angle, flip, merge — all exposed. No mid-plane in engine.
        { "revolve", "Revolve", "revolve_boss", {
            Well  ("profile",  "Profile",        "From",      K_FACE, true),
            Well  ("axis",     "Axis",           "From",      K_EDGE, false),
            Combo ("mode",     "Type",           "Direction", "boss|cut"),
            Dim   ("angle",    "Angle",          "Direction", 360.0),
            Check ("flip",     "Reverse direction", "Direction", false),
            Check ("merge",    "Merge result",   "Options",   true),
        }},
        // --- sweep ---  mode/merge ; profile[] (region) + path (sketch chain or model edge)
        //   Engine (sweep.py): profile resolves like extrude (whole-sketch/index/centroid region or
        //   model face); path = a sketch region selector (its edge chain becomes the spine) or a
        //   model edge. The viewport region picker fills profile; path is seeded/clicked.
        { "sweep", "Sweep", "sweep", {
            Well      ("profile", "Profile",      "Profile and Path", K_FACE, true),
            Well      ("path",    "Path",         "Profile and Path", K_EDGE, false),
            ComboL    ("mode",    "Type",         "Options", "boss|cut", "Boss|Cut"),
            Check     ("merge",   "Merge result", "Options", true),
        }},
        // --- loft ---  mode/ruled/merge ; sections[] (>=2 ordered region selectors)
        //   Engine (loft.py): ThruSections through each section's outer wire, in list order.
        { "loft", "Loft", "loft", {
            Well      ("sections","Profiles",     "Profiles", K_FACE, true),
            ComboL    ("mode",    "Type",         "Options", "boss|cut", "Boss|Cut"),
            Check     ("ruled",   "Ruled (straight transitions)", "Options", false),
            Check     ("merge",   "Merge result", "Options", true),
        }},
        // --- fillet ---  radius / [radius2] ; edges[]  OR  full_round + middle_face
        //   C30-A: variable-size fillet. A ComboL "type" (constant|variable) gates radius2 via the
        //   existing DimIf/enableKey mechanism — mirrors the chamfer Symmetric|Asymmetric gating.
        //   "type" is UI-only; the engine keys off radius2 (absent/equal => constant path).
        //   C33-B: extend "type" with "full_round" (Check has no gating constructor, so the proven
        //   Combo-gating path is the clean fit). full_round swaps the edge/radius rows for a single
        //   "Middle Face" face well; the type combo also emits params.full_round=true (the engine
        //   reads params.full_round + refs.middle_face, ignoring edges/radius on that path).
        //   tangent_prop remains disabled "(coming soon)" for discoverability.
        { "fillet", "Fillet", "fillet", {
            WellIf    ("edges",      "Edges",              "Items to Fillet",    K_EDGE, true,
                       "type", "constant|variable"),
            WellIf    ("middle_face","Middle Face",        "Items to Fillet",    K_FACE, false,
                       "type", "full_round"),
            ComboL    ("type",       "Fillet Type",        "Fillet Parameters",
                       "constant|variable|full_round",
                       "Constant Size|Variable Size|Full Round"),
            DimIf     ("radius",     "Radius",             "Fillet Parameters",  2.0,
                       "type", "constant|variable"),
            DimIf     ("radius2",    "End Radius",         "Fillet Parameters",  2.0,
                       "type", "variable"),
            CheckSoon ("tangent_prop","Tangent propagation (coming soon)", "Fillet Parameters"),
        }},
        // --- chamfer ---  length / [length2] ; edges[]
        //   C27-C: asymmetric chamfer. A Combo "kind" (symmetric|asymmetric) gates length2 via
        //   the existing DimIf/enableKey mechanism — the only gating plumbing in this form system.
        //   (Check has no gating constructor; a 2-entry Combo is the cleanest existing fit.)
        //   Engine ignores "kind" param; length2=0/absent => symmetric path in dress.py.
        { "chamfer", "Chamfer", "chamfer", {
            Well    ("edges",    "Edges",                       "Items to Chamfer",   K_EDGE, true),
            Dim     ("length",   "Distance",                    "Chamfer Parameters", 1.0),
            ComboL  ("kind",     "Chamfer Type",                "Chamfer Parameters",
                     "symmetric|asymmetric", "Symmetric|Asymmetric"),
            DimIf   ("length2",  "Distance 2",                  "Chamfer Parameters", 1.0,
                     "kind", "asymmetric"),
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
                     "M2|M2.5|M3|M4|M5|M6|M8|M10|M12|custom",
                     "M2|M2.5|M3|M4|M5|M6|M8|M10|M12|Custom"),
            DimIf   ("diameter", "Diameter",         "Hole Type", 6.6, "standard", "custom", true),
            AngleIf ("csink_angle", "C'Sink Angle",  "Hole Type", 90.0, "style", "csink"),
            ComboL  ("end",      "End Condition",    "End Condition",
                     "through_all|blind", "Through All|Blind"),
            DimIf   ("depth",    "Depth",            "End Condition", 10.0, "end", "blind"),
            Well    ("positions","Position Vertices","Positions", K_VERTEX, true),
            Int     ("nx",       "Count X",          "Positions", 1),
            Int     ("ny",       "Count Y",          "Positions", 1),
            Dim     ("dx",       "Pitch X",          "Positions", 20.0),
            Dim     ("dy",       "Pitch Y",          "Positions", 20.0),
        }},
        // --- mirror ---  plane (datum/face) + features[] (seeded from selection)
        //   Engine (pattern.py compile_mirror): reflects each seed feature's tool body across the
        //   plane with the same boolean; empty features => whole-body mirror + union.
        { "mirror", "Mirror", "mirror_feature", {
            DatumRef ("plane",    "Mirror Face/Plane", "Mirror Plane",
                      "plane.right|plane.front|plane.top",
                      "Right plane|Front plane|Top plane"),
            FeatWell ("features", "Features to Mirror", "Features to Mirror"),
        }},
        // --- linear_pattern ---  dir1(+count/spacing), optional dir2, features[]
        //   Engine (pattern.py compile_linear_pattern): lays down a count1×count2 grid of the seed
        //   features' tool bodies (re-applying each boolean). count2=1 disables direction 2.
        { "linear_pattern", "Linear Pattern", "linear_pattern", {
            DatumRef ("dir1",     "Direction 1",  "Direction 1",
                      "axis.x|axis.y|axis.z", "X axis|Y axis|Z axis"),
            Int      ("count1",   "Instances",    "Direction 1", 3),
            Dim      ("spacing1", "Spacing",      "Direction 1", 20.0),
            DatumRef ("dir2",     "Direction 2",  "Direction 2",
                      "axis.y|axis.x|axis.z", "Y axis|X axis|Z axis"),
            Int      ("count2",   "Instances",    "Direction 2", 1),
            Dim      ("spacing2", "Spacing",      "Direction 2", 20.0),
            FeatWell ("features", "Features to Pattern", "Features to Pattern"),
        }},
        // --- circular_pattern ---  axis + count/angle + equal_spacing, features[]
        //   Engine (pattern.py compile_circular_pattern): count instances about the axis; equal
        //   spacing fills the angle (step = angle/count), else angle is the per-step increment.
        { "circular_pattern", "Circular Pattern", "circular_pattern", {
            DatumRef ("axis",     "Pattern Axis", "Parameters",
                      "axis.z|axis.x|axis.y", "Z axis|X axis|Y axis"),
            Int      ("count",    "Instances",    "Parameters", 4),
            Angle    ("angle",    "Angle",        "Parameters", 360.0),
            Check    ("equal_spacing", "Equal spacing", "Parameters", true),
            FeatWell ("features", "Features to Pattern", "Features to Pattern"),
        }},
        // --- draft ---  angle + neutral plane + faces[]
        //   Engine (draft.py): tapers each selected face about the line where it meets the neutral
        //   plane (OCCT DraftAngle). Pull direction defaults to the neutral plane normal.
        { "draft", "Draft", "draft", {
            Angle    ("angle",   "Draft Angle",   "Draft", 3.0),
            DatumRef ("neutral", "Neutral Plane", "Draft",
                      "plane.top|plane.front|plane.right",
                      "Top plane|Front plane|Right plane"),
            Well     ("faces",   "Faces to Draft", "Draft", K_FACE, true),
        }},
        // --- helix (Curve) --- engine features/curves.py. Produces a sweep PATH (a Sweep can then
        //   reference this helix feature). v1 is "Height & Pitch"-defined; the engine derives turns
        //   = height/pitch. radius defaults the coil radius when base is a datum plane.
        { "helix", "Helix / Spiral", "curve_helix", {
            DatumRef ("base",   "Axis Plane",       "Helix",
                      "plane.top|plane.front|plane.right", "Top plane|Front plane|Right plane"),
            Dim      ("radius", "Radius",           "Helix", 10.0),
            Dim      ("pitch",  "Pitch",            "Helix", 5.0),
            Dim      ("height", "Height",           "Helix", 20.0),
            Angle    ("taper",  "Taper Angle",      "Helix", 0.0),
            Angle    ("start_angle", "Start Angle", "Helix", 0.0),
            Check    ("ccw",    "Counterclockwise", "Helix", true),
        }},
        // --- ref_plane (datum) --- engine datum.py compile_plane. method gates which refs apply:
        //   offset/angle use base (+ second hinge for angle); three_point uses 3 vertices; midplane
        //   uses base + second. base is a DatumRef (the 3 origin planes); second/points are picks.
        { "plane", "Plane", "ref_plane", {
            ComboL   ("method", "Type",            "First Reference",
                      "offset|angle|three_point|midplane",
                      "Offset|At Angle|Three Points|Mid Plane"),
            DatumRef ("base",   "Reference",       "First Reference",
                      "plane.top|plane.front|plane.right", "Top plane|Front plane|Right plane"),
            DimIf    ("offset", "Offset Distance", "Parameters", 10.0, "method", "offset"),
            AngleIf  ("angle",  "Angle",           "Parameters", 45.0, "method", "angle"),
            WellIf   ("second", "Hinge / 2nd Plane","Second Reference", K_FACEEDGE, false,
                      "method", "angle|midplane"),
            WellIf   ("points", "Three Vertices",  "Points", K_VERTEX, true,
                      "method", "three_point"),
            Check    ("flip",   "Flip",            "Parameters", false),
        }},
        // --- ref_axis (datum) --- engine datum.py compile_axis. a = the primary reference (cyl
        //   face / edge / vertex); b = the second vertex (two_points) or plane (plane_intersect).
        { "axis", "Axis", "ref_axis", {
            ComboL ("method", "Type",        "Reference",
                    "cylinder|two_points|edge|plane_intersect",
                    "Cylindrical Face|Two Points|Edge|Two Planes"),
            Well   ("a",      "Reference 1", "Reference", K_FEV, false),
            WellIf ("b",      "Reference 2", "Reference", K_FACEVERT, false,
                    "method", "two_points|plane_intersect"),
        }},
        // --- ref_point (datum) --- engine datum.py compile_point. vertex/arc-center/on-face.
        { "point", "Point", "ref_point", {
            ComboL ("method", "Type",      "Reference",
                    "vertex|center|on_face", "At Vertex|Arc/Circle Center|On Face"),
            Well   ("target", "Reference", "Reference", K_FEV, false),
        }},
        // --- ref_csys (datum) --- engine datum.py compile_csys. origin + optional X/Y direction
        //   edges (orthonormalized engine-side).
        { "csys", "Coordinate System", "ref_csys", {
            Well ("origin", "Origin",        "Coordinate System", K_VERTEX, false),
            Well ("x_dir",  "X Axis (edge)", "Coordinate System", K_EDGE, false),
            Well ("y_dir",  "Y Axis (edge)", "Coordinate System", K_EDGE, false),
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
        { "sm_base_flange", "Base Flange", "extrude_boss", {
            Well ("profile",            "Profile",            "Profile",          K_FACE, true),
            Dim  ("thickness",          "Thickness",          "Sheet Metal Gauges", 2.0),
            DimU ("k_factor",           "K-Factor",           "Sheet Metal Gauges", 0.45),
            Dim  ("default_bend_radius","Bend Radius",        "Sheet Metal Gauges", 2.0),
        }},
        // --- sm_edge_flange (Sheet Metal) --- engine features/sheet_metal sm_edge_flange. The bend
        //   edge is a single straight thickness-face edge (K_EDGE). angle 0..180 deg; length is the
        //   flange wall; radius is the inner bend radius (defaults to thickness). position picks how
        //   the flange aligns to the picked edge; flip mirrors the bend direction. k_factor is
        //   inherited from the base flange — intentionally NOT on this form.
        { "sm_edge_flange", "Edge Flange", "shell", {
            Well   ("edge",     "Bend Edge",      "Flange",   K_EDGE, false),
            Angle  ("angle",    "Angle",          "Flange",   90.0),
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
        { "sm_hem", "Hem", "shell", {
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
        { "sm_jog", "Jog", "extrude_boss", {
            Well   ("edge",     "Bend Edge",      "Jog",   K_EDGE, false),
            Angle  ("angle",    "Angle",          "Jog",   90.0),
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
        { "weld_member", "Structural Member", "sweep", {
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
            Angle     ("angle",   "Rotation",      "Settings", 0.0),
            ComboL    ("justify", "Justify",       "Settings", "center|corner", "Center|Corner"),
            Check     ("trim_corners", "Trim corners", "Settings", true),
        }},
        // --- weld_gusset (Weldments / Gusset) --- engine features/weldment weld_gusset. A triangular
        //   (or polygonal cut-corner) reinforcing plate welded into the L-corner of two members. The
        //   faces well takes TWO planar faces (one on each member at the corner — e.g. the two
        //   members' top faces); the engine recovers each member axis from the pick and builds the
        //   plate in the faces' shared plane. d1/d2 are the legs along each member; type picks the
        //   plate shape; location shifts the plate ±½·thickness off the shared plane.
        { "weld_gusset", "Gusset", "shell", {
            Well   ("faces",     "Supporting Faces", "Selections", K_FACE, true),
            Dim    ("thickness", "Thickness",        "Gusset",  5.0),
            Dim    ("d1",        "Leg 1 (d1)",       "Gusset",  30.0),
            Dim    ("d2",        "Leg 2 (d2)",       "Gusset",  30.0),
            ComboL ("type",      "Profile",          "Gusset",
                    "triangular|polygonal", "Triangular|Polygonal"),
            ComboL ("location",  "Location",         "Settings",
                    "centered|inner|outer", "Centered|Inner|Outer"),
            Dim    ("offset",    "Offset",           "Settings", 0.0),
        }},
        // --- weld_end_cap (Weldments / End Cap) --- engine features/weldment weld_end_cap. A plate
        //   closing the open END of a hollow member. The face well takes ONE planar member end face.
        //   type: outer mounts a plate covering the whole tube end + overhang on the end face; inner
        //   insets a plate matching the bore into the tube. offset shifts along the member axis.
        { "weld_end_cap", "End Cap", "fillet", {
            Well   ("face",      "End Face",         "Selections", K_FACE, false),
            Dim    ("thickness", "Thickness",        "End Cap", 5.0),
            ComboL ("type",      "Type",             "End Cap",
                    "outer|inner", "Outer (on end)|Inner (inset)"),
            DimU   ("overhang",  "Overhang",         "End Cap", 0.0),
            Dim    ("offset",    "Offset",           "End Cap", 0.0),
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
    // Primitive quick-start commands (AI-CAD extensions; not in the C14 ribbon but reachable
    // via the M1 scenario / future "Insert > Primitive" entries).
    if (cmdId == "box")           return pmFindForm("box");
    if (cmdId == "cylinder")      return pmFindForm("cylinder");
    return nullptr;
}

}  // namespace ui
