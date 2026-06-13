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
static PMFieldDef DimIf(const char* key, const char* label, const char* group, double def,
                        const char* enableKey, const char* enableVals) {
    PMFieldDef f = Dim(key, label, group, def);
    f.enableKey = enableKey; f.enableVals = enableVals; return f;
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
// Angle dim (shows "[deg]" instead of "[mm]"; value is plain degrees, like revolve's angle).
static PMFieldDef Angle(const char* key, const char* label, const char* group, double def) {
    PMFieldDef f = Dim(key, label, group, def); f.unitLabel = " [deg]"; return f;
}
// A datum-reference combo whose chosen value is emitted as a refs string ("axis.x"/"plane.right").
// For _SEL_OR_DATUM slots (pattern direction, mirror plane, draft neutral plane).
static PMFieldDef DatumRef(const char* key, const char* label, const char* group,
                           const char* options, const char* labels) {
    PMFieldDef f; f.kind = PMFieldKind::DatumRef; f.key = key; f.label = label;
    f.group = group; f.comboOptions = options; f.comboLabels = labels; return f;
}
// A feature-id list (refs _FEAT_LIST), seeded from the FeatureManager selection — the features a
// pattern/mirror replicates.
static PMFieldDef FeatWell(const char* key, const char* label, const char* group) {
    PMFieldDef f; f.kind = PMFieldKind::FeatureWell; f.key = key; f.label = label;
    f.group = group; return f;
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
        // --- hole (Hole Wizard) ---  face + style/standard/diameter/end/depth
        //   Engine (hole.py): style simple|cbore|csink|tap; standard "custom" uses params.diameter,
        //   else an ISO-metric table (M2..M12) supplies diameter (+cbore/csink dims). No positions =>
        //   one hole at the face centre (multi-position grids are an engine capability the viewport
        //   point picker can drive later). end through_all/blind; blind uses depth.
        { "hole", "Hole", "hole_wizard", {
            Well   ("face",     "Face",          "Hole Position", K_FACE, false),
            ComboL ("style",    "Type",          "Hole Type",
                    "simple|cbore|csink|tap", "Simple|Counterbore|Countersink|Tapped"),
            ComboL ("standard", "Size standard", "Hole Type",
                    "custom|M2|M2.5|M3|M4|M5|M6|M8|M10|M12",
                    "Custom|M2|M2.5|M3|M4|M5|M6|M8|M10|M12"),
            DimIf  ("diameter", "Diameter",      "Hole Type", 6.0, "standard", "custom"),
            ComboL ("end",      "End Condition", "Direction",
                    "through_all|blind", "Through All|Blind"),
            DimIf  ("depth",    "Depth",         "Direction", 10.0, "end", "blind"),
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
    // Primitive quick-start commands (AI-CAD extensions; not in the C14 ribbon but reachable
    // via the M1 scenario / future "Insert > Primitive" entries).
    if (cmdId == "box")           return pmFindForm("box");
    if (cmdId == "cylinder")      return pmFindForm("cylinder");
    return nullptr;
}

}  // namespace ui
