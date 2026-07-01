// F4 worker/state seam for sketch mode — bridges app::AppState + F1 Renderer to the host-shaped
// API the ported sketch_mode expects. Owned write-set: ui/imgui/src/ui/sketch/** only.
#pragma once

#include <nlohmann/json.hpp>
#include <string>
#include <unordered_set>
#include <vector>

#include "app/app_state.h"
#include "pick_hit.h"
#include "renderer.h"
#include "async_rpc.h"
#include "render/id_pass.h"

namespace ui::sketch {

enum class ToastLevel { Info, Warn, Error };

struct SketchClipboard {
    enum class Kind { None, SketchEntities };
    Kind kind = Kind::None;
    std::string srcDocId;
    nlohmann::json payload;
    float copyOriginU = 0.f, copyOriginV = 0.f;
    void clear() { kind = Kind::None; payload = nullptr; }
};

struct SketchHost;

// Property-manager hooks the full port expects; nullptr until C2/C3 panels land.
struct SketchPmSeam {
    bool active() const { return false; }
    bool activeWellWantsPlane() const { return false; }
    bool activeWellWantsAxis() const { return false; }
    bool activeWellWantsPathFeature() const { return false; }
    std::string previewFeatureId() const { return {}; }
    std::vector<std::string> pathHighlightFeatureIds() const { return {}; }
    std::vector<std::string> regionCandidateSketches(SketchHost& st) const;
    bool wantsRegionPicker() const { return false; }
    bool sectionsAreOrdered() const { return false; }
    std::vector<nlohmann::json> profilePicks() const { return {}; }
    void setProfilePicks(SketchHost& st, const std::vector<nlohmann::json>& sels) const;
    bool routeDatumPlane(const std::string& datumId, const std::string& label = "") const;
    bool routeDatumAxis(const std::string& fid, const std::string& name) const;
    bool routePathFeature(const std::string& fid, const std::string& name, bool isCurve) const;
};

enum class CursorKind { Default, Crosshair, Hand };

// Per-session sketch host context (camera + pick + clipboard live here until AppState grows them).
struct SketchHost {
    app::AppState& app;
    Renderer* renderer = nullptr;
    SketchPmSeam* propertyManager = nullptr;

    // camera (mirrors host AppState yaw/pitch/zoom/pan during sketch)
    float yaw = 35.f, pitch = 30.f, zoom = 1.f;
    float pan[3] = {0, 0, 0};

    worker::PickHit hover;
    std::string selectedFeatureId;
    bool sketchPlanePickActive = false;

    bool showPlane[3] = {true, true, true};
    int planeEmphasis = -1;
    bool treeScrollToSelection = false;
    bool hasDocMesh = false;
    render::IdPass* idPass = nullptr;

    std::unordered_set<std::string> hiddenFeatures;

    void selectionClear();

    SketchClipboard clipboard;

    core::DocMirror& docMirror() { return app.docMirror; }
    const core::DocMirror& docMirror() const { return app.docMirror; }
    WorkerClient& worker() { return app.worker; }
    bool workerStarted() const { return app.workerStarted; }
    core::AsyncRpc& rpc() { return app.rpc; }
    const core::AsyncRpc& rpc() const { return app.rpc; }

    bool ips() const { return false; }  // MMGS until units land on AppState

    void toast(ToastLevel lvl, const std::string& msg);
    void addLog(const std::string& msg);
    void loadDocMesh(const std::string& aicmPath, std::string& loadErr);
    void clearViewportForNewDoc();
    void requestCursor(CursorKind kind);
    void docUndo();
    void docRedo();

    // Selection items for enterNewOnSelection (face picks from core::Selection when wired).
    struct SelItem { std::string kind; nlohmann::json ref; std::string selector; };
    std::vector<SelItem> selectionItems() const;
};

void requestCameraAnimation(SketchHost& st, float yaw, float pitch, float zoom, const float pan[3]);

SketchHost makeHost(app::AppState& app, Renderer* renderer);

}  // namespace ui::sketch
