// Shared appearance / material picker (W1-B item 9) + C5 panel seam helpers.
// See host appearance_common.h; c5::* bridges app::AppState until G3 wires full host parity.
#pragma once

#include <string>
#include <vector>

#include "imgui.h"
#include "theme/theme.h"
#include "ui/toast.h"

namespace app { struct AppState; }
class Renderer;

namespace ui {

// ---- appearance presets (feature tree + viewport editors) --------------------

struct MaterialPreset {
    const char* name;
    float metallic, roughness, rgb[3];
};

constexpr int kMaterialsCount = 5;
extern const MaterialPreset kMaterials[kMaterialsCount];

struct PhysicalMaterial {
    const char* key;
    const char* name;
    float metallic, roughness, rgb[3];
};

constexpr int kPhysicalMaterialsCount = 9;
extern const PhysicalMaterial kPhysicalMaterials[kPhysicalMaterialsCount];

bool materialAppearance(const std::string& key, float outColor[3],
                        float& outMetallic, float& outRoughness);

bool appearanceColorPicker(const char* popupId, float outColor[3],
                           float* outMetallic, float* outRoughness);

// ---- C5 panel seam (asm/config/design-table/equations/cutlist/evaluate) ----

namespace c5 {

using AppState = app::AppState;

inline float scale() { return theme::S(); }
inline float px(float logical) { return logical * theme::S(); }

inline ImVec4 txt()      { return theme::vec(theme::pal().txt0); }
inline ImVec4 txtDim()   { return theme::vec(theme::pal().txt1); }
inline ImVec4 warnCol()  { return theme::vec(theme::pal().warn); }
inline ImVec4 errCol()   { return theme::vec(theme::pal().bad); }
inline ImVec4 accent()   { return theme::vec(theme::pal().accent); }
inline ImVec4 glassBg()  { return theme::vec(theme::pal().glassBg); }
inline ImVec4 glassBorder() { return theme::vec(theme::pal().glassBorder); }

bool ips(const AppState& st);
bool activeTabIsDrawing(const AppState& st);
bool busy(const AppState& st);
void toast(ToastLevel lvl, const std::string& msg);
void addLog(AppState& st, const std::string& msg);
bool loadDocMesh(AppState& st, const std::string& aicm, std::string& err);
void markActiveTabDirty(AppState& st);

bool pmParseDim(const std::string& text, double& outMm, std::string& err, bool angle = false,
                double bareLengthScale = 1.0);
std::string pmFormatDim(double mm, bool ipsUnits, bool angle = false);

// PM form bridge — nullptr until C2 PropertyManager lands (design-table add-column).
enum class PMFieldKind { Dim, SelectorWell, Combo, Check, Int, DatumRef, FeatureWell, Text };
struct PMFieldDef {
    PMFieldKind kind = PMFieldKind::Dim;
    const char* key = "";
    const char* label = "";
    const char* unitLabel = nullptr;
    bool comingSoon = false;
};
struct PMFormDef {
    const char* type = "";
    std::vector<PMFieldDef> fields;
};
const PMFormDef* pmFindForm(const std::string& type);

namespace metrics {
inline float iconHeader()       { return px(20.f); }
inline float asmHeaderIconGap() { return px(6.f); }
inline float asmBtnW()          { return px(90.f); }
inline float asmInputW()        { return px(120.f); }
inline float pmLabelCol()       { return px(116.f); }
inline float pmOkBtn()          { return px(28.f); }
inline float U(float n = 1.f)   { return theme::U(n); }
} // namespace metrics

void setEvaluateRenderer(Renderer* r);
Renderer* evaluateRenderer();

struct LegendState {
    bool on = false;
    std::string title;
    std::string units;
    float min = 0.f;
    float max = 1.f;
};
LegendState& legendState();
void setColormap(bool on);
void setZebra(bool on, float freq);
bool scalarRange(float& vmin, float& vmax);

} // namespace c5

// Panel visibility flags (module state until AppState fields land at G3).
bool& showEquationsDialog();
bool& showDesignTablePanel();
bool& showCutListPanel();

} // namespace ui
