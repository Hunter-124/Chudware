#include "panel_spec.h"

namespace aicad::ui {

static const PanelSpec kSpecs[] = {
    {
        "extrude",
        "Extrude",
        {
            {"depth", "Depth", FieldKind::Dim, "25.0"},
            {"end", "End Condition", FieldKind::Combo, "blind", {"blind", "through_all", "up_to_next", "up_to_surface"}},
            {"draft", "Draft Angle", FieldKind::Dim, "0.0"},
            {"merge", "Merge Result", FieldKind::Check, "true"},
        },
    },
    {
        "revolve",
        "Revolve",
        {
            {"angle", "Angle", FieldKind::Dim, "360.0"},
            {"merge", "Merge Result", FieldKind::Check, "true"},
        },
    },
    {
        "fillet",
        "Fillet",
        {
            {"radius", "Radius", FieldKind::Dim, "2.0"},
        },
    },
    {
        "box",
        "Box",
        {
            {"length", "Length", FieldKind::Dim, "100.0"},
            {"width", "Width", FieldKind::Dim, "50.0"},
            {"height", "Height", FieldKind::Dim, "25.0"},
        },
    },
};

const PanelSpec* findPanelSpec(const char* featureType) {
    if (!featureType) return nullptr;
    for (const auto& s : kSpecs)
        if (s.featureType == featureType) return &s;
    return nullptr;
}

const std::vector<PanelSpec>& allPanelSpecs() {
    static const std::vector<PanelSpec> v(std::begin(kSpecs), std::end(kSpecs));
    return v;
}

} // namespace aicad::ui
