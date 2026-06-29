#pragma once
#include "variant.h"
#include <functional>
#include <string>
#include <vector>

namespace aicad::ui {

enum class FieldKind {
    Dim, Combo, Check, Int, DatumRef, Selector, Text, Separator
};

struct PanelField {
    std::string id;
    std::string label;
    FieldKind kind = FieldKind::Dim;
    std::string defaultJson;
    std::vector<std::string> comboOptions;
    bool visibleWhenExpr;  // reserved: expression gating (future)
    std::string visibleWhen; // e.g. "end:blind" — simple key match for now
};

struct PanelSpec {
    std::string featureType;  // engine feature type / tool id
    std::string title;
    std::vector<PanelField> fields;
};

// Lookup a panel spec by feature/tool type. Data-driven Inspector (replaces
// hardcoded PM forms in host/src/ui/property_manager.cpp over time).
const PanelSpec* findPanelSpec(const char* featureType);
const std::vector<PanelSpec>& allPanelSpecs();

} // namespace aicad::ui
