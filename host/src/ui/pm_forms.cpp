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
static PMFieldDef Well(const char* key, const char* label, const char* group,
                       PMSelKinds kinds, bool multi) {
    PMFieldDef f; f.kind = PMFieldKind::SelectorWell; f.key = key; f.label = label;
    f.group = group; f.selKinds = kinds; f.selMulti = multi; return f;
}
static PMFieldDef Combo(const char* key, const char* label, const char* group,
                        const char* options) {
    PMFieldDef f; f.kind = PMFieldKind::Combo; f.key = key; f.label = label;
    f.group = group; f.comboOptions = options; return f;
}
static PMFieldDef Check(const char* key, const char* label, const char* group, bool def) {
    PMFieldDef f; f.kind = PMFieldKind::Check; f.key = key; f.label = label;
    f.group = group; f.checkDefault = def; return f;
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
        { "extrude", "Extrude", "extrude_boss", {
            Well  ("profile",  "Profile",  "From",  K_FACE, true),
            Combo ("mode",     "Type",     "Direction", "boss|cut"),
            Combo ("end",      "End",      "Direction", "blind|through_all|mid_plane"),
            Dim   ("depth",    "Depth",    "Direction", 10.0),
            Check ("flip",     "Flip",     "Direction", false),
            Dim   ("draft",    "Draft",    "Draft",  0.0),
            Check ("draft_outward", "Draft outward", "Draft", false),
            Check ("merge",    "Merge result", "Options", true),
        }},
        // --- revolve ---  mode/angle/flip/merge ; profile[]/axis
        { "revolve", "Revolve", "revolve_boss", {
            Well  ("profile",  "Profile",  "From",  K_FACE, true),
            Well  ("axis",     "Axis",     "From",  K_EDGE, false),
            Combo ("mode",     "Type",     "Direction", "boss|cut"),
            Dim   ("angle",    "Angle",    "Direction", 360.0),
            Check ("flip",     "Flip",     "Direction", false),
            Check ("merge",    "Merge result", "Options", true),
        }},
        // --- fillet ---  radius ; edges[]
        { "fillet", "Fillet", "fillet", {
            Well  ("edges",    "Edges",    "Items to Fillet", K_EDGE, true),
            Dim   ("radius",   "Radius",   "Fillet Parameters", 2.0),
        }},
        // --- chamfer ---  length/kind ; edges[]
        { "chamfer", "Chamfer", "chamfer", {
            Well  ("edges",    "Edges",    "Items to Chamfer", K_EDGE, true),
            Dim   ("length",   "Distance", "Chamfer Parameters", 1.0),
        }},
        // --- shell ---  thickness/outward ; remove[]
        { "shell", "Shell", "shell", {
            Well  ("remove",   "Faces to Remove", "Parameters", K_FACE, true),
            Dim   ("thickness","Thickness", "Parameters", 2.0),
            Check ("outward",  "Shell outward",   "Parameters", false),
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
    // Primitive quick-start commands (AI-CAD extensions; not in the C14 ribbon but reachable
    // via the M1 scenario / future "Insert > Primitive" entries).
    if (cmdId == "box")           return pmFindForm("box");
    if (cmdId == "cylinder")      return pmFindForm("cylinder");
    return nullptr;
}

}  // namespace ui
