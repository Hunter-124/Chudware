// Display/Delete Relations panel for the 2D sketcher (SolidWorks "Display/Delete Relations").
//
// A self-contained ImGui panel over a plain row model: SketchMode builds one RelationRow per
// sketch constraint (relations AND dimensions) each frame the panel is open, and applies the
// returned actions (delete through sketch_delete_constraint ops, selection into selRelIds_,
// hover highlight of the referenced geometry). The panel owns only its view state
// (filter, search text, scroll); it never talks to the engine.
#pragma once

#include <string>
#include <vector>

#include "imgui.h"

namespace ui {

struct RelationRow {
    std::string id;                    // constraint id ("c12")
    std::string type;                  // engine type ("coincident", "distance", "pierce" ...)
    std::string label;                 // caller-built text, e.g. "Line3.p2, Arc5.start"
    std::string value;                 // dimension value text ("25.00 mm", "90°"); "" for relations
    bool dimension = false;            // distance / angle / radius / diameter / length / arc_length
    bool driven = false;               // reference (driven) dimension
    bool external = false;             // touches geometry owned by an external (model) reference
    bool touchesSelection = false;     // references an entity in the current sketch selection
    enum class State { Ok, Redundant, Conflicting, Skipped } state = State::Ok;
    std::string stateReason;           // solver reason for Skipped / Conflicting, shown as tooltip
};

struct RelationsPanelState {
    bool open = false;
    // 0 all, 1 relations only, 2 dimensions only, 3 problems (redundant/conflicting/skipped),
    // 4 external references, 5 relations of the selected entities.
    int filter = 0;
    char search[64] = {};
};

struct RelationsPanelResult {
    std::vector<std::string> deleteIds;  // rows the user deleted this frame (button or Delete key)
    std::vector<std::string> selectIds;  // replacement relation selection; meaningful only if
    bool selectionChanged = false;       //   selectionChanged is true
    std::string hoveredId;               // row under the mouse this frame ("" when none)
};

// Draws the panel as a floating window placed inside [viewportMin, viewportMax] (top-right by
// default, user-movable). `selected` is the current relation selection. Does nothing and
// returns an empty result while state.open is false; the window close button clears it.
RelationsPanelResult drawRelationsPanel(RelationsPanelState& state,
                                        const std::vector<RelationRow>& rows,
                                        const std::vector<std::string>& selected,
                                        const ImVec2& viewportMin, const ImVec2& viewportMax);

}  // namespace ui
