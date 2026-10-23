// Chudware Viewport panel — 3D orbit view + parts list + fit readout + overlays.
#include <filesystem>

#include <cctype>
#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <unordered_set>

#include "imgui.h"
#include "imgui_internal.h"   // GetWindowDockNode + DockNodeBeginAmendTabBar (tab-bar "+" New file)
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif

#include "viewport_panel.h"

#include "legend.h"
#include "icons.h"
#include "mouse_gestures.h"
#include "shortcut_bar.h"
#include "camera_anim.h"     // C25 W2-D: eased camera tween (presets / Normal-To / zoom-to-fit)
#include "view_selector.h"   // C25 W2-E: SolidWorks-style glass-box view selector (Space)
#include "section_tool.h"    // C25 W3-J: section-view tool (another agent owns impl)
#include "cam/cam_panel.h"   // CAM: viewport toolpath backplot overlay
#include "cam/cam_mirror.h"  // CAM: CamBackplot type
#include "widgets/kit.h"     // ui::kit::primaryButton/dangerButton — the one commit/cancel pair

#include "../core/app_state.h"

#include "../renderer.h"
#include "../render/id_pass.h"
#include "../render/overlays.h"

#include "sketch_mode.h"   // C18
#include "sketch3d_mode.h" // 3D free-space sketcher overlay
#include "dragdrop.h"      // kFileDragPayload (Explorer drag source -> viewport drop target)
#include "property_manager.h"  // C27-A: in-viewport parameter callouts
#include "sim_manager.h"        // simulation fixture/load previews in the viewport
#include "sim/sim_interaction.h"
#include "sim/sim_scene.h"
#include "evaluate_dialogs.h"  // Measure/Mass viewport overlays + body alpha
#include "appearance_common.h"  // W1-B (item 9): shared MaterialPreset table + appearance picker
#include "drawing_panel.h"  // DRAWINGS: 2D sheet renderer for a Drawing tab
#include "doc_tabs.h"
#include "feature_tree.h"  // openComponentPart + featureTreeActivePath (assembly component ctx)
#include "copilot/context_chips.h"  // ui::addContextRef — context-cube picks → chat chips
#include "file_dialog.h"   // revealInFileManager (assembly component ctx "Open Part Location")
#include "layout.h"        // ui::overlayColumnEdge — HUD dodges the pinned glass columns
#include "metrics.h"       // ui::metrics::U — clear-region gutter

namespace ui {

// C25: file-local gesture/shortcut state — consistent with how SketchMode is held externally.
static MouseGestures s_gestures;
static ShortcutBar   s_shortcutBar;
static ViewSelector  s_viewSelector;   // C25 W2-E: glass-box view selector (Space)
static SectionTool   s_sectionTool;    // C25 W3-J: section-view (impl owned by section_tool agent)
static float         s_huFade = 0.25f; // heads-up toolbar idle opacity (ramps on hover)
// Clear (un-occluded) region of the full-bleed 3D image this frame (screen coords) — see the
// computation after renderer.resize(). Published through viewportClearRect().
static ImVec2        s_clearMin(0.0f, 0.0f), s_clearMax(0.0f, 0.0f);
// Face-well chip hover claim (claimViewportHoverFace): drawn as the hover highlight while the
// claim was renewed this or the previous frame.
static render::PickHit s_uiHoverFace;
static int             s_uiHoverFrame = -10;

void claimViewportHoverFace(uint32_t bodyId, uint32_t faceId) {
    if (bodyId == 0 || faceId == 0) return;
    s_uiHoverFace = render::PickHit{};
    s_uiHoverFace.body_id = bodyId;
    s_uiHoverFace.topo_id = faceId;
    s_uiHoverFace.kind = render::PickKind::Face;
    s_uiHoverFrame = ImGui::GetFrameCount();
}

bool viewportClearRect(ImVec2& outMin, ImVec2& outMax) {
    if (s_clearMax.x <= s_clearMin.x || s_clearMax.y <= s_clearMin.y) return false;
    outMin = s_clearMin;
    outMax = s_clearMax;
    return true;
}

#ifdef __EMSCRIPTEN__
// Touch points belong to shell.html and are canvas-local CSS coordinates.
// Gesture ownership is latched for the entire contact sequence: crossing a
// drawer, or lifting one finger after a pinch, never starts a fresh orbit.
static bool wasmViewportTouches(AppState& st, Renderer& renderer, ImVec2 min,
                                ImVec2 max, int viewportHeight, bool cameraEnabled) {
    double points[4] = {};
    const int count = EM_ASM_INT({
        var touches = window.__chudwareTouches || [];
        for (var i = 0; i < Math.min(2, touches.length); ++i) {
            setValue($0 + i * 16, touches[i].x, 'double');
            setValue($0 + i * 16 + 8, touches[i].y, 'double');
        }
        return touches.length;
    }, points);
    static int previousFrame = -1;
    static bool blocked = false, pinched = false, havePair = false;
    static ImVec2 previousMid;
    static float previousDistance = 0.0f;
    const int frame = ImGui::GetFrameCount();
    if (count == 0) {
        previousFrame = frame;
        blocked = pinched = havePair = false;
        return false;
    }
    if (previousFrame != frame - 1) blocked = true;
    previousFrame = frame;
    ImGuiContext& g = *ImGui::GetCurrentContext();
    ImGuiWindow* viewportWindow = ImGui::GetCurrentWindow();
    const ImGuiID surfaceId = ImGui::GetItemID();
    const ImVec2 origin = ImGui::GetMainViewport()->Pos;
    ImVec2 p[2] = {ImVec2((float)points[0] + origin.x, (float)points[1] + origin.y),
                   ImVec2((float)points[2] + origin.x, (float)points[3] + origin.y)}; // gui-lint: allow — array indices, not pixel literals
    auto onSurface = [&](ImVec2 point) {
        if (!ImRect(min, max).Contains(point)) return false;
        ImGuiWindow* hovered = nullptr;
        ImGuiWindow* underMoving = nullptr;
        ImGui::FindHoveredWindowEx(point, false, &hovered, &underMoving);
        return hovered == viewportWindow;
    };
    if (!onSurface(p[0]) || (count > 1 && !onSurface(p[1])) ||
        ImGui::IsPopupOpen(nullptr, ImGuiPopupFlags_AnyPopupId) ||
        (g.ActiveId != 0 && g.ActiveId != surfaceId))
        blocked = true;
    if (count > 1) pinched = true;
    if (count != 2 || blocked || !cameraEnabled) {
        havePair = false;
        return blocked || pinched;
    }
    const ImVec2 mid((p[0].x + p[1].x) * 0.5f, (p[0].y + p[1].y) * 0.5f);
    const float dx = p[1].x - p[0].x, dy = p[1].y - p[0].y;
    const float distance = std::sqrt(dx * dx + dy * dy);
    if (havePair && distance > 1.0f && previousDistance > 1.0f) {
        cancelCameraAnimation();
        const auto& camera = renderer.camera();
        const float worldPerPixel = renderer.worldPerPixel(viewportHeight);
        const float moveX = mid.x - previousMid.x, moveY = mid.y - previousMid.y;
        st.pan[0] += (-camera.right.x * moveX + camera.up.x * moveY) * worldPerPixel;
        st.pan[1] += (-camera.right.y * moveX + camera.up.y * moveY) * worldPerPixel;
        st.pan[2] += (-camera.right.z * moveX + camera.up.z * moveY) * worldPerPixel;
        st.zoom = std::clamp(st.zoom * distance / previousDistance, 0.1f,
                             renderer.maxSceneZoom());
    }
    previousMid = mid;
    previousDistance = distance;
    havePair = true;
    return true;
}
#endif

// ---- Liquid-glass v2 host port (H6a: local://glass_host.md) ----
// Published viewport image rect (screen coords) + the live Renderer, kept for the registered
// ui::LuminanceSampler (a captureless lambda can only read file-static state) and for
// invalidateViewportBackdrop(). Mirrors ui/imgui/src/ui/viewport.cpp's s_bdMin/s_bdMax/s_bdLive
// + g_glVp pattern; refreshed every frame from drawViewportPanel (st.renderer is a stable single
// instance for the app's lifetime -- see AppState::renderer / run_gui).
static Renderer* s_bdRenderer = nullptr;
static ImVec2    s_bdMin  = {0, 0};
static ImVec2    s_bdMax  = {0, 0};
static bool      s_bdLive = false;

// Registers the screen->viewport-local luminance sampler exactly once. Ported from
// ui/imgui/src/ui/viewport.cpp setViewportPipeline's inset-band clamp (A7): a rect fully outside
// the viewport degenerates to an edge column/row, so such rects grow ~15% (min 24px) INTO the
// viewport, letting side panels adapt to real scene content instead of the 1px border line.
static void registerViewportLuminanceSamplerOnce() {
    static bool s_registered = false;
    if (s_registered) return;
    s_registered = true;
    ui::setLuminanceSampler(+[](ImVec2 mn, ImVec2 mx) -> float {
        if (!s_bdLive || !s_bdRenderer) return -1.f;
        ImVec2 cmn(std::min(std::max(mn.x, s_bdMin.x), s_bdMax.x),
                  std::min(std::max(mn.y, s_bdMin.y), s_bdMax.y));
        ImVec2 cmx(std::min(std::max(mx.x, s_bdMin.x), s_bdMax.x),
                  std::min(std::max(mx.y, s_bdMin.y), s_bdMax.y));
        const float kInset = std::max(24.f, (s_bdMax.x - s_bdMin.x) * 0.15f);
        if (cmx.x - cmn.x < 4.f) {
            if (cmn.x <= s_bdMin.x + 1.f) cmx.x = std::min(s_bdMin.x + kInset, s_bdMax.x);
            else                          cmn.x = std::max(s_bdMax.x - kInset, s_bdMin.x);
        }
        if (cmx.y - cmn.y < 4.f) {
            if (cmn.y <= s_bdMin.y + 1.f) cmx.y = std::min(s_bdMin.y + kInset, s_bdMax.y);
            else                          cmn.y = std::max(s_bdMax.y - kInset, s_bdMin.y);
        }
        return s_bdRenderer->averageLuminance(cmn.x - s_bdMin.x, cmn.y - s_bdMin.y,
                                              cmx.x - s_bdMin.x, cmx.y - s_bdMin.y);
    });
    // Dynamic-color bridge (contract §6): mean-RGB/luminance/variance stats for the same rect,
    // with the IDENTICAL clamp + inset-band fixup as the luminance lambda above. Returning false
    // (no live backdrop / no readback yet) makes consumers behave as if dynamicColor were off.
    ui::setBackdropStatsSampler(+[](ImVec2 mn, ImVec2 mx, ui::BackdropStats& out) -> bool {
        if (!s_bdLive || !s_bdRenderer) return false;
        ImVec2 cmn(std::min(std::max(mn.x, s_bdMin.x), s_bdMax.x),
                  std::min(std::max(mn.y, s_bdMin.y), s_bdMax.y));
        ImVec2 cmx(std::min(std::max(mx.x, s_bdMin.x), s_bdMax.x),
                  std::min(std::max(mx.y, s_bdMin.y), s_bdMax.y));
        const float kInset = std::max(24.f, (s_bdMax.x - s_bdMin.x) * 0.15f);
        if (cmx.x - cmn.x < 4.f) {
            if (cmn.x <= s_bdMin.x + 1.f) cmx.x = std::min(s_bdMin.x + kInset, s_bdMax.x);
            else                          cmn.x = std::max(s_bdMax.x - kInset, s_bdMin.x);
        }
        if (cmx.y - cmn.y < 4.f) {
            if (cmn.y <= s_bdMin.y + 1.f) cmx.y = std::min(s_bdMin.y + kInset, s_bdMax.y);
            else                          cmn.y = std::max(s_bdMax.y - kInset, s_bdMin.y);
        }
        float rgb[3], lum, var;
        if (!s_bdRenderer->averageBackdropStats(cmn.x - s_bdMin.x, cmn.y - s_bdMin.y,
                                                cmx.x - s_bdMin.x, cmx.y - s_bdMin.y,
                                                rgb, lum, var))
            return false;
        float h, sv, v;
        ImGui::ColorConvertRGBtoHSV(rgb[0], rgb[1], rgb[2], h, sv, v);
        out.lum = lum;
        out.variance = var;
        out.hue = h * 360.f;
        out.sat = sv;
        return true;
    });
}

// A6 (host): invalidate the published backdrop + stop the renderer's blur/luminance work outright
// whenever the Viewport window's content does not draw this frame (hidden dock tab -- ECAD/CAM
// Manufacture forward -- a Drawing tab, or the viewport hidden outright). Panels then take the
// palette-fallback surface automatically (adaptivePanel's l<0 path).
static void invalidateViewportBackdrop() {
    ui::setViewportBackdrop(ui::ViewportBackdropInfo{});
    s_bdLive = false;
    if (s_bdRenderer) s_bdRenderer->setBackdropEnabled(false);
}


enum class NewDocStage { Root, Drawing, Simulation };

static bool        s_newDocLauncherOpen = false;
static NewDocStage s_newDocStage = NewDocStage::Root;

static ImU32 withAlpha(const float rgba[4], float alpha) {
    return ImGui::ColorConvertFloat4ToU32(ImVec4(rgba[0], rgba[1], rgba[2], alpha));
}

static void drawNewDocPreview(ImDrawList* dl, ImVec2 p0, ImVec2 p1, int kind) {
    const auto& pal = ui::palette;
    const float S = ui::uiScale;
    const ImU32 accent = Palette::toU32(pal.accent);
    const ImU32 accentDim = Palette::toU32(pal.accentDim);
    const ImU32 border = withAlpha(pal.border, 0.95f);
    const ImU32 textDim = withAlpha(pal.textDim, 0.70f);
    const ImU32 pane = withAlpha(pal.headerBg, 0.82f);
    dl->AddRectFilled(p0, p1, pane, 5.0f * S);
    dl->AddRect(p0, p1, border, 5.0f * S, 0, std::max(1.0f, 1.0f * S));

    ImVec2 c((p0.x + p1.x) * 0.5f, (p0.y + p1.y) * 0.5f);
    float w = p1.x - p0.x, h = p1.y - p0.y;
    if (kind == 0) { // part
        ImVec2 a(c.x - w * 0.26f, c.y - h * 0.04f), b(c.x + w * 0.20f, c.y + h * 0.24f);
        ImVec2 off(w * 0.12f, -h * 0.18f);
        ImVec2 ao(a.x + off.x, a.y + off.y);
        ImVec2 bo(b.x + off.x, b.y + off.y);
        ImVec2 topRight(b.x + off.x, a.y + off.y);
        ImVec2 botLeft(a.x + off.x, b.y + off.y);
        dl->AddQuadFilled(ao, topRight, bo, botLeft,
                          withAlpha(pal.accent, 0.26f));
        dl->AddQuadFilled(a, ImVec2(b.x, a.y), b, ImVec2(a.x, b.y), withAlpha(pal.text, 0.20f));
        dl->AddLine(a, ao, accent, 1.4f * S);
        dl->AddLine(ImVec2(b.x, a.y), topRight, accent, 1.4f * S);
        dl->AddLine(b, bo, accent, 1.4f * S);
        ImVec2 cubePts[] = {ao, topRight, bo, botLeft, ao};
        dl->AddPolyline(cubePts, 5, accent, 0, 1.4f * S);
    } else if (kind == 1) { // assembly
        dl->AddRectFilled(ImVec2(c.x - w * 0.34f, c.y - h * 0.08f),
                          ImVec2(c.x - w * 0.02f, c.y + h * 0.20f), withAlpha(pal.accent, 0.28f), 3.0f * S);
        dl->AddRectFilled(ImVec2(c.x + w * 0.04f, c.y - h * 0.22f),
                          ImVec2(c.x + w * 0.33f, c.y + h * 0.06f), withAlpha(pal.text, 0.18f), 3.0f * S);
        dl->AddLine(ImVec2(c.x - w * 0.02f, c.y + h * 0.02f),
                    ImVec2(c.x + w * 0.04f, c.y - h * 0.08f), accent, 2.0f * S);
        dl->AddCircle(c, 4.0f * S, accent, 18, 1.8f * S);
    } else if (kind == 2) { // drawing
        ImVec2 s0(c.x - w * 0.34f, c.y - h * 0.28f), s1(c.x + w * 0.34f, c.y + h * 0.28f);
        dl->AddRectFilled(s0, s1, withAlpha(pal.text, 0.10f), 2.0f * S);
        dl->AddRect(s0, s1, border, 2.0f * S, 0, 1.1f * S);
        dl->AddCircle(ImVec2(c.x - w * 0.11f, c.y - h * 0.03f), 13.0f * S, accent, 24, 1.4f * S);
        dl->AddRect(ImVec2(c.x + w * 0.08f, c.y - h * 0.12f),
                    ImVec2(c.x + w * 0.25f, c.y + h * 0.05f), accentDim, 2.0f * S, 0, 1.2f * S);
        dl->AddLine(ImVec2(s0.x + 9.0f * S, s1.y - 14.0f * S),
                    ImVec2(s1.x - 9.0f * S, s1.y - 14.0f * S), textDim, 1.0f * S);
    } else if (kind == 3 || kind == 5) { // simulation/stress
        ImVec2 beam0(c.x - w * 0.34f, c.y - h * 0.02f), beam1(c.x + w * 0.34f, c.y + h * 0.15f);
        // Stress colormap: the app's err/warn/ok triple, so the tile's legend matches every other
        // hot->cold readout in the product.
        const ImU32 hot = withAlpha(pal.statusErr, 180.0f / 255.0f);
        const ImU32 mid = withAlpha(pal.statusWarn, 170.0f / 255.0f);
        const ImU32 cold = withAlpha(pal.statusOk, 150.0f / 255.0f);
        dl->AddRectFilledMultiColor(beam0, beam1, hot, mid, cold, cold);
        dl->AddRect(beam0, beam1, border, 2.0f * S, 0, 1.1f * S);
        for (int i = 0; i < 3; ++i) {
            float x = c.x - w * 0.22f + i * w * 0.22f;
            dl->AddLine(ImVec2(x, c.y - h * 0.30f), ImVec2(x, beam0.y - 2.0f * S), accent, 1.5f * S);
            dl->AddTriangleFilled(ImVec2(x - 4.0f * S, beam0.y - 4.0f * S),
                                  ImVec2(x + 4.0f * S, beam0.y - 4.0f * S),
                                  ImVec2(x, beam0.y + 4.0f * S), accent);
        }
    } else if (kind == 4) { // pcb
        ImVec2 b0(c.x - w * 0.34f, c.y - h * 0.23f), b1(c.x + w * 0.34f, c.y + h * 0.24f);
        dl->AddRectFilled(b0, b1, withAlpha(pal.accentDim, 170.0f / 255.0f), 5.0f * S);
        dl->AddRect(b0, b1, accent, 5.0f * S, 0, 1.2f * S);
        for (int i = 0; i < 4; ++i) {
            float x = b0.x + (i + 1) * (b1.x - b0.x) / 5.0f;
            dl->AddCircleFilled(ImVec2(x, c.y - h * 0.08f), 4.0f * S,
                                withAlpha(pal.statusWarn, 210.0f / 255.0f));
            dl->AddLine(ImVec2(x, c.y - h * 0.08f), ImVec2(c.x, c.y + h * 0.14f),
                        withAlpha(pal.statusWarn, 190.0f / 255.0f), 1.3f * S);
        }
    } else if (kind == 6) { // fluids
        ImVec2 ch0(c.x - w * 0.34f, c.y - h * 0.18f), ch1(c.x + w * 0.34f, c.y + h * 0.18f);
        dl->AddRect(ch0, ch1, border, 8.0f * S, 0, 1.2f * S);
        for (int i = 0; i < 4; ++i) {
            float y = ch0.y + (i + 1) * (ch1.y - ch0.y) / 5.0f;
            dl->PathClear();
            dl->PathLineTo(ImVec2(ch0.x + 8.0f * S, y));
            dl->PathBezierCubicCurveTo(ImVec2(c.x - 16.0f * S, y - 12.0f * S),
                                       ImVec2(c.x + 16.0f * S, y + 12.0f * S),
                                       ImVec2(ch1.x - 8.0f * S, y));
            dl->PathStroke(accent, 0, 1.5f * S);
        }
    } else if (kind == 7) { // electrical
        dl->AddCircleFilled(ImVec2(c.x - w * 0.20f, c.y), 9.0f * S, accentDim);
        dl->AddCircleFilled(ImVec2(c.x + w * 0.22f, c.y - h * 0.12f), 9.0f * S, accentDim);
        ImVec2 tracePts[] = {ImVec2(c.x - w * 0.20f, c.y), ImVec2(c.x - w * 0.02f, c.y),
                             ImVec2(c.x + w * 0.06f, c.y - h * 0.12f),
                             ImVec2(c.x + w * 0.22f, c.y - h * 0.12f)};
        dl->AddPolyline(tracePts, 4, accent, 0, 2.0f * S);
        ImVec2 boltPts[] = {ImVec2(c.x - 5.0f * S, c.y - h * 0.25f),
                            ImVec2(c.x + 5.0f * S, c.y - 4.0f * S),
                            ImVec2(c.x - 2.0f * S, c.y - 4.0f * S),
                            ImVec2(c.x + 8.0f * S, c.y + h * 0.22f)};
        dl->AddPolyline(boltPts, 4, withAlpha(pal.statusWarn, 230.0f / 255.0f), 0, 2.0f * S);
    } else if (kind == 8) { // open existing — an open folder with a file lifting out
        ImVec2 f0(c.x - w * 0.32f, c.y - h * 0.10f), f1(c.x + w * 0.32f, c.y + h * 0.26f);
        dl->AddRectFilled(ImVec2(f0.x, f0.y - h * 0.10f), ImVec2(f0.x + w * 0.26f, f0.y + 2.0f * S),
                          withAlpha(pal.accent, 0.30f), 2.0f * S);   // folder tab
        dl->AddRectFilled(f0, f1, withAlpha(pal.accent, 0.20f), 3.0f * S);
        dl->AddRect(f0, f1, accent, 3.0f * S, 0, 1.3f * S);
        // a document sheet emerging upward out of the folder
        ImVec2 d0(c.x - w * 0.12f, c.y - h * 0.30f), d1(c.x + w * 0.12f, c.y + h * 0.06f);
        dl->AddRectFilled(d0, d1, withAlpha(pal.text, 0.16f), 2.0f * S);
        dl->AddRect(d0, d1, border, 2.0f * S, 0, 1.1f * S);
        for (int i = 0; i < 3; ++i) {
            float y = d0.y + (i + 1) * (d1.y - d0.y) / 4.5f;
            dl->AddLine(ImVec2(d0.x + 4.0f * S, y), ImVec2(d1.x - 4.0f * S, y), textDim, 1.0f * S);
        }
    }
}

static bool newDocTile(const char* id, const char* title, const char* desc,
                       int previewKind, ImVec2 size) {
    const auto& pal = ui::palette;
    const float S = ui::uiScale;
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::InvisibleButton(id, size);
    bool hovered = ImGui::IsItemHovered();
    bool clicked = ImGui::IsItemClicked(ImGuiMouseButton_Left);
    ImVec2 p1(p0.x + size.x, p0.y + size.y);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImU32 fill = Palette::toU32(hovered ? pal.buttonHovered : pal.frameBg);
    dl->AddRectFilled(p0, p1, fill, 7.0f * S);
    dl->AddRect(p0, p1, Palette::toU32(hovered ? pal.accentMid : pal.border),
                7.0f * S, 0, std::max(1.0f, hovered ? 1.5f * S : 1.0f * S));
    ImVec2 pv0(p0.x + 9.0f * S, p0.y + 9.0f * S);
    ImVec2 pv1(p1.x - 9.0f * S, p0.y + size.y * 0.58f);
    drawNewDocPreview(dl, pv0, pv1, previewKind);
    ImVec2 ts = ImGui::CalcTextSize(title);
    dl->AddText(ImVec2(p0.x + 12.0f * S, pv1.y + 9.0f * S), Palette::toU32(pal.text), title);
    dl->AddText(ImVec2(p0.x + 12.0f * S, pv1.y + 30.0f * S),
                withAlpha(pal.textDim, 0.95f), desc);
    if (hovered)
        dl->AddLine(ImVec2(p0.x + 12.0f * S, p1.y - 10.0f * S),
                    ImVec2(p0.x + 12.0f * S + std::min(ts.x, size.x - 24.0f * S), p1.y - 10.0f * S),
                    Palette::toU32(pal.accent), 2.0f * S);
    return clicked;
}

// Task 02: non-intrusive "AI editing" badge painted over the 3D view while the in-app copilot
// (agent_run_v2) is driving the document, so the user SEES that the AI — not they — is making the
// edits that land live in the viewport. Driven off the same run lifecycle (agentV2Running /
// agentV2Status) that feeds the Activity panel, so panel and viewport tell one story. Uses the
// shared viewport-pill palette so it reads like the other 3D-view overlays. Top-center, gentle
// pulse, single short line — the Activity panel carries the full per-stage detail.
static void drawAiActivityBadge(ImVec2 vr0, ImVec2 vr1, const std::string& status) {
    const auto& pal = ui::palette;
    const float S = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();

    // Pulse reads as "live/working" without flashing (0.55..1.0).
    float pulse = 0.55f + 0.45f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 3.0f));

    std::string label = "AI editing";
    if (!status.empty()) {
        std::string s = status;
        const size_t kMax = 40;
        if (s.size() > kMax) s = s.substr(0, kMax - 1) + "\xE2\x80\xA6";  // ellipsis
        label += "  \xC2\xB7  " + s;  // middle dot
    }

    ImVec2 ts = ImGui::CalcTextSize(label.c_str());
    const float padX = 10.0f * S, padY = 4.0f * S, dotR = 4.0f * S, gap = 6.0f * S;
    float w = padX + dotR * 2.0f + gap + ts.x + padX;
    float h = ts.y + padY * 2.0f;
    float cx = (vr0.x + vr1.x) * 0.5f;
    ImVec2 p0(cx - w * 0.5f, vr0.y + 10.0f * S);
    ImVec2 p1(p0.x + w, p0.y + h);

    // Liquid-glass v2 (H6a): adaptive UltraThin surface instead of the fixed glassBackground fill
    // so the badge's contrast tracks the live scene under it. Accent pulse border/dot stay
    // palette (a semantic "live" cue, not surface text).
    const ui::AdaptiveSurface& surf =
        ui::adaptivePanel(ui::panelId("vp_ai_badge"), ImRect(p0, p1), ui::GlassGroup::Hud);
    ui::glassPanel(dl, ImRect(p0, p1), ui::GlassLevel::UltraThin, h * 0.5f / S, surf);
    dl->AddRect(p0, p1, withAlpha(pal.accent, 0.35f + 0.45f * pulse), h * 0.5f, 0, 1.5f * S);
    ImVec2 dotc(p0.x + padX + dotR, (p0.y + p1.y) * 0.5f);
    dl->AddCircleFilled(dotc, dotR, withAlpha(pal.accent, pulse));
    ui::glassText(dl, nullptr, ImGui::GetFontSize(),
                 ImVec2(dotc.x + dotR + gap, p0.y + padY), surf, label.c_str());
}

// ---- Tab-switch loading state ------------------------------------------------------
// Clicking a document tab whose model isn't loaded yet wipes the scene (unloadDocMesh) and then
// stays silent while the async doc_activate + aicm mesh load run — the click reads as dropped.
// tabModelLoading() derives "the ACTIVE part/asm tab's model is still on its way" from existing
// AppState state only (no new fields), and drawTabLoadingOverlay() dims the 3D band with a frosted
// glass scrim + centered brand mark and busy label until the real mesh commits. The overlay
// self-clears the frame drainMeshLoads() commits (hasDocMesh flips true and the session's
// cachedDocMesh fills).
static bool tabModelLoading(const AppState& st) {
    if (st.activeTab < 0 || st.activeTab >= (int)st.docTabs.size()) return false;
    const AppState::DocTab& tab = st.docTabs[st.activeTab];
    // Drawing/Schematic/PCB tabs own the central canvas themselves — 3D viewport only.
    if (tab.kind != AppState::DocTab::TabKind::Part &&
        tab.kind != AppState::DocTab::TabKind::Assembly)
        return false;
    if (tab.docId.empty()) return false;
    // A live pushed revision can commit this tab's mesh while its doc_activate is still queued
    // behind an agent that holds the doc's engine lock (engine_events applyChangedActive adopts
    // the snapshot the daemon pushed with doc.changed, then loads the mesh itself — it does not
    // wait for the activation). The geometry is on screen; claiming "Loading" over it is a lie,
    // and it is exactly the case the user hits while spectating an AI build. Whatever is still
    // in flight reports through the status-bar rebuild chip instead.
    if (st.hasDocMesh && st.docMirror.docId() == tab.docId) return false;
    // This tab's doc_activate is coalesced behind another in-flight activation
    // (switchToTab's async-rebuild-coalesced branch).
    if (st.pendingActivateTab == st.activeTab) return true;
    // A doc_activate is in flight and the live mirror is not this tab's doc yet — i.e. the
    // in-flight activation is THIS tab's. A warm/loaded active tab always has a matching mirror,
    // so a background tab's in-flight activation never trips this.
    if (st.activationInFlight && st.docMirror.docId() != tab.docId) return true;
    // Activation landed (mirror adopted, aicm parked on the session); the loader thread is
    // parsing and drainMeshLoads hasn't committed yet. Blank/empty docs never reach this: no
    // aicm is parked for them, and the empty-doc commit fills cachedDocMesh immediately.
    if (!st.hasDocMesh && st.docMirror.docId() == tab.docId && tab.session &&
        !tab.session->cachedAicmPath.empty() && !tab.session->cachedDocMesh)
        return true;
    return false;
}

static void drawTabLoadingOverlay(AppState& st, const ImVec2& vr0, const ImVec2& vr1,
                                  const ImVec2& cr0, const ImVec2& cr1) {
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float S = ui::uiScale;
    // Frosted full-bleed scrim over the 3D band — the wiped scene reads as cleared, and the
    // adaptive surface keeps the dim theme-correct over the live backdrop (palette-only colors).
    // Painted before the HUD pills below so status chrome (busy pill, AI badge) stays on top.
    const ui::AdaptiveSurface& surf =
        ui::adaptivePanel(ui::panelId("vp_tab_loading"), ImRect(vr0, vr1), ui::GlassGroup::Hud);
    ui::glassPanel(dl, ImRect(vr0, vr1), ui::GlassLevel::Regular, 0.0f, surf);

    // Centered in the VISIBLE content rect (cr0/cr1 excludes the pinned glass columns): the app
    // logo with a gentle alpha pulse (same pulse idiom as the AI badge) over a busyLabel
    // spinner + "Loading <tab>…". Logo texture can be invalid headless — fall back to text only.
    const AppState::DocTab& tab = st.docTabs[st.activeTab];
    const ImVec2 ctr((cr0.x + cr1.x) * 0.5f, (cr0.y + cr1.y) * 0.5f);
    const float pulse = 0.55f + 0.45f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 3.0f));

    const Tex& logo = st.logoIcon;
    const bool logoOk = logo.id != 0 && logo.w > 0 && logo.h > 0;
    const float logoH = logoOk ? 64.0f * S : 0.0f;
    const float logoW = logoOk ? logoH * (float)logo.w / (float)logo.h : 0.0f;
    const float rowH  = ImGui::GetTextLineHeight();
    const float gap   = 14.0f * S;
    float y = ctr.y - (logoH + (logoOk ? gap : 0.0f) + rowH) * 0.5f;

    if (logoOk) {
        dl->AddImage((ImTextureID)(intptr_t)logo.id,
                     ImVec2(ctr.x - logoW * 0.5f, y), ImVec2(ctr.x + logoW * 0.5f, y + logoH),
                     ImVec2(0, 0), ImVec2(1, 1),
                     ImGui::ColorConvertFloat4ToU32(ImVec4(1.0f, 1.0f, 1.0f, pulse)));
        y += logoH + gap;
    }

    std::string title = tab.displayTitle();
    const size_t kMax = 48;
    if (title.size() > kMax) title = title.substr(0, kMax - 1) + "\xE2\x80\xA6";  // ellipsis
    const std::string label = "Loading " + title + "\xE2\x80\xA6";
    // busyLabel draws its brand-mark spinner in a line-height square, then the label inline.
    const float rowW = rowH + ImGui::GetStyle().ItemInnerSpacing.x +
                       ImGui::CalcTextSize(label.c_str()).x;
    const ImVec2 savedCursor = ImGui::GetCursorScreenPos();
    ImGui::SetCursorScreenPos(ImVec2(ctr.x - rowW * 0.5f, y));
    ui::busyLabel(label.c_str(), ImGui::ColorConvertFloat4ToU32(surf.textPrimary));
    ImGui::SetCursorScreenPos(savedCursor);
}

// drawNewDocChooser is declared in viewport_panel.h (public — hosted at app scope).

// The document strip is drawn by the central view window itself. That keeps Part/Assembly tabs
// contained in the view area while reserving height before the 3D canvas or drawing sheet.
static void vpDockStripDraw(AppState& st) {
    ImGuiDockNode* node = ImGui::GetWindowDockNode();
    // Always hide the node's own window tab — the redundant "Viewport"/"ECAD" label should never
    // show (the document tabs carry identity; before any file is open the row is simply empty).
    if (node) node->LocalFlags |= ImGuiDockNodeFlags_NoTabBar;
    if (ui::docTabStripHeight(st) > 0.0f) ui::drawDocTabs(st, ImGui::GetContentRegionAvail().x);
}

static void closeNewDocPopup() {
    s_newDocLauncherOpen = false;
    s_newDocStage = NewDocStage::Root;
    ImGui::CloseCurrentPopup();
}

static void runSimulationChoice(AppState& st, const char* cmd) {
    st.showSimulationTools = true;
    st.showRibbon = true;
    st.ribbonTab = AppState::RT_Simulation;
    st.leftPaneTab = AppState::LP_Simulation;
    if (st.ribbonDispatch) st.ribbonDispatch(cmd);
}

void drawNewDocChooser(AppState& st) {
    const auto& pal = ui::palette;
    const float S = ui::uiScale;
    // The doc-strip "+" / Ctrl+N raise this one-shot request; consume it into the launcher state.
    if (st.wantNewDocChooser) {
        st.wantNewDocChooser = false;
        s_newDocLauncherOpen = true;
        s_newDocStage = NewDocStage::Root;
    }
    if (s_newDocLauncherOpen) ImGui::OpenPopup("Create New##viewport_new_doc");
    // Center in the main viewport (hosted at app scope now, so position it explicitly).
    ImGuiViewport* mv = ImGui::GetMainViewport();
    ImGui::SetNextWindowPos(ImVec2(mv->Pos.x + mv->Size.x * 0.5f, mv->Pos.y + mv->Size.y * 0.5f),
                            ImGuiCond_Appearing, ImVec2(0.5f, 0.5f));
    ImGui::SetNextWindowSize(ImVec2(690.0f * S, 430.0f * S), ImGuiCond_Appearing);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(16.0f * S, 14.0f * S));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(10.0f * S, 10.0f * S));
    ImGui::PushStyleColor(ImGuiCol_PopupBg, Palette::toU32(pal.popupBg));
    if (ImGui::BeginPopupModal("Create New##viewport_new_doc", nullptr,
                               ImGuiWindowFlags_NoSavedSettings | ImGuiWindowFlags_NoResize)) {
        const char* title = s_newDocStage == NewDocStage::Root ? "Create new file" :
                            s_newDocStage == NewDocStage::Drawing ? "Drawing source" :
                            "Simulation type";
        const char* sub = s_newDocStage == NewDocStage::Root
            ? "Choose a workspace. Each tile opens the matching document or setup flow."
            : s_newDocStage == NewDocStage::Drawing
                ? "Drawings are generated from an active part or assembly source model."
                : "Start a study and surface the simulation tools.";
        ImGui::TextUnformatted(title);
        ImGui::TextColored(ImVec4(pal.textDim[0], pal.textDim[1], pal.textDim[2], 1), "%s", sub);
        ImGui::Separator();

        const float gap = 10.0f * S;
        const float avail = ImGui::GetContentRegionAvail().x;
        const int cols = (avail > 600.0f * S) ? 3 : 2;
        const float tileW = (avail - gap * (cols - 1)) / cols;
        const float tileH = 132.0f * S;
        int i = 0;
        auto next = [&]() {
            if (++i % cols != 0) ImGui::SameLine(0, gap);
        };
        auto finish = [&]() {
            closeNewDocPopup();
        };

        if (s_newDocStage == NewDocStage::Root) {
            if (newDocTile("##new_part", "Part", "Parametric 3D model", 0, ImVec2(tileW, tileH))) {
                ui::createNewDoc(st, 0); finish();
            } next();
            if (newDocTile("##new_assembly", "Assembly", "Components and mates", 1, ImVec2(tileW, tileH))) {
                ui::createNewDoc(st, 1); finish();
            } next();
            if (newDocTile("##new_drawing", "Drawing", "Projected shop sheet", 2, ImVec2(tileW, tileH))) {
                s_newDocStage = NewDocStage::Drawing;
            } next();
            if (newDocTile("##new_sim", "Simulation", "Stress, flow, electrical", 3, ImVec2(tileW, tileH))) {
                s_newDocStage = NewDocStage::Simulation;
            } next();
            if (newDocTile("##new_pcb", "PCB", "Board layout workspace", 4, ImVec2(tileW, tileH))) {
                st.showPcbTools = true; st.showRibbon = true; st.ribbonTab = AppState::RT_PCB;
                ui::createNewDoc(st, 4); finish();
            } next();
            // Open an existing document instead of creating one — routes through the normal deferred
            // open pump (native dialog + recent), and the untouched starter part is auto-discarded.
            if (newDocTile("##open_existing", "Open existing", "Browse for a saved file", 8, ImVec2(tileW, tileH))) {
                st.requestOpenDialog = true; finish();
            }
        } else if (s_newDocStage == NewDocStage::Drawing) {
            if (newDocTile("##drawing_active", "From active model", "Use current part/assembly", 2, ImVec2(tileW, tileH))) {
                ui::createNewDoc(st, 2); finish();
            } next();
            if (newDocTile("##drawing_part", "New source part", "Start model first", 0, ImVec2(tileW, tileH))) {
                ui::createNewDoc(st, 0);
                st.toast(ui::ToastLevel::Info, "Model the part, then create its drawing.");
                finish();
            } next();
            if (newDocTile("##drawing_assembly", "New source assembly", "Build assembly first", 1, ImVec2(tileW, tileH))) {
                ui::createNewDoc(st, 1);
                st.toast(ui::ToastLevel::Info, "Build the assembly, then create its drawing.");
                finish();
            }
        } else {
            if (newDocTile("##sim_stress", "Stress FEA", "Static structural study", 5, ImVec2(tileW, tileH))) {
                runSimulationChoice(st, "sim_new_static"); finish();
            } next();
            if (newDocTile("##sim_fluids", "Fluids", "Flow field study", 6, ImVec2(tileW, tileH))) {
                runSimulationChoice(st, "sim_new_flow"); finish();
            } next();
            if (newDocTile("##sim_thermal", "Thermal FEA", "Steady heat conduction in the part", 6, ImVec2(tileW, tileH))) {
                runSimulationChoice(st, "sim_new_thermal_fea"); finish();
            } next();
            if (newDocTile("##sim_electrical", "Electrical", "Circuit simulation", 7, ImVec2(tileW, tileH))) {
                st.showPcbTools = true; st.showRibbon = true; st.ribbonTab = AppState::RT_PCB;
                ui::createNewDoc(st, 3);
                st.ecadSimAnalysis = 0;
                st.toast(ui::ToastLevel::Info, "Schematic created. Add sources, then run electrical simulation.");
                finish();
            }
        }

        ImGui::SetCursorPosY(ImGui::GetWindowHeight() - 42.0f * S);
        if (s_newDocStage != NewDocStage::Root) {
            if (ImGui::Button("Back", ImVec2(92.0f * S, 0))) s_newDocStage = NewDocStage::Root;
            ImGui::SameLine();
        }
        if (ImGui::Button("Cancel", ImVec2(92.0f * S, 0))) closeNewDocPopup();
        ImGui::EndPopup();
    }
    ImGui::PopStyleColor();
    ImGui::PopStyleVar(2);
}

// Lenient pan clamp: stop the model's bounding sphere from drifting fully out of the viewport while
// panning / zoom-to-cursor, WITHOUT being so tight you can't push an edge feature past center to
// inspect it. kPanLeniency is the extra travel (in model radii) allowed past the view edge — any
// surface point can still reach screen-center. Rollback: set kClampPan = false to restore the old
// free-pan behavior. (Intentionally not "tight": the model may sit mostly off-screen at high zoom.)
static constexpr bool  kClampPan    = true;
static constexpr float kPanLeniency = 1.25f;

// C25 W3-J: zoom-area mode state.
static bool  s_zoomAreaArmed = false;
static bool  s_zoomAreaDragging = false;
static ImVec2 s_zoomAreaP0 = {0, 0};  // drag start (screen)
static ImVec2 s_zoomAreaP1 = {0, 0};  // drag current (screen)

// SolidWorks-style "rotate while sketching": once the user MMB-orbits inside a sketch, stop forcing
// the normal-to-plane lock so they can view the sketch from any angle (reset on sketch exit / on
// re-entry, which goes through the !inSketch reset below). The sketch overlay projects live each
// frame, so a rotated view stays correct.
static bool   s_sketchRotated  = false;

// C37-C: LMB drag-threshold state — suppress orbit micro-movement on click-selects.
static bool   s_lmbDragActive  = false;  // true once the 4px threshold has been crossed
static ImVec2 s_lmbDragOrigin  = {0, 0}; // screen position of the LMB press
struct SimulationFaceDrag {
    std::string docId;
    nlohmann::json selector;
    render::PickHit hit;
    long revision = -1;
};
static SimulationFaceDrag s_simFaceDrag;

// Evaluate Section Props: which plane handle is being dragged along its normal (-1 = none).
// Owns the mouse while >= 0 so orbit/pan/pick never fight the cut-plane drag.
static int s_sectionDragIdx = -1;

// ---- Instant3D push-pull (drag-to-modify a face) -------------------------------------------
// When EXACTLY ONE face is selected we draw a normal-axis drag handle (a short ⟂ arrow + its
// mirror) at the face centroid. Pressing on the arrow starts a push-pull drag instead of an orbit:
// the cursor's screen motion is projected onto the face-normal's screen direction and converted
// px→mm at the face depth, then fed to doc_push_pull through the checkpoint/revert envelope.
struct PushPullDrag {
    bool      active     = false;     // a push-pull drag is in flight (owns the mouse, no orbit)
    std::string docId;                // document owning this drag's checkpoint
    uint32_t  bodyId     = 0;         // PickHit body (1-based) of the dragged face
    uint32_t  topoId     = 0;         // picked FACE topo_id (1-based)
    std::string checkpoint;           // doc_checkpoint id taken on press (for revert/cancel)
    ImVec2    pressPos    = {0, 0};    // screen pos at press (drag origin)
    float     nsx = 0, nsy = 0;       // unit screen-space direction of the face OUTWARD normal
    float     worldPerPx  = 1.0f;     // world(mm) per screen pixel at the face depth (this frame)
    double    lastApplied = 1e30f;    // last cumulative distance actually applied (readout)
    double    curDist     = 0.0;      // current cumulative signed distance (mm)
    // Live readout from the most recent draggable response (shown near the cursor).
    bool        haveReadout = false;
    std::string param;                // e.g. "depth"
    double      newValue  = 0.0;
    bool        clamped   = false;
    // Single-in-flight latest-wins (revert+push_pull pair on AsyncRpc FIFO).
    uint64_t  gen         = 0;
    bool      inFlight    = false;
    bool      pending     = false;    // curDist changed while a sample was open
    bool      wantCommit  = false;    // next kick uses commit=true
    bool      finishing   = false;    // mouse-up seen; don't re-queue every frame
    // Persists across the frame so the readout chip can draw even on a no-op sample.
    void reset() { const auto nextGen = gen + 1; *this = PushPullDrag{}; gen = nextGen; }
};
static PushPullDrag s_pp;

// ---- Instant3D dimension-handle drag (drag a FEATURE'S DIMENSION) --------------------------
// Complements the face push-pull (W8): when a FEATURE is selected (in the tree or via its
// geometry) — even with NO PropertyManager form open — we render its key linear/angular
// dimensions as draggable chips anchored near the feature's geometry. Dragging a dimension
// handle sets that ONE param to a new ABSOLUTE value live via edit_feature, mirroring the PM
// preview path: doc_checkpoint on press, then per sample doc_revert + applyOps(edit_feature with
// the full params, one field changed) + loadDocMesh, and on release one committed edit_feature
// (single undo entry). Esc cancels (revert). Throttled to one in-flight call.
//
// Which params are draggable is read from the PM-form field table (pm_forms): every enabled Dim
// field whose param exists on the feature is a candidate. Angle dims (unitLabel " [deg]") drag in
// degrees; length dims drag in mm using the same 1mm-world-probe px->scale as ppHandleGeometry.
//
// Precedence vs the W8 face arrow: the face arrow wins. The dimension handles are SUPPRESSED for
// the feature whenever a single FACE is selected (that face shows the push-pull arrow). They show
// when a feature is selected with no single drivable face under the cursor (tree selection, a
// body/edge pick, or a multi-face selection). This keeps the two gestures from ever overlapping.
struct DimDrag {
    bool        active   = false;     // a dimension drag is in flight (owns the mouse, no orbit)
    std::string docId;                // document owning the PM field being dragged
    std::string featureId;            // the feature being edited
    std::string param;                // the param key being driven ("depth"/"radius"/"angle"/…)
    bool        angle    = false;     // true => value is degrees (drag maps px->deg)
    bool        allowZero = false;    // 0 is a valid value (draft/twist/offset) => drag floor is 0
    // PMFieldDef range metadata (canonical mm/deg). rangeMax > rangeMin => BOUNDED field (revolve
    // angle 0..360, draft 0..89, k-factor 0..1) — the drag clamps to [rangeMin, rangeMax] and the
    // allowZero/size-floor heuristic is bypassed. Signed offsets carry rangeMin = -1e9.
    double      rangeMin = 0.0;
    double      rangeMax = 0.0;
    std::string label;                // PM label ("Depth"/"Radius"/"Angle"/…)
    std::string checkpoint;           // doc_checkpoint id taken on press (for revert/cancel)
    nlohmann::json baseParams;        // the feature's params at press (full; one field changes)
    nlohmann::json baseRefs;          // the feature's refs at press (emitted unchanged)
    double      oldValue = 0.0;       // value before the drag
    double      curValue = 0.0;       // current applied value
    double      lastApplied = 1e30;   // last value actually sent (skip dup samples)
    double      lastFireT = -1e9;     // last time a drag sample fired a preview (rate throttle)
    bool        clamped  = false;     // last sample hit the min/range clamp
    ImVec2      pressPos = {0, 0};    // screen pos at press (drag origin)
    float       dsx = 0, dsy = 0;     // unit screen-space drag direction (value grows along this)
    float       worldPerPx = 1.0f;    // mm per screen px along the measured world direction
    float       degPerPx   = 0.5f;    // deg per screen px (angle dims)
    bool        haveReadout = false;  // show the readout chip
    // Extrude-like depth + Reverse direction: when the PM has a "flip" Check, dragging the depth
    // arrow past the sketch plane toggles flip and keeps depth = |signed| (engine requires depth>0).
    // flipAtPress is the stable origin so multiple zero-crossings stay consistent; the press-time
    // screen axis is frozen (no per-frame re-derive) so the arrow reorient after flip doesn't invert
    // the delta mid-drag.
    bool        autoFlip = false;
    bool        flipAtPress = false;
    bool        curFlip = false;
    bool        lastFlipApplied = false;
    void reset() { *this = DimDrag{}; }
};
static DimDrag s_dim;

// ---- Move-Component drag (drag a COMPONENT in an ASSEMBLY) ----------------------------------
// When the open doc is an assembly and exactly one component body is selected, pressing on that
// body's geometry starts a constrained move-component drag instead of an orbit. The cursor's
// screen motion is projected onto the camera screen-plane at the component's world depth to form a
// world-space TARGET point, which is fed to doc_solve_mates {drag:{component, target}}. The engine
// soft-pulls that one component toward the target along its free DOF (mates re-seat the rest) and
// bumps the revision once on the committed solve = one undo. Mirrors the push-pull live-drag
// envelope: doc_checkpoint on press, transient solves during drag, revert + one final solve on
// release (so the drag is a SINGLE undo step), revert on Esc/cancel. Owns the mouse while active.
struct AsmDrag {
    bool        active   = false;     // a move-component drag is in flight (owns the mouse)
    std::string docId;                // assembly owning this drag's checkpoint
    std::string component;            // the component k-id being dragged
    std::string checkpoint;           // doc_checkpoint id taken on press (for the single-undo commit)
    ImVec2      pressPos = {0, 0};    // screen pos at press (drag origin)
    Vec3        startPos;             // component world origin at press (transform translation)
    Vec3        camR, camU;           // camera screen-right / screen-up (world) captured at press
    float       worldPerPx = 1.0f;    // world(mm) per screen pixel at the component depth (at press)
    ImVec2      lastSent = {1e30f, 1e30f};  // last cursor sample actually kicked (throttle)
    ImVec2      curPos   = {0, 0};    // latest mouse (for pending)
    // Single-in-flight latest-wins (revert + doc_solve_mates pair).
    uint64_t    gen      = 0;
    bool        inFlight = false;
    bool        pending  = false;
    bool        wantCommit = false;
    bool        finishing  = false;   // mouse-up seen; don't re-queue every frame
    void reset() { const auto nextGen = gen + 1; *this = AsmDrag{}; gen = nextGen; }
};
static AsmDrag s_asmDrag;

// One draggable dimension of the selected feature: the param key, its PM label, whether it is an
// angle, the world anchor point (where the handle sits), and the unit world DIRECTION the value is
// measured along (a length grows by +1 value per +1mm along dirW; an angle ignores dirW).
struct DimCandidate {
    std::string param;
    std::string label;
    bool        angle = false;
    bool        allowZero = false;  // 0 is a valid value (draft/twist/offset) -> drag floor is 0
    double      rangeMin = 0.0;     // PMFieldDef::dimMin (canonical mm/deg)
    double      rangeMax = 0.0;     // PMFieldDef::dimMax; > rangeMin => bounded field
    double      value = 0.0;        // current stored value (mm or deg)
    float       anchor[3] = {0, 0, 0};
    Vec3        dirW;               // unit world direction the dim is measured along (length dims)
};

// Find the feature object (by id) in the mirror tree. Returns nullptr if absent / not a part doc.
static const nlohmann::json* findFeature(const nlohmann::json& tree, const std::string& fid) {
    if (fid.empty() || !tree.is_object()) return nullptr;
    auto feat = tree.find("features");
    if (feat == tree.end() || !feat->is_array()) return nullptr;
    for (const auto& f : *feat)
        if (f.is_object() && f.value("id", std::string()) == fid) return &f;
    return nullptr;
}

// Current stored value of a dim-or-number param (resolves the {value,expr} shape or a bare number).
// Returns false when absent, non-numeric, or expression-driven (a drag must never stomp an expr).
static bool dimParamValue(const nlohmann::json& params, const std::string& key, double& out,
                          bool& isExpr) {
    isExpr = false;
    auto it = params.find(key);
    if (it == params.end()) return false;
    if (it->is_object()) {
        auto ev = it->find("expr");
        if (ev != it->end() && ev->is_string() && !ev->get<std::string>().empty()) {
            isExpr = true; return false;   // equation-driven — not draggable
        }
        auto vv = it->find("value");
        if (vv == it->end() || !vv->is_number()) return false;
        out = vv->get<double>(); return true;
    }
    if (it->is_number()) { out = it->get<double>(); return true; }
    return false;
}

// World direction a given dim is measured along (length dims only; angle dims ignore it). The map
// mirrors the box/extrude conventions the engine push-pull uses (x->length, y->width, z->height)
// and falls back to world-X for "size" dims that have no principal axis (radius/thickness/diameter).
static Vec3 dimWorldDir(const std::string& param) {
    if (param == "width")                              return Vec3(0, 1, 0);
    if (param == "height" || param == "depth" ||
        param == "depth2" || param == "offset" ||
        param == "pitch"  || param == "thickness")     return Vec3(0, 0, 1);
    // length/radius/radius2/diameter/distance/spacing/dx/dy and any other size dim grow along +X.
    return Vec3(1, 0, 0);
}

// World anchor for the selected feature's handles: the centre of the union AABB of the bodies the
// feature owns (owner map), else any cached face/edge centroid the feature owns, else the scene
// centre. Also returns a representative extent (half-diagonal) used to space the handles out.
static bool featureAnchor(AppState& st, const std::string& fid, float out[3], float& extent) {
    float bmin[3] = { 1e30f,  1e30f,  1e30f};
    float bmax[3] = {-1e30f, -1e30f, -1e30f};
    bool got = false;
    auto grow = [&](const float* b /*6*/) {
        for (int i = 0; i < 3; ++i) {
            if (b[i]   < bmin[i]) bmin[i] = b[i];
            if (b[i+3] > bmax[i]) bmax[i] = b[i+3];
        }
        got = true;
    };
    const auto& om = st.docMirror.ownerMap();
    auto oit = om.find(fid);
    if (oit != om.end()) {
        for (uint32_t bodyId : oit->second.bodies) {
            auto bit = st.bodyBBoxCache.find(bodyId);
            if (bit != st.bodyBBoxCache.end()) grow(bit->second.data());
        }
        if (!got) {  // no body bbox (dress-up feature owns faces, not whole bodies): use its faces
            for (const auto& bf : oit->second.faces) {
                uint64_t key = ((uint64_t)bf.first << 32) | (uint64_t)bf.second;
                auto fbit = st.faceBBoxCache.find(key);
                if (fbit != st.faceBBoxCache.end()) grow(fbit->second.data());
            }
        }
    }
    if (got) {
        out[0] = (bmin[0] + bmax[0]) * 0.5f;
        out[1] = (bmin[1] + bmax[1]) * 0.5f;
        out[2] = (bmin[2] + bmax[2]) * 0.5f;
        float dx = bmax[0] - bmin[0], dy = bmax[1] - bmin[1], dz = bmax[2] - bmin[2];
        extent = 0.5f * std::sqrt(dx*dx + dy*dy + dz*dz);
        if (extent < 1.0f) extent = 1.0f;
        return true;
    }
    // Whole-scene fallback (e.g. owner map not shipped): centre on the model.
    if (st.renderer) {
        out[0] = 0; out[1] = 0; out[2] = 0;   // pan-relative model centre is the orbit target
        extent = st.renderer->modelRadius();
        if (extent < 1.0f) extent = 1.0f;
        return true;
    }
    return false;
}

// TASK A: constrain + orient the extrude (and any planar-cap) DEPTH handle to the real geometry.
// dimWorldDir() only knows a hardcoded axis map (depth -> world +Z), so for a sketch on any plane
// other than the top the depth arrow pointed the WRONG way and, anchored at the AABB volume-centre,
// floated inside the solid instead of sitting on the extruded surface. Here we recover the true
// extrude axis + a point ON the extruded cap from cached geometry (populated for ALL faces at mesh
// load, not pick-gated), so the handle is anchored to the surface being pulled and points along the
// active extrusion direction — flipping when "Reverse direction" (params.flip) is toggled.
//
// Method: among the feature's owned faces, find the most anti-parallel normal pair (the two extrude
// caps are parallel to the sketch plane, so their normals are ~opposite). That pair's shared axis IS
// the sketch-plane normal — derived from geometry, correct for ANY plane, no hardcoded axis. Returns
// false (caller keeps the old behaviour, no regression) when the caps can't be cleanly identified
// (dress-up feature, degenerate/curved caps, symmetric mid-plane where no single far cap dominates).
static bool extrudeAxisHandle(AppState& st, const std::string& fid, bool flip, double depth,
                              Vec3& axisOut, float anchorOut[3]) {
    const auto& om = st.docMirror.ownerMap();
    auto oit = om.find(fid);
    if (oit == om.end()) return false;
    // Collect cached (normal, centroid) for every owned face.
    struct FC { Vec3 n; Vec3 c; };
    std::vector<FC> faces;
    faces.reserve(oit->second.faces.size());
    for (const auto& bf : oit->second.faces) {
        uint64_t key = ((uint64_t)bf.first << 32) | (uint64_t)bf.second;
        auto it = st.faceNormalCache.find(key);
        if (it == st.faceNormalCache.end()) continue;
        const auto& v = it->second;  // {nx,ny,nz, cx,cy,cz}
        Vec3 n(v[0], v[1], v[2]);
        float nl = std::sqrt(n.x*n.x + n.y*n.y + n.z*n.z);
        if (nl < 1e-6f) continue;
        faces.push_back({Vec3(n.x/nl, n.y/nl, n.z/nl), Vec3(v[3], v[4], v[5])});
    }
    if (faces.size() < 2) return false;
    // Find the extrude caps among anti-parallel owned-face pairs. A rectangular extrude has THREE
    // anti-parallel pairs (caps + the two side-wall pairs), all with dot ~ -1, so "most opposed"
    // alone is ambiguous and would pick an arbitrary owner-map-ordered pair (possibly a side wall).
    // The caps are the unique pair whose centroids are separated by the extrude DEPTH along the
    // shared normal (side walls are separated by the profile width/height instead). So: among pairs
    // that are genuinely opposed, pick the one whose axial separation best matches |depth|. Curved
    // side walls (cylinders) average to a ~null normal and never enter this loop, so a round extrude
    // still resolves to its two flat caps.
    int bi = -1, bj = -1; float bestErr = 1e30f;
    const float dtarget = (float)std::fabs(depth);
    for (size_t i = 0; i < faces.size(); ++i)
        for (size_t j = i + 1; j < faces.size(); ++j) {
            if (dot(faces[i].n, faces[j].n) > -0.85f) continue;   // not an opposed pair
            Vec3 axis = faces[i].n;
            float sep = std::fabs(dot(faces[i].c - faces[j].c, axis));
            float err = (dtarget > 1e-4f) ? std::fabs(sep - dtarget) : -sep;  // no depth -> widest pair
            if (err < bestErr) { bestErr = err; bi = (int)i; bj = (int)j; }
        }
    if (bi < 0) return false;
    // When we DO know the depth, reject the match if no opposed pair sits near it (the caps aren't in
    // the owned set this frame) — better to fall back than to anchor on a mis-identified wall.
    if (dtarget > 1e-4f) {
        Vec3 ax = faces[bi].n;
        float sep = std::fabs(dot(faces[bi].c - faces[bj].c, ax));
        if (std::fabs(sep - dtarget) > 0.25f * dtarget + 0.5f) return false;
    }
    // The extrude axis is the (unsigned) shared normal; use cap bi's outward normal as +axis.
    Vec3 axis = faces[bi].n;
    // Anchor on the far cap (the surface the solid grew TOWARD). growDir is recovered from the
    // LIVE preview solid, which already embeds params.flip in the engine (document.cpp negates
    // dir when flip is true). So farCap is always the active extruded face — do NOT re-apply
    // flip here or the arrow double-negates and freezes on Reverse direction / drag past plane
    // (arrow tip stays on the same side of the sketch while the solid jumps, or both freeze if
    // the mesh is also stale). The flip argument is kept for call-site clarity / future use.
    (void)flip;
    float pi = dot(faces[bi].c, axis), pj = dot(faces[bj].c, axis);
    const FC& farCap = (pi >= pj) ? faces[bi] : faces[bj];
    const FC& nearCap = (pi >= pj) ? faces[bj] : faces[bi];
    // Orient +axis to point OUT of the body (from the near/sketch cap toward the far/extruded
    // cap) so the arrow's primary tip reads as "grow the extrusion" on the side the solid is.
    Vec3 growDir = farCap.c - nearCap.c;
    if (dot(growDir, axis) < 0.0f) axis = Vec3(-axis.x, -axis.y, -axis.z);
    axisOut = axis;
    anchorOut[0] = farCap.c.x; anchorOut[1] = farCap.c.y; anchorOut[2] = farCap.c.z;
    return true;

}

// Build the draggable-dimension list for the selected feature, reusing the PM-form field table to
// learn which params are dims and their labels/units. Skips greyed/conditional-off and expr-bound
// dims, and any dim whose value the engine table drives (omitWhenDisabled while disabled). Returns
// at most ~4 candidates (the dims a user actually drives) so the overlay stays readable.
static std::vector<DimCandidate> dimCandidates(AppState& st, const std::string& fid) {
    std::vector<DimCandidate> out;
    if (fid.empty() || !st.docMirror.hasDoc()) return out;
    const nlohmann::json& tree = st.docMirror.tree();
    const nlohmann::json* feat = findFeature(tree, fid);
    if (!feat) return out;
    std::string type = feat->value("type", std::string());
    const ui::PMFormDef* form = ui::pmFindForm(type);
    if (!form) return out;
    const nlohmann::json& params = feat->contains("params") && (*feat)["params"].is_object()
                                       ? (*feat)["params"] : nlohmann::json::object();

    float anchor[3]; float extent = 1.0f;
    bool hasAnchor = featureAnchor(st, fid, anchor, extent);

    // Resolve a Combo field's currently-selected value (to honour DimIf enable conditions).
    auto comboValueOf = [&](const char* key) -> std::string {
        if (!key) return std::string();
        for (const auto& fd : form->fields) {
            if (fd.kind != ui::PMFieldKind::Combo || std::strcmp(fd.key, key) != 0) continue;
            // The stored param value (engine echoes the chosen enum); fall back to the first option.
            auto it = params.find(key);
            if (it != params.end() && it->is_string()) return it->get<std::string>();
            if (fd.comboOptions) { std::string o(fd.comboOptions);
                                   return o.substr(0, o.find('|')); }
        }
        return std::string();
    };
    auto enabled = [&](const ui::PMFieldDef& fd) -> bool {
        if (fd.comingSoon) return false;
        if (!fd.enableKey)  return true;
        std::string cur = comboValueOf(fd.enableKey);
        if (cur.empty()) return false;
        std::string vals = fd.enableVals ? fd.enableVals : "";
        // pipe-separated membership test
        size_t p = 0;
        while (p <= vals.size()) {
            size_t e = vals.find('|', p);
            std::string tok = vals.substr(p, e == std::string::npos ? std::string::npos : e - p);
            if (tok == cur) return true;
            if (e == std::string::npos) break;
            p = e + 1;
        }
        return false;
    };

    int idx = 0;
    for (const auto& fd : form->fields) {
        if (fd.kind != ui::PMFieldKind::Dim) continue;   // only Dim/Angle rows (Int/Combo skipped)
        if (!enabled(fd)) continue;
        double val = 0.0; bool isExpr = false;
        if (!dimParamValue(params, fd.key, val, isExpr)) continue;  // absent or expr-driven
        bool isAngle = fd.unitLabel && std::strstr(fd.unitLabel, "deg");
        DimCandidate c;
        c.param = fd.key; c.label = fd.label; c.angle = isAngle; c.value = val;
        // A dim whose form default is 0 (draft angle, sweep twist, extrude offsets) is one where 0
        // is a legitimate value — so dragging it must be able to reach 0 rather than clamping to the
        // size floor (0.1mm / 1deg) that protects genuine size dims (depth/radius/revolve angle).
        c.allowZero = (fd.dimDefault == 0.0);
        // Carry the form's range metadata: a bounded field's drag clamps to [dimMin, dimMax]
        // (replacing the allowZero heuristic); unbounded signed fields lower the floor to dimMin.
        c.rangeMin = fd.dimMin;
        c.rangeMax = fd.dimMax;
        c.dirW = dimWorldDir(fd.key);
        // Dress-up dimensions belong on the affected surface, not at the whole body's centre
        // plus an arbitrary fraction of its size (which placed the fillet radius on a shell's
        // floor). Use a real face created by the feature, with no bbox-based displacement.
        bool surfaceAnchored = false;
        if (type == "fillet" || type == "chamfer") {
            const auto owner = st.docMirror.ownerMap().find(fid);
            if (owner != st.docMirror.ownerMap().end()) {
                for (const auto& bf : owner->second.faces) {
                    const uint64_t key = ((uint64_t)bf.first << 32) | bf.second;
                    const auto face = st.faceNormalCache.find(key);
                    if (face == st.faceNormalCache.end()) continue;
                    const auto& geometry = face->second;
                    for (int k = 0; k < 3; ++k) c.anchor[k] = geometry[k + 3];
                    surfaceAnchored = true;
                    break;
                }
            }
        }
        // TASK A: the primary extrude/planar depth handle is constrained to the extruded surface and
        // oriented along the real extrusion axis (recovered from cached cap geometry), honouring the
        // "Reverse direction" flag. This replaces the hardcoded dimWorldDir()+AABB-centre placement
        // that floated inside the solid and pointed along world +Z regardless of the sketch plane.
        bool extrudeAnchored = false;
        if (!isAngle && (c.param == "depth")) {
            // Prefer the live PM flip checkbox over the tree's last-committed params — during an
            // in-flight preview the tree can lag one apply behind the checkbox the user just
            // toggled (or the drag just auto-flipped), which re-anchors the arrow on the wrong
            // side for a frame and makes Reverse look dead.
            bool flip = params.value("flip", false);
            if (st.propertyManager && st.propertyManager->active() &&
                st.propertyManager->targetId() == fid) {
                bool pmFlip = false;
                if (st.propertyManager->dragGetCheck("flip", pmFlip)) flip = pmFlip;
            }
            Vec3 axis; float capAnchor[3];
            if (extrudeAxisHandle(st, fid, flip, val, axis, capAnchor)) {
                c.dirW = axis;
                c.anchor[0] = capAnchor[0];
                c.anchor[1] = capAnchor[1];
                c.anchor[2] = capAnchor[2];
                extrudeAnchored = true;
            }
        }
        if (!extrudeAnchored && !surfaceAnchored && hasAnchor) {
            // Spread the handles around the anchor so chips/arrows don't stack: nudge each one a
            // little further out along its own measured axis (or +Z for angles).
            Vec3 d = isAngle ? Vec3(0, 0, 1) : c.dirW;
            float off = extent * 0.6f + (float)idx * extent * 0.15f;
            c.anchor[0] = anchor[0] + d.x * off;
            c.anchor[1] = anchor[1] + d.y * off;
            c.anchor[2] = anchor[2] + d.z * off;
        }
        out.push_back(std::move(c));
        if (++idx >= 4) break;   // keep the overlay readable
    }
    return out;
}

// ---- C26-C / C37-C: Normal-To (Ctrl+8 / shortcut) -------------------------------------------
// "Look straight at the selected face." If a face is selected and the C26-C normal cache has data
// for it, animate the camera so the view direction equals the face's outward normal. Second press
// while already looking at that face flips to the anti-view (SolidWorks behavior). Pan resets to
// the face centroid (so the face centers on screen). Zoom is kept.
//
// C37-C additions:
//   - While in sketch: Ctrl+8 orients to the sketch plane (uses cameraLock yaw/pitch), second press
//     flips 180° (anti-plane view). Cooperates with cameraLock (sets same yaw/pitch so the lock
//     re-asserts them without fighting).
//   - Cylindrical/conical faces: reads axis from selector JSON if available, falls back to face
//     normal cache. Looking DOWN the axis (axis treated as face normal for yaw/pitch derivation).
//   - Adds to context menu and HUD view-orientation dropdown (done in the drawing section below).
//
// Yaw/pitch derivation (Z-up, target-to-eye convention):
//   dir = (cos(pitch)*sin(yaw), -cos(pitch)*cos(yaw), sin(pitch))
//   Given face normal N = (nx, ny, nz):
//     pitch = asin(nz)        (clamped ±89.5°)
//     yaw   = atan2(nx, -ny)
//
// Nearest-axis fallback: when no face is selected and not in sketch, snaps to nearest axis-aligned.

// Persistent state for second-press flip detection (file-static, one viewport):
static uint64_t s_normalToKey   = 0;    // last cached key (body<<32|face_id, or UINT64_MAX for sketch)
static float    s_normalToYaw   = 0;    // the yaw we animated to
static float    s_normalToPitch = 0;    // the pitch we animated to

// C37-C: internal helper — convert a unit direction vector to yaw/pitch degrees.
static void dirToYawPitch(float nx, float ny, float nz, float& yawDeg, float& pitchDeg) {
    const float kPiOver180 = 3.14159265f / 180.0f;
    float nzC = nz > 1.0f ? 1.0f : (nz < -1.0f ? -1.0f : nz);
    float pitch = std::asin(nzC);
    float yaw   = std::atan2(nx, -ny);
    yawDeg   = yaw   / kPiOver180;
    pitchDeg = pitch / kPiOver180;
}

static uint64_t simulationDocumentScope(const AppState& st) {
    uint64_t scope = 1469598103934665603ull;
    if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
        scope ^= (uint64_t)(st.activeTab + 1);
        scope *= 1099511628211ull;
        for (unsigned char c : st.docTabs[st.activeTab].docId) {
            scope ^= c;
            scope *= 1099511628211ull;
        }
    }
    return scope;
}

// Bounds share the producers' immutable batch revisions, just like their retained GL buffers.
// Unchanged frames merge small AABBs, never walk the triangle/line payloads again.
// Returns false when the document scope changed this frame: the retained framing radius belonged
// to the previous document, so the caller must not rescale the restored camera by it.
static bool updateSimulationSceneBounds(AppState& st, const simscene::Scene& scene,
                                        uint64_t documentScope, bool toolArmed) {
    Renderer& renderer = *st.renderer;
    struct BatchBounds {
        uint64_t key = 0, revision = 0;
        float lo[3]{}, hi[3]{};
        bool lines = false, valid = false, seen = false;
    };
    static std::vector<BatchBounds> batches;
    static uint64_t previousScope = 0;
    static const Renderer* previousRenderer = nullptr;
    // Padded framing radius held for auxiliary (simulation) geometry in THIS scope; 0 = none.
    // It only ever exceeds the document's own model/empty radius: a large previous document or a
    // model-radius change must never pin a small or new document's framing (B1 speck regression).
    static float retainedRadius = 0.0f;
    const bool sameScope = previousScope == documentScope && previousRenderer == &renderer;
    if (!sameScope) {
        batches.clear();
        retainedRadius = 0.0f;
        previousScope = documentScope;
        previousRenderer = &renderer;
    }
    for (auto& batch : batches) batch.seen = false;
    float lo[3]{}, hi[3]{};
    bool haveBounds = false;
    auto include = [&](uint64_t key, uint64_t revision, const std::vector<float>* xyz,
                       bool lines) {
        BatchBounds* bounds = nullptr;
        for (auto& batch : batches) {
            if (batch.key == key && batch.lines == lines) {
                bounds = &batch;
                break;
            }
        }
        const bool added = bounds == nullptr;
        if (added) {
            batches.emplace_back();
            bounds = &batches.back();
            bounds->key = key;
            bounds->lines = lines;
        }
        bounds->seen = true;
        if (added || bounds->revision != revision) {
            bounds->revision = revision;
            bounds->valid = xyz && xyz->size() >= 3;
            if (bounds->valid) {
                for (int k = 0; k < 3; ++k) bounds->lo[k] = bounds->hi[k] = (*xyz)[k];
                for (size_t i = 3; i + 2 < xyz->size(); i += 3) {
                    for (int k = 0; k < 3; ++k) {
                        bounds->lo[k] = std::min(bounds->lo[k], (*xyz)[i + k]);
                        bounds->hi[k] = std::max(bounds->hi[k], (*xyz)[i + k]);
                    }
                }
            }
        }
        if (!bounds->valid) return;
        for (int k = 0; k < 3; ++k) {
            lo[k] = haveBounds ? std::min(lo[k], bounds->lo[k]) : bounds->lo[k];
            hi[k] = haveBounds ? std::max(hi[k], bounds->hi[k]) : bounds->hi[k];
        }
        haveBounds = true;
    };
    for (const auto& batch : scene.lit)
        if (batch.bounds) include(batch.key, batch.revision, batch.xyz, false);
    for (const auto& batch : scene.lines)
        if (batch.bounds) include(batch.key, batch.revision, batch.xyz, true);
    batches.erase(std::remove_if(batches.begin(), batches.end(),
                                 [](const BatchBounds& batch) { return !batch.seen; }),
                  batches.end());
    if (haveBounds) renderer.setAuxSceneBounds(lo, hi);
    else            renderer.clearAuxSceneBounds();
    // Read the exact cached bounds radius without the retained authoring floor.
    renderer.setAuxSceneRadiusFloor(0.0f);
    const float requiredRadius = renderer.effectiveSceneRadius();
    const float baseRadius = renderer.modelRadius() > 0.0f
        ? renderer.modelRadius() : Renderer::kEmptySceneRadius;
    float framingRadius = std::max(requiredRadius, retainedRadius);
    if (requiredRadius > baseRadius && requiredRadius > retainedRadius) {
        // Auxiliary geometry outgrew the held frame. Geometric 25% bands leave headroom for small
        // edits without repeatedly resizing the datums; bands are computed only at a threshold.
        const float band = std::ceil(std::log(requiredRadius / baseRadius) / std::log(1.25f));
        framingRadius = std::max(requiredRadius, baseRadius * std::pow(1.25f, band));
    } else if (retainedRadius > requiredRadius && !toolArmed &&
               requiredRadius < retainedRadius * 0.7f) {
        float nextRadius = requiredRadius;
        if (requiredRadius > baseRadius) {
            const float band = std::ceil(std::log(requiredRadius / baseRadius) / std::log(1.25f));
            nextRadius = std::max(requiredRadius, baseRadius * std::pow(1.25f, band));
        }
        float minimumZoom = st.zoom;
        if (st.animActive)
            minimumZoom = std::min(minimumZoom, std::min(st.animZoom0, st.animZoom1));
        // Clamping after radius compensation would visibly jump at the 0.1 zoom limit.
        // Retain the old radius until both the live view and animation can shrink exactly.
        if (minimumZoom * (nextRadius / retainedRadius) >= 0.1f) framingRadius = nextRadius;
    }
    retainedRadius = framingRadius > baseRadius ? framingRadius : 0.0f;
    renderer.setAuxSceneRadiusFloor(retainedRadius);
    return sameScope;
}

// C37-C: shared AABB-framing helper — pan to bbox center, zoom to fit bbox radius (animated).
// Used by Zoom to Selection (Shift+F), st.zoomToBBoxRequested, and can be called directly.
// bboxMin/Max are world-space. Uses the same math as ZoomFit (zoom=1 frames scene radius r;
// for a sub-box we set zoom = sceneRadius / bboxRadius so the sub-box fills the view).
static void frameBBox(AppState& st, const float bboxMin[3], const float bboxMax[3]) {
    // Bbox center becomes the pan target.
    float cx = (bboxMin[0] + bboxMax[0]) * 0.5f;
    float cy = (bboxMin[1] + bboxMax[1]) * 0.5f;
    float cz = (bboxMin[2] + bboxMax[2]) * 0.5f;
    // Bbox radius (half-diagonal).
    float dx = bboxMax[0] - bboxMin[0];
    float dy = bboxMax[1] - bboxMin[1];
    float dz = bboxMax[2] - bboxMin[2];
    float bboxR = 0.5f * std::sqrt(dx * dx + dy * dy + dz * dz);
    // Use the same auxiliary-aware framing sphere and zoom limits as orbit navigation.
    const float sceneR = st.renderer ? st.renderer->effectiveSceneRadius()
                                    : Renderer::kEmptySceneRadius;
    const float maxZoom = st.renderer ? st.renderer->maxSceneZoom() : 10.0f;
    // Degenerate (vertex/edge selection): zoom in close.
    const float targetZoom = std::clamp(bboxR < 1e-5f ? 4.0f : sceneR / bboxR,
                                        0.1f, maxZoom);
    Vec3 mc = st.renderer ? st.renderer->modelCenter() : Vec3{0, 0, 0};
    float pan[3] = {cx - mc.x, cy - mc.y, cz - mc.z};
    requestCameraAnimation(st, st.yaw, st.pitch, targetZoom, pan);
}

// C37-C: Zoom to Selection — union AABB over selection items, then frame it.
// Empty selection: falls back to ZoomFit (same as applyViewPreset ZoomFit).
static void zoomToSelection(AppState& st) {
    if (st.selection.empty()) {
        applyViewPreset(st, ViewPreset::ZoomFit);
        return;
    }
    float bmin[3] = { 1e30f,  1e30f,  1e30f};
    float bmax[3] = {-1e30f, -1e30f, -1e30f};
    bool gotAny = false;
    const float kEdgePad = 3.0f;  // mm padding for edge/vertex degenerate extent
    for (const auto& item : st.selection.items()) {
        const auto& h = item.hit;
        if (!h.valid()) continue;
        if (h.kind == render::PickKind::Face) {
            uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;
            auto it = st.faceBBoxCache.find(key);
            if (it != st.faceBBoxCache.end()) {
                const auto& b = it->second;
                for (int i = 0; i < 3; ++i) {
                    if (b[i]   < bmin[i]) bmin[i] = b[i];
                    if (b[i+3] > bmax[i]) bmax[i] = b[i+3];
                }
                gotAny = true;
            }
        } else if (h.kind == render::PickKind::Edge) {
            uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;
            auto it = st.edgeCentroidCache.find(key);
            if (it != st.edgeCentroidCache.end()) {
                const auto& c = it->second;
                for (int i = 0; i < 3; ++i) {
                    if (c[i] - kEdgePad < bmin[i]) bmin[i] = c[i] - kEdgePad;
                    if (c[i] + kEdgePad > bmax[i]) bmax[i] = c[i] + kEdgePad;
                }
                gotAny = true;
            }
        } else if (h.kind == render::PickKind::Vertex) {
            uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;
            auto it = st.edgeCentroidCache.find(key);  // vertex centroid falls in edgeCentroidCache
            if (it != st.edgeCentroidCache.end()) {
                const auto& c = it->second;
                for (int i = 0; i < 3; ++i) {
                    if (c[i] - kEdgePad < bmin[i]) bmin[i] = c[i] - kEdgePad;
                    if (c[i] + kEdgePad > bmax[i]) bmax[i] = c[i] + kEdgePad;
                }
                gotAny = true;
            } else {
                // Fall back to body bbox if no per-vertex data.
                auto bit = st.bodyBBoxCache.find(h.body_id);
                if (bit != st.bodyBBoxCache.end()) {
                    const auto& b = bit->second;
                    for (int i = 0; i < 3; ++i) {
                        if (b[i]   < bmin[i]) bmin[i] = b[i];
                        if (b[i+3] > bmax[i]) bmax[i] = b[i+3];
                    }
                    gotAny = true;
                }
            }
        }
    }
    if (!gotAny) {
        applyViewPreset(st, ViewPreset::ZoomFit);
        return;
    }
    frameBBox(st, bmin, bmax);
}

static void normalToSelectionOrNearestAxis(AppState& st, bool inSketch) {
    const float kPiOver180 = 3.14159265f / 180.0f;

    // C37-C: while in sketch, orient to the sketch plane via cameraLock; second press flips 180°.
    if (inSketch) {
        auto* sketch = st.sketchMode;
        if (sketch && sketch->active()) {
            float ly = 0, lp = 0;
            if (sketch->cameraLock(ly, lp)) {
                const uint64_t kSketchKey = 0xFFFFFFFFFFFFFFFFull;
                bool alreadyLocked = (s_normalToKey == kSketchKey);
                float targetYaw = ly, targetPitch = lp;
                if (alreadyLocked) {
                    // Flip: rotate 180° about the plane normal (add 180° yaw, negate pitch).
                    targetYaw = ly + 180.0f;
                    targetPitch = -lp;
                    s_normalToKey = 0;
                } else {
                    s_normalToKey = kSketchKey;
                }
                s_normalToYaw   = targetYaw;
                s_normalToPitch = targetPitch;
                float pan[3] = {st.pan[0], st.pan[1], st.pan[2]};
                requestCameraAnimation(st, targetYaw, targetPitch, -1.0f, pan);
            }
        }
        return;
    }

    // ---- try true face normal path ----
    if (!st.selection.empty()) {
        const core::SelItem& selItem = st.selection.items().back();
        const render::PickHit& h = selItem.hit;
        if (h.valid() && h.kind == render::PickKind::Face) {
            uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;

            // C37-C: try to read axis from the resolved selector JSON (cylinder/cone support).
            // The selector is in selItem.selector (filled by C16 resolve round-trip).
            float axNx = 0, axNy = 0, axNz = 0;
            float faceCx = 0, faceCy = 0, faceCz = 0;
            bool hasAxis = false;

            if (!selItem.selector.empty()) {
                try {
                    auto sj = nlohmann::json::parse(selItem.selector);
                    // Selector format per contracts/selectors.md: for cylinder/cone the
                    // selector carries "axis": [x,y,z] (unit vector along the axis).
                    if (sj.contains("axis") && sj["axis"].is_array() && sj["axis"].size() == 3) {
                        axNx = sj["axis"][0].get<float>();
                        axNy = sj["axis"][1].get<float>();
                        axNz = sj["axis"][2].get<float>();
                        // Normalize (should already be, but guard against float drift).
                        float len = std::sqrt(axNx*axNx + axNy*axNy + axNz*axNz);
                        if (len > 1e-5f) { axNx /= len; axNy /= len; axNz /= len; hasAxis = true; }
                    }
                } catch (...) {}
            }

            // Fall back to faceNormalCache for centroid and (if no axis) normal.
            auto it = st.faceNormalCache.find(key);
            if (it != st.faceNormalCache.end()) {
                const auto& v = it->second;
                faceCx = v[3]; faceCy = v[4]; faceCz = v[5];
                if (!hasAxis) {
                    axNx = v[0]; axNy = v[1]; axNz = v[2];
                    hasAxis = true;
                }
            }

            if (hasAxis) {
                // Second-press flip: if we already animated to this face, flip to anti-view.
                bool alreadyNormal = (key == s_normalToKey);
                float targetNx = axNx, targetNy = axNy, targetNz = axNz;
                if (alreadyNormal) {
                    targetNx = -axNx; targetNy = -axNy; targetNz = -axNz;
                    s_normalToKey = 0;  // next press goes forward again
                } else {
                    s_normalToKey = key;
                }

                float yawDeg, pitchDeg;
                dirToYawPitch(targetNx, targetNy, targetNz, yawDeg, pitchDeg);
                s_normalToYaw   = yawDeg;
                s_normalToPitch = pitchDeg;

                // Pan to centroid so the face is centered on screen.
                Vec3 mc = st.renderer ? st.renderer->modelCenter() : Vec3{0, 0, 0};
                float pan[3] = {faceCx - mc.x, faceCy - mc.y, faceCz - mc.z};
                requestCameraAnimation(st, yawDeg, pitchDeg, -1.0f, pan);
                return;
            }
        }
    }
    s_normalToKey = 0;  // no face selected: reset flip state

    // ---- nearest-axis fallback ----
    // dir = view direction from target toward eye (target-to-eye convention):
    //   dir = (cos p sin y, -cos p cos y, sin p)
    float y = st.yaw * kPiOver180, p = st.pitch * kPiOver180;
    float dx = std::cos(p) * std::sin(y);
    float dy2 = -std::cos(p) * std::cos(y);
    float dz = std::sin(p);
    struct AxView { float x, y, z; ViewPreset preset; };
    static const AxView kAx[6] = {
        { 0, -1,  0, ViewPreset::Front  },
        { 0,  1,  0, ViewPreset::Back   },
        { 1,  0,  0, ViewPreset::Right  },
        {-1,  0,  0, ViewPreset::Left   },
        { 0,  0,  1, ViewPreset::Top    },
        { 0,  0, -1, ViewPreset::Bottom },
    };
    int best = 0; float bestDot = -2.0f;
    for (int i = 0; i < 6; ++i) {
        float d = dx * kAx[i].x + dy2 * kAx[i].y + dz * kAx[i].z;
        if (d > bestDot) { bestDot = d; best = i; }
    }
    applyViewPreset(st, kAx[best].preset);
}

// W1-B (item 9): the appearance picker + MaterialPreset table moved to ui/appearance_common.{h,cpp}
// (formerly duplicated verbatim here and in feature_tree.cpp). The call site below uses
// ui::appearanceColorPicker().

// ---- C26: apply a set_appearance op straight from the viewport, then reload the mesh. ----
// A DIRECT apply (not the feature tree's deferred queue) is SAFE here: the viewport context menu is
// NOT drawn while iterating the DocMirror tree json (the feature tree's use-after-free hazard) — it
// runs inside the Viewport window after rendering, touching no tree-iteration state. This mirrors
// feature_tree's applyTreeOpsNow: apply -> loadDocMesh(resp.aicm_path) repaints the body with its
// new color on the next frame.
static bool refuseViewportEdit(AppState& st, const std::string& docId) {
    if (!st.docLockedByAgent(docId)) return false;
    st.toast(ui::ToastLevel::Warn, st.agentLockReason(docId));
    return true;
}

static void toastViewportEditKept(AppState& st, const std::string& docId) {
    std::string part = docId;
    for (const auto& tab : st.docTabs)
        if (tab.docId == docId && !tab.title.empty()) { part = tab.title; break; }
    st.toast(ui::ToastLevel::Warn,
             "Agent started editing " + part + " — your edit was kept and closed.");
}

static bool viewportEditMenuItem(const char* label, const std::string& disabledReason) {
    const bool clicked = ImGui::MenuItem(label, nullptr, false, disabledReason.empty());
    if (!disabledReason.empty() && ImGui::IsItemHovered(ImGuiHoveredFlags_AllowWhenDisabled))
        ImGui::SetTooltip("%s", disabledReason.c_str());
    return clicked;
}

static void applyAppearanceFromViewport(AppState& st, const std::string& docId,
                                        const std::string& bodyKey,
                                        const float* color /* nullptr = clear */,
                                        float metallic = 0.0f, float roughness = 0.5f) {
    if (refuseViewportEdit(st, docId)) return;
    if (!st.docMirror.hasDoc() || !st.workerStarted || st.docMirror.docId() != docId) return;
    nlohmann::json op = {{"op", "set_appearance"}, {"id", bodyKey}};
    if (color) {
        op["color"] = {color[0], color[1], color[2]};
        op["metallic"] = metallic;   // C32-A RealView material
        op["roughness"] = roughness;
    } else {
        op["color"] = nullptr;
    }
    auto resp = st.docMirror.applyOps(st.worker, nlohmann::json::array({op}));
    if (resp.ok && !resp.aicm_path.empty()) {
        std::string lerr;
        st.loadDocMesh(resp.aicm_path, lerr);
        if (!lerr.empty()) st.addLog("[err] loadDocMesh: " + lerr);
    } else if (!resp.ok) {
        st.addLog("[err] set_appearance: " + resp.error_message);
    }
}

// ---- Instant3D push-pull helpers -----------------------------------------------------------

// Project a world point through the renderer's published MVP to viewport-screen pixels.
// Returns false when the point is behind the camera. (Same column-major math the plane-label and
// PM-callout overlays use.)
static bool ppProject(const Renderer& renderer, const ImVec2& vr0, const ImVec2& vr1,
                      int vw, int vh, const float w[3], ImVec2& out) {
    const float* m = renderer.camera().mvp.m;  // column-major model->clip
    float x = m[0]*w[0] + m[4]*w[1] + m[8]*w[2]  + m[12];
    float y = m[1]*w[0] + m[5]*w[1] + m[9]*w[2]  + m[13];
    float wc= m[3]*w[0] + m[7]*w[1] + m[11]*w[2] + m[15];
    if (wc <= 1e-4f) return false;  // behind the camera
    out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vw);
    out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vh);
    (void)vr1;
    return true;
}

// Screen-space hit-test for reference datum axes (model mode). Matches Renderer::drawRefDatums
// half-length via refAxisHalfLength (default axes: sceneRadius*1.25; plane-derived auto axes:
// that * 0.55 so they only cover the user-plane quad) and prefers the closest line within a
// beefed pick band (~10 px * uiScale) so hairline axes stay clickable even when a translucent
// plane sits in front. Does not re-solve plane pairs — consumes the same refDatums the renderer
// draws.
static bool hitTestRefAxis(const Renderer& renderer, AppState& st, ImVec2 mouse,
                           const ImVec2& vr0, const ImVec2& vr1, int vw, int vh,
                           ImVec2& outA, ImVec2& outB, std::string& outFid, std::string& outName) {
    (void)vr1;
    auto distToSeg = [](ImVec2 p, ImVec2 a, ImVec2 b) -> float {
        float vx = b.x - a.x, vy = b.y - a.y, wx = p.x - a.x, wy = p.y - a.y;
        float len2 = vx * vx + vy * vy;
        float t = len2 > 1e-6f ? (wx * vx + wy * vy) / len2 : 0.0f;
        t = t < 0.0f ? 0.0f : (t > 1.0f ? 1.0f : t);
        float dx = a.x + t * vx - p.x, dy = a.y + t * vy - p.y;
        return std::sqrt(dx * dx + dy * dy);
    };

    struct Cand { std::string id, name; float ox, oy, oz, dx, dy, dz; };
    std::vector<Cand> cands;
    std::unordered_set<std::string> seen;
    auto add = [&](const std::string& id, const std::string& name,
                   float ox, float oy, float oz, float dx, float dy, float dz) {
        if (id.empty() || seen.count(id) || st.hiddenFeatures.count(id)) return;
        const float len = std::sqrt(dx * dx + dy * dy + dz * dz);
        if (len < 1e-9f) return;
        dx /= len; dy /= len; dz /= len;
        cands.push_back({id, name.empty() ? id : name, ox, oy, oz, dx, dy, dz});
        seen.insert(id);
    };

    bool any = false;
    for (const auto& d : renderer.refDatums()) {
        if (d.kind != Renderer::RefDatum::Kind::Axis) continue;
        any = true;
        add(d.id, d.name, d.origin.x, d.origin.y, d.origin.z, d.dir.x, d.dir.y, d.dir.z);
    }
    // Pre-inject fallback: predefined X/Y/Z always listed in the FM tree.
    if (!any) {
        add("axis.x", "X Axis", 0, 0, 0, 1, 0, 0);
        add("axis.y", "Y Axis", 0, 0, 0, 0, 1, 0);
        add("axis.z", "Z Axis", 0, 0, 0, 0, 0, 1);
    }

    // Beefed pick band: ~10 px * uiScale (hairline axes over translucent plane fills).
    // Line wins inside this band before plane-fill hit-testing consumes the click.
    float best = 10.0f * ui::uiScale;
    bool found = false;
    for (const auto& c : cands) {
        // Per-axis half-length: default axes use e; axis.auto:* use the user-plane ps.
        const float half = renderer.refAxisHalfLength(c.id);
        float aW[3] = {c.ox - c.dx * half, c.oy - c.dy * half, c.oz - c.dz * half};
        float bW[3] = {c.ox + c.dx * half, c.oy + c.dy * half, c.oz + c.dz * half};
        ImVec2 sa, sb;
        if (!ppProject(renderer, vr0, vr1, vw, vh, aW, sa)) continue;
        if (!ppProject(renderer, vr0, vr1, vw, vh, bW, sb)) continue;
        float dist = distToSeg(mouse, sa, sb);
        if (dist < best) {
            best = dist; found = true; outA = sa; outB = sb;
            outFid = c.id; outName = c.name;
        }
    }
    return found;
}

// Stroke a dashed screen-space segment a..b (dash/gap in px). A degenerate segment (a curved edge
// we can't reconstruct) draws a small ring at `a` instead so it's still a visible deselect target.
static void ppDashedLine(ImDrawList* dl, const ImVec2& a, const ImVec2& b, ImU32 col, float thick,
                         float dash, float gap) {
    float dx = b.x - a.x, dy = b.y - a.y;
    float len = std::sqrt(dx * dx + dy * dy);
    if (len < 1e-3f) { dl->AddCircle(a, 4.0f * ui::uiScale, col, 0, thick); return; }
    float ux = dx / len, uy = dy / len;
    for (float t = 0.0f; t < len; t += dash + gap) {
        float e = std::min(t + dash, len);
        dl->AddLine(ImVec2(a.x + ux * t, a.y + uy * t),
                    ImVec2(a.x + ux * e, a.y + uy * e), col, thick);
    }
}

// One shared Instant3D manipulator arrow (push-pull face handle + dimension handles — the two
// previously-diverging arrowHead lambdas; contrast audit P1). A bidirectional accent arrow with:
// a soft accentSoft glow while hovered/dragged, a thin dark canvas-ink rim under shaft + heads so
// the handle stays legible over faces of any color, solid AA heads, and a hub disc + soft ring.
// (dx,dy) is the unit screen direction toward tipP; headLen/headW/shaftW are logical px
// (pre-uiScale) so each call site keeps its authored sizes.
static void drawManipArrow(ImDrawList* dl, const ImVec2& c, const ImVec2& tipP, const ImVec2& tipN,
                           float dx, float dy, float headLen, float headW, float shaftW,
                           bool hot, bool dragging) {
    const auto& pal = ui::palette;
    const float sc = ui::uiScale;
    const ImU32 acc  = ui::Palette::toU32(pal.accent);
    const ImU32 accS = ui::Palette::toU32(pal.accentSoft);
    float rimF[4]; ui::Palette::withAlpha(pal.viewportEdge, rimF, 0.80f);
    const ImU32 rim = ui::Palette::toU32(rimF);
    const float lw = (dragging ? 3.0f : (hot ? shaftW + 0.6f : shaftW)) * sc;
    const float hl = headLen * sc, hw = headW * sc;
    // Arrowhead corners for one tip; `grow` inflates the triangle for the rim under-pass.
    auto headPts = [&](const ImVec2& tip, float hdx, float hdy, float grow,
                       ImVec2& tp, ImVec2& l, ImVec2& r) {
        tp = ImVec2(tip.x + hdx * grow, tip.y + hdy * grow);
        ImVec2 base(tip.x - hdx * hl, tip.y - hdy * hl);
        l = ImVec2(base.x + hdy * (hw + grow), base.y - hdx * (hw + grow));
        r = ImVec2(base.x - hdy * (hw + grow), base.y + hdx * (hw + grow));
    };
    // Hover/drag glow: wide accentSoft under-stroke (soft halo that reads on light AND dark faces).
    if (hot || dragging) {
        dl->AddLine(c, tipP, accS, lw + 5.0f * sc);
        dl->AddLine(c, tipN, accS, lw + 5.0f * sc);
    }
    // Contrast rim, then accent strokes on top.
    dl->AddLine(c, tipP, rim, lw + 2.0f * sc);
    dl->AddLine(c, tipN, rim, lw + 2.0f * sc);
    dl->AddLine(c, tipP, acc, lw);
    dl->AddLine(c, tipN, acc, lw);
    for (int e = 0; e < 2; ++e) {
        const ImVec2& tip = e ? tipN : tipP;
        float hdx = e ? -dx : dx, hdy = e ? -dy : dy;
        ImVec2 tp, l, r;
        headPts(tip, hdx, hdy, 1.2f * sc, tp, l, r);
        dl->AddTriangleFilled(tp, l, r, rim);   // inflated rim head underneath
        headPts(tip, hdx, hdy, 0.0f, tp, l, r);
        dl->AddTriangleFilled(tp, l, r, acc);   // solid AA accent head
    }
    // Hub: rim disc under an accent disc + soft ring.
    const float hubR = (dragging ? 4.0f : (hot ? 3.5f : 3.0f)) * sc;
    dl->AddCircleFilled(c, hubR + 1.0f * sc, rim);
    dl->AddCircleFilled(c, hubR, acc);
    dl->AddCircle(c, (dragging ? 7.0f : 5.0f) * sc, accS, 0, 1.5f * sc);
}

// SolidWorks-style fillet/chamfer edit overlay: the live body shows the ROUND, while each selected
// (original) edge is drawn as a dashed 3D line at its old location so the user can see and click to
// DESELECT edges the round has eaten away. Returns true while the cursor is over a dashed edge (the
// caller suppresses the model pick); a left click over one removes that edge from the active well.
static bool drawBlendEdgeOverlay(AppState& st, const Renderer& renderer, ImGuiIO& io,
                                 const ImVec2& vr0, const ImVec2& vr1, int vw, int vh,
                                 bool imgHovered) {
    if (!st.propertyManager || !st.propertyManager->active()) return false;
    std::vector<PropertyManager::EdgeSeg> segs;
    st.propertyManager->collectActiveWellEdgeSegments(segs);
    if (segs.empty()) return false;

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float S = ui::uiScale;
    // Palette roles (contrast audit P2): statusWarn is the semantic "original-edge ghost" amber in
    // both themes; the hovered deselect target switches to accent so the hover state gains chroma
    // contrast instead of a paler wash that vanished over light faces.
    float selF[4]; ui::Palette::withAlpha(ui::palette.statusWarn, selF, 0.90f);
    const ImU32 colSel = ui::Palette::toU32(selF);
    const ImU32 colHot = ui::Palette::toU32(ui::palette.accent);
    const float tol = 8.0f * S;

    // Project each edge polyline to screen. A point fails to project when it's behind the camera; we
    // keep per-point validity so partly-clipped loops still draw their visible spans.
    struct Proj { std::vector<ImVec2> pts; std::vector<char> ok; bool closed; int pick; };
    std::vector<Proj> projs; projs.reserve(segs.size());
    for (const auto& sg : segs) {
        Proj pr; pr.closed = sg.closed; pr.pick = sg.pickIndex;
        pr.pts.reserve(sg.pts.size()); pr.ok.reserve(sg.pts.size());
        for (const auto& w : sg.pts) {
            ImVec2 sp; bool ok = ppProject(renderer, vr0, vr1, vw, vh, w.data(), sp);
            pr.pts.push_back(sp); pr.ok.push_back(ok ? 1 : 0);
        }
        projs.push_back(std::move(pr));
    }

    // Distance from the cursor to a polyline (only segments whose BOTH ends projected).
    auto distToProj = [&](const Proj& pr, ImVec2 mp) -> float {
        float best = 1e30f;
        int n = (int)pr.pts.size();
        if (n == 1) { float dx = mp.x - pr.pts[0].x, dy = mp.y - pr.pts[0].y;
                      return pr.ok[0] ? std::sqrt(dx * dx + dy * dy) : 1e30f; }
        int last = pr.closed ? n : n - 1;
        for (int s = 0; s < last; ++s) {
            int j = (s + 1) % n;
            if (!pr.ok[s] || !pr.ok[j]) continue;
            ImVec2 a = pr.pts[s], b = pr.pts[j];
            ImVec2 ab(b.x - a.x, b.y - a.y), ap(mp.x - a.x, mp.y - a.y);
            float ab2 = ab.x * ab.x + ab.y * ab.y;
            float t = ab2 > 1e-6f ? (ap.x * ab.x + ap.y * ab.y) / ab2 : 0.0f;
            t = t < 0.0f ? 0.0f : (t > 1.0f ? 1.0f : t);
            float cx = a.x + ab.x * t, cy = a.y + ab.y * t;
            float dx = mp.x - cx, dy = mp.y - cy, d2 = dx * dx + dy * dy;
            if (d2 < best) best = d2;
        }
        return std::sqrt(best);
    };

    int hotIdx = -1; float hotD = tol;
    if (imgHovered) {
        for (const auto& pr : projs) {
            float d = distToProj(pr, io.MousePos);
            if (d < hotD) { hotD = d; hotIdx = pr.pick; }
        }
    }

    for (const auto& pr : projs) {
        bool hot = (pr.pick == hotIdx);
        ImU32 col = hot ? colHot : colSel;
        float thick = (hot ? 2.6f : 1.7f) * S;
        int n = (int)pr.pts.size();
        if (n == 1) { if (pr.ok[0]) dl->AddCircle(pr.pts[0], 4.0f * S, col, 12, thick); continue; }
        int last = pr.closed ? n : n - 1;
        for (int s = 0; s < last; ++s) {
            int j = (s + 1) % n;
            if (!pr.ok[s] || !pr.ok[j]) continue;
            ppDashedLine(dl, pr.pts[s], pr.pts[j], col, thick, 7.0f * S, 5.0f * S);
        }
    }

    if (hotIdx >= 0 && imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        st.propertyManager->removeActiveWellPick(hotIdx);
        return true;  // swallow the click (deselect, not a new pick)
    }
    return hotIdx >= 0;
}

// The push-pull handle for the single selected face: returns false when there is no single face
// selected or the face has no cached normal/centroid. On success fills the centroid screen pos,
// the two arrow tips (+normal and −normal) in screen space, the unit screen-space normal
// direction, and the world-per-pixel scale at the face depth.
static bool ppHandleGeometry(AppState& st, const Renderer& renderer, const ImVec2& vr0,
                             const ImVec2& vr1, int vw, int vh, ImVec2& centroidSp,
                             ImVec2& tipPos, ImVec2& tipNeg, float& nsx, float& nsy,
                             float& worldPerPx, uint32_t& bodyId, uint32_t& topoId) {
    if (st.selection.size() != 1) return false;
    const render::PickHit& h = st.selection.items().front().hit;
    if (!h.valid() || h.kind != render::PickKind::Face) return false;
    uint64_t key = ((uint64_t)h.body_id << 32) | (uint64_t)h.topo_id;
    auto it = st.faceNormalCache.find(key);
    if (it == st.faceNormalCache.end()) return false;
    const auto& v = it->second;  // {nx,ny,nz, cx,cy,cz}
    float nx = v[0], ny = v[1], nz = v[2];
    float nlen = std::sqrt(nx*nx + ny*ny + nz*nz);
    if (nlen < 1e-6f) return false;
    nx /= nlen; ny /= nlen; nz /= nlen;
    float cx = v[3], cy = v[4], cz = v[5];

    float C[3] = {cx, cy, cz};
    if (!ppProject(renderer, vr0, vr1, vw, vh, C, centroidSp)) return false;

    // world-per-pixel at the face depth: project a point one mm along the normal and measure the
    // screen displacement. (Robust to perspective: uses the centroid's actual depth, not the orbit
    // target plane.) Use a 1mm probe; if the normal is near edge-on the screen vector is tiny, so
    // fall back to the camera-plane scale published by Renderer::worldPerPixel().
    //
    // INVARIANT (extrude-arrow sign): tipPos/tipNeg are the endpoints actually drawn, and nsx/nsy
    // is always (tipPos - centroid) normalized — the same screen direction the arrow is rasterized
    // along. Drag projects the cursor delta onto THAT, so the handle always moves with the cursor
    // (no separate probe-normal projection for the delta path that can invert vs the drawn arrow).
    float Cp[3] = {cx + nx, cy + ny, cz + nz};
    ImVec2 cpSp;
    bool gotProbe = ppProject(renderer, vr0, vr1, vw, vh, Cp, cpSp);
    float sdx = gotProbe ? (cpSp.x - centroidSp.x) : 0.0f;
    float sdy = gotProbe ? (cpSp.y - centroidSp.y) : 0.0f;
    float slen = std::sqrt(sdx*sdx + sdy*sdy);

    if (slen > 1e-3f) {
        nsx = sdx / slen; nsy = sdy / slen;
        worldPerPx = 1.0f / slen;   // 1mm spans `slen` px => mm-per-px = 1/slen
    } else {
        // Face is nearly edge-on: fall back to the published camera basis + worldPerPixel seam
        // (matches pan/zoom conversion in both ortho and perspective).
        worldPerPx = renderer.worldPerPixel(vh);
        const auto& cam = renderer.camera();
        Vec3 N(nx, ny, nz);
        float rN = dot(cam.right, N), uN = dot(cam.up, N);
        float l2 = std::sqrt(rN*rN + uN*uN);
        if (l2 < 1e-4f) return false;  // truly edge-on AND parallel to view: no usable handle
        // screen x is +R, screen y is -U (y down).
        nsx = rN / l2; nsy = -uN / l2;
    }

    float armLenPx = 46.0f * ui::uiScale;  // arrow length in screen px
    tipPos = ImVec2(centroidSp.x + nsx * armLenPx, centroidSp.y + nsy * armLenPx);
    tipNeg = ImVec2(centroidSp.x - nsx * armLenPx, centroidSp.y - nsy * armLenPx);
    // Re-derive unit screen dir from the drawn endpoints (single source of truth for drag sign).
    {
        float tdx = tipPos.x - centroidSp.x, tdy = tipPos.y - centroidSp.y;
        float tlen = std::sqrt(tdx * tdx + tdy * tdy);
        if (tlen > 1e-3f) { nsx = tdx / tlen; nsy = tdy / tlen; }
    }
    bodyId = h.body_id; topoId = h.topo_id;
    return true;
}

// Queue a latest-wins push-pull sample. The kick issues doc_revert then doc_push_pull
// back-to-back on AsyncRpc FIFO so the checkpoint envelope stays paired without blocking GL.
static void ppKickAsync(AppState& st);

// Keep the last applied geometry when an agent takes over; never consume the checkpoint.
static void ppAbandon(AppState& st) {
    if (st.docLockedByAgent(s_pp.docId)) toastViewportEditKept(st, s_pp.docId);
    s_pp.reset();  // invalidates queued completions as well as pending samples
}

static void ppQueueSample(AppState& st, double cumulative, bool commit) {
    if (!st.docMirror.hasDoc()) return;
    if (st.docLockedByAgent(s_pp.docId) || st.docMirror.docId() != s_pp.docId) {
        ppAbandon(st);
        return;
    }
    if (s_pp.finishing && !commit) return;  // no more previews after mouse-up
    s_pp.curDist = cumulative;
    if (commit) {
        if (s_pp.finishing) return;  // commit already queued this gesture
        s_pp.wantCommit = true;
        s_pp.finishing = true;
    }
    s_pp.pending = true;
    ppKickAsync(st);
}

static void ppKickAsync(AppState& st) {
    if (st.docLockedByAgent(s_pp.docId) || st.docMirror.docId() != s_pp.docId) {
        ppAbandon(st);
        return;
    }
    if (s_pp.inFlight || !s_pp.pending) return;
    if (!st.docMirror.hasDoc() || s_pp.checkpoint.empty()) return;
    s_pp.pending = false;
    s_pp.inFlight = true;
    const uint64_t gen = ++s_pp.gen;
    const double dist = s_pp.curDist;
    const bool commit = s_pp.wantCommit;
    if (commit) s_pp.wantCommit = false;
    const std::string docId = s_pp.docId;
    const std::string cp = s_pp.checkpoint;
    const int bodyId = (int)s_pp.bodyId;
    const int topoId = (int)s_pp.topoId;

    // Pair: revert then push_pull. Only the push_pull drain callback clears inFlight / re-kicks.
    st.rpc.call("doc_revert",
                nlohmann::json{{"doc_id", docId}, {"checkpoint", cp}},
                [gen, docId, &st](const core::RpcResult& r) {
                    if (gen != s_pp.gen) return;  // cancelled
                    if (st.docLockedByAgent(docId) || st.docMirror.docId() != docId) {
                        ppAbandon(st);
                        return;
                    }
                    if (r.ok && r.result.is_object() && !r.result.contains("code"))
                        st.docMirror.ingestOpsResponse(r.result);
                    // Mesh refresh deferred to the push_pull result (same sample).
                }, /*timeoutMs=*/60000);

    nlohmann::json ppParams = {
        {"doc_id", docId},
        {"body_id", bodyId},
        {"topo_id", topoId},
        {"distance", dist},
        {"commit", commit}
    };
    st.rpc.call("doc_push_pull", std::move(ppParams),
                [gen, docId, dist, commit, &st](const core::RpcResult& r) {
                    if (gen != s_pp.gen) return;
                    if (st.docLockedByAgent(docId) || st.docMirror.docId() != docId) {
                        ppAbandon(st);
                        return;
                    }
                    s_pp.inFlight = false;
                    if (!r.ok) {
                        st.toast(ui::ToastLevel::Error, "Push-pull failed: " + r.err);
                    } else {
                        auto pr = st.docMirror.ingestPushPullResponse(r.result);
                        if (!pr.ok) {
                            st.toast(ui::ToastLevel::Error, "Push-pull failed: " + pr.error_message);
                        } else if (!pr.draggable) {
                            st.toast(ui::ToastLevel::Warn,
                                     "This face can't be dragged — " + pr.reason);
                        } else {
                            if (!pr.ops.aicm_path.empty()) {
                                std::string le; st.loadDocMesh(pr.ops.aicm_path, le);
                            }
                            s_pp.haveReadout = true;
                            s_pp.param = pr.param;
                            s_pp.newValue = pr.new_value;
                            s_pp.clamped = pr.clamped;
                            s_pp.lastApplied = dist;
                        }
                    }
                    if (commit) {
                        // Final sample of this drag — clear envelope (checkpoint already consumed).
                        s_pp.active = false;
                        s_pp.checkpoint.clear();
                        s_pp.haveReadout = false;
                        s_pp.pending = false;
                        s_pp.wantCommit = false;
                        return;
                    }
                    if (s_pp.pending || s_pp.wantCommit) ppKickAsync(st);
                }, /*timeoutMs=*/60000);
}

// Cancel an in-flight push-pull drag: revert to the checkpoint and clear all drag state. Safe to
// call when no drag is active (no-op). Leaves NO zombie checkpoint/flag behind.
static void ppCancel(AppState& st) {
    if (!s_pp.active && s_pp.checkpoint.empty()) { s_pp.reset(); return; }
    if (st.docLockedByAgent(s_pp.docId) || st.docMirror.docId() != s_pp.docId) {
        ppAbandon(st);
        return;
    }
    ++s_pp.gen;  // drop in-flight async completions
    s_pp.inFlight = false;
    s_pp.pending = false;
    s_pp.wantCommit = false;
    if (!s_pp.checkpoint.empty() && st.docMirror.hasDoc()) {
        auto rv = st.docMirror.revert(st.worker, s_pp.checkpoint);
        if (rv.ok && !rv.aicm_path.empty()) { std::string le; st.loadDocMesh(rv.aicm_path, le); }
    }
    s_pp.reset();
}

// ---- Move-Component drag helpers ------------------------------------------------------------

// True when the mirror's open document is an assembly (aicad.asm/1). Mirrors feature_tree.cpp.
static bool docIsAssembly(AppState& st) {
    const auto& tree = st.docMirror.tree();
    return tree.is_object() && tree.value("schema", std::string()) == "aicad.asm/1";
}

// The component object owning a 1-based AICM body id in an assembly: the ordinal among
// NON-suppressed components (shared with the FeatureManager's viewport->tree sync, so both agree).
// nullptr for component-pattern instances and out-of-range ids.
static const nlohmann::json* asmComponentByOrdinal(AppState& st, uint32_t bodyId) {
    return ui::featureTreeAssemblyComponent(st.docMirror.tree(), bodyId);
}

// Component world origin (the translation of its row-major 16-float transform). Falls back to the
// cached body bbox centre, then origin. transform[3],[7],[11] are tx,ty,tz (row-major).
static Vec3 asmComponentOrigin(AppState& st, const nlohmann::json* comp, uint32_t bodyId) {
    if (comp && comp->contains("transform") && (*comp)["transform"].is_array() &&
        (*comp)["transform"].size() == 16) {
        const auto& t = (*comp)["transform"];
        return Vec3(t[3].get<float>(), t[7].get<float>(), t[11].get<float>());
    }
    auto bit = st.bodyBBoxCache.find(bodyId);
    if (bit != st.bodyBBoxCache.end()) {
        const auto& b = bit->second;
        return Vec3((b[0] + b[3]) * 0.5f, (b[1] + b[4]) * 0.5f, (b[2] + b[5]) * 0.5f);
    }
    return Vec3(0, 0, 0);
}

// Resolve a component source path relative to the active assembly doc (same rules as
// feature_tree.cpp's resolveComponentPath: absolute stays absolute; relative joins the
// assembly parent directory). assemblyDocPath is the assembly's on-disk path.
static std::string resolveVpComponentPath(const std::string& srcPath,
                                          const std::string& assemblyDocPath) {
    if (srcPath.empty()) return {};
    namespace fs = std::filesystem;
    fs::path path(srcPath);
    if (path.is_absolute()) return path.lexically_normal().string();
    if (assemblyDocPath.empty()) return {};
    return (fs::path(assemblyDocPath).parent_path() / path).lexically_normal().string();
}

// ---- Wave4: chat context-ref capture (composer "context cube") ----------------------------
// Human feature name from the mirror tree (fallback to fid).
static std::string contextFeatureName(AppState& st, const std::string& fid) {
    if (fid.empty()) return {};
    const nlohmann::json* f = findFeature(st.docMirror.tree(), fid);
    if (!f) return fid;
    std::string n = f->value("name", std::string());
    return n.empty() ? fid : n;
}

// Attach a viewport pick (face / edge / vertex) as a chat context chip. The chip appears at once
// with its local owner/component names; ui::addContextRef then resolves it asynchronously on
// st.rpc (doc_resolve_pick) into the engine selector + stable sid the model can target — never a
// synchronous worker call on the GL thread, and never skipped while a turn is running.
static void addPickContextRef(AppState& st, const render::PickHit& h) {
    if (!h.valid()) return;
    core::ContextRef ref;
    ref.kind = h.kind == render::PickKind::Face ? "face"
             : h.kind == render::PickKind::Edge ? "edge"
                                                : "vertex";
    ref.bodyId = h.body_id;
    ref.topoId = h.topo_id;
    ref.revision = st.docMirror.revision();
    ref.id = ref.kind + ":" + std::to_string(h.body_id) + ":" + std::to_string(h.topo_id);
    std::string word = ref.kind;
    word[0] = (char)std::toupper((unsigned char)word[0]);
    if (docIsAssembly(st)) {
        if (const nlohmann::json* comp = asmComponentByOrdinal(st, h.body_id)) {
            ref.component = comp->value("id", std::string());
            ref.componentName = comp->value("name", std::string());
        }
        const std::string of = ref.componentName.empty() ? ref.component : ref.componentName;
        ref.label = of.empty() ? word : word + " of " + of;
    } else {
        ref.owner = h.kind == render::PickKind::Face ? st.docMirror.ownerOfFace(h.body_id, h.topo_id)
                  : h.kind == render::PickKind::Edge ? st.docMirror.ownerOfEdge(h.body_id, h.topo_id)
                                                     : std::string();
        ref.ownerName = contextFeatureName(st, ref.owner);
        if (!ref.ownerName.empty()) ref.label = word + " of " + ref.ownerName;
        else {
            ref.label = st.pickLabel(h);
            if (ref.label.empty()) ref.label = word;
        }
    }
    ui::addContextRef(st, std::move(ref));
}

static void addComponentContextRef(AppState& st, const std::string& cid,
                                   const std::string& cname) {
    core::ContextRef ref;
    ref.kind = "component";
    ref.id = cid;
    ref.label = cname.empty() ? cid : cname;
    ref.componentName = cname;
    ui::addContextRef(st, std::move(ref));
}


// Latest-wins async move-component (revert + doc_solve_mates pair on AsyncRpc FIFO).
static void asmDragKickAsync(AppState& st);

static void asmDragAbandon(AppState& st) {
    if (st.docLockedByAgent(s_asmDrag.docId)) toastViewportEditKept(st, s_asmDrag.docId);
    s_asmDrag.reset();
}

static void asmDragQueue(AppState& st, const ImVec2& cur, bool commit) {
    if (!st.docMirror.hasDoc() || s_asmDrag.component.empty()) return;
    if (st.docLockedByAgent(s_asmDrag.docId) || st.docMirror.docId() != s_asmDrag.docId) {
        asmDragAbandon(st);
        return;
    }
    if (s_asmDrag.finishing && !commit) return;
    s_asmDrag.curPos = cur;
    if (commit) {
        if (s_asmDrag.finishing) return;
        s_asmDrag.wantCommit = true;
        s_asmDrag.finishing = true;
    }
    s_asmDrag.pending = true;
    asmDragKickAsync(st);
}

static void asmDragKickAsync(AppState& st) {
    if (st.docLockedByAgent(s_asmDrag.docId) || st.docMirror.docId() != s_asmDrag.docId) {
        asmDragAbandon(st);
        return;
    }
    if (s_asmDrag.inFlight || !s_asmDrag.pending) return;
    if (!st.docMirror.hasDoc() || s_asmDrag.checkpoint.empty()) return;
    s_asmDrag.pending = false;
    s_asmDrag.inFlight = true;
    const uint64_t gen = ++s_asmDrag.gen;
    const ImVec2 cur = s_asmDrag.curPos;
    const bool commit = s_asmDrag.wantCommit;
    if (commit) s_asmDrag.wantCommit = false;
    const std::string docId = s_asmDrag.docId;
    const std::string cp = s_asmDrag.checkpoint;
    const std::string component = s_asmDrag.component;

    float dx = cur.x - s_asmDrag.pressPos.x;
    float dy = cur.y - s_asmDrag.pressPos.y;
    float k = s_asmDrag.worldPerPx;
    Vec3 target(
        s_asmDrag.startPos.x + (s_asmDrag.camR.x * dx - s_asmDrag.camU.x * dy) * k,
        s_asmDrag.startPos.y + (s_asmDrag.camR.y * dx - s_asmDrag.camU.y * dy) * k,
        s_asmDrag.startPos.z + (s_asmDrag.camR.z * dx - s_asmDrag.camU.z * dy) * k);

    st.rpc.call("doc_revert",
                nlohmann::json{{"doc_id", docId}, {"checkpoint", cp}},
                [gen, docId, &st](const core::RpcResult& r) {
                    if (gen != s_asmDrag.gen) return;
                    if (st.docLockedByAgent(docId) || st.docMirror.docId() != docId) {
                        asmDragAbandon(st);
                        return;
                    }
                    if (r.ok && r.result.is_object() && !r.result.contains("code"))
                        st.docMirror.ingestOpsResponse(r.result);
                }, /*timeoutMs=*/60000);

    nlohmann::json params = {
        {"doc_id", docId},
        {"drag", {{"component", component},
                  {"target", {target.x, target.y, target.z}}}}};
    st.rpc.call("doc_solve_mates", std::move(params),
                [gen, docId, cur, commit, &st](const core::RpcResult& r) {
                    if (gen != s_asmDrag.gen) return;
                    if (st.docLockedByAgent(docId) || st.docMirror.docId() != docId) {
                        asmDragAbandon(st);
                        return;
                    }
                    s_asmDrag.inFlight = false;
                    if (!r.ok) {
                        st.toast(ui::ToastLevel::Error, "Move component failed: " + r.err);
                    } else if (r.result.is_object() && r.result.contains("code")) {
                        st.toast(ui::ToastLevel::Error,
                                 "Move component: " + r.result.value("message", std::string()));
                    } else {
                        // Prefer ops-shaped ingest; fall back to resync for older envelopes.
                        if (r.result.is_object() &&
                            (r.result.contains("revision") || r.result.contains("tree"))) {
                            st.docMirror.ingestOpsResponse(r.result);
                        } else {
                            st.docMirror.resync(st.worker);
                        }
                        std::string aicm;
                        if (r.result.contains("tess") && r.result["tess"].is_object())
                            aicm = r.result["tess"].value("aicm_path", std::string());
                        if (!aicm.empty()) { std::string le; st.loadDocMesh(aicm, le); }
                        s_asmDrag.lastSent = cur;
                    }
                    if (commit) {
                        s_asmDrag.active = false;
                        s_asmDrag.checkpoint.clear();
                        s_asmDrag.pending = false;
                        s_asmDrag.wantCommit = false;
                        return;
                    }
                    if (s_asmDrag.pending || s_asmDrag.wantCommit) asmDragKickAsync(st);
                }, /*timeoutMs=*/60000);
}

// Cancel an in-flight move-component drag: revert to the checkpoint and clear state. No-op when no
// drag is active.
static void asmDragCancel(AppState& st) {
    if (!s_asmDrag.active && s_asmDrag.checkpoint.empty()) { s_asmDrag.reset(); return; }
    if (st.docLockedByAgent(s_asmDrag.docId) || st.docMirror.docId() != s_asmDrag.docId) {
        asmDragAbandon(st);
        return;
    }
    ++s_asmDrag.gen;
    s_asmDrag.inFlight = false;
    s_asmDrag.pending = false;
    s_asmDrag.wantCommit = false;
    if (!s_asmDrag.checkpoint.empty() && st.docMirror.hasDoc()) {
        auto rv = st.docMirror.revert(st.worker, s_asmDrag.checkpoint);
        if (rv.ok && !rv.aicm_path.empty()) { std::string le; st.loadDocMesh(rv.aicm_path, le); }
    }
    s_asmDrag.reset();
}

// ---- Instant3D dimension-handle drag helpers ------------------------------------------------

// Screen geometry for one dimension handle: project the world anchor to screen, derive the unit
// screen-space drag direction (along which the value GROWS), the two arrow tips, and the world(mm)
// or deg per screen-pixel scale. For a LENGTH dim the px scale is the exact 1mm-world-probe used by
// ppHandleGeometry (probe the anchor +1mm along dirW, measure screen displacement). For an ANGLE
// dim the handle is laid horizontally on screen and a fixed deg/px maps drag to degrees.
static bool dimHandleGeometry(AppState& st, const Renderer& renderer, const ImVec2& vr0,
                              const ImVec2& vr1, int vw, int vh, const DimCandidate& c,
                              ImVec2& anchorSp, ImVec2& tipPos, ImVec2& tipNeg,
                              float& dsx, float& dsy, float& worldPerPx, float& degPerPx) {
    float A[3] = {c.anchor[0], c.anchor[1], c.anchor[2]};
    if (!ppProject(renderer, vr0, vr1, vw, vh, A, anchorSp)) return false;

    if (c.angle) {
        // Angle handle: horizontal on screen; a constant deg/px (SolidWorks-ish drag sensitivity).
        dsx = 1.0f; dsy = 0.0f;
        worldPerPx = 0.0f;
        degPerPx = 0.5f;   // 720px == full turn
    } else {
        // Probe 1mm along the measured world direction; the screen displacement gives both the
        // screen-space direction and the world-per-px scale at this depth (perspective-correct).
        float P[3] = {A[0] + c.dirW.x, A[1] + c.dirW.y, A[2] + c.dirW.z};
        ImVec2 pSp;
        bool gotProbe = ppProject(renderer, vr0, vr1, vw, vh, P, pSp);
        float sdx = gotProbe ? (pSp.x - anchorSp.x) : 0.0f;
        float sdy = gotProbe ? (pSp.y - anchorSp.y) : 0.0f;
        float slen = std::sqrt(sdx*sdx + sdy*sdy);
        if (slen > 1e-3f) {
            dsx = sdx / slen; dsy = sdy / slen;
            worldPerPx = 1.0f / slen;       // 1mm spans `slen` px
        } else {
            // Direction is nearly edge-on to the screen: fall back to the published camera-plane
            // scale and a screen-horizontal handle so the dim is still grabbable (rare; degenerate).
            worldPerPx = renderer.worldPerPixel(vh);
            dsx = 1.0f; dsy = 0.0f;
        }
        degPerPx = 0.0f;
    }
    float armLenPx = 42.0f * ui::uiScale;
    tipPos = ImVec2(anchorSp.x + dsx * armLenPx, anchorSp.y + dsy * armLenPx);
    tipNeg = ImVec2(anchorSp.x - dsx * armLenPx, anchorSp.y - dsy * armLenPx);
    // Unit drag dir from the drawn endpoints (single source of truth for sign).
    {
        float tdx = tipPos.x - anchorSp.x, tdy = tipPos.y - anchorSp.y;
        float tlen = std::sqrt(tdx * tdx + tdy * tdy);
        if (tlen > 1e-3f) { dsx = tdx / tlen; dsy = tdy / tlen; }
    }
    return true;
}

// (The dimension drag now feeds the open PropertyManager field directly via dragSetDim(); the old
// independent edit_feature/checkpoint sample+cancel helpers were removed with that rework — the PM
// owns the live preview and the single OK/Cancel commit, so there is no separate doc op to manage.)

void drawViewportPanel(AppState& st, ImGuiIO& io, bool busy) {
    // Check the session's document before any input, cancel, or outside-viewport commit path.
    if ((s_pp.active || !s_pp.checkpoint.empty()) &&
        (st.docLockedByAgent(s_pp.docId) || st.docMirror.docId() != s_pp.docId))
        ppAbandon(st);
    if ((s_asmDrag.active || !s_asmDrag.checkpoint.empty()) &&
        (st.docLockedByAgent(s_asmDrag.docId) || st.docMirror.docId() != s_asmDrag.docId))
        asmDragAbandon(st);
    if (s_dim.active &&
        (st.docLockedByAgent(s_dim.docId) || st.docMirror.docId() != s_dim.docId))
        s_dim.reset();  // PM owns keep-close; do not restore the old value or re-preview.
    // Valid only for an explicit pick demand serviced by this invocation.
    st.pickReady = false;
    if (!st.renderer) return;
    Renderer& renderer = *st.renderer;
    const bool sceneActive = st.activeWorkspace == AppState::Workspace::Simulate &&
                             ui::simscene::active(st);
    const bool sceneToolArmed = sceneActive && ui::simscene::toolArmed(st);
    const uint64_t sceneDocumentScope = simulationDocumentScope(st);
    static ui::simscene::Scene simulationScene;
    simulationScene.clear();  // retain capacities across frames and document switches
    if (sceneActive) ui::simscene::collect(st, simulationScene);
    // Collect before bbox requests and camera ticking: a producer may request framing while it
    // publishes new geometry. Changing only R/zoom leaves the view and orbit center untouched.
    // A document switch restores that document's own camera, so a radius change caused by
    // dropping the previous document's auxiliary bounds/floor must not rescale it.
    const float previousSceneRadius = renderer.effectiveSceneRadius();
    const bool sameSceneScope =
        updateSimulationSceneBounds(st, simulationScene, sceneDocumentScope, sceneToolArmed);
    const float sceneRadius = renderer.effectiveSceneRadius();
    if (sameSceneScope && sceneRadius != previousSceneRadius) {
        const float ratio = sceneRadius / previousSceneRadius;
        // Both projection scales and the orbit distance depend only on R/zoom. The bounds
        // helper already deferred unsafe shrinking; maxSceneZoom scales by the same ratio.
        // Do not clamp here: doing so would alter the absolute view (or the animated path).
        st.zoom *= ratio;
        if (st.animActive) {
            st.animZoom0 *= ratio;
            st.animZoom1 *= ratio;
        }
    }
    // Liquid-glass v2 (H6a): st.renderer is a stable singleton for the app's lifetime (set once
    // in run_gui), so refreshing this pointer + registering the sampler here covers every branch
    // below (live 3D, Drawing tab, hidden dock tab) with one call.
    s_bdRenderer = &renderer;
    registerViewportLuminanceSamplerOnce();
    auto viewportTitle = [&]() {
        if (st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size()) {
            const auto& tab = st.docTabs[st.activeTab];
            std::string label = tab.displayTitle();   // real file name (matches the doc-tab strip)
            if (tab.dirty) label += "*";
            return label + " - Viewport###Viewport";
        }
        return std::string("Viewport###Viewport");
    };

    // ---------- DRAWINGS module: a Drawing tab hosts a 2D sheet in the SAME central dock window
    // as the 3D viewport. Branch here BEFORE any 3D state is touched so part/asm behavior stays
    // byte-identical and the dock layout is unchanged (the "Viewport" window just paints a sheet).
    if (st.showViewport && st.activeTabIsDrawing()) {
        const std::string title = viewportTitle();
        if (ImGui::Begin(title.c_str())) {
            vpDockStripDraw(st);   // hide the redundant dock tab; app.cpp owns the filename strip
            ui::drawDrawingSheet(st);
        }
        ImGui::End();
        invalidateViewportBackdrop();
        return;
    }
    // Drawing workspace with no drawing tab active: a kit empty state (Make Drawing + open
    // drawings) instead of the 3D scene.
    if (st.showViewport && st.activeWorkspace == AppState::Workspace::Drawing &&
        !st.activeTabIsDrawing()) {
        const std::string title = viewportTitle();
        if (ImGui::Begin(title.c_str())) {
            vpDockStripDraw(st);
            ui::drawDrawingEmptyState(st);
        }
        ImGui::End();
        invalidateViewportBackdrop();
        return;
    }

    // ---------- Simulate > Hydraulics (hook): while the Hydraulics panel shows its Schematic or
    // Plots view, the central Viewport window paints that full-size 2D canvas instead of the 3D
    // scene — the same branch shape as the Drawing sheet above. SimManager/HydraulicPanel own the
    // content; the panel's "3D" toggle makes this false and the normal 3D path below runs.
    if (st.showViewport && st.activeWorkspace == AppState::Workspace::Simulate && st.simManager &&
        st.simManager->centralViewActive()) {
        const std::string title = viewportTitle();
        if (ImGui::Begin(title.c_str())) {
            vpDockStripDraw(st);
            st.simManager->drawCentralView(st);
        }
        ImGui::End();
        invalidateViewportBackdrop();
        return;
    }

    // C18: while a sketch is being edited the camera locks normal to the sketch plane and the
    // sketch tools own the mouse (no orbit, no topology picking).
    auto* sketch = st.sketchMode;
    const bool inSketch = sketch && sketch->active();
    // 3D sketcher: free camera (MMB orbit stays), but LMB belongs to the overlay and ALL camera
    // motion freezes while an entity drag is armed/live (dead-zone rule, mirrors the 2D sketcher).
    auto* sketch3d = st.sketch3dMode;
    const bool inSketch3d = sketch3d && sketch3d->active();

    // ---------- Viewport ----------
    if (st.showViewport) {
    const std::string title = viewportTitle();
    // W2 full-bleed viewport: the central Viewport window paints NO background so the full-window 3D
    // (composited on the background draw list) shows through the central dock node. The doc-tab strip
    // and assembly parts panel still paint their own backgrounds on top of the 3D. (Also requires the
    // dockspace backdrop to be transparent — app.cpp pushes a transparent ImGuiCol_WindowBg around the
    // DockSpace() call, since PassthruCentralNode only punches the hole when the central node is empty.)
    ImGui::SetNextWindowBgAlpha(0.0f);
    if (ImGui::Begin(title.c_str()))
    {
        vpDockStripDraw(st);        // hide the redundant dock tab; app.cpp owns the filename strip
        // (The "Create New" chooser modal is hosted at app scope — ui::drawNewDocChooser — so it
        //  works regardless of which central document is forward.)

        // ---- Move/Rotate Entities gizmo HUD (just under the file title, top-left of the viewport) ----
        // A compact glass panel with a green check / red X to finish/cancel plus the live X/Y offset
        // (Move) or an editable angle field (Rotate). Buttons/typed values route back to SketchMode,
        // which the tool branch reads next frame (commit/cancel/setHudAngle).
        if (sketch && sketch->active()) {
            SketchMode::TransformHud h = sketch->transformHudState();
            if (h.kind != 0) {
                const float s = ui::uiScale;
                ImGui::BeginChild("##gizmoHud", ImVec2(206 * s, 0),
                                  ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY,
                                  ImGuiWindowFlags_NoScrollbar);
                ImGui::TextUnformatted(h.kind == 1 ? "Move Entities" : "Rotate Entities");
                ImGui::Separator();
                if (h.kind == 1) {
                    ImGui::Text("\xCE\x94X  %8.3f mm", h.dx);
                    ImGui::Text("\xCE\x94Y  %8.3f mm", h.dy);
                } else {
                    float ang = h.angle;
                    ImGui::TextUnformatted("Angle");
                    ImGui::SameLine();
                    ImGui::SetNextItemWidth(96 * s);
                    if (ImGui::InputFloat("##gzAngle", &ang, 0.0f, 0.0f, "%.2f\xC2\xB0",
                                          ImGuiInputTextFlags_EnterReturnsTrue))
                        sketch->setHudAngle(ang);
                    else if (ImGui::IsItemDeactivatedAfterEdit())
                        sketch->setHudAngle(ang);
                }
                ImGui::Spacing();
                // The app's primary/danger buttons. The old hand-rolled green/red pushes also
                // carried U+2713/U+2717 in the label, which the app font has no codepoint for.
                if (ui::kit::primaryButton("Finish", true, nullptr, ImVec2(96 * s, 0)))
                    sketch->commitTransform(st);
                ImGui::SameLine();
                if (ui::kit::dangerButton("Cancel", true, nullptr, ImVec2(96 * s, 0)))
                    sketch->cancelTransform();
                ImGui::EndChild();
            }
        }


        // ---- assembly fit readout + parts list (multi-part builds only) ----
        if (!st.asmParts.empty()) {
            ImGui::Separator();
            drawFitReadout(st.asmFit, /*detailed=*/true);   // always visible (key readout)
            // parts selector — scrolls if there are many bodies
            float ph = ImGui::GetContentRegionAvail().y * 0.30f;
            ph = ph > 150 ? 150 : (ph < 78 ? 78 : ph);
            ImGui::BeginChild("partspanel", ImVec2(0, ph), ImGuiChildFlags_Border);
            const auto& palVp = ui::palette;
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palVp.partsDim[0], palVp.partsDim[1], palVp.partsDim[2], 1));
            ImGui::TextUnformatted("Parts  (click to isolate in the viewport)");
            ImGui::PopStyleColor();
            if (ImGui::RadioButton("Assembly (all parts)", st.selectedPart == -1)) {
                st.selectedPart = -1;
                st.showPartMesh(-1);
            }
            for (int pi2 = 0; pi2 < (int)st.asmParts.size(); ++pi2) {
                ImGui::ColorButton(("##sw" + std::to_string(pi2)).c_str(), partColor(pi2),
                                   ImGuiColorEditFlags_NoTooltip | ImGuiColorEditFlags_NoDragDrop,
                                   ImVec2(12 * ui::uiScale, 12 * ui::uiScale));
                ImGui::SameLine(0, 6 * ui::uiScale);
                std::string lbl = st.asmParts[pi2].name + "##p" + std::to_string(pi2);
                if (ImGui::RadioButton(lbl.c_str(), st.selectedPart == pi2)) {
                    st.selectedPart = pi2;
                    st.showPartMesh(pi2);
                }
                if (!st.asmParts[pi2].dims.empty()) {
                    ImGui::SameLine();
                    ImGui::PushStyleColor(ImGuiCol_Text,
                        ImVec4(palVp.textDim[0], palVp.textDim[1], palVp.textDim[2], 1));
                    ImGui::Text("  %s", st.asmParts[pi2].dims.c_str());
                    ImGui::PopStyleColor();
                }
            }
            ImGui::EndChild();
        }

        ImVec2 avail = ImGui::GetContentRegionAvail();
        // W2 full-bleed viewport: the visible (un-occluded) 3D region is this docked window's
        // content rect. cr0/cr1 anchor the HUD chrome and drive the lens-shift re-centering.
        const ImVec2 cr0 = ImGui::GetCursorScreenPos();
        const ImVec2 cr1 = ImVec2(cr0.x + avail.x, cr0.y + avail.y);
        // Overlay layout: the 3D band renders full-WIDTH (behind the translucent pinned columns)
        // AND full-HEIGHT of this window — from the window top (under the floating doc-tab strip)
        // down to the content bottom — so the studio surface truly spans ribbon-to-status-bar and
        // the doc tabs float over live 3D. With the splitless central node the content rect already
        // spans the window width, so the lens shift below self-neutralizes (shift 0).
        ImGuiViewport* mainVp = ImGui::GetMainViewport();
        const float winTopY = ImGui::GetWindowPos().y;   // == dockspace top (central node fills it)
        // Full-bleed height runs to the WINDOW bottom (== status-bar top), not the content-region
        // bottom: the docked viewport's own WindowPadding.y inset cr1.y a few px above the window
        // edge, leaving a thin strip of dead window background above the status bar. The 3D
        // image, the FBO (renderer.resize) and the projection all key off vh, so extending it keeps
        // them consistent.
        const float winBotY = ImGui::GetWindowPos().y + ImGui::GetWindowSize().y;
        int vw = (int)mainVp->Size.x > 64 ? (int)mainVp->Size.x : 64;
        int vh = (int)(winBotY - winTopY) > 64 ? (int)(winBotY - winTopY) : 64;
        renderer.resize(vw, vh);
        // Clear (un-occluded) region of the full-bleed 3D image: between the pinned FeatureManager /
        // Simulation column (overlayColumnEdge(0)) and the Copilot column (overlayColumnEdge(1)),
        // below the floating doc-tab band, inset by a small gutter. Framing (lens shift + zoom=1
        // fit), wheel/area zoom pivots and scene-scale setup overlays all key off this rect so the
        // model is never framed under a glass pane. Falls back to the full image when the columns
        // are hidden or would leave a degenerate strip.
        const float imgX0 = mainVp->Pos.x, imgX1 = imgX0 + (float)vw;
        const float imgY0 = winTopY, imgY1 = winTopY + (float)vh;
        ImVec2 clr0(imgX0, imgY0), clr1(imgX1, imgY1);
        {
            const float gut = ui::metrics::U(3.0f);
            const float minSpan = ui::metrics::U(40.0f);
            const float le = ui::overlayColumnEdge(0);
            const float re = ui::overlayColumnEdge(1);
            const float tb = ui::docTabBandBottom();
            if (le > imgX0 && le < imgX1) clr0.x = le + gut;
            if (re > clr0.x && re < imgX1) clr1.x = re - gut;
            if (tb > imgY0 && tb < imgY1) clr0.y = tb + gut;
            clr1.y = imgY1 - gut;
            if (clr1.x - clr0.x < minSpan) { clr0.x = imgX0; clr1.x = imgX1; }
            if (clr1.y - clr0.y < minSpan) { clr0.y = imgY0; clr1.y = imgY1; }
        }
        s_clearMin = clr0;
        s_clearMax = clr1;
        const ImVec2 clrC((clr0.x + clr1.x) * 0.5f, (clr0.y + clr1.y) * 0.5f);
        // Feed the adaptive quality ladder this frame's cadence (io.DeltaTime carries
        // the vsync-cliff signal the ladder keys on). No-op unless adaptive is enabled.
        renderer.noteFrame(ImGui::GetIO().DeltaTime * 1000.0f);
        // C18: lock the camera normal to the sketch plane while editing a sketch — UNTIL the user
        // middle-drags to rotate (SolidWorks lets you orbit a sketch). s_sketchRotated latches that
        // and is cleared whenever we leave sketch mode (below), so re-entry starts normal-to again.
        if (!inSketch) s_sketchRotated = false;
        // Latch the moment an MMB orbit begins so the very first drag frame isn't snapped back to
        // normal-to before the handler (below) gets to add its delta — that first-frame fight made
        // the orbit feel dead. Once dragging, the lock yields immediately; s_sketchRotated keeps it
        // yielded for the rest of the sketch session.
        if (inSketch && !s_sketchRotated && ImGui::IsMouseDragging(ImGuiMouseButton_Middle) &&
            !ImGui::GetIO().KeyCtrl && !ImGui::GetIO().KeyShift &&
            !(sketch != nullptr && sketch->entityDragActive()))   // MMB yields to an entity drag
            s_sketchRotated = true;
        if (inSketch && !s_sketchRotated) {
            float ly, lp; if (sketch->cameraLock(ly, lp)) { st.yaw = ly; st.pitch = lp; }
        }
        // Freeze the bbox-derived camera framing while editing a sketch: real-time rebuilds of a
        // downstream feature (e.g. the extrude that consumes this sketch) must not re-frame the
        // view on every added entity. Geometry still updates live; the view stays normal-to-plane
        // at a stable zoom (no fit-to-view "bouncing"). Unfreezes on exit so the model reframes
        // normally again. Manual zoom/pan (st.zoom/st.pan) still work — only auto-framing is held.
        renderer.setFramingFrozen(inSketch);
        // C37-C: reset zoom-area state on mode transitions (avoids cross-mode state leak).
        // Check at frame start: if sketch mode OR plane-pick activated, disarm zoom-area.
        if (inSketch || st.sketchPlanePickActive) {
            s_zoomAreaArmed    = false;
            s_zoomAreaDragging = false;
            s_lmbDragActive    = false;
        }
        // Also disarm when a PM form is open (item 3 requirement).
        if (st.propertyManager && st.propertyManager->active()) {
            s_zoomAreaArmed    = false;
            s_zoomAreaDragging = false;
        }

        // Instant3D push-pull safety net (runs every frame, regardless of hover): if a sketch /
        // plane-pick / PM took over the mouse mid-drag, cancel (revert) so no zombie checkpoint/flag
        // survives. If the LMB was released while the cursor was OUTSIDE the viewport image (the
        // hover-gated release handler never ran), commit the drag at its last distance.
        if (s_pp.active) {
            bool ppPmNow = st.propertyManager && st.propertyManager->active();
            if (inSketch || st.sketchPlanePickActive || ppPmNow) {
                ppCancel(st);
            } else if (!ImGui::IsMouseDown(ImGuiMouseButton_Left) && !s_pp.finishing) {
                // Async commit; active clears when the commit sample drains.
                ppQueueSample(st, s_pp.curDist, /*commit=*/true);
            }
        }

        // Instant3D dimension-drag safety net: the drag is valid only while its feature's PM stays
        // open. If the PM closed / switched feature, or a sketch / plane-pick took over, drop the
        // drag (the PM owns the field value + the commit, so there is nothing to revert). If the LMB
        // was released outside the viewport image, leave the last value in the PM field. No zombie.
        if (s_dim.active) {
            bool pmOk = st.propertyManager && st.propertyManager->active() &&
                        st.propertyManager->targetId() == s_dim.featureId;
            if (inSketch || st.sketchPlanePickActive || !pmOk) {
                s_dim.reset();
            } else if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                st.propertyManager->dragSetDim(s_dim.param, s_dim.curValue);
                if (s_dim.autoFlip)
                    st.propertyManager->dragSetCheck("flip", s_dim.curFlip);
                s_dim.active = false;
                s_dim.haveReadout = false;
            }
        }

        // Move-component drag safety net (mirrors the push-pull one): if a sketch / plane-pick / PM
        // took over the mouse, or the doc is no longer an assembly, cancel (revert). If the LMB was
        // released outside the viewport image, do one final solve (commit). No zombie state.
        if (s_asmDrag.active) {
            bool asmPmNow = st.propertyManager && st.propertyManager->active();
            if (inSketch || st.sketchPlanePickActive || asmPmNow || !docIsAssembly(st)) {
                asmDragCancel(st);
            } else if (!ImGui::IsMouseDown(ImGuiMouseButton_Left) && !s_asmDrag.finishing) {
                // Async commit; active clears when the commit sample drains.
                asmDragQueue(st, io.MousePos, /*commit=*/true);
            }
        }

        // Evaluate Section Props plane-drag safety net: drop the handle grab if the section window
        // closes, a sketch / plane-pick / PM takes the mouse, or LMB is released outside the image.
        if (s_sectionDragIdx >= 0) {
            bool secOpen = st.evaluateDialogs && st.evaluateDialogs->sectionDragActive();
            bool secPm = st.propertyManager && st.propertyManager->active();
            if (!secOpen || inSketch || inSketch3d || st.sketchPlanePickActive || secPm ||
                !ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                s_sectionDragIdx = -1;
            }
        }

        // C37-C: consume st.zoomToBBoxRequested (one-shot from feature tree "Zoom to Feature").
        if (st.zoomToBBoxRequested) {
            st.zoomToBBoxRequested = false;
            frameBBox(st, st.zoomToBBoxMin, st.zoomToBBoxMax);
        }

        // W2-ref / ITEM 13: one-shot Zoom-to-Fit on the FIRST actual display of a doc with a
        // known model radius. Prefer per-doc fitPendingDocIds (survives opening while another
        // tab/workspace is focused); migrate the legacy global flag onto the active doc id.
        // Consumed once — never re-fits on rebuilds or later tab switches.
        {
            const std::string curDocId = st.docMirror.hasDoc() ? st.docMirror.docId() : std::string();
            if (st.fitViewOnNextMeshCommit) {
                st.fitViewOnNextMeshCommit = false;
                if (!curDocId.empty()) st.fitPendingDocIds.insert(curDocId);
            }
            if (!curDocId.empty() && st.fitPendingDocIds.count(curDocId) &&
                st.hasDocMesh && renderer.modelRadius() > 0.0f) {
                st.fitPendingDocIds.erase(curDocId);
                applyViewPreset(st, ViewPreset::ZoomFit);
            }
        }

        // Shared hasSolidContent signal (same definition as AppState::applyDocDrivenVisibility):
        // a solid/3D-producing feature OR any assembly component => references auto-hide.
        // Sketches and pure reference datums alone do NOT hide planes/origin/axes.
        bool hasSolidContent = false;
        if (st.docMirror.hasDoc()) {
            const auto& tree = st.docMirror.tree();
            if (tree.is_object()) {
                auto tci = tree.find("components");
                if (tci != tree.end() && tci->is_array() && !tci->empty()) {
                    hasSolidContent = true;
                } else {
                    auto tfi = tree.find("features");
                    if (tfi != tree.end() && tfi->is_array()) {
                        auto isRefOnly = [](const std::string& type) {
                            return type == "sketch" || type == "sketch3d" ||
                                   type == "plane" || type == "datum_plane" || type == "ref_plane" ||
                                   type == "axis" || type == "datum_axis" || type == "ref_axis" ||
                                   type == "point" || type == "datum_point" || type == "ref_point" ||
                                   type == "csys" || type == "coordinate_system" || type == "ref_csys";
                        };
                        for (const auto& f : *tfi) {
                            if (!f.is_object() || f.value("suppressed", false)) continue;
                            const std::string type = f.value("type", std::string());
                            if (!type.empty() && !isRefOnly(type)) {
                                hasSolidContent = true;
                                break;
                            }
                        }
                    }
                }
            }
        }

        // Reference planes: hidden while sketching (the sketch grid owns the view), else user state.
        // Until the user takes control, each default plane stays visible until a downstream 3D
        // feature consumes it. AppState records those transitive dependency hits alongside consumed
        // user-created planes; this replaces the old all-or-nothing "any body hides all planes".
        // ITEM 2: also auto-hides all three whenever hasSolidContent (not mere sketches).
        bool effPlane[3] = {st.showPlane[0], st.showPlane[1], st.showPlane[2]};
        if (st.docMirror.hasDoc() && !st.planesUserToggled) {
            static const char* kPlaneFids[3] = {"plane.front", "plane.top", "plane.right"};
            for (int i = 0; i < 3; ++i)
                effPlane[i] = !st.autoHiddenPlanes.count(kPlaneFids[i]);
        }
        // A feature PM with an active plane-accepting well (ref-axis "Two Planes", ref-plane "Mid
        // Plane", …): force-show the origin planes so the user can click one, same as the new-sketch
        // plane pick. The plane picker (drawPlanePicker) hit-tests them and feeds pmPlaneHover() back.
        bool pmWantsPlane = !inSketch && st.propertyManager && st.propertyManager->active() &&
                            st.propertyManager->activeWellWantsPlane();
        // C37-C item 5: while sketchPlanePickActive, force-show all three origin planes so a first
        // sketch has something to click — BUT only on an empty scene. If visible model geometry is
        // already present (e.g. an extruded cube the user wants to pick a face of), forcing the three
        // datum planes on just clutters the view over the geometry; the plane hit-test still runs, so
        // the user can pick a planar face instead. We only ever OR-in true (never force-HIDE), so a
        // plane the user explicitly turned on stays on regardless. "Visible geometry" = at least one
        // shown solid body (bodyVisible is one slot per non-empty body, default-visible on load).
        bool anyVisibleBody = false;
        for (uint8_t v : st.bodyVisible)
            if (v) { anyVisibleBody = true; break; }
        // pmWantsPlane (ref-plane/ref-axis creation) genuinely needs the origin planes as pickable
        // datums even amid geometry, so it stays unconditional.
        if ((st.sketchPlanePickActive && !anyVisibleBody) || pmWantsPlane)
            effPlane[0] = effPlane[1] = effPlane[2] = true;
        // Publish the effective visibility so plane hit-tests (drawPlanePicker / new-sketch pick /
        // hitTestOriginPlane) gate on what is actually DRAWN, not the raw showPlane toggle. Without
        // this, a force-shown plane renders but stays unclickable (issues #20/#21).
        for (int pv = 0; pv < 3; ++pv) st.planeVisEff[pv] = effPlane[pv];

        // Origin gizmo + axis auto-hide/force-show: one effective hidden-datums set per frame.
        // Origin auto-hide: show until hasSolidContent; hide once a solid exists (unless user-toggled).
        // Axis auto-hide: hasSolidContent gate with per-id sticky axesUserToggledIds.
        // Defaults still honor aggregate axesUserToggled; axis.auto:* always auto-hide on
        // solid parts unless THAT id was eye-toggled (global flag must not unlock auto axes).
        // ALSO keep st.hiddenFeatures in step so FM eyes match draw. PM axis wells force-show.
        bool pmWantsAxis = !inSketch && !inSketch3d && st.propertyManager &&
                           st.propertyManager->active() &&
                           st.propertyManager->activeWellWantsAxis();
        // Sketch force-hide: once a part has solid geometry, non-sticky axes stay hidden while
        // sketching. Sticky per-id eyes still honor hiddenFeatures. Empty/sketch-only docs
        // keep default axes as a first-sketch reference.
        const bool sketchHideAxes = (inSketch || inSketch3d) && hasSolidContent;
        // Always need effDatums while auto axes may need per-id hide, or origin/PM/sketch paths.
        const bool needEffDatums =
            !st.originUserToggled || !st.axesUserToggled || pmWantsAxis || sketchHideAxes ||
            !st.axisDatumIds.empty();
        if (needEffDatums) {
            // Persist auto axis visibility into hiddenFeatures so FM eyes match the draw path.
            // Per-id sticky skips; defaults skip only when aggregate axesUserToggled.
            auto syncAxisHid = [&](const std::string& id) {
                if (id.empty()) return;
                if (st.axesUserToggledIds.count(id)) return;
                const bool isAuto = id.rfind("axis.auto:", 0) == 0;
                if (!isAuto && st.axesUserToggled) return;
                if (hasSolidContent) st.hiddenFeatures.insert(id);
                else                 st.hiddenFeatures.erase(id);
            };
            // Defaults first (pre-inject / empty axisDatumIds still lists X/Y/Z in the tree).
            syncAxisHid("axis.x");
            syncAxisHid("axis.y");
            syncAxisHid("axis.z");
            for (const auto& id : st.axisDatumIds) syncAxisHid(id);
            // Self-heal from the renderer's OWN datum set: any axis id that can draw must be
            // visibility-managed here, even if the DocMirror-listener inject never ran for it
            // (cached-AICM opens, instant tab binds, pre-auto-axis files). Otherwise it renders
            // with no FM row and no hiddenFeatures entry.
            for (const auto& rd : renderer.refDatums())
                if (rd.kind == Renderer::RefDatum::Kind::Axis) syncAxisHid(rd.id);

            std::unordered_set<std::string> effDatums = st.hiddenFeatures;
            if (!st.originUserToggled) {
                if (hasSolidContent) effDatums.insert("point.origin");
                else                 effDatums.erase("point.origin");
                st.originVisEff = !hasSolidContent;
            } else {
                st.originVisEff = !st.hiddenFeatures.count("point.origin");
            }
            if (pmWantsAxis) {
                for (const auto& id : st.axisDatumIds) effDatums.erase(id);
                for (auto it = effDatums.begin(); it != effDatums.end(); ) {
                    if (it->size() >= 5 && it->compare(0, 5, "axis.") == 0)
                        it = effDatums.erase(it);
                    else
                        ++it;
                }
            } else {
                // Apply per-id auto-hide into effDatums (belt + braces with hiddenFeatures sync).
                auto effAxis = [&](const std::string& id) {
                    if (id.empty()) return;
                    if (st.axesUserToggledIds.count(id)) return; // honor hiddenFeatures
                    const bool isAuto = id.rfind("axis.auto:", 0) == 0;
                    if (!isAuto && st.axesUserToggled) return;
                    if (hasSolidContent) effDatums.insert(id);
                    else                 effDatums.erase(id);
                };
                effAxis("axis.x");
                effAxis("axis.y");
                effAxis("axis.z");
                for (const auto& id : st.axisDatumIds) effAxis(id);
                for (const auto& rd : renderer.refDatums())
                    if (rd.kind == Renderer::RefDatum::Kind::Axis) effAxis(rd.id);
            }
            renderer.setHiddenDatums(std::move(effDatums));
        } else {
            // User took control of origin AND axes, no axis well, no datums: resync to
            // hiddenFeatures so a just-closed axis well doesn't leave force-shown axes drawn.
            renderer.setHiddenDatums(st.hiddenFeatures);
            st.originVisEff = !st.hiddenFeatures.count("point.origin");
        }
        // Planes force-hidden in sketch (grid owns the view). Axes follow the same auto-hide
        // policy as outside sketch: with solid content they stay hidden unless the user unhid
        // them via the FeatureManager eye (axesUserToggled + hiddenFeatures). Empty/sketch-only
        // docs keep axes drawn as a first-sketch reference.
        if (inSketch) { bool none[3] = {false,false,false}; renderer.setPlanes(none, -1); }
        else if (st.sketchPlanePickActive && sketch != nullptr)
            // Plane-pick: the hovered plane (from last frame's overlay hit-test) gets the
            // emphasis brightening; the other two dim. One-frame lag is invisible.
            renderer.setPlanes(effPlane, sketch->planeHover());
        else if (pmWantsPlane && sketch != nullptr)
            // Same one-frame-lagged emphasis, driven by the PM plane picker's hover.
            renderer.setPlanes(effPlane, sketch->pmPlaneHover());
        else          renderer.setPlanes(effPlane, st.planeEmphasis);

        // Orbit-centre override: while sketching, orbit/frame around the SKETCH geometry centre (the
        // body bbox doesn't include sketch entities, so an offset sketch would otherwise spin about
        // the origin). Off elsewhere — center_ (the model bbox centre) is already correct.
        {
            // Drag dead zone: the override tracks the sketch bbox centre, which MOVES
            // while an entity is being dragged — an unfrozen pivot re-translates the camera target
            // every frame, so the view "scrolls" under the drag. Freeze the pivot for the drag's
            // whole duration; on release, fold the pivot delta into pan so the handover to the new
            // bbox centre is seamless (no recenter jump on commit).
            static bool s_skDragPivotLock = false;
            static Vec3 s_skDragPivot{0, 0, 0};
            Vec3 sc;
            if (inSketch && sketch != nullptr && sketch->sketchOrbitCenter(sc)) {
                if (sketch->entityDragActive()) {
                    if (!s_skDragPivotLock) { s_skDragPivotLock = true; s_skDragPivot = sc; }
                    sc = s_skDragPivot;
                } else if (s_skDragPivotLock) {
                    s_skDragPivotLock = false;
                    st.pan[0] += s_skDragPivot.x - sc.x;   // target = pivot + pan stays constant
                    st.pan[1] += s_skDragPivot.y - sc.y;
                    st.pan[2] += s_skDragPivot.z - sc.z;
                }
                renderer.setOrbitCenterOverride(true, sc);
            } else {
                s_skDragPivotLock = false;
                renderer.setOrbitCenterOverride(false, Vec3{0, 0, 0});
            }
        }
        // Emphasize the currently-selected datum (reference axis/plane/point) in the viewport.
        renderer.setSelectedDatumId(st.selectedFeatureId);
        // The datum plane open in the PM draws its normal arrow; cleared once nothing is being edited
        // so committed planes carry no persistent normal handle.
        renderer.setEditingDatumId(
            (st.propertyManager && st.propertyManager->active()) ? st.propertyManager->targetId()
                                                                 : std::string());
        // C25 W2-D: advance any in-flight camera tween (presets / Normal-To / zoom-to-fit) before
        // rendering. Sketch mode hard-locks the camera, so cancel/skip the tween while sketching.
        if (inSketch) cancelCameraAnimation();
        else          tickCameraAnimation(st);
        // SolidWorks-style ghosted edit preview: while a feature's PropertyManager is open and
        // previewing a part (not sketching), the feature currently being created/edited renders
        // translucent while the rest of the model stays opaque — so the in-progress feature reads
        // as a ghost. The owner map gives the exact (body,face) set that feature owns; we hand those
        // keys to the renderer, which draws just those faces in a second translucent pass.
        bool pmPreviewing = st.propertyManager && st.propertyManager->active() &&
                            st.hasDocMesh && !inSketch && !docIsAssembly(st);
        if (pmPreviewing && !st.propertyManager->targetId().empty()) {
            std::vector<uint64_t> editKeys;
            const auto& om = st.docMirror.ownerMap();
            auto oit = om.find(st.propertyManager->targetId());
            if (oit != om.end()) {
                editKeys.reserve(oit->second.faces.size());
                for (const auto& bf : oit->second.faces)
                    editKeys.push_back(((uint64_t)bf.first << 32) | (uint64_t)bf.second);
            }
            renderer.setPreviewIsolation(editKeys, 0.40f);
        } else {
            renderer.clearPreviewIsolation();
        }
        // W2 full-bleed viewport: frame the part in the CLEAR region (clr0..clr1: between the pinned
        // columns, below the doc-tab band) via an asymmetric-frustum lens shift plus a zoom=1 fit
        // scale — NOT by moving pan_, so the published cam_.mvp/halfHeight (and thus the id-pass
        // picks, every overlay, pan/zoom math) stay pixel-exact. zoom=1 (Zoom to Fit, first display)
        // then fits the model's framing sphere into the clear region's shorter side, centred in it,
        // and a hidden/collapsed column falls out automatically (its edge is unpublished).
        // A result legend in the clear rect's bottom-right corner takes its width out of the
        // framing rect (not the published clear rect — overlays/HUD still use the full region),
        // so the fitted model sits beside the legend instead of under it.
        {
            const float fullW = (float)vw, fullH = (float)vh;
            float frameX1 = clr1.x;
            const float legendW = ui::legendReserveWidth();
            if (legendW > 0.0f && (clr1.x - legendW) - clr0.x >= ui::metrics::U(40.0f))
                frameX1 = clr1.x - legendW;
            const float cw = frameX1 - clr0.x, ch = clr1.y - clr0.y;
            const float frameCx = (clr0.x + frameX1) * 0.5f;
            const float offX = 2.0f * (frameCx - (imgX0 + fullW * 0.5f)) / fullW;   // NDC, +right
            const float offY = 2.0f * (clrC.y - (imgY0 + fullH * 0.5f)) / fullH;   // px-down units
            const float fitSide = std::min(cw, ch);
            const float scale = fitSide > 1.0f ? std::clamp(fullH / fitSide, 1.0f, 4.0f) : 1.0f;
            // Shift sign: drawCurrent moves the image by -shift in NDC (NDC y is up, screen y down).
            renderer.setFramingRegion(-offX, offY, scale);
        }
        // Projection mode: live AppState mirror (persisted via Settings::orthographic).
        renderer.setOrthographic(st.orthographic);
        const bool inManufacture =
            st.activeWorkspace == AppState::Workspace::Manufacture;
        // Upload remnant stock before renderOrbit so the renderer's normal shaded pass owns it.
        // Generation is part of the key: frame 3 from a newly simulated job must replace frame 3
        // from the previous generation rather than inheriting its GPU buffers.
        static int s_lastAuxGen = -1;
        static int s_lastAuxFrame = -1;
        if (inManufacture && ui::camSimHudActive()) {
            const int gen = ui::camSimGen();
            const int frame = ui::camCurrentFrameIndex();
            if (frame >= 0 &&
                (gen != s_lastAuxGen || frame != s_lastAuxFrame || !renderer.hasAuxMesh())) {
                if (ui::camFrameMeshFor(frame)) {
                    renderer.setAuxMesh(ui::camFrameMesh(), 0.45f);
                    s_lastAuxGen = gen;
                    s_lastAuxFrame = frame;
                } else {
                    if (renderer.hasAuxMesh()) renderer.clearAuxMesh();
                    s_lastAuxGen = -1;
                    s_lastAuxFrame = -1;
                }
            } else if (frame < 0) {
                if (renderer.hasAuxMesh()) renderer.clearAuxMesh();
                s_lastAuxGen = -1;
                s_lastAuxFrame = -1;
            }
        } else {
            if (renderer.hasAuxMesh()) renderer.clearAuxMesh();
            s_lastAuxGen = -1;
            s_lastAuxFrame = -1;
        }
        // Datum axes are scene-scale ink: keep them inside the clear rect so the dashed lines never
        // cross the text of the translucent glass columns. No clip when nothing is pinned (the clear
        // rect is the whole image, e.g. headless shots with hidden columns or narrow layouts).
        const bool clipDatumAxes = clr0.x > imgX0 || clr1.x < imgX1 || clr0.y > imgY0;
        if (clipDatumAxes)
            renderer.setDatumAxisClip((clr0.x - imgX0) / (float)vw, (clr0.y - imgY0) / (float)vh,
                                      (clr1.x - imgX0) / (float)vw, (clr1.y - imgY0) / (float)vh);
        renderer.renderOrbit(st.yaw, st.pitch, st.zoom, st.pan);
        if (clipDatumAxes) renderer.clearDatumAxisClip();

        // ---- topology picking (C15): id pass + overlay highlights drawn over the shaded view ----
        const bool picking = st.hasDocMesh && st.idPass && st.overlays;
        if (sceneToolArmed) {
            // A simulation line tool owns clicks/box gestures, never navigation drags.
            s_zoomAreaArmed = false;
            s_zoomAreaDragging = false;
        }
        // Refresh lazily at the point of read. render() validates the complete draw key, so this is
        // a no-op for an unchanged hover while frames woken by typing or other panels do no ID-pass
        // work at all. Re-sample section state here because a section interaction may change it
        // after the visual overlay was configured earlier in the frame.
        auto ensurePickFresh = [&](bool facesOnly = false) {
            float pickClipN[3][3] = {{1,0,0},{0,1,0},{0,0,1}};
            float pickClipOff[3] = {0,0,0};
            const int pickClipCount = renderer.sectionClip(pickClipN, pickClipOff);
            st.idPass->setSectionClip(pickClipCount, pickClipN, pickClipOff);
            st.pickReady = st.idPass->render(renderer.camera(), facesOnly);
        };
        if (st.overlays) {
            float clipN[3][3] = {{1,0,0},{0,1,0},{0,0,1}};
            float clipOff[3] = {0,0,0};
            int clipCount = renderer.sectionClip(clipN, clipOff);
            if (picking) st.idPass->setSectionClip(clipCount, clipN, clipOff);
            st.overlays->setSectionClip(clipCount, clipN, clipOff);
            // HiDPI: overlay line/point widths are authored in logical px; the overlays draw into
            // the supersampled FBO, so fold BOTH the supersample factor and the UI scale in.
            // renderScale (adaptive quality) shrinks the FBO below display res — fold
            // that in too so authored widths stay constant in screen px after the upscale.
            st.overlays->setWidthScale((float)renderer.supersample() * renderer.renderScale() *
                                       ui::uiScale);
            // The scripted center-pick consumer runs after this panel, so satisfy that explicit
            // demand here. Normal mouse hover/picks refresh at their read sites below.
            if (picking && st.wantPick) ensurePickFresh();
            // Re-draw hover + selection highlights into the studio color FBO before it's sampled.
            renderer.beginOverlay();
            // C37-C item 4: draw feature-highlight faces first (underneath selection/hover) using
            // a slightly dimmer tint. Empty vector is zero-cost. Stale keys after rebuild are
            // silently skipped inside drawFeatureHighlight. Suppressed while a PM edit preview is
            // ghosting: the translucent feature already marks itself, and an opaque tint on top of
            // the ghost would read muddy.
            if (picking && !st.featureHighlightFaces.empty() && !pmPreviewing) {
                if (st.activeWorkspace == AppState::Workspace::Simulate && st.simManager &&
                    st.simManager->motionCollisionHighlightActive()) {
                    const float collisionColor[4] = {
                        ui::palette.statusErr[0], ui::palette.statusErr[1],
                        ui::palette.statusErr[2], 0.45f};
                    st.overlays->drawFaceWash(renderer.camera(), st.featureHighlightFaces,
                                             collisionColor);
                } else {
                    st.overlays->drawFeatureHighlight(renderer.camera(), st.featureHighlightFaces);
                }
            }
            // Build the simulation CPU batches first: collectFlowResultLines may lazily rebuild its
            // cached stream/slice geometry and advance overlayGeneration(). Capturing the generation
            // afterwards ensures this frame's persistent GL buffers are keyed to the data drawn.
            const bool inSimulation =
                st.activeWorkspace == AppState::Workspace::Simulate && st.simManager;
            const bool simPreview = inSimulation && st.simManager->previewActive(st);
            const bool flowSetup = inSimulation && st.simManager->flowSetupActive(st);
            const bool flowResult = inSimulation && st.simManager->flowResultLinesActive(st);
            std::vector<uint64_t> fix, load;
            std::vector<uint64_t> topoPreserve, topoExclude, topoPorts;
            std::vector<float> topoPreserveBox, topoExcludeBox;
            std::vector<float> loadSegs, fixSegs, loadTris, fixTris;
            std::vector<float> arrowSegs, inletSegs, arrowTris;
            std::vector<ui::SimManager::FlowLineBatch> flowBatches;
            bool meshPreview = false;
            if (simPreview) {
                st.simManager->collectFixtureFaces(fix);
                st.simManager->collectLoadFaces(load);
                st.simManager->collectTopologyPreview(topoPreserve, topoExclude, topoPorts,
                                                      topoPreserveBox, topoExcludeBox);
                st.simManager->collectIndicatorSegments(st, loadSegs, fixSegs,
                                                        &loadTris, &fixTris);
                meshPreview = st.simManager->hasMeshPreview(st);
            }
            if (flowSetup)
                st.simManager->collectFlowSetup(st, arrowSegs, inletSegs, &arrowTris);
            if (flowResult) st.simManager->collectFlowResultLines(st, flowBatches);

            const bool hasSimulationFrame = simPreview || flowSetup || flowResult || sceneActive;
            uint64_t simulationGeneration = 0;
            if (hasSimulationFrame) {
                simulationGeneration = st.simManager ? st.simManager->overlayGeneration() : 0;
                // Scope the GL cache to the active worker document as well as its manager
                // generation. This prevents equal generation values in different tabs from
                // retaining one another's mesh/result buffers.
                st.overlays->beginSimulationFrame(simulationGeneration, sceneDocumentScope);
            } else {
                st.overlays->clearSimulationBuffers();
            }

            constexpr uint64_t kSimLoadSegments = 1;
            constexpr uint64_t kSimFixtureSegments = 2;
            constexpr uint64_t kSimLoadTriangles = 3;
            constexpr uint64_t kSimFixtureTriangles = 4;
            constexpr uint64_t kSimMeshSegments = 5;
            constexpr uint64_t kSimFlowInletSegments = 7;
            constexpr uint64_t kSimFlowArrowSegments = 8;
            constexpr uint64_t kSimFlowArrowTriangles = 9;
            constexpr uint64_t kSimFlowResultBase = 0x100;
            constexpr uint64_t kSimTopoPreserveBox = 10;
            constexpr uint64_t kSimTopoExcludeBox = 11;

            // Simulation pre-run previews: paint the selected study's fixture faces green and its
            // load faces red so the setup reads at a glance before solving (gated off while a result
            // colormap is showing so the tints don't muddy the stress plot).
            if (simPreview) {
                const auto& vpal = ui::palette;
                if (!fix.empty())  st.overlays->drawFaceWash(renderer.camera(), fix, vpal.simFixture);
                if (!load.empty()) st.overlays->drawFaceWash(renderer.camera(), load, vpal.simLoad);
                // Topology keep-in (accent) / keep-out (domain slate) / flow-port (inlet teal)
                // washes, drawn translucent from existing palette roles.
                auto washOf = [](const float role[4], float alpha, float out[4]) {
                    out[0] = role[0]; out[1] = role[1]; out[2] = role[2]; out[3] = alpha;
                };
                float preserveWash[4], excludeWash[4], portWash[4];
                washOf(vpal.accent, 0.45f, preserveWash);
                washOf(vpal.flowDomain, 0.60f, excludeWash);
                washOf(vpal.flowInlet, 0.45f, portWash);
                if (!topoPreserve.empty())
                    st.overlays->drawFaceWash(renderer.camera(), topoPreserve, preserveWash);
                if (!topoExclude.empty())
                    st.overlays->drawFaceWash(renderer.camera(), topoExclude, excludeWash);
                if (!topoPorts.empty())
                    st.overlays->drawFaceWash(renderer.camera(), topoPorts, portWash);
                if (!topoPreserveBox.empty())
                    st.overlays->drawSimulationSegments(renderer.camera(), kSimTopoPreserveBox,
                                                        topoPreserveBox, vpal.accent, 2.0f);
                if (!topoExcludeBox.empty())
                    st.overlays->drawSimulationSegments(renderer.camera(), kSimTopoExcludeBox,
                                                        topoExcludeBox, vpal.flowDomain, 2.0f);
                // On-demand indicator batches use content fingerprints because SimManager's
                // generation intentionally covers only its retained mesh/flow geometry.
                if (!loadSegs.empty())
                    st.overlays->drawSimulationSegments(renderer.camera(), kSimLoadSegments,
                                                        loadSegs, vpal.simArrow, 2.5f);
                if (!fixSegs.empty())
                    st.overlays->drawSimulationSegments(renderer.camera(), kSimFixtureSegments,
                                                        fixSegs, vpal.simGlyph, 2.5f);
                if (!loadTris.empty())
                    st.overlays->drawSimulationTriangles(renderer.camera(), kSimLoadTriangles,
                                                         loadTris, vpal.simArrow);
                if (!fixTris.empty())
                    st.overlays->drawSimulationTriangles(renderer.camera(), kSimFixtureTriangles,
                                                         fixTris, vpal.simGlyph);
                if (meshPreview)
                    st.overlays->drawSimulationSegments(
                        renderer.camera(), kSimMeshSegments,
                        st.simManager->meshPreviewSegments(), vpal.simMesh, 1.0f, true,
                        simulationGeneration);
            }
            // Flow-study setup preview: quiet incoming-flow arrows and inlet/outlet outlines.
            // Scissor scene-scale annotations to the clear region, away from the glass panes.
            if (flowSetup) {
                const auto& vpal = ui::palette;
                float inletCol[4], arrowCol[4];
                for (int k = 0; k < 4; ++k) {
                    inletCol[k] = vpal.flowInlet[k];
                    arrowCol[k] = vpal.flowArrow[k];
                }
                inletCol[3] *= 0.55f;
                arrowCol[3] *= 0.70f;
                st.overlays->beginScreenClip(renderer.camera(),
                                             (clr0.x - imgX0) / (float)vw,
                                             (clr0.y - imgY0) / (float)vh,
                                             (clr1.x - imgX0) / (float)vw,
                                             (clr1.y - imgY0) / (float)vh);
                if (!inletSegs.empty())
                    st.overlays->drawSimulationSegments(renderer.camera(),
                                                        kSimFlowInletSegments, inletSegs,
                                                        inletCol, 1.0f);
                if (!arrowSegs.empty())
                    st.overlays->drawSimulationSegments(renderer.camera(),
                                                        kSimFlowArrowSegments, arrowSegs,
                                                        arrowCol, 1.5f);
                if (!arrowTris.empty())
                    st.overlays->drawSimulationTriangles(renderer.camera(),
                                                         kSimFlowArrowTriangles, arrowTris,
                                                         arrowCol);
                st.overlays->endScreenClip();
            }
            // Flow-study result: speed-colored streamlines + velocity slices drawn over the
            // Cp-colored model. Stable logical batch keys plus overlayGeneration avoid both
            // per-frame uploads and payload scans for these potentially large retained vectors.
            if (flowResult) {
                st.overlays->beginScreenClip(renderer.camera(),
                                             (clr0.x - imgX0) / (float)vw,
                                             (clr0.y - imgY0) / (float)vh,
                                             (clr1.x - imgX0) / (float)vw,
                                             (clr1.y - imgY0) / (float)vh);
                for (size_t i = 0; i < flowBatches.size(); ++i) {
                    const auto& fb = flowBatches[i];
                    if (fb.segs && !fb.segs->empty())
                        st.overlays->drawSimulationSegments(
                            renderer.camera(), kSimFlowResultBase + (uint64_t)i,
                            *fb.segs, fb.color, 2.0f, true, simulationGeneration);
                }
                st.overlays->endScreenClip();
            }
            if (sceneActive) {
                for (const auto& batch : simulationScene.lit) {
                    if (batch.xyz && batch.nrm)
                        st.overlays->drawSimulationLitTriangles(
                            renderer.camera(), batch.key, *batch.xyz, *batch.nrm, batch.rgb,
                            batch.rgba, batch.revision);
                }
                // Clip annotations, not shaded pipe/component geometry, to the clear viewport.
                st.overlays->beginScreenClip(renderer.camera(),
                                             (clr0.x - imgX0) / (float)vw,
                                             (clr0.y - imgY0) / (float)vh,
                                             (clr1.x - imgX0) / (float)vw,
                                             (clr1.y - imgY0) / (float)vh);
                for (const auto& batch : simulationScene.lines) {
                    if (batch.xyz)
                        st.overlays->drawSimulationSegments(
                            renderer.camera(), batch.key, *batch.xyz, batch.rgba, batch.width,
                            batch.overSurface, batch.revision);
                }
                st.overlays->endScreenClip();
            }
            if (hasSimulationFrame) st.overlays->endSimulationFrame();
            // CAM geometry is a Manufacture-only native overlay. Path batches stay in their
            // original CPU buffers: drawCamSegments uploads each class once, then selects the
            // completed prefix and pending suffix with GL ranges using the end-time array.
            if (picking && inManufacture) {
                if (const ui::CamBackplot* bp = ui::camActiveBackplot()) {
                    const auto& vpal = ui::palette;
                    const bool progressing = ui::camSimHudActive();
                    const double playTime = progressing ? ui::camPlayT() : 0.0;
                    auto drawClass = [&](const std::vector<float>& segments,
                                         const std::vector<float>& endTimes,
                                         const std::vector<float>& directions,
                                         const float color[4], float width) {
                        if (!segments.empty())
                            st.overlays->drawCamSegments(renderer.camera(), segments, endTimes,
                                                         playTime, progressing, color, width);
                        if (!directions.empty())
                            st.overlays->drawTriangles(renderer.camera(), directions, color, false);
                    };
                    drawClass(bp->rapid,  bp->rapidT,  bp->rapidDir,  vpal.camRapid,  1.0f);
                    drawClass(bp->lead,   bp->leadT,   bp->leadDir,   vpal.camLead,   1.6f);
                    drawClass(bp->plunge, bp->plungeT, bp->plungeDir, vpal.camPlunge, 1.6f);
                    drawClass(bp->cut,    bp->cutT,    bp->cutDir,    vpal.camCut,    1.8f);
                    drawClass(bp->arc,    bp->arcT,    bp->arcDir,    vpal.camArc,    1.8f);

                    if (progressing) {
                        float toolPosition[3];
                        if (ui::camToolPoseAt(playTime, toolPosition))
                            st.overlays->drawCamToolMarker(renderer.camera(), toolPosition,
                                                           vpal.camTool);
                    }
                }
                if (const std::vector<float>* stock = ui::camStockBoxSegments())
                    st.overlays->drawSegments(renderer.camera(), *stock, ui::palette.camStock,
                                              1.2f, false);
            }
            // Evaluate Measure / Mass Properties markers (distance lines, COM cross, I/J axes).
            if (picking && st.evaluateDialogs) {
                st.evaluateDialogs->prepareFrame(st);
                const auto& es = st.evaluateDialogs->overlaySegments();
                if (!es.empty()) {
                    const auto& pal = ui::palette;
                    const float accent[4] = {pal.accent[0], pal.accent[1], pal.accent[2], 0.95f};
                    st.overlays->drawSegments(renderer.camera(), es, accent, 2.2f, true);
                }
                // Per-ref Measure highlights: face wash / edge polyline / vertex cross.
                for (const auto& hl : st.evaluateDialogs->overlayHighlights()) {
                    if (hl.kind == 1 /*Face*/ && hl.faceKey != 0) {
                        std::vector<uint64_t> keys = {hl.faceKey};
                        st.overlays->drawFaceWash(renderer.camera(), keys, hl.rgba);
                    } else if (hl.kind == 2 /*Edge*/ && hl.polyline.size() >= 6) {
                        // Consecutive polyline samples -> segment list.
                        std::vector<float> segs;
                        const size_t nPts = hl.polyline.size() / 3;
                        segs.reserve((nPts > 1 ? nPts - 1 : 0) * 6);
                        for (size_t i = 0; i + 1 < nPts; ++i) {
                            segs.push_back(hl.polyline[i * 3 + 0]);
                            segs.push_back(hl.polyline[i * 3 + 1]);
                            segs.push_back(hl.polyline[i * 3 + 2]);
                            segs.push_back(hl.polyline[(i + 1) * 3 + 0]);
                            segs.push_back(hl.polyline[(i + 1) * 3 + 1]);
                            segs.push_back(hl.polyline[(i + 1) * 3 + 2]);
                        }
                        if (!segs.empty())
                            st.overlays->drawSegments(renderer.camera(), segs, hl.rgba, 2.4f, true);
                    } else if (hl.kind == 3 /*Vertex*/ ||
                               (hl.kind != 1 && hl.kind != 2 &&
                                (hl.point[0] != 0.f || hl.point[1] != 0.f || hl.point[2] != 0.f))) {
                        // Small cross at the vertex / anchor point.
                        const float s = 1.5f;
                        std::vector<float> cross = {
                            hl.point[0] - s, hl.point[1], hl.point[2],
                            hl.point[0] + s, hl.point[1], hl.point[2],
                            hl.point[0], hl.point[1] - s, hl.point[2],
                            hl.point[0], hl.point[1] + s, hl.point[2],
                            hl.point[0], hl.point[1], hl.point[2] - s,
                            hl.point[0], hl.point[1], hl.point[2] + s,
                        };
                        st.overlays->drawSegments(renderer.camera(), cross, hl.rgba, 2.4f, true);
                    } else if (!hl.polyline.empty() && hl.polyline.size() >= 6) {
                        // Enrich-only edge without kind.
                        std::vector<float> segs;
                        const size_t nPts = hl.polyline.size() / 3;
                        segs.reserve((nPts > 1 ? nPts - 1 : 0) * 6);
                        for (size_t i = 0; i + 1 < nPts; ++i) {
                            segs.push_back(hl.polyline[i * 3 + 0]);
                            segs.push_back(hl.polyline[i * 3 + 1]);
                            segs.push_back(hl.polyline[i * 3 + 2]);
                            segs.push_back(hl.polyline[(i + 1) * 3 + 0]);
                            segs.push_back(hl.polyline[(i + 1) * 3 + 1]);
                            segs.push_back(hl.polyline[(i + 1) * 3 + 2]);
                        }
                        if (!segs.empty())
                            st.overlays->drawSegments(renderer.camera(), segs, hl.rgba, 2.4f, true);
                    }
                }
                // Interference view: body washes + overlap triangle meshes.
                const auto& iv = st.evaluateDialogs->interferenceView();
                if (iv.active) {
                    for (const auto& pr : iv.pairs) {
                        if (pr.bodyA > 0)
                            st.overlays->drawBodyWash(renderer.camera(), (uint32_t)pr.bodyA,
                                                     pr.rgbaA);
                        if (pr.bodyB > 0)
                            st.overlays->drawBodyWash(renderer.camera(), (uint32_t)pr.bodyB,
                                                     pr.rgbaB);
                        if (pr.overlapTris.size() >= 9)
                            st.overlays->drawTriangles(renderer.camera(), pr.overlapTris,
                                                      pr.overlapRgba, true);
                    }
                }
                // Section Props: translucent cutting quads + normal arrows (world-space).
                // Handles / labels / drag live in the screen overlay pass below.
                if (st.evaluateDialogs->sectionDragActive()) {
                    for (const auto& pl : st.evaluateDialogs->sectionPlanes()) {
                        if (!pl.active) continue;
                        const float hs = pl.halfSize;
                        const float ox = pl.origin[0], oy = pl.origin[1], oz = pl.origin[2];
                        const float ux = pl.uAxis[0] * hs, uy = pl.uAxis[1] * hs,
                                    uz = pl.uAxis[2] * hs;
                        const float vx = pl.vAxis[0] * hs, vy = pl.vAxis[1] * hs,
                                    vz = pl.vAxis[2] * hs;
                        // Corners: origin ± u*hs ± v*hs (sign combos).
                        const float c00[3] = {ox - ux - vx, oy - uy - vy, oz - uz - vz};
                        const float c10[3] = {ox + ux - vx, oy + uy - vy, oz + uz - vz};
                        const float c11[3] = {ox + ux + vx, oy + uy + vy, oz + uz + vz};
                        const float c01[3] = {ox - ux + vx, oy - uy + vy, oz - uz + vz};
                        std::vector<float> tris = {
                            c00[0], c00[1], c00[2], c10[0], c10[1], c10[2], c11[0], c11[1], c11[2],
                            c00[0], c00[1], c00[2], c11[0], c11[1], c11[2], c01[0], c01[1], c01[2],
                        };
                        float planeRgba[4] = {pl.rgba[0], pl.rgba[1], pl.rgba[2],
                                             pl.rgba[3] > 0.01f ? pl.rgba[3] * 0.35f : 0.28f};
                        st.overlays->drawTriangles(renderer.camera(), tris, planeRgba, true);

                        // Normal arrow shaft + short 2-segment arrowhead.
                        const float L = hs * 0.8f;
                        const float tipX = ox + pl.normal[0] * L;
                        const float tipY = oy + pl.normal[1] * L;
                        const float tipZ = oz + pl.normal[2] * L;
                        const float hLen = L * 0.18f;
                        const float hW = hLen * 0.55f;
                        // Head base sits back along -normal; barbs use ±uAxis for a simple V.
                        const float bx = tipX - pl.normal[0] * hLen;
                        const float by = tipY - pl.normal[1] * hLen;
                        const float bz = tipZ - pl.normal[2] * hLen;
                        std::vector<float> arrow = {
                            ox, oy, oz, tipX, tipY, tipZ,
                            tipX, tipY, tipZ,
                            bx + pl.uAxis[0] * hW, by + pl.uAxis[1] * hW, bz + pl.uAxis[2] * hW,
                            tipX, tipY, tipZ,
                            bx - pl.uAxis[0] * hW, by - pl.uAxis[1] * hW, bz - pl.uAxis[2] * hW,
                        };
                        st.overlays->drawSegments(renderer.camera(), arrow, pl.rgba, 2.4f, true);
                    }
                }
                // Whole-body translucency while mass markers / section / interference are active.
                if (st.renderer)
                    st.renderer->setBodyAlpha(st.evaluateDialogs->requestedBodyAlpha());
            }
            if (picking) {
                std::vector<render::PickHit> selHits;
                selHits.reserve(st.selection.size());
                for (const auto& it : st.selection.items()) selHits.push_back(it.hit);
                // Highlight every pick in the active edge/face well, including when an edit
                // preview fails. Stale topology after rebuilding is skipped by the overlay.
                if (st.propertyManager && st.propertyManager->active()) {
                    std::vector<std::array<uint32_t, 3>> wellHl;
                    st.propertyManager->collectActiveWellHighlights(wellHl);
                    for (const auto& h : wellHl) {
                        render::PickHit ph;
                        ph.body_id = h[0]; ph.topo_id = h[1]; ph.kind = (render::PickKind)h[2];
                        selHits.push_back(ph);
                    }
                }
                // Pane face-well chips claim hover without changing the viewport pick.
                const bool uiHoverLive = s_uiHoverFrame >= ImGui::GetFrameCount() - 1;
                st.overlays->draw(renderer.camera(), uiHoverLive ? s_uiHoverFace : st.hover, selHits);
            }
            renderer.endOverlay();
        }

        // W2 full-bleed viewport: the shaded 3D is composited on the BACKGROUND draw list so it sits
        // BEHIND every panel — full window WIDTH (visible behind the translucent FeatureManager) and
        // full window HEIGHT (the doc-tab strip floats over live 3D). vr0/vr1 is this image's exact
        // screen rect; ppProject, the id-pick mouse->pixel mapping, and every projected overlay read
        // it, so they stay pixel-exact (the lens shift lives in cam_.mvp; the rect is the NDC map).
        ImVec2 vr0(mainVp->Pos.x, winTopY);   // window top: the 3D runs under the doc-tab strip
        ImVec2 vr1(vr0.x + (float)vw, vr0.y + (float)vh);  // == vr0 + (vw,vh) so it matches ppProject + the FBO exactly
        ImGui::GetBackgroundDrawList()->AddImage(
            (ImTextureID)(intptr_t)renderer.resolvedColorTexture(), vr0, vr1,
            ImVec2(0, 1), ImVec2(1, 0));
        // ---- Liquid-glass v2 (H6a): publish the Kawase-blurred backdrop with THIS image's
        // screen rect so ui::glassPanel can project any panel rect into backdrop UVs. uv0=(0,1),
        // uv1=(1,0) above (GL bottom-left texture) is the same orientation ViewportBackdropInfo's
        // flipV=true assumes, so flipV=true here matches the AddImage call verbatim.
        renderer.setBackdropEnabled(true);   // frost from the studio bg even with no mesh
        {
            const auto bd = renderer.backdrop();
            ui::ViewportBackdropInfo bdInfo;
            bdInfo.textureId = bd.textureId;
            bdInfo.uvMin = ImVec2(bd.uvMin[0], bd.uvMin[1]);
            bdInfo.uvMax = ImVec2(bd.uvMax[0], bd.uvMax[1]);
            bdInfo.screenMin = vr0;
            bdInfo.screenMax = vr1;
            bdInfo.flipV = true;
            ui::setViewportBackdrop(bdInfo);
            s_bdMin = vr0;
            s_bdMax = vr1;
            s_bdLive = bdInfo.valid();
        }
        // (The dock-tab band that used to paint here was removed: drawDocTabs() paints one glass
        // band for the whole tab row; the 3D image now starts at the window TOP, so the strip's
        // glass projects live 3D instead of a dead panel fill.)
        // Input surface: an invisible button over the visible 3D (the content rect). It carries the
        // hover/active state the navigation, picking, and drag-drop target all read (imgHovered),
        // exactly as the old ImGui::Image item did — minus the opaque quad that would hide the 3D.
        ImGui::SetCursorScreenPos(cr0);
        ImGui::InvisibleButton("##viewport3d",
            ImVec2(avail.x > 1.0f ? avail.x : 1.0f, avail.y > 1.0f ? avail.y : 1.0f));
        // Hover MUST be sampled here, immediately after the input surface and BEFORE any
        // overlay below draws. Overlays that submit their own ImGui items (drawTabLoadingOverlay
        // ends in ui::busyLabel — a Dummy + TextUnformatted) would otherwise become "the last
        // item", so a later IsItemHovered() reported the hover state of a centered "Loading…"
        // label instead of the viewport. That silently killed EVERY navigation and picking path
        // gated on imgHovered (orbit, pan, zoom, selection) for as long as the scrim was up —
        // the user could watch the AI build the part but could not move the camera.
#ifdef __EMSCRIPTEN__
        const bool touchOwnsNavigation = wasmViewportTouches(
            st, renderer, cr0, cr1, vh, !inSketch && !inSketch3d);
        const bool imgHovered = ImGui::IsItemHovered() && !touchOwnsNavigation;
        const bool imgMouseOver =
            ImGui::IsItemHovered(ImGuiHoveredFlags_NoNavOverride) && !touchOwnsNavigation;
#else
        const bool imgHovered = ImGui::IsItemHovered();
        // Enter/Backspace/Tab are ImGui nav keys: pressing one hides mouse hover until the mouse
        // moves, so a pipe tool finishing with Enter would see "not hovered". Scene tools read the
        // real pointer state instead.
        const bool imgMouseOver = ImGui::IsItemHovered(ImGuiHoveredFlags_NoNavOverride);
#endif
        // Capture the actual press face before hover can move into an overlay column. Left-drag
        // on geometry in Simulate copies a face; blank-space left-drag and MMB keep navigation.
        const bool simGestures = st.activeWorkspace == AppState::Workspace::Simulate &&
            st.simManager && picking && !sceneToolArmed && !inSketch && !inSketch3d &&
            !st.sketchPlanePickActive &&
            !(st.propertyManager && st.propertyManager->active()) && !s_zoomAreaArmed &&
            !s_viewSelector.active() && !tabModelLoading(st) && !st.colormapOn &&
            !st.rpc.interactionBusy() && !st.simManager->runUi().running &&
            ImGui::GetTopMostPopupModal() == nullptr;
        if (!simGestures || s_simFaceDrag.docId != st.docMirror.docId() ||
            s_simFaceDrag.revision != static_cast<long>(st.docMirror.revision()) ||
            (!sceneToolArmed && ImGui::IsKeyPressed(ImGuiKey_Escape)))
            s_simFaceDrag = {};
        if (simGestures && imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
            !io.KeyCtrl && !io.KeyShift && !io.KeyAlt && st.vpDragMode == 0) {
            s_simFaceDrag = {};
            ensurePickFresh(true);
            const auto face = st.idPass->pickFinal(static_cast<int>(io.MousePos.x - vr0.x),
                static_cast<int>(io.MousePos.y - vr0.y), vw, vh);
            if (face.valid() && face.kind == render::PickKind::Face) {
                std::string error;
                const std::string resolved = st.docMirror.resolvePick(st.worker, face.body_id,
                    "face", face.topo_id, error);
                auto selector = nlohmann::json::parse(resolved, nullptr, false);
                if (ui::sim::validFace(selector, face.body_id, face.topo_id)) {
                    s_simFaceDrag.docId = st.docMirror.docId();
                    s_simFaceDrag.revision = static_cast<long>(st.docMirror.revision());
                    s_simFaceDrag.selector = std::move(selector);
                    s_simFaceDrag.hit = face;
                }
            }
        }
        // Drop TARGET (Wave 3A item 5): dropping an in-app Explorer file row onto the viewport opens
        // it. The payload is the dragged file's absolute path (NUL-terminated); we hand it to the
        // GL-thread request pump via st.requestOpenPath, which routes CAD vs ECAD by extension — the
        // same safe deferred-open path the Explorer double-click and File menu use. (OS-level file
        // drops from Windows Explorer are handled separately by the GLFW drop callback in app.cpp.)
        // Bound to the viewport button for the same reason imgHovered is sampled above:
        // BeginDragDropTarget() targets the LAST submitted item, so it has to run before the
        // overlays below submit theirs.
        if (ImGui::BeginDragDropTarget()) {
            if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload(ui::kFileDragPayload)) {
                if (pl->Data && pl->DataSize > 0)
                    st.requestOpenPath = std::string(static_cast<const char*>(pl->Data));
            }
            if (simGestures) {
                if (const ImGuiPayload* pl = ImGui::AcceptDragDropPayload(ui::sim::kSetupToolDragPayload)) {
                    if (pl->IsDelivery() && ui::sim::setupToolDragDocument() == st.docMirror.docId() &&
                        pl->Data && pl->DataSize > 1 &&
                        static_cast<const char*>(pl->Data)[pl->DataSize - 1] == '\0' &&
                        io.MousePos.x >= cr0.x && io.MousePos.x < cr1.x &&
                        io.MousePos.y >= cr0.y && io.MousePos.y < cr1.y) {
                        // Setup tools consume the real triangle face at the exact drop pixel,
                        // not a coincident edge/vertex painted over it for ordinary selection.
                        ensurePickFresh(true);
                        const auto face = st.idPass->pickFinal(
                            static_cast<int>(io.MousePos.x - vr0.x),
                            static_cast<int>(io.MousePos.y - vr0.y), vw, vh);
                        if (face.valid() && face.kind == render::PickKind::Face) {
                            std::string error;
                            auto selector = nlohmann::json::parse(st.docMirror.resolvePick(
                                st.worker, face.body_id, "face", face.topo_id, error), nullptr, false);
                            if (ui::sim::validFace(selector, face.body_id, face.topo_id))
                                st.simManager->routeSetupDrop(st,
                                    std::string(static_cast<const char*>(pl->Data), pl->DataSize - 1),
                                    selector, face.body_id, face.topo_id);
                        }
                    }
                }
            }
            ImGui::EndDragDropTarget();
        }
        if (simGestures && s_simFaceDrag.hit.valid())
            ui::sim::faceDragSource(s_simFaceDrag.docId, s_simFaceDrag.selector,
                s_simFaceDrag.hit.body_id, s_simFaceDrag.hit.topo_id, "Model face");
        const bool simFaceOwnsDrag = simGestures && s_simFaceDrag.hit.valid() &&
                                     ImGui::IsMouseDown(ImGuiMouseButton_Left);
        if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) s_simFaceDrag = {};
        // Tab-switch loading state: while the ACTIVE part/asm tab's async activation + mesh load
        // are still on the way, frost the wiped scene and center a brand mark + "Loading <tab>…"
        // so the click reads as registered. Painted before the HUD chrome so status pills (busy,
        // AI badge) stay on top of the scrim; self-clears when the mesh commit lands.
        if (tabModelLoading(st)) drawTabLoadingOverlay(st, vr0, vr1, cr0, cr1);
        // Task 02: while the in-app copilot is driving the doc, paint an "AI editing" badge over the
        // view so the live edits are clearly attributed to the AI (not the user). Same run-lifecycle
        // signal as the Activity panel; non-intrusive top-center pill.
        if (st.agentV2Running) drawAiActivityBadge(cr0, cr1, st.agentV2Status);
        // Subtle quality readout while the adaptive ladder is holding a reduced step
        // (hidden at FULL or when adaptive is off — native default). Bottom-right, dim text.
        if (renderer.adaptiveQuality() && renderer.qualityStep() != Renderer::QualityStep::Full) {
            char qbuf[64];
            std::snprintf(qbuf, sizeof(qbuf), "%s \xC2\xB7 %.0f ms",
                          renderer.qualityStepName(), renderer.frameMsEma());
            ImDrawList* qdl = ImGui::GetWindowDrawList();
            const ImVec2 qts = ImGui::CalcTextSize(qbuf);
            const float qS = ui::uiScale;
            qdl->AddText(ImVec2(vr1.x - qts.x - 12.0f * qS, vr1.y - qts.y - 8.0f * qS),
                         withAlpha(ui::palette.textDim, 0.65f), qbuf);
        }
        // Manufacture owns every CAM screen-space overlay just as it owns the native geometry.
        if (inManufacture && ui::camSimHudActive())
            ui::camDrawViewportHud(st, vr0, ImVec2(vr1.x - vr0.x, vr1.y - vr0.y));
        // Simulate > Hydraulics (hook): the panel's Schematic | Plots | 3D toggle over the 3D view.
        if (st.activeWorkspace == AppState::Workspace::Simulate && st.simManager)
            st.simManager->drawViewportHud(st);
        // C25 W2-D: camera navigation. All of it is suppressed while sketching (camera is
        // plane-locked) except the wheel zoom, which stays usable. Any drag-navigation cancels an
        // in-flight tween so the user takes over instantly.
        if (imgHovered && !inSketch && !inSketch3d) {
            // Pixels -> world units at the orbit target plane via the published camera seam
            // (Renderer::worldPerPixel / viewHalfHeight). Works for both ortho and perspective.
            float worldPerPx = renderer.worldPerPixel(vh);
            const auto& cam = renderer.camera();
            Vec3 R = cam.right;   // screen-right in world space
            Vec3 U = cam.up;     // screen-up in world space

            // Lenient pan clamp (see kClampPan/kPanLeniency). pan is the world-space offset of the
            // look-at target from the model center, so |pan| is exactly how far the model center
            // sits from screen-center. Cap it at one view-half (so the model edge reaches the
            // viewport border) plus kPanLeniency model-radii (so any surface point can pass center).
            auto clampPan = [&]() {
                if (!kClampPan) return;
                const float sceneR = renderer.modelRadius();
                if (sceneR <= 1e-5f) return;
                // Live half-height scales with 1/zoom; recompute from current zoom so wheel-zoom
                // then pan still clamps correctly (cam_.halfHeight is from the last drawn frame).
                float zl = st.zoom < 0.05f ? 0.05f : st.zoom;
                float zDrawn = cam.zoom > 1e-5f ? cam.zoom : 1.0f;
                const float viewHalf = cam.halfHeight * (zDrawn / zl);
                const float maxPan   = viewHalf + sceneR * kPanLeniency;
                float pl = std::sqrt(st.pan[0]*st.pan[0] + st.pan[1]*st.pan[1] + st.pan[2]*st.pan[2]);
                if (pl > maxPan && pl > 1e-6f) {
                    float k = maxPan / pl;
                    st.pan[0] *= k; st.pan[1] *= k; st.pan[2] *= k;
                }
            };
            auto applyPan = [&](float ddx, float ddy) {
                // Drag right (ddx>0) moves the model right -> target shifts left (-R). Drag down
                // (ddy>0, screen y is down) moves the model down -> target shifts up (+U).
                st.pan[0] += (-R.x * ddx + U.x * ddy) * worldPerPx;
                st.pan[1] += (-R.y * ddx + U.y * ddy) * worldPerPx;
                st.pan[2] += (-R.z * ddx + U.z * ddy) * worldPerPx;
                clampPan();
            };
            const bool ctrl = io.KeyCtrl, shift = io.KeyShift;

            // C25 W3-J: apply display style from AppState each frame (cheap no-op when unchanged).
            renderer.setDisplayStyle(static_cast<DisplayStyle>(st.displayStyle));

            // C25 W3-J: zoom-area rubber-band drag.
            if (s_zoomAreaArmed) {
                if (ImGui::IsMouseDragging(ImGuiMouseButton_Left)) {
                    if (!s_zoomAreaDragging) {
                        s_zoomAreaDragging = true;
                        s_zoomAreaP0 = io.MouseClickedPos[ImGuiMouseButton_Left];
                    }
                    s_zoomAreaP1 = io.MousePos;
                } else if (s_zoomAreaDragging &&
                           !ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                    // Released: compute zoom+pan to frame the rect.
                    float rx0 = s_zoomAreaP0.x < s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                    float ry0 = s_zoomAreaP0.y < s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                    float rx1 = s_zoomAreaP0.x > s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                    float ry1 = s_zoomAreaP0.y > s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                    float rw = rx1 - rx0, rh = ry1 - ry0;
                    if (rw > 4 && rh > 4) {
                        // Center of the rect relative to the framed centre (the clear region's
                        // centre, where the lens shift puts the orbit target).
                        float vcx = clrC.x, vcy = clrC.y;
                        float rcx = (rx0 + rx1) * 0.5f - vcx;
                        float rcy = (ry0 + ry1) * 0.5f - vcy;
                        // Pan shift: move target so rect center lands at view center.
                        // rcx>0 = rect center is right of view center: shift target rightward (+R).
                        // rcy>0 = rect center is below view center (screen Y down): shift target downward (-U).
                        float k = worldPerPx;
                        st.pan[0] += (R.x * rcx - U.x * rcy) * k;
                        st.pan[1] += (R.y * rcx - U.y * rcy) * k;
                        st.pan[2] += (R.z * rcx - U.z * rcy) * k;
                        // Zoom: scale so rect extent maps to ~90% of the clear region.
                        float vExt = std::min(clr1.x - clr0.x, clr1.y - clr0.y) * 0.9f;
                        float rExt = rw < rh ? rw : rh;
                        if (rExt > 0.0f) {
                            st.zoom = std::clamp(st.zoom * vExt / rExt, 0.1f,
                                                 renderer.maxSceneZoom());
                        }
                        float newPan[3] = {st.pan[0], st.pan[1], st.pan[2]};
                        requestCameraAnimation(st, st.yaw, st.pitch, st.zoom, newPan);
                    }
                    s_zoomAreaArmed = false;
                    s_zoomAreaDragging = false;
                }
                // Esc or second click disarms.
                if (ImGui::IsKeyPressed(ImGuiKey_Escape) ||
                    ImGui::IsMouseClicked(ImGuiMouseButton_Right)) {
                    s_zoomAreaArmed = false;
                    s_zoomAreaDragging = false;
                }
            }

            // C37-C item 3: LMB drag threshold — track press origin and only commit to drag
            // once 4px * uiScale has been exceeded, so click-selects don't micro-orbit.
            if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                s_lmbDragOrigin = io.MousePos;
                s_lmbDragActive = false;
            }
            if (!s_lmbDragActive && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                float ddx = io.MousePos.x - s_lmbDragOrigin.x;
                float ddy = io.MousePos.y - s_lmbDragOrigin.y;
                const float kThresh = 4.0f * ui::uiScale;
                if (std::sqrt(ddx * ddx + ddy * ddy) >= kThresh)
                    s_lmbDragActive = true;
            }
            if (!ImGui::IsMouseDown(ImGuiMouseButton_Left))
                s_lmbDragActive = false;

            // C37-C: crosshair cursor while zoom-area is armed.
            if (s_zoomAreaArmed)
                st.requestCursor(ui::CursorKind::Crosshair);

            bool ppPmOpen = st.propertyManager && st.propertyManager->active();

            // ---- Move-Component drag (assembly): press / drag / release -------------------------
            // Active only when the open doc is an ASSEMBLY, a mesh is loaded, nothing else owns the
            // mouse, and EXACTLY ONE component body is selected. Pressing on that component's
            // geometry begins a constrained drag (doc_solve_mates {drag:{component, target}}); the
            // engine soft-pulls it toward the cursor along its free DOF and re-seats the rest.
            // Owns the mouse while active (orbit/pan/pick all skip while s_asmDrag.active). Runs
            // BEFORE push-pull so it wins in assemblies; the push-pull/dim blocks below also gate on
            // !s_asmDrag.active so the two gestures never fight.
            if (st.hasDocMesh && docIsAssembly(st) && !sceneToolArmed && !simFaceOwnsDrag &&
                !st.sketchPlanePickActive && !ppPmOpen && !s_zoomAreaArmed) {
                // Press: require a single-component selection and a press on its geometry.
                if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !s_asmDrag.active &&
                    st.selection.size() == 1) {
                    const render::PickHit& h = st.selection.items().front().hit;
                    if (h.valid()) {
                        const nlohmann::json* comp = asmComponentByOrdinal(st, h.body_id);
                        if (comp) {
                            // Only the picked component starts a drag — and only if the user pressed
                            // ON that component (the cursor's pick body matches the selection). This
                            // keeps a press in empty space / on another body from hijacking an orbit.
                            bool pressOnSel = st.hover.valid() && st.hover.body_id == h.body_id;
                            std::string cid = comp->value("id", std::string());
                            bool fixed = comp->value("fixed", false);
                            if (pressOnSel && !cid.empty() &&
                                !refuseViewportEdit(st, st.docMirror.docId())) {
                                if (fixed) {
                                    st.toast(ui::ToastLevel::Warn,
                                             "This component is fixed — float it to move (right-click "
                                             "> Float).");
                                } else {
                                    // Camera basis + px->mm scale at the component depth, captured for
                                    // the whole drag (R/U/worldPerPx already computed for this frame).
                                    std::string cp = st.docMirror.hasDoc()
                                                         ? st.docMirror.checkpoint(st.worker)
                                                         : std::string();
                                    s_asmDrag.reset();
                                    s_asmDrag.active     = true;
                                    s_asmDrag.docId      = st.docMirror.docId();
                                    s_asmDrag.component  = cid;
                                    s_asmDrag.checkpoint = cp;
                                    s_asmDrag.pressPos   = io.MousePos;
                                    s_asmDrag.startPos    = asmComponentOrigin(st, comp, h.body_id);
                                    s_asmDrag.camR        = R;
                                    s_asmDrag.camU        = U;
                                    s_asmDrag.worldPerPx  = worldPerPx;
                                    s_asmDrag.lastSent    = ImVec2(1e30f, 1e30f);
                                }
                            }
                        }
                    }
                }

                // Drag: project cursor motion to a world target and solve (throttled ~3px;
                // latest-wins async — at most one pair in flight).
                if (s_asmDrag.active && !s_asmDrag.finishing &&
                    ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                    st.requestCursor(ui::CursorKind::Hand);
                    cancelCameraAnimation();
                    ImVec2 mp = io.MousePos;
                    float ddx = mp.x - s_asmDrag.lastSent.x;
                    float ddy = mp.y - s_asmDrag.lastSent.y;
                    if (s_asmDrag.lastSent.x > 1e29f ||
                        std::sqrt(ddx * ddx + ddy * ddy) >= 3.0f * ui::uiScale)
                        asmDragQueue(st, mp, /*commit=*/false);
                }

                // Release: one final solve (engine bumps revision = single undo); state clears
                // when the async commit sample drains.
                if (s_asmDrag.active && !s_asmDrag.finishing &&
                    ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                    asmDragQueue(st, io.MousePos, /*commit=*/true);
                }

                // Esc cancels the drag (revert to the pre-drag pose).
                if (s_asmDrag.active && ImGui::IsKeyPressed(ImGuiKey_Escape))
                    asmDragCancel(st);
            }

            // ---- Instant3D push-pull: handle press / drag / release ----------------------------
            // The drag-to-extrude arrow is an EDIT-FEATURE affordance: it is live only while the
            // user has explicitly opened tree "Edit Feature" on a feature (PM in edit mode) and the
            // single selected face belongs to THAT feature. It never arms on a passive face pick.
            // While armed it owns the mouse for the drag's whole lifetime (orbit/pan/pick all skip).
            const bool ppEditing = st.propertyManager && st.propertyManager->isEditMode() &&
                                   st.propertyManager->hasPreviewTarget();
            if (st.hasDocMesh && !sceneToolArmed && !st.sketchPlanePickActive && ppEditing &&
                !s_zoomAreaArmed &&
                !s_asmDrag.active && !st.inSimulation()) {
                // Simulation lockout: no Instant3D push-pull while a study is open (the model is frozen
                // so fixtures/loads/results stay valid). Press: hit-test the handle arrow. We use the
                // handle geometry computed for THIS
                // frame's camera; the centroid screen pos + the two tips define a thick segment.
                if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !s_pp.active) {
                    ImVec2 cSp, tipP, tipN; float nsx, nsy, wpp; uint32_t bId, tId;
                    if (ppHandleGeometry(st, renderer, vr0, vr1, vw, vh, cSp, tipP, tipN,
                                         nsx, nsy, wpp, bId, tId) &&
                        st.docMirror.ownerOfFace(bId, tId) == st.propertyManager->targetId()) {
                        // Distance from the press point to the handle axis segment (centroid..tip,
                        // both directions). A generous hit radius makes the thin arrow grabbable.
                        ImVec2 mp = io.MousePos;
                        float hitR = 10.0f * ui::uiScale;
                        // Project press onto the infinite handle axis through the centroid.
                        float dx = mp.x - cSp.x, dy = mp.y - cSp.y;
                        float along = dx * nsx + dy * nsy;          // signed distance along the axis
                        float perp  = std::fabs(dx * (-nsy) + dy * nsx);  // perpendicular distance
                        float armLenPx = 46.0f * ui::uiScale;
                        bool onAxis = perp <= hitR && std::fabs(along) <= armLenPx + hitR;
                        // Also accept a direct hit near either arrowhead tip.
                        auto nearTip = [&](const ImVec2& t) {
                            float ddx = mp.x - t.x, ddy = mp.y - t.y;
                            return std::sqrt(ddx*ddx + ddy*ddy) <= hitR * 1.6f;
                        };
                        if ((onAxis || nearTip(tipP) || nearTip(tipN)) &&
                            !refuseViewportEdit(st, st.docMirror.docId())) {
                            // Validate draggability + take a checkpoint BEFORE mutating anything.
                            std::string cp = st.docMirror.hasDoc()
                                                 ? st.docMirror.checkpoint(st.worker) : std::string();
                            auto probe = st.docMirror.pushPull(st.worker, bId, tId, 0.0, false);
                            if (!probe.ok) {
                                st.toast(ui::ToastLevel::Error,
                                         "Push-pull failed: " + probe.error_message);
                                if (!cp.empty() && !st.activeDocLockedByAgent())
                                    st.docMirror.revert(st.worker, cp);
                            } else if (!probe.draggable) {
                                st.toast(ui::ToastLevel::Warn,
                                         "This face can't be dragged — " + probe.reason);
                                if (!cp.empty() && !st.activeDocLockedByAgent())
                                    st.docMirror.revert(st.worker, cp);
                            } else {
                                // Begin the drag. The zero-distance probe left geometry unchanged.
                                s_pp.reset();
                                s_pp.active     = true;
                                s_pp.docId      = st.docMirror.docId();
                                s_pp.bodyId     = bId;
                                s_pp.topoId     = tId;
                                s_pp.checkpoint = cp;
                                s_pp.pressPos   = mp;
                                s_pp.nsx = nsx; s_pp.nsy = nsy;
                                s_pp.worldPerPx = wpp;
                                s_pp.curDist    = 0.0;
                                s_pp.lastApplied = 1e30f;
                                s_pp.haveReadout = true;
                                s_pp.param    = probe.param;
                                s_pp.newValue = probe.old_value;  // value before the drag
                                s_pp.clamped  = false;
                            }
                        }
                    }
                }

                // Drag: convert cursor motion to a cumulative signed distance and queue a sample
                // (latest-wins async; ~0.05mm throttle so steady holds don't spam).
                // Screen direction is re-derived every frame from the drawn arrow endpoints
                // (tipPos - centroid) so the handle always moves WITH the cursor — single source
                // of truth for sign (no separate probe-normal projection that can invert).
                if (s_pp.active && !s_pp.finishing && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                    st.requestCursor(ui::CursorKind::Crosshair);
                    cancelCameraAnimation();
                    ImVec2 cSp, tipP, tipN; float nsx, nsy, wpp; uint32_t bId, tId;
                    if (ppHandleGeometry(st, renderer, vr0, vr1, vw, vh, cSp, tipP, tipN,
                                         nsx, nsy, wpp, bId, tId)) {
                        s_pp.nsx = nsx; s_pp.nsy = nsy;
                        s_pp.worldPerPx = wpp;
                    }
                    float dx = io.MousePos.x - s_pp.pressPos.x;
                    float dy = io.MousePos.y - s_pp.pressPos.y;
                    float px = dx * s_pp.nsx + dy * s_pp.nsy;   // signed px along the drawn arrow
                    double cumulative = (double)px * (double)s_pp.worldPerPx;
                    s_pp.curDist = cumulative;
                    if (std::fabs(cumulative - s_pp.lastApplied) >= 0.05)
                        ppQueueSample(st, cumulative, /*commit=*/false);
                }

                // Release: commit exactly one undo entry at the final distance (async).
                if (s_pp.active && !s_pp.finishing &&
                    ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                    ppQueueSample(st, s_pp.curDist, /*commit=*/true);
                }
            }

            // ---- Instant3D dimension-handle drag: press / drag / release ------------------------
            // ONLY while a feature's PropertyManager is open (creating/editing) — never on a passive
            // FeatureManager-tree highlight. The handles drive the feature being previewed
            // (pm->targetId()); a drag sets the matching PM field and fires the PM's live preview, so
            // the on-screen drag and the form field are ONE edit — the PM's OK/Cancel owns the single
            // commit/undo (no separate doc op or checkpoint here). The face push-pull is suppressed
            // while the PM is open, so the two gestures never fight.
            const bool pmDimActive = ppPmOpen && st.propertyManager &&
                                     st.propertyManager->hasPreviewTarget();
            const std::string dimFid = pmDimActive ? st.propertyManager->targetId() : std::string();
            if (st.hasDocMesh && !sceneToolArmed && !st.sketchPlanePickActive && pmDimActive &&
                !s_zoomAreaArmed &&
                !s_pp.active && !s_asmDrag.active) {
                // Press: hit-test every dimension handle of the feature under edit.
                if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !s_dim.active) {
                    auto cands = dimCandidates(st, dimFid);
                    ImVec2 mp = io.MousePos;
                    float hitR = 9.0f * ui::uiScale;
                    float armLenPx = 42.0f * ui::uiScale;
                    for (const auto& c : cands) {
                        ImVec2 aSp, tipP, tipN; float dsx, dsy, wpp, dpp;
                        if (!dimHandleGeometry(st, renderer, vr0, vr1, vw, vh, c, aSp, tipP,
                                               tipN, dsx, dsy, wpp, dpp))
                            continue;
                        float dx = mp.x - aSp.x, dy = mp.y - aSp.y;
                        float along = dx * dsx + dy * dsy;
                        float perp  = std::fabs(dx * (-dsy) + dy * dsx);
                        bool onAxis = perp <= hitR && std::fabs(along) <= armLenPx + hitR;
                        auto nearTip = [&](const ImVec2& t) {
                            float ddx = mp.x - t.x, ddy = mp.y - t.y;
                            return std::sqrt(ddx*ddx + ddy*ddy) <= hitR * 1.6f;
                        };
                        if (!(onAxis || nearTip(tipP) || nearTip(tipN))) continue;
                        if (refuseViewportEdit(st, st.docMirror.docId())) break;
                        // Grab this handle. The drag feeds the PM field — no doc op / checkpoint here;
                        // the PM owns the live preview and the single OK/Cancel commit.
                        s_dim.reset();
                        s_dim.active      = true;
                        s_dim.docId       = st.docMirror.docId();
                        s_dim.featureId   = dimFid;
                        s_dim.param       = c.param;
                        s_dim.angle       = c.angle;
                        s_dim.allowZero   = c.allowZero;
                        s_dim.rangeMin    = c.rangeMin;
                        s_dim.rangeMax    = c.rangeMax;
                        s_dim.label       = c.label;
                        s_dim.oldValue    = c.value;
                        s_dim.curValue    = c.value;
                        s_dim.lastApplied = 1e30;
                        s_dim.pressPos    = mp;
                        s_dim.dsx = dsx; s_dim.dsy = dsy;
                        s_dim.worldPerPx = wpp; s_dim.degPerPx = dpp;
                        s_dim.haveReadout = true;
                        s_dim.clamped = false;
                        // Depth + Reverse direction: if the open PM has a "flip" Check, drag past
                        // the sketch plane auto-toggles flip and keeps depth = |signed| (>0). Only
                        // the primary "depth" handle (Direction 1) — depth2 has no paired flip.
                        if (!c.angle && c.param == "depth" && st.propertyManager) {
                            bool flip = false;
                            if (st.propertyManager->dragGetCheck("flip", flip)) {
                                s_dim.autoFlip = true;
                                s_dim.flipAtPress = flip;
                                s_dim.curFlip = flip;
                                s_dim.lastFlipApplied = flip;
                                // Freeze the UNFLIPPED plane-normal screen axis: the drawn arrow
                                // already honours params.flip, so undo that so px delta is signed
                                // relative to the sketch plane (stable across zero crossings).
                                if (flip) { s_dim.dsx = -s_dim.dsx; s_dim.dsy = -s_dim.dsy; }
                            }
                        }
                        break;   // one handle per press
                    }
                }

                // Drag: convert cursor motion to an absolute value and push it into the PM field.
                // Length dims re-derive (dsx,dsy,worldPerPx) from the drawn arrow endpoints each
                // frame so the handle moves with the cursor (same invariant as push-pull) — except
                // autoFlip depth, which freezes the press-time plane-normal axis so a flip-driven
                // arrow reorient mid-drag cannot invert the signed delta.
                if (s_dim.active && ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                    st.requestCursor(ui::CursorKind::Crosshair);
                    cancelCameraAnimation();
                    if (!s_dim.angle && !s_dim.autoFlip) {
                        auto cands = dimCandidates(st, s_dim.featureId);
                        for (const auto& c : cands) {
                            if (c.param != s_dim.param) continue;
                            ImVec2 aSp, tipP, tipN; float dsx, dsy, wpp, dpp;
                            if (dimHandleGeometry(st, renderer, vr0, vr1, vw, vh, c, aSp, tipP,
                                                   tipN, dsx, dsy, wpp, dpp)) {
                                // Unit dir from drawn endpoints (tipP - anchor).
                                float tdx = tipP.x - aSp.x, tdy = tipP.y - aSp.y;
                                float tlen = std::sqrt(tdx * tdx + tdy * tdy);
                                if (tlen > 1e-3f) {
                                    s_dim.dsx = tdx / tlen; s_dim.dsy = tdy / tlen;
                                    s_dim.worldPerPx = wpp;
                                }
                            }
                            break;
                        }
                    }
                    float dx = io.MousePos.x - s_dim.pressPos.x;
                    float dy = io.MousePos.y - s_dim.pressPos.y;
                    float px = dx * s_dim.dsx + dy * s_dim.dsy;   // signed px along the handle axis
                    double newVal;
                    if (s_dim.angle)
                        newVal = s_dim.oldValue + (double)px * (double)s_dim.degPerPx;
                    else
                        newVal = s_dim.oldValue + (double)px * (double)s_dim.worldPerPx;
                    // Clamp per the form's range metadata: a BOUNDED field (revolve angle 0..360,
                    // draft 0..89, k-factor 0..1) clamps to [dimMin, dimMax]; unbounded fields keep
                    // the sane-minimum heuristic (engine push-pull floor 0.1mm / 1deg; dims where 0
                    // is valid floor at 0; signed offsets carry rangeMin = -1e9 and go negative).
                    // Flag clamped so the readout shows the limit.
                    s_dim.clamped = false;
                    if (s_dim.autoFlip && !s_dim.angle) {
                        // Signed depth relative to the sketch plane: positive = flipAtPress side.
                        // Crossing zero toggles Reverse direction; depth stays |signed| (>= 0.1).
                        double signed0 = s_dim.flipAtPress ? -s_dim.oldValue : s_dim.oldValue;
                        double signedVal = signed0 + (double)px * (double)s_dim.worldPerPx;
                        bool flip = signedVal < 0.0;
                        newVal = std::fabs(signedVal);
                        if (newVal < 0.1) { newVal = 0.1; s_dim.clamped = true; }
                        s_dim.curFlip = flip;
                    } else if (s_dim.rangeMax > s_dim.rangeMin) {
                        if (newVal < s_dim.rangeMin) { newVal = s_dim.rangeMin; s_dim.clamped = true; }
                        if (newVal > s_dim.rangeMax) { newVal = s_dim.rangeMax; s_dim.clamped = true; }
                    } else if (s_dim.angle) {
                        double lo = s_dim.rangeMin < 0.0 ? s_dim.rangeMin
                                                         : (s_dim.allowZero ? 0.0 : 1.0);
                        if (newVal < lo)    { newVal = lo;    s_dim.clamped = true; }
                        if (newVal > 360.0) { newVal = 360.0; s_dim.clamped = true; }
                    } else {
                        double lo = s_dim.rangeMin < 0.0 ? s_dim.rangeMin
                                                         : (s_dim.allowZero ? 0.0 : 0.1);
                        if (newVal < lo)    { newVal = lo;    s_dim.clamped = true; }
                    }
                    s_dim.curValue = newVal;
                    // Throttle: push to the PM field (which fires a live-preview rebuild) only when
                    // the value moved enough to matter (angles a touch coarser) AND at most ~20 Hz.
                    // Each field write backdates the PM debounce to rebuild immediately; on the shared
                    // daemon an unthrottled drag machine-guns edit_feature rebuilds (one-in-flight only
                    // bounds concurrency, not rate), so a slow rebuild stacks into multi-second stalls.
                    // A flip toggle bypasses the rate gate (direction must feel instant); the release
                    // always pushes the final value (below), so the committed depth is never throttled off.
                    const double nowT = ImGui::GetTime();
                    double thresh = s_dim.angle ? 0.25 : 0.05;
                    bool flipChanged = s_dim.autoFlip && (s_dim.curFlip != s_dim.lastFlipApplied);
                    bool moved  = std::fabs(newVal - s_dim.lastApplied) >= thresh;
                    bool rateOk = (nowT - s_dim.lastFireT) >= 0.05;   // ~20 previews/sec ceiling
                    if (flipChanged || (moved && rateOk)) {
                        bool ok = st.propertyManager->dragSetDim(s_dim.param, newVal);
                        if (ok && s_dim.autoFlip)
                            st.propertyManager->dragSetCheck("flip", s_dim.curFlip);
                        if (ok) {
                            s_dim.lastApplied = newVal;
                            s_dim.lastFlipApplied = s_dim.curFlip;
                            s_dim.lastFireT = nowT;
                        }
                    }
                }

                // Release: leave the final value in the PM field (its OK button commits it). The drag
                // never wrote a doc op of its own, so there is nothing to commit/undo here.
                if (s_dim.active && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                    st.propertyManager->dragSetDim(s_dim.param, s_dim.curValue);
                    if (s_dim.autoFlip)
                        st.propertyManager->dragSetCheck("flip", s_dim.curFlip);
                    s_dim.active = false;
                    s_dim.haveReadout = false;
                }
            }

            // ---- Evaluate Section Props: drag cutting plane along its normal --------------------
            // Press near a plane's arrow-tip handle (screen-space square drawn in the HUD pass),
            // project mouse delta onto the normal's screen direction, convert px→mm, and call
            // dragSectionPlane for live re-eval. Never fires while orbit/pan/PM/sketch own the mouse.
            if (!sceneToolArmed && st.evaluateDialogs && st.evaluateDialogs->sectionDragActive() &&
                st.hasDocMesh && !st.sketchPlanePickActive && !inSketch && !inSketch3d &&
                !s_zoomAreaArmed && !s_pp.active && !s_dim.active && !s_asmDrag.active) {
                const bool secPmOpen = st.propertyManager && st.propertyManager->active();
                if (!secPmOpen) {
                    // Press: hit-test arrow-tip handles (~10px).
                    if (imgHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
                        s_sectionDragIdx < 0) {
                        const float hitR = 10.0f * ui::uiScale;
                        const ImVec2 mp = io.MousePos;
                        int best = -1;
                        float bestD = hitR;
                        for (const auto& pl : st.evaluateDialogs->sectionPlanes()) {
                            if (!pl.active) continue;
                            const float L = pl.halfSize * 0.8f;
                            float tipW[3] = {
                                pl.origin[0] + pl.normal[0] * L,
                                pl.origin[1] + pl.normal[1] * L,
                                pl.origin[2] + pl.normal[2] * L,
                            };
                            ImVec2 tipSp;
                            if (!ppProject(renderer, vr0, vr1, vw, vh, tipW, tipSp)) continue;
                            float ddx = mp.x - tipSp.x, ddy = mp.y - tipSp.y;
                            float d = std::sqrt(ddx * ddx + ddy * ddy);
                            if (d <= bestD) { bestD = d; best = pl.index; }
                        }
                        if (best >= 0) {
                            s_sectionDragIdx = best;
                            cancelCameraAnimation();
                        }
                    }

                    // Drag: mouse delta along normal's screen direction → world mm.
                    if (s_sectionDragIdx >= 0 && ImGui::IsMouseDown(ImGuiMouseButton_Left) &&
                        (io.MouseDelta.x != 0.f || io.MouseDelta.y != 0.f)) {
                        st.requestCursor(ui::CursorKind::Crosshair);
                        cancelCameraAnimation();
                        for (const auto& pl : st.evaluateDialogs->sectionPlanes()) {
                            if (pl.index != s_sectionDragIdx) continue;
                            float oW[3] = {pl.origin[0], pl.origin[1], pl.origin[2]};
                            // Probe 1 mm along the normal for screen dir + world-per-px scale.
                            float nW[3] = {pl.origin[0] + pl.normal[0],
                                           pl.origin[1] + pl.normal[1],
                                           pl.origin[2] + pl.normal[2]};
                            ImVec2 oSp, nSp;
                            if (!ppProject(renderer, vr0, vr1, vw, vh, oW, oSp) ||
                                !ppProject(renderer, vr0, vr1, vw, vh, nW, nSp))
                                break;
                            float sdx = nSp.x - oSp.x, sdy = nSp.y - oSp.y;
                            float slen = std::sqrt(sdx * sdx + sdy * sdy);
                            if (slen < 1e-3f) break;
                            float nsx = sdx / slen, nsy = sdy / slen;
                            float worldPerPxN = 1.0f / slen;  // 1 mm spans `slen` px
                            float pxAlong = io.MouseDelta.x * nsx + io.MouseDelta.y * nsy;
                            float worldDelta = pxAlong * worldPerPxN;
                            if (std::fabs(worldDelta) > 1e-6f)
                                st.evaluateDialogs->dragSectionPlane(st, s_sectionDragIdx,
                                                                    worldDelta);
                            break;
                        }
                    }

                    if (s_sectionDragIdx >= 0 && !ImGui::IsMouseDown(ImGuiMouseButton_Left))
                        s_sectionDragIdx = -1;
                }
            } else if (s_sectionDragIdx >= 0) {
                s_sectionDragIdx = -1;
            }

            // Left-drag: orbit or pan depending on vpDragMode. Cancels any tween.
            // Zoom-area mode intercepts left-drag above; skip orbit/pan if armed.
            // C37-C: only starts dragging once the 4px threshold is crossed (s_lmbDragActive).
            // Instant3D: a push-pull OR dimension drag owns the mouse — never orbit/pan while active.
            if (!s_pp.active && !s_dim.active && !s_asmDrag.active && !s_zoomAreaArmed &&
                s_sectionDragIdx < 0 && !simFaceOwnsDrag &&
                s_lmbDragActive && (io.MouseDelta.x != 0 || io.MouseDelta.y != 0)) {
                cancelCameraAnimation();
                if (st.vpDragMode == 1) {
                    applyPan(io.MouseDelta.x, io.MouseDelta.y);
                } else {
                    st.yaw += io.MouseDelta.x * 0.4f;
                    st.pitch += io.MouseDelta.y * 0.4f;   // free orbit: no pitch clamp
                }
            }
            // W1-B (item 3): in pan-drag mode, show the Hand cursor over the viewport so the user
            // sees they can grab-pan (SolidWorks behavior). The cursor manager in app.cpp consumes
            // this per-frame request. (Wave-2 sketch tools / PM wells request Crosshair similarly.)
            if (st.vpDragMode == 1 && !s_zoomAreaArmed)
                st.requestCursor(ui::CursorKind::Hand);

            // Middle-button navigation (SolidWorks bindings):
            //   MMB        = orbit / "spin the part around" (free 360, no pitch clamp)
            //   Ctrl+MMB   = pan  / slide the view (translate)
            //   Shift+MMB  = zoom (vertical, smooth exponential)
            if (ImGui::IsMouseDragging(ImGuiMouseButton_Middle) &&
                (io.MouseDelta.x != 0 || io.MouseDelta.y != 0)) {
                cancelCameraAnimation();
                if (ctrl) {
                    applyPan(io.MouseDelta.x, io.MouseDelta.y);
                } else if (shift) {
                    // Drag up = zoom in. exp keeps each pixel a constant multiplicative step.
                    st.zoom = std::clamp(st.zoom * std::exp(-io.MouseDelta.y * 0.005f),
                                         0.1f, renderer.maxSceneZoom());
                } else {
                    st.yaw += io.MouseDelta.x * 0.4f;
                    st.pitch += io.MouseDelta.y * 0.4f;   // free orbit: no pitch clamp
                }
            }

            // Double-click MMB = animated zoom-to-fit (zoom->1, pan->0, orientation kept).
            if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Middle)) {
                applyViewPreset(st, ViewPreset::ZoomFit);
            }

            // Wheel zoom toward the cursor: scale zoom, then shift pan so the world point under the
            // cursor stays put. Cursor offset from viewport center, in pixels.
            if (io.MouseWheel != 0) {
                cancelCameraAnimation();
                float oldZoom = st.zoom < 0.05f ? 0.05f : st.zoom;
                st.zoom = std::clamp(st.zoom * std::pow(1.1f, io.MouseWheel),
                                     0.1f, renderer.maxSceneZoom());
                float newZoom = st.zoom;
                if (newZoom != oldZoom) {
                    float cxp = io.MousePos.x - clrC.x;
                    float cyp = io.MousePos.y - clrC.y;
                    // worldPerPx is inversely proportional to zoom; the target must move so the
                    // cursor ray hits the same world point. delta = cursorOffset * (1/old - 1/new)
                    // in world units, projected onto the camera plane (R right, U up; y is down).
                    // Recompute scale at the OLD zoom (worldPerPx above is from last drawn frame).
                    float k = worldPerPx * (1.0f - oldZoom / newZoom);
                    st.pan[0] += ( R.x * cxp - U.x * cyp) * k;
                    st.pan[1] += ( R.y * cxp - U.y * cyp) * k;
                    st.pan[2] += ( R.z * cxp - U.z * cyp) * k;
                    clampPan();   // keep the model from drifting fully out of view as we zoom in
                }
            }
        } else if (imgHovered && (inSketch || inSketch3d)) {
            // View navigation WHILE sketching (left-click stays for drawing entities):
            //   middle-drag        = orbit / "spin the part" (unlatches the normal-to lock; free 360)
            //   Ctrl + middle-drag = pan / slide the view (translate)
            //   Shift + middle-drag= zoom ; wheel = zoom
            // The sketch overlay reprojects from the live camera each frame, so a rotated view stays
            // correct (entity picking included).
            const bool ctrl = io.KeyCtrl, shift = io.KeyShift;
            // Deconflict: while a sketch entity/handle/group LMB drag is armed or live, ALL camera
            // input yields — no MMB orbit/pan/zoom and no wheel zoom. The sketch overlay's own
            // edge auto-pan is the only thing allowed to move the view mid-drag.
            const bool skDrag = (inSketch && sketch != nullptr && sketch->entityDragActive()) ||
                                (inSketch3d && sketch3d != nullptr && sketch3d->entityDragActive());
            if (!skDrag && ImGui::IsMouseDragging(ImGuiMouseButton_Middle) &&
                (io.MouseDelta.x != 0 || io.MouseDelta.y != 0)) {
                cancelCameraAnimation();
                if (ctrl) {
                    // World-space pan via the published camera seam (ortho + perspective).
                    float worldPerPx = renderer.worldPerPixel(vh);
                    const auto& cam = renderer.camera();
                    Vec3 R = cam.right;
                    Vec3 U = cam.up;
                    st.pan[0] += (-R.x * io.MouseDelta.x + U.x * io.MouseDelta.y) * worldPerPx;
                    st.pan[1] += (-R.y * io.MouseDelta.x + U.y * io.MouseDelta.y) * worldPerPx;
                    st.pan[2] += (-R.z * io.MouseDelta.x + U.z * io.MouseDelta.y) * worldPerPx;
                } else if (shift) {
                    st.zoom = std::clamp(st.zoom * std::exp(-io.MouseDelta.y * 0.005f),
                                         0.1f, renderer.maxSceneZoom());
                } else {
                    st.yaw += io.MouseDelta.x * 0.4f;
                    st.pitch += io.MouseDelta.y * 0.4f;   // free orbit: no pitch clamp
                    s_sketchRotated = true;   // stop forcing normal-to so the rotation sticks
                }
            }
            if (!skDrag && io.MouseWheel != 0) {
                st.zoom = std::clamp(st.zoom * std::pow(1.1f, io.MouseWheel),
                                     0.1f, renderer.maxSceneZoom());
            }
        }

        // C37-C: keyboard view shortcuts — globalized: fire when the viewport is VISIBLE and
        // !io.WantTextInput and no modal open, regardless of hover. Keep !inSketch for the preset
        // rotations. Ctrl+8 (Normal To) is allowed while in sketch (item 1). F/Shift+F blocked in
        // sketch (camera is locked; ZoomFit would fight the lock). Space opens ViewSelector.
        // Condition: viewport is showing (we are inside the Begin/End block), and no text input.
        if (st.showViewport && !io.WantTextInput) {
            bool noModal = !ImGui::IsPopupOpen("", ImGuiPopupFlags_AnyPopupId);
            if (noModal) {
                if (io.KeyCtrl && !inSketch) {
                    // Ctrl+1..7 = view presets (not in sketch).
                    if      (ImGui::IsKeyPressed(ImGuiKey_1)) applyViewPreset(st, ViewPreset::Front);
                    else if (ImGui::IsKeyPressed(ImGuiKey_2)) applyViewPreset(st, ViewPreset::Back);
                    else if (ImGui::IsKeyPressed(ImGuiKey_3)) applyViewPreset(st, ViewPreset::Left);
                    else if (ImGui::IsKeyPressed(ImGuiKey_4)) applyViewPreset(st, ViewPreset::Right);
                    else if (ImGui::IsKeyPressed(ImGuiKey_5)) applyViewPreset(st, ViewPreset::Top);
                    else if (ImGui::IsKeyPressed(ImGuiKey_6)) applyViewPreset(st, ViewPreset::Bottom);
                    else if (ImGui::IsKeyPressed(ImGuiKey_7)) applyViewPreset(st, ViewPreset::Isometric);
                }
                // Ctrl+8 = Normal To — allowed in AND out of sketch (item 1).
                if (io.KeyCtrl && ImGui::IsKeyPressed(ImGuiKey_8))
                    normalToSelectionOrNearestAxis(st, inSketch);
                if (!inSketch) {
                    // F = Zoom to Fit (not in sketch).
                    if (!io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_F))
                        applyViewPreset(st, ViewPreset::ZoomFit);
                    // C37-C item 2: Shift+F = Zoom to Selection (not in sketch).
                    if (io.KeyShift && ImGui::IsKeyPressed(ImGuiKey_F))
                        zoomToSelection(st);
                    // C37-C item 3: Space = open View Selector (when viewport visible, not sketching).
                    if (imgHovered && ImGui::IsKeyPressed(ImGuiKey_Space))
                        /* Space handled by ViewSelector::update() below — it reads imgHovered and
                           !inSketch; we just ensure no double-handling here. */ (void)0;
                }
                // Esc: cancel in-flight camera tween (selection-clear Esc handling lives in the
                // picking block below; canceling here is harmless when there is no animation).
                if (!sceneToolArmed && ImGui::IsKeyPressed(ImGuiKey_Escape) &&
                    cameraAnimating()) cancelCameraAnimation();
            }
        }

        bool sceneConsumedClick = false;
        if (sceneActive) {
            ui::simscene::ViewportCtx sceneViewport;
            sceneViewport.cam = renderer.camera();
            sceneViewport.imgMin = vr0;
            sceneViewport.imgMax = vr1;
            sceneViewport.vw = vw;
            sceneViewport.vh = vh;
            sceneViewport.hovered = imgMouseOver &&
                !ImGui::IsPopupOpen("", ImGuiPopupFlags_AnyPopupId) &&
                ImGui::GetTopMostPopupModal() == nullptr;
            sceneViewport.navigating = s_lmbDragActive ||
                ImGui::IsMouseDragging(ImGuiMouseButton_Left) ||
                ImGui::IsMouseDragging(ImGuiMouseButton_Middle);
            sceneConsumedClick = ui::simscene::handleViewport(
                st, sceneViewport, ImGui::GetWindowDrawList());
        }
        // Keep the frame-start armed state even if Esc just disarmed the tool; that Esc must
        // not fall through to selection clearing or another viewport tool in the same frame.
        const bool sceneOwnsClick = sceneToolArmed || sceneConsumedClick;
        const bool selectionHovered = imgHovered && !sceneOwnsClick;

        // ---- C18: sketch overlay owns the mouse while a sketch is being edited ----
        // C37-C item 5: also call drawOverlay during sketchPlanePickActive (no active sketch yet,
        // but the sketcher agent needs to draw plane hover/click overlay). Guard with sketch != null.
        // C40: inactive sketches ghost-render in every mode (SolidWorks grey sketches); the
        // active sketch (if any) is skipped — drawOverlay below owns it.
        // Sketch-plane pick (SolidWorks "New Sketch -> pick a face/plane") needs a live face hover so a
        // planar SOLID FACE under the cursor can win the pick over the datum plane behind it. The main
        // pick block (~2664) is suppressed during sketchPlanePickActive (the overlay owns the mouse +
        // click), so st.hover was never populated and the overlay's face branch was dead. Compute a
        // hover-only FACE pick HERE — before drawOverlay reads st.hover — with NO click/selection/Esc
        // side effects (the overlay below consumes the click). Placed above drawOverlay for same-frame
        // correctness (no one-frame hover lag).
        if (picking && st.sketchPlanePickActive && !s_viewSelector.active()) {
            if (imgHovered && !s_lmbDragActive && st.idPass) {
                int px = (int)(io.MousePos.x - vr0.x);
                int py = (int)(io.MousePos.y - vr0.y);
                ensurePickFresh();
                st.hover = st.idPass->pickFinal(px, py, vw, vh);
            } else {
                st.hover = render::PickHit{};
            }
        }
        if (sketch != nullptr) sketch->drawGhosts(st, vr0, vr1, vw, vh);
        if (inSketch) {
            // While the glass-box View Selector is open over a sketch, it owns the cursor — tell the
            // sketch overlay it's not hovered so a click on a box face switches the view instead of
            // dropping a sketch entity.
            // Convert, Smart Dimension and relation reference picking read the model ID buffer
            // inside drawOverlay, even while their tool owns all sketch input. A committed sketch
            // edit reloads the mesh and invalidates that buffer; refresh BEFORE those reads rather
            // than relying on the ordinary fallback below (which armed tools deliberately skip).
            // IdPass::render caches the complete draw key, so a stationary tool hover does not
            // re-render the model every frame.
            if (picking && imgHovered && !s_viewSelector.active() &&
                !s_lmbDragActive && sketch->needsModelPicks())
                ensurePickFresh();
            sketch->drawOverlay(st, io, vr0, vr1, vw, vh,
                                imgHovered && !s_viewSelector.active());
        } else if (st.sketchPlanePickActive && sketch != nullptr) {
            // Plane-pick mode: sketcher draws the plane quads/hover/click, but no sketch is active.
            sketch->drawOverlay(st, io, vr0, vr1, vw, vh, imgHovered);
        }

        // ---- 3D sketch overlay (working-plane grid + entities + tools + toolbar) ----
        if (inSketch3d)
            sketch3d->drawOverlay(st, io, vr0, vr1, vw, vh,
                                  imgHovered && !s_viewSelector.active());

        // ---- C51: feature-PM region picker (hover-highlight + click-to-toggle profiles) ----
        // Runs when a feature PropertyManager with a profile/sections well is open and no sketch is
        // being edited. Returns true while the cursor is over a sketch region, so the model-pick
        // click below is suppressed (a region click must not also select a model face/edge).
        // Suppressed while Instant3D depth/push-pull is active — dragging the extrude arrow must
        // not also toggle profile contours under the cursor.
        bool regionPickerHot = false;
        if (sketch != nullptr && !inSketch && !inSketch3d && !st.sketchPlanePickActive &&
            !s_dim.active && !s_pp.active)
            regionPickerHot = sketch->drawRegionPicker(st, io, vr0, vr1, vw, vh, selectionHovered);

        // ---- Wave4: origin triad pick priority (before any plane hit-test) ----
        // With planes force-shown the three origin quads all pass through the world origin, so a
        // plane hit-test always swallows clicks on the origin glyph. Give the origin a generous
        // screen-space grab radius (matches SketchMode::drawDatumSelect) and test it FIRST so the
        // glyph wins for both hover affordance and click. Skipped while an axis/plane PM well owns
        // datum picks (those wells want planes/axes, not the origin point).
        bool originPickHot = false;
        {
            auto* pm = st.propertyManager;
            const bool pmOwnsAxisPlane = pm && pm->active() &&
                (pm->activeWellWantsAxis() || pm->activeWellWantsPlane());
            if (!inSketch && !inSketch3d && !st.sketchPlanePickActive && selectionHovered &&
                !s_viewSelector.active() && !s_lmbDragActive && !pmOwnsAxisPlane &&
                st.originVisEff) {
                // Pick hitbox stays a generous fixed pixel radius (not zoom-dependent).
                const float grabRadius = 20.0f * ui::uiScale;
                // ITEM 14: DRAWN ring is a smaller fixed pixel radius — NOT grabRadius*0.8 and
                // NOT derived from scene/model size. Root cause of zoom-dependent look: the 3D
                // origin glyph is world-sized (renderer os*0.16f ~ sceneR), so its on-screen size
                // scales with zoom; a ring tied to the large pick hitbox (16px) looked huge vs the
                // tiny zoomed-out glyph and tiny vs the zoomed-in glyph. Keep pick = grabRadius.
                const float ringRadius = 9.0f * ui::uiScale;
                const float originW[3] = {0.0f, 0.0f, 0.0f};
                ImVec2 originS;
                if (ppProject(renderer, vr0, vr1, vw, vh, originW, originS)) {
                    const float dx = io.MousePos.x - originS.x;
                    const float dy = io.MousePos.y - originS.y;
                    if (std::sqrt(dx * dx + dy * dy) < grabRadius) {
                        originPickHot = true;
                        // Hover ring only when not selected — selected uses a distinct style below.
                        if (st.selectedFeatureId != "point.origin") {
                            ImDrawList* wdl = ImGui::GetWindowDrawList();
                            wdl->PushClipRect(vr0, vr1, true);
                            float accA[4];
                            ui::Palette::withAlpha(ui::palette.accent, accA, 0.95f);
                            wdl->AddCircle(originS, ringRadius, ui::Palette::toU32(accA),
                                           24, 2.0f * ui::uiScale);
                            wdl->PopClipRect();
                        }
                        st.requestCursor(ui::CursorKind::Hand);
                        if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                            if (!io.KeyCtrl) st.selection.clear();
                            st.selectedFeatureId = "point.origin";
                            st.planeEmphasis = -1;
                            st.treeScrollToSelection = true;
                            // Point-well PM: also route into the well (same as drawPointPicker).
                            if (pm && pm->active() && pm->activeWellWantsPoint())
                                pm->routeDatumPoint("point.origin", "Origin");
                        }
                    }
                }
            }
            // Selected-origin highlight: fixed screen-space circle (ringRadius), distinct style
            // (soft fill + thicker accent stroke). Pick hitbox remains grabRadius above.
            if (!inSketch && !inSketch3d && st.originVisEff &&
                st.selectedFeatureId == "point.origin") {
                const float ringRadius = 9.0f * ui::uiScale;
                const float originW[3] = {0.0f, 0.0f, 0.0f};
                ImVec2 originS;
                if (ppProject(renderer, vr0, vr1, vw, vh, originW, originS)) {
                    ImDrawList* wdl = ImGui::GetWindowDrawList();
                    wdl->PushClipRect(vr0, vr1, true);
                    float fillA[4];
                    ui::Palette::withAlpha(ui::palette.selectionHighlight, fillA, 1.0f);
                    wdl->AddCircleFilled(originS, ringRadius, ui::Palette::toU32(fillA), 24);
                    float ringA[4];
                    ui::Palette::withAlpha(ui::palette.accent, ringA, 1.0f);
                    wdl->AddCircle(originS, ringRadius, ui::Palette::toU32(ringA),
                                   24, 2.5f * ui::uiScale);
                    wdl->PopClipRect();
                }
            }
        }

        // ---- general model-mode reference-axis pick (BEFORE plane fill) ----
        // Axes are lines; planes are areas. Inside the ~10 px axis band the line wins so a
        // translucent datum plane never swallows the click. drawPlanePicker / drawDatumSelect
        // run AFTER this and skip when axisSelectHot. Sketch entity picks and origin grab win first.
        bool axisSelectHot = false;
        bool axisPickerHot = false;
        {
            auto* pm = st.propertyManager;
            // Only a PM *plane* well owns plane routing. Plain plane-fill select must NOT preempt
            // axis hits — that was the "can't click axis through a plane" bug (drawPlanePicker
            // returned hot over the whole quad).
            const bool pmOwnsPlaneWell = pm && pm->active() && pm->activeWellWantsPlane();
            const bool pmOwnsAxisWell  = pm && pm->active() && pm->activeWellWantsAxis();
            if (!inSketch && !inSketch3d && !st.sketchPlanePickActive && selectionHovered &&
                !s_viewSelector.active() && !s_lmbDragActive && !originPickHot &&
                !pmOwnsAxisWell && !pmOwnsPlaneWell && !s_pp.active && !s_dim.active) {
                ImVec2 a, b;
                std::string fid, name;
                if (hitTestRefAxis(renderer, st, io.MousePos, vr0, vr1, vw, vh, a, b, fid, name)) {
                    axisSelectHot = true;
                    // Hover affordance only when not already selected (selected uses renderer bold).
                    if (st.selectedFeatureId != fid) {
                        ImDrawList* wdl = ImGui::GetWindowDrawList();
                        wdl->PushClipRect(vr0, vr1, true);
                        float accA[4];
                        ui::Palette::withAlpha(ui::palette.accent, accA, 0.90f);
                        wdl->AddLine(a, b, ui::Palette::toU32(accA), 4.0f * ui::uiScale);
                        wdl->PopClipRect();
                    }
                    st.requestCursor(ui::CursorKind::Hand);
                    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                        if (!io.KeyCtrl) st.selection.clear();
                        st.selectedFeatureId = fid;   // FM row + renderer.setSelectedDatumId
                        st.planeEmphasis = -1;
                        st.treeScrollToSelection = true;
                    }
                }
            }
            // PM axis well: run axis picker HERE (before plane fill) so a plane quad never
            // steals the ~10 px axis band while routing into the well.
            if (pmOwnsAxisWell && sketch != nullptr && !inSketch && !inSketch3d &&
                !st.sketchPlanePickActive && !originPickHot)
                axisPickerHot = sketch->drawAxisPicker(st, io, vr0, vr1, vw, vh, selectionHovered);
        }

        // ---- datum-plane picker (feature PM well OR plain graphics-area select) ----
        // Runs when a feature PM with an active plane-accepting well is open, OR for plain
        // plane select. Suppressed while an axis is hot so plane fill never steals the band.
        // Wave4: also suppressed while the origin grab radius owns the cursor (origin wins).
        bool planePickerHot = false;
        if (sketch != nullptr && !inSketch && !inSketch3d && !st.sketchPlanePickActive &&
            !originPickHot && !axisSelectHot && !axisPickerHot)
            planePickerHot = sketch->drawPlanePicker(st, io, vr0, vr1, vw, vh, selectionHovered);

        // ---- datum-axis picker (revolve "Axis", circular-pattern axis) ----
        // Already handled above when a PM axis well is open. Non-well path keeps a no-op call
        // site for any future non-PM axis-picker consumers (currently always false without a well).
        if (!axisPickerHot && sketch != nullptr && !inSketch && !inSketch3d &&
            !st.sketchPlanePickActive && !originPickHot && !planePickerHot)
            axisPickerHot = sketch->drawAxisPicker(st, io, vr0, vr1, vw, vh, selectionHovered);

        // ---- datum-point picker (world origin / user point-datum into a point well) ----
        // Wave4: also suppressed while the origin grab radius owns the cursor (origin wins).
        bool pointPickerHot = false;
        if (sketch != nullptr && !inSketch && !inSketch3d && !st.sketchPlanePickActive &&
            !originPickHot && !planePickerHot && !axisPickerHot)
            pointPickerHot = sketch->drawPointPicker(st, io, vr0, vr1, vw, vh, selectionHovered);

        // ---- sweep/loft path picker (click a displayed helix / curve to set it as the path) ----
        // Runs when a feature PM with an active path/centerline well is open. Returns true while the
        // cursor is over a drawn curve datum, so the model-pick click below is suppressed (the curve
        // click routes the feature into the path well via routePathFeature).
        bool curvePickerHot = false;
        if (sketch != nullptr && !inSketch && !inSketch3d && !st.sketchPlanePickActive &&
            !originPickHot && !planePickerHot && !axisPickerHot && !pointPickerHot)
            curvePickerHot = sketch->drawCurvePathPicker(st, io, vr0, vr1, vw, vh, selectionHovered);

        // ---- general datum selection: planes / user points (axes handled above) ----
        // Runs in the normal (non-sketch, no-PM-picker) flow. Skipped while an axis is hot so
        // plane hit-tests never steal the click from a coincident reference axis.
        // Wave4: origin already claimed above (same grab radius).
        bool datumSelectHot = false;
        if (sketch != nullptr && !inSketch && !inSketch3d && !st.sketchPlanePickActive &&
            !originPickHot && !planePickerHot && !axisPickerHot && !pointPickerHot &&
            !curvePickerHot && !axisSelectHot)
            datumSelectHot = sketch->drawDatumSelect(st, io, vr0, vr1, vw, vh, selectionHovered);

        // ---- fillet/chamfer dashed old-edge overlay (deselect targets over the live round) ----
        bool blendDeselectHot = false;
        if (!inSketch && !inSketch3d && !st.sketchPlanePickActive && !originPickHot &&
            !planePickerHot && !axisPickerHot && !pointPickerHot && !axisSelectHot && !datumSelectHot)
            blendDeselectHot = drawBlendEdgeOverlay(st, renderer, io, vr0, vr1, vw, vh,
                                                   selectionHovered);

        // ---- hover + click selection (once per frame; only while a doc mesh is loaded) ----
        // C25 W2-E: suppressed while the glass-box View Selector is up (it owns the click).
        // C37-C item 5: also suppressed while sketchPlanePickActive (the overlay owns the mouse).
        //
        // Model geometry is a fallback behind sketch handles, except when Add Relation explicitly
        // asks to select model geometry: that mode must reach an edge/vertex even if a sketch
        // entity lies on top of it. Armed drawing tools still own their own clicks.
        const bool sketchEntityHot = (inSketch && sketch && sketch->inSketchEntityHot()) ||
                                     (inSketch3d && sketch3d && sketch3d->inSketchEntityHot());
        const bool inAnySketch = inSketch || inSketch3d;
        const bool relationModelPick = inSketch && sketch && sketch->modelSelectionAllowed();
        if (picking && (!inAnySketch || !sketchEntityHot || relationModelPick) &&
            !s_viewSelector.active() && !st.sketchPlanePickActive) {
            // Instant3D: while a push-pull OR dimension drag owns the mouse, freeze hover and swallow
            // the click so the press that grabbed the handle doesn't also re-select / clear. (Both
            // are non-sketch features, so their guards are always false while in a sketch.)
            if (imgHovered && !s_lmbDragActive && !originPickHot && !regionPickerHot && !planePickerHot &&
                !axisPickerHot && !pointPickerHot && !curvePickerHot && !axisSelectHot &&
                !datumSelectHot && !blendDeselectHot &&
                !s_pp.active && !s_dim.active && s_sectionDragIdx < 0) {
                int px = (int)(io.MousePos.x - vr0.x);
                int py = (int)(io.MousePos.y - vr0.y);
                // Fillet/chamfer "Edges" wells: snap to the nearest edge within a grab radius so the
                // 1-px curved rims of a revolved/round body are clickable (they'd otherwise lose the
                // centre pixel to the face behind). Same edge-priority trick the Convert tool uses.
                // In-sketch fallback picking always prefers edge/vertex the same way — sketch
                // reference picking (Convert, snap targets) cares about edges/vertices far more
                // than the coplanar face usually sitting right behind the sketch plane.
                const bool preferEdge = inAnySketch ||
                    (st.propertyManager && st.propertyManager->active() &&
                     st.propertyManager->activeWellPrefersEdge());
                // General viewport hover ALSO snaps to nearby edges/vertices (SolidWorks/Fusion pick
                // feel): points and edges are tiny targets, so an exact single-pixel pick almost
                // always lands on a face interior and thin geometry becomes unclickable. Use a modest
                // radius here so faces stay pickable in their interiors, and the larger reference
                // radius for the tools that explicitly hunt edges (Convert Entities, edge wells).
                // pickFinalPreferEdge falls back to the exact-pixel face when nothing thin is in range.
                const float snapRadius = preferEdge ? 14.0f : 7.0f;
                ensurePickFresh();
                st.hover = st.idPass->pickFinalPreferEdge(px, py, vw, vh,
                                                          (int)(snapRadius * ui::uiScale));
            } else {
                st.hover = render::PickHit{};
            }

            // Click = replace, Ctrl-click = toggle. Empty-space click clears.
            // C37-C item 3: guard against micro-orbit clicks — only fire if drag was not active.
            // C51: a click consumed by the region picker never falls through to model selection.
            // Instant3D: a click that started a push-pull / dimension drag is NOT a selection.
            if (selectionHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left) &&
                !s_lmbDragActive && !originPickHot && !regionPickerHot && !planePickerHot &&
                !axisPickerHot && !pointPickerHot && !curvePickerHot && !axisSelectHot &&
                !datumSelectHot &&
                !blendDeselectHot && !s_pp.active && !s_dim.active && s_sectionDragIdx < 0) {
                // Wave4 context cube: arm → append/toggle pendingContextRefs (chips via composer).
                // Still update normal selection so the face/edge highlight stays readable.
                if (st.contextPickActive && st.hover.valid()) addPickContextRef(st, st.hover);
                if (io.KeyCtrl) {
                    if (st.hover.valid()) st.selection.toggle(st.hover);
                } else if (st.hover.valid()) {
                    st.selection.selectOne(st.hover);
                } else if (!st.contextPickActive) {
                    // Empty-space clear only when the cube is NOT armed (armed mode accumulates).
                    st.clearSelection();
                }
            }
            // ESC: during a push-pull / dimension drag, cancel it (revert) and DON'T clear the
            // selection. Otherwise ESC clears the selection while the viewport is focused/hovered.
            // C37-C item 5: do NOT consume Esc while sketchPlanePickActive (guard above already
            // prevents this block from running in that case, so this Esc is safe).
            if (!sceneToolArmed && ImGui::IsKeyPressed(ImGuiKey_Escape)) {
                if (s_pp.active) ppCancel(st);
                else if (s_dim.active) {
                    // Cancel the in-progress dim drag: restore the field to its press-time value
                    // (the PM re-previews) and end the drag. The PM itself stays open.
                    if (st.propertyManager && !st.docLockedByAgent(s_dim.docId) &&
                        st.docMirror.docId() == s_dim.docId) {
                        st.propertyManager->dragSetDim(s_dim.param, s_dim.oldValue);
                        if (s_dim.autoFlip)
                            st.propertyManager->dragSetCheck("flip", s_dim.flipAtPress);
                    }
                    s_dim.reset();
                } else if (s_sectionDragIdx >= 0) {
                    s_sectionDragIdx = -1;
                } else if (st.contextPickActive) {
                    // Wave4: Esc finishes context-pick mode (composer cube). Keep selection.
                    st.contextPickActive = false;
                }
                else if (imgHovered) st.selection.clear();
            }

            // Status text: prefer the live hover, else the last selected item.
            if (st.hover.valid()) {
                st.pickStatus = st.pickLabel(st.hover);
            } else if (!st.selection.empty()) {
                st.pickStatus = st.pickLabel(st.selection.items().back().hit);
            } else {
                st.pickStatus.clear();
            }
        }

        // ---- Wave4: empty-doc whitespace deselect ----
        // Topology picking only runs while hasDocMesh (id-pass needs a body). On a body-less part
        // the only selection is a plane/datum (viewport click, FM row, or plane chip). Without this
        // branch, empty-space clicks never clear selectedFeatureId/planeEmphasis and the plane
        // selection sticks. Same click-vs-drag threshold as the mesh path (s_lmbDragActive).
        if (!picking && !inSketch && !inSketch3d && !st.sketchPlanePickActive &&
            !s_viewSelector.active() && selectionHovered &&
            ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !s_lmbDragActive &&
            !originPickHot && !regionPickerHot && !planePickerHot && !axisPickerHot &&
            !pointPickerHot && !curvePickerHot && !axisSelectHot && !datumSelectHot &&
            !blendDeselectHot &&
            !s_pp.active && !s_dim.active) {
            st.clearSelection();
        }

        // ---- C25: mouse gesture wheel (right-drag) + 'S' shortcut bar ----
        // Gestures are suppressed while in sketch mode (camera is plane-locked; no view presets).
        if (!inSketch) {
            s_gestures.update(st, imgHovered, cr0, cr1);
            s_gestures.draw(ImGui::GetWindowDrawList());
        }
        // Shortcut bar is context-aware: shows sketch tools when inSketch, model tools otherwise.
        s_shortcutBar.update(st, imgHovered, inSketch, cr0, cr1);
        s_shortcutBar.draw(st, inSketch);

        // ---- C25 W3-J: viewport right-click context menu ----
        // Opened by mouse_gestures when right button released without drag (kRmbContextMenuHook).
        // ITEM 11: RMB over a component with no prior LMB selection still resolves Open Part —
        // promote the live hover pick into the selection when the menu opens.
        if (st.wantContextMenu && !inSketch) {
            st.wantContextMenu = false;
            if (st.hover.valid() &&
                (st.selection.empty() ||
                 st.selection.items().back().hit.body_id != st.hover.body_id ||
                 st.selection.items().back().hit.topo_id != st.hover.topo_id ||
                 st.selection.items().back().hit.kind != st.hover.kind)) {
                st.selection.selectOne(st.hover);
            }
            ImGui::OpenPopup("##vp_ctx");
        }
        // C26: appearance state shared between the vp_ctx menu item and its sibling picker popup.
        static float s_vpColor[3]   = {0.71f, 0.76f, 0.82f};
        static float s_vpMetallic = 0.0f, s_vpRoughness = 0.5f;  // C32-A material
        static std::string s_vpKey;          // "b<ordinal>" of the body to recolor
        static std::string s_vpDocId;        // appearance popup's source document
        static bool  s_openVpPicker = false;
        // The picked body's stable appearance key (part bodies: "b<ordinal>", matching the engine).
        // Appearance is offered for part docs only. Assembly component actions are handled below
        // via body_id → asmComponentByOrdinal.
        // NOTE: tree() is null until the first ops response after doc_new — json::value()
        // on a null json THROWS (this crashed the m1 scenario); guard with is_object().
        bool vpIsPart = st.docMirror.hasDoc() && st.docMirror.tree().is_object() &&
                        st.docMirror.tree().value("schema", std::string()) != "aicad.asm/1";
        std::string vpBodyKey;
        if (vpIsPart && !st.selection.empty()) {
            uint32_t bid = st.selection.items().back().hit.body_id;
            if (bid != 0) vpBodyKey = "b" + std::to_string(bid);
        }

        // Assembly component under the current selection (body ordinal → component JSON).
        const nlohmann::json* vpComp = nullptr;
        std::string vpCompId, vpCompName, vpCompAbsPath;
        bool vpCompHidden = false;
        if (docIsAssembly(st) && !st.selection.empty()) {
            uint32_t bid = st.selection.items().back().hit.body_id;
            vpComp = asmComponentByOrdinal(st, bid);
            if (vpComp) {
                vpCompId = vpComp->value("id", std::string());
                vpCompName = vpComp->value("name", std::string());
                if (vpCompName.empty()) vpCompName = vpCompId;
                vpCompHidden = vpComp->value("hidden", false);
                std::string srcPath;
                if (vpComp->contains("source") && (*vpComp)["source"].is_object())
                    srcPath = (*vpComp)["source"].value("path", std::string());
                // Prefer the FeatureManager's active path (same s_docPath the tree resolves
                // against); fall back to the active tab path if the tree hasn't set it yet.
                std::string asmPath = featureTreeActivePath();
                if (asmPath.empty() && st.activeTab >= 0 &&
                    st.activeTab < (int)st.docTabs.size())
                    asmPath = st.docTabs[st.activeTab].path;
                vpCompAbsPath = resolveVpComponentPath(srcPath, asmPath);
            }
        }

        // Part-body slot under selection (for Hide/Show/Isolate on solid bodies).
        int vpBodySlot = -1;
        uint32_t vpBodyId = 0;
        bool vpBodyHidden = false;
        if (!st.selection.empty()) {
            vpBodyId = st.selection.items().back().hit.body_id;
            for (int i = 0; i < (int)st.docBodyIds.size(); ++i) {
                if (st.docBodyIds[i] == vpBodyId) {
                    vpBodySlot = i;
                    if (i < (int)st.bodyVisible.size())
                        vpBodyHidden = (st.bodyVisible[i] == 0);
                    break;
                }
            }
        }
        const bool vpHasTopoSel = !st.selection.empty() && st.selection.items().back().hit.valid();
        const bool vpHasFace = vpHasTopoSel &&
            st.selection.items().back().hit.kind == render::PickKind::Face;
        const bool vpHasEdge = vpHasTopoSel &&
            st.selection.items().back().hit.kind == render::PickKind::Edge;
        const bool vpHasComp = vpComp && !vpCompId.empty();
        const bool vpHasBody = vpBodySlot >= 0 && !vpHasComp;

        ui::pushContextMenuStyle();
        if (ImGui::BeginPopup("##vp_ctx")) {
            const std::string vpEditReason = st.agentLockReason(st.docMirror.docId());
            // ---- ITEM 8: selection-dependent entries ----
            if (vpHasComp) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                           ui::palette.textDim[2], 1.0f));
                ImGui::TextUnformatted(vpCompName.c_str());
                ImGui::PopStyleColor();
                const bool pathOk = !vpCompAbsPath.empty();
                if (!pathOk) ImGui::BeginDisabled();
                // ITEM 11: Open Part must open a focused tab. openComponentPart dedups by path
                // and sets requestOpenPath / switchToTab; path resolution uses asm tab path.
                if (ImGui::MenuItem("Open Part")) {
                    if (pathOk) openComponentPart(st, vpCompAbsPath);
                    else st.toast(ui::ToastLevel::Warn, "Component has no resolvable source path");
                }
                if (haveOsFileManager() && ImGui::MenuItem("Open Part Location"))
                    revealInFileManager(vpCompAbsPath);
                std::string modelEditReason = vpEditReason;
                if (pathOk && modelEditReason.empty()) {
                    namespace fs = std::filesystem;
                    std::error_code ec;
                    fs::path wanted = fs::weakly_canonical(vpCompAbsPath, ec);
                    if (ec) wanted = fs::path(vpCompAbsPath).lexically_normal();
                    for (const auto& tab : st.docTabs) {
                        if (tab.path.empty() || !st.docLockedByAgent(tab.docId)) continue;
                        std::error_code tabEc;
                        fs::path have = fs::weakly_canonical(tab.path, tabEc);
                        if (tabEc) have = fs::path(tab.path).lexically_normal();
                        if (have == wanted) { modelEditReason = st.agentLockReason(tab.docId); break; }
                    }
                }
                if (viewportEditMenuItem("Edit Model", modelEditReason) &&
                    !refuseViewportEdit(st, st.docMirror.docId()))
                    st.enterComponentEdit(vpCompId, vpCompAbsPath);
                if (!pathOk) ImGui::EndDisabled();
                if (viewportEditMenuItem("Edit Mates", vpEditReason))
                    st.requestEditComponentMates = vpCompId;
                if (ImGui::MenuItem("Add to AI context")) addComponentContextRef(st, vpCompId, vpCompName);
                if (vpHasTopoSel && (vpHasFace || vpHasEdge) &&
                    ImGui::MenuItem(vpHasFace ? "Add Face to AI context" : "Add Edge to AI context"))
                    addPickContextRef(st, st.selection.items().back().hit);
                if (viewportEditMenuItem(vpCompHidden ? "Show Component" : "Hide Component",
                                         vpEditReason)) {
                    if (st.docMirror.hasDoc() && st.workerStarted &&
                        !refuseViewportEdit(st, st.docMirror.docId())) {
                        nlohmann::json op = {{"op", "asm_edit_component"},
                                             {"id", vpCompId},
                                             {"hidden", !vpCompHidden}};
                        auto resp = st.docMirror.applyOps(st.worker,
                                                          nlohmann::json::array({op}));
                        if (resp.ok && !resp.aicm_path.empty()) {
                            std::string lerr;
                            st.loadDocMesh(resp.aicm_path, lerr);
                            if (!lerr.empty()) st.addLog("[err] loadDocMesh: " + lerr);
                        } else if (!resp.ok) {
                            st.addLog("[err] asm_edit_component: " + resp.error_message);
                        }
                    }
                }
                ImGui::Separator();
            }

            // Face/edge/body selection actions (part or non-component body).
            if (vpHasTopoSel && !vpHasComp) {
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                           ui::palette.textDim[2], 1.0f));
                if (!st.pickStatus.empty())
                    ImGui::TextUnformatted(st.pickStatus.c_str());
                else
                    ImGui::TextUnformatted(vpHasFace ? "Face" : (vpHasEdge ? "Edge" : "Body"));
                ImGui::PopStyleColor();

                // Edit owning feature when we know it.
                if (!st.selectedFeatureId.empty() && st.propertyManager) {
                    if (ImGui::MenuItem("Edit Feature")) {
                        if (st.propertyManager->openForEdit(st, st.selectedFeatureId))
                            st.leftPaneTab = AppState::LP_PropertyManager;
                    }
                }

                // Wave4: push current topo selection into chat context chips (no pick mode needed).
                if (ImGui::MenuItem("Add to AI context"))
                    addPickContextRef(st, st.selection.items().back().hit);

                // Hide / Show / Isolate body (host-side bodyVisible, same as FM Solid Bodies).
                if (vpHasBody) {
                    if (ImGui::MenuItem(vpBodyHidden ? "Show Body" : "Hide Body")) {
                        if (vpBodySlot >= 0 && vpBodySlot < (int)st.bodyVisible.size()) {
                            st.bodyVisible[vpBodySlot] = vpBodyHidden ? 1 : 0;
                            if (!vpBodyHidden) {
                                // Drop selection on the body we just hid.
                                st.selection.clear();
                            }
                            st.applyBodyVisibility();
                        }
                    }
                    if (ImGui::MenuItem("Isolate")) {
                        bool othersAllHidden = true;
                        for (int j = 0; j < (int)st.bodyVisible.size(); ++j)
                            if (j != vpBodySlot && st.bodyVisible[j]) {
                                othersAllHidden = false; break;
                            }
                        if (othersAllHidden) {
                            // Already isolated -> Exit Isolate = Show All.
                            std::fill(st.bodyVisible.begin(), st.bodyVisible.end(), (uint8_t)1);
                        } else {
                            for (int j = 0; j < (int)st.bodyVisible.size(); ++j)
                                st.bodyVisible[j] = (j == vpBodySlot) ? 1 : 0;
                        }
                        st.applyBodyVisibility();
                    }
                }

                if (ImGui::MenuItem("Clear Selection")) st.selection.clear();
                // C26: recolor the selected body.
                if (!vpBodyKey.empty()) {
                    if (viewportEditMenuItem("Appearance...", vpEditReason)) {
                        s_vpKey = vpBodyKey;
                        s_vpDocId = st.docMirror.docId();
                        s_vpColor[0] = 0.71f; s_vpColor[1] = 0.76f; s_vpColor[2] = 0.82f;
                        s_vpMetallic = 0.0f; s_vpRoughness = 0.5f;
                        s_openVpPicker = true;
                    }
                    if (viewportEditMenuItem("Clear Appearance", vpEditReason))
                        applyAppearanceFromViewport(st, st.docMirror.docId(), vpBodyKey, nullptr);
                }
                ImGui::Separator();
            } else if (!vpHasComp && !st.pickStatus.empty()) {
                // Hover status without a resolvable body (rare) — still offer clear.
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(ui::palette.textDim[0], ui::palette.textDim[1],
                           ui::palette.textDim[2], 1.0f));
                ImGui::TextUnformatted(st.pickStatus.c_str());
                ImGui::PopStyleColor();
                if (ImGui::MenuItem("Clear Selection")) st.selection.clear();
                ImGui::Separator();
            }

            // Empty-space / always-available view commands.
            if (ImGui::BeginMenu("View Orientation")) {
                struct { const char* lbl; ViewPreset p; } prows[] = {
                    {"Front",    ViewPreset::Front},
                    {"Back",     ViewPreset::Back},
                    {"Left",     ViewPreset::Left},
                    {"Right",    ViewPreset::Right},
                    {"Top",      ViewPreset::Top},
                    {"Bottom",   ViewPreset::Bottom},
                    {"Isometric",ViewPreset::Isometric},
                };
                for (const auto& r : prows)
                    if (ImGui::MenuItem(r.lbl)) applyViewPreset(st, r.p);
                ImGui::Separator();
                if (ImGui::MenuItem("Normal To (Ctrl+8)"))
                    normalToSelectionOrNearestAxis(st, inSketch);
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Zoom to Fit (F)"))   applyViewPreset(st, ViewPreset::ZoomFit);
            {
                bool hasSelection = !st.selection.empty();
                if (!hasSelection) ImGui::BeginDisabled();
                if (ImGui::MenuItem("Zoom to Selection (Shift+F)")) zoomToSelection(st);
                if (!hasSelection) ImGui::EndDisabled();
            }
            if (ImGui::MenuItem("Zoom to Area")) {
                s_zoomAreaArmed    = true;
                s_zoomAreaDragging = false;
            }
            if (ImGui::BeginMenu("Display Style")) {
                struct { const char* lbl; int val; } drows[] = {
                    {"Shaded",                0},
                    {"Shaded with Edges",     1},
                    {"Hidden Lines Removed",  2},
                    {"Hidden Lines Visible",  3},
                    {"Wireframe",             4},
                };
                for (const auto& dr : drows) {
                    if (ImGui::MenuItem(dr.lbl, nullptr, st.displayStyle == dr.val)) {
                        st.displayStyle = dr.val;
                        renderer.setDisplayStyle(static_cast<DisplayStyle>(dr.val));
                    }
                }
                ImGui::EndMenu();
            }
            if (ImGui::MenuItem("Section View", nullptr, s_sectionTool.active()))
                s_sectionTool.toggle(st);
            // Hide/show references — empty-space menu and always available.
            if (ImGui::BeginMenu("Hide / Show")) {
                bool allPlanes = st.planeVisEff[0] && st.planeVisEff[1] && st.planeVisEff[2];
                if (ImGui::MenuItem("Reference Planes", nullptr, allPlanes)) {
                    bool nv = !allPlanes;
                    st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = nv;
                    st.planesUserToggled = true;
                }
                ImGui::MenuItem("Origin Triad",  nullptr, &st.showTriad);
                ImGui::MenuItem("Ground Shadow", nullptr, &st.showGroundShadow);
                ImGui::MenuItem("RealView reflections", nullptr, &st.showRealView);
                // Show All Bodies when any body is hidden.
                bool anyBodyHidden = false;
                for (uint8_t v : st.bodyVisible) if (!v) { anyBodyHidden = true; break; }
                if (anyBodyHidden && ImGui::MenuItem("Show All Bodies")) {
                    std::fill(st.bodyVisible.begin(), st.bodyVisible.end(), (uint8_t)1);
                    st.applyBodyVisibility();
                }
                ImGui::EndMenu();
            }
            ImGui::Separator();
            if (ImGui::MenuItem("View Selector (Space)")) {
                if (!s_viewSelector.active()) s_viewSelector.open();
            }
            ImGui::EndPopup();
        }
        ui::popContextMenuStyle();

        // C26: appearance picker — a SIBLING popup of vp_ctx (opened from its menu item) to avoid
        // nested-popup ordering issues. Direct-applies on commit (safe outside tree iteration).
        // Appearance ribbon "Appearance..." -> arm this picker for the whole model (body "b1").
        if (st.requestAppearanceEditor) {
            st.requestAppearanceEditor = false;
            if (!refuseViewportEdit(st, st.docMirror.docId())) {
                s_vpKey = "b1";
                s_vpDocId = st.docMirror.docId();
                s_vpColor[0] = 0.71f; s_vpColor[1] = 0.76f; s_vpColor[2] = 0.82f;
                s_vpMetallic = 0.0f; s_vpRoughness = 0.5f;
                s_openVpPicker = true;
            }
        }
        if (s_openVpPicker) { ImGui::OpenPopup("##appearance_vp"); s_openVpPicker = false; }
        const std::string appearanceDisabledReason = !ImGui::IsPopupOpen("##appearance_vp")
            ? std::string()
            : st.docMirror.docId() != s_vpDocId
                ? "Switch back to the part that opened this appearance editor."
                : st.agentLockReason(s_vpDocId);
        if (appearanceColorPicker("##appearance_vp", s_vpColor, &s_vpMetallic, &s_vpRoughness,
                                  appearanceDisabledReason.c_str())
            && !s_vpKey.empty())
            applyAppearanceFromViewport(st, s_vpDocId, s_vpKey, s_vpColor,
                                        s_vpMetallic, s_vpRoughness);

        // ---- in-context component-edit banner (SolidWorks "Edit Component") ----
        // Drawn as a true viewport HUD on the visible 3D surface — right of the FeatureManager
        // glass column, below the floating doc-tab strip — same dodge pattern as the heads-up
        // view toolbar / orientation triad. The early BeginChild at content origin sat under the
        // FM header and was clipped. Compact auto-width glass pill; button exits component edit.
        if (st.inComponentEdit()) {
            const float s = ui::uiScale;
            const auto& palC = ui::palette;
            const std::string& cid = st.componentEdit.componentId;
            const char* label = cid.empty() ? "(component)" : cid.c_str();

            // Left edge: just past the FM pinned column (fallback to content left / cr0).
            float bx0 = cr0.x + 10.0f * s;
            {
                const float fe = ui::overlayColumnEdge(0);
                if (fe > cr0.x) bx0 = fe + 10.0f * s;
            }
            // Top edge: below the doc-tab file strip.
            float by0 = cr0.y + 6.0f * s;
            {
                const float dtb = ui::docTabBandBottom();
                if (dtb > by0) by0 = dtb + 6.0f * s;
            }
            // Never run under the Copilot column — clamp max width to its left edge.
            float maxRight = cr1.x - 10.0f * s;
            {
                const float e = ui::overlayColumnEdge(1);
                if (e > bx0 + 80.0f * s && e < cr1.x) maxRight = e - 10.0f * s;
            }
            const float maxW = std::max(1.0f, maxRight - bx0);
            const float pillW = std::min(300.0f * s, maxW);

            ImGui::SetCursorScreenPos(ImVec2(bx0, by0));
            ImGui::PushStyleColor(ImGuiCol_ChildBg,
                ImVec4(palC.overlayBg[0], palC.overlayBg[1], palC.overlayBg[2], palC.overlayBg[3]));
            ImGui::PushStyleColor(ImGuiCol_Border,
                ImVec4(palC.accent[0], palC.accent[1], palC.accent[2], 0.55f));
            ImGui::BeginChild("##compEditHud", ImVec2(pillW, 0),
                              ImGuiChildFlags_Border | ImGuiChildFlags_AutoResizeY,
                              ImGuiWindowFlags_NoScrollbar);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palC.overlayText[0], palC.overlayText[1], palC.overlayText[2], 1.0f));
            ImGui::Text("Editing model: %s", label);
            ImGui::PopStyleColor();
            ImGui::Spacing();
            ImGui::PushStyleColor(ImGuiCol_Button,
                ImVec4(palC.accentDim[0], palC.accentDim[1], palC.accentDim[2], 1.0f));
            ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
                ImVec4(palC.accent[0], palC.accent[1], palC.accent[2], 1.0f));
            if (ImGui::Button("Return to assembly", ImVec2(0, 0)))
                st.exitComponentEdit();
            ImGui::PopStyleColor(2);
            ImGui::EndChild();
            ImGui::PopStyleColor(2);
        }

        // ---- C25 W3-J: heads-up view toolbar (SolidWorks-style, fully functional) ----
        // Top-right row-only glass pills; fades when idle so the central workspace stays clear.
        // Groups: [zoom-fit | zoom-area | pan | rotate] [view-orientation ▾] [display-style ▾]
        //         [hide/show ▾] [section]
        if (!inSketch) {
            const auto& hpal = ui::palette;
            ImDrawList* hdl = ImGui::GetWindowDrawList();
            const float s   = ui::uiScale;
            float btn = 22.0f * s, gap = 2.0f * s, padX = 3.0f * s, padY = 2.0f * s;

            const int nHU = 8;
            float barW = nHU * btn + (nHU - 1) * gap + padX * 2;
            float barH = btn + padY * 2;
            // Anchor LEFT of the pinned Copilot column — the raw cr1-relative anchor put the
            // toolbar underneath the floating glass column, unreachable (visual-review fix).
            // Right edge = min(viewport rect, draw-list clip rect, Copilot column edge). The
            // viewport rect spans under the pinned columns while this window's clip rect ends at
            // the visible viewport edge, so anchoring to cr1.x/column edge alone let the last pill
            // be clipped (visual audit). Margin matches the left-side overlay margin (10*s).
            float hudRight = std::min(cr1.x, hdl->GetClipRectMax().x);
            {
                const float e = ui::overlayColumnEdge(1);
                if (e > cr0.x + barW + 40.0f * s && e < hudRight) hudRight = e;
            }
            float bx0  = hudRight - barW - 10.0f * s;
            // Never let the toolbar slide under the FeatureManager column either (left clamp), so its
            // icons can't overlap the pinned columns' text on any window width / panel size.
            {
                const float fe = ui::overlayColumnEdge(0);
                if (fe > cr0.x && bx0 < fe + 10.0f * s) bx0 = fe + 10.0f * s;
            }
            float by0  = cr0.y + 6.0f * s;
            // Drop the toolbar below the doc-tab file strip that overlays the top of the viewport, so
            // the file display never clips over these buttons. docTabBandBottom() is the
            // strip's live bottom edge in screen space (-1 when no strip this frame).
            {
                const float dtb = ui::docTabBandBottom();
                if (dtb > by0) by0 = dtb + 6.0f * s;
            }
            float byi  = by0 + padY;

            ImVec2 mp = io.MousePos;
            ImRect hudZone(ImVec2(bx0 - 14.0f * s, by0 - 10.0f * s),
                           ImVec2(bx0 + barW + 10.0f * s, by0 + barH + 10.0f * s));
            const bool hudHot = hudZone.Contains(mp) || s_zoomAreaArmed ||
                ImGui::IsPopupOpen("##hu_vieworient") || ImGui::IsPopupOpen("##hu_dispstyle") ||
                ImGui::IsPopupOpen("##hu_hideshow");
            // Idle floor raised 0.20 -> 0.38 (contrast audit P1): at 0.20 the idle icons landed at
            // ~25% alpha and vanished over busy/contrasty models. The icon floor and a fixed halo
            // minimum below keep the resting toolbar findable over ANY backdrop.
            const float targetFade = hudHot ? 1.0f : 0.38f;
            s_huFade += (targetFade - s_huFade) * std::min(1.0f, io.DeltaTime * 10.0f);
            const float pillA = 0.38f * s_huFade;
            const int iconA = (int)(200.0f * s_huFade + 55.0f);
            // Halo never fades below half strength — it's the rescue exactly when idle.
            const float haloA = std::max(s_huFade, 0.5f);

            // Liquid-glass v2 (H6a): per-pill AdaptiveSurface (id = panelId(iconName), window-
            // independent) drives both the frosted UltraThin glassPanel AND the icon/halo tone, so
            // the toolbar's contrast tracks the live viewport content instead of a fixed palette
            // tint. Returns the surface so callers can tint their own icon draw to match.
            auto huPill = [&](float bxi, float by, bool selected,
                              const char* iconName) -> const ui::AdaptiveSurface& {
                ImVec2 mn(bxi, by), mx(bxi + btn, by + btn);
                const ui::AdaptiveSurface& surf =
                    ui::adaptivePanel(ui::panelId(iconName), ImRect(mn, mx), ui::GlassGroup::Hud);
                ui::glassPanel(hdl, ImRect(mn, mx), ui::GlassLevel::UltraThin, 4.0f, surf,
                              s_huFade * (selected ? 1.0f : 0.85f));
                if (selected && s_huFade > 0.55f)
                    ui::glassRowBackground(hdl, mn, mx, 4.0f, false, true, 0.42f * s_huFade);
                return surf;
            };

            // Helper: draw one toolbar icon button. Returns true on click.
            auto huBtn = [&](int idx, const char* iconName, const char* tip, bool active) -> bool {
                float bxi = bx0 + padX + (float)idx * (btn + gap);
                const ui::AdaptiveSurface& surf = huPill(bxi, byi, active, iconName);
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(idx);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov && !active && pillA > 0.04f) {
                    float hovCol[4]; Palette::withAlpha(hpal.buttonHovered, hovCol, pillA * 0.65f);
                    hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                       Palette::toU32(hovCol), 3.0f * s);
                }
                float iz = 16.0f * s;
                ImVec4 iconCol = surf.textPrimary; iconCol.w *= (iconA / 255.0f);
                ui::drawIconGlass(hdl, iconName, ImVec2(bxi + btn * 0.5f, byi + btn * 0.5f), iz,
                                  ImGui::ColorConvertFloat4ToU32(iconCol),
                                  ui::glassIconHalo(surf, haloA));
                if (hov) ImGui::SetTooltip("%s", tip);
                ImGui::PopID();
                return clicked;
            };
            // Separator tick between groups.
            auto huSep = [&](int afterIdx) {
                if (s_huFade < 0.12f) return;
                float bxi = bx0 + padX + (float)afterIdx * (btn + gap);
                float sepA = s_huFade * 0.55f;
                float scol[4]; Palette::withAlpha(hpal.border, scol, sepA);
                hdl->AddLine(ImVec2(bxi + btn + gap * 0.5f, byi + 3 * s),
                             ImVec2(bxi + btn + gap * 0.5f, byi + btn - 3 * s),
                             Palette::toU32(scol), 1.0f);
            };

            ImGui::PushID("##headsup");

            // [0] Zoom to Fit
            if (huBtn(0, "zoom_fit", "Zoom to Fit (F)", false))
                applyViewPreset(st, ViewPreset::ZoomFit);

            // [1] Zoom Area (toggle; left-drag = rubber-band, Esc disarms)
            if (huBtn(1, "zoom_area", "Zoom to Area", s_zoomAreaArmed)) {
                s_zoomAreaArmed   = !s_zoomAreaArmed;
                s_zoomAreaDragging = false;
            }

            // [2] Pan mode toggle
            if (huBtn(2, "pan_view", "Pan (left-drag while active)", st.vpDragMode == 1))
                st.vpDragMode = (st.vpDragMode == 1) ? 0 : 1;

            // [3] Rotate mode toggle (default)
            if (huBtn(3, "rotate_view", "Rotate (left-drag)", st.vpDragMode == 0))
                st.vpDragMode = 0;

            huSep(3);  // ---- separator: navigation | orientation

            // [4] View orientation dropdown ▾
            {
                float bxi = bx0 + padX + 4.0f * (btn + gap);
                const ui::AdaptiveSurface& surf = huPill(bxi, byi, false, "view_selector");
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(4);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov && pillA > 0.04f) {
                    float hovCol[4]; Palette::withAlpha(hpal.buttonHovered, hovCol, pillA * 0.65f);
                    hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                       Palette::toU32(hovCol), 3.0f);
                }
                {
                    float iz = 16.0f * s;
                    ImVec4 iconCol = surf.textPrimary; iconCol.w *= (iconA / 255.0f);
                    ui::drawIconGlass(hdl, "view_selector",
                                      ImVec2(bxi + btn * 0.5f, byi + btn * 0.5f), iz,
                                      ImGui::ColorConvertFloat4ToU32(iconCol),
                                      ui::glassIconHalo(surf, haloA));
                }
                if (hov) ImGui::SetTooltip("View Orientation");
                if (clicked) ImGui::OpenPopup("##hu_vieworient");
                ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.0f * s), ImGuiCond_Always);
                if (ImGui::BeginPopup("##hu_vieworient")) {
                    struct { const char* label; ViewPreset p; } vrows[] = {
                        {"Front",    ViewPreset::Front},
                        {"Back",     ViewPreset::Back},
                        {"Left",     ViewPreset::Left},
                        {"Right",    ViewPreset::Right},
                        {"Top",      ViewPreset::Top},
                        {"Bottom",   ViewPreset::Bottom},
                        {"Isometric",ViewPreset::Isometric},
                    };
                    for (const auto& r : vrows) {
                        if (ImGui::MenuItem(r.label)) applyViewPreset(st, r.p);
                    }
                    ImGui::Separator();
                    // C37-C item 1: Normal To entry in HUD view-orientation dropdown.
                    if (ImGui::MenuItem("Normal To (Ctrl+8)"))
                        normalToSelectionOrNearestAxis(st, inSketch);
                    ImGui::Separator();
                    // C37-C item 2: Zoom to Selection entry in HUD view dropdown.
                    {
                        bool hasSelection = !st.selection.empty();
                        if (!hasSelection) ImGui::BeginDisabled();
                        if (ImGui::MenuItem("Zoom to Selection (Shift+F)")) zoomToSelection(st);
                        if (!hasSelection) ImGui::EndDisabled();
                    }
                    ImGui::Separator();
                    // C37-C item 3: wire View Selector to actually open the selector widget.
                    if (ImGui::MenuItem("View Selector (Space)")) {
                        if (!s_viewSelector.active()) s_viewSelector.openFramed(st);
                    }
                    ImGui::EndPopup();
                }
                ImGui::PopID();
            }

            huSep(4);  // ---- separator: orientation | display

            // [5] Display style dropdown ▾
            {
                // Icon reflects current style.
                static const char* kDsIcons[] = {
                    "display_shaded", "display_shaded_edges", "display_hlr",
                    "display_hlv",    "display_wireframe"
                };
                const char* curIcon = kDsIcons[st.displayStyle < 5 ? st.displayStyle : 1];
                float bxi = bx0 + padX + 5.0f * (btn + gap);
                const ui::AdaptiveSurface& surf = huPill(bxi, byi, false, curIcon);
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(5);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov && pillA > 0.04f) {
                    float hovCol[4]; Palette::withAlpha(hpal.buttonHovered, hovCol, pillA * 0.65f);
                    hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                       Palette::toU32(hovCol), 3.0f);
                }
                {
                    float iz = 16.0f * s;
                    ImVec4 iconCol = surf.textPrimary; iconCol.w *= (iconA / 255.0f);
                    ui::drawIconGlass(hdl, curIcon, ImVec2(bxi + btn * 0.5f, byi + btn * 0.5f), iz,
                                      ImGui::ColorConvertFloat4ToU32(iconCol),
                                      ui::glassIconHalo(surf, haloA));
                }
                if (hov) ImGui::SetTooltip("Display Style");
                if (clicked) ImGui::OpenPopup("##hu_dispstyle");
                ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.0f * s), ImGuiCond_Always);
                if (ImGui::BeginPopup("##hu_dispstyle")) {
                    struct { const char* icon; const char* label; int val; } drows[] = {
                        {"display_shaded",       "Shaded",                0},
                        {"display_shaded_edges", "Shaded with Edges",     1},
                        {"display_hlr",          "Hidden Lines Removed",  2},
                        {"display_hlv",          "Hidden Lines Visible",  3},
                        {"display_wireframe",    "Wireframe",             4},
                    };
                    for (const auto& dr : drows) {
                        IconUV div = ui::getIcon(dr.icon, 20);
                        ImGui::Image(div.texId, ImVec2(16.0f * s, 16.0f * s), div.uv0, div.uv1);
                        ImGui::SameLine(0, 6.0f * s);
                        bool sel = (st.displayStyle == dr.val);
                        if (sel) ImGui::PushStyleColor(ImGuiCol_Text,
                            ImVec4(hpal.accent[0], hpal.accent[1], hpal.accent[2], 1.0f));
                        if (ImGui::MenuItem(dr.label, nullptr, sel)) {
                            st.displayStyle = dr.val;
                            renderer.setDisplayStyle(static_cast<DisplayStyle>(dr.val));
                        }
                        if (sel) ImGui::PopStyleColor();
                    }
                    ImGui::EndPopup();
                }
                ImGui::PopID();
            }

            // [6] Hide/Show items dropdown ▾
            {
                float bxi = bx0 + padX + 6.0f * (btn + gap);
                const ui::AdaptiveSurface& surf = huPill(bxi, byi, false, "hide_show_eye");
                ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
                ImGui::PushID(6);
                bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
                bool hov = ImGui::IsItemHovered();
                if (hov && pillA > 0.04f) {
                    float hovCol[4]; Palette::withAlpha(hpal.buttonHovered, hovCol, pillA * 0.65f);
                    hdl->AddRectFilled(ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                       Palette::toU32(hovCol), 3.0f);
                }
                {
                    float iz = 16.0f * s;
                    ImVec4 iconCol = surf.textPrimary; iconCol.w *= (iconA / 255.0f);
                    ui::drawIconGlass(hdl, "hide_show_eye",
                                      ImVec2(bxi + btn * 0.5f, byi + btn * 0.5f), iz,
                                      ImGui::ColorConvertFloat4ToU32(iconCol),
                                      ui::glassIconHalo(surf, haloA));
                }
                if (hov) ImGui::SetTooltip("Hide / Show Items");
                if (clicked) ImGui::OpenPopup("##hu_hideshow");
                ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.0f * s), ImGuiCond_Always);
                if (ImGui::BeginPopup("##hu_hideshow")) {
                    bool allPlanes = st.planeVisEff[0] && st.planeVisEff[1] && st.planeVisEff[2];
                    if (ImGui::MenuItem("Reference Planes", nullptr, allPlanes)) {
                        bool nv = !allPlanes;
                        st.showPlane[0] = st.showPlane[1] = st.showPlane[2] = nv;
                        st.planesUserToggled = true;
                    }
                    ImGui::MenuItem("Origin Triad",    nullptr, &st.showTriad);
                    ImGui::MenuItem("Ground Shadow",   nullptr, &st.showGroundShadow);
                    ImGui::MenuItem("RealView reflections", nullptr, &st.showRealView);
                    ImGui::EndPopup();
                }
                ImGui::PopID();
            }

            huSep(6);  // ---- separator: hide/show | section

            // [7] Section view toggle
            if (huBtn(7, "section_view", "Section View", s_sectionTool.active()))
                s_sectionTool.toggle(st);

            ImGui::PopID();

            // ---- C25 W3-J: zoom-area rubber-band rect overlay ----
            if (s_zoomAreaDragging) {
                float rx0 = s_zoomAreaP0.x < s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                float ry0 = s_zoomAreaP0.y < s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                float rx1 = s_zoomAreaP0.x > s_zoomAreaP1.x ? s_zoomAreaP0.x : s_zoomAreaP1.x;
                float ry1 = s_zoomAreaP0.y > s_zoomAreaP1.y ? s_zoomAreaP0.y : s_zoomAreaP1.y;
                hdl->AddRectFilled(ImVec2(rx0, ry0), ImVec2(rx1, ry1),
                                   ui::Palette::toU32(hpal.accentSoft));
                hdl->AddRect(ImVec2(rx0, ry0), ImVec2(rx1, ry1),
                             ui::Palette::toU32(hpal.accent), 1.5f);
            }
        }

        // ---- C25 W2-E: glass-box View Selector (Space). Drawn after the heads-up toolbar so its
        // orientation popup sits just below it; uses the same window draw list. While active it
        // suppresses normal click-picking (guard added on the picking click above).
        // The selector now opens in any edit/feature state (sketch, PM, plane-pick). When it drives
        // the camera while a sketch is open, latch s_sketchRotated so the normal-to lock yields and
        // the picked orientation sticks (matches a manual orbit). Check active() both before and
        // after update() so the frame the user CLICKS a face — which closes the selector inside
        // update() — still latches.
        bool selActiveBefore = s_viewSelector.active();
        s_viewSelector.update(st, renderer, imgHovered, inSketch, vr0, vr1);
        if (inSketch && (selActiveBefore || s_viewSelector.active())) s_sketchRotated = true;
        s_viewSelector.draw(st, ImGui::GetWindowDrawList(), vr0, vr1);

        // ---- C25 W3-I: Section View tool. Pushes the clip plane to the renderer each frame and
        // draws the PM panel + in-viewport plane/arrow manipulator. Auto-suspends while sketching.
        s_sectionTool.updateAndDraw(st, renderer, imgHovered, inSketch, vr0, vr1);

        // ---- overlays on the 3D view ----
        ImDrawList* vdl = ImGui::GetWindowDrawList();
        {
            // Palette handle for this frame's overlays (all pills are glass-v2 adaptive now; the
            // legacy overlayBg/overlayText ImU32 cache is gone with its last consumers).
            const auto& vpal = ui::palette;

        if (busy) {
            // live progress pill: pulsing dot + the agent's current phase
            std::string lt = st.livePhase.empty() ? std::string("Working...") : st.livePhase;
            ImVec2 ts = ImGui::CalcTextSize(lt.c_str());
            float maxW = (cr1.x - cr0.x) - 58.0f;
            // Anchor the pill's left edge just RIGHT of the FeatureManager column (like the
            // doc-tab strip: fmEdge + margin) so "Working..." lands under the active file tabs
            // instead of ghosting behind the FM toolbar buttons at the viewport's left edge.
            float busyLeft = cr0.x + 8 * ui::uiScale;
            {
                const float fe = ui::overlayColumnEdge(0);
                if (fe > cr0.x) busyLeft = fe + 12.0f * ui::uiScale;
            }
            // Drop below the doc-tab file strip so the pill sits UNDER the active file display,
            // not on the same row as the tabs (mirrors the heads-up toolbar's docTabBandBottom
            // dodge). docTabBandBottom() is the strip's live bottom edge (-1 when no strip).
            float busyTop = cr0.y + 8 * ui::uiScale;
            {
                const float dtb = ui::docTabBandBottom();
                if (dtb > busyTop) busyTop = dtb + 6.0f * ui::uiScale;
            }
            ImVec2 p0(busyLeft, busyTop);
            ImVec2 p1(p0.x + (ts.x > maxW ? maxW : ts.x) + 28.0f * ui::uiScale, p0.y + ts.y + 8.0f * ui::uiScale);
            const ui::AdaptiveSurface& busySurf =
                ui::adaptivePanel(ui::panelId("vp_busy"), ImRect(p0, p1), ui::GlassGroup::Hud);
            ui::glassPanel(vdl, ImRect(p0, p1), ui::GlassLevel::UltraThin,
                          (p1.y - p0.y) * 0.5f / ui::uiScale, busySurf);
            float pulse = 0.35f + 0.65f * (0.5f + 0.5f * std::sin((float)ImGui::GetTime() * 5.0f));
            vdl->AddCircleFilled(ImVec2(p0.x + 11 * ui::uiScale, (p0.y + p1.y) * 0.5f), 4.0f * ui::uiScale,
                                 ImGui::ColorConvertFloat4ToU32(
                                     ImVec4(vpal.accent[0], vpal.accent[1],
                                            vpal.accent[2], pulse)));
            vdl->PushClipRect(p0, ImVec2(p1.x - 6 * ui::uiScale, p1.y), true);
            ui::glassText(vdl, nullptr, ImGui::GetFontSize(),
                         ImVec2(p0.x + 19 * ui::uiScale, p0.y + 4 * ui::uiScale), busySurf, lt.c_str());
            vdl->PopClipRect();
        }
        if (st.hasDocMesh && !st.pickStatus.empty()) {
            // pick readout, top-right ("Face 12 of Body1") — anchored LEFT of the pinned Copilot
            // column, like the heads-up toolbar, so the text never ghosts under the glass column.
            ImVec2 ts = ImGui::CalcTextSize(st.pickStatus.c_str());
            const float s2 = ui::uiScale;
            float pickRight = cr1.x;
            {
                const float e = ui::overlayColumnEdge(1);
                if (e > cr0.x + ts.x + 60.0f * s2 && e < cr1.x) pickRight = e;
            }
            // Below the heads-up toolbar row (btn 22 + pad 2*2 + inset 6) when that row is drawn,
            // else at the top inset — the two used to stack on the same corner and the readout
            // ghosted the toolbar icons.
            ImVec2 p1(pickRight - 8 * s2, cr0.y + (inSketch ? 8.0f : 40.0f) * s2);
            ImVec2 p0(p1.x - ts.x - 14.0f * s2, p1.y);
            ImVec2 p1b(p1.x, p1.y + ts.y + 8.0f * s2);
            const ui::AdaptiveSurface& pickSurf =
                ui::adaptivePanel(ui::panelId("vp_pick_status"), ImRect(p0, p1b), ui::GlassGroup::Hud);
            ui::glassPanel(vdl, ImRect(p0, p1b), ui::GlassLevel::UltraThin, 5.0f, pickSurf);
            ui::glassText(vdl, nullptr, ImGui::GetFontSize(),
                         ImVec2(p0.x + 7 * s2, p0.y + 4 * s2), pickSurf, st.pickStatus.c_str());
        }

        // Evaluate Measure / Mass Properties labels (world → screen).
        if (st.evaluateDialogs && !st.evaluateDialogs->overlayLabels().empty()) {
            const float* m = renderer.camera().mvp.m;
            auto project = [&](float wx, float wy, float wz, ImVec2& out) -> bool {
                float x = m[0] * wx + m[4] * wy + m[8] * wz + m[12];
                float y = m[1] * wx + m[5] * wy + m[9] * wz + m[13];
                float wc = m[3] * wx + m[7] * wy + m[11] * wz + m[15];
                if (wc <= 1e-4f) return false;
                out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
                out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
                return true;
            };
            const auto& vpal2 = ui::palette;
            for (const auto& lab : st.evaluateDialogs->overlayLabels()) {
                ImVec2 sp;
                if (!project(lab.first[0], lab.first[1], lab.first[2], sp)) continue;
                ImVec2 ts = ImGui::CalcTextSize(lab.second.c_str());
                ImVec2 q0(sp.x - ts.x * 0.5f - 6.0f * ui::uiScale,
                          sp.y - ts.y - 8.0f * ui::uiScale);
                ImVec2 q1(q0.x + ts.x + 12.0f * ui::uiScale, q0.y + ts.y + 6.0f * ui::uiScale);
                const ui::AdaptiveSurface& ls =
                    ui::adaptivePanel(ui::panelId("vp_eval_lab"), ImRect(q0, q1),
                                      ui::GlassGroup::Hud);
                ui::glassPanel(vdl, ImRect(q0, q1), ui::GlassLevel::UltraThin, 4.0f, ls);
                ui::glassText(vdl, nullptr, ImGui::GetFontSize(),
                              ImVec2(q0.x + 6.0f * ui::uiScale, q0.y + 3.0f * ui::uiScale), ls,
                              lab.second.c_str());
                (void)vpal2;
            }
        }
        // Evaluate Measure colored callouts (world → screen glass chips in ref color).
        if (st.evaluateDialogs && !st.evaluateDialogs->overlayCallouts().empty()) {
            const float* m = renderer.camera().mvp.m;
            auto project = [&](float wx, float wy, float wz, ImVec2& out) -> bool {
                float x = m[0] * wx + m[4] * wy + m[8] * wz + m[12];
                float y = m[1] * wx + m[5] * wy + m[9] * wz + m[13];
                float wc = m[3] * wx + m[7] * wy + m[11] * wz + m[15];
                if (wc <= 1e-4f) return false;
                out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
                out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
                return true;
            };
            for (const auto& co : st.evaluateDialogs->overlayCallouts()) {
                if (co.text.empty()) continue;
                ImVec2 sp;
                if (!project(co.pos[0], co.pos[1], co.pos[2], sp)) continue;
                ImVec2 ts = ImGui::CalcTextSize(co.text.c_str());
                ImVec2 q0(sp.x - ts.x * 0.5f - 6.0f * ui::uiScale,
                          sp.y - ts.y - 8.0f * ui::uiScale);
                ImVec2 q1(q0.x + ts.x + 12.0f * ui::uiScale, q0.y + ts.y + 6.0f * ui::uiScale);
                const ui::AdaptiveSurface& ls =
                    ui::adaptivePanel(ui::panelId("vp_eval_callout"), ImRect(q0, q1),
                                      ui::GlassGroup::Hud);
                ui::glassPanel(vdl, ImRect(q0, q1), ui::GlassLevel::UltraThin, 4.0f, ls);
                // Colored border matching the measure ref palette.
                ImU32 bord = IM_COL32((int)(co.rgba[0] * 255), (int)(co.rgba[1] * 255),
                                     (int)(co.rgba[2] * 255), (int)(0.95f * 255));
                ImU32 fill = IM_COL32((int)(co.rgba[0] * 255), (int)(co.rgba[1] * 255),
                                     (int)(co.rgba[2] * 255), (int)(0.28f * 255));
                vdl->AddRectFilled(q0, q1, fill, 4.0f * ui::uiScale);
                vdl->AddRect(q0, q1, bord, 4.0f * ui::uiScale, 0, 1.5f * ui::uiScale);
                ui::glassText(vdl, nullptr, ImGui::GetFontSize(),
                              ImVec2(q0.x + 6.0f * ui::uiScale, q0.y + 3.0f * ui::uiScale), ls,
                              co.text.c_str());
            }
        }

        // Evaluate Mass Properties value chips (COM + per-plane I/J) — world → screen glass chips
        // with hover tooltips. Independent of Section (both may be open concurrently).
        if (st.evaluateDialogs && !st.evaluateDialogs->massMarkers().empty()) {
            const float* m = renderer.camera().mvp.m;
            auto project = [&](float wx, float wy, float wz, ImVec2& out) -> bool {
                float x = m[0] * wx + m[4] * wy + m[8] * wz + m[12];
                float y = m[1] * wx + m[5] * wy + m[9] * wz + m[13];
                float wc = m[3] * wx + m[7] * wy + m[11] * wz + m[15];
                if (wc <= 1e-4f) return false;
                out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
                out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
                return true;
            };
            for (const auto& mk : st.evaluateDialogs->massMarkers()) {
                if (mk.label.empty()) continue;
                ImVec2 sp;
                if (!project(mk.pos[0], mk.pos[1], mk.pos[2], sp)) continue;
                ImVec2 ts = ImGui::CalcTextSize(mk.label.c_str());
                ImVec2 q0(sp.x - ts.x * 0.5f - 6.0f * ui::uiScale,
                          sp.y - ts.y - 8.0f * ui::uiScale);
                ImVec2 q1(q0.x + ts.x + 12.0f * ui::uiScale, q0.y + ts.y + 6.0f * ui::uiScale);
                const ui::AdaptiveSurface& ls =
                    ui::adaptivePanel(ui::panelId("vp_eval_mass"), ImRect(q0, q1),
                                      ui::GlassGroup::Hud);
                ui::glassPanel(vdl, ImRect(q0, q1), ui::GlassLevel::UltraThin, 4.0f, ls);
                // Marker-color wash + border (matches Measure callout chip language).
                ImU32 bord = IM_COL32((int)(mk.rgba[0] * 255), (int)(mk.rgba[1] * 255),
                                     (int)(mk.rgba[2] * 255), (int)(0.95f * 255));
                ImU32 fill = IM_COL32((int)(mk.rgba[0] * 255), (int)(mk.rgba[1] * 255),
                                     (int)(mk.rgba[2] * 255), (int)(0.28f * 255));
                vdl->AddRectFilled(q0, q1, fill, 4.0f * ui::uiScale);
                vdl->AddRect(q0, q1, bord, 4.0f * ui::uiScale, 0, 1.5f * ui::uiScale);
                ui::glassText(vdl, nullptr, ImGui::GetFontSize(),
                              ImVec2(q0.x + 6.0f * ui::uiScale, q0.y + 3.0f * ui::uiScale), ls,
                              mk.label.c_str());
                if (ImGui::IsMouseHoveringRect(q0, q1) && !mk.tooltip.empty())
                    ImGui::SetTooltip("%s", mk.tooltip.c_str());
            }
        }

        // Evaluate Section Props: prominent per-plane I labels + draggable tip handles.
        // Independent of Mass chips above — both render when both windows are open.
        // World quads/arrows are drawn earlier via overlays; this pass is the HUD chrome + grab.
        if (st.evaluateDialogs && st.evaluateDialogs->sectionDragActive()) {
            const float* m = renderer.camera().mvp.m;
            auto project = [&](float wx, float wy, float wz, ImVec2& out) -> bool {
                float x = m[0] * wx + m[4] * wy + m[8] * wz + m[12];
                float y = m[1] * wx + m[5] * wy + m[9] * wz + m[13];
                float wc = m[3] * wx + m[7] * wy + m[11] * wz + m[15];
                if (wc <= 1e-4f) return false;
                out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
                out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
                return true;
            };
            const float sc = ui::uiScale;
            const float handleHalf = 5.0f * sc;
            // Slightly larger than default HUD so per-plane moment-of-inertia reads at a glance.
            const float fontSz = ImGui::GetFontSize() * 1.14f;
            ImFont* font = ImGui::GetFont();
            for (const auto& pl : st.evaluateDialogs->sectionPlanes()) {
                if (!pl.active) continue;

                // Label chip near plane origin. Dialogs put the section I in pl.label
                // (e.g. "Sec 1  I=…"); fallback names still show while waiting.
                if (!pl.label.empty()) {
                    ImVec2 osp;
                    if (project(pl.origin[0], pl.origin[1], pl.origin[2], osp)) {
                        const float padX = 9.0f * sc;
                        const float padY = 5.0f * sc;
                        ImVec2 ts = font->CalcTextSizeA(fontSz, FLT_MAX, 0.0f, pl.label.c_str());
                        // Fan labels by plane index so three centroid-origin planes don't stack.
                        // Step = measured chip height + margin so larger I strings stay clear.
                        const float chipH = ts.y + padY * 2.0f;
                        const float stepY = chipH + 6.0f * sc;
                        const float nudgeX = (float)(pl.index - 1) * 30.0f * sc;
                        const float nudgeY = -12.0f * sc - (float)pl.index * stepY;
                        osp.x += nudgeX;
                        osp.y += nudgeY;

                        ImVec2 q0(osp.x - ts.x * 0.5f - padX, osp.y - ts.y - padY - 4.0f * sc);
                        ImVec2 q1(q0.x + ts.x + padX * 2.0f, q0.y + ts.y + padY * 2.0f);
                        char panelKey[40];
                        std::snprintf(panelKey, sizeof(panelKey), "vp_eval_sec_lab_%d", pl.index);
                        const ui::AdaptiveSurface& ls =
                            ui::adaptivePanel(ui::panelId(panelKey), ImRect(q0, q1),
                                              ui::GlassGroup::Hud);
                        ui::glassPanel(vdl, ImRect(q0, q1), ui::GlassLevel::UltraThin, 5.0f, ls);
                        // Stronger plane-color wash so I chips read as belonging to each cut.
                        ImU32 bord = IM_COL32((int)(pl.rgba[0] * 255), (int)(pl.rgba[1] * 255),
                                             (int)(pl.rgba[2] * 255), (int)(0.98f * 255));
                        ImU32 fill = IM_COL32((int)(pl.rgba[0] * 255), (int)(pl.rgba[1] * 255),
                                             (int)(pl.rgba[2] * 255), (int)(0.38f * 255));
                        vdl->AddRectFilled(q0, q1, fill, 5.0f * sc);
                        vdl->AddRect(q0, q1, bord, 5.0f * sc, 0, 2.0f * sc);
                        ui::glassText(vdl, font, fontSz,
                                      ImVec2(q0.x + padX, q0.y + padY), ls,
                                      pl.label.c_str());
                    }
                }

                // Draggable handle square at the arrow tip (origin + normal * L).
                const float L = pl.halfSize * 0.8f;
                ImVec2 tipSp;
                if (project(pl.origin[0] + pl.normal[0] * L,
                            pl.origin[1] + pl.normal[1] * L,
                            pl.origin[2] + pl.normal[2] * L, tipSp)) {
                    ImVec2 h0(tipSp.x - handleHalf, tipSp.y - handleHalf);
                    ImVec2 h1(tipSp.x + handleHalf, tipSp.y + handleHalf);
                    const bool hot = (s_sectionDragIdx == pl.index) ||
                                     ImGui::IsMouseHoveringRect(h0, h1);
                    ImU32 fill = IM_COL32((int)(pl.rgba[0] * 255), (int)(pl.rgba[1] * 255),
                                         (int)(pl.rgba[2] * 255),
                                         hot ? 220 : 170);
                    ImU32 bord = IM_COL32((int)(pl.rgba[0] * 255), (int)(pl.rgba[1] * 255),
                                         (int)(pl.rgba[2] * 255), 255);
                    vdl->AddRectFilled(h0, h1, fill, 2.0f * sc);
                    vdl->AddRect(h0, h1, bord, 2.0f * sc, 0, (hot ? 2.0f : 1.4f) * sc);
                    if (hot)
                        ImGui::SetTooltip("Drag to move section plane");
                }
            }
        }

        // Wave4: context-cube pick mode banner (mirrors PM selection-well language).
        if (st.contextPickActive) {
            const float s2 = ui::uiScale;
            const char* hint =
                "Pick parts, faces, edges, sketches, or features for chat — Esc to finish";
            ImVec2 ts = ImGui::CalcTextSize(hint);
            float bannerRight = cr1.x;
            {
                const float e = ui::overlayColumnEdge(1);
                if (e > cr0.x + ts.x + 60.0f * s2 && e < cr1.x) bannerRight = e;
            }
            // Sit just under the pick-status readout row.
            ImVec2 bp0(bannerRight - ts.x - 20.0f * s2,
                       cr0.y + (inSketch ? 32.0f : 64.0f) * s2);
            ImVec2 bp1(bannerRight - 8.0f * s2, bp0.y + ts.y + 10.0f * s2);
            const ui::AdaptiveSurface& banSurf =
                ui::adaptivePanel(ui::panelId("vp_ctx_pick_banner"), ImRect(bp0, bp1),
                                 ui::GlassGroup::Hud);
            ui::glassPanel(vdl, ImRect(bp0, bp1), ui::GlassLevel::UltraThin, 5.0f, banSurf);
            // Accent left bar so it reads as an active pick mode (same language as PM wells).
            vdl->AddRectFilled(ImVec2(bp0.x, bp0.y + 2.0f * s2),
                              ImVec2(bp0.x + 3.0f * s2, bp1.y - 2.0f * s2),
                              ui::Palette::toU32(vpal.accent), 1.5f * s2);
            ui::glassText(vdl, nullptr, ImGui::GetFontSize(),
                         ImVec2(bp0.x + 9.0f * s2, bp0.y + 5.0f * s2), banSurf, hint);
        }


        // ---- Instant3D push-pull handle + live readout ----
        // The drag-to-extrude arrow is shown ONLY while the user is editing (tree "Edit Feature")
        // the feature that owns the single selected face — never on a passive face selection. The
        // handle is a bidirectional accent arrow along the face outward normal; press it to
        // drag-modify the face. While dragging, a readout chip near the cursor shows the driven
        // param + new value (and a "clamped" hint at the limit).
        bool ppHandleVisible = false;
        {
            bool ppEditing2 = st.propertyManager && st.propertyManager->isEditMode() &&
                              st.propertyManager->hasPreviewTarget();
            bool canShowHandle = st.hasDocMesh && !inSketch && !inSketch3d && !st.sketchPlanePickActive &&
                                 ppEditing2 && !s_viewSelector.active();
            if (canShowHandle) {
                ImVec2 cSp, tipP, tipN; float nsx, nsy, wpp; uint32_t bId, tId;
                if (ppHandleGeometry(st, renderer, vr0, vr1, vw, vh, cSp, tipP, tipN,
                                     nsx, nsy, wpp, bId, tId) &&
                    st.docMirror.ownerOfFace(bId, tId) == st.propertyManager->targetId()) {
                    ppHandleVisible = true;
                    const auto& vpal2 = ui::palette;
                    const ImU32 acc  = ui::Palette::toU32(vpal2.accent);
                    float sc = ui::uiScale;
                    bool dragging = s_pp.active;
                    // Capsule hover test (same perp/along band the tooltip used) BEFORE drawing,
                    // so the handle itself brightens + gains a soft halo under the cursor.
                    bool hovArrow = false;
                    if (!dragging) {
                        ImVec2 mp = io.MousePos;
                        float dxh = mp.x - cSp.x, dyh = mp.y - cSp.y;
                        float perp  = std::fabs(dxh * (-nsy) + dyh * nsx);
                        float along = std::fabs(dxh * nsx + dyh * nsy);
                        float armLenPx = 46.0f * sc;
                        hovArrow = perp <= 10.0f * sc && along <= armLenPx + 10.0f * sc;
                    }
                    drawManipArrow(vdl, cSp, tipP, tipN, nsx, nsy, 9.0f, 5.0f, 2.2f,
                                   hovArrow, dragging);

                    // Live readout chip near the cursor while dragging.
                    if (dragging && s_pp.haveReadout) {
                        // Title-case the param for display (depth -> "Depth").
                        std::string label = s_pp.param.empty() ? std::string("Value") : s_pp.param;
                        if (!label.empty()) label[0] = (char)std::toupper((unsigned char)label[0]);
                        // Format the (mm) drag distance in the active document units (mm / inch).
                        std::string txt = label + " " + ui::pmFormatDim(s_pp.newValue, st.ips());
                        if (s_pp.clamped) txt += "  (min)";
                        ImVec2 ts2 = ImGui::CalcTextSize(txt.c_str());
                        float padX = 8.0f * sc, padY = 4.0f * sc;
                        ImVec2 q0(io.MousePos.x + 16.0f * sc, io.MousePos.y + 16.0f * sc);
                        ImVec2 q1(q0.x + ts2.x + padX * 2, q0.y + ts2.y + padY * 2);
                        // keep inside the viewport rect
                        if (q1.x > cr1.x - 4) { float w = q1.x - q0.x; q1.x = cr1.x - 4; q0.x = q1.x - w; }
                        if (q1.y > cr1.y - 4) { float h = q1.y - q0.y; q1.y = cr1.y - 4; q0.y = q1.y - h; }
                        const ui::AdaptiveSurface& ppSurf =
                            ui::adaptivePanel(ui::panelId("vp_pp_readout"), ImRect(q0, q1),
                                              ui::GlassGroup::Hud);
                        ui::glassPanel(vdl, ImRect(q0, q1), ui::GlassLevel::UltraThin, 4.0f, ppSurf);
                        // statusWarn = the semantic "limit" amber in both themes (was a literal).
                        ImU32 chipBord = s_pp.clamped
                            ? ui::Palette::toU32(vpal2.statusWarn)
                            : acc;
                        vdl->AddRect(q0, q1, chipBord, 4.0f * sc);
                        ui::glassText(vdl, nullptr, ImGui::GetFontSize(),
                                     ImVec2(q0.x + padX, q0.y + padY), ppSurf, txt.c_str());
                    } else if (!dragging && hovArrow) {
                        // Hint tooltip when hovering the handle (not dragging).
                        ImGui::SetTooltip("Drag to push/pull this face");
                    }
                }
            }
        }

        // ---- Instant3D dimension handles + live readout ----
        // Drawn whenever a FEATURE is selected and NO single face is (that face's domain is the
        // push-pull arrow above — clear precedence: the face arrow wins). Each draggable dim renders
        // a small accent handle (a value chip on a short bidirectional arrow) anchored near the
        // feature's geometry; press it to drag-edit that param. While dragging, the dragged chip
        // shows "Depth 25.0 -> 31.5 mm" in the active units, and a "(min)" hint at the clamp.
        {
            // Dimension handles render ONLY while a feature's PM is open (creating/editing) — they
            // drive pm->targetId(), not the tree selection. Never shown on a passive tree highlight.
            const std::string dimRenderFid =
                (st.propertyManager && st.propertyManager->hasPreviewTarget())
                    ? st.propertyManager->targetId() : std::string();
            bool canShowDim = st.hasDocMesh && !inSketch && !inSketch3d && !st.sketchPlanePickActive &&
                              !s_viewSelector.active() && !s_pp.active && !ppHandleVisible &&
                              !dimRenderFid.empty();
            if (canShowDim) {
                auto cands = dimCandidates(st, dimRenderFid);
                const auto& vpal2 = ui::palette;
                const ImU32 acc  = ui::Palette::toU32(vpal2.accent);
                float sc = ui::uiScale;
                ImVec2 mp = io.MousePos;
                for (const auto& c : cands) {
                    ImVec2 aSp, tipP, tipN; float dsx, dsy, wpp, dpp;
                    if (!dimHandleGeometry(st, renderer, vr0, vr1, vw, vh, c, aSp, tipP, tipN,
                                           dsx, dsy, wpp, dpp))
                        continue;
                    bool isDragged = s_dim.active && s_dim.featureId == dimRenderFid &&
                                     s_dim.param == c.param;
                    // While THIS dim is being dragged, suppress the static chips for the OTHER dims so
                    // the view stays clean; the dragged dim keeps its handle + a live readout.
                    if (s_dim.active && !isDragged) continue;

                    // Capsule hover test BEFORE drawing so the handle gains a hover state (shared
                    // drawManipArrow soft halo + brighter shaft), not just a tooltip.
                    bool hovArrow = false;
                    if (!s_dim.active) {
                        float dxh = mp.x - aSp.x, dyh = mp.y - aSp.y;
                        float perp  = std::fabs(dxh * (-dsy) + dyh * dsx);
                        float along = std::fabs(dxh * dsx + dyh * dsy);
                        float armLenPx = 42.0f * sc;
                        hovArrow = perp <= 9.0f * sc && along <= armLenPx + 9.0f * sc;
                    }
                    drawManipArrow(vdl, aSp, tipP, tipN, dsx, dsy, 8.0f, 4.5f, 2.0f,
                                   hovArrow, isDragged);

                    // Value chip drawn ONLY while THIS dim is being dragged (the live
                    // "Depth 25.0 -> 31.5 mm" readout follows the cursor). The resting per-dim values
                    // no longer float over the geometry — they live in the fixed active-feature status
                    // panel pinned top-right (left of the Copilot column); see below.
                    if (isDragged && s_dim.haveReadout) {
                        std::string label = c.label.empty() ? c.param : c.label;
                        std::string oldS = c.angle ? (std::to_string((int)std::lround(s_dim.oldValue)) + "°")
                                                   : ui::pmFormatDim(s_dim.oldValue, st.ips());
                        std::string newS = c.angle ? (std::to_string((int)std::lround(s_dim.curValue)) + "°")
                                                   : ui::pmFormatDim(s_dim.curValue, st.ips());
                        std::string txt = label + " " + oldS + " -> " + newS;
                        if (s_dim.clamped) txt += (c.angle ? "  (limit)" : "  (min)");
                        ImVec2 ts2 = ImGui::CalcTextSize(txt.c_str());
                        float padX = 7.0f * sc, padY = 3.5f * sc;
                        ImVec2 q0(mp.x + 16.0f * sc, mp.y + 16.0f * sc);
                        ImVec2 q1(q0.x + ts2.x + padX * 2, q0.y + ts2.y + padY * 2);
                        // keep inside the viewport rect
                        if (q1.x > cr1.x - 4) { float w = q1.x - q0.x; q1.x = cr1.x - 4; q0.x = q1.x - w; }
                        if (q0.x < cr0.x + 4) { float w = q1.x - q0.x; q0.x = cr0.x + 4; q1.x = q0.x + w; }
                        if (q1.y > cr1.y - 4) { float h = q1.y - q0.y; q1.y = cr1.y - 4; q0.y = q1.y - h; }
                        if (q0.y < cr0.y + 4) { float h = q1.y - q0.y; q0.y = cr0.y + 4; q1.y = q0.y + h; }
                        const ui::AdaptiveSurface& dimSurf =
                            ui::adaptivePanel(ui::panelId(c.param.c_str()), ImRect(q0, q1),
                                              ui::GlassGroup::Hud);
                        ui::glassPanel(vdl, ImRect(q0, q1), ui::GlassLevel::UltraThin, 4.0f, dimSurf);
                        // statusWarn = the semantic "limit" amber in both themes (was a literal).
                        ImU32 chipBord = s_dim.clamped ? ui::Palette::toU32(vpal2.statusWarn) : acc;
                        vdl->AddRect(q0, q1, chipBord, 4.0f * sc);
                        ui::glassText(vdl, nullptr, ImGui::GetFontSize(),
                                     ImVec2(q0.x + padX, q0.y + padY), dimSurf, txt.c_str());
                    }

                    // Hover hint when not dragging and the cursor is over the handle.
                    if (hovArrow)
                        ImGui::SetTooltip(c.angle ? "Drag to change this angle"
                                                  : "Drag to change this dimension");
                }
            }
        }
        // The far-right "Active Feature" dimension readout panel that used to be pinned
        // top-right (left of the Copilot column) was REMOVED. It mirrored the PM's dimension
        // fields (Depth, Draft angle, radii, …) for EVERY feature via dimCandidates(activeFid),
        // producing a depth/draft double-display (once in the PM, once behind the
        // Copilot tab). The PM form is the single source of truth for these values; the live
        // drag-time readout chip (above) still follows the cursor while a dim handle is dragged.

        // C25 W3-J: apply ground-shadow visibility (last call wins; app.cpp may set it per-frame
        // before viewport_panel runs — the ordering means this value wins for the rendered frame,
        // which is the desired result since viewport_panel calls renderOrbit immediately before).
        // NOTE for orchestrator: if app.cpp pushes groundShadow AFTER viewport_panel in the frame
        // loop, the visibility toggle here would be overridden. Verify render call order.
        {
            const auto& vpal = ui::palette;
            float alpha = st.showGroundShadow ? vpal.groundShadow[3] : 0.0f;
            renderer.setGroundShadow(vpal.groundShadow[0], vpal.groundShadow[1],
                                     vpal.groundShadow[2], alpha);
        }
        // The row the triad resolves to, shared with the model-dimensions readout below (that pill
        // documents itself as sitting ABOVE the triad, so it has to follow when an obstacle lifts
        // the triad). Value here = today's placement, used verbatim when the triad is hidden.
        float triadInfoBottom = cr1.y - 112.0f * ui::uiScale;
        // ---- C24 / C26-C: orientation triad, pinned in the bottom-left corner of the clear
        // region, flush against the FeatureManager column edge (SolidWorks-style) ----
        // C25 W3-J: gated by st.showTriad.
        // C26-C: each axis tip is a small invisible ImGui button. Clicking an axis goes to the
        // corresponding preset view; clicking the axis you're already looking down flips opposite.
        //   +X tip → Right  (yaw=90,  pitch=0)
        //   +Y tip → Front  (yaw=0,   pitch=0)  — "look from the +Y side" = Front in this basis
        //   +Z tip → Top    (yaw=0,   pitch=89.5)
        // Hover: accentSoft circle behind the label. Tooltip: view name.
        if (st.showTriad) {
            const float ts = ui::uiScale;
            // Glyph geometry (logical px): axis length, label-chip offset beyond the tip, and the
            // chip half-extent. `reach` is the screen radius the whole glyph (labels included)
            // can occupy around the hub, so the corner anchor keeps every chip inside the clear
            // rect. These are pixel values: metrics::U() is a 4-px grid unit and previously
            // multiplied them by 4, floating the triad ~200 px into the canvas.
            const float L = ui::metrics::px(22.0f);
            const float labelOff = ui::metrics::px(10.0f);
            const float reach = L + labelOff + ui::metrics::px(8.0f);
            const float margin = ui::metrics::px(6.0f);
            // clr0.x is already the FeatureManager column's edge (plus gutter); anchoring there
            // keeps the triad stable regardless of how much content the column shows.
            const float cx = clr0.x + margin + reach;
            float cy = clr1.y - margin - reach;
            const ui::ViewportHudRect hud = ui::viewportHudRect();
            if (hud.valid && cx + reach + margin > hud.minX &&
                cx - reach - margin < hud.maxX &&
                cy + reach + margin > hud.minY && cy - reach - margin < hud.maxY)
                cy = std::max(hud.minY - margin - reach, clr0.y + reach + margin);
            // Model-info pill bottom = triadInfoBottom + 10 px; keep a small gap above the glyph.
            triadInfoBottom = cy - reach - ui::metrics::px(16.0f);
            float yawR = st.yaw * 3.14159265f / 180.0f, pitR = st.pitch * 3.14159265f / 180.0f;
            float cyaw = std::cos(yawR), syaw = std::sin(yawR);
            float cpit = std::cos(pitR), spit = std::sin(pitR);
            // toCam: +1 when the axis points straight at the camera, -1 straight away — drives the
            // depth cue (rear axes dim + shorten) so orientation stays readable near edge-on.
            auto proj = [&](float wx, float wy, float wz, float& sx, float& sy, float& toCam) {
                float rx = wx * cyaw + wy * syaw;
                float ry = -wx * syaw + wy * cyaw;
                sx = rx;
                sy = -(wz * cpit - ry * spit);
                toCam = ry * cpit + wz * spit;
            };
            struct Ax {
                float x, y, z;
                const char* l;
                const float* col;
                ViewPreset preset;
                const char* viewName;
                // The yaw/pitch that represents "already looking at this axis" (approx check).
                float chkYaw, chkPitch;
            };
            const Ax axes[] = {
                {1, 0, 0, "X", ui::palette.axisX, ViewPreset::Right,  "Right",  90.0f,  0.0f},
                {0, 1, 0, "Y", ui::palette.axisY, ViewPreset::Front,  "Front",   0.0f,  0.0f},
                {0, 0, 1, "Z", ui::palette.axisZ, ViewPreset::Top,    "Top",     0.0f, 89.5f},
            };
            // Project all three axes, then draw rear-pointing ones first, dimmed and slightly
            // shortened (contrast audit P1: no depth cue made near-edge-on orientation ambiguous).
            // Each stroke gets a 1px-ish light halo under-pass so the RGB ink also reads over a
            // dark part face, matching the glassText halo convention.
            float toCam[3]; ImVec2 tips[3];
            for (int ai = 0; ai < 3; ++ai) {
                float sx, sy; proj(axes[ai].x, axes[ai].y, axes[ai].z, sx, sy, toCam[ai]);
                float lenMul = 0.85f + 0.15f * (0.5f + 0.5f * toCam[ai]);
                tips[ai] = ImVec2(cx + sx * L * lenMul, cy + sy * L * lenMul);
            }
            int order[3] = {0, 1, 2};
            std::sort(order, order + 3, [&](int a, int b) { return toCam[a] < toCam[b]; });
            float haloF[4]; ui::Palette::withAlpha(ui::palette.viewportTop, haloF, 0.55f);
            const ImU32 axHalo = ui::Palette::toU32(haloF);
            for (int oi = 0; oi < 3; ++oi) {
                const auto& a = axes[order[oi]];
                float fade = 0.45f + 0.50f * (0.5f + 0.5f * toCam[order[oi]]);  // rear .45 -> front .95
                ImU32 col = ImGui::ColorConvertFloat4ToU32(
                    ImVec4(a.col[0], a.col[1], a.col[2], fade));
                const ImVec2 tip = tips[order[oi]];
                vdl->AddLine(ImVec2(cx, cy), tip, axHalo, 3.6f * ts);
                vdl->AddLine(ImVec2(cx, cy), tip, col, 1.8f * ts);
                // Arrowhead at the tip so each stroke reads as a proper axis VECTOR (the bare
                // stubs were nearly invisible — visual-review fix). Skip near-degenerate
                // projections (axis pointing at the camera).
                float adx = tip.x - cx, ady = tip.y - cy;
                float alen = std::sqrt(adx * adx + ady * ady);
                if (alen > 4.0f * ts) {
                    adx /= alen; ady /= alen;
                    const float ah = 4.5f * ts, aw = 2.6f * ts;
                    ImVec2 back(tip.x - adx * ah, tip.y - ady * ah);
                    ImVec2 p1(back.x - ady * aw, back.y + adx * aw);
                    ImVec2 p2(back.x + ady * aw, back.y - adx * aw);
                    vdl->AddTriangleFilled(tip, p1, p2, col);
                }
            }
            // Hub: canvas-stable dark ink over a light halo disc (was overlayText — near-white in
            // the dark theme, washed out on the light canvas).
            vdl->AddCircleFilled(ImVec2(cx, cy), 2.8f * ts, axHalo);
            vdl->AddCircleFilled(ImVec2(cx, cy), 2.0f * ts,
                                 ui::Palette::toU32(ui::palette.viewportEdge));

            // Draw buttons + labels on top.
            ImGui::PushID("##triad");
            const float hitR = 8.0f * ts;  // invisible button half-size
            for (int ai = 0; ai < 3; ++ai) {
                const auto& a = axes[ai];
                ImVec2 tip = tips[ai];

                // Label chip center sits BEYOND the arrow tip along the axis direction, so the
                // three letter badges never pile up on the stubs or on each other
                // (visual-review fix). Near-degenerate projections fall back up-right.
                float ldx = tip.x - cx, ldy = tip.y - cy;
                float llen = std::sqrt(ldx * ldx + ldy * ldy);
                if (llen > 1e-3f) { ldx /= llen; ldy /= llen; }
                else              { ldx = 0.7071f; ldy = -0.7071f; }
                const ImVec2 chipC(tip.x + ldx * labelOff, tip.y + ldy * labelOff);

                // Invisible button centered on the label chip (the visible click target).
                ImGui::SetCursorScreenPos(ImVec2(chipC.x - hitR, chipC.y - hitR));
                ImGui::PushID(ai);
                bool clicked = ImGui::InvisibleButton("##tb", ImVec2(hitR * 2, hitR * 2));
                bool hov = ImGui::IsItemHovered();
                ImGui::PopID();

                if (hov) {
                    // Hover: accentSoft circle highlight.
                    vdl->AddCircleFilled(chipC, hitR,
                        ui::Palette::toU32(ui::palette.accentSoft));
                    ImGui::SetTooltip("%s view", a.viewName);
                }
                if (clicked) {
                    // Check if already looking from this axis (within 5° tolerance).
                    float dy2  = std::fabs(std::fmod(st.yaw - a.chkYaw + 540.0f, 360.0f) - 180.0f);
                    float dp2  = std::fabs(st.pitch - a.chkPitch);
                    bool already = (dy2 < 5.0f && dp2 < 5.0f);
                    if (already) {
                        // Flip to opposite view.
                        static const float kZeroPan[3] = {0,0,0};
                        requestCameraAnimation(st, a.chkYaw + 180.0f,
                                               -a.chkPitch, -1.0f, kZeroPan);
                    } else {
                        applyViewPreset(st, a.preset);
                    }
                }

                // Label chip: adaptive glass pill (the fixed overlayBg rect was off-family with
                // every other HUD chip) with an axis-colored border keeping the color coding and
                // glassText for guaranteed letter contrast; offsets now uiScale'd.
                ImU32 col = ImGui::ColorConvertFloat4ToU32(
                    ImVec4(a.col[0], a.col[1], a.col[2], 0.95f));
                ImVec2 lts = ImGui::CalcTextSize(a.l);
                ImVec2 lp(chipC.x - lts.x * 0.5f, chipC.y - lts.y * 0.5f);
                ImVec2 c0(lp.x - 3.0f * ts, lp.y - 1.5f * ts);
                ImVec2 c1(lp.x + lts.x + 3.0f * ts, lp.y + lts.y + 1.5f * ts);
                static const char* kTriadChipId[3] = {"vp_triad_x", "vp_triad_y", "vp_triad_z"};
                const ui::AdaptiveSurface& axSurf = ui::adaptivePanel(
                    ui::panelId(kTriadChipId[ai]), ImRect(c0, c1), ui::GlassGroup::Hud);
                ui::glassPanel(vdl, ImRect(c0, c1), ui::GlassLevel::UltraThin, 3.0f, axSurf);
                vdl->AddRect(c0, c1, col, 3.0f * ts, 0, 1.0f * ts);
                ui::glassText(vdl, nullptr, ImGui::GetFontSize(), lp, axSurf, a.l);
            }
            ImGui::PopID();
        }
        // C27-A: hide the model-info dims pill while a PM form is active (callouts own the
        // viewport's parameter readout; avoids visual clutter / overlap).
        bool pmActive = st.propertyManager && st.propertyManager->active();
        if (!st.modelInfo.empty() && renderer.hasMesh() && !pmActive) {
            // dimensions readout, bottom-left (sits above the orientation triad — triadInfoBottom
            // is the row the triad resolved to, so the pill rides up with a lifted triad)
            ImFont* mf = st.fonts.mono ? st.fonts.mono : ImGui::GetFont();
            float fs = mf->FontSize * 0.92f;
            ImVec2 ts = mf->CalcTextSizeA(fs, 1e30f, 0.0f, st.modelInfo.c_str());
            // Dodge the pinned FeatureManager column and clear the (taller) triad below.
            float infoLeft = cr0.x;
            {
                const float e = ui::overlayColumnEdge(0);
                if (e > cr0.x && e < cr1.x - 140.0f * ui::uiScale) infoLeft = e;
            }
            ImVec2 p0(infoLeft + 12, triadInfoBottom - ts.y);
            ImVec2 p1(p0.x + ts.x + 20.0f, p0.y + ts.y + 10.0f);
            const ui::AdaptiveSurface& infoSurf =
                ui::adaptivePanel(ui::panelId("vp_model_info"), ImRect(p0, p1), ui::GlassGroup::Hud);
            ui::glassPanel(vdl, ImRect(p0, p1), ui::GlassLevel::UltraThin, 6.0f, infoSurf);
            ui::glassText(vdl, mf, fs, ImVec2(p0.x + 10 * ui::uiScale, p0.y + 5 * ui::uiScale),
                         infoSurf, st.modelInfo.c_str());
        }
        // ---- reference-plane name chips (projected to screen) ----
        // C25: gate on effPlane (same effective visibility as the quads) so labels never
        // float without their planes (the empty-doc bug).
        if (!inSketch && (effPlane[0] || effPlane[1] || effPlane[2])) {
            const float* m = renderer.camera().mvp.m;  // column-major model->clip
            auto project = [&](const Vec3& w, ImVec2& out) -> bool {
                float x = m[0]*w.x + m[4]*w.y + m[8]*w.z  + m[12];
                float y = m[1]*w.x + m[5]*w.y + m[9]*w.z  + m[13];
                float wc= m[3]*w.x + m[7]*w.y + m[11]*w.z + m[15];
                if (wc <= 1e-4f) return false;  // behind the camera
                out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
                out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
                return true;
            };
            static const char* kPlaneName[3] = {"X/Z", "X/Y", "Y/Z"};
            // W4-K: chip border tracks the datum-plane outline color (light-blue, SolidWorks look)
            // so the labels read as part of the unified overlay family instead of per-axis RGB.
            const ImU32 planeChipBord = ui::Palette::toU32(vpal.planeOutline);
            for (int p = 0; p < 3; ++p) {
                if (!effPlane[p]) continue;
                // Edge-on planes are lines, not readable surfaces. Do not clamp their labels to
                // an unrelated viewport edge when a sketch/Normal To view faces another plane.
                const Vec3 viewDir = renderer.camera().dir;
                const float facing = p == 0 ? viewDir.y : p == 1 ? viewDir.z : viewDir.x;
                if (std::fabs(facing) < 0.03f) continue;
                // Anchor the chip to the quad corner that currently sits farthest from the
                // viewport centre among the corners IN FRONT of the camera. A fixed world
                // corner swings behind the eye while orbiting (wc <= 0 -> chip popped out of
                // existence) and clamps to a random viewport edge when zoomed in; re-picking
                // per frame keeps the label riding a visible outer corner, SolidWorks-style.
                Vec3 c4[4];
                if (!renderer.planeQuad(p, c4)) continue;
                const float vcx = clrC.x, vcy = clrC.y;
                ImVec2 sp; float bestD2 = -1.0f;
                for (int ci = 0; ci < 4; ++ci) {
                    ImVec2 cand;
                    if (!project(c4[ci], cand)) continue;
                    const float dx = cand.x - vcx, dy = cand.y - vcy;
                    const float d2 = dx * dx + dy * dy;
                    if (d2 > bestD2) { bestD2 = d2; sp = cand; }
                }
                if (bestD2 < 0.0f) continue;  // every corner behind the camera
                const char* nm = kPlaneName[p];
                ImVec2 ts = ImGui::CalcTextSize(nm);
                // Keep the chip inside the clear region so it never shows through a glass pane,
                // with a margin that clears the Copilot column's resize grip, and below the
                // heads-up view toolbar row (drawn whenever this block runs: !inSketch) — the
                // toolbar sits at docTabBandBottom + 6dp and is 26dp tall (22dp buttons + 2*2dp).
                const float chipMargin = 10.0f * ui::uiScale;
                float chipTop = clr0.y + chipMargin;
                {
                    const float dtb = ui::docTabBandBottom();
                    const float huTop = dtb > clr0.y - 6.0f * ui::uiScale ? dtb + 6.0f * ui::uiScale
                                                                          : clr0.y;
                    chipTop = std::max(chipTop, huTop + 26.0f * ui::uiScale + chipMargin);
                }
                float x0 = sp.x, y0 = sp.y;
                if (x0 + ts.x + 12 > clr1.x - chipMargin) x0 = clr1.x - chipMargin - ts.x - 12;
                if (x0 < clr0.x + chipMargin) x0 = clr0.x + chipMargin;
                if (y0 + ts.y + 6 > clr1.y - chipMargin) y0 = clr1.y - chipMargin - ts.y - 6;
                if (y0 < chipTop) y0 = chipTop;
                ImVec2 q0(x0, y0), q1(x0 + ts.x + 12, y0 + ts.y + 6);
                // C43: during plane-pick the label chips are CLICK TARGETS (the quads overlap
                // heavily near the origin; the labels are unambiguous). Outside a pick — and outside
                // a PM form that owns plane picking — the chip is still clickable to PRE-SELECT the
                // plane in the graphics area (SolidWorks "select a plane, then Sketch"): the click
                // sets it as the FeatureManager selection so the ribbon "New Sketch" starts on it.
                bool pmOwnsPlanes = st.propertyManager && st.propertyManager->active();
                bool chipClickable = sceneToolArmed || st.sketchPlanePickActive || !pmOwnsPlanes;
                bool chipHovered = chipClickable && ImGui::IsMouseHoveringRect(q0, q1);
                static const char* kPlaneSel[3] = {"plane.front", "plane.top", "plane.right"};
                bool chipSelected = !st.sketchPlanePickActive &&
                                    st.selectedFeatureId == kPlaneSel[p];
                const ui::AdaptiveSurface& planeSurf =
                    ui::adaptivePanel(ui::panelId(nm), ImRect(q0, q1), ui::GlassGroup::Hud);
                ui::glassPanel(vdl, ImRect(q0, q1), ui::GlassLevel::UltraThin, 4.0f, planeSurf);
                vdl->AddRect(q0, q1, (chipHovered || chipSelected)
                                         ? ui::Palette::toU32(vpal.accent)
                                         : planeChipBord, 4.0f * ui::uiScale, 0,
                             (chipHovered || chipSelected) ? 2.0f : 1.0f);
                ui::glassText(vdl, nullptr, ImGui::GetFontSize(),
                              ImVec2(x0 + 6 * ui::uiScale, y0 + 3 * ui::uiScale), planeSurf, nm);
                if (chipHovered && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    static constexpr ui::simscene::Plane scenePlanes[3] = {
                        ui::simscene::Plane::XZ, ui::simscene::Plane::XY, ui::simscene::Plane::YZ};
                    if (sceneToolArmed && ui::simscene::onPlaneChip(st, scenePlanes[p]))
                        continue;
                    if (st.sketchPlanePickActive && st.sketchMode) {
                        st.sketchPlanePickActive = false;
                        st.sketchMode->consumePlanePick(st, kPlaneSel[p]);
                    } else {
                        // Pre-select the plane (graphics-area). enterNewOnSelection consumes
                        // st.selectedFeatureId, so the next "New Sketch" starts on this plane.
                        st.selection.clear();
                        st.selectedFeatureId = kPlaneSel[p];
                        st.planeEmphasis = p;
                    }
                }
            }
        }

        // ---- C27-A: SolidWorks-style in-viewport parameter callouts ----
        // While the PropertyManager is editing a feature, show a small chip ("Radius: 4mm")
        // per visible+enabled Dim field, tied by a leader line to the affected geometry.
        if (st.propertyManager && st.hasDocMesh) {
            auto* pm = st.propertyManager;
            if (pm->active()) {
                std::vector<ui::PMCalloutItem> items = pm->calloutItems(st);
                if (!items.empty()) {
                    const float* m = renderer.camera().mvp.m;  // column-major model->clip
                    auto project = [&](const float w[3], ImVec2& out) -> bool {
                        float x = m[0]*w[0] + m[4]*w[1] + m[8]*w[2]  + m[12];
                        float y = m[1]*w[0] + m[5]*w[1] + m[9]*w[2]  + m[13];
                        float wc= m[3]*w[0] + m[7]*w[1] + m[11]*w[2] + m[15];
                        if (wc <= 1e-4f) return false;  // behind the camera
                        out.x = vr0.x + (x / wc * 0.5f + 0.5f) * (vr1.x - vr0.x);
                        out.y = vr0.y + (1.0f - (y / wc * 0.5f + 0.5f)) * (vr1.y - vr0.y);
                        return true;
                    };
                    const float sc = ui::uiScale;
                    const ImU32 leaderCol = ImGui::ColorConvertFloat4ToU32(
                        ImVec4(vpal.accent[0], vpal.accent[1], vpal.accent[2], 0.8f));
                    const ImU32 chipBord = ui::Palette::toU32(vpal.accent);
                    // Keep callouts inside the clear viewport, beneath the view-toolbar and
                    // pick-status rows; the full-bleed image also spans the glass columns/tabs.
                    const float margin = ui::metrics::U(8.0f);
                    const float calloutTop = clr0.y + ui::metrics::U(60.0f);
                    const float rightBound = clr1.x - margin;
                    // Limit to ~3 chips; stagger vertically so anchored chips don't overlap.
                    int shown = 0, anchoredRow = 0, dockRow = 0;
                    for (const auto& ci : items) {
                        if (shown >= 3) break;
                        std::string txt = ci.label + ": " + ci.value;
                        ImVec2 ts = ImGui::CalcTextSize(txt.c_str());
                        float padX = 8.0f * sc, padY = 4.0f * sc;
                        float cw = ts.x + padX * 2, ch = ts.y + padY * 2;

                        ImVec2 chipTL;
                        bool drawLeader = false;
                        ImVec2 anchorSp;
                        if (ci.hasAnchor && project(ci.anchor, anchorSp)) {
                            // Offset the chip up-right from the anchor; stagger by row.
                            float off = 40.0f * sc;
                            chipTL = ImVec2(anchorSp.x + off,
                                            anchorSp.y - off - anchoredRow * (ch + 6.0f * sc));
                            drawLeader = true;
                            ++anchoredRow;
                        } else {
                            // No geometry: dock below the toolbar and pick-status rows.
                            chipTL = ImVec2(rightBound - cw,
                                            calloutTop + dockRow * (ch + ui::metrics::U(6.0f)));
                            ++dockRow;
                        }
                        const float maxLeft = std::max(clr0.x + margin, rightBound - cw);
                        const float maxTop = std::max(calloutTop, clr1.y - margin - ch);
                        chipTL.x = std::clamp(chipTL.x, clr0.x + margin, maxLeft);
                        chipTL.y = std::clamp(chipTL.y, calloutTop, maxTop);
                        ImVec2 chipBR(chipTL.x + cw, chipTL.y + ch);

                        if (drawLeader) {
                            // Leader from the anchor to the nearest chip corner-ish point.
                            ImVec2 leaderEnd(chipTL.x, chipTL.y + ch * 0.5f);
                            vdl->AddLine(anchorSp, leaderEnd, leaderCol, 1.5f * sc);
                            vdl->AddCircleFilled(anchorSp, 2.6f * sc, leaderCol);
                        }
                        const ui::AdaptiveSurface& pmSurf =
                            ui::adaptivePanel(ui::panelId(ci.label.c_str()), ImRect(chipTL, chipBR),
                                              ui::GlassGroup::Hud);
                        ui::glassPanel(vdl, ImRect(chipTL, chipBR), ui::GlassLevel::UltraThin, 4.0f,
                                      pmSurf);
                        vdl->AddRect(chipTL, chipBR, chipBord, 4.0f * sc);
                        ui::glassText(vdl, nullptr, ImGui::GetFontSize(),
                                     ImVec2(chipTL.x + padX, chipTL.y + padY), pmSurf, txt.c_str());
                        ++shown;
                    }
                }
            }
        }

        // ---- C19: result colormap legend (bottom-right) ----
        ui::drawLegend(st, vdl, cr0, cr1);
        }  // end overlay colors scope
    } else {
        invalidateViewportBackdrop();
    }
    ImGui::End();
    } else {
        invalidateViewportBackdrop();
    }
}

}  // namespace ui
