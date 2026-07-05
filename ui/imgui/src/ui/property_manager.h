// Data-driven Property Manager — renders aicad::ui::PanelSpec forms (C2 port).
#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>
#include "panel_spec.h"
#include "app/app_state.h"

namespace ui {

bool pmParseDim(const std::string& text, double& outMm, std::string& err, bool angle = false,
                double bareLengthScale = 1.0);
std::string pmFormatDim(double mm, bool ips, bool angle = false);

struct PMSelEntry {
    nlohmann::json selector;
    std::string label;
    uint32_t pickBody = 0;
    uint32_t pickTopo = 0;
    int      pickKind = -1;
};

struct PMField {
    const aicad::ui::PanelField* def = nullptr;
    std::vector<char> dimBuf{64, 0};
    double dimValue = 0.0;
    std::string dimExpr;
    std::string dimError;
    std::vector<PMSelEntry> picks;
    bool wellActive = false;
    bool wellDangling = false;
    int comboIndex = 0;
    bool checkValue = false;
    int intValue = 1;
    std::vector<char> textBuf{std::vector<char>(128, '\0')};
};

class PropertyManager {
public:
    bool active() const { return active_; }
    std::string activeFormType() const;
    const std::string& targetId() const { return targetId_; }
    bool hasPreviewTarget() const { return active_ && !targetId_.empty(); }
    bool isEditMode() const { return active_ && editMode_; }
    std::string previewFeatureId() const;

    bool openForCommand(app::AppState& st, const std::string& cmdId);
    bool openForEdit(app::AppState& st, const std::string& featureId);
    void presetFieldForShot(const std::string& key, const std::string& value);
    bool dragSetDim(const std::string& key, double value);

    void drawBody(app::AppState& st);
    void tick(app::AppState& st);
    void closeIfActive();

    bool routePick(const nlohmann::json& selector, int pickKind,
                   uint32_t pickBody = 0, uint32_t pickTopo = 0);

    struct EdgeSeg {
        std::vector<std::array<float, 3>> pts;
        bool closed = false;
        int  pickIndex = -1;
    };
    void collectActiveWellHighlights(std::vector<std::array<uint32_t, 3>>& out) const;
    void collectActiveWellEdgeSegments(std::vector<EdgeSeg>& out) const;
    bool removeActiveWellPick(int pickIndex);

    bool routeDatumPlane(const std::string& datumId, const std::string& label = "");
    bool routeDatumAxis(const std::string& fid, const std::string& label);
    // T-BL37: route origin/point-datum into a SelectorWell that has selKinds.point=true.
    bool routeDatumPoint(const std::string& fid, const std::string& label = "");
    bool routePathFeature(const std::string& fid, const std::string& name, bool isCurve);

    bool activeWellWantsPlane() const;
    bool activeWellWantsAxis() const;
    bool activeWellWantsPoint() const;  // T-BL37
    bool activeWellWantsPathFeature() const;
    bool activeWellWantsBody() const;
    bool activeWellPrefersEdge() const;
    bool activeWellExpandsFaceToEdges() const;
    bool wantsRegionPicker() const;
    bool sectionsAreOrdered() const;
    std::vector<nlohmann::json> profilePicks() const;
    void setProfilePicks(app::AppState& st, const std::vector<nlohmann::json>& sels);
    std::vector<std::string> regionCandidateSketches(app::AppState& st) const;
    std::vector<std::string> pathHighlightFeatureIds() const;
    std::string activeWellKindHint() const;

    bool cancelEdits(app::AppState& st);

private:
    void close();
    void commit(app::AppState& st);
    void buildOpFeature(nlohmann::json& feature) const;
    bool applyPreview(app::AppState& st);
    void loadFromTree(app::AppState& st, const nlohmann::json& feature);
    PMField* activeWell();
    PMField* profileWell();
    const PMField* profileWell() const;
    void advanceWellAfterFill(PMField* filled);
    void reformatDimDisplays();
    bool ips(const app::AppState& st) const { return st.units == "in"; }

    bool active_ = false;
    bool editMode_ = false;
    const aicad::ui::PanelSpec* form_ = nullptr;
    std::string mode_;
    std::string title_;
    std::string targetId_;
    std::vector<PMField> fields_;
    bool ips_ = false;
    bool dirty_ = false;
    double lastEditTime_ = 0.0;
    bool firstPreviewDone_ = false;
    int previewBatches_ = 0;
    std::string checkpointId_;
    std::string previewError_;
    bool wantFocusFirstField_ = false;
    bool pmWidgetActivePrevFrame_ = false;
    bool previewUndoGroupStarted_ = false;
    bool focusedOnOpen_ = false;
    std::string previewWarning_;
    bool committing_ = false;
};

PropertyManager& propertyManager();
void pmApplyShotEnv(app::AppState& st);

}  // namespace ui
