// Shared appearance / material picker + C5 panel seam — ported from host/src/ui/appearance_common.cpp.
#include "ui/appearance_common.h"

#include <array>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <string>
#include <unordered_map>

#include "imgui.h"

#include "app/app_state.h"
#include "viewport/scene.h"
#include "doc_tabs.h"
#include "renderer.h"
#include "theme/icons.h"

namespace ui {

const MaterialPreset kMaterials[kMaterialsCount] = {
    {"Plastic",    0.00f, 0.50f, {0.71f, 0.76f, 0.82f}},
    {"Aluminum",   0.90f, 0.35f, {0.83f, 0.85f, 0.88f}},
    {"Steel",      0.85f, 0.25f, {0.56f, 0.58f, 0.62f}},
    {"Brass",      0.95f, 0.30f, {0.80f, 0.62f, 0.28f}},
    {"Dark Metal", 0.90f, 0.45f, {0.16f, 0.17f, 0.19f}},
};

const PhysicalMaterial kPhysicalMaterials[kPhysicalMaterialsCount] = {
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
    for (int i = 0; i < kPhysicalMaterialsCount; ++i)
        if (key == kPhysicalMaterials[i].key) {
            emit(kPhysicalMaterials[i].metallic, kPhysicalMaterials[i].roughness,
                 kPhysicalMaterials[i].rgb);
            return true;
        }
    auto has = [&](const char* s) { return key.find(s) != std::string::npos; };
    int p = 0;
    if (has("brass") || has("copper")) p = 3;
    else if (has("alu")) p = 1;
    else if (has("steel") || has("stainless") || has("iron") || has("inconel") || has("tungsten")) p = 2;
    else if (has("ti_") || has("titanium") || has("magnesium") || has("lead")) p = 4;
    emit(kMaterials[p].metallic, kMaterials[p].roughness, kMaterials[p].rgb);
    return true;
}

bool appearanceColorPicker(const char* popupId, float outColor[3],
                           float* outMetallic, float* outRoughness) {
    struct PickState {
        std::array<float, 3> pick{0.71f, 0.76f, 0.82f};
        float metal = 0.5f, rough = 0.5f;
        bool seeded = false;
    };
    static std::unordered_map<std::string, PickState> s_states;
    PickState& ps = s_states[popupId];
    float* s_pick = ps.pick.data();
    bool committed = false;
    const float S = c5::scale();
    if (ImGui::BeginPopup(popupId)) {
        if (!ps.seeded) {
            s_pick[0] = outColor[0]; s_pick[1] = outColor[1]; s_pick[2] = outColor[2];
            if (outMetallic)  ps.metal = *outMetallic;
            if (outRoughness) ps.rough = *outRoughness;
            ps.seeded = true;
        }
        ImGui::TextUnformatted("Appearance");
        ImGui::Separator();
        ImGui::TextUnformatted("Material");
        const float sw = 22.f * S;
        for (int i = 0; i < kMaterialsCount; ++i) {
            if (i) ImGui::SameLine(0, 6.f * S);
            ImVec4 mc(kMaterials[i].rgb[0], kMaterials[i].rgb[1], kMaterials[i].rgb[2], 1.f);
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
            ImGui::SameLine(0, 2.f * S);
            ImGui::TextUnformatted(kMaterials[i].name);
        }
        ImGui::Separator();
        static const float kPresets[16][3] = {
            {0.71f, 0.76f, 0.82f}, {0.50f, 0.50f, 0.52f}, {0.20f, 0.21f, 0.23f}, {0.85f, 0.85f, 0.86f},
            {0.80f, 0.15f, 0.15f}, {0.15f, 0.35f, 0.80f}, {0.18f, 0.60f, 0.25f}, {0.92f, 0.80f, 0.12f},
            {0.90f, 0.50f, 0.10f}, {0.55f, 0.20f, 0.75f}, {0.10f, 0.75f, 0.85f}, {0.55f, 0.35f, 0.18f},
            {0.08f, 0.18f, 0.55f}, {0.45f, 0.80f, 0.15f}, {0.95f, 0.92f, 0.85f}, {0.08f, 0.08f, 0.10f},
        };
        for (int i = 0; i < 16; ++i) {
            if (i && i != 8) ImGui::SameLine(0, 4.f * S);
            ImVec4 c(kPresets[i][0], kPresets[i][1], kPresets[i][2], 1.f);
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
        ImGui::SetNextItemWidth(180.f * S);
        ImGui::ColorPicker3("##appk", s_pick,
                            ImGuiColorEditFlags_NoSidePreview | ImGuiColorEditFlags_NoSmallPreview);
        if (outMetallic && outRoughness) {
            ImGui::Separator();
            ImGui::SetNextItemWidth(180.f * S);
            ImGui::SliderFloat("##metal", &ps.metal, 0.f, 1.f, "Metalness %.2f");
            ImGui::SetNextItemWidth(180.f * S);
            ImGui::SliderFloat("##rough", &ps.rough, 0.f, 1.f, "Roughness %.2f");
        }
        if (ImGui::Button("Apply", ImVec2(80.f * S, 0))) {
            outColor[0] = s_pick[0]; outColor[1] = s_pick[1]; outColor[2] = s_pick[2];
            if (outMetallic)  *outMetallic = ps.metal;
            if (outRoughness) *outRoughness = ps.rough;
            committed = true;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(80.f * S, 0))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    } else {
        ps.seeded = false;
    }
    return committed;
}

// ---- C5 seam ----------------------------------------------------------------

namespace c5 {

static Renderer* gRenderer = nullptr;
static LegendState gLegend;

bool ips(const AppState& st) { return st.units == "in" || st.units == "ips"; }

bool activeTabIsDrawing(const AppState& st) {
    return st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
           st.docTabs[st.activeTab].kind == core::DocTabKind::Drawing;
}

bool busy(const AppState& st) {
    return st.job.running.load() || st.agent.running;
}

void toast(ToastLevel lvl, const std::string& msg) { ui::postToast(lvl, msg); }

void addLog(AppState& st, const std::string& msg) { st.status = msg; }

bool loadDocMesh(AppState& st, const std::string& aicm, std::string& err) {
    err.clear();
    if (aicm.empty()) return true;
    if (st.scene) {
        if (!st.scene->loadAicm(aicm)) { err = "scene load failed"; return false; }
        return true;
    }
    err = "no viewport scene";
    return false;
}

void markActiveTabDirty(AppState& st) {
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size())
        st.docTabs[st.activeTab].dirty = true;
}

bool pmParseDim(const std::string& text, double& outMm, std::string& err, bool angle,
                double bareLengthScale) {
    err.clear();
    std::string s;
    for (char c : text) if (!std::isspace((unsigned char)c)) s += c;
    if (s.empty()) { err = "empty"; return false; }
    for (auto& c : s) c = (char)std::tolower((unsigned char)c);
    if (angle) {
        if (!s.empty() && s.back() == 'g' && s.size() >= 3 && s[s.size() - 3] == 'd' &&
            s[s.size() - 2] == 'e')
            s.resize(s.size() - 3);
        char* end = nullptr;
        double v = std::strtod(s.c_str(), &end);
        if (end == s.c_str() || (end && *end)) { err = "bad value"; return false; }
        outMm = v;
        return true;
    }
    double scale = bareLengthScale;
    if (s.size() >= 2 && s.compare(s.size() - 2, 2, "mm") == 0) {
        s.resize(s.size() - 2);
        scale = 1.0;
    } else if (s.size() >= 2 && s.compare(s.size() - 2, 2, "in") == 0) {
        s.resize(s.size() - 2);
        scale = 25.4;
    }
    char* end = nullptr;
    double v = std::strtod(s.c_str(), &end);
    if (end == s.c_str() || (end && *end)) { err = "bad value"; return false; }
    outMm = v * scale;
    return true;
}

std::string pmFormatDim(double mm, bool ipsUnits, bool angle) {
    char buf[48];
    if (angle) std::snprintf(buf, sizeof(buf), "%.4gdeg", mm);
    else if (ipsUnits) std::snprintf(buf, sizeof(buf), "%.4gin", mm / 25.4);
    else std::snprintf(buf, sizeof(buf), "%.4gmm", mm);
    return buf;
}

const PMFormDef* pmFindForm(const std::string&) { return nullptr; }  // C2 PropertyManager

void setEvaluateRenderer(Renderer* r) { gRenderer = r; }
Renderer* evaluateRenderer() { return gRenderer; }

LegendState& legendState() { return gLegend; }

void setColormap(bool on) {
    if (gRenderer) gRenderer->setColormapMode(on);
}

void setZebra(bool on, float freq) {
    if (gRenderer) gRenderer->setZebra(on, freq);
}

bool scalarRange(float& vmin, float& vmax) {
    return gRenderer && gRenderer->scalarRange(vmin, vmax);
}

} // namespace c5

static bool gShowEquations = false;
static bool gShowDesignTable = false;
static bool gShowCutList = false;

bool& showEquationsDialog() { return gShowEquations; }
bool& showDesignTablePanel() { return gShowDesignTable; }
bool& showCutListPanel() { return gShowCutList; }

} // namespace ui
