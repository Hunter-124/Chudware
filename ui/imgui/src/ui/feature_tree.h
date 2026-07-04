// FeatureManager tree panel (C1). Ported from host/src/ui/feature_tree.h.
// Renders the left dock model tree — fixed nodes + feature history via F4 DocMirror/worker seam.
#pragma once

#include <functional>
#include <string>
#include <unordered_set>

namespace app { struct AppState; }

namespace ui {

void drawFeatureTreeBody(app::AppState& st,
                         const std::function<void(const std::string& featureId)>& editCallback = {});

void featureTreeResetTransient();
void featureClearTreeMultiSel();
void refreshFeatureHighlight(app::AppState& st);

bool featureCopySelection(app::AppState& st);
bool featureCutSelection(app::AppState& st);
bool featurePasteSelection(app::AppState& st);
bool featureDeleteSelection(app::AppState& st);
bool featureSelectAll(app::AppState& st);

bool        featureTreeOpenPath(app::AppState& st, const std::string& path, bool asSolid = false);
std::string featureTreeOpenDialog(app::AppState& st);
std::string featureTreeSaveDoc(app::AppState& st, bool saveAs);
std::string featureTreeActivePath();

// T-BL37: read-only views of the FM's datum-visibility/selection state (hidden set +
// primary selected id) for the viewport's reference-datum push.
const std::unordered_set<std::string>& featureTreeHiddenFeatures();
const std::string& featureTreeSelectedFeatureId();

void pollAiCadHandoff(app::AppState& st);

}  // namespace ui
