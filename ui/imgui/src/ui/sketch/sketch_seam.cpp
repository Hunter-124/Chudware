#include "sketch_seam.h"

#include "sketch_internal.h"
#include "sketch_palette.h"
#include "ui/cursors.h"
#include "ui/property_manager.h"
#include "viewport/scene.h"
#include "theme/theme.h"
#include "ui/command_registry.h"
#include "action_bus.h"

namespace ui::sketch {

static SketchPmSeam kPmSeam;

bool SketchPmSeam::active() const { return pm && pm->active(); }
bool SketchPmSeam::activeWellWantsPlane() const { return pm && pm->activeWellWantsPlane(); }
bool SketchPmSeam::activeWellWantsAxis() const { return pm && pm->activeWellWantsAxis(); }
bool SketchPmSeam::activeWellWantsPathFeature() const { return pm && pm->activeWellWantsPathFeature(); }
std::string SketchPmSeam::previewFeatureId() const { return pm ? pm->previewFeatureId() : std::string(); }
std::vector<std::string> SketchPmSeam::pathHighlightFeatureIds() const {
    return pm ? pm->pathHighlightFeatureIds() : std::vector<std::string>{};
}
std::vector<std::string> SketchPmSeam::spineFeatureIds() const {
    return pm ? pm->spineFeatureIds() : std::vector<std::string>{};
}
std::string SketchPmSeam::activeWellKey() const {
    return pm ? pm->activeWellKey() : std::string();
}
bool SketchPmSeam::wantsRegionPicker() const { return pm && pm->wantsRegionPicker(); }
bool SketchPmSeam::sectionsAreOrdered() const { return pm && pm->sectionsAreOrdered(); }
std::vector<nlohmann::json> SketchPmSeam::profilePicks() const {
    return pm ? pm->profilePicks() : std::vector<nlohmann::json>{};
}

SketchHost makeHost(app::AppState& app, Renderer* renderer) {
    kPmSeam.pm = &ui::propertyManager();
    SketchHost h{app, renderer, &kPmSeam};
    sketchPalette().scale = theme::S();
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
    return pm ? pm->regionCandidateSketches(st.app) : std::vector<std::string>{};
}

void SketchPmSeam::setProfilePicks(SketchHost& st, const std::vector<nlohmann::json>& sels) const {
    if (pm) pm->setProfilePicks(st.app, sels);
}

bool SketchPmSeam::routeDatumPlane(const std::string& datumId, const std::string& label) const {
    return pm && pm->routeDatumPlane(datumId, label);
}

bool SketchPmSeam::routeDatumAxis(const std::string& fid, const std::string& name) const {
    return pm && pm->routeDatumAxis(fid, name);
}

bool SketchPmSeam::routePathFeature(const std::string& fid, const std::string& name, bool isCurve) const {
    return pm && pm->routePathFeature(fid, name, isCurve);
}

}  // namespace ui::sketch
