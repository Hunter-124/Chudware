// Shared appearance / material picker (W1-B item 9). See appearance_common.h.
//
// Extracted verbatim from the formerly-duplicated copies in feature_tree.cpp and viewport_panel.cpp
// so there is one source of truth for the preset table + the picker popup.
#include "appearance_common.h"

#include <array>
#include <string>
#include <unordered_map>

#include "imgui.h"

#include "../ui_theme.h"

namespace ui {

const MaterialPreset kMaterials[5] = {
    {"Plastic",    0.00f, 0.50f, {0.71f, 0.76f, 0.82f}},
    {"Aluminum",   0.90f, 0.35f, {0.83f, 0.85f, 0.88f}},  // silver
    {"Steel",      0.85f, 0.25f, {0.56f, 0.58f, 0.62f}},  // darker
    {"Brass",      0.95f, 0.30f, {0.80f, 0.62f, 0.28f}},  // warm
    {"Dark Metal", 0.90f, 0.45f, {0.16f, 0.17f, 0.19f}},  // near-black
};

bool appearanceColorPicker(const char* popupId, float outColor[3],
                           float* outMetallic, float* outRoughness) {
    // C38 (A1): per-popup-id seed state. Three callers share this function with distinct popup ids;
    // a single shared s_pick/s_seeded corrupted the seed when two pickers were armed the same frame
    // (the second BeginPopup would reuse the first's half-seeded buffer). Key it by popupId so each
    // picker keeps its own working color + seeded flag. Map is tiny (one entry per popup id).
    struct PickState { std::array<float, 3> pick{0.71f, 0.76f, 0.82f}; bool seeded = false; };
    static std::unordered_map<std::string, PickState> s_states;
    PickState& ps = s_states[popupId];
    float* s_pick = ps.pick.data();
    bool committed = false;
    if (ImGui::BeginPopup(popupId)) {
        // Seed the picker from the caller's current color once per open.
        if (!ps.seeded) { s_pick[0] = outColor[0]; s_pick[1] = outColor[1]; s_pick[2] = outColor[2];
                          ps.seeded = true; }
        ImGui::TextUnformatted("Appearance");
        ImGui::Separator();
        // C32-A: RealView material presets (above the color swatches). Click = apply color+material.
        ImGui::TextUnformatted("Material");
        for (int i = 0; i < 5; ++i) {
            if (i) ImGui::SameLine(0, 4.0f * ui::uiScale);
            if (ImGui::SmallButton(kMaterials[i].name)) {
                outColor[0] = kMaterials[i].rgb[0];
                outColor[1] = kMaterials[i].rgb[1];
                outColor[2] = kMaterials[i].rgb[2];
                if (outMetallic)  *outMetallic = kMaterials[i].metallic;
                if (outRoughness) *outRoughness = kMaterials[i].roughness;
                committed = true;
                ImGui::CloseCurrentPopup();
            }
        }
        ImGui::Separator();
        // Preset swatches (SolidWorks-ish: greys, primaries). Click = apply immediately.
        static const float kPresets[8][3] = {
            {0.71f, 0.76f, 0.82f},  // default grey-blue
            {0.50f, 0.50f, 0.52f},  // mid grey
            {0.20f, 0.21f, 0.23f},  // dark grey
            {0.85f, 0.85f, 0.86f},  // light grey
            {0.80f, 0.15f, 0.15f},  // red
            {0.15f, 0.35f, 0.80f},  // blue
            {0.18f, 0.60f, 0.25f},  // green
            {0.92f, 0.80f, 0.12f},  // yellow
        };
        float sw = 22.0f * ui::uiScale;
        for (int i = 0; i < 8; ++i) {
            if (i) ImGui::SameLine(0, 4.0f * ui::uiScale);
            ImVec4 c(kPresets[i][0], kPresets[i][1], kPresets[i][2], 1.0f);
            if (ImGui::ColorButton(("##aprs" + std::to_string(i)).c_str(), c,
                                   ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                                   ImVec2(sw, sw))) {
                outColor[0] = kPresets[i][0];
                outColor[1] = kPresets[i][1];
                outColor[2] = kPresets[i][2];
                committed = true;
                ImGui::CloseCurrentPopup();
            }
        }
        ImGui::Separator();
        ImGui::SetNextItemWidth(180.0f * ui::uiScale);
        ImGui::ColorPicker3("##appk", s_pick,
                            ImGuiColorEditFlags_NoSidePreview | ImGuiColorEditFlags_NoSmallPreview);
        if (ImGui::Button("Apply", ImVec2(80.0f * ui::uiScale, 0))) {
            outColor[0] = s_pick[0]; outColor[1] = s_pick[1]; outColor[2] = s_pick[2];
            committed = true;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(80.0f * ui::uiScale, 0))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    } else {
        ps.seeded = false;  // re-seed next time the popup opens (C38 A1: per-id flag)
    }
    return committed;
}

}  // namespace ui
