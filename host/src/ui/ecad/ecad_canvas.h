// EcadCanvas — the pure-2D drawing and interaction surface for ECAD documents.
// ECAD world coordinates are +y up; this is the single renderer that flips them to screen +y down.
// The canvas owns only its camera and transient visual overlays. Document edits remain panel-owned.
#pragma once

#include <string>
#include <vector>

#include "imgui.h"
#include "ecad_mirror.h"

namespace ui {

struct EcadCanvasIO {
    bool   hovered = false;            // pointer is over the canvas
    bool   leftClicked = false;        // LEFT released inside, no drag this gesture
    bool   leftDoubleClicked = false;
    bool   rightClicked = false;       // RIGHT pressed (terminate route / context menu)
    bool   middleClicked = false;
    bool   leftDragStarted = false;    // first frame of a left drag
    bool   leftDragging = false;
    bool   leftDragEnded = false;      // release frame of a left drag
    EcadPt dragStartWorld;             // world mm where the left drag began
    EcadPt worldMouse;                 // pointer in ECAD-world mm
    bool   hasWorldMouse = false;
    bool   ctrl = false, shift = false, alt = false;
};

class EcadCanvas {
public:
    EcadCanvasIO draw(const EcadMirror& doc, const ImVec2& avail);
    void zoomToFit(const EcadMirror& doc, const ImVec2& avail);
    void requestFit();
    void focusOn(const EcadPt& w);
    void focusOn(const EcadPt& w, bool isError);
    float zoom() const;
    void setSelection(const std::vector<std::string>& tokens);
    void setHighlightedNet(const std::string& net);
    void setMarquee(bool active, const EcadPt& a, const EcadPt& b);
    void setRoutePreview(const std::vector<EcadPt>& pts, bool bus);
    void setSnapTarget(bool active, const EcadPt& w, const std::string& label);
    void setDragOffset(float dx, float dy);
    void setShowErcMarkers(bool on);
    void setShowDrcMarkers(bool on);
    void setShowSheetFrame(bool on);

private:
    // Pan with middle/right drag; left drag is reserved exclusively for schematic editing.
    ImVec2 toScreen(const EcadPt& w) const;
    EcadPt toWorld(const ImVec2& s) const;
    void handleNavigation(const EcadCanvasIO& io, const ImVec2& avail);
    void drawGrid(const EcadMirror& doc, const ImVec2& avail);
    void drawSch(const EcadMirror& doc);
    void drawPcb(const EcadMirror& doc);
    void drawOverlays(const EcadMirror& doc);
    bool selected(const char* kind, const std::string& id) const;
    bool sheetFrameGeometry(const EcadMirror& doc, const EcadSheet*& page,
                            EcadPt& outMin, EcadPt& outMax, float& pageScale) const;
    bool selected(const char* kind, const std::string& first,
                  const std::string& second) const;

    ImDrawList* dl_ = nullptr;
    ImVec2 imgMin_{0, 0};
    ImVec2 imgSize_{0, 0};
    float pxPerMm_ = 8.0f;
    EcadPt pan_{0, 0};
    bool wantFit_ = true;
    bool inited_ = false;

    std::vector<std::string> selected_;
    std::string highlightedNet_;
    float dragDx_ = 0.0f, dragDy_ = 0.0f;

    bool marqueeActive_ = false;
    EcadPt marqueeA_{0, 0}, marqueeB_{0, 0};
    std::vector<EcadPt> routePreview_;
    bool routePreviewBus_ = false;
    bool snapTargetActive_ = false;
    EcadPt snapTarget_{0, 0};
    std::string snapTargetLabel_;
    bool showErcMarkers_ = true;
    bool showDrcMarkers_ = true;
    // Reused screen-space scratch keeps labels and violation badges clear of pads.
    struct OverlayRect { ImVec2 min, max; };
    std::vector<OverlayRect> pcbOverlayKeepouts_;
    std::vector<ImVec2> drcAnchorPts_;
    std::vector<ImVec2> drcMarkerPts_;
    bool showSheetFrame_ = false;

    EcadPt focusLoc_{0, 0};
    bool focusIsError_ = true;
    int focusFrames_ = 0;

    bool leftGestureActive_ = false;
    bool leftGestureDragged_ = false;
    EcadPt leftDragStartWorld_{0, 0};
};

}  // namespace ui
