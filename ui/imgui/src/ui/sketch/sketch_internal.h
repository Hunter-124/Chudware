// Shared sketch-mode helpers: F1 camera projection (no host renderer), palette, draw utilities.
#pragma once

#include <cmath>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "imgui.h"
#include "renderer.h"
#include "sketch_palette.h"
#include "pick_hit.h"
#include "render/id_pass.h"

namespace ui { struct SketchEntity; }

namespace ui::sketch {

struct SketchHost;

constexpr float kSnapPx = 6.0f;
constexpr float kHandlePx = 8.0f;
constexpr float kHVDeg = 5.0f;
constexpr float kGridStep = 10.0f;
constexpr float kInferHVDeg = 4.0f;
constexpr float kTangentDeg = 6.0f;

inline bool snapHasPrefix(const std::string& s, const char* pfx) {
    return s.rfind(pfx, 0) == 0;
}
inline std::string coincRef(const std::string& s) {
    if (snapHasPrefix(s, "mid:") || snapHasPrefix(s, "rim:") || snapHasPrefix(s, "tan:"))
        return {};
    return s;
}

inline ViewportScreen viewportScreen(const ImVec2& imgMin) {
    return ViewportScreen{imgMin.x, imgMin.y};
}

// F1 worldToScreen — ImVec2 wrapper (verified: uses ui/viewport/renderer.h, NOT host/).
inline bool worldToScreen(const Renderer::CameraXform& cam, const ImVec2& imgMin, int vw, int vh,
                          float wx, float wy, float wz, ImVec2& out) {
    ScreenPt sp;
    if (!::worldToScreen(cam, viewportScreen(imgMin), vw, vh, wx, wy, wz, sp)) return false;
    out.x = sp.x;
    out.y = sp.y;
    return true;
}

// F1 screenRay — ImVec2 wrapper.
inline bool screenRay(const Renderer::CameraXform& cam, const ImVec2& imgMin, int vw, int vh,
                      ImVec2 mouse, Vec3& outOrig, Vec3& outDir) {
    ScreenPt sp{mouse.x, mouse.y};
    return ::screenRay(cam, viewportScreen(imgMin), vw, vh, sp, outOrig, outDir);
}

inline ImU32 col(const float role[4], float a = 1.0f) {
    return sketchPalette().toU32(role, a);
}

inline float uiScale() { return sketchPalette().scale; }

std::vector<ImVec2> catmullRomUV(const std::vector<ImVec2>& p, int perSeg = 16);

bool circumcircle(const ImVec2& a, const ImVec2& b, const ImVec2& c,
                  float& cu, float& cv, float& r);

void drawDashedLine(ImDrawList* dl, ImVec2 a, ImVec2 b, ImU32 col_,
                    float thickness, float dash, float gap);

void drawDashedPolyline(ImDrawList* dl, const std::vector<ImVec2>& pts, ImU32 col_,
                        float thickness, float dash, float gap);

// Simplified dimension parse (full pmParseDim lands with PropertyManager port).
bool parseSketchDim(const std::string& text, double& outMm, std::string& err, bool angle = false,
                    double bareLengthScale = 1.0);
std::string formatSketchDim(double mm, bool ips, bool angle = false);

// Arc sweep test (shared by hit-test + trim helpers across TUs).
bool arcSweepContains(const ui::SketchEntity& arc, float ang);

extern bool g_skIps;
const char* skLenUnit();
std::string fmtLen(double mm);
std::string fmtAng(double deg);
void arrowHead(ImDrawList* dl, ImVec2 tip, float dx, float dy, float sz, ImU32 c);

inline worker::PickHit toWorkerPick(const render::PickHit& h) {
    worker::PickHit w;
    w.body_id = h.body_id;
    w.topo_id = h.topo_id;
    w.kind = static_cast<worker::PickKind>(static_cast<uint8_t>(h.kind));
    return w;
}

// Shared across sketch TUs (were static in sketch_mode.cpp).
void parseEntities(const nlohmann::json& res, std::vector<ui::SketchEntity>& out);
std::string skBaseId(const std::string& handle);
const nlohmann::json* sketchParamsInTree(SketchHost& st, const std::string& fid);
void resyncMirror(SketchHost& st);

bool pointInPolyUV(const std::vector<ImVec2>& poly, float x, float y);
float polyAreaUV(const std::vector<ImVec2>& p);
bool ptInRect(const ImVec2& p, const ImVec2& lo, const ImVec2& hi);
bool segCrossesRect(const ImVec2& a, const ImVec2& b, const ImVec2& lo, const ImVec2& hi);

}  // namespace ui::sketch
