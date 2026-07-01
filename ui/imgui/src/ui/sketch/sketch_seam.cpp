#include "sketch_seam.h"

#include "sketch_internal.h"
#include "sketch_palette.h"
#include "ui/cursors.h"
#include "viewport/scene.h"
#include "theme/theme.h"
#include "ui/command_registry.h"
#include "action_bus.h"

namespace ui::sketch {

static SketchPmSeam kNullPm;

SketchHost makeHost(app::AppState& app, Renderer* renderer) {
    SketchHost h{app, renderer, &kNullPm};
    sketchPalette().scale = theme::S();
    if (app.scene) {
        // Scene owns orbit state; sketch enter/exit will read/write via scene when wired in G2.
    }
    h.sketchPlanePickActive = app.inSketch;
    return h;
}

void SketchHost::toast(ToastLevel, const std::string& msg) {
    app.status = msg;
}

void SketchHost::addLog(const std::string& msg) {
    (void)msg;
}

void SketchHost::loadDocMesh(const std::string& aicmPath, std::string& loadErr) {
    loadErr.clear();
    if (!app.scene || aicmPath.empty()) return;
    if (!app.scene->loadAicm(aicmPath))
        loadErr = app.scene->lastError;
    else
        app.viewMeshRev = app.engine.revision();
}

void SketchHost::clearViewportForNewDoc() {
    if (app.scene) app.scene->clearMesh();
    app.viewMeshRev = -1;
}

void SketchHost::requestCursor(CursorKind kind) {
    switch (kind) {
        case CursorKind::Crosshair: ui::requestCursor(ui::CursorKind::Crosshair); break;
        case CursorKind::Hand:      ui::requestCursor(ui::CursorKind::Hand); break;
        default: break;
    }
}

void SketchHost::docUndo() {
    aicad::ui::Action a;
    a.cmdId = "edit_undo";
    a.source = aicad::ui::ActionSource::Shortcut;
    if (app.actionBus.submit(a).ok) app.onDocMutation();
}

void SketchHost::docRedo() {
    aicad::ui::Action a;
    a.cmdId = "edit_redo";
    a.source = aicad::ui::ActionSource::Shortcut;
    if (app.actionBus.submit(a).ok) app.onDocMutation();
}

void SketchHost::selectionClear() {
    app.selection.clear();
}

void requestCameraAnimation(SketchHost& st, float yaw, float pitch, float zoom, const float pan[3]) {
    st.yaw = yaw;
    st.pitch = pitch;
    st.zoom = zoom;
    st.pan[0] = pan[0];
    st.pan[1] = pan[1];
    st.pan[2] = pan[2];
}

std::vector<SketchHost::SelItem> SketchHost::selectionItems() const {
    return {};
}

std::vector<std::string> SketchPmSeam::regionCandidateSketches(SketchHost& st) const {
    (void)st;
    return {};
}

void SketchPmSeam::setProfilePicks(SketchHost& st, const std::vector<nlohmann::json>& sels) const {
    (void)st; (void)sels;
}

bool SketchPmSeam::routeDatumPlane(const std::string& datumId, const std::string& label) const {
    (void)datumId; (void)label;
    return false;
}

bool SketchPmSeam::routeDatumAxis(const std::string& fid, const std::string& name) const {
    (void)fid; (void)name;
    return false;
}

bool SketchPmSeam::routePathFeature(const std::string& fid, const std::string& name, bool isCurve) const {
    (void)fid; (void)name; (void)isCurve;
    return false;
}

}  // namespace ui::sketch
