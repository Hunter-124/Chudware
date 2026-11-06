// Read-only 2D preview. ECAD coordinates are +y up and converted to screen +y down here.
#pragma once

#include <string>
#include <vector>

#include "imgui.h"
#include "ecad_mirror.h"

namespace ui {

struct EcadCanvasIO {
    bool hovered = false;
    bool leftClicked = false;
    EcadPt worldMouse;
    bool hasWorldMouse = false;
    bool ctrl = false, shift = false;
};

class EcadCanvas {
public:
    EcadCanvasIO draw(const EcadMirror& doc, const std::vector<EcadViolation>& checks,
                      const ImVec2& avail);
    void zoomToFit(const EcadMirror& doc, const ImVec2& avail);
    void requestFit();
    void focusOn(const EcadPt& w);
    void focusOn(const EcadPt& w, bool isError);
    float zoom() const;
    void setSelection(const std::vector<std::string>& tokens);
    void setHighlightedNet(const std::string& net);
    void setShowSheetFrame(bool on);

private:
    // Pan with middle/right drag; left clicks select for inspection.
    ImVec2 toScreen(const EcadPt& w) const;
    EcadPt toWorld(const ImVec2& s) const;
    void handleNavigation(const EcadCanvasIO& io, const ImVec2& avail);
    void drawGrid(const EcadMirror& doc, const ImVec2& avail);
    void drawSch(const EcadMirror& doc);
    void drawPcb(const EcadMirror& doc);
    void drawOverlays(const EcadMirror& doc, const std::vector<EcadViolation>& checks);
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
    // Reused screen-space scratch keeps labels and violation badges clear of pads.
    struct OverlayRect { ImVec2 min, max; };
    std::vector<OverlayRect> pcbOverlayKeepouts_;
    std::vector<ImVec2> drcAnchorPts_;
    std::vector<ImVec2> drcMarkerPts_;
    bool showSheetFrame_ = false;

    EcadPt focusLoc_{0, 0};
    bool focusIsError_ = true;
    int focusFrames_ = 0;
};

}  // namespace ui
