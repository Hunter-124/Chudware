#pragma once
#include "pipe_mesh.h"
#include "../piping_panel.h"
#include <array>

namespace ui::pipescene {
class SketchTool {
public:
    bool handle(AppState& st, PipingPanel& panel, MeshCache& mesh,
                const simscene::ViewportCtx& vp, ImDrawList* dl);
    void collect(simscene::Scene& out) const;
    bool onPlane(PipingPanel& panel, simscene::Plane plane);
    void reset();
    void tick(AppState& st, PipingPanel& panel);
private:
    enum class Snap { None, Vertex, Interior, Axis, Grid, Vertical, Start, Last };
    std::vector<Point> points_, previewPath_, previewRounded_;
    Soup ghost_;
    std::array<Soup,4> planePatch_;
    std::array<std::vector<float>,3> grid_;
    std::uint64_t ghostRevision_=1, gridRevision_=1;
    PipingPanel::Tool previousTool_=PipingPanel::Tool::None;
    bool pressed_=false, dragged_=false, doubleClick_=false, swallowRelease_=false;
    Point cursor_, numericDirection_;
    bool haveCursor_=false, numeric_=false;
    bool pendingFinish_=false;
    nlohmann::json pendingRunOps_, pendingFittingOps_;
    Snap snap_=Snap::None;
    int axis_=-1;
    std::array<char,64> number_{};
    std::size_t numberLength_=0;
    int activePlane_=0, explicitPlane_=-1, gridPlane_=-1;
    Point gridCenter_;
    float gridPitch_=0, gridExtent_=0;
    FittingGeometry previewFitting_;
    float fittingOd_=0, runOd_=0, runBend_=0;
    double runNps_=0;
    bool runShortRadius_=false;
    bool cursorWorld(PipingPanel& panel, const MeshCache& mesh,
                     const simscene::ViewportCtx& vp, Point& out);
    bool finish(AppState& st, PipingPanel& panel);
    void clearPolyline(PipingPanel* panel=nullptr);
    void updatePreview(PipingPanel& panel, const MeshCache& mesh, const Hit* fittingHit,
                       bool show);
    void updatePlane(PipingPanel& panel,const simscene::ViewportCtx& vp);
    void updateGrid(PipingPanel& panel,const simscene::ViewportCtx& vp,bool show);
    void drawHud(PipingPanel& panel,const simscene::ViewportCtx& vp,ImDrawList* dl) const;
};
} // namespace ui::pipescene
