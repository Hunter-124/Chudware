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

const MaterialPreset kMaterials[kMaterialsCount] = {
    {"Plastic",    0.00f, 0.50f, {0.71f, 0.76f, 0.82f}},
    {"Aluminum",   0.90f, 0.35f, {0.83f, 0.85f, 0.88f}},  // silver
    {"Steel",      0.85f, 0.25f, {0.56f, 0.58f, 0.62f}},  // darker
    {"Brass",      0.95f, 0.30f, {0.80f, 0.62f, 0.28f}},  // warm
    {"Dark Metal", 0.90f, 0.45f, {0.16f, 0.17f, 0.19f}},  // near-black
};

const PhysicalMaterial kPhysicalMaterials[kPhysicalMaterialsCount] = {
    // engine key            menu label              metal  rough  display rgb
    {"alu_6061",            "Aluminum 6061-T6",     0.90f, 0.35f, {0.83f, 0.85f, 0.88f}},
    {"steel_1018",          "Steel 1018",           0.85f, 0.25f, {0.56f, 0.58f, 0.62f}},
    {"steel_304_stainless", "Stainless Steel 304",  0.88f, 0.20f, {0.72f, 0.74f, 0.77f}},
    {"ti_6al4v",            "Titanium Ti-6Al-4V",   0.80f, 0.45f, {0.42f, 0.43f, 0.46f}},
    {"brass_c360",          "Brass C360",           0.95f, 0.30f, {0.80f, 0.62f, 0.28f}},
    {"copper_c110",         "Copper C110",          0.95f, 0.32f, {0.72f, 0.43f, 0.27f}},
    {"cast_iron_gray",      "Cast Iron (Gray)",     0.65f, 0.55f, {0.24f, 0.25f, 0.27f}},
    {"abs",                 "ABS Plastic",          0.00f, 0.50f, {0.30f, 0.32f, 0.36f}},
    {"pla",                 "PLA",                  0.00f, 0.55f, {0.88f, 0.87f, 0.83f}},
};

bool materialAppearance(const std::string& key, float outColor[3],
                        float& outMetallic, float& outRoughness) {
    auto emit = [&](float m, float r, const float rgb[3]) {
        outColor[0] = rgb[0]; outColor[1] = rgb[1]; outColor[2] = rgb[2];
        outMetallic = m; outRoughness = r;
    };
    // 1) Exact curated match — the menu's own choices land here.
    for (int i = 0; i < kPhysicalMaterialsCount; ++i)
        if (key == kPhysicalMaterials[i].key) {
            emit(kPhysicalMaterials[i].metallic, kPhysicalMaterials[i].roughness,
                 kPhysicalMaterials[i].rgb);
            return true;
        }
    // 2) Category heuristic for any other engine/custom key -> a kMaterials preset (indices:
    //    0 Plastic, 1 Aluminum, 2 Steel, 3 Brass, 4 Dark Metal). Keys are lowercase in materials.json.
    auto has = [&](const char* s) { return key.find(s) != std::string::npos; };
    int p = 0;  // Plastic (dielectric) default — also covers polymers/glass/concrete/carbon.
    if (has("brass") || has("copper")) p = 3;
    else if (has("alu")) p = 1;
    else if (has("steel") || has("stainless") || has("iron") || has("inconel") || has("tungsten")) p = 2;
    else if (has("ti_") || has("titanium") || has("magnesium") || has("lead")) p = 4;
    emit(kMaterials[p].metallic, kMaterials[p].roughness, kMaterials[p].rgb);
    return true;
}

bool appearanceColorPicker(const char* popupId, float outColor[3],
                           float* outMetallic, float* outRoughness) {
    // C38 (A1): per-popup-id seed state. Three callers share this function with distinct popup ids;
    // a single shared s_pick/s_seeded corrupted the seed when two pickers were armed the same frame
    // (the second BeginPopup would reuse the first's half-seeded buffer). Key it by popupId so each
    // picker keeps its own working color + seeded flag. Map is tiny (one entry per popup id).
    struct PickState { std::array<float, 3> pick{0.71f, 0.76f, 0.82f}; float metal = 0.5f, rough = 0.5f; bool seeded = false; };
    static std::unordered_map<std::string, PickState> s_states;
    PickState& ps = s_states[popupId];
    float* s_pick = ps.pick.data();
    bool committed = false;
    if (ImGui::BeginPopup(popupId)) {
        // Seed the picker from the caller's current color once per open.
        if (!ps.seeded) { s_pick[0] = outColor[0]; s_pick[1] = outColor[1]; s_pick[2] = outColor[2];
                          if (outMetallic)  ps.metal = *outMetallic;
                          if (outRoughness) ps.rough = *outRoughness;
                          ps.seeded = true; }
        ImGui::TextUnformatted("Appearance");
        ImGui::Separator();
        // C32-A: RealView material presets (above the color swatches). Click = apply color+material.
        ImGui::TextUnformatted("Material");
        // C32-A: each material preset shows its characteristic color as a swatch; clicking it
        // applies that color plus the preset's metalness/roughness finish.
        const float sw = 22.0f * ui::uiScale;
        for (int i = 0; i < kMaterialsCount; ++i) {
            if (i) ImGui::SameLine(0, 6.0f * ui::uiScale);
            ImVec4 mc(kMaterials[i].rgb[0], kMaterials[i].rgb[1], kMaterials[i].rgb[2], 1.0f);
            if (ImGui::ColorButton(("##aprm" + std::to_string(i)).c_str(), mc,
                                   ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                                   ImVec2(sw, sw))) {
                outColor[0] = kMaterials[i].rgb[0];
                outColor[1] = kMaterials[i].rgb[1];
                outColor[2] = kMaterials[i].rgb[2];
                if (outMetallic)  *outMetallic = kMaterials[i].metallic;
                if (outRoughness) *outRoughness = kMaterials[i].roughness;
                committed = true;
                ImGui::CloseCurrentPopup();
            }
            ImGui::SameLine(0, 2.0f * ui::uiScale);
            ImGui::TextUnformatted(kMaterials[i].name);
        }
        ImGui::Separator();
        // Preset swatches (greys, primaries, plus common engineering colors). Click = apply
        // immediately. Sixteen entries rendered in two rows of eight.
        static const float kPresets[16][3] = {
            {0.71f, 0.76f, 0.82f},  // default grey-blue
            {0.50f, 0.50f, 0.52f},  // mid grey
            {0.20f, 0.21f, 0.23f},  // dark grey
            {0.85f, 0.85f, 0.86f},  // light grey
            {0.80f, 0.15f, 0.15f},  // red
            {0.15f, 0.35f, 0.80f},  // blue
            {0.18f, 0.60f, 0.25f},  // green
            {0.92f, 0.80f, 0.12f},  // yellow
            {0.90f, 0.50f, 0.10f},  // orange
            {0.55f, 0.20f, 0.75f},  // purple
            {0.10f, 0.75f, 0.85f},  // cyan
            {0.55f, 0.35f, 0.18f},  // brown
            {0.08f, 0.18f, 0.55f},  // navy
            {0.45f, 0.80f, 0.15f},  // lime
            {0.95f, 0.92f, 0.85f},  // cream
            {0.08f, 0.08f, 0.10f},  // near-black
        };
        for (int i = 0; i < 16; ++i) {
            if (i && i != 8) ImGui::SameLine(0, 4.0f * ui::uiScale);
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
        // C-overhaul: metalness + roughness sliders let a CUSTOM color carry any PBR finish (not just
        // the finish inherited from the last named material). Captured by Apply below.
        if (outMetallic && outRoughness) {
            ImGui::Separator();
            ImGui::SetNextItemWidth(180.0f * ui::uiScale);
            ImGui::SliderFloat("##metal", &ps.metal, 0.0f, 1.0f, "Metalness %.2f");
            ImGui::SetNextItemWidth(180.0f * ui::uiScale);
            ImGui::SliderFloat("##rough", &ps.rough, 0.0f, 1.0f, "Roughness %.2f");
        }
        if (ImGui::Button("Apply", ImVec2(80.0f * ui::uiScale, 0))) {
            outColor[0] = s_pick[0]; outColor[1] = s_pick[1]; outColor[2] = s_pick[2];
            if (outMetallic)  *outMetallic = ps.metal;
            if (outRoughness) *outRoughness = ps.rough;
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
