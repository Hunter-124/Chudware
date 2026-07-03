// FeatureManager tree panel (C1). Ported from host/src/ui/feature_tree.cpp.
// F4 DocMirror/worker seam via FmState adapter until AppState grows C1 fields (G3 integrator).
#include "ui/feature_tree.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

#include "imgui.h"

#include "app/app_state.h"
#include "platform/file_dialog.h"
#include "platform/paths.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "theme/tokens.h"
#include "theme/domain_tokens.h"
#include "ui/toast.h"
#include "ui/cursors.h"

#include "async_rpc.h"
#include "doc_mirror.h"
#include "worker_client.h"
#include "viewport/scene.h"

#include <nlohmann/json.hpp>

namespace ui {
namespace detail {

// JSON helpers (from host feature_tree.cpp).
static inline std::string jfld(const nlohmann::json& o, const char* k) {
    auto it = o.find(k);
    return (it != o.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
static inline bool jftbool(const nlohmann::json& o, const char* k, bool def = false) {
    auto it = o.find(k);
    return (it != o.end() && it->is_boolean()) ? it->get<bool>() : def;
}
static inline double jftnum(const nlohmann::json& o, const char* k, double def = -1.0) {
    auto it = o.find(k);
    return (it != o.end() && it->is_number()) ? it->get<double>() : def;
}

// ---- host palette shim (float[4] fields expected by the port) ----------------
struct FmPalette {
    float text[4], textDim[4], accent[4], accentSoft[4], accentMid[4];
    float statusOk[4], statusWarn[4], statusErr[4];
    float treeHover[4], treeSection[4];
    float border[4], panelBg[4];
};
static FmPalette g_fmPal;
static ImU32 g_fmGlassHalo = 0;  // contrast halo for icons::drawGlass; 0 = no live backdrop (icons.h: alpha 0 => no halo)

static void fmPaletteRefresh(const theme::AdaptiveSurface* live = nullptr) {
    const auto& p = theme::pal();
    auto c4 = [&](unsigned h, float* o) {
        ImVec4 v = theme::vec(h);
        o[0] = v.x; o[1] = v.y; o[2] = v.z; o[3] = v.w;
    };
    auto v4 = [&](const ImVec4& v, float* o) {
        o[0] = v.x; o[1] = v.y; o[2] = v.z; o[3] = v.w;
    };
    c4(p.txt0, g_fmPal.text);
    c4(p.txt1, g_fmPal.textDim);
    c4(p.accent, g_fmPal.accent);
    c4(p.accentSoft, g_fmPal.accentSoft);
    c4(p.accentMid, g_fmPal.accentMid);
    c4(p.good, g_fmPal.statusOk);
    c4(p.warn, g_fmPal.statusWarn);
    c4(p.bad, g_fmPal.statusErr);
    g_fmPal.treeHover[0] = g_fmPal.accent[0]; g_fmPal.treeHover[1] = g_fmPal.accent[1];
    g_fmPal.treeHover[2] = g_fmPal.accent[2]; g_fmPal.treeHover[3] = 0.10f;
    c4(p.txt1, g_fmPal.treeSection);
    c4(p.line, g_fmPal.border);
    c4(p.glassBg, g_fmPal.panelBg);
    // A7/contract §5: over a LIVE viewport backdrop, adaptive contrast wins for the text/dim/border
    // roles (max legibility over frosted glass); semantic roles (accent/status/hover) stay palette.
    if (live) {
        v4(live->textPrimary,   g_fmPal.text);
        v4(live->textSecondary, g_fmPal.textDim);
        v4(live->textSecondary, g_fmPal.treeSection);
        v4(live->textTertiary,  g_fmPal.border);
        // surf.tint is already the opposite-tone neutral (theme.cpp adaptiveSurface); ~0x50 alpha.
        g_fmGlassHalo = ImGui::ColorConvertFloat4ToU32(
            ImVec4(live->tint.x, live->tint.y, live->tint.z, 0x50 / 255.f));
    } else {
        g_fmGlassHalo = 0;  // icons.h contract: haloCol alpha 0 => drawGlass draws plain
    }
}
static const FmPalette& fmPalette() { return g_fmPal; }
static ImU32 fmToU32(const float f[4]) {
    return ImGui::ColorConvertFloat4ToU32(ImVec4(f[0], f[1], f[2], f[3]));
}
// Icon tint over the live glass surface (palette text/dim fallback already folded into g_fmPal by
// fmPaletteRefresh() above). `a` is the caller's own alpha (dim/hover/suppressed states).
static ImU32 fmIconTint(float a) {
    const float* f = fmPalette().text;
    return ImGui::ColorConvertFloat4ToU32(ImVec4(f[0], f[1], f[2], f[3] * a));
}
static ImU32 fmIconTintDim(float a) {
    const float* f = fmPalette().textDim;
    return ImGui::ColorConvertFloat4ToU32(ImVec4(f[0], f[1], f[2], f[3] * a));
}
static ImU32 fmGlassHalo() { return g_fmGlassHalo; }

// ---- appearance_common (inlined; host appearance_common.cpp) -------------------
struct MaterialPreset { const char* name; float metallic, roughness, rgb[3]; };
constexpr int kMaterialsCount = 5;
struct PhysicalMaterial { const char* key; const char* name; float metallic, roughness, rgb[3]; };
constexpr int kPhysicalMaterialsCount = 9;

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
        const float sw = 22.0f * theme::S();
        for (int i = 0; i < kMaterialsCount; ++i) {
            if (i) ImGui::SameLine(0, 6.0f * theme::S());
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
            ImGui::SameLine(0, 2.0f * theme::S());
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
            if (i && i != 8) ImGui::SameLine(0, 4.0f * theme::S());
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
        ImGui::SetNextItemWidth(180.0f * theme::S());
        ImGui::ColorPicker3("##appk", s_pick,
                            ImGuiColorEditFlags_NoSidePreview | ImGuiColorEditFlags_NoSmallPreview);
        // C-overhaul: metalness + roughness sliders let a CUSTOM color carry any PBR finish (not just
        // the finish inherited from the last named material). Captured by Apply below.
        if (outMetallic && outRoughness) {
            ImGui::Separator();
            ImGui::SetNextItemWidth(180.0f * theme::S());
            ImGui::SliderFloat("##metal", &ps.metal, 0.0f, 1.0f, "Metalness %.2f");
            ImGui::SetNextItemWidth(180.0f * theme::S());
            ImGui::SliderFloat("##rough", &ps.rough, 0.0f, 1.0f, "Roughness %.2f");
        }
        if (ImGui::Button("Apply", ImVec2(80.0f * theme::S(), 0))) {
            outColor[0] = s_pick[0]; outColor[1] = s_pick[1]; outColor[2] = s_pick[2];
            if (outMetallic)  *outMetallic = ps.metal;
            if (outRoughness) *outRoughness = ps.rough;
            committed = true;
            ImGui::CloseCurrentPopup();
        }
        ImGui::SameLine();
        if (ImGui::Button("Cancel", ImVec2(80.0f * theme::S(), 0))) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    } else {
        ps.seeded = false;  // re-seed next time the popup opens (C38 A1: per-id flag)
    }
    return committed;
}


static int promptImportMeshAsSolid() {
    if (std::getenv("AI_CAD_NO_FILE_DIALOGS")) return 1;
    return 1;  // default: import mesh as solid until platform prompt lands
}

// ---- FmState: extends app::AppState with host FM fields (module-local) --------
struct PickHit { uint32_t body_id = 0; };

struct FmState {
    app::AppState* app = nullptr;

    std::string selectedFeatureId;
    std::vector<uint64_t> featureHighlightFaces;
    bool showPlane[3] = {false, false, false};
    int  planeEmphasis = -1;
    bool planesUserToggled = false;
    bool hasDocMesh = false;
    std::vector<uint8_t> bodyVisible;
    std::vector<uint32_t> docBodyIds;
    std::unordered_set<std::string> hiddenFeatures;
    bool treeScrollToSelection = false;
    bool showEquations = false;
    bool handoffQuietOpen = false;
    bool agentV2Running = false;
    PickHit hover;
    std::string pickStatus;
    float zoomToBBoxMin[3]{}, zoomToBBoxMax[3]{};
    bool zoomToBBoxRequested = false;
    float yaw = 35.f, pitch = 28.f, zoom = 1.f, pan[3]{};
    std::unordered_map<uint64_t, std::array<float, 6>> faceBBoxCache;
    std::unordered_map<uint32_t, std::array<float, 6>> bodyBBoxCache;

    struct Clipboard {
        enum Kind { None, Feature } kind = None;
        std::string srcDocId;
        std::vector<nlohmann::json> payloads;
        void clear() { kind = None; payloads.clear(); srcDocId.clear(); }
    } clipboard;

    bool sketchPlanePickActive = false;
    void* sketchMode = nullptr;
    void* propertyManager = nullptr;

    void bind(app::AppState& st) {
        app = &st;
        agentV2Running = st.agent.running;
        if (!selectedFeatureId.empty()) st.selectedFeature = selectedFeatureId;
        else if (!st.selectedFeature.empty()) selectedFeatureId = st.selectedFeature;
        hasDocMesh = st.scene && st.engineReady && !st.engine.aicmPath().empty();
    }
    void syncToApp() {
        if (!app) return;
        app->selectedFeature = selectedFeatureId;
        app->selCount = (int)app->selection.size();
    }

    void addLog(const std::string& msg) { (void)msg; }
    void toast(ui::ToastLevel lvl, const std::string& msg) { ui::postToast(lvl, msg); }
    void requestCursor(ui::CursorKind k) { ui::requestCursor(k); }

    void loadDocMesh(const std::string& aicm, std::string& err) {
        err.clear();
        if (!app || !app->scene) { err = "no scene"; return; }
        app->scene->loadAicm(aicm);
        app->viewMeshRev = -1;
        hasDocMesh = true;
    }
    void clearViewportForNewDoc() {
        featureTreeResetTransient();
        hasDocMesh = false;
        bodyVisible.clear();
        docBodyIds.clear();
        showPlane[0] = showPlane[1] = showPlane[2] = false;
        planeEmphasis = -1;
        planesUserToggled = false;
        if (app && app->scene) app->scene->clearSelection();
    }
    void applyBodyVisibility() {}
    void applyDatumVisibility() {}
    void clearActiveTabDirty() {
        if (app && app->activeTab >= 0 && app->activeTab < (int)app->docTabs.size())
            app->docTabs[app->activeTab].dirty = false;
    }
    void clearDrawingDirtyForSource(const std::string&) {}
    void dropBlankStarterTabs(int) {}
    void adoptDrawingTab(const std::string&, const std::string&, const std::string&) {}
    void closeDocTab(int) {}
    void addDocTab(bool isAsm) { if (app) app->addDocTab(isAsm); }

    void docUndo() {
        if (!app) return;
        auto resp = app->docMirror.undo(app->worker);
        if (resp.ok) {
            if (!resp.aicm_path.empty()) { std::string le; loadDocMesh(resp.aicm_path, le); }
            app->onDocMutation();
            toast(ui::ToastLevel::Info, "Undo");
        } else if (!resp.error_message.empty())
            toast(ui::ToastLevel::Warn, std::string("Undo: ") + resp.error_message);
    }
    void docRedo() {
        if (!app) return;
        auto resp = app->docMirror.redo(app->worker);
        if (resp.ok) {
            if (!resp.aicm_path.empty()) { std::string le; loadDocMesh(resp.aicm_path, le); }
            app->onDocMutation();
            toast(ui::ToastLevel::Info, "Redo");
        } else if (!resp.error_message.empty())
            toast(ui::ToastLevel::Warn, std::string("Redo: ") + resp.error_message);
    }
};

static FmState g_fm;
static std::unordered_set<int> g_handoffTabs;

static bool fmHandoffTab(int i) { return g_handoffTabs.count(i) > 0; }
static bool fmHandoffActiveTab(FmState& st) {
    return st.app && st.app->activeTab >= 0 && fmHandoffTab(st.app->activeTab);
}
static void fmSetHandoffActive(FmState& st, bool v) {
    if (st.app && st.app->activeTab >= 0) {
        if (v) g_handoffTabs.insert(st.app->activeTab);
        else g_handoffTabs.erase(st.app->activeTab);
    }
}

using AppState = FmState;

static void refreshFeatureHighlight(FmState& st);

// ---- icon helper ----
// Attempt to draw a named atlas icon; fall back to a text glyph if the atlas is absent.
// C37-E: `alpha` dims the icon (suppressed / rolled-back rows get a faded icon, item 6).
static void treeIcon(const char* name, const char* fallback, float size = 16.0f, float alpha = 1.0f) {
    (void)fallback;  // icons::drawGlass carries its own hand-drawn -> atlas -> monogram fallback
    const float px = size * theme::S();
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::Dummy(ImVec2(px, px));
    icons::drawGlass(ImGui::GetWindowDrawList(), name, ImVec2(p0.x + px * 0.5f, p0.y + px * 0.5f),
                     px * 0.5f, fmIconTint(alpha), fmGlassHalo());
}

// Frosted row pill at the current cursor (full content width). Stripe index advances per call.
static int s_fmStripeIdx = 0;
static void fmRowPill(bool selected = false) {
    const float S = theme::S();
    const float rowH = ImGui::GetFrameHeightWithSpacing();
    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    float availW = ImGui::GetContentRegionAvail().x;
    ImVec2 rowMax(rowMin.x + availW, rowMin.y + rowH);
    const float wash = selected ? theme::rowWash::selected : theme::rowWash::idle;
    theme::glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, 3.5f,
                           (s_fmStripeIdx++ % 2) == 0, selected, wash);
}

// ---- Solid Bodies visibility helper ----
// Show/hide one renderer body slot. Host-side state only (AppState::bodyVisible) — no doc ops,
// so it is applied immediately (no s_pendingTreeOps detour needed). Hiding also drops any
// selection/hover that references the body: the id pass stops rendering it, so a stale highlight
// would point at geometry the user can no longer see or re-pick.
static void setBodyShown(AppState& st, int slot, bool shown) {
    if (slot < 0 || slot >= (int)st.bodyVisible.size()) return;
    st.bodyVisible[slot] = shown ? 1 : 0;
    if (!shown) {
        uint32_t bid = slot < (int)st.docBodyIds.size() ? st.docBodyIds[slot] : 0;
        bool selHasBody = false;
        for (const auto& it : st.app->selection.items())
            if (it.hit.body_id == bid) { selHasBody = true; break; }
        if (selHasBody) st.app->selection.clear();
        if (st.hover.body_id == bid) { st.hover = PickHit{}; st.pickStatus.clear(); }
    }
    st.applyBodyVisibility();
}

// ---- per-feature eye (FeatureManager hide/show on every tree row) ----
// Renderer slot index for an AICM body id (or -1 if that body isn't currently loaded).
static int slotForBodyId(AppState& st, uint32_t bid) {
    for (int i = 0; i < (int)st.docBodyIds.size(); ++i)
        if (st.docBodyIds[i] == bid) return i;
    return -1;
}
// Is a feature currently shown? Features that OWN bodies derive it from those bodies' visibility
// (any owned body hidden => the feature reads as hidden); everything else from the hiddenFeatures
// set. This keeps the eye and the "Solid Bodies" folder in sync for solids while still giving
// sketches/datums a hide toggle.
static bool featureEyeShown(AppState& st, const std::string& fid,
                            const std::vector<uint32_t>& ownedBodies) {
    if (!ownedBodies.empty()) {
        for (uint32_t bid : ownedBodies) {
            int slot = slotForBodyId(st, bid);
            if (slot >= 0 && slot < (int)st.bodyVisible.size() && !st.bodyVisible[slot])
                return false;
        }
        return true;
    }
    return st.hiddenFeatures.find(fid) == st.hiddenFeatures.end();
}
// Flip a feature's visibility. Body-owning => toggle every owned body (reuses setBodyShown so the
// Solid Bodies folder agrees); other features => flip hiddenFeatures membership (the sketch-ghost
// renderer honors it).
static void toggleFeatureEye(AppState& st, const std::string& fid,
                             const std::vector<uint32_t>& ownedBodies, bool currentlyShown) {
    if (!ownedBodies.empty()) {
        for (uint32_t bid : ownedBodies) {
            int slot = slotForBodyId(st, bid);
            if (slot >= 0) setBodyShown(st, slot, !currentlyShown);
        }
    } else {
        if (currentlyShown) st.hiddenFeatures.insert(fid);
        else st.hiddenFeatures.erase(fid);
        // Datum features (ref axis/plane/point/csys) are reference overlays, not bodies — push the
        // hidden set to the renderer so the toggle hides/shows them immediately (sketches read
        // st.hiddenFeatures live in drawGhosts, so they need no extra push).
        st.applyDatumVisibility();
    }
}

// ---- status badge (ok/warn/error/rolled-back) ----
// C37-E: a rolled-back feature (after the rollback bar) is NOT an error — it gets a distinct muted
// "paused" marker, never the red "!". Pass rolledBack=true for those rows.
static void featureBadge(bool ok, bool hasError, const std::string& errText,
                         const std::vector<std::string>& danglingRefs,
                         bool rolledBack = false, const std::string& warnText = "") {
    (void)hasError;
    // Pick this row's health dot: color, filled-vs-hollow, and tooltip. Rendering the badge as a
    // draw-list circle (not a •/!/?/◦ text glyph) makes it pixel-identical at any DPI and removes the
    // per-font glyph-advance/size variance the old SameLine+TextUnformatted markers had.
    const float S = theme::S();
    ImU32 col = 0;
    bool  hollow = false;
    bool  hasTip = false;
    std::string tip;
    if (rolledBack) {
        // Muted hollow ring — "not currently built" (distinct from ok/error/dangling).
        col = ImGui::ColorConvertFloat4ToU32(
            ImVec4(fmPalette().textDim[0], fmPalette().textDim[1], fmPalette().textDim[2], 0.75f));
        hollow = true;
        tip = "Rolled back (after the rollback bar — not built)"; hasTip = true;
    } else if (ok && danglingRefs.empty() && !warnText.empty()) {
        // Built, but with a non-fatal note (e.g. fillet rounded a subset of edges) — amber dot.
        col = fmToU32(fmPalette().statusWarn);
        tip = warnText; hasTip = true;
    } else if (ok && danglingRefs.empty()) {
        col = fmToU32(fmPalette().statusOk);    // healthy — teal dot
    } else if (!ok) {
        col = fmToU32(fmPalette().statusErr);   // error — red-orange dot
        if (!errText.empty()) { tip = errText; hasTip = true; }
    } else if (!danglingRefs.empty()) {
        col = fmToU32(fmPalette().statusWarn);  // dangling refs — amber dot
        tip = "Dangling refs:\n";
        for (const auto& r : danglingRefs) { tip += "  "; tip += r; tip += '\n'; }
        hasTip = true;
    } else {
        return;   // nothing to flag
    }

    const float r = 3.5f * S;
    const float w = 2.0f * r + 4.0f * S;   // dot diameter + breathing room
    ImGui::SameLine();
    ImVec2 p = ImGui::GetCursorScreenPos();
    float  lineH = ImGui::GetTextLineHeight();
    ImVec2 center(p.x + r + 2.0f * S, p.y + lineH * 0.5f);
    // A non-interactive Dummy advances the layout cursor by [w]; the tooltip uses a geometric
    // IsMouseHoveringRect (same pattern as the per-row eye above) so the badge never captures hover
    // or competes with the row's click / context-menu binding — tree behavior is unchanged.
    ImGui::Dummy(ImVec2(w, lineH));
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hollow) dl->AddCircle(center, r, col, 0, std::max(1.0f, 1.5f * S));
    else        dl->AddCircleFilled(center, r, col);
    if (hasTip && ImGui::IsMouseHoveringRect(p, ImVec2(p.x + w, p.y + lineH)))
        ImGui::SetTooltip("%s", tip.c_str());
}

// ---- glass popover styling (shared by every FeatureManager popup) ----
// A slightly lighter, blue-tinted, mostly-opaque popup bg (glass depth illusion), a teal accent
// border, rounded corners and roomier padding. Push BEFORE the BeginPopup*/picker call and pop
// AFTER EndPopup/the call: the styles must stay live for the popup's whole deferred draw. The
// push/pop is balanced every frame regardless of whether the popup is actually open.
static void pushTreePopupGlass() {
    const auto& pal = fmPalette();
    ImGui::PushStyleColor(ImGuiCol_PopupBg,
        ImVec4(pal.panelBg[0] * 1.08f, pal.panelBg[1] * 1.08f, pal.panelBg[2] * 1.20f, 0.92f));
    ImGui::PushStyleColor(ImGuiCol_Border,
        ImVec4(pal.accentMid[0], pal.accentMid[1], pal.accentMid[2], 0.60f));
    ImGui::PushStyleVar(ImGuiStyleVar_PopupRounding, 8.0f * theme::S());
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
        ImVec2(10.0f * theme::S(), 7.0f * theme::S()));
}
static void popTreePopupGlass() {
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(2);
}

// ---- styled section rule with a centered label ----
// Groups the tree into its three logical sections (document header / reference geometry / feature
// history) far more clearly than a bare Separator(): a treeSection-colored centered label flanked by
// two short draw-list hairlines, then a Dummy to re-sync the layout cursor below the drawn row.
static void treeSectionDivider(const char* label) {
    const auto& pal = fmPalette();
    const float S = theme::S();
    ImVec2 p = ImGui::GetCursorScreenPos();
    float  w = ImGui::GetContentRegionAvail().x;
    float  lineH = ImGui::GetTextLineHeight();
    ImVec2 tsz = ImGui::CalcTextSize(label);
    float  textX = p.x + (w - tsz.x) * 0.5f;
    float  cy = p.y + lineH * 0.5f;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImU32 lineCol = fmToU32(pal.border);
    if (textX > p.x + 4.0f * S) {   // guard tiny panels (avoid zero/negative-length segments)
        dl->AddLine(ImVec2(p.x + 4.0f * S, cy), ImVec2(textX - 6.0f * S, cy),
                    lineCol, std::max(1.0f, 1.0f * S));
        dl->AddLine(ImVec2(textX + tsz.x + 6.0f * S, cy), ImVec2(p.x + w - 4.0f * S, cy),
                    lineCol, std::max(1.0f, 1.0f * S));
    }
    ImGui::SetCursorScreenPos(ImVec2(textX, p.y));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.treeSection[0], pal.treeSection[1], pal.treeSection[2], pal.treeSection[3]));
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
    ImGui::SetCursorScreenPos(ImVec2(p.x, p.y));
    ImGui::Dummy(ImVec2(w, lineH + 4.0f * S));
}

// ---- shared op applier (DocMirror ops -> rebuild -> reload mesh) ----
// Centralizes the apply_ops -> loadDocMesh pattern used by suppress/delete/reorder/rename/folder.
// Dependency violations (reorder/delete) are reported as a warning, leaving the tree unchanged.
//
// C25 CRASH FIX: DocMirror::applyOps REPLACES the mirror's tree json, but every caller here
// sits inside the draw code that iterates references INTO that json (drag-drop reorder,
// row context menus, Ctrl+Up/Down) — applying immediately is a use-after-free. applyTreeOps
// therefore only ENQUEUES; flushPendingTreeOps() applies after all tree reads are done
// (end of drawFeatureTree).
struct PendingTreeOps { nlohmann::json ops; std::string what; };
static std::vector<PendingTreeOps> s_pendingTreeOps;

static void applyTreeOpsNow(AppState& st, const nlohmann::json& ops, const char* what) {
    if (!st.app->docMirror.hasDoc() || !st.app->workerStarted) return;
    auto resp = st.app->docMirror.applyOps(st.app->worker, ops);
    if (resp.ok && !resp.aicm_path.empty()) {
        std::string lerr;
        st.loadDocMesh(resp.aicm_path, lerr);
        if (!lerr.empty()) st.addLog("[err] loadDocMesh: " + lerr);
    } else if (!resp.ok) {
        if (resp.error_code == "dependency_exists") {
            st.addLog(std::string("[warn] ") + what +
                      ": blocked — a feature depends on this order");
            st.toast(ui::ToastLevel::Warn,
                     std::string(what) + " blocked: a feature depends on this order");
        } else {
            st.addLog(std::string("[err] ") + what + ": " + resp.error_message);
            // W1-B (item 2): surface the failure instead of only logging it. (Transport errors are
            // already toasted via the worker error sink; this covers method-level op rejections.)
            if (resp.error_code != "transport")
                st.toast(ui::ToastLevel::Error,
                         std::string(what) + " failed: " +
                         (resp.error_message.empty() ? resp.error_code : resp.error_message));
        }
    }
}

static void applyTreeOps(AppState& st, const nlohmann::json& ops, const char* what) {
    (void)st;
    s_pendingTreeOps.push_back({ops, what});
}

static void flushPendingTreeOps(AppState& st) {
    if (s_pendingTreeOps.empty()) return;
    auto pending = std::move(s_pendingTreeOps);
    s_pendingTreeOps.clear();
    for (const auto& p : pending) applyTreeOpsNow(st, p.ops, p.what.c_str());
}

// Inline-rename state: which feature id is being renamed + its edit buffer.
static std::string s_renamingId;
static char        s_renameBuf[128] = {0};

// C37-E: multi-selection (Ctrl+click). File-local; st.selectedFeatureId tracks the PRIMARY (last
// clicked). Cleared on a plain click, on doc change (featureTreeResetTransient), and whenever the
// selection is validated away. A row is "selected" if it is the primary OR in this set.
static std::unordered_set<std::string> s_multiSel;

// C37-E: slow-double-click rename arming. A second single-click on the already-primary row, with a
// gap in (0.4s, 2.0s), arms the inline editor (SolidWorks "click-pause-click to rename"). We record
// the time of the last single-click commit per row.
static std::string s_lastClickId;
static double      s_lastClickTime = -1.0;

// C37-E: hover-to-highlight is CLICK-ONLY in this revision (see DEVIATIONS). A transient hover tint
// would have to repaint st.featureHighlightFaces on enter and restore the click selection on leave
// every frame; in immediate mode that fought the click highlight and flickered, and the card
// explicitly permits click-only. So we do not track hover dwell.

// C37-E: rollback-bar drag state. While the user is dragging the bar handle we track it here so the
// drop computes the feature the bar should sit AFTER (or null = end). s_rbDropAfter is the fid the
// bar would land after given the cursor's current row (sentinel "\0end" = drop at the end).
static bool        s_rollbackDragging = false;
static std::string s_rbDropAfter;   // updated each frame while dragging; "" until a row is hit

// C37-E: per-row appearance picker target. The row's context menu stashes which body to recolor + a
// one-shot open flag; drawFeatureTree renders the picker popup (sibling) and turns a commit into a
// set_appearance op. File-local so one picker serves every row.
static std::string s_apprBody;     // body key ("b1") the picker targets, or empty
static bool        s_apprOpen = false;
static float       s_apprColor[3] = {0.71f, 0.76f, 0.82f};
static float       s_apprMetallic = 0.0f, s_apprRoughness = 0.5f;

// C41: the document's CURRENT file path ("" = never saved). Owned by this panel: Save re-uses
// it silently (the dialog only shows on first save / Save As), the bottom strip displays its
// file name, and a successful Open sets it. Cleared with the other transients on every doc
// change (featureTreeResetTransient), so a New Part never inherits the previous doc's path.
static std::string s_docPath;

// ---- Wave 4C: FeatureManager copy / cut / paste ----------------------------------------------
// copy_feature is READ-ONLY (no doc mutation) but the engine's session-level apply_ops still bumps
// the revision + pushes an undo snapshot + marks the session dirty (there is no read-only fast path
// at the worker level). We therefore wrap the copy in suppressTabDirty so it does NOT light the tab's
// unsaved marker, and we skip the (unchanged) mesh reload — the mirror's revision advances in
// lockstep so the next real edit is never stale. The portable bundle comes back via the op-result
// id_map side-channel ("$bundle"). include_dependencies defaults true so the paste is self-contained
// (the engine resolves cross-doc external refs at paste time).
//
// Returns true if at least one feature's bundle was captured into st.clipboard.
static bool copyFeatureToClipboard(AppState& st, const std::vector<std::string>& fids) {
    if (fids.empty() || !st.app->docMirror.hasDoc() || !st.app->workerStarted) return false;
    if (st.app->job.running.load()) return false;   // same canDoc gate the tree mutations use
    std::vector<nlohmann::json> bundles;
    bool prevSuppress = st.app->suppressTabDirty;
    st.app->suppressTabDirty = true;   // copy is read-only — don't dirty the tab on the phantom rev bump
    for (const auto& fid : fids) {
        nlohmann::json ops = nlohmann::json::array({
            {{"op", "copy_feature"}, {"id", fid}, {"include_dependencies", true}}});
        auto resp = st.app->docMirror.applyOps(st.app->worker, ops);
        if (!resp.ok) {
            st.addLog("[copy] copy_feature failed: " + resp.error_message);
            continue;
        }
        // The bundle rides the id_map side-channel. "$bundle" is the last copy's bundle;
        // "$bundle:0" is this batch's (we send one copy_feature per batch, so index 0).
        if (resp.id_map.is_object()) {
            if (resp.id_map.contains("$bundle") && resp.id_map["$bundle"].is_object())
                bundles.push_back(resp.id_map["$bundle"]);
            else if (resp.id_map.contains("$bundle:0") && resp.id_map["$bundle:0"].is_object())
                bundles.push_back(resp.id_map["$bundle:0"]);
        }
    }
    st.app->suppressTabDirty = prevSuppress;
    if (bundles.empty()) {
        st.toast(ui::ToastLevel::Warn, "Copy failed");
        return false;
    }
    st.clipboard.clear();
    st.clipboard.kind = FmState::Clipboard::Kind::Feature;
    st.clipboard.srcDocId = st.app->docMirror.docId();
    st.clipboard.payloads = std::move(bundles);
    st.addLog("[copy] " + std::to_string(st.clipboard.payloads.size()) + " feature bundle(s)");
    st.toast(ui::ToastLevel::Info,
             "Copied " + std::to_string(st.clipboard.payloads.size()) + " feature(s)");
    return true;
}

// The set of feature ids a Copy/Cut should act on: the full multi-selection if the primary is part
// of it, else just the primary. Mirrors the context menu's `multi` rule.
static std::vector<std::string> featureCopySet(AppState& st) {
    std::vector<std::string> fids;
    if (s_multiSel.size() > 1 && !st.selectedFeatureId.empty() &&
        s_multiSel.count(st.selectedFeatureId) > 0) {
        for (const auto& id : s_multiSel) fids.push_back(id);
    } else if (!st.selectedFeatureId.empty()) {
        fids.push_back(st.selectedFeatureId);
    }
    return fids;
}

// Wave 4D: public entry points so app.cpp's global Ctrl+C/X/V routing + the "edit_*" dispatcher ids
// can drive FeatureManager copy/cut/paste without reaching into this TU's file-statics. These run
// OUTSIDE the tree walk (app.cpp's pre-draw key handler), so the immediate DocMirror calls are safe
// (nothing is iterating the tree json). Each is a no-op (returns false) without an eligible
// selection / clipboard / live doc.
bool featureCopySelection(AppState& st) {
    return copyFeatureToClipboard(st, featureCopySet(st));
}
bool featureCutSelection(AppState& st) {
    auto fids = featureCopySet(st);
    if (!copyFeatureToClipboard(st, fids)) return false;
    // Cut = copy then delete (the same delete op the context menu / multi-delete uses). The engine
    // blocks the delete if a downstream feature depends on it (reported as a warning by
    // applyTreeOpsNow); the copy still stuck on the clipboard, so the user can paste it elsewhere.
    nlohmann::json ops = nlohmann::json::array();
    for (const auto& id : fids)
        ops.push_back({{"op", "delete_feature"}, {"id", id}, {"cascade", false}});
    applyTreeOpsNow(st, ops, "cut");
    return true;
}
bool featurePasteSelection(AppState& st) {
    if (st.clipboard.kind != FmState::Clipboard::Kind::Feature ||
        st.clipboard.payloads.empty())
        return false;
    if (!st.app->docMirror.hasDoc() || !st.app->workerStarted || st.app->job.running.load()) return false;
    nlohmann::json ops = nlohmann::json::array();
    for (const auto& bundle : st.clipboard.payloads)
        ops.push_back({{"op", "paste_feature"}, {"bundle", bundle}});
    applyTreeOpsNow(st, ops, "paste");
    st.toast(ui::ToastLevel::Info,
             "Pasted " + std::to_string(st.clipboard.payloads.size()) + " feature(s)");
    return true;
}

// Wave-5-fill: Edit > Delete — delete the FeatureManager selection. Mirrors featureCutSelection
// minus the copy: same delete_feature op (cascade:false) the row context menu and multi-delete use.
// Runs OUTSIDE the tree walk (app.cpp's pump), so the immediate DocMirror call is safe. applyTreeOpsNow
// surfaces an engine block (a downstream feature depends on this one) as a warning toast and leaves
// the tree unchanged. No-op (false) without a live, idle doc or an eligible selection.
bool featureDeleteSelection(AppState& st) {
    if (!st.app->docMirror.hasDoc() || !st.app->workerStarted || st.app->job.running.load()) return false;
    auto fids = featureCopySet(st);   // full multi-select if the primary is in it, else the primary
    if (fids.empty()) return false;
    nlohmann::json ops = nlohmann::json::array();
    for (const auto& id : fids)
        ops.push_back({{"op", "delete_feature"}, {"id", id}, {"cascade", false}});
    applyTreeOpsNow(st, ops, "delete");
    // Drop the now-deleted ids from the selection so a stale primary/multi-set can't linger (the tree
    // walk also validates against the live tree, but clearing here keeps the menu's state honest).
    for (const auto& id : fids) s_multiSel.erase(id);
    st.selectedFeatureId.clear();
    return true;
}

// Wave-5-fill: Edit > Select All — select every feature in the active part's FeatureManager. Reads
// the doc tree's "features" array (the same source the tree-walk's `order` list is built from) and
// puts every feature id into the multi-select, with the last as the primary so context-menu multi
// actions light up. No-op (false) for assemblies (no flat feature history), empty docs, or no doc.
bool featureSelectAll(AppState& st) {
    if (!st.app->docMirror.hasDoc()) return false;
    const nlohmann::json& tree = st.app->docMirror.tree();
    if (!tree.is_object()) return false;
    if (jfld(tree, "schema") == "aicad.asm/1") return false;  // assemblies have no feature history
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array() || feat->empty()) return false;
    s_multiSel.clear();
    std::string last;
    for (const auto& f : *feat) {
        if (!f.is_object()) continue;
        std::string id = jfld(f, "id");
        if (id.empty()) continue;
        s_multiSel.insert(id);
        last = id;
    }
    if (s_multiSel.empty()) return false;
    st.selectedFeatureId = last;        // primary = last feature (drives the multi-action gating)
    refreshFeatureHighlight(st);        // light the primary's owned faces in the viewport
    return true;
}

// ---- Wave 4C: deferred copy/cut/paste from the row CONTEXT MENU ----
// copyFeatureToClipboard calls DocMirror::applyOps IMMEDIATELY (it needs the id_map bundle back
// synchronously). That REPLACES the mirror's tree json — a use-after-free if invoked while the row
// walk is still iterating that json (the same hazard applyTreeOps defers around). So the context
// menu only RECORDS a request here; flushPendingClipboard() runs it after the tree walk, next to
// flushPendingTreeOps. The captured fid set is snapshotted so a later selection change can't move it.
enum class ClipReq { None, Copy, Cut, Paste };
static ClipReq                  s_clipReq = ClipReq::None;
static std::vector<std::string> s_clipReqFids;

static void requestClipboard(ClipReq req, std::vector<std::string> fids) {
    s_clipReq = req;
    s_clipReqFids = std::move(fids);
}

static void flushPendingClipboard(AppState& st) {
    if (s_clipReq == ClipReq::None) return;
    ClipReq req = s_clipReq;
    std::vector<std::string> fids = std::move(s_clipReqFids);
    s_clipReq = ClipReq::None;
    s_clipReqFids.clear();
    switch (req) {
        case ClipReq::Copy:
            copyFeatureToClipboard(st, fids);
            break;
        case ClipReq::Cut:
            if (copyFeatureToClipboard(st, fids)) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : fids)
                    ops.push_back({{"op", "delete_feature"}, {"id", id}, {"cascade", false}});
                applyTreeOpsNow(st, ops, "cut");
            }
            break;
        case ClipReq::Paste:
            if (st.clipboard.kind == FmState::Clipboard::Kind::Feature &&
                !st.clipboard.payloads.empty()) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& bundle : st.clipboard.payloads)
                    ops.push_back({{"op", "paste_feature"}, {"bundle", bundle}});
                applyTreeOpsNow(st, ops, "paste");
                st.toast(ui::ToastLevel::Info,
                         "Pasted " + std::to_string(st.clipboard.payloads.size()) + " feature(s)");
            }
            break;
        default: break;
    }
}

// W1-B (item 5): drop the tree's in-progress transient state on a doc change. Declared in
// feature_tree.h; called from AppState::clearViewportForNewDoc and wholesale-replace paths.
void featureTreeResetTransient() {
    s_renamingId.clear();
    s_renameBuf[0] = '\0';
    s_pendingTreeOps.clear();
    s_multiSel.clear();                 // C37-E
    s_lastClickId.clear();              // C37-E
    s_lastClickTime = -1.0;
    s_rollbackDragging = false;
    s_rbDropAfter.clear();
    s_apprBody.clear();
    s_apprOpen = false;
    s_docPath.clear();                  // C41: a fresh/replaced doc has no file path yet
    s_clipReq = ClipReq::None;          // Wave 4C: drop any unflushed copy/cut/paste request
    s_clipReqFids.clear();              // (the clipboard payload itself survives a doc switch)
}

// Clear only the multi-select set — see feature_tree.h.
void featureClearTreeMultiSel() { s_multiSel.clear(); }

// ---- C26: appearance color-picker popup ----
// W1-B (item 9): the MaterialPreset table + the appearance picker popup, formerly duplicated here
// and in viewport_panel.cpp, now live in ui/appearance_common.{h,cpp}. Both call ui::appearance-
// ColorPicker(); the CALLER opens the popup (ImGui::OpenPopup(popupId)) after stashing which body
// the edit targets, then turns a true return into a set_appearance op.

// ---- feature_status (ok / error / dangling / rolled-back / compile ms) ----
struct FStatus {
    bool ok = true;
    std::string error;
    std::string warning;         // non-fatal note on an ok feature (e.g. fillet rounded a subset)
    std::vector<std::string> dangling;
    bool   rolledBack = false;   // C37-E: feature sits after the rollback bar (not built, not error)
    double ms = -1.0;            // C37-E: last compile time (ms); <0 = unknown
};

static FStatus parseStatusRow(const nlohmann::json& s) {
    FStatus fs;
    fs.ok = jftbool(s, "ok", true);
    fs.rolledBack = jftbool(s, "rolled_back", false);
    fs.ms = jftnum(s, "ms", -1.0);
    auto it = s.find("error");
    if (it != s.end() && it->is_string()) fs.error = it->get<std::string>();
    auto wt = s.find("warning");
    if (wt != s.end() && wt->is_string()) fs.warning = wt->get<std::string>();
    auto dr = s.find("dangling_refs");
    if (dr != s.end() && dr->is_array())
        for (const auto& d : *dr)
            if (d.is_string()) fs.dangling.push_back(d.get<std::string>());
    return fs;
}

// Linear scan — kept for the assembly tree (component count is small). The feature history uses the
// per-frame map below (buildStatusMap) to avoid an O(N^2) scan.
static FStatus getFeatureStatus(const nlohmann::json& statusArr, const std::string& fid) {
    if (!statusArr.is_array()) return {};
    for (const auto& s : statusArr) {
        if (!s.is_object()) continue;
        if (jfld(s, "id") != fid) continue;
        return parseStatusRow(s);
    }
    return {};
}

// C37-E (item 2): build one id->status map per frame so each feature row is an O(1) lookup instead
// of re-scanning the whole status array (the old O(N^2)).
static std::unordered_map<std::string, FStatus> buildStatusMap(const nlohmann::json& statusArr) {
    std::unordered_map<std::string, FStatus> m;
    if (!statusArr.is_array()) return m;
    m.reserve(statusArr.size());
    for (const auto& s : statusArr) {
        if (!s.is_object()) continue;
        std::string id = jfld(s, "id");
        if (!id.empty()) m.emplace(std::move(id), parseStatusRow(s));
    }
    return m;
}

// ---- C37-E: feature -> viewport highlight (owner map -> featureHighlightFaces) ----
// Fills st.featureHighlightFaces with the (body<<32)|face keys this feature owns. The viewport
// agent tints them. Tolerant of a missing owner map: the vector is simply left empty.
static void fillHighlightFaces(AppState& st, const std::string& fid,
                               std::vector<uint64_t>& out) {
    out.clear();
    const auto& om = st.app->docMirror.ownerMap();
    auto it = om.find(fid);
    if (it == om.end()) return;
    out.reserve(it->second.faces.size());
    for (const auto& bf : it->second.faces)
        out.push_back(((uint64_t)bf.first << 32) | (uint64_t)bf.second);
}

// Apply the persistent (click) highlight for a feature: clear topology selection (the tree owns the
// highlight now) and repopulate featureHighlightFaces. No toast when there's no owner map — an empty
// highlight is fine (the card says so).
static void selectFeatureInViewport(AppState& st, const std::string& fid) {
    st.app->selection.clear();
    fillHighlightFaces(st, fid, st.featureHighlightFaces);
}

static void clearFeatureHighlight(AppState& st) {
    st.featureHighlightFaces.clear();
}

static void clearTreeFeatureSelection(AppState& st) {
    st.selectedFeatureId.clear();
    s_multiSel.clear();
    clearFeatureHighlight(st);
}

// C38 (T1): public re-backfill of the feature highlight from the CURRENT owner map. The owner map's
// face tags are rebuilt on every applyOps/undo/redo/resync, so a previously-populated
// featureHighlightFaces (captured at click time) points at stale face ids and tints the wrong faces
// — or none. Re-derive it for the currently-selected feature; clear when nothing is selected or the
// feature is gone. Cheap (one map lookup + small vector fill); safe to call every rebuild.
void refreshFeatureHighlight(AppState& st) {
    if (st.selectedFeatureId.empty()) { st.featureHighlightFaces.clear(); return; }
    fillHighlightFaces(st, st.selectedFeatureId, st.featureHighlightFaces);
}

// C37-E (item 3): "Zoom to Feature" — union the owned faces'/bodies' bboxes (from the host-side
// bbox caches) and post a one-shot camera request. Toasts when nothing framable was found.
static void zoomToFeature(AppState& st, const std::string& fid) {
    const auto& om = st.app->docMirror.ownerMap();
    auto it = om.find(fid);
    bool any = false;
    float mn[3] = {0, 0, 0}, mx[3] = {0, 0, 0};
    auto acc = [&](const std::array<float, 6>& b) {
        if (!any) { for (int i = 0; i < 3; ++i) { mn[i] = b[i]; mx[i] = b[3 + i]; } any = true; }
        else {
            for (int i = 0; i < 3; ++i) {
                if (b[i]     < mn[i]) mn[i] = b[i];
                if (b[3 + i] > mx[i]) mx[i] = b[3 + i];
            }
        }
    };
    if (it != om.end()) {
        for (const auto& bf : it->second.faces) {
            uint64_t key = ((uint64_t)bf.first << 32) | (uint64_t)bf.second;
            auto fit = st.faceBBoxCache.find(key);
            if (fit != st.faceBBoxCache.end()) acc(fit->second);
        }
        // Fall back to the owned bodies' boxes (faces may not all be cached).
        if (!any)
            for (uint32_t b : it->second.bodies) {
                auto bit = st.bodyBBoxCache.find(b);
                if (bit != st.bodyBBoxCache.end()) acc(bit->second);
            }
    }
    if (!any) {
        st.toast(ui::ToastLevel::Info, "Zoom to Feature: no cached geometry for this feature");
        return;
    }
    for (int i = 0; i < 3; ++i) { st.zoomToBBoxMin[i] = mn[i]; st.zoomToBBoxMax[i] = mx[i]; }
    st.zoomToBBoxRequested = true;
}

// ---- draw one feature row ----
// All the data + behavior a single feature row needs. Returns the click outcome so the caller can
// update selection. The struct keeps the (formerly sprawling) parameter list readable.
struct RowCtx {
    std::string fid, name, type;
    const char* iconName = nullptr;
    const char* iconFallback = nullptr;
    bool suppressed = false;
    bool selected   = false;        // primary OR multi-selected
    bool rolledBack = false;        // after the rollback bar → greyed/italic, NOT error styling
    FStatus fs;                     // ok / error / dangling / ms
    int  ownerBodyCount = 0;        // for the tooltip
    std::vector<uint32_t> ownedBodies;  // AICM body ids this feature owns (per-row eye toggle)
    bool nested = false;            // a sketch child node (indented under its consumer)
    bool canDoc = true;            // gates every mutating menu item + drag-reorder
    bool barAtEnd = true;          // hides "Roll to End" when already at the end
    std::string firstOwnedBody;    // body key for the per-row Appearance item ("b<n>")
    const char* convertLabel = nullptr;
    std::function<void()> convertCb = nullptr;
    // reorderCb(dragged, before): drop dragged immediately before `before` ("" = end).
    std::function<void(const std::string&, const std::string&)> reorderCb;
    const std::function<void(const std::string&)>* editCallback = nullptr;
};

// Outcomes of interacting with a row this frame (consumed by the caller).
struct RowResult {
    bool clicked     = false;   // plain left-click (select)
    bool ctrlClicked = false;   // Ctrl+left-click (toggle multi-select)
    bool doubleClick = false;   // open the edit form
    bool hovered     = false;
};

static RowResult featureRow(RowCtx& c, AppState& st) {
    RowResult res;
    const std::string& fid = c.fid;
    // #4.2: feature rows now use the SAME logical 16px icon as headers/folders (was 20) so the icon
    // column is uniform; treeIcon bakes in *uiScale.
    float iconW = 16.0f;
    if (c.nested) ImGui::Indent(14.0f * theme::S());   // #4.3: scaled sketch child indent
    // C37-E (item 6): suppressed/rolled-back rows get a faded icon (clearer than just the [S] tag).
    float iconAlpha = c.suppressed ? 0.45f : (c.rolledBack ? 0.6f : 1.0f);
    treeIcon(c.iconName, c.iconFallback, iconW, iconAlpha);
    ImGui::SameLine(0, 4 * theme::S());   // #4.5: scaled icon↔label gap

    // ---- inline rename editor (replaces the label while active) ----
    if (s_renamingId == fid) {
        ImGui::SetNextItemWidth(-1);
        if (!ImGui::IsAnyItemActive()) ImGui::SetKeyboardFocusHere();
        ImGui::PushStyleColor(ImGuiCol_FrameBg,
            ImVec4(fmPalette().accentSoft[0], fmPalette().accentSoft[1],
                   fmPalette().accentSoft[2], 0.25f));
        ImGui::PushStyleColor(ImGuiCol_Border,
            ImVec4(fmPalette().accent[0], fmPalette().accent[1],
                   fmPalette().accent[2], 0.70f));
        ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 4.0f * theme::S());
        bool commit = ImGui::InputText(("##rn_" + fid).c_str(), s_renameBuf, sizeof(s_renameBuf),
                                       ImGuiInputTextFlags_EnterReturnsTrue |
                                       ImGuiInputTextFlags_AutoSelectAll);
        ImGui::PopStyleVar();
        ImGui::PopStyleColor(2);
        bool cancel = ImGui::IsKeyPressed(ImGuiKey_Escape);
        bool deact  = ImGui::IsItemDeactivated();
        if (commit || cancel || deact) {
            std::string newName = s_renameBuf;
            std::string old = s_renamingId;
            s_renamingId.clear();
            // C37-E (item 2): verify the fid still exists in the live tree before sending; a rename
            // committed onto a feature that was deleted/reordered away would be a stale op.
            bool stillExists = false;
            {
                const nlohmann::json& tr = st.app->docMirror.tree();
                auto fa = tr.is_object() ? tr.find("features") : tr.end();
                if (tr.is_object() && fa != tr.end() && fa->is_array())
                    for (const auto& f : *fa)
                        if (f.is_object() && jfld(f, "id") == old) { stillExists = true; break; }
            }
            if (!cancel && !newName.empty() && newName != c.name) {
                if (stillExists) {
                    nlohmann::json ops = nlohmann::json::array();
                    ops.push_back({{"op", "rename"}, {"id", old}, {"name", newName}});
                    applyTreeOps(st, ops, "rename");
                } else {
                    st.toast(ui::ToastLevel::Warn, "Rename dropped: feature no longer exists");
                }
            }
        }
        if (c.nested) ImGui::Unindent(14.0f * theme::S());   // #4.3: balanced with the scaled Indent
        return res;
    }

    ImGuiTreeNodeFlags flags =
        ImGuiTreeNodeFlags_Leaf |
        ImGuiTreeNodeFlags_NoTreePushOnOpen |
        ImGuiTreeNodeFlags_SpanFullWidth;

    // Suppressed → dim heavily. Rolled back → dim + italic-ish look (we cannot truly italicize a
    // default font, so we use the lighter dim + the "(Rolled back)" cue in the badge/tooltip).
    bool dim = c.suppressed || c.rolledBack;
    if (dim) {
        float a = c.suppressed ? 0.5f : 0.62f;
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(fmPalette().textDim[0], fmPalette().textDim[1],
                   fmPalette().textDim[2], a));
    }

    // Label: dimmed-icon suppressed cue handled via the tree icon; keep a compact [S] too.
    std::string label = c.name;
    if (c.suppressed) label += " [S]";
    label += "##ft_" + fid;

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(3.0f * theme::S(), 3.0f * theme::S()));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 2.0f * theme::S()));
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, ImVec4(0, 0, 0, 0));
    ImGui::PushStyleColor(ImGuiCol_HeaderActive,  ImVec4(0, 0, 0, 0));
    ImGui::TreeNodeEx(label.c_str(), flags);
    ImGui::PopStyleColor(2);
    ImGui::PopStyleVar(2);

    if (dim) ImGui::PopStyleColor();

    // ---- per-feature eye hit-box (right edge of the row) ----
    // Computed from the SpanFullWidth node rect now; drawn + handled at the end of the row. A click
    // landing on the eye must NOT also select/double-click the row, so the classification below skips
    // when the cursor is over the eye.
    ImVec2 nodeMin = ImGui::GetItemRectMin();
    ImVec2 nodeMax = ImGui::GetItemRectMax();
    float  eyeSz   = 14.0f * theme::S();
    float  eyePad  = 6.0f  * theme::S();
    ImVec2 eyeMin(nodeMax.x - eyeSz - eyePad, nodeMin.y + (nodeMax.y - nodeMin.y - eyeSz) * 0.5f);
    ImVec2 eyeMax(eyeMin.x + eyeSz, eyeMin.y + eyeSz);
    bool   overEye = ImGui::IsMouseHoveringRect(eyeMin, eyeMax);

    // ---- click classification (defer the actual selection change to the caller) ----
    bool itemHovered = ImGui::IsItemHovered();
    res.hovered = itemHovered;
    if (ImGui::IsItemClicked(ImGuiMouseButton_Left) && !overEye) {
        if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left)) res.doubleClick = true;
        else if (ImGui::GetIO().KeyCtrl)                        res.ctrlClicked = true;
        else                                                    res.clicked = true;
    }

    // ---- drag-drop reorder (gated on canDoc) ----
    if (c.reorderCb && c.canDoc && ImGui::BeginDragDropSource(ImGuiDragDropFlags_None)) {
        ImGui::SetDragDropPayload("FEATURE_ROW", fid.c_str(), fid.size() + 1);
        ImGui::Text("Move %s", c.name.c_str());
        ImGui::EndDragDropSource();
    }
    if (c.reorderCb && c.canDoc && ImGui::BeginDragDropTarget()) {
        // C37-E (item 3): draw the insertion indicator in the GAP at the real insertion position —
        // top edge if the cursor is in the upper half (drop BEFORE this row), bottom edge if in the
        // lower half (drop AFTER → before the next row).
        ImVec2 rmin = ImGui::GetItemRectMin(), rmax = ImGui::GetItemRectMax();
        float  mid  = (rmin.y + rmax.y) * 0.5f;
        bool   after = ImGui::GetIO().MousePos.y > mid;
        float  ly = after ? rmax.y : rmin.y;
        ImGui::GetWindowDrawList()->AddLine(ImVec2(rmin.x, ly), ImVec2(rmax.x, ly),
                                            fmToU32(fmPalette().accent),
                                            std::max(1.0f, 2.5f * theme::S()));  // #4.5
        if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload("FEATURE_ROW")) {
            std::string dragged(static_cast<const char*>(pl->Data));
            if (dragged != fid) {
                // reorderCb takes a "before" id. Dropping in this row's UPPER half = insert before
                // this row (pass fid). LOWER half = insert after this row, which we encode with the
                // "@after:<fid>" sentinel so the caller resolves the following feature's id.
                c.reorderCb(dragged, after ? ("@after:" + fid) : fid);
            }
        }
        ImGui::EndDragDropTarget();
    }

    // Context menu — MUST come while the TreeNode row is still the last item:
    // BeginPopupContextItem opens on right-click of the LAST item, and featureBadge() below
    // emits its own SameLine text item. With the old order the menu only opened when the user
    // right-clicked the tiny status dot, i.e. effectively never (C40 fix).
    pushTreePopupGlass();
    if (ImGui::BeginPopupContextItem(("##ctx_" + fid).c_str())) {
        bool isSketch = c.iconName && std::strcmp(c.iconName, "sketch_node") == 0;
        bool multi    = s_multiSel.size() > 1 && s_multiSel.count(fid) > 0;
        if (c.selected && ImGui::MenuItem("Deselect"))
            clearTreeFeatureSelection(st);
        if (c.selected) ImGui::Separator();
        if (!c.canDoc) ImGui::BeginDisabled();   // gate every mutating item while an agent run is busy

        if (isSketch) {
            // A sketch gets "Edit Sketch" (enter the sketcher) + "Edit Sketch Plane" (re-pick the
            // plane it sits on) — NOT the generic "Edit" form, which doesn't apply to a sketch.
            if (ImGui::MenuItem("Edit Sketch")) {
                /* sketch ctx deferred */
            }
            if (ImGui::MenuItem("Edit Sketch Plane/Face")) {
                /* sketch ctx deferred */
            }
        } else if (c.editCallback && *c.editCallback) {
            if (ImGui::MenuItem("Edit")) (*c.editCallback)(fid);
        } else {
            ImGui::BeginDisabled(); ImGui::MenuItem("Edit"); ImGui::EndDisabled();
        }
        if (ImGui::MenuItem("Rename")) {
            s_renamingId = fid;
            std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", c.name.c_str());
        }
        if (ImGui::MenuItem("Remove from folder")) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "set_folder"}, {"id", fid}, {"folder", ""}}}), "set_folder");
        }

        // ---- Wave 4C: Copy / Cut / Paste ----
        // The copy set is the whole multi-selection when this row is part of it, else just this row.
        // Recorded as a deferred request (run after the tree walk) because the copy's DocMirror call
        // replaces the tree json this menu sits inside. Paste enables only when a feature bundle is
        // on the clipboard.
        ImGui::Separator();
        {
            std::vector<std::string> copySet;
            if (multi) for (const auto& id : s_multiSel) copySet.push_back(id);
            else copySet.push_back(fid);
            std::string copyLabel = multi
                ? ("Copy " + std::to_string((int)copySet.size()) + " features\tCtrl+C")
                : std::string("Copy\tCtrl+C");
            std::string cutLabel = multi
                ? ("Cut " + std::to_string((int)copySet.size()) + " features\tCtrl+X")
                : std::string("Cut\tCtrl+X");
            if (ImGui::MenuItem(copyLabel.c_str())) requestClipboard(ClipReq::Copy, copySet);
            if (ImGui::MenuItem(cutLabel.c_str()))  requestClipboard(ClipReq::Cut, copySet);
            bool canPaste = st.clipboard.kind == FmState::Clipboard::Kind::Feature &&
                            !st.clipboard.payloads.empty();
            if (!canPaste) ImGui::BeginDisabled();
            if (ImGui::MenuItem("Paste\tCtrl+V"))
                requestClipboard(ClipReq::Paste, std::vector<std::string>{});
            if (!canPaste) ImGui::EndDisabled();
        }

        ImGui::Separator();
        // C37-E (item 3): multi-selection applies Suppress/Unsuppress/Delete to ALL selected rows.
        if (multi) {
            int n = (int)s_multiSel.size();
            if (ImGui::MenuItem(("Suppress " + std::to_string(n) + " features").c_str())) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : s_multiSel)
                    ops.push_back({{"op", "set_suppressed"}, {"id", id}, {"value", true}});
                applyTreeOps(st, ops, "suppress");
            }
            if (ImGui::MenuItem(("Unsuppress " + std::to_string(n) + " features").c_str())) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : s_multiSel)
                    ops.push_back({{"op", "set_suppressed"}, {"id", id}, {"value", false}});
                applyTreeOps(st, ops, "unsuppress");
            }
            if (ImGui::MenuItem(("Delete " + std::to_string(n) + " features").c_str())) {
                nlohmann::json ops = nlohmann::json::array();
                for (const auto& id : s_multiSel)
                    ops.push_back({{"op", "delete_feature"}, {"id", id}, {"cascade", false}});
                applyTreeOps(st, ops, "delete");
            }
        } else {
            if (ImGui::MenuItem(c.suppressed ? "Unsuppress" : "Suppress")) {
                applyTreeOps(st, nlohmann::json::array({
                    {{"op", "set_suppressed"}, {"id", fid}, {"value", !c.suppressed}}}), "suppress");
            }
        }

        // "Convert to Solid Body": sew an imported mesh (STL/OBJ/PLY/3MF/glTF/3dm) into an OCCT solid
        // so it can be cut/filleted/combined. Shown only for an `import` feature still in mesh form
        // (as_solid not yet set). Flips the import's as_solid param; the rebuild re-sews it. (Engine:
        // edit_feature -> import recompiles via mesh_solidify.) Deferred via applyTreeOps like the rest.
        if (!multi) {
            const nlohmann::json& tree = st.app->docMirror.tree();
            if (tree.is_object() && tree.contains("features") && tree["features"].is_array()) {
                for (const auto& f : tree["features"]) {
                    if (!f.is_object() || f.value("id", std::string()) != fid) continue;
                    if (f.value("type", std::string()) == "import") {
                        const bool already = f.contains("params") && f["params"].is_object() &&
                                             f["params"].value("as_solid", false);
                        if (!already) {
                            ImGui::Separator();
                            if (ImGui::MenuItem("Convert to Solid Body")) {
                                applyTreeOps(st, nlohmann::json::array({
                                    {{"op", "edit_feature"}, {"id", fid},
                                     {"params", {{"as_solid", true}}}}}), "convert to solid");
                            }
                        }
                    }
                    break;
                }
            }
        }

        if (c.convertCb && c.convertLabel) {
            ImGui::Separator();
            if (ImGui::MenuItem(c.convertLabel)) c.convertCb();
        }

        // C37-E (item 3): Zoom to Feature / Rollback to Here / Roll to End / Appearance.
        ImGui::Separator();
        if (ImGui::MenuItem("Zoom to Feature")) zoomToFeature(st, fid);
        if (ImGui::MenuItem("Rollback to Here")) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "set_rollback"}, {"after", fid}}}), "rollback");
        }
        if (!c.barAtEnd) {
            if (ImGui::MenuItem("Roll to End")) {
                applyTreeOps(st, nlohmann::json::array({
                    {{"op", "set_rollback"}, {"after", nullptr}}}), "rollback");
            }
        }
        if (!c.firstOwnedBody.empty()) {
            if (ImGui::MenuItem("Appearance...")) {
                s_apprBody = c.firstOwnedBody;
                s_apprColor[0] = 0.71f; s_apprColor[1] = 0.76f; s_apprColor[2] = 0.82f;
                s_apprMetallic = 0.0f; s_apprRoughness = 0.5f;
                s_apprOpen = true;   // a sibling popup opens next frame (see drawFeatureTree)
            }
        }

        ImGui::Separator();
        if (ImGui::MenuItem("Delete")) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "delete_feature"}, {"id", fid}, {"cascade", false}}}), "delete");
        }
        if (!c.canDoc) ImGui::EndDisabled();
        ImGui::EndPopup();
    }
    popTreePopupGlass();

    // ---- per-feature eye toggle (every row, right edge) ----
    // Hidden body-owning features hide their bodies; hidden sketches/datums drop out of the ghost
    // overlay. The glyph dims when hidden, with a slash (the atlas carries one eye icon, not a pair).
    {
        bool shown = featureEyeShown(st, fid, c.ownedBodies);
        ImDrawList* eyeDl = ImGui::GetWindowDrawList();
        float a = shown ? (overEye ? 1.0f : 0.70f) : (overEye ? 0.55f : 0.28f);
        ImVec2 eyeC((eyeMin.x + eyeMax.x) * 0.5f, (eyeMin.y + eyeMax.y) * 0.5f);
        float  eyeHalf = (eyeMax.x - eyeMin.x) * 0.5f;
        icons::drawGlass(eyeDl, "hide_show_eye", eyeC, eyeHalf, fmIconTint(a), fmGlassHalo());
        if (!shown)
            eyeDl->AddLine(ImVec2(eyeMin.x, eyeMax.y), ImVec2(eyeMax.x, eyeMin.y),
                           ImGui::ColorConvertFloat4ToU32(
                               ImVec4(fmPalette().textDim[0], fmPalette().textDim[1],
                                      fmPalette().textDim[2], 0.9f)),
                           std::max(1.0f, 1.5f * theme::S()));
        if (overEye) {
            ImGui::SetTooltip(shown ? "Hide" : "Show");
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left))
                toggleFeatureEye(st, fid, c.ownedBodies, shown);
        }
    }

    featureBadge(c.fs.ok, !c.fs.error.empty(), c.fs.error, c.fs.dangling, c.rolledBack);

    // The status badge owns detailed status/error hover text. Keeping the row itself quiet avoids
    // tooltip cards covering selected rows while the user is trying to scan or drag the tree.

    if (c.nested) ImGui::Unindent(14.0f * theme::S());   // #4.3: balanced with the scaled Indent
    return res;
}

// ---- feature type -> icon name ----
// C24: every type maps to an atlas name that actually exists (see host/assets/icons/atlas.json),
// so the FeatureManager shows a per-type glyph and the old 'datum_plane'/'feature' unknown-icon
// warnings are gone.
static bool featureModeIsCut(const nlohmann::json* feature) {
    if (!feature || !feature->is_object()) return false;
    auto pIt = feature->find("params");
    if (pIt == feature->end() || !pIt->is_object()) return false;
    auto mIt = pIt->find("mode");
    if (mIt != pIt->end() && mIt->is_string())
        return mIt->get<std::string>() == "cut";
    auto opIt = pIt->find("operation");
    if (opIt != pIt->end() && opIt->is_string()) {
        std::string op = opIt->get<std::string>();
        return op == "cut" || op == "subtract";
    }
    return false;
}

static const char* featureIcon(const std::string& type, const nlohmann::json* feature = nullptr) {
    if (type == "sketch")           return "sketch_node";
    if (type == "extrude")          return featureModeIsCut(feature) ? "extrude_cut" : "extrude_boss";
    if (type == "revolve")          return featureModeIsCut(feature) ? "revolve_cut" : "revolve_boss";
    if (type == "sweep")            return featureModeIsCut(feature) ? "sweep_cut" : "sweep";
    if (type == "loft")             return featureModeIsCut(feature) ? "loft_cut" : "loft";
    if (type == "fillet")           return "fillet";
    if (type == "chamfer")          return "chamfer";
    if (type == "shell")            return "shell";
    if (type == "draft")            return "draft";
    if (type == "rib")              return "rib";
    if (type == "hole")             return "hole_wizard";
    if (type == "linear_pattern")   return "linear_pattern";
    if (type == "circular_pattern") return "circular_pattern";
    if (type == "mirror")           return "mirror";
    if (type == "plane" || type == "datum_plane") return "ref_plane";
    if (type == "axis" || type == "datum_axis")   return "ref_axis";
    if (type == "point" || type == "datum_point") return "ref_point";
    if (type == "import")           return "open";
    if (type == "script")           return "history";
    if (type == "helix")            return "curve_helix";
    if (type == "csys")             return "ref_csys";
    if (type == "boolean")          return "combine";
    if (type == "split")            return "split";
    // Sheet metal
    if (type == "sm_base_flange")   return "sm_base_flange";
    if (type == "sm_edge_flange")   return "sm_edge_flange";
    if (type == "sm_hem")           return "sm_hem";
    if (type == "sm_jog")           return "sm_jog";
    // Weldments
    if (type == "weld_member")      return "weld_member";
    if (type == "weld_gusset")      return "weld_gusset";
    if (type == "weld_end_cap")     return "weld_end_cap";
    // Surfacing — reuse closest solid icon where no dedicated surface glyph exists
    if (type == "surf_extrude")     return "extrude_boss";
    if (type == "surf_revolve")     return "revolve_boss";
    if (type == "surf_sweep")       return "sweep";
    if (type == "surf_loft")        return "loft";
    if (type == "surf_planar")      return "ref_plane";
    if (type == "surf_offset")      return "offset_ent";
    if (type == "surf_boundary")    return "boundary";
    if (type == "surf_fill")        return "boundary";
    if (type == "surf_trim")        return "trim";
    if (type == "surf_knit")        return "combine";
    if (type == "surf_thicken")     return "extrude_boss";
    if (type == "surf_ruled")       return "shell";
    if (type == "surf_untrim")      return "convert_ent";
    if (type == "surf_delete_face") return "subtract";
    if (type == "surf_replace_face")return "wrap";
    // Primitives (box/cylinder/sphere/cone/torus) and anything else: the generic part glyph.
    if (type == "box")              return "box_feature";
    if (type == "cylinder")         return "cylinder_feature";
    if (type == "sphere")           return "sphere_feature";
    if (type == "cone")             return "cone_feature";
    if (type == "torus")            return "torus_feature";
    return "part";
}
static const char* featureFallback(const std::string& type) {
    // Sketch
    if (type == "sketch")            return "[S]";
    // Solid operations (boss)
    if (type == "extrude")           return "[E]";
    if (type == "revolve")           return "[Rv]";
    if (type == "sweep")             return "[Sw]";
    if (type == "loft")              return "[Lt]";
    // Dress-up
    if (type == "fillet")            return "[F]";
    if (type == "chamfer")           return "[C]";
    if (type == "shell")             return "[Sh]";
    if (type == "draft")             return "[Dr]";
    if (type == "rib")               return "[Rb]";
    // Feature operations
    if (type == "hole")              return "[H]";
    if (type == "mirror")            return "[Mi]";
    if (type == "linear_pattern")    return "[LP]";
    if (type == "circular_pattern")  return "[CP]";
    // Datum
    if (type == "plane" || type == "datum_plane") return "[Pl]";
    if (type == "axis" || type == "datum_axis")   return "[Ax]";
    if (type == "point" || type == "datum_point") return "[Pt]";
    if (type == "csys")              return "[CS]";
    // Primitives
    if (type == "box")               return "[Bx]";
    if (type == "cylinder")          return "[Cy]";
    if (type == "sphere")            return "[Sp]";
    if (type == "cone")              return "[Co]";
    if (type == "torus")             return "[To]";
    // Curves
    if (type == "helix")             return "[Hx]";
    // Sheet metal
    if (type == "sm_base_flange")    return "[BF]";
    if (type == "sm_edge_flange")    return "[EF]";
    if (type == "sm_hem")            return "[Hm]";
    if (type == "sm_jog")            return "[Jg]";
    // Weldments
    if (type == "weld_member")       return "[WM]";
    if (type == "weld_gusset")       return "[WG]";
    if (type == "weld_end_cap")      return "[EC]";
    // Multi-body boolean
    if (type == "boolean")           return "[Bl]";
    // Misc
    if (type == "import")            return "[Im]";
    if (type == "script")            return "[Sc]";
    if (type == "split")             return "[Sl]";
    // Surfaces
    if (type == "surf_extrude")      return "[SE]";
    if (type == "surf_revolve")      return "[SR]";
    if (type == "surf_sweep")        return "[SS]";
    if (type == "surf_loft")         return "[SL]";
    if (type == "surf_planar")       return "[SP]";
    if (type == "surf_offset")       return "[SO]";
    if (type == "surf_ruled")        return "[SU]";
    if (type == "surf_boundary")     return "[SB]";
    if (type == "surf_fill")         return "[SF]";
    if (type == "surf_knit")         return "[SK]";
    if (type == "surf_thicken")      return "[ST]";
    if (type == "surf_trim")         return "[Sm]";
    if (type == "surf_untrim")       return "[Sn]";
    if (type == "surf_delete_face")  return "[Sd]";
    if (type == "surf_replace_face") return "[Sf]";
    return "[?]";
}

// Category color strip: thin left-edge accent by feature group.
// Groups: sketch, solid-add, solid-cut, dress-up, pattern, datum, sheet-metal, weldment, surface.
static ImU32 categoryColor(const std::string& type, bool isCut) {
    if (type == "sketch")
        return theme::featcat::sketch();   // soft blue
    if (isCut && (type == "extrude" || type == "revolve" ||
                  type == "sweep"   || type == "loft"))
        return theme::featcat::solidCut();   // orange-red (solid-cut)
    if (type == "extrude" || type == "revolve" || type == "sweep" || type == "loft" ||
        type == "box" || type == "cylinder" || type == "sphere" ||
        type == "cone" || type == "torus"   || type == "hole")
        return theme::featcat::solidAdd();   // teal accent (solid-add)
    if (type == "fillet" || type == "chamfer" || type == "shell" ||
        type == "draft"  || type == "rib")
        return theme::featcat::dressUp();  // purple (dress-up)
    if (type == "linear_pattern" || type == "circular_pattern" || type == "mirror")
        return theme::featcat::pattern();   // cyan (pattern)
    if (type == "plane" || type == "datum_plane" ||
        type == "axis"  || type == "point" || type == "csys")
        return theme::featcat::datum();  // slate-blue (datum)
    if (type.size() >= 3 && type[0]=='s' && type[1]=='m' && type[2]=='_')
        return theme::featcat::sheetMetal();   // amber (sheet-metal)
    if (type.size() >= 5 && type[0]=='w' && type[1]=='e' && type[2]=='l' &&
        type[3]=='d' && type[4]=='_')
        return theme::featcat::weldment();    // coral-red (weldment)
    if (type.size() >= 5 && type[0]=='s' && type[1]=='u' && type[2]=='r' &&
        type[3]=='f' && type[4]=='_')
        return theme::featcat::surface();   // green (surface)
    if (type == "boolean")
        return theme::featcat::booleanOp();  // soft purple (multi-body boolean)
    return theme::featcat::unknown();      // grey (unknown)
}

// ---- C20b: assembly tree (components + Mates folder) ----
// Renders an aicad.asm/1 doc: each component as a "(f)"-fixed-marked row (SolidWorks), then a
// Mates folder listing each mate as "Concentric1 (base<->peg)" with a suppress/delete context
// menu (asm_edit_mate / asm_delete_mate). Returns nothing; mutates the doc via DocMirror.
static void drawAssemblyTree(AppState& st, const nlohmann::json& tree,
                             const nlohmann::json& statusArr) {
    const auto& pal = fmPalette();

    // Map component id -> display name (for the mate captions).
    auto compName = [&](const std::string& cid) -> std::string {
        if (tree.contains("components") && tree["components"].is_array())
            for (const auto& c : tree["components"])
                if (c.is_object() && jfld(c, "id") == cid) {
                    std::string n = jfld(c, "name");
                    return n.empty() ? cid : n;
                }
        return cid;
    };

    auto reloadFrom = [&](const core::OpsResponse& r) {
        if (r.ok && !r.aicm_path.empty()) {
            std::string le; st.loadDocMesh(r.aicm_path, le);
            if (!le.empty()) st.addLog("[err] loadDocMesh: " + le);
        } else if (!r.ok) {
            st.addLog("[err] mate op: " + r.error_message);
        }
    };

    // ---- components ----
    int ci = 0;
    if (tree.contains("components") && tree["components"].is_array()) {
        for (const auto& c : tree["components"]) {
            if (!c.is_object()) { ++ci; continue; }
            std::string cid   = jfld(c, "id");
            std::string cname = jfld(c, "name");
            if (cname.empty()) cname = cid;
            bool fixed = jftbool(c, "fixed");
            bool suppressed = jftbool(c, "suppressed");
            // Component vs subassembly icon (we only have parts for now; subassemblies = C24).
            bool isAsm = false;
            if (c.contains("source") && c["source"].is_object()) {
                std::string p = jfld(c["source"], "path");
                std::string low = p; for (auto& ch : low) ch = (char)std::tolower((unsigned char)ch);
                isAsm = low.find(".asmstep") != std::string::npos;  // reserved; none today
            }
            FStatus fs = getFeatureStatus(statusArr, cid);

            fmRowPill();
            treeIcon(isAsm ? "assembly" : "part", isAsm ? "[A]" : "[P]", 16.0f);
            ImGui::SameLine(0, 4 * theme::S());   // #4.5
            // SolidWorks marks fixed components with a "(f)" prefix.
            std::string label = (fixed ? "(f) " : "") + cname;
            if (suppressed) label += " [S]";
            ImGuiTreeNodeFlags lf = ImGuiTreeNodeFlags_Leaf |
                ImGuiTreeNodeFlags_NoTreePushOnOpen | ImGuiTreeNodeFlags_SpanFullWidth;
            ImGui::TreeNodeEx((label + "##comp_" + cid).c_str(), lf);
            featureBadge(fs.ok, !fs.error.empty(), fs.error, fs.dangling, false, fs.warning);
            ++ci;
        }
    }

    ImGui::Separator();

    // ---- Mates folder ----
    fmRowPill();
    treeIcon("mates_folder", "[M]", 16.0f);
    ImGui::SameLine(0, 4 * theme::S());   // #4.5
    ImGuiTreeNodeFlags ff = ImGuiTreeNodeFlags_DefaultOpen | ImGuiTreeNodeFlags_SpanFullWidth;
    if (ImGui::TreeNodeEx("Mates##mates_folder", ff)) {
        if (tree.contains("mates") && tree["mates"].is_array()) {
            // Per-type running index so each mate gets a SolidWorks-style name "Concentric1".
            std::map<std::string, int> typeCount;
            int mi = 0;
            for (const auto& m : tree["mates"]) {
                if (!m.is_object()) { ++mi; continue; }
                std::string mid   = jfld(m, "id");
                std::string mtype = jfld(m, "type");
                bool msup = jftbool(m, "suppressed");
                int n = ++typeCount[mtype];
                std::string disp = mtype.empty() ? "Mate" : mtype;
                if (!disp.empty()) disp[0] = (char)std::toupper((unsigned char)disp[0]);
                disp += std::to_string(n);
                // "(base<->peg)" from the two refs' components.
                std::string pair;
                if (m.contains("refs") && m["refs"].is_array() && m["refs"].size() >= 2) {
                    std::string a = compName(jfld(m["refs"][0], "component"));
                    std::string b = compName(jfld(m["refs"][1], "component"));
                    pair = " (" + a + "<->" + b + ")";
                }
                std::string row = disp + pair;
                if (msup) row += " [S]";

                fmRowPill();
                treeIcon("mate", "[m]", 16.0f);
                ImGui::SameLine(0, 4 * theme::S());   // #4.5
                if (msup) ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.5f));
                ImGuiTreeNodeFlags mf = ImGuiTreeNodeFlags_Leaf |
                    ImGuiTreeNodeFlags_NoTreePushOnOpen | ImGuiTreeNodeFlags_SpanFullWidth;
                ImGui::TreeNodeEx((row + "##mate_" + mid).c_str(), mf);
                if (msup) ImGui::PopStyleColor();

                pushTreePopupGlass();
                if (ImGui::BeginPopupContextItem(("##mctx_" + mid).c_str())) {
                    // C25: deferred via applyTreeOps — applying here would invalidate the
                    // tree json this loop iterates (same UAF as feature reorder).
                    if (ImGui::MenuItem(msup ? "Unsuppress" : "Suppress")) {
                        applyTreeOps(st, nlohmann::json::array({
                            {{"op","asm_edit_mate"},{"id",mid},{"suppressed",!msup}}}), "mate");
                    }
                    ImGui::Separator();
                    if (ImGui::MenuItem("Delete")) {
                        applyTreeOps(st, nlohmann::json::array({
                            {{"op","asm_delete_mate"},{"id",mid}}}), "mate");
                    }
                    ImGui::EndPopup();
                }
                popTreePopupGlass();
                ++mi;
            }
            if (tree["mates"].empty()) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.7f));
                ImGui::TextUnformatted("  (no mates)");
                ImGui::PopStyleColor();
            }
        }
        ImGui::TreePop();
    }
}

// ---- C24: small icon-button used in the FeatureManager top toolbar (24px square) ----
// Returns true on click. Disabled buttons render dimmed and don't fire.
static bool toolbarIconBtn(const char* id, const char* iconName, const char* tooltip,
                           bool enabled) {
    float btn = 24.0f * theme::S();
    ImGui::PushID(id);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    // Always-on subtle pill; stronger wash on hover (draw-only — must not affect hit-test).
    theme::glassRowBackground(dl, p0, ImVec2(p0.x + btn, p0.y + btn), 3.0f, false, false, theme::rowWash::idle);
    bool clicked = ImGui::InvisibleButton("##tb", ImVec2(btn, btn)) && enabled;
    bool hov = ImGui::IsItemHovered();
    if (hov && enabled)
        theme::glassRowBackground(dl, p0, ImVec2(p0.x + btn, p0.y + btn), 3.0f, false, false, theme::rowWash::hover);
    float iz = 18.0f * theme::S(), off = (btn - iz) * 0.5f;
    ImU32 tint = enabled ? fmIconTint(1.0f) : fmIconTintDim(150.f / 255.f);
    icons::drawGlass(dl, iconName, ImVec2(p0.x + off + iz * 0.5f, p0.y + off + iz * 0.5f),
                     iz * 0.5f, tint, fmGlassHalo());
    if (hov && tooltip) ImGui::SetTooltip("%s", tooltip);
    ImGui::PopID();
    return clicked;
}

// ---- C41: real file Save / Open (native dialogs + viewport refresh) -------------------------
// fm_save = classic Save: silent re-save to s_docPath, dialog only on first save (or Shift+click
// = Save As). fm_open = native Open dialog -> DocMirror::open -> applyOpenedDoc, the one shared
// "apply a just-opened doc" seam that resets the viewport AND refreshes the geometry (the old
// path-strip open updated the tree but never loaded the mesh).

// Save filter: native AI-CAD part only (a silent Save / first-save always writes .step+).
// Save As writes the native .step+ ONLY — non-native formats live under File > Export (the Win32
// dialog appends the default extension regardless of the chosen filter, so a multi-format Save As
// silently wrote .step+ anyway).
static const char* kStepPlusFilter = "AI-CAD part (*.step+)\0*.step+\0All files (*.*)\0*.*\0";

// F4: Open/Import filter — every format the engine can import (see engine/doc/formats.py
// FORMAT_REGISTRY / doc_formats RPC, the future dynamic source). A leading aggregate entry lets
// the user see all CAD+mesh files at once, then grouped entries narrow by family, then All files.
// Win32 common-dialog layout: NUL-separated "label\0patterns\0"... pairs, double-NUL terminated
// (the C string literal's own trailing NUL supplies the final one).
static const char* kImportFilter =
    "All CAD & mesh files\0*.step+;*.step;*.stp;*.iges;*.igs;*.brep;*.stl;*.3mf;*.obj;*.ply;*.gltf;*.glb;*.3dm;*.sldprt;*.sldasm;*.x_t;*.x_b\0"
    "AI-CAD part (*.step+)\0*.step+\0"
    "STEP (*.step;*.stp)\0*.step;*.stp\0"
    "IGES (*.iges;*.igs)\0*.iges;*.igs\0"
    "BREP (*.brep)\0*.brep\0"
    "Mesh (*.stl;*.obj;*.ply;*.3mf;*.gltf;*.glb;*.3dm)\0*.stl;*.obj;*.ply;*.3mf;*.gltf;*.glb;*.3dm\0"
    // SolidWorks/Parasolid import is experimental (engine routes via SolidWorks COM -> STEP).
    "SolidWorks/Parasolid (*.sldprt;*.sldasm;*.x_t;*.x_b)\0*.sldprt;*.sldasm;*.x_t;*.x_b\0"
    "All files (*.*)\0*.*\0";

// Mesh import formats that can be sewn into a solid on import (engine doc_open `as_solid`). B-rep
// sources (.step/.iges/.brep/.step+) already import as solids, so they are NOT here and never prompt.
// When a mesh file is picked we ask the user "import as solid?" (ui::promptImportMeshAsSolid).
static bool isMeshImportFormat(const std::string& path) {
    size_t dot = path.find_last_of('.');
    if (dot == std::string::npos) return false;
    std::string ext = path.substr(dot);
    for (char& ch : ext) ch = (char)std::tolower((unsigned char)ch);
    return ext == ".stl" || ext == ".obj" || ext == ".ply" || ext == ".3mf" ||
           ext == ".gltf" || ext == ".glb" || ext == ".3dm";
}

static std::string pathBaseName(const std::string& p) {
    size_t cut = p.find_last_of("\\/");
    return cut == std::string::npos ? p : p.substr(cut + 1);
}

// Escape hatch: headless shot scenarios / CI can't click a native dialog. With
// AI_CAD_NO_FILE_DIALOGS set the bottom strip keeps the old raw-path InputText fallback and
// the dialog-needing paths refuse politely instead of blocking a headless run.
static bool fileDialogsDisabled() {
    static const bool disabled = std::getenv("AI_CAD_NO_FILE_DIALOGS") != nullptr;
    return disabled;
}

// Suggested file name for the first save: the doc's name (else "Part1") + ".step+".
static std::string suggestedSaveName(AppState& st) {
    std::string name = jfld(st.app->docMirror.tree(), "name");
    if (name.empty()) name = "Part1";
    if (name.find('.') == std::string::npos) name += ".step+";
    return name;
}

// Save the document. forceDialog = "Save As..." (always ask); otherwise the dialog only
// appears when no path is known yet (classic Save semantics).
static void doSaveDoc(AppState& st, bool forceDialog) {
    if (!st.app->docMirror.hasDoc()) return;
    st.addLog("[doc] save requested");
    std::string path = s_docPath;
    if (forceDialog || path.empty()) {
        if (fileDialogsDisabled()) {
            st.toast(ui::ToastLevel::Info,
                     "File dialogs disabled — use the path field below the tree");
            return;
        }
        std::string suggest = s_docPath.empty() ? suggestedSaveName(st)
                                                : pathBaseName(s_docPath);
        // Save / Save As always write the native .step+ (non-native formats are File > Export).
        path = platform::saveCadFile(suggest);
        if (path.empty()) return;  // canceled
    }

    std::string err;
    if (st.app->docMirror.save(st.app->worker, path, err)) {
        s_docPath = path;
        // Multi-document: a successful save clears the active tab's unsaved marker and records the
        // path so the tab tooltip / future Save reuses it.
        st.clearActiveTabDirty();
        if (st.app->activeTab >= 0 && st.app->activeTab < (int)st.app->docTabs.size())
            st.app->docTabs[st.app->activeTab].path = path;
        // doc_save embeds this part's open drawings into the .step+, so any Drawing tabs sourced from
        // it are now persisted too — clear their unsaved markers.
        st.clearDrawingDirtyForSource(st.app->docMirror.docId());
        st.addLog("[doc] saved: " + path);
        st.toast(ui::ToastLevel::Info, "Saved " + pathBaseName(path));
    } else {
        st.addLog("[err] save: " + err);
        st.toast(ui::ToastLevel::Error, "Save failed: " + err);
    }
}

// Apply a JUST-OPENED document (DocMirror::open already succeeded, so the mirror holds the new
// doc id / revision / tree). Factored out so BOTH open paths (native dialog and the
// AI_CAD_NO_FILE_DIALOGS path-strip fallback) take the identical route.
//
// Geometry refresh: the worker's doc_open response DOES carry a tess block, but
// DocMirror::open() discards it (doc_mirror.cpp parses only doc_id/revision/tree and is owned
// elsewhere), so we fetch the mesh with a forced doc_rebuild — async via st.app->rpc to keep the GL
// thread live. A forced rebuild does NOT bump the revision (engine/doc/session.py rebuild()),
// so the mirror stays in step. The callback runs on the GL thread (AsyncRpc::drain), so
// loadDocMesh / toast / resync are all safe there.
static void applyOpenedDoc(AppState& st, const std::string& path) {
    st.clearViewportForNewDoc();   // resets viewport + tree transients (clears s_docPath too)
    s_docPath = path;
    // Multi-document: an opened file lands in its own tab (DocMirror::open already pointed the
    // mirror at the new worker doc). The tab is born clean (just-loaded == saved) with its path.
    {
        bool isAsm = jfld(st.app->docMirror.tree(), "schema") == "aicad.asm/1";
        st.app->addDocTab(isAsm);
        if (st.app->activeTab >= 0 && st.app->activeTab < (int)st.app->docTabs.size()) {
            st.app->docTabs[st.app->activeTab].path = path;
            st.app->docTabs[st.app->activeTab].dirty = false;
        }
        // A real file is now open — discard the blank launch "starter" example if it's still
        // pristine (the user opened instead of modeling in it). Keeps the just-opened tab.
        st.dropBlankStarterTabs(st.app->activeTab);
    }
    st.addLog("[doc] opened: " + path + "  rev=" + std::to_string(st.app->docMirror.revision()));
    if (!st.handoffQuietOpen)   // silent on a live MCP-handoff refresh (would toast every build step)
        st.toast(ui::ToastLevel::Info, "Opened " + pathBaseName(path));
    // W12+ — re-open any drawings the engine restored from this .step+ (their `source` already
    // re-pointed to the just-opened part). Each becomes a Drawing tab ALONGSIDE the part tab (the
    // part stays active). Tolerant: an empty/absent list is a no-op (a file with no drawings).
    {
        const nlohmann::json& drws = st.app->docMirror.lastOpenedDrawings();
        if (drws.is_array() && !drws.empty()) {
            for (const auto& d : drws) {
                if (!d.is_object()) continue;
                st.adoptDrawingTab(jfld(d, "doc_id"), jfld(d, "name"), jfld(d, "source_doc_id"));
            }
            if (!st.handoffQuietOpen)
                st.toast(ui::ToastLevel::Info,
                         "Restored " + std::to_string(drws.size()) + " drawing(s)");
        }
    }
    std::string base = pathBaseName(path);
    // Multi-document: bind this open-refresh to the doc it targets so a tab switch before the async
    // result drains can't load THIS doc's mesh / tree into whatever doc is foreground by then.
    const std::string openDocId = st.app->docMirror.docId();
    st.app->rpc.call("doc_rebuild", {{"doc_id", openDocId}},
        [&st, base, openDocId](const core::RpcResult& r) {
            if (st.app->docMirror.docId() != openDocId) {
                st.addLog("[open] mesh refresh for " + openDocId + " ignored — active doc changed");
                return;
            }
            if (!r.ok || r.result.contains("code")) {
                std::string msg = !r.ok ? r.err : jfld(r.result, "message");
                st.addLog("[err] open mesh refresh: " + msg);
                st.toast(ui::ToastLevel::Warn,
                         "Opened " + base + " — mesh refresh failed: " + msg);
                return;
            }
            std::string aicm;
            if (r.result.contains("tess") && r.result["tess"].is_object())
                aicm = jfld(r.result["tess"], "aicm_path");
            if (!aicm.empty()) {
                std::string lerr;
                st.loadDocMesh(aicm, lerr);
                if (!lerr.empty())
                    st.addLog("[err] loadDocMesh: " + lerr);
            }
            // doc_open left the mirror without feature_status / owner_map (DocMirror::open
            // clears them); doc_tree is a cheap no-rebuild pull that restores both (status
            // badges + feature->face highlights) and fires the listener so the tree repaints.
            st.app->docMirror.resync(st.app->worker);
        }, 60000);
}

// Native-dialog Open: pick a .step+ file, open it in the worker, apply it.
static void doOpenDoc(AppState& st) {
    if (fileDialogsDisabled()) {
        st.toast(ui::ToastLevel::Info,
                 "File dialogs disabled — use the path field below the tree");
        return;
    }
    std::string path = platform::openCadFile();
    if (path.empty()) return;  // canceled
    // Mesh files: ask whether to import as a solid (vs. graphics mesh). Non-mesh formats never prompt.
    bool asSolid = false;
    if (isMeshImportFormat(path)) {
        int choice = promptImportMeshAsSolid();
        if (choice < 0) return;        // canceled
        asSolid = (choice == 1);
    }
    std::string err;
    if (st.app->docMirror.open(st.app->worker, path, err, asSolid)) {
        applyOpenedDoc(st, path);
    } else {
        st.addLog("[err] open: " + err);
        st.toast(ui::ToastLevel::Error, "Open failed: " + err);
    }
}

// ---- "Name & group" — SolidWorks-style auto-naming + folder grouping ----
// Renames every feature to a conventional per-type name ("Boss-Extrude1", "Fillet2", ...) and
// sorts dress-up / pattern / reference / sketch features into folders. One batched op set, one
// rebuild. (A future revision can route this through the AI agent for semantic names.)
static void nameAndGroupFeatures(AppState& st) {
    const nlohmann::json& tree = st.app->docMirror.tree();
    if (!tree.is_object()) return;
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array()) return;

    std::map<std::string, int> counts;
    nlohmann::json ops = nlohmann::json::array();

    for (const auto& f : *feat) {
        if (!f.is_object()) continue;
        std::string fid = jfld(f, "id");
        std::string ty  = jfld(f, "type");
        std::string mode;
        if (f.contains("params") && f["params"].is_object())
            mode = jfld(f["params"], "mode");
        bool cut = (mode == "cut");

        std::string base, folder;
        if      (ty == "sketch")           { base = "Sketch";              folder = "Sketches"; }
        else if (ty == "extrude")          { base = cut ? "Cut-Extrude" : "Boss-Extrude"; }
        else if (ty == "revolve")          { base = cut ? "Cut-Revolve" : "Boss-Revolve"; }
        else if (ty == "sweep")            { base = cut ? "Cut-Sweep"   : "Boss-Sweep"; }
        else if (ty == "loft")             { base = cut ? "Cut-Loft"    : "Boss-Loft"; }
        else if (ty == "fillet")           { base = "Fillet";   folder = "Dress-up"; }
        else if (ty == "chamfer")          { base = "Chamfer";  folder = "Dress-up"; }
        else if (ty == "shell")            { base = "Shell";    folder = "Dress-up"; }
        else if (ty == "draft")            { base = "Draft";    folder = "Dress-up"; }
        else if (ty == "rib")              { base = "Rib";      folder = "Dress-up"; }
        else if (ty == "hole")             { base = "Hole"; }
        else if (ty == "linear_pattern")   { base = "LPattern"; folder = "Patterns"; }
        else if (ty == "circular_pattern") { base = "CPattern"; folder = "Patterns"; }
        else if (ty == "mirror")           { base = "Mirror";   folder = "Patterns"; }
        else if (ty == "plane" || ty == "datum_plane") { base = "Plane"; folder = "Reference"; }
        else if (ty == "axis")             { base = "Axis";  folder = "Reference"; }
        else if (ty == "point")            { base = "Point"; folder = "Reference"; }
        else                                { base = ty.empty() ? "Feature" : ty; }

        int n = ++counts[base];
        std::string name = base + std::to_string(n);
        ops.push_back({{"op", "rename"}, {"id", fid}, {"name", name}});
        ops.push_back({{"op", "set_folder"}, {"id", fid}, {"folder", folder}});
    }
    if (!ops.empty()) {
        applyTreeOps(st, ops, "name & group");
        st.addLog("[tree] named & grouped " + std::to_string(ops.size() / 2) + " features");
    }
}

// ---- C37-E (item 4): the rollback bar widget ----
// Draws a thick grabbable horizontal line at the CURRENT cursor position (the caller places it right
// after the bar feature's row, or after the last row when the bar is at the end). The bar is a
// proper drag interaction: hovering shows a vertical-resize cursor + a brighter line; dragging it and
// releasing over a row gap enqueues set_rollback{after: <fid above the drop gap> | null}. It also
// remains a DROP TARGET for a dragged FEATURE_ROW (drop-to-end reorder, kept from C24).
//
// While dragging, the per-row hit-test in the feature walk records s_rbDropAfter (the feature the
// bar would land after); the walk's release handler turns it into set_rollback. `enabled` (canDoc)
// gates the mutating feature-row drop. `barAfterFid` (non-empty = the bar sits mid-history) also
// drives the "rolled back" zone label drawn below the bar.
static void drawRollbackBarLine(AppState& st, bool enabled, const std::string& barAfterFid) {
    const auto& pal = fmPalette();
    ImGui::Dummy(ImVec2(0, 2.0f * theme::S()));
    ImVec2 r0 = ImGui::GetCursorScreenPos();
    float  w  = ImGui::GetContentRegionAvail().x;
    float  rsc = theme::S();
    float  barH = 12.0f * rsc;          // #4.6: thicker grabbable band height
    // An invisible button across the whole band gives us hover + active (drag) state.
    ImGui::PushID("##rollback_bar");
    ImGui::InvisibleButton("##rb", ImVec2(w, barH));
    bool hovered = ImGui::IsItemHovered();
    bool active  = ImGui::IsItemActive();
    if (hovered || active) st.requestCursor(ui::CursorKind::ResizeV);
    if (active) s_rollbackDragging = true;

    ImDrawList* rdl = ImGui::GetWindowDrawList();
    float ly = r0.y + barH * 0.5f;
    ImU32 lineCol = fmToU32((hovered || active) ? pal.accent : pal.accentMid);
    rdl->AddLine(ImVec2(r0.x, ly), ImVec2(r0.x + w, ly), lineCol,
                 std::max(1.0f, (hovered || active) ? 3.0f * rsc : 2.0f * rsc));  // #4.6
    float hw = 28.0f * rsc, hh = 8.0f * rsc, hx = r0.x + (w - hw) * 0.5f;  // #4.6: bigger handle
    rdl->AddRectFilled(ImVec2(hx, ly - hh * 0.5f), ImVec2(hx + hw, ly + hh * 0.5f),
                       fmToU32(pal.accent), std::max(1.0f, 3.0f * rsc));  // #4.5
    if (hovered && !active)
        ImGui::SetTooltip("Rollback bar — drag up/down to roll the model back/forward,\n"
                          "or drop a feature here to move it to the end");

    // Drop target: a dragged feature row landing on the bar goes to the end (kept from C24).
    if (enabled && ImGui::BeginDragDropTarget()) {
        rdl->AddLine(ImVec2(r0.x, ly), ImVec2(r0.x + w, ly),
                     fmToU32(pal.accent), std::max(1.0f, 2.5f * rsc));  // #4.5
        if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload("FEATURE_ROW")) {
            std::string dragged(static_cast<const char*>(pl->Data));
            applyTreeOps(st, nlohmann::json::array(
                {{{"op", "reorder_feature"}, {"id", dragged}}}), "reorder");
        }
        ImGui::EndDragDropTarget();
    }
    // Bar mid-history (features below are rolled back): a dim centered label makes the divider's
    // meaning obvious — clearly not a plain separator. Skipped when the bar is at the end.
    if (!barAfterFid.empty()) {
        ImGui::Dummy(ImVec2(0, 3.0f * rsc));
        const char* rbLbl = "↓ rolled back";
        float rbW = ImGui::CalcTextSize(rbLbl).x;
        float rbX0 = ImGui::GetCursorPosX();
        ImGui::SetCursorPosX(rbX0 + std::max(0.0f, (w - rbW) * 0.5f));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.55f));
        ImGui::TextUnformatted(rbLbl);
        ImGui::PopStyleColor();
        ImGui::Dummy(ImVec2(0, 2.0f * rsc));
    }
    ImGui::PopID();
}

static char s_fmFilter[128] = {};

static bool fm_icontains(const char* hay, const char* needle) {
    if (!needle || !needle[0]) return true;
    if (!hay) return false;
    for (const char* h = hay; *h; ++h) {
        const char* ph = h, *pn = needle;
        while (*ph && *pn) {
            char a = (*ph >= 'A' && *ph <= 'Z') ? (char)(*ph + 32) : *ph;
            char b = (*pn >= 'A' && *pn <= 'Z') ? (char)(*pn + 32) : *pn;
            if (a != b) break;
            ++ph; ++pn;
        }
        if (!*pn) return true;
    }
    return false;
}

// ---- C24: FeatureManager top strip — a SolidWorks-style tab row (tree active; properties /
// configurations tabs are visual stubs) plus a compact document toolbar (New Part / New Assembly /
// Open / Save / Undo / Redo) that replaces the old wide text-button strip at the panel bottom.
static void drawFeatureManagerTopStrip(AppState& st) {
    const auto& pal = fmPalette();
    bool canDoc = st.app->workerStarted && !st.app->job.running.load();
    const float S = theme::S();

    // W5C: the old tree/Properties/Configurations stub tabs were removed here — they were visual
    // decoration that duplicated the real vertical tab strip in the LeftPane container (layout.cpp
    // drawLeftPane). This strip now hosts only the functional document toolbar + "Name & group".
    // --- centered document toolbar ---
    // glassRowAtCursor is draw-only and does not advance the layout cursor. Without an explicit
    // row reservation the full-width filter InputText below was placed at the same Y and stole
    // clicks from undo/redo (and the rest of the toolbar).
    const float bw = 24.0f * S, gap = 2.0f * S, nBtns = 6.0f;
    const float toolW = nBtns * bw + (nBtns - 1) * gap;
    ImVec2 toolRowMin = ImGui::GetCursorScreenPos();
    const float availW = ImGui::GetContentRegionAvail().x;
    theme::glassRowBackground(ImGui::GetWindowDrawList(), toolRowMin,
                           ImVec2(toolRowMin.x + availW, toolRowMin.y + bw),
                           3.5f, false, false, theme::rowWash::idle);
    float bx = toolRowMin.x + std::max(0.0f, (availW - toolW) * 0.5f);
    const float by = toolRowMin.y;
    auto toolBtn = [&](const char* id, const char* icon, const char* tip, bool en) -> bool {
        ImGui::SetCursorScreenPos(ImVec2(bx, by));
        bool hit = toolbarIconBtn(id, icon, tip, en);
        bx += bw + gap;
        return hit;
    };
    ImGui::PushID("fm_toolbar");
    if (toolBtn("fm_new", "new_doc", "New Part", canDoc) && canDoc) {
        std::string err;
        // Multi-document: New Part opens a NEW tab (it does not replace the current doc). The
        // worker registry keeps the previous doc alive; addDocTab records this one and activates it.
        st.app->suppressTabDirty = true;
        bool ok = st.app->docMirror.newPart(st.app->worker, err);
        st.app->suppressTabDirty = false;
        if (ok) {
            st.addLog("[doc] new part  rev=" + std::to_string(st.app->docMirror.revision()));
            st.clearViewportForNewDoc();   // C34: fresh part = clean viewport (planes state)
            st.app->addDocTab(/*isAssembly=*/false);
        }
        else st.addLog("[err] new part: " + err);
    }
    if (toolBtn("fm_newasm", "assembly", "New Assembly", canDoc) && canDoc) {
        std::string err;
        st.app->suppressTabDirty = true;
        bool ok = st.app->docMirror.newAssembly(st.app->worker, err);
        st.app->suppressTabDirty = false;
        if (ok) {
            st.addLog("[doc] new assembly  rev=" + std::to_string(st.app->docMirror.revision()));
            st.clearViewportForNewDoc();   // C34
            st.app->addDocTab(/*isAssembly=*/true);
            // ribbon tab switch deferred (G2)
        } else st.addLog("[err] new assembly: " + err);
    }
    if (toolBtn("fm_open", "open", "Open...", canDoc) && canDoc)
        doOpenDoc(st);   // C41: native dialog -> DocMirror::open -> viewport refresh
    bool canSave = canDoc && st.app->docMirror.hasDoc();
    if (toolBtn("fm_save", "save", "Save  (Shift-click: Save As...)", canSave) && canSave)
        doSaveDoc(st, /*forceDialog=*/ImGui::GetIO().KeyShift);   // C41
    // W1-B (item 4): route through the shared AppState helper so the toolbar buttons and the global
    // Ctrl+Z / Ctrl+Y keys take the identical path (mesh refresh + owner map + confirmation toast).
    if (toolBtn("fm_undo", "undo", "Undo", canDoc) && canDoc) st.docUndo();
    if (toolBtn("fm_redo", "redo", "Redo", canDoc) && canDoc) st.docRedo();
    ImGui::PopID();
    ImGui::SetCursorScreenPos(ImVec2(toolRowMin.x, toolRowMin.y + bw));
    ImGui::Dummy(ImVec2(availW, 0));

    // --- "Name & group" — auto-name features SolidWorks-style and sort into folders. ---
    // Shown only for a part doc that actually has features (not assemblies / empty docs).
    bool isPart = st.app->docMirror.hasDoc() &&
                  jfld(st.app->docMirror.tree(), "schema") != "aicad.asm/1";
    int featCount = 0;
    if (isPart) {
        const nlohmann::json& tr = st.app->docMirror.tree();
        auto fa = tr.find("features");
        if (fa != tr.end() && fa->is_array()) featCount = (int)fa->size();
    }
    if (isPart && featCount > 0) {
        ImGui::Spacing();
        bool can = canDoc;
        if (!can) ImGui::BeginDisabled();
        {
            const float rowH = ImGui::GetFrameHeight();
            ImVec2 rowMin = ImGui::GetCursorScreenPos();
            float rowW = ImGui::GetContentRegionAvail().x;
            theme::glassRowBackground(ImGui::GetWindowDrawList(), rowMin,
                                   ImVec2(rowMin.x + rowW, rowMin.y + rowH),
                                   3.5f, true, false, theme::rowWash::idle);
        }
        ImGui::PushStyleColor(ImGuiCol_Button, fmToU32(pal.accentSoft));
        if (ImGui::Button("Name & group features", ImVec2(-1, 0)) && can)
            nameAndGroupFeatures(st);
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Auto-name every feature (Boss-Extrude1, Fillet2, ...) and\n"
                              "group dress-up / patterns / reference geometry into folders.");
        if (!can) ImGui::EndDisabled();
    }

    ImGui::Spacing();
    {
        const float filterH = ImGui::GetFrameHeight();
        ImVec2 fMin = ImGui::GetCursorScreenPos();
        const float fW = ImGui::GetContentRegionAvail().x;
        theme::glassRowBackground(ImGui::GetWindowDrawList(), fMin,
                               ImVec2(fMin.x + fW, fMin.y + filterH),
                               3.5f, false, false, theme::rowWash::idle);
        ImGui::SetCursorScreenPos(fMin);
        ImGui::SetNextItemWidth(fW);
        ImGui::InputTextWithHint("##fm_filter", "Filter features...", s_fmFilter, sizeof(s_fmFilter));
        ImGui::SetCursorScreenPos(ImVec2(fMin.x, fMin.y + filterH));
        ImGui::Dummy(ImVec2(0, 0));
    }

    ImGui::Separator();
}

// ---- main draw function ----

// Wave 1D: the tree body WITHOUT Begin/End — the caller (drawFeatureTree wrapper, or the Wave 1D
// drawLeftPane container) owns the surrounding window/child. Everything below was the interior of
// the former drawFeatureTree(); the only change is the removed Begin/End at the top and bottom.
static void drawFeatureTreeBodyImpl(AppState& st,
                         const std::function<void(const std::string&)>& editCallback) {
    const auto& pal = fmPalette();
    s_fmStripeIdx = 0;

    // C24: SolidWorks-style top strip (tab row + compact doc toolbar), replacing the old wide
    // New Part / Undo / Redo text buttons that used to live at the panel bottom.
    drawFeatureManagerTopStrip(st);

    // ---- document title ----
    const nlohmann::json& tree = st.app->docMirror.tree();
    std::string docName = "Part1";
    if (tree.is_object()) docName = jfld(tree, "name");
    if (docName.empty()) docName = "Part1";

    // C37-E (item 2): the SAME guard the top toolbar uses (st.app->workerStarted && !st.app->job.running).
    // Every mutating tree interaction — context menu, drag-reorder, Ctrl+Up/Down, rollback drag —
    // is gated on this so no op fires while an agent run is in flight.
    bool canDocTree = st.app->workerStarted && !st.app->job.running.load();

    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(pal.text[0], pal.text[1], pal.text[2], pal.text[3]));
    {
        ImGuiTreeNodeFlags rootFlags =
            ImGuiTreeNodeFlags_DefaultOpen |
            ImGuiTreeNodeFlags_SpanFullWidth;
        fmRowPill();
        bool rootOpen = ImGui::TreeNodeEx(docName.c_str(), rootFlags);

        // C26: root-row "Appearance…" — sets the primary body's color (the common single-body
        // case). Body key "b1" matches the engine's per-body appearance key (session._write_aicm).
        // Routed through the DEFERRED applyTreeOps queue, like every other tree-originated op.
        {
            bool isAsmDoc = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
            static float s_rootColor[3] = {0.71f, 0.76f, 0.82f};
            static float s_rootMetallic = 0.0f, s_rootRoughness = 0.5f;  // C32-A material
            static bool  s_openRootPicker = false;
            if (!isAsmDoc && st.app->docMirror.hasDoc()) {
                pushTreePopupGlass();
                if (ImGui::BeginPopupContextItem("##root_ctx")) {
                    if (ImGui::MenuItem("Appearance...")) {
                        s_rootColor[0] = 0.71f; s_rootColor[1] = 0.76f; s_rootColor[2] = 0.82f;
                        s_rootMetallic = 0.0f; s_rootRoughness = 0.5f;
                        s_openRootPicker = true;  // open the picker (a sibling popup) next
                    }
                    if (ImGui::MenuItem("Clear Appearance")) {
                        applyTreeOps(st, nlohmann::json::array({
                            {{"op", "set_appearance"}, {"id", "b1"}, {"color", nullptr}}}),
                            "set_appearance");
                    }
                    ImGui::EndPopup();
                }
                popTreePopupGlass();
                // Picker popup lives OUTSIDE the context menu (a sibling) to avoid nested-popup
                // ordering pitfalls; it is opened by the menu item via s_openRootPicker.
                if (s_openRootPicker) { ImGui::OpenPopup("##appearance_root"); s_openRootPicker = false; }
                pushTreePopupGlass();
                if (appearanceColorPicker("##appearance_root", s_rootColor,
                                          &s_rootMetallic, &s_rootRoughness)) {
                    applyTreeOps(st, nlohmann::json::array({
                        {{"op", "set_appearance"}, {"id", "b1"},
                         {"color", {s_rootColor[0], s_rootColor[1], s_rootColor[2]}},
                         {"metallic", s_rootMetallic}, {"roughness", s_rootRoughness}}}),
                        "set_appearance");
                }
                popTreePopupGlass();
            }
        }

        if (rootOpen) {

            // ---- fixed header nodes ----
            // Equations
            {
                int eqCount = 0;
                if (tree.is_object()) {
                    auto eq = tree.find("equations");
                    if (eq != tree.end() && eq->is_object()) {
                        auto gl = eq->find("globals");
                        if (gl != eq->end() && gl->is_array())
                            eqCount = (int)gl->size();
                    }
                }
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_Leaf |
                                       ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth;
                char eqLabel[64];
                std::snprintf(eqLabel, sizeof(eqLabel), "Equations (%d)##eqhdr", eqCount);
                fmRowPill();
                treeIcon("equations", "[=]", 16.0f);
                ImGui::SameLine(0, 4 * theme::S());   // #4.5
                ImGui::TreeNodeEx(eqLabel, f);
                // Double-click the node to open the Equations / Global Variables editor (SolidWorks).
                if (ImGui::IsItemHovered() && ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Left))
                    st.showEquations = true;
            }

            // Material
            {
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_Leaf |
                                       ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth;
                std::string matKey;
                if (tree.is_object()) {
                    auto m = tree.find("material");
                    if (m != tree.end() && m->is_object()) {
                        std::string key = jfld(*m, "key");
                        if (!key.empty()) matKey = key;
                    }
                }
                fmRowPill();
                treeIcon("material", "[M]", 16.0f);
                ImGui::SameLine(0, 4 * theme::S());   // #4.5
                ImGui::TreeNodeEx("##mat_hdr", f | ImGuiTreeNodeFlags_NoTreePushOnOpen);
                // W2 (material UX): right-click the Material node to assign a PHYSICAL material.
                // Each choice (a) sets the part's material key (set_material) — read by FEA studies
                // and shown on this row — and (b) recolours every body to the material's look via a
                // derived set_appearance (the material->appearance map in appearance_common). An
                // Appearance-tab edit applied later still wins (it is a later set_appearance). Ops are
                // DEFERRED through applyTreeOps like every other tree-originated op (UAF-safe).
                {
                    bool matAsm = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
                    if (!matAsm && st.app->docMirror.hasDoc()) {
                        pushTreePopupGlass();
                        if (ImGui::BeginPopupContextItem("##mat_ctx")) {
                            ImGui::TextDisabled("Assign Material");
                            ImGui::Separator();
                            // The set_appearance keys are AICM body ordinals ("b<n>"); recolour every
                            // body so a multi-body part takes the material wholesale. Empty body list
                            // (mesh not loaded yet) falls back to "b1", the common single-body case.
                            std::vector<uint32_t> bodies = st.docBodyIds;
                            if (bodies.empty()) bodies.push_back(1);
                            for (int i = 0; i < kPhysicalMaterialsCount; ++i) {
                                const auto& pm = kPhysicalMaterials[i];
                                bool cur = (matKey == pm.key);
                                if (ImGui::MenuItem(pm.name, nullptr, cur)) {
                                    float col[3]; float mtl = 0.0f, rgh = 0.5f;
                                    materialAppearance(pm.key, col, mtl, rgh);
                                    nlohmann::json ops = nlohmann::json::array();
                                    ops.push_back({{"op", "set_material"}, {"key", pm.key}});
                                    for (uint32_t bid : bodies)
                                        ops.push_back({{"op", "set_appearance"},
                                                       {"id", "b" + std::to_string(bid)},
                                                       {"color", {col[0], col[1], col[2]}},
                                                       {"metallic", mtl}, {"roughness", rgh}});
                                    applyTreeOps(st, ops, "set_material");
                                }
                            }
                            if (!matKey.empty()) {
                                ImGui::Separator();
                                if (ImGui::MenuItem("Remove Material")) {
                                    applyTreeOps(st, nlohmann::json::array({
                                        {{"op", "set_material"}, {"key", nullptr}}}),
                                        "set_material");
                                }
                            }
                            ImGui::EndPopup();
                        }
                        popTreePopupGlass();
                    }
                }
                ImGui::SameLine();
                ImGui::TextUnformatted("Material");
                // Show the friendly material name (kPhysicalMaterials) when the assigned key is one
                // we know; fall back to the raw engine key for any other (custom / MCP-set) material.
                const char* matLabel = matKey.c_str();
                for (int i = 0; i < kPhysicalMaterialsCount; ++i)
                    if (matKey == kPhysicalMaterials[i].key) { matLabel = kPhysicalMaterials[i].name; break; }
                if (!matKey.empty()) {
                    ImGui::SameLine();
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1.0f));
                    ImGui::TextUnformatted(matLabel);
                    ImGui::PopStyleColor();
                }
                if (ImGui::IsItemHovered()) {
                    if (matKey.empty()) ImGui::SetTooltip("No material assigned — right-click to assign");
                    else ImGui::SetTooltip("Material: %s", matLabel);
                }
            }

            // ---- Solid Bodies folder (per-body hide/show, SolidWorks-style) ----
            // Part docs only, when the loaded doc mesh has at least one body. One row per
            // renderer body slot: click toggles visibility (same interaction as the plane rows
            // below); the context menu adds Hide/Show, Isolate (when every OTHER body is already
            // hidden it acts as SolidWorks Exit Isolate = Show All), and Show All Bodies. Pure
            // host-side state — see setBodyShown above.
            {
                bool isAsmDoc = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
                int nBodies = (int)st.bodyVisible.size();
                if (!isAsmDoc && st.hasDocMesh && nBodies >= 1) {
                    char hdr[64];
                    std::snprintf(hdr, sizeof(hdr), "Solid Bodies (%d)##solid_bodies", nBodies);
                    fmRowPill();
                    treeIcon("body_folder", "[F]", 16.0f);   // #4.4: was borrowed "mates_folder"
                    ImGui::SameLine(0, 4 * theme::S());   // #4.5
                    bool open = ImGui::TreeNodeEx(hdr, ImGuiTreeNodeFlags_DefaultOpen |
                                                       ImGuiTreeNodeFlags_SpanFullWidth);
                    if (open) {
                        for (int bi = 0; bi < nBodies; ++bi) {
                            bool hidden = !st.bodyVisible[bi];
                            uint32_t bid = bi < (int)st.docBodyIds.size()
                                               ? st.docBodyIds[bi] : (uint32_t)bi + 1;
                            char bname[32];
                            std::snprintf(bname, sizeof(bname), "Body%u", bid);
                            ImGuiTreeNodeFlags bf = ImGuiTreeNodeFlags_Leaf |
                                                    ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                                    ImGuiTreeNodeFlags_SpanFullWidth;
                            fmRowPill();
                            treeIcon("part", "[B]", 16.0f, hidden ? 0.45f : 1.0f);
                            ImGui::SameLine(0, 4 * theme::S());   // #4.5
                            if (hidden)
                                ImGui::PushStyleColor(ImGuiCol_Text,
                                    ImVec4(pal.textDim[0], pal.textDim[1],
                                           pal.textDim[2], 0.55f));
                            char rowId[48];
                            std::snprintf(rowId, sizeof(rowId), "%s##body%d", bname, bi);
                            ImGui::TreeNodeEx(rowId, bf);
                            if (hidden) ImGui::PopStyleColor();
                            // Capture row interactions NOW; BeginPopupContextItem binds to the
                            // LAST item, so it must come right after TreeNodeEx, before any
                            // other widget.
                            bool rowClicked = ImGui::IsItemClicked();
                            bool rowHovered = ImGui::IsItemHovered();
                            char ctxId[32];
                            std::snprintf(ctxId, sizeof(ctxId), "##body_ctx%d", bi);
                            pushTreePopupGlass();
                            if (ImGui::BeginPopupContextItem(ctxId)) {
                                if (ImGui::MenuItem(hidden ? "Show" : "Hide"))
                                    setBodyShown(st, bi, hidden);
                                if (ImGui::MenuItem("Isolate")) {
                                    bool othersAllHidden = true;
                                    for (int j = 0; j < nBodies; ++j)
                                        if (j != bi && st.bodyVisible[j]) {
                                            othersAllHidden = false; break;
                                        }
                                    if (othersAllHidden) {
                                        // Already isolated -> Exit Isolate = plain Show All.
                                        std::fill(st.bodyVisible.begin(),
                                                  st.bodyVisible.end(), (uint8_t)1);
                                        st.applyBodyVisibility();
                                    } else {
                                        for (int j = 0; j < nBodies; ++j)
                                            if (j != bi && st.bodyVisible[j])
                                                setBodyShown(st, j, false);
                                        if (!st.bodyVisible[bi]) setBodyShown(st, bi, true);
                                    }
                                }
                                if (ImGui::MenuItem("Show All Bodies")) {
                                    std::fill(st.bodyVisible.begin(),
                                              st.bodyVisible.end(), (uint8_t)1);
                                    st.applyBodyVisibility();
                                }
                                ImGui::EndPopup();
                            }
                            popTreePopupGlass();
                            if (rowClicked) setBodyShown(st, bi, hidden);
                            if (rowHovered)
                                ImGui::SetTooltip(hidden ? "Show %s" : "Hide %s", bname);
                        }
                        ImGui::TreePop();
                    }
                }
            }
            // Section rule: reference geometry (Origin + planes) follows.
            treeSectionDivider("Reference");

            // Origin — expandable; the three default planes nest underneath (no duplicate flat list).
            {
                ImGuiTreeNodeFlags f = ImGuiTreeNodeFlags_DefaultOpen |
                                       ImGuiTreeNodeFlags_SpanFullWidth;
                fmRowPill();
                treeIcon("origin", "[O]", 16.0f);
                ImGui::SameLine(0, 4 * theme::S());
                bool originOpen = ImGui::TreeNodeEx("Origin##origin", f);
                if (originOpen) {
                    static const char* kPlanes[] = {"Front Plane", "Top Plane", "Right Plane"};
                    bool planesAutoShow = !st.hasDocMesh && st.app->docMirror.hasDoc() && !st.planesUserToggled;
                    auto takePlaneControl = [&st, planesAutoShow]() {
                        if (st.planesUserToggled) return;
                        if (planesAutoShow)
                            st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = true;
                        st.planesUserToggled = true;
                    };
                    for (int pi = 0; pi < 3; ++pi) {
                        ImGuiTreeNodeFlags pf = ImGuiTreeNodeFlags_Leaf |
                                                ImGuiTreeNodeFlags_NoTreePushOnOpen |
                                                ImGuiTreeNodeFlags_SpanFullWidth;
                        if (st.planeEmphasis == pi) pf |= ImGuiTreeNodeFlags_Selected;
                        bool planeShownEff = st.showPlane[pi] || planesAutoShow;
                        bool hidden = !planeShownEff;
                        treeIcon("ref_plane", "[P]", 16.0f, hidden ? 0.45f : 1.0f);
                        ImGui::SameLine(0, 4 * theme::S());
                        if (hidden)
                            ImGui::PushStyleColor(ImGuiCol_Text,
                                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 0.55f));
                        fmRowPill(st.planeEmphasis == pi);
                        ImGui::TreeNodeEx((std::string(kPlanes[pi]) + "##plane" + std::to_string(pi)).c_str(), pf);
                        if (hidden) ImGui::PopStyleColor();
                        ImVec2 pNodeMin = ImGui::GetItemRectMin();
                        ImVec2 pNodeMax = ImGui::GetItemRectMax();
                        float  pEyeSz = 14.0f * theme::S(), pEyePad = 6.0f * theme::S();
                        ImVec2 pEyeMin(pNodeMax.x - pEyeSz - pEyePad,
                                       pNodeMin.y + (pNodeMax.y - pNodeMin.y - pEyeSz) * 0.5f);
                        ImVec2 pEyeMax(pEyeMin.x + pEyeSz, pEyeMin.y + pEyeSz);
                        bool   pOverEye = ImGui::IsMouseHoveringRect(pEyeMin, pEyeMax);
                        {
                            ImDrawList* pdl = ImGui::GetWindowDrawList();
                            float a = !hidden ? (pOverEye ? 1.0f : 0.70f) : (pOverEye ? 0.55f : 0.28f);
                            ImVec2 pEyeC((pEyeMin.x + pEyeMax.x) * 0.5f, (pEyeMin.y + pEyeMax.y) * 0.5f);
                            float  pEyeHalf = (pEyeMax.x - pEyeMin.x) * 0.5f;
                            icons::drawGlass(pdl, "hide_show_eye", pEyeC, pEyeHalf, fmIconTint(a), fmGlassHalo());
                            if (hidden)
                                pdl->AddLine(ImVec2(pEyeMin.x, pEyeMax.y), ImVec2(pEyeMax.x, pEyeMin.y),
                                             ImGui::ColorConvertFloat4ToU32(ImVec4(pal.textDim[0],
                                                 pal.textDim[1], pal.textDim[2], 0.9f)),
                                             std::max(1.0f, 1.5f * theme::S()));
                            if (pOverEye) {
                                ImGui::SetTooltip(hidden ? "Show %s" : "Hide %s", kPlanes[pi]);
                                if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                                    static const char* kPlaneEyeId[] = {"plane.front","plane.top","plane.right"};
                                    takePlaneControl();
                                    st.showPlane[pi] = hidden;
                                    if (!st.showPlane[pi]) {
                                        if (st.planeEmphasis == pi) st.planeEmphasis = -1;
                                        if (st.selectedFeatureId == kPlaneEyeId[pi]) st.selectedFeatureId.clear();
                                    }
                                }
                            }
                        }
                        if (!pOverEye && ImGui::IsItemClicked()) {
                            if (st.sketchPlanePickActive) {
                                st.sketchPlanePickActive = false;
                                static const char* kPlaneSelId[] = {"plane.front", "plane.top", "plane.right"};
                                takePlaneControl();
                                st.showPlane[pi] = true;
                                st.planeEmphasis = pi;
                                st.app->selection.clear();
                                st.selectedFeatureId = kPlaneSelId[pi];
                            } else {
                                static const char* kPlaneSelId[] = {"plane.front", "plane.top", "plane.right"};
                                takePlaneControl();
                                bool wasSel = (st.selectedFeatureId == kPlaneSelId[pi]);
                                if (wasSel && planeShownEff) {
                                    st.showPlane[pi] = false;
                                    st.planeEmphasis = -1;
                                    st.selectedFeatureId.clear();
                                } else {
                                    st.showPlane[pi] = true;
                                    st.planeEmphasis = pi;
                                    st.app->selection.clear();
                                    st.selectedFeatureId = kPlaneSelId[pi];
                                }
                            }
                        }
                        if (ImGui::IsItemHovered() && !pOverEye)
                            ImGui::SetTooltip(st.sketchPlanePickActive ? "Sketch on %s"
                                              : ((false)
                                                     ? "Dimension to %s"
                                                     : (hidden ? "Show %s" : "Hide %s")), kPlanes[pi]);
                    }
                    ImGui::TreePop();
                }
            }

            treeSectionDivider("Features");

            // ---- feature history ----
            const nlohmann::json& statusArr = st.app->docMirror.featureStatus();
            std::string& selectedFeatureId = st.selectedFeatureId;

            // C20b: assemblies render components + a Mates folder instead of a feature history.
            bool isAssembly = tree.is_object() && jfld(tree, "schema") == "aicad.asm/1";
            if (isAssembly) {
                drawAssemblyTree(st, tree, statusArr);
            } else
            if (tree.is_object()) {
                auto feat = tree.find("features");
                if (feat != tree.end() && feat->is_array()) {
                    // C37-E (item 2): one status map per frame (kills the per-row O(N^2) scan).
                    std::unordered_map<std::string, FStatus> statusMap = buildStatusMap(statusArr);

                    // Ordered id list (drives selection validation, Ctrl+Up/Down, rollback math).
                    std::vector<std::string> order;
                    order.reserve(feat->size());
                    for (const auto& f : *feat)
                        if (f.is_object()) order.push_back(jfld(f, "id"));

                    // C37-E (item 2): validate the selection/multi-set against the live tree; drop
                    // anything that vanished (a delete/reorder/undo can orphan them) and clear the
                    // viewport highlight if the primary is gone.
                    {
                        std::unordered_set<std::string> live(order.begin(), order.end());
                        // The three reserved ORIGIN-plane ids are valid selections (SolidWorks
                        // "select a plane, then Sketch") but are NOT features, so they must be exempt
                        // from this live-feature prune or a plane selection would be wiped next frame.
                        bool isOriginPlane = selectedFeatureId == "plane.front" ||
                                             selectedFeatureId == "plane.top"   ||
                                             selectedFeatureId == "plane.right";
                        if (!selectedFeatureId.empty() && !live.count(selectedFeatureId) &&
                            !isOriginPlane) {
                            selectedFeatureId.clear();
                            clearFeatureHighlight(st);
                        }
                        for (auto it = s_multiSel.begin(); it != s_multiSel.end();)
                            it = live.count(*it) ? std::next(it) : s_multiSel.erase(it);
                    }

                    // C37-E (item 4): rollback bar position. `rollback_after` = the feature the bar
                    // sits AFTER (null/absent = bar at end). Everything after it is "rolled back".
                    std::string rollbackAfter;
                    {
                        auto rb = tree.find("rollback_after");
                        if (rb != tree.end() && rb->is_string()) rollbackAfter = rb->get<std::string>();
                    }
                    int barPos = (int)order.size() - 1;   // index of the feature the bar sits after
                    if (!rollbackAfter.empty()) {
                        for (int i = 0; i < (int)order.size(); ++i)
                            if (order[i] == rollbackAfter) { barPos = i; break; }
                    }
                    bool barAtEnd = rollbackAfter.empty() || barPos >= (int)order.size() - 1;

                    // C37-E (item 5): sketch nesting. A sketch consumed by EXACTLY ONE downstream
                    // feature (via a region ref {"kind":"region","sketch":"fX"} or positions_sketch)
                    // nests under that consumer. Unconsumed / multiply-consumed sketches stay flat.
                    std::unordered_map<std::string, int> consumeCount;   // sketch fid -> #consumers
                    std::unordered_map<std::string, std::string> consumer;  // sketch fid -> consumer
                    std::unordered_map<std::string, std::vector<std::string>> childrenOf;  // consumer -> sketches
                    {
                        std::unordered_set<std::string> sketchIds;
                        for (const auto& f : *feat)
                            if (f.is_object() && jfld(f, "type") == "sketch")
                                sketchIds.insert(jfld(f, "id"));
                        // Recursively scan a feature's refs for sketch references. A sketch is
                        // "consumed" via (a) a region selector {"kind":"region","sketch":"fX"} or
                        // (b) a bare sketch-id string under a *_sketch / sketch / profile key (e.g.
                        // positions_sketch). We deliberately do NOT treat a bare sketch id under a
                        // generic "features" list (pattern/mirror) as consumption — patterns don't
                        // absorb sketches, and that would wrongly hide them.
                        std::function<void(const nlohmann::json&, const std::string&,
                                           std::unordered_set<std::string>&)> scan =
                            [&](const nlohmann::json& node, const std::string& key,
                                std::unordered_set<std::string>& seen) {
                            if (node.is_object()) {
                                if (jfld(node, "kind") == "region") {
                                    std::string sk = jfld(node, "sketch");
                                    if (!sk.empty() && sketchIds.count(sk)) seen.insert(sk);
                                }
                                for (auto it = node.begin(); it != node.end(); ++it)
                                    scan(it.value(), it.key(), seen);
                            } else if (node.is_array()) {
                                for (const auto& e : node) scan(e, key, seen);
                            } else if (node.is_string()) {
                                bool keyImpliesSketch =
                                    key.find("sketch") != std::string::npos || key == "profile";
                                std::string v = node.get<std::string>();
                                if (keyImpliesSketch && sketchIds.count(v)) seen.insert(v);
                            }
                        };
                        for (const auto& f : *feat) {
                            if (!f.is_object()) continue;
                            std::string fid = jfld(f, "id");
                            if (jfld(f, "type") == "sketch") continue;  // a sketch isn't a consumer
                            auto refs = f.find("refs");
                            if (refs == f.end()) continue;
                            std::unordered_set<std::string> seen;
                            scan(*refs, std::string(), seen);   // top-level refs object: no key yet
                            for (const auto& sk : seen) {
                                ++consumeCount[sk];
                                consumer[sk] = fid;  // last writer; only meaningful when count==1
                            }
                        }
                        for (const auto& kv : consumer) {
                            if (consumeCount[kv.first] != 1) continue;
                            // C48: a consumer that is ROLLED BACK or ERRORED does not absorb its
                            // sketch — the sketch un-nests to a flat row (SolidWorks behavior).
                            // Critically, this keeps the rollback bar drawable/grabbable when it
                            // sits after a profile sketch: a nested sketch never renders a flat
                            // row, so a bar anchored to it simply vanished from the tree.
                            auto cs = statusMap.find(kv.second);
                            if (cs != statusMap.end() && (!cs->second.ok || cs->second.rolledBack))
                                continue;
                            childrenOf[kv.second].push_back(kv.first);
                        }
                    }
                    // A sketch is nested (hidden from the flat list, shown under its consumer) iff
                    // it landed in childrenOf (one consumer, and that consumer actually builds).
                    std::unordered_set<std::string> nestedSketchIds;
                    for (const auto& kv : childrenOf)
                        for (const auto& sk : kv.second) nestedSketchIds.insert(sk);
                    auto isNestedSketch = [&](const std::string& fid) {
                        return nestedSketchIds.count(fid) != 0;
                    };

                    // Reorder op: drop draggedId before beforeId. A "@after:<id>" sentinel means drop
                    // AFTER <id> (i.e. before the feature following <id>, or end). "" = end.
                    auto reorderCb = [&st, &order](const std::string& dragged, const std::string& before) {
                        std::string beforeId = before;
                        if (before.rfind("@after:", 0) == 0) {
                            std::string anchor = before.substr(7);
                            beforeId.clear();
                            for (int i = 0; i < (int)order.size(); ++i)
                                if (order[i] == anchor) {
                                    if (i + 1 < (int)order.size()) beforeId = order[i + 1];
                                    break;
                                }
                        }
                        nlohmann::json op = {{"op", "reorder_feature"}, {"id", dragged}};
                        if (!beforeId.empty()) op["before"] = beforeId;  // omit => move to end
                        applyTreeOps(st, nlohmann::json::array({op}), "reorder");
                    };

                    // Owner-map → first owned body key ("b<n>") for the per-row Appearance item, and
                    // the owned-body count for the tooltip.
                    const auto& ownerMap = st.app->docMirror.ownerMap();
                    auto firstBodyKey = [&](const std::string& fid) -> std::string {
                        auto it = ownerMap.find(fid);
                        if (it == ownerMap.end() || it->second.bodies.empty()) return {};
                        return "b" + std::to_string(it->second.bodies.front());
                    };
                    auto bodyCount = [&](const std::string& fid) -> int {
                        auto it = ownerMap.find(fid);
                        return it == ownerMap.end() ? 0 : (int)it->second.bodies.size();
                    };
                    auto ownedBodies = [&](const std::string& fid) -> std::vector<uint32_t> {
                        auto it = ownerMap.find(fid);
                        return it == ownerMap.end() ? std::vector<uint32_t>{} : it->second.bodies;
                    };

                    // ---- render one feature row (shared by flat + nested-sketch paths) ----
                    int rowIdx = 0;   // visual stripe index (stable across nesting)
                    auto drawOneRow = [&](const nlohmann::json& f, bool nested) {
                        std::string fid    = jfld(f, "id");
                        std::string fname  = jfld(f, "name");
                        std::string ftype  = jfld(f, "type");
                        if (fname.empty()) fname = ftype + "·" + fid;  // C37-E: stable fid, not index
                        if (s_fmFilter[0] && !fm_icontains(fname.c_str(), s_fmFilter) &&
                            !fm_icontains(ftype.c_str(), s_fmFilter))
                            return;

                        FStatus fs;
                        { auto it = statusMap.find(fid); if (it != statusMap.end()) fs = it->second; }
                        bool suppressed = jftbool(f, "suppressed");
                        bool rolledBack = fs.rolledBack;   // authoritative (engine sets it)
                        bool sel = (selectedFeatureId == fid) || s_multiSel.count(fid) > 0;

                        // Row backgrounds: subtle striping plus a restrained selected-row card.
                        // Avoid ImGui's TreeNode selected paint here; it was stacking with our
                        // viewport highlight and made selected features look heavy and smeared.
                        ImVec2 rowMin = ImGui::GetCursorScreenPos();
                        float availW = ImGui::GetContentRegionAvail().x;
                        const float S = theme::S();
                        const float rowH = ImGui::GetTextLineHeight() + 10.0f * S;
                        ImVec2 rowMax(rowMin.x + availW, rowMin.y + rowH);
                        ImDrawList* rowDl = ImGui::GetWindowDrawList();
                        // Readable row pill via shared glass helper (panel stays transparent).
                        theme::glassRowBackground(rowDl, rowMin, rowMax, 3.5f, rowIdx % 2 == 0, sel,
                                               sel ? theme::rowWash::selected : theme::rowWash::idle);
                        // Category color strip: ~3px vertical bar on the far-left edge.
                        // Colored by feature group (sketch/solid-add/solid-cut/dress-up/pattern/
                        // datum/sheet-metal/weldment/surface) so each family is instantly scannable.
                        // The selected card's accent bar (drawn below) overlaps it on selected rows —
                        // the strip's first ~2 dp still shows through, but the accent bar dominates.
                        {
                            const bool catIsCut = featureModeIsCut(&f);
                            ImU32 catCol  = categoryColor(ftype, catIsCut);
                            float catW    = 3.0f * S;
                            float catInset = 2.0f * S;
                            rowDl->AddRectFilled(
                                ImVec2(rowMin.x,        rowMin.y + catInset),
                                ImVec2(rowMin.x + catW, rowMax.y - catInset),
                                catCol, 1.5f * S);
                        }
                        if (suppressed)
                            rowDl->AddRectFilled(rowMin, rowMax,
                                IM_COL32((int)(pal.statusWarn[0] * 255),
                                         (int)(pal.statusWarn[1] * 255),
                                         (int)(pal.statusWarn[2] * 255), 8));
                        if (sel) {
                            // selection card drawn by glassRowBackground(selected=true)
                            if (selectedFeatureId == fid && st.treeScrollToSelection) {
                                ImGui::SetScrollHereY(0.5f);
                                st.treeScrollToSelection = false;
                            }
                        }
                        ++rowIdx;

                        // C27-C: convert fillet↔chamfer (kept).
                        const char* convertLabel = nullptr;
                        std::function<void()> convertCb = nullptr;
                        bool isFillet  = (ftype == "fillet");
                        bool isChamfer = (ftype == "chamfer");
                        if (isFillet || isChamfer) {
                            convertLabel = isFillet ? "Convert to Chamfer" : "Convert to Fillet";
                            nlohmann::json capturedFeat = f;
                            std::string capturedType = ftype, capturedFid = fid, nextId;
                            { bool found = false;
                              for (const auto& ff : *feat) {
                                  if (!ff.is_object()) continue;
                                  if (found) { nextId = jfld(ff, "id"); break; }
                                  if (jfld(ff, "id") == fid) found = true;
                              } }
                            convertCb = [&st, capturedFeat, capturedType, capturedFid, nextId]() {
                                std::string toType = (capturedType == "fillet") ? "chamfer" : "fillet";
                                nlohmann::json newParams = nlohmann::json::object();
                                if (capturedType == "fillet") {
                                    auto rIt = capturedFeat["params"].find("radius");
                                    newParams["length"] = (rIt != capturedFeat["params"].end())
                                        ? *rIt : nlohmann::json({{"value", 1.0}});
                                } else {
                                    auto lIt = capturedFeat["params"].find("length");
                                    newParams["radius"] = (lIt != capturedFeat["params"].end())
                                        ? *lIt : nlohmann::json({{"value", 2.0}});
                                }
                                nlohmann::json newFeat = {
                                    {"type", toType}, {"params", newParams},
                                    {"refs", capturedFeat.value("refs", nlohmann::json::object())},
                                };
                                nlohmann::json addOp = {{"op", "add_feature"}, {"feature", newFeat}};
                                if (!nextId.empty()) addOp["before"] = nextId;
                                applyTreeOps(st, nlohmann::json::array({
                                    {{"op", "delete_feature"}, {"id", capturedFid}, {"cascade", false}},
                                    addOp}), "convert");
                            };
                        }

                        RowCtx ctx;
                        ctx.fid = fid; ctx.name = fname; ctx.type = ftype;
                        ctx.iconName = featureIcon(ftype, &f);
                        ctx.iconFallback = featureFallback(ftype);
                        ctx.suppressed = suppressed; ctx.selected = sel;
                        ctx.rolledBack = rolledBack; ctx.fs = fs;
                        ctx.ownerBodyCount = bodyCount(fid);
                        ctx.ownedBodies = ownedBodies(fid);
                        ctx.nested = nested;
                        ctx.canDoc = canDocTree;
                        ctx.barAtEnd = barAtEnd;
                        ctx.firstOwnedBody = firstBodyKey(fid);
                        ctx.convertLabel = convertLabel; ctx.convertCb = convertCb;
                        ctx.reorderCb = reorderCb;
                        ctx.editCallback = &editCallback;

                        RowResult rr = featureRow(ctx, st);
                        if (rr.hovered && !sel && !ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                            ImVec2 hovMin(rowMin.x + 2.0f * S, rowMin.y + 1.0f * S);
                            ImVec2 hovMax(rowMax.x - 3.0f * S, rowMax.y - 1.0f * S);
                            ImVec4 hoverCol(pal.accent[0], pal.accent[1], pal.accent[2], 0.45f);
                            ImGui::GetWindowDrawList()->AddRect(
                                hovMin, hovMax, ImGui::ColorConvertFloat4ToU32(hoverCol),
                                4.0f * S, 0, std::max(1.0f, 1.0f * S));
                            ImGui::GetWindowDrawList()->AddRectFilled(
                                hovMin, hovMax, fmToU32(pal.treeHover), 4.0f * S);
                        }

                        // ---- rollback-bar drag hit-test (item 4) ----
                        // While the bar is being dragged, find which row's gap the cursor is over so
                        // the release can compute "after: <fid>". A line is drawn at that gap.
                        if (s_rollbackDragging && !nested) {
                            ImVec2 imn = ImGui::GetItemRectMin(), imx = ImGui::GetItemRectMax();
                            float my = ImGui::GetIO().MousePos.y;
                            if (my >= imn.y && my <= imx.y) {
                                s_rbDropAfter = fid;  // drop the bar right after this feature
                                ImGui::GetWindowDrawList()->AddLine(
                                    ImVec2(imn.x, imx.y), ImVec2(imx.x, imx.y),
                                    fmToU32(pal.accent),
                                    std::max(1.0f, 2.5f * theme::S()));  // #4.5
                            } else if (my < imn.y && s_rbDropAfter.empty()) {
                                // cursor above the first row → roll back before everything (to start)
                                s_rbDropAfter = "\1start";
                            }
                        }

                        // ---- selection / interaction handling ----
                        // User-defined datum PLANE clicked in the tree: targets it like the origin
                        // planes do — start a new sketch on it during plane-pick, or fill an armed
                        // plane well (SolidWorks lets you pick a reference plane from the tree). The
                        // sketch plane is the bare feature-id string; a plane well takes id + name.
                        if (rr.clicked && ftype == "plane" && st.sketchPlanePickActive) {
                            st.sketchPlanePickActive = false;
                            selectedFeatureId = fid;
                            s_multiSel.clear();
                            selectFeatureInViewport(st, fid);
                        } else if (rr.doubleClick) {
                            selectedFeatureId = fid;
                            s_multiSel.clear();
                            selectFeatureInViewport(st, fid);
                            if (ftype == "sketch") {                /* sketch enter deferred until SketchHost wire */
                            } else if (editCallback) {
                                editCallback(fid);
                            }
                        } else if (rr.ctrlClicked) {
                            // Ctrl+click toggles this row in/out of the multi-set. The current
                            // primary joins the set first (so the FIRST ctrl-click on a 2nd row keeps
                            // both). Primary tracks the last-clicked; toggling the primary off moves
                            // it to any remaining member (or clears).
                            if (!selectedFeatureId.empty()) s_multiSel.insert(selectedFeatureId);
                            if (s_multiSel.count(fid)) {
                                s_multiSel.erase(fid);
                                if (selectedFeatureId == fid) {
                                    selectedFeatureId = s_multiSel.empty() ? std::string()
                                                                           : *s_multiSel.begin();
                                }
                            } else {
                                s_multiSel.insert(fid);
                                selectedFeatureId = fid;
                            }
                            selectFeatureInViewport(st, selectedFeatureId);
                        } else if (rr.clicked) {
                            // Slow-double-click rename: a 2nd plain click on the already-primary row
                            // within (0.4s, 2.0s) arms the inline editor.
                            double now = ImGui::GetTime();
                            bool armRename = (selectedFeatureId == fid && s_lastClickId == fid &&
                                              s_lastClickTime > 0 &&
                                              (now - s_lastClickTime) > 0.4 &&
                                              (now - s_lastClickTime) < 2.0);
                            if (armRename) {
                                s_renamingId = fid;
                                std::snprintf(s_renameBuf, sizeof(s_renameBuf), "%s", fname.c_str());
                                s_lastClickTime = -1.0;
                            } else if (selectedFeatureId == fid && s_multiSel.empty()) {
                                clearTreeFeatureSelection(st);
                                s_lastClickId.clear();
                                s_lastClickTime = -1.0;
                            } else {
                                selectedFeatureId = fid;
                                s_multiSel.clear();
                                selectFeatureInViewport(st, fid);
                                s_lastClickId = fid;
                                s_lastClickTime = now;
                            }
                        }
                    };

                    // fid the rollback bar currently sits AFTER (empty = end).
                    std::string barFid = (!barAtEnd && barPos >= 0 && barPos < (int)order.size())
                                       ? order[barPos] : std::string();

                    // Reset the bar drop-target each frame the bar is being dragged; rows below set
                    // it as the cursor passes over them. If no row claims it (cursor below the last
                    // row), it stays empty → set_rollback{after:null} = bar at the end.
                    if (s_rollbackDragging) s_rbDropAfter.clear();

                    // ---- walk the feature list ----
                    std::string openFolder;
                    bool folderOpen = false;
                    for (int i = 0; i < (int)feat->size(); ++i) {
                        const auto& f = (*feat)[i];
                        if (!f.is_object()) continue;
                        std::string fid    = jfld(f, "id");
                        std::string ftype  = jfld(f, "type");
                        std::string folder = jfld(f, "folder");

                        // Skip nested sketches in the flat pass — they render under their consumer.
                        if (ftype == "sketch" && isNestedSketch(fid)) continue;

                        // Folder grouping (C37-E item 2: ImGui id from the folder NAME, stable across
                        // reorders, not a per-frame counter).
                        if (folder != openFolder) {
                            if (folderOpen) { ImGui::TreePop(); folderOpen = false; }
                            openFolder = folder;
                            if (!folder.empty()) {
                                ImGui::PushStyleColor(ImGuiCol_Text,
                                    ImVec4(pal.text[0], pal.text[1], pal.text[2], 1.0f));
                                ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing,
                                    ImVec2(0, 4.0f * theme::S()));
                                fmRowPill();
                                treeIcon("body_folder", "[F]", 16.0f);   // #4.4: was "mates_folder"
                                ImGui::SameLine(0, 4 * theme::S());   // #4.5
                                folderOpen = ImGui::TreeNodeEx(
                                    (folder + "##fld_" + folder).c_str(),
                                    ImGuiTreeNodeFlags_DefaultOpen |
                                    ImGuiTreeNodeFlags_SpanFullWidth);
                                ImGui::PopStyleVar();
                                ImGui::PopStyleColor();
                            }
                        }
                        if (!openFolder.empty() && !folderOpen) continue;

                        drawOneRow(f, /*nested=*/false);

                        // Nested-sketch children: render absorbed sketches indented under this row.
                        auto kids = childrenOf.find(fid);
                        if (kids != childrenOf.end()) {
                            for (const auto& skId : kids->second) {
                                for (const auto& sf : *feat)
                                    if (sf.is_object() && jfld(sf, "id") == skId) {
                                        drawOneRow(sf, /*nested=*/true);
                                        break;
                                    }
                            }
                        }

                        // C37-E (item 4): draw the rollback bar inline right after the bar feature
                        // (and its absorbed sketches) when it sits mid-history.
                        if (!barFid.empty() && fid == barFid)
                            drawRollbackBarLine(st, canDocTree, barFid);
                    }
                    if (folderOpen) { ImGui::TreePop(); folderOpen = false; }
                    // Bar at the end → draw it after the last row.
                    if (barAtEnd)
                        drawRollbackBarLine(st, canDocTree, std::string());

                    // ---- Ctrl+Up / Ctrl+Down: move the selected feature among its siblings ----
                    if (canDocTree && !selectedFeatureId.empty() &&
                        ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows)) {
                        int p = -1;
                        for (int i = 0; i < (int)order.size(); ++i)
                            if (order[i] == selectedFeatureId) { p = i; break; }
                        if (p >= 0) {
                            if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_UpArrow) && p > 0)
                                reorderCb(selectedFeatureId, order[p - 1]);
                            else if (ImGui::IsKeyChordPressed(ImGuiMod_Ctrl | ImGuiKey_DownArrow) &&
                                     p < (int)order.size() - 1) {
                                if (p + 2 < (int)order.size())
                                    reorderCb(selectedFeatureId, order[p + 2]);
                                else
                                    reorderCb(selectedFeatureId, "");  // already next-to-last -> end
                            }
                        }
                    }

                    // ---- Rollback bar drag release (C37-E item 4) ----
                    // The bar is rendered inline above. When a bar drag ends, enqueue set_rollback
                    // for the gap the cursor landed in: after the hovered feature, or null (= end)
                    // when below the last row, or null/first when above (roll fully back ≈ start →
                    // we encode "to start" as set_rollback{after: first feature} so at least the
                    // first feature stays, matching SolidWorks' minimum-one rolled state... actually
                    // SW allows rolling to the very top; we map "above first" to the first feature).
                    if (s_rollbackDragging && !ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                        std::string target = s_rbDropAfter;
                        nlohmann::json after = nullptr;   // default = end
                        if (target == "\1start") {
                            after = order.empty() ? nlohmann::json(nullptr)
                                                  : nlohmann::json(order.front());
                        } else if (!target.empty()) {
                            // Dropping after the last feature = bar at end (null).
                            if (!order.empty() && target == order.back()) after = nullptr;
                            else after = target;
                        }
                        if (canDocTree)
                            applyTreeOps(st, nlohmann::json::array({
                                {{"op", "set_rollback"}, {"after", after}}}), "rollback");
                        s_rollbackDragging = false;
                        s_rbDropAfter.clear();
                    } else if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                        // Mouse up without a drag in progress — clear stale hit state.
                        s_rbDropAfter.clear();
                    }
                } else if (s_rollbackDragging) {
                    // C38 (T2): the feature list vanished mid-drag (no walk loop, so the release
                    // handler above never runs). Clear the drag flag so it can't linger a frame and
                    // leave the bar visually "stuck" to the cursor.
                    s_rollbackDragging = false;
                    s_rbDropAfter.clear();
                }
            }

            ImGui::PopStyleColor();  // text color (kept live for the entire open subtree)
            ImGui::TreePop();
        } else {
            ImGui::PopStyleColor();  // text color
        }
    }

    if (!st.selectedFeatureId.empty() || !s_multiSel.empty()) {
        const bool treeFocused = ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows);
        const bool treeHovered = ImGui::IsWindowHovered(ImGuiHoveredFlags_RootAndChildWindows |
                                                        ImGuiHoveredFlags_AllowWhenBlockedByActiveItem);
        if (treeFocused && !ImGui::GetIO().WantTextInput &&
            ImGui::IsKeyPressed(ImGuiKey_Escape, false)) {
            clearTreeFeatureSelection(st);
        } else if (treeHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
                   !ImGui::IsAnyItemHovered() && !ImGui::IsAnyItemActive()) {
            clearTreeFeatureSelection(st);
        }
    }

    // C37-E (item 3): per-row "Appearance..." picker. The row's context menu stashed the target body
    // key + a one-shot open flag; render the picker as a sibling popup (same pattern as the root
    // node) and turn a commit into a set_appearance op on that body.
    if (s_apprOpen && !s_apprBody.empty()) { ImGui::OpenPopup("##appearance_feat"); s_apprOpen = false; }
    if (!s_apprBody.empty()) {
        pushTreePopupGlass();
        if (appearanceColorPicker("##appearance_feat", s_apprColor,
                                  &s_apprMetallic, &s_apprRoughness)) {
            applyTreeOps(st, nlohmann::json::array({
                {{"op", "set_appearance"}, {"id", s_apprBody},
                 {"color", {s_apprColor[0], s_apprColor[1], s_apprColor[2]}},
                 {"metallic", s_apprMetallic}, {"roughness", s_apprRoughness}}}),
                "set_appearance");
            s_apprBody.clear();
        }
        popTreePopupGlass();
    }

    // ---- C41: bottom file strip ----
    // Compact row: [Save] [Save As...] [Open...] + the current file name (dimmed; "(unsaved)"
    // until the doc is saved/opened). Buttons size themselves from their labels — no fixed-width
    // math, so nothing clips off the panel edge (the old strip's Open button did).
    // AI_CAD_NO_FILE_DIALOGS keeps the old raw-path InputText fallback for headless/CI use.
    bool canDoc = canDocTree;
    if (canDoc && (st.app->docMirror.hasDoc() || !fileDialogsDisabled())) {
        ImGui::Separator();
        if (fileDialogsDisabled()) {
            // Escape hatch: raw path + Save/Open, the pre-C41 behavior — except Open now routes
            // through applyOpenedDoc so the viewport mesh refreshes (the old code never did).
            if (st.app->docMirror.hasDoc()) {
                static char savePath[512] = "";
                const ImGuiStyle& style = ImGui::GetStyle();
                float btnW = ImGui::CalcTextSize("Save").x + ImGui::CalcTextSize("Open").x +
                             4.0f * style.FramePadding.x + 2.0f * style.ItemSpacing.x;
                ImGui::SetNextItemWidth(-btnW);
                ImGui::InputText("##savepath", savePath, sizeof(savePath));
                ImGui::SameLine();
                if (ImGui::Button("Save") && savePath[0]) {
                    std::string err;
                    if (st.app->docMirror.save(st.app->worker, savePath, err)) {
                        s_docPath = savePath;
                        st.addLog("[doc] saved: " + std::string(savePath));
                    } else st.addLog("[err] save: " + err);
                }
                ImGui::SameLine();
                if (ImGui::Button("Open") && savePath[0]) {
                    std::string err;
                    std::string p = savePath;
                    if (st.app->docMirror.open(st.app->worker, p, err)) applyOpenedDoc(st, p);
                    else st.addLog("[err] open: " + err);
                }
            }
        } else {
            // File name on its own dimmed line (a narrow panel must never push it — or the
            // buttons — past the edge); the full path shows on hover.
            theme::glassRowAtCursor(3.5f, false, false, 5.0f);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1.0f));
            std::string fileLabel = s_docPath.empty() ? "(unsaved)" : pathBaseName(s_docPath);
            ImGui::TextUnformatted(fileLabel.c_str());
            ImGui::PopStyleColor();
            if (!s_docPath.empty() && ImGui::IsItemHovered())
                ImGui::SetTooltip("%s", s_docPath.c_str());
            bool canSave = st.app->docMirror.hasDoc();
            if (!canSave) ImGui::BeginDisabled();
            {
                const ImGuiStyle& style = ImGui::GetStyle();
                const float gap = style.ItemSpacing.x;
                const float halfW = (ImGui::GetContentRegionAvail().x - gap) * 0.5f;
                fmRowPill();
                if (ImGui::Button("Save", ImVec2(halfW, 0))) doSaveDoc(st, /*forceDialog=*/false);
                ImGui::SameLine(0, gap);
                if (ImGui::Button("Save As...", ImVec2(halfW, 0))) doSaveDoc(st, /*forceDialog=*/true);
            }
            if (!canSave) ImGui::EndDisabled();
            theme::glassRowAtCursor(3.5f, false, false, 6.0f);
            if (ImGui::Button("Open...", ImVec2(-1, 0))) doOpenDoc(st);
        }
    }

    // C25: apply ops queued during drawing — safe now, nothing reads the tree json below.
    flushPendingTreeOps(st);
    // Wave 4C: run any copy/cut/paste requested from a row context menu (deferred to here because
    // the copy's DocMirror call replaces the tree json the menu was drawn over).
    flushPendingClipboard(st);
}

// ---- Wave 3: public Save/Open wrappers (the File menu + File Explorer reuse these) ----
// The actual RPC/dialog/mesh-refresh logic lives in the file-local doSaveDoc / doOpenDoc /
// applyOpenedDoc above; these thin wrappers expose it without duplicating any of it, and keep
// s_docPath (the active doc's on-disk path) as the single source of truth.

// Open a file by absolute path (no dialog). Used by the File Explorer double-click and the
// Recent-files menu. Returns true on success; toasts + logs on failure (never throws).
bool featureTreeOpenPath(AppState& st, const std::string& path, bool asSolid) {
    if (path.empty()) return false;
    std::string err;
    if (st.app->docMirror.open(st.app->worker, path, err, asSolid)) {
        applyOpenedDoc(st, path);   // s_docPath = path, new tab, mesh refresh, recent handled by caller
        return true;
    }
    st.addLog("[err] open: " + err);
    st.toast(ui::ToastLevel::Error, "Open failed: " + pathBaseName(path) + " — " + err);
    return false;
}

// Live MCP handoff poll — see the header. The external MCP server (the engine Claude drives)
// publishes the part it's working on to %APPDATA%\AI-CAD\handoff\ as a NATIVE .step+ doc plus a
// tiny live.json {rev, step}. We open/refresh a single tab from it so the user watches the AI
// build in the viewport. Loading the native .step+ (not bare STEP) brings the whole feature tree.
void pollAiCadHandoff(AppState& st) {
    if (!st.app->workerStarted) return;   // worker not up — nothing can open yet
    if (st.agentV2Running) return;   // an in-app agent run owns the doc; don't fight it
    namespace fs = std::filesystem;
    static const std::string ctrl = platform::appdataRoot() + "/AI-CAD/handoff/live.json";

    static long long shownRev   = -1;   // the rev currently drawn in the mirror tab
    static long long pendingRev = -1;   // the latest rev the MCP published (may be deferred)
    static std::string pendingStep;     // the .step+ that goes with pendingRev
    static fs::file_time_type lastMtime{};

    // 1) Cheaply pick up a newer control file. Only parse when its mtime advances; a published rev
    //    is remembered as "pending" until it is SAFE to show (we may defer it, see step 3).
    std::error_code ec;
    if (fs::exists(ctrl, ec)) {
        fs::file_time_type mt = fs::last_write_time(ctrl, ec);
        if (!ec && mt != lastMtime) {
            lastMtime = mt;
            nlohmann::json j;
            std::ifstream f(ctrl, std::ios::binary);
            if (f) { try { f >> j; } catch (...) { j = nlohmann::json{}; } }  // half-written: retry next change
            const long long rev = j.value("rev", (long long)-1);
            const std::string step = j.value("step", std::string());
            if (rev > pendingRev && !step.empty() && fs::exists(step, ec)) {
                pendingRev = rev;
                pendingStep = step;
            }
        }
    }

    // 2) Nothing new to reveal.
    if (pendingRev < 0 || pendingRev == shownRev) return;

    // 3) SAFETY GATES — defer rather than disrupt the user. The AI keeps building in its OWN engine
    //    process; the on-screen mirror just catches up the moment it is safe, so user interaction
    //    can never break the AI's work and the AI's refresh can never wreck the user's.
    const bool userMidEdit =
        false ||           // an open sketch
        false;   // an open feature-edit (PM) form
    int handoffIdx = -1;
    for (int i = 0; i < (int)st.app->docTabs.size(); ++i)
        if (fmHandoffTab(i)) { handoffIdx = i; break; }

    const bool firstReveal = (handoffIdx < 0);
    if (firstReveal) {
        // No mirror tab yet — opening one makes it active (steals focus). Don't pop it over an
        // in-progress edit; wait until the user finishes, then reveal.
        if (userMidEdit) return;
    } else {
        // Mirror already exists — only refresh it while the user is actually looking at it and not
        // mid-edit. If they switched to their own tab (or are editing), leave the update pending;
        // it applies the next frame they return, so we never yank focus or cancel their edit.
        if (handoffIdx != st.app->activeTab || userMidEdit) return;
    }

    // 4) Apply. Preserve the user's camera across the in-place refresh of the mirror tab so watching
    //    the AI build doesn't reset their orbit/zoom each step.
    const bool keepCam = (!firstReveal && handoffIdx == st.app->activeTab);
    const float cy = st.yaw, cp = st.pitch, cz = st.zoom,
                px = st.pan[0], py = st.pan[1], pz = st.pan[2];

    // Close the prior mirror tab(s) (matched by the aiHandoff flag — robust to tab reorder/close),
    // then open the freshly published .step+. Quiet: applyOpenedDoc must not toast every step.
    for (int i = (int)st.app->docTabs.size() - 1; i >= 0; --i)
        if (fmHandoffTab(i)) st.closeDocTab(i);

    st.handoffQuietOpen = true;
    const bool opened = featureTreeOpenPath(st, pendingStep, false);
    st.handoffQuietOpen = false;

    if (opened && st.app->activeTab >= 0 && st.app->activeTab < (int)st.app->docTabs.size()) {
        fmSetHandoffActive(st, true);   // tag the just-opened tab as the mirror
        if (keepCam) {
            st.yaw = cy; st.pitch = cp; st.zoom = cz;
            st.pan[0] = px; st.pan[1] = py; st.pan[2] = pz;
            st.zoomToBBoxRequested = false;          // keep our restored view; cancel the fresh-open auto-fit
        }
        shownRev = pendingRev;
        if (firstReveal)
            st.toast(ui::ToastLevel::Info, "ShopCraft: live view of the AI's part — it updates as it builds");
    }
}

// Native "Open..." dialog -> open. Returns the opened path (for Recent), or "" on cancel/failure.
std::string featureTreeOpenDialog(AppState& st) {
    if (fileDialogsDisabled()) {
        st.toast(ui::ToastLevel::Info,
                 "File dialogs disabled — use the path field below the tree");
        return std::string();
    }
    std::string path = platform::openCadFile();
    if (path.empty()) return std::string();   // canceled
    // Mesh files: ask whether to import as a solid (vs. graphics mesh). Non-mesh formats never prompt.
    bool asSolid = false;
    if (isMeshImportFormat(path)) {
        int choice = promptImportMeshAsSolid();
        if (choice < 0) return std::string();   // canceled
        asSolid = (choice == 1);
    }
    return featureTreeOpenPath(st, path, asSolid) ? path : std::string();
}

// Save the active doc. saveAs=true forces the "Save As..." dialog; otherwise a silent re-save to
// the known path (dialog only on first save). Returns the path saved to (for Recent), or "" on
// cancel/failure/no-doc.
std::string featureTreeSaveDoc(AppState& st, bool saveAs) {
    if (!st.app->docMirror.hasDoc()) return std::string();
    doSaveDoc(st, /*forceDialog=*/saveAs);
    return s_docPath;   // doSaveDoc updates s_docPath on success; unchanged on cancel/failure
}

// The active document's on-disk path ("" if the doc has never been saved). Lets the File menu grey
// "Save As" vs. "Save" correctly and lets the Explorer highlight the open file.
std::string featureTreeActivePath() { return s_docPath; }
}  // namespace detail

static detail::FmState& bindFm(app::AppState& st, const theme::AdaptiveSurface* live = nullptr) {
    detail::fmPaletteRefresh(live);
    detail::g_fm.bind(st);
    return detail::g_fm;
}

void drawFeatureTreeBody(app::AppState& st,
                         const std::function<void(const std::string&)>& editCallback) {
    // A1: fetch (never paint) the Browser pane's ONE glass surface — left_dock.cpp already painted
    // it this frame under the same panelId; A2 makes this a cached, near-free lookup.
    ImVec2 wp_ = ImGui::GetWindowPos(); ImVec2 ws_ = ImGui::GetWindowSize();
    ImRect paneRect(wp_, ImVec2(wp_.x + ws_.x, wp_.y + ws_.y));
    const theme::AdaptiveSurface& surf = theme::adaptivePanel(theme::panelId("browser_glass"), paneRect);
    const bool live = theme::viewportBackdrop().valid();
    auto& fm = bindFm(st, live ? &surf : nullptr);
    detail::drawFeatureTreeBodyImpl(fm, editCallback);
    fm.syncToApp();
}

void featureTreeResetTransient() {
    detail::featureTreeResetTransient();
}
void featureClearTreeMultiSel() {
    detail::featureClearTreeMultiSel();
}
void refreshFeatureHighlight(app::AppState& st) {
    auto& fm = bindFm(st);
    detail::refreshFeatureHighlight(fm);
    fm.syncToApp();
}
bool featureCopySelection(app::AppState& st) {
    auto& fm = bindFm(st);
    bool ok = detail::featureCopySelection(fm);
    fm.syncToApp();
    return ok;
}
bool featureCutSelection(app::AppState& st) {
    auto& fm = bindFm(st);
    bool ok = detail::featureCutSelection(fm);
    fm.syncToApp();
    return ok;
}
bool featurePasteSelection(app::AppState& st) {
    auto& fm = bindFm(st);
    bool ok = detail::featurePasteSelection(fm);
    fm.syncToApp();
    return ok;
}
bool featureDeleteSelection(app::AppState& st) {
    auto& fm = bindFm(st);
    bool ok = detail::featureDeleteSelection(fm);
    fm.syncToApp();
    return ok;
}
bool featureSelectAll(app::AppState& st) {
    auto& fm = bindFm(st);
    bool ok = detail::featureSelectAll(fm);
    fm.syncToApp();
    return ok;
}
bool featureTreeOpenPath(app::AppState& st, const std::string& path, bool asSolid) {
    auto& fm = bindFm(st);
    bool ok = detail::featureTreeOpenPath(fm, path, asSolid);
    fm.syncToApp();
    return ok;
}
std::string featureTreeOpenDialog(app::AppState& st) {
    auto& fm = bindFm(st);
    auto p = detail::featureTreeOpenDialog(fm);
    fm.syncToApp();
    return p;
}
std::string featureTreeSaveDoc(app::AppState& st, bool saveAs) {
    auto& fm = bindFm(st);
    auto p = detail::featureTreeSaveDoc(fm, saveAs);
    fm.syncToApp();
    return p;
}
std::string featureTreeActivePath() { return detail::featureTreeActivePath(); }
void pollAiCadHandoff(app::AppState& st) {
    auto& fm = bindFm(st);
    detail::pollAiCadHandoff(fm);
    fm.syncToApp();
}

}  // namespace ui
