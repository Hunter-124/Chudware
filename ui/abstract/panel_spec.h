#pragma once
#include "variant.h"
#include <cstring>
#include <string>
#include <vector>

namespace aicad::ui {

enum class FieldKind {
    Dim, Combo, Check, Int, DatumRef, SelectorWell, FeatureWell, Text
};

// Topology kinds a selector well accepts (mirrors feature_params sel(kinds…)).
struct SelectorKinds {
    bool face = false, edge = false, vertex = false, body = false, plane = false, axis = false;
};

struct PanelField {
    const char* key = "";
    const char* label = "";
    const char* group = "";
    FieldKind kind = FieldKind::Dim;
    double dimDefault = 0.0;
    const char* unitLabel = nullptr;  // nullptr = length [mm]/[in]; "[deg]" = angle; "" = unitless
    SelectorKinds selKinds{};
    bool selMulti = true;
    const char* comboOptions = nullptr;
    const char* comboLabels = nullptr;
    const char* placeholder = nullptr;
    bool checkDefault = false;
    const char* enableKey = nullptr;
    const char* enableVals = nullptr;
    bool omitWhenDisabled = false;
    bool comingSoon = false;
};

struct PanelSpec {
    const char* featureType = "";
    const char* title = "";
    const char* icon = "";
    std::vector<PanelField> fields;
};

const PanelSpec* findPanelSpec(const char* featureType);
const PanelSpec* panelFormForCommand(const std::string& cmdId, std::string& mode);
const std::vector<PanelSpec>& allPanelSpecs();

// T-U3: look up a CAM operation parameter spec by its type string
// (e.g. "face", "pocket2d", "drill", "turn_rough"). Returns nullptr if not found.
const PanelSpec* findCamOpSpec(const char* opType);

} // namespace aicad::ui
