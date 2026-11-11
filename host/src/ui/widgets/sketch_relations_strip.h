// Sketch-ribbon relations strip (SolidWorks "Existing Relations" + "Add Relations", docked at the
// right end of the Sketch ribbon tab while a 2D sketch is being edited).
//
// The strip is a pure view over a published model. SketchMode publishes the relations on the
// current selection and the relations its Add Relation rule set offers for it once per frame
// (publishSketchRelationsStrip); the ribbon reserves sketchRelationsStripWidth() on the right of
// its body and draws the strip there (drawSketchRelationsStrip). Clicks are queued and SketchMode
// applies them on its next frame (takeSketchRelationsStripResult), so the ribbon never talks to the
// engine or to SketchMode directly.
#pragma once

#include <string>
#include <vector>

#include "imgui.h"

namespace ui {

// Draws one relation glyph centered at `center` inside a square chip of side `side` (the
// viewport's relation-badge glyphs, supplied by SketchMode).
using RelationGlyphFn = void (*)(ImDrawList* dl, ImVec2 center, float side, const char* type,
                                 ImU32 ink);

struct SketchRelationsStripModel {
    struct Existing {
        std::string id;                // constraint id
        std::string type;              // glyph type ("horizontal", "coincident", "pierce", ...)
        std::string tip;               // tooltip text (name + references)
        bool selected = false;         // in the sketch's relation selection
        bool automatic = false;        // soft auto-relation (drawn dimmed, like the viewport)
        bool problem = false;          // conflicting / skipped
    };
    struct Possible {
        std::string type;              // glyph type
        std::string label;             // Add Relation label ("Parallel", "Horizontal (each)")
    };
    bool hasSelection = false;         // any sketch entity or relation selected
    std::vector<Existing> existing;
    std::vector<Possible> possible;
    RelationGlyphFn drawGlyph = nullptr;
};

struct SketchRelationsStripResult {
    int addIndex = -1;                 // index into SketchRelationsStripModel::possible
    std::string toggleId;              // existing relation clicked (Ctrl: toggle, else select)
    bool additive = false;             // Ctrl held on toggleId
    std::string deleteId;              // existing relation deleted from its context menu
    bool openPanel = false;            // "+N" overflow: open Display/Delete Relations
    bool openAddRelation = false;      // overflow of the possible list: open Add Relation
    std::string hoveredId;             // existing relation under the mouse this frame
};

// SketchMode -> strip, once per frame while a 2D sketch is being edited.
void publishSketchRelationsStrip(SketchRelationsStripModel model);
// Strip -> SketchMode: the clicks gathered since the last call. Hover persists per frame.
SketchRelationsStripResult takeSketchRelationsStripResult();

// Width the ribbon reserves at the right of its body for the strip (DPI scaled).
float sketchRelationsStripWidth();
// Draws the strip as a ribbon group pill spanning [min, max] (screen space) inside the ribbon body
// window. `contentH` is the ribbon's button zone height; the caption sits below it.
void drawSketchRelationsStrip(const ImVec2& min, const ImVec2& max, float contentH);

}  // namespace ui
