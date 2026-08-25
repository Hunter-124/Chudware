// EcadCanvas — the 2D drawing surface for an ECAD document. Unlike host/src/ui/sketch_mode.cpp
// (which projects a sketch onto a 3D plane via the Renderer's camera MVP), the ECAD canvas is a
// PURE 2D editor: it owns its own pan/zoom camera and converts ECAD-world mm <-> screen pixels with
// a simple affine transform. The +y-up math convention of the document is flipped to +y-down screen
// here (the single place the contract says the C++ side flips).
//
// It renders BOTH document kinds from an EcadMirror:
//   * Schematic: symbol bodies (themed rects/lines/circles/polylines) + pin stubs + ref/value text +
//     orthogonal wires + junction dots + net labels + power-port glyphs.
//   * Board: board outline, footprint courtyards + pads (per-layer colored) + drill holes, copper
//     tracks per layer (back-to-front), vias, zone outlines, and ratsnest airwires as dashed lines.
//
// Drawing is done with ImGui::GetWindowDrawList() + ui::palette ROLES (never hardcoded colors),
// exactly as sketch_mode.cpp does. The canvas is stateless w.r.t. the document — it reads the mirror
// each frame — but it persists its camera (pan/zoom) between frames.
//
#pragma once

#include <string>

#include "imgui.h"
#include "ecad_mirror.h"

namespace ui {

// Per-frame interaction result the panel can act on.
struct EcadCanvasIO {
    bool  hovered = false;        // mouse is over the canvas
    bool  clicked = false;        // left-click released without a drag this frame
    EcadPt worldMouse;            // mouse position in ECAD-world mm
    bool  hasWorldMouse = false;  // worldMouse is valid (mouse inside canvas)
};

class EcadCanvas {
public:
    // Draw the mirror into the current ImGui window's content region. `avail` is the region size in
    // pixels (usually ImGui::GetContentRegionAvail()). Returns interaction info for the frame.
    // Call between ImGui::BeginChild()/EndChild() (the panel sets up the child + clip rect).
    EcadCanvasIO draw(const EcadMirror& doc, const ImVec2& avail);

    // Reset the camera so the whole document fits with a margin. Call on first load / "zoom to fit".
    void zoomToFit(const EcadMirror& doc, const ImVec2& avail);
    void requestFit() { wantFit_ = true; }  // defer a fit to the next draw (mirror may be empty now)

    // Center the view on a world point (mm), ensure a readable zoom, and flash a pulsing marker there
    // for a couple seconds. Used by "zoom to ERC/DRC violation" (clicking a violation row).
    void focusOn(const EcadPt& w);

    // Camera accessors (mm/px).
    float zoom() const { return pxPerMm_; }

    // Selection: the panel sets a "<kind>:<id>" token (e.g. "s:s1","fp:fp1","w:w1","t:t1")
    // before draw(); the canvas outlines that entity. Empty = nothing selected.
    void setSelection(const std::string& sel) { selected_ = sel; }
    void setHighlightedNet(const std::string& net) { highlightedNet_ = net; }

    // Transient drag-move offset (world mm) applied to the selected symbol/footprint while a
    // left-drag is in progress, for live visual feedback. The panel sets it each frame; (0,0)
    // when not dragging.
    void setDragOffset(float dx, float dy) { dragDx_ = dx; dragDy_ = dy; }

private:
    // ---- camera (affine; no 3D) ----
    // screen = origin_ + (worldX - pan_.x)*pxPerMm_,   y flips: screen.y grows downward.
    ImVec2 toScreen(const EcadPt& w) const;
    EcadPt toWorld(const ImVec2& s) const;
    void   handleNavigation(const EcadCanvasIO& io, const ImVec2& avail);

    void drawGrid(const ImVec2& avail);
    void drawSch(const EcadMirror& doc);
    void drawPcb(const EcadMirror& doc);

    ImDrawList* dl_ = nullptr;     // valid only during draw()
    ImVec2 imgMin_{0, 0};          // top-left of the canvas region in screen px
    ImVec2 imgSize_{0, 0};
    float  pxPerMm_ = 8.0f;        // zoom (pixels per mm)
    EcadPt pan_{0, 0};             // world point shown at the canvas centre
    bool   wantFit_ = true;        // fit on first draw
    bool   inited_ = false;
    std::string selected_;         // "<kind>:<id>" of the highlighted entity ("" = none)
    std::string highlightedNet_;
    float  dragDx_ = 0.0f, dragDy_ = 0.0f;  // live drag-move offset (world mm) for selected sym/fp
    EcadPt focusLoc_{0, 0};        // "zoom to violation" marker location (world mm)
    int    focusFrames_ = 0;       // frames the marker remains (counts down each draw; 0 = hidden)
};

}  // namespace ui
