#include "sim_scene.h"
#include "pipe_mesh.h"
#include "pipe_sketch.h"
#include "../../core/app_state.h"
#include "../sim_manager.h"

namespace ui::pipescene {
namespace {
struct State {
    PipingPanel* panel=nullptr;
    MeshCache mesh;
    SketchTool sketch;
};
State& state(PipingPanel& panel) {
    static State s;
    if(s.panel!=&panel) {
        s.panel=&panel;
        s.sketch.reset();
        // Keep vector capacities, but fence document switches even when revision numbers match.
        s.mesh.owner=nullptr;
    }
    s.mesh.update(panel);
    return s;
}
PipingPanel* openPanel(AppState& st) {
    if(st.activeWorkspace!=AppState::Workspace::Simulate || !st.simManager) return nullptr;
    auto& p=st.simManager->piping(); return p.isOpen()?&p:nullptr;
}
}
bool active(AppState& st) {
    auto* p=openPanel(st);
    return p && (p->network().is_object() || p->tool()!=PipingPanel::Tool::None);
}
void collect(AppState& st,simscene::Scene& out) {
    auto* panel=openPanel(st); if(!panel) return;
    auto& s=state(*panel);
    s.sketch.tick(st,*panel);
    s.mesh.collect(out);
    if(panel->tool()!=PipingPanel::Tool::None) s.sketch.collect(out);
}
bool toolArmed(AppState& st) {
    auto* p=openPanel(st); return p && p->tool()!=PipingPanel::Tool::None;
}
bool handleViewport(AppState& st,const simscene::ViewportCtx& vp,ImDrawList* dl) {
    auto* panel=openPanel(st); if(!panel) return false;
    auto& s=state(*panel);
    return s.sketch.handle(st,*panel,s.mesh,vp,dl);
}
bool onPlaneChip(AppState& st,simscene::Plane plane) {
    auto* panel=openPanel(st); if(!panel || panel->tool()==PipingPanel::Tool::None) return false;
    return state(*panel).sketch.onPlane(*panel,plane);
}
} // namespace ui::pipescene
