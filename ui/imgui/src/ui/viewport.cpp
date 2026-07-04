#include "ui/viewport.h"
#include "ui/doc_tabs.h"
#include "ui/shortcut_bar.h"
#include "ui/sketch/sketch_mode.h"
#include "ui/sketch/sketch_seam.h"
#include "ui/camera_anim.h"
#include "ui/mouse_gestures.h"
#include "ui/view_selector.h"
#include "ui/section_tool.h"
#include "ui/legend.h"
#include "ui/drawing_panel.h"
#include "ui/sim_manager.h"
#include "ui/cam/cam_panel.h"
#include "ui/ecad/ecad_panel.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "viewport_impl.h"
#include "render/aicm_loader.h"
#include "imgui.h"
#include "imgui_internal.h"
#include <algorithm>
#include <cmath>
#include <cstdio>

namespace ui {

using namespace app;

static aicad::viewport::GlViewport* g_glVp = nullptr;
static ViewportState              g_vpState;
static ShortcutBar                s_shortcutBar;
static MouseGestures              s_gestures;
static ViewSelector               s_viewSelector;
static SectionTool                s_sectionTool;
static float                      s_huFade = 0.25f;
// T-BL46: moved to file scope so elementDragActive() is accessible before the if(r) block
// where it was previously declared as a local static.  Single instance — same semantics.
static SketchMode                 s_sketch;

static bool   s_zoomAreaArmed = false;
static bool   s_zoomAreaDragging = false;
static ImVec2 s_zoomAreaP0 = {0, 0};
static ImVec2 s_zoomAreaP1 = {0, 0};

// Published viewport image rect (screen coords), kept for the theme::LuminanceSampler (A7) and
// for setViewportBackdropActive's invalidation path. Updated once per frame in publishBackdrop().
static ImVec2 s_bdMin  = {0, 0};
static ImVec2 s_bdMax  = {0, 0};
static bool   s_bdLive = false;

ViewportState& viewportState() { return g_vpState; }
// T-BL46: accessor so scenarios.cpp can read s_sketch.elementDragActive() without friendship.
SketchMode& sketchMode() { return s_sketch; }
// T-BL46: expose the GlViewport for world→screen coordinate computation in scenarios.
aicad::viewport::GlViewport* viewportGl() { return g_glVp; }
// T-BL46: published image rect accessors (safe to call from scenarios tick).
ImVec2 viewportImageMin() { return s_bdMin; }
ImVec2 viewportImageMax() { return s_bdMax; }

ImU32 glassIconHalo(const theme::AdaptiveSurface& surf, float alphaMul) {
    // Opposite vibrancy tone from surf's text (A8: theme-independent tokens stand in for the
    // literal white/black halo theme::glassText uses internally).
    const unsigned int hex = surf.darkText ? theme::pal().vibrancyLightPrimary
                                           : theme::pal().vibrancyDarkPrimary;
    return theme::roleU32A(hex, (0x50 / 255.f) * alphaMul);
}

namespace {

Renderer* renderer() {
    return g_glVp ? &g_glVp->renderer() : nullptr;
}

// Convert one AICM body's tessellation to a shaded Mesh (studio pass + camera bounds).
// Ported verbatim from host/src/core/app_state.cpp meshFromBody: reuses AICM positions/
// normals/indices and per-EDGE polylines as feature edges so the shaded view matches the
// id pass 1:1.
Mesh meshFromBody(const render::BodyMesh& b) {
    Mesh m;
    m.positions = b.pos;
    m.normals = b.nrm;
    m.indices = b.idx;
    for (const auto& es : b.edges) {
        for (uint32_t i = 0; i + 1 < es.pt_count; ++i) {
            const float* a = &b.edge_pts[(size_t)(es.first_pt + i) * 3];
            const float* c = &b.edge_pts[(size_t)(es.first_pt + i + 1) * 3];
            m.edgeLines.insert(m.edgeLines.end(), {a[0], a[1], a[2], c[0], c[1], c[2]});
        }
    }
    Vec3 mn(1e30f, 1e30f, 1e30f), mx(-1e30f, -1e30f, -1e30f);
    for (size_t i = 0; i + 2 < b.pos.size(); i += 3) {
        float x = b.pos[i], y = b.pos[i + 1], z = b.pos[i + 2];
        if (x < mn.x) mn.x = x; if (y < mn.y) mn.y = y; if (z < mn.z) mn.z = z;
        if (x > mx.x) mx.x = x; if (y > mx.y) mx.y = y; if (z > mx.z) mx.z = z;
    }
    m.bbMin = mn; m.bbMax = mx; m.center = (mn + mx) * 0.5f;
    m.radius = std::max(0.001f, length(mx - mn) * 0.5f);
    return m;
}

// Upload every body of a parsed DocMesh into the shaded renderer (per-body color/material +
// per-face triangle runs). The doc/id-pass side is fed separately via GlViewport::setDoc.
bool uploadDocMeshes(const render::DocMesh& doc) {
    if (!g_glVp) return false;
    std::vector<Renderer::PartMesh> pms;
    pms.reserve(doc.bodies.size());
    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        Renderer::PartMesh pm;
        pm.mesh = meshFromBody(b);
        pm.color[0] = b.color[0]; pm.color[1] = b.color[1]; pm.color[2] = b.color[2];
        pm.metallic = b.metallic; pm.roughness = b.roughness;
        pm.body_id = b.body_id;
        pm.faces.reserve(b.faces.size());
        for (const auto& fr : b.faces)
            pm.faces.push_back({fr.face_id, fr.first_tri, fr.tri_count});
        pms.push_back(std::move(pm));
    }
    g_glVp->setMeshes(pms);
    return !pms.empty();
}

void syncCameraToGl() {
    if (!g_glVp) return;
    aicad::ui::CameraState cam;
    cam.yaw = g_vpState.yaw;
    cam.pitch = g_vpState.pitch;
    cam.distance = g_vpState.zoom;
    cam.target[0] = g_vpState.pan[0];
    cam.target[1] = g_vpState.pan[1];
    cam.target[2] = g_vpState.pan[2];
    g_glVp->setCamera(cam);
}

bool ensureDocLoaded(AppState& s) {
    if (!g_glVp) return false;
    // Externally-injected mesh (pick/glassperf harness via viewportLoadAicm): render it even
    // while the engine has no aicm of its own. Was `return false` — pure superset.
    if (!s.engineReady || s.engine.aicmPath().empty()) return g_glVp->renderer().hasMesh();
    if (s.viewMeshRev == s.engine.revision() && g_glVp->renderer().hasMesh()) return true;

    render::DocMesh doc;
    std::string err;
    if (!render::loadAicmFile(s.engine.aicmPath(), doc, err)) return false;
    if (!g_glVp->setDoc(doc, err)) return false;
    uploadDocMeshes(doc);  // shaded studio pass (was never wired in AI-CAD2 — id pass only)
    g_glVp->setSceneRevision(s.engine.revision());
    s.viewMeshRev = s.engine.revision();
    return true;
}

void renderViewport(AppState& s, int w, int h) {
    if (!g_glVp || w < 8 || h < 8) return;
    ensureDocLoaded(s);  // best-effort: an EMPTY scene still renders the GL studio backdrop

    Renderer& r = g_glVp->renderer();
    g_glVp->resize(w, h);
    syncCameraToGl();
    r.setDisplayStyle(static_cast<DisplayStyle>(g_vpState.displayStyle));
    g_glVp->render();
}

// Publishes the Kawase-blurred backdrop with the viewport image's SCREEN RECT so theme can
// project any panel rect into backdrop UVs (contract §4/A7). Also feeds the file-static rect
// consumed by the registered theme::LuminanceSampler below.
void publishBackdrop(ImVec2 a, ImVec2 b) {
    if (!g_glVp) { s_bdLive = false; return; }
    const auto bd = g_glVp->backdrop();
    theme::ViewportBackdrop tbd;
    tbd.textureId = bd.textureId;
    tbd.uvMin = ImVec2(bd.uvMin[0], bd.uvMin[1]);
    tbd.uvMax = ImVec2(bd.uvMax[0], bd.uvMax[1]);
    tbd.screenMin = a;
    tbd.screenMax = b;
    tbd.flipV = true;
    theme::setViewportBackdrop(tbd);
    s_bdMin = a;
    s_bdMax = b;
    s_bdLive = tbd.valid();
}

// A6: publish an empty backdrop (palette-fallback path everywhere) and mark it not-live so the
// registered LuminanceSampler returns -1 immediately.
void invalidateBackdrop() {
    theme::setViewportBackdrop(theme::ViewportBackdrop{});
    s_bdLive = false;
}

void drawHeadsUpToolbar(AppState& st, ImGuiIO& io, ImDrawList* dl,
                        ImVec2 cr0, ImVec2 cr1, Renderer& renderer, bool inSketch) {
    if (inSketch) return;

    const float S = theme::S();
    float btn = 22.f * S, gap = 2.f * S, padX = 3.f * S, padY = 2.f * S;
    const int nHU = 8;
    float barW = nHU * btn + (nHU - 1) * gap + padX * 2;
    float barH = btn + padY * 2;
    float bx0  = cr1.x - barW - 10.f * S;
    float by0  = cr0.y + 6.f * S;
    float byi  = by0 + padY;

    ImVec2 mp = io.MousePos;
    ImRect hudZone(ImVec2(bx0 - 14.f * S, by0 - 10.f * S),
                   ImVec2(bx0 + barW + 10.f * S, by0 + barH + 10.f * S));
    const bool hudHot = hudZone.Contains(mp) || s_zoomAreaArmed ||
        ImGui::IsPopupOpen("##hu_vieworient") || ImGui::IsPopupOpen("##hu_dispstyle") ||
        ImGui::IsPopupOpen("##hu_hideshow");
    // Idle fade floors at 0.25 (never fully invisible) and ramps to full opacity within ~120ms of
    // hover (spec §8.5); the fade-out on hover-exit uses a gentler tau.
    const float targetFade = hudHot ? 1.0f : 0.25f;
    const float fadeTau = hudHot ? 0.04f : 0.12f;
    s_huFade = theme::approach(s_huFade, targetFade, io.DeltaTime, fadeTau);
    if (s_huFade <= 0.04f) return;

    // Per-pill adaptive surface (A1: id keyed by panelId(iconName), window-independent) + the
    // frosted background; returns the surface so the caller can tint its icon glyph to match.
    auto huPill = [&](float bxi, float by, bool selected,
                      const char* iconName) -> const theme::AdaptiveSurface& {
        ImVec2 mn(bxi, by), mx(bxi + btn, by + btn);
        const theme::AdaptiveSurface& surf =
            theme::adaptivePanel(theme::panelId(iconName), ImRect(mn, mx));
        theme::glassPanel(dl, ImRect(mn, mx), theme::MaterialLevel::UltraThin, 6.f * S, surf,
                          s_huFade * (selected ? 1.f : 0.85f));
        if (selected && s_huFade > 0.55f)
            theme::glassRowBackground(dl, mn, mx, 4.f * S, false, true, 0.42f * s_huFade);
        return surf;
    };

    // icons::drawGlass with the pill's own surface tone + opposite-tone halo, faded with s_huFade.
    auto huIcon = [&](const theme::AdaptiveSurface& surf, const char* iconName, ImVec2 c, float sz) {
        ImVec4 col = surf.textPrimary;
        col.w *= s_huFade;
        icons::drawGlass(dl, iconName, c, sz, ImGui::ColorConvertFloat4ToU32(col),
                         glassIconHalo(surf, s_huFade));
    };

    auto huBtn = [&](int idx, const char* iconName, const char* tip, bool active) -> bool {
        float bxi = bx0 + padX + (float)idx * (btn + gap);
        const theme::AdaptiveSurface& surf = huPill(bxi, byi, active, iconName);
        ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
        ImGui::PushID(idx);
        bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
        bool hov = ImGui::IsItemHovered();
        if (hov && !active)
            theme::glassRowBackground(dl, ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                      4.f * S, false, false, 0.32f * s_huFade);
        huIcon(surf, iconName, ImVec2(bxi + btn * 0.5f, byi + btn * 0.5f), btn * 0.36f);
        if (hov) ImGui::SetTooltip("%s", tip);
        ImGui::PopID();
        return clicked;
    };

    auto huSep = [&](int afterIdx) {
        if (s_huFade < 0.12f) return;
        float bxi = bx0 + padX + (float)afterIdx * (btn + gap);
        float sepA = s_huFade * 0.55f;
        dl->AddLine(ImVec2(bxi + btn + gap * 0.5f, byi + 3 * S),
                    ImVec2(bxi + btn + gap * 0.5f, byi + btn - 3 * S),
                    theme::roleU32(theme::pal().line, sepA), 1.f);
    };

    ImGui::PushID("##headsup");

    if (huBtn(0, "zoom_fit", "Zoom to Fit (F)", false))
        applyViewPreset(st, ViewPreset::ZoomFit);

    if (huBtn(1, "zoom_area", "Zoom to Area", s_zoomAreaArmed)) {
        s_zoomAreaArmed = !s_zoomAreaArmed;
        s_zoomAreaDragging = false;
    }

    if (huBtn(2, "pan_view", "Pan (left-drag while active)", g_vpState.vpDragMode == 1))
        g_vpState.vpDragMode = (g_vpState.vpDragMode == 1) ? 0 : 1;

    if (huBtn(3, "rotate_view", "Rotate (left-drag)", g_vpState.vpDragMode == 0))
        g_vpState.vpDragMode = 0;

    huSep(3);

    {
        float bxi = bx0 + padX + 4.f * (btn + gap);
        const theme::AdaptiveSurface& surf = huPill(bxi, byi, false, "view_selector");
        ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
        ImGui::PushID(4);
        bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
        bool hov = ImGui::IsItemHovered();
        if (hov)
            theme::glassRowBackground(dl, ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                      4.f * S, false, false, 0.32f * s_huFade);
        huIcon(surf, "view_selector", ImVec2(bxi + btn * 0.5f, byi + btn * 0.5f), btn * 0.36f);
        if (hov) ImGui::SetTooltip("View Orientation");
        if (clicked) ImGui::OpenPopup("##hu_vieworient");
        ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.f * S), ImGuiCond_Always);
        if (ImGui::BeginPopup("##hu_vieworient")) {
            struct { const char* label; ViewPreset p; } vrows[] = {
                {"Front", ViewPreset::Front}, {"Back", ViewPreset::Back},
                {"Left", ViewPreset::Left}, {"Right", ViewPreset::Right},
                {"Top", ViewPreset::Top}, {"Bottom", ViewPreset::Bottom},
                {"Isometric", ViewPreset::Isometric},
            };
            for (const auto& r : vrows)
                if (ImGui::MenuItem(r.label)) applyViewPreset(st, r.p);
            ImGui::Separator();
            if (ImGui::MenuItem("View Selector (Space)") && !s_viewSelector.active())
                s_viewSelector.openFramed(st);
            ImGui::EndPopup();
        }
        ImGui::PopID();
    }

    huSep(4);

    {
        static const char* kDsIcons[] = {
            "display_shaded", "display_shaded_edges", "display_hlr",
            "display_hlv", "display_wireframe"
        };
        const char* curIcon = kDsIcons[g_vpState.displayStyle < 5 ? g_vpState.displayStyle : 1];
        float bxi = bx0 + padX + 5.f * (btn + gap);
        const theme::AdaptiveSurface& surf = huPill(bxi, byi, false, curIcon);
        ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
        ImGui::PushID(5);
        bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
        bool hov = ImGui::IsItemHovered();
        if (hov)
            theme::glassRowBackground(dl, ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                      4.f * S, false, false, 0.32f * s_huFade);
        huIcon(surf, curIcon, ImVec2(bxi + btn * 0.5f, byi + btn * 0.5f), btn * 0.36f);
        if (hov) ImGui::SetTooltip("Display Style");
        if (clicked) ImGui::OpenPopup("##hu_dispstyle");
        ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.f * S), ImGuiCond_Always);
        if (ImGui::BeginPopup("##hu_dispstyle")) {
            struct { const char* icon; const char* label; int val; } drows[] = {
                {"display_shaded", "Shaded", 0},
                {"display_shaded_edges", "Shaded with Edges", 1},
                {"display_hlr", "Hidden Lines Removed", 2},
                {"display_hlv", "Hidden Lines Visible", 3},
                {"display_wireframe", "Wireframe", 4},
            };
            for (const auto& dr : drows) {
                icons::inlineIcon(dr.icon, 16.f * S, TCOL(txt1));
                ImGui::SameLine(0, 6.f * S);
                bool sel = (g_vpState.displayStyle == dr.val);
                if (sel) ImGui::PushStyleColor(ImGuiCol_Text, TVEC(accent));
                if (ImGui::MenuItem(dr.label, nullptr, sel)) {
                    g_vpState.displayStyle = dr.val;
                    st.viewStyle = dr.val;
                    renderer.setDisplayStyle(static_cast<DisplayStyle>(dr.val));
                }
                if (sel) ImGui::PopStyleColor();
            }
            ImGui::EndPopup();
        }
        ImGui::PopID();
    }

    {
        float bxi = bx0 + padX + 6.f * (btn + gap);
        const theme::AdaptiveSurface& surf = huPill(bxi, byi, false, "hide_show_eye");
        ImGui::SetCursorScreenPos(ImVec2(bxi, byi));
        ImGui::PushID(6);
        bool clicked = ImGui::InvisibleButton("##hb", ImVec2(btn, btn));
        bool hov = ImGui::IsItemHovered();
        if (hov)
            theme::glassRowBackground(dl, ImVec2(bxi, byi), ImVec2(bxi + btn, byi + btn),
                                      4.f * S, false, false, 0.32f * s_huFade);
        huIcon(surf, "hide_show_eye", ImVec2(bxi + btn * 0.5f, byi + btn * 0.5f), btn * 0.36f);
        if (hov) ImGui::SetTooltip("Hide / Show Items");
        if (clicked) ImGui::OpenPopup("##hu_hideshow");
        ImGui::SetNextWindowPos(ImVec2(bxi, byi + btn + 4.f * S), ImGuiCond_Always);
        if (ImGui::BeginPopup("##hu_hideshow")) {
            bool allPlanes = g_vpState.showPlane[0] && g_vpState.showPlane[1] && g_vpState.showPlane[2];
            if (ImGui::MenuItem("Reference Planes", nullptr, allPlanes)) {
                bool nv = !allPlanes;
                g_vpState.showPlane[0] = g_vpState.showPlane[1] = g_vpState.showPlane[2] = nv;
            }
            ImGui::MenuItem("Origin Triad", nullptr, &g_vpState.showTriad);
            ImGui::MenuItem("Ground Shadow", nullptr, &g_vpState.showGroundShadow);
            ImGui::MenuItem("RealView reflections", nullptr, &g_vpState.showRealView);
            ImGui::EndPopup();
        }
        ImGui::PopID();
    }

    huSep(6);

    if (huBtn(7, "section_view", "Section View", s_sectionTool.active())) {
        s_sectionTool.toggle(st);
        if (s_sectionTool.active() && renderer.hasMesh())
            s_sectionTool.updateAndDraw(st, renderer, false, false, cr0, cr1);
    }

    ImGui::PopID();

    if (s_zoomAreaDragging) {
        float rx0 = std::min(s_zoomAreaP0.x, s_zoomAreaP1.x);
        float ry0 = std::min(s_zoomAreaP0.y, s_zoomAreaP1.y);
        float rx1 = std::max(s_zoomAreaP0.x, s_zoomAreaP1.x);
        float ry1 = std::max(s_zoomAreaP0.y, s_zoomAreaP1.y);
        dl->AddRectFilled(ImVec2(rx0, ry0), ImVec2(rx1, ry1), TCOL(accentSoft));
        dl->AddRect(ImVec2(rx0, ry0), ImVec2(rx1, ry1), TCOL(accent), 1.5f);
    }
}

void drawOrientationGizmo(ImDrawList* dl, ImVec2 g) {
    const float S = theme::S();
    float L = 26.f * S;
    dl->AddLine(g, ImVec2(g.x + L, g.y), TCOL(bad), 2.5f * S);
    dl->AddText(ImVec2(g.x + L + 2 * S, g.y - 6 * S), TCOL(bad), "X");
    dl->AddLine(g, ImVec2(g.x, g.y - L), TCOL(good), 2.5f * S);
    dl->AddText(ImVec2(g.x - 3 * S, g.y - L - 14 * S), TCOL(good), "Z");
    dl->AddLine(g, ImVec2(g.x - L * 0.6f, g.y + L * 0.6f), TCOL(accent), 2.5f * S);
    dl->AddText(ImVec2(g.x - L * 0.6f - 12 * S, g.y + L * 0.6f), TCOL(accent), "Y");
    dl->AddCircleFilled(g, 3 * S, TCOL(txt0));
}

}  // namespace

bool viewportLoadAicm(const std::string& path) {
    if (!g_glVp) return false;
    render::DocMesh doc;
    std::string err;
    if (!render::loadAicmFile(path, doc, err)) {
        std::fprintf(stderr, "viewport: aicm load failed: %s\n", err.c_str());
        return false;
    }
    if (!g_glVp->setDoc(doc, err)) {
        std::fprintf(stderr, "viewport: setDoc failed: %s\n", err.c_str());
        return false;
    }
    return uploadDocMeshes(doc);
}

// H4: like uploadDocMeshes, but attaches doc.scalars/doc.displacements (per-body, matched by
// body_id) onto each PartMesh so the shaded pass can colormap/deform the result. Returns the
// chosen field's [vmin,vmax] (the first body that carries it) and a suggested deform-scale hint
// (the largest body's ScalarField-adjacent DispField::scale_hint, or 1.0 if none).
static bool uploadResultMeshes(const render::DocMesh& doc, const std::string& scalarField,
                               float& vmin, float& vmax, float& dispScaleHint) {
    if (!g_glVp) return false;
    std::vector<Renderer::PartMesh> pms;
    pms.reserve(doc.bodies.size());
    bool foundScalar = false;
    dispScaleHint = 1.0f;
    for (const auto& b : doc.bodies) {
        if (b.pos.empty()) continue;
        Renderer::PartMesh pm;
        pm.mesh = meshFromBody(b);
        pm.color[0] = b.color[0]; pm.color[1] = b.color[1]; pm.color[2] = b.color[2];
        pm.metallic = b.metallic; pm.roughness = b.roughness;
        pm.body_id = b.body_id;
        pm.faces.reserve(b.faces.size());
        for (const auto& fr : b.faces)
            pm.faces.push_back({fr.face_id, fr.first_tri, fr.tri_count});

        for (const auto& sc : doc.scalars) {
            if (sc.body_id != b.body_id) continue;
            if (!scalarField.empty() && sc.name != scalarField) continue;
            if (sc.values.size() != b.n_verts) continue;
            pm.scalar = sc.values;
            pm.scalarMin = sc.vmin; pm.scalarMax = sc.vmax;
            if (!foundScalar) { vmin = sc.vmin; vmax = sc.vmax; foundScalar = true; }
            break;
        }
        for (const auto& d : doc.displacements) {
            if (d.body_id != b.body_id) continue;
            if (d.vec.size() != (size_t)b.n_verts * 3) continue;
            pm.disp = d.vec;
            pm.dispScaleHint = d.scale_hint;
            dispScaleHint = d.scale_hint;
            break;
        }
        pms.push_back(std::move(pm));
    }
    g_glVp->setMeshes(pms);
    if (!foundScalar) { vmin = 0.f; vmax = 1.f; }
    return !pms.empty();
}

bool viewportLoadResultAicm(const std::string& path, const std::string& scalarField,
                            float& vmin, float& vmax, float& dispScaleHint) {
    if (!g_glVp) return false;
    render::DocMesh doc;
    std::string err;
    if (!render::loadAicmFile(path, doc, err)) {
        std::fprintf(stderr, "viewport: result aicm load failed: %s\n", err.c_str());
        return false;
    }
    if (!g_glVp->setDoc(doc, err)) {
        std::fprintf(stderr, "viewport: result setDoc failed: %s\n", err.c_str());
        return false;
    }
    bool ok = uploadResultMeshes(doc, scalarField, vmin, vmax, dispScaleHint);
    if (ok) g_glVp->renderer().setColormapMode(true);
    return ok;
}

void viewportSetColormapMode(bool on) {
    if (g_glVp) g_glVp->renderer().setColormapMode(on);
}
void viewportSetDeformScale(float scale) {
    if (g_glVp) g_glVp->renderer().setDeformScale(scale);
}
float viewportDeformScale() {
    return g_glVp ? g_glVp->renderer().deformScale() : 0.f;
}
bool viewportScalarRange(float& vmin, float& vmax) {
    return g_glVp && g_glVp->renderer().scalarRange(vmin, vmax);
}

void setViewportPipeline(aicad::viewport::GlViewport* viewport) {
    g_glVp = viewport;
    // A7: register the screen->viewport-local luminance sampler ONCE. Captureless lambda reads
    // the file-static published rect (s_bdMin/s_bdMax/s_bdLive), updated each frame by
    // publishBackdrop()/invalidateBackdrop() below.
    static bool s_samplerRegistered = false;
    if (!s_samplerRegistered) {
        theme::setLuminanceSampler(+[](ImVec2 mn, ImVec2 mx) -> float {
            if (!s_bdLive || !g_glVp) return -1.f;
            ImVec2 cmn(std::min(std::max(mn.x, s_bdMin.x), s_bdMax.x),
                      std::min(std::max(mn.y, s_bdMin.y), s_bdMax.y));
            ImVec2 cmx(std::min(std::max(mx.x, s_bdMin.x), s_bdMax.x),
                      std::min(std::max(mx.y, s_bdMin.y), s_bdMax.y));
            // A rect fully OUTSIDE the viewport clamps to a degenerate edge column/row, which
            // can sample the 1px border line instead of the scene. Grow such rects ~24px into
            // the viewport so side panels adapt to real scene content.
            const float kInset = std::max(24.f, (s_bdMax.x - s_bdMin.x) * 0.15f);
            if (cmx.x - cmn.x < 4.f) {
                if (cmn.x <= s_bdMin.x + 1.f) cmx.x = std::min(s_bdMin.x + kInset, s_bdMax.x);
                else                          cmn.x = std::max(s_bdMax.x - kInset, s_bdMin.x);
            }
            if (cmx.y - cmn.y < 4.f) {
                if (cmn.y <= s_bdMin.y + 1.f) cmx.y = std::min(s_bdMin.y + kInset, s_bdMax.y);
                else                          cmn.y = std::max(s_bdMax.y - kInset, s_bdMin.y);
            }
            ViewportRect vr{cmn.x - s_bdMin.x, cmn.y - s_bdMin.y,
                           cmx.x - s_bdMin.x, cmx.y - s_bdMin.y};
            return g_glVp->averageLuminance(vr);
        });
        s_samplerRegistered = true;
    }
}

void setViewportBackdropActive(bool active) {
    if (active) {
        if (g_glVp) g_glVp->setBackdropEnabled(true);
        return;
    }
    // A6: Viewport###viewport window body did not execute this frame (hidden tab / no doc) —
    // invalidate the published backdrop and stop the renderer's blur/luminance work outright.
    invalidateBackdrop();
    if (g_glVp) g_glVp->setBackdropEnabled(false);
}

GlassTimings viewportGlassTimings() {
    GlassTimings t;
    if (g_glVp) {
        const auto gt = g_glVp->glassTimings();
        t.sceneMs = gt.sceneMs;
        t.blurMs  = gt.blurMs;
        t.lumaMs  = gt.lumaMs;
        t.valid   = gt.valid;
    }
    return t;
}

void drawViewport(AppState& s) {
    const float S = theme::S();
    g_vpState.displayStyle = s.viewStyle;

    ImGuiDockNode* node = ImGui::GetWindowDockNode();
    if (node) node->LocalFlags |= ImGuiDockNodeFlags_NoTabBar;
    if (docTabStripHeight(s) > 0.f) drawDocTabs(s, ImGui::GetContentRegionAvail().x);

    // H3: the Drafting workspace hosts the 2D drawing sheet INSIDE this same central Viewport
    // window instead of the 3D render — mirroring host, which swaps the central area's content by
    // active-tab kind rather than docking Drawing as a separate tab (unlike CAM/ECAD, which share
    // this dock node as their own windows). "make_drawing" (app/app_state.cpp dispatch) flips
    // workspace to Drafting; drawDrawingSheet self-creates the drawing on first render.
    // H4: sim tick() runs EVERY frame drawViewport draws (not gated to the Simulate workspace) so a
    // result overlay left showing gets restored to the editable model the instant the user navigates
    // away (e.g. back to Model) — mirrors host's tick() running unconditionally each frame. Cheap when
    // idle (no doc / no studies): syncResultView + the owner-gated SimClient drain both early-out.
    ui::simManager().tick(s);

    if (s.workspace == cmd::Workspace::Drafting) {
        ui::drawDrawingSheet(s);
        return;
    }

    // H2: ECAD (Schematic/Board) workspaces host the ECAD editor INSIDE this viewport window
    // (content-swap, full takeover) instead of the 3D render. Moving it here — rather than keeping
    // it as a separate dock tab sharing the central node — is the fix for the headless surfacing
    // gap: the central dock node hides its tab bar (NoTabBar), so the separate "ECAD" dock window
    // was never brought forward in headless/offscreen shots. The begin("ECAD") block in
    // application.cpp is now a no-draw stub (kept for dock-layout registration, never draws).
    {
        const cmd::Workspace ews = s.effectiveWorkspace();
        if (ews == cmd::Workspace::Schematic || ews == cmd::Workspace::Board) {
            ui::drawEcadEditor(s);
            return;
        }
    }

    // H1: CAM panel content-swap. Same headless surfacing gap as ECAD above. The cam scenario
    // sets st.camActive = true after building the job; cleared when leaving to a non-CAM mode.
    if (s.camActive) {
        ui::drawCamManager(s);
        return;
    }

    // The Simulate workspace overlays the study tree/setup/results column ON TOP of the same 3D
    // model viewport (sim results — a colormap + legend — read on the part itself), unlike
    // Drafting's full takeover above. The column is drawn near the end of this function, after the
    // 3D scene + HUD (see drawEmbedded call below).

    ImVec2 p = ImGui::GetCursorScreenPos();
    ImVec2 sz = ImGui::GetContentRegionAvail();
    if (sz.x < 8 || sz.y < 8) return;

    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImVec2 a = p, b(p.x + sz.x, p.y + sz.y);
    ImGuiIO& io = ImGui::GetIO();

    const int vw = (int)sz.x, vh = (int)sz.y;
    bool live = false;
    bool imgHovered = false;

    Renderer* r = renderer();
    // GL path whenever the pipeline exists: with no mesh the renderer still draws the studio
    // background, which feeds the liquid-glass backdrop + luminance from FIRST LAUNCH (before,
    // an empty scene fell back to a flat draw-list gradient and every panel lost its frost).
    if (g_glVp) {
        if (!s.inSketch) tickCameraAnimation(s);
        else cancelCameraAnimation();

        renderViewport(s, vw, vh);
        live = r && r->hasMesh();
        g_glVp->setBackdropEnabled(true);  // frost from the studio bg even with no mesh
        publishBackdrop(a, b);

        if (live) {
            ImGui::SetNextItemAllowOverlap();
            ImGui::Image((ImTextureID)(intptr_t)g_glVp->colorTextureId(), sz, ImVec2(0, 1), ImVec2(1, 0));
            imgHovered = ImGui::IsItemHovered();

            if (imgHovered && !s_viewSelector.active() && !s_gestures.active()) {
                // T-BL46: left-button drag threshold and capture priority.
                // (a) kLmbDragPx — dead zone before left-button camera nav engages. 8 px is
                //     large enough to absorb accidental micro-drags while starting a click
                //     but small enough that intentional view-drag is not sluggish.
                // (b) s_sketch.elementDragActive() — capture priority: if the sketch overlay
                //     has armed or is executing an element drag (handle reshape, group/profile
                //     translate, dim-text drag) the LEFT BUTTON must not also navigate the
                //     camera. Middle/right buttons remain unambiguous view-nav and keep 0 px.
                //     The overlay arms on mouse-down and promotes to live drag past 4 px; both
                //     states must block camera nav or a fast drag still steals to the view.
                static constexpr float kLmbDragPx = 8.f;
                const bool sketchCapture = s.inSketch && s_sketch.elementDragActive();

                bool dragging = ImGui::IsMouseDragging(ImGuiMouseButton_Middle, 0.f) ||
                                ImGui::IsMouseDragging(ImGuiMouseButton_Right, 0.f) ||
                                ImGui::IsMouseDragging(ImGuiMouseButton_Left, kLmbDragPx);

                if (ImGui::IsMouseDragging(ImGuiMouseButton_Middle, 0.f)) {
                    if (io.KeyCtrl) g_glVp->pan(io.MouseDelta.x, io.MouseDelta.y);
                    else            g_glVp->orbit(-io.MouseDelta.x * 0.01f, io.MouseDelta.y * 0.01f);
                } else if (ImGui::IsMouseDragging(ImGuiMouseButton_Right, 0.f)) {
                    g_glVp->pan(io.MouseDelta.x, io.MouseDelta.y);
                } else if (!sketchCapture && g_vpState.vpDragMode == 1 &&
                           ImGui::IsMouseDragging(ImGuiMouseButton_Left, kLmbDragPx)) {
                    g_glVp->pan(io.MouseDelta.x, io.MouseDelta.y);
                } else if (!sketchCapture && g_vpState.vpDragMode == 0 &&
                           ImGui::IsMouseDragging(ImGuiMouseButton_Left, kLmbDragPx) &&
                           !s_zoomAreaArmed) {
                    g_glVp->orbit(-io.MouseDelta.x * 0.01f, io.MouseDelta.y * 0.01f);
                }

                if (io.MouseWheel != 0.f) g_glVp->zoom(io.MouseWheel);

                if (ImGui::IsMouseDoubleClicked(ImGuiMouseButton_Middle)) {
                    g_vpState.zoom = 1.f;
                    g_vpState.pan[0] = g_vpState.pan[1] = g_vpState.pan[2] = 0.f;
                    applyViewPreset(s, ViewPreset::ZoomFit);
                }

                aicad::ui::CameraState cam = g_glVp->camera();
                g_vpState.yaw = cam.yaw;
                g_vpState.pitch = cam.pitch;
                g_vpState.zoom = cam.distance;
                g_vpState.pan[0] = cam.target[0];
                g_vpState.pan[1] = cam.target[1];
                g_vpState.pan[2] = cam.target[2];

                if (s_zoomAreaArmed && ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
                    s_zoomAreaDragging = true;
                    s_zoomAreaP0 = s_zoomAreaP1 = io.MousePos;
                }
                if (s_zoomAreaDragging) {
                    s_zoomAreaP1 = io.MousePos;
                    if (!ImGui::IsMouseDown(ImGuiMouseButton_Left)) {
                        s_zoomAreaDragging = false;
                        s_zoomAreaArmed = false;
                    }
                }

                if (!dragging && r && g_glVp) {
                    aicad::ui::PickRequest req;
                    req.x = (int)(io.MousePos.x - a.x);
                    req.y = (int)(io.MousePos.y - a.y);
                    req.width = vw;
                    req.height = vh;
                    aicad::ui::PickResult pr = g_glVp->pick(req);
                    if (pr.hit) s.selCount = 1;
                    else if (ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !io.KeyShift)
                        s.selCount = 0;
                }
            }
        } else {
            // Empty scene: the GL studio backdrop just rendered above — show it (glass panels
            // frost over it) instead of the old flat draw-list gradient.
            ImGui::Image((ImTextureID)(intptr_t)g_glVp->colorTextureId(), sz, ImVec2(0, 1), ImVec2(1, 0));
            imgHovered = ImGui::IsItemHovered();
            const char* hint = s.engineReady ? "Building preview..." : "3D viewport  -  engine offline (demo mode)";
            ImVec2 ts = ImGui::CalcTextSize(hint);
            const theme::AdaptiveSurface& hs =
                theme::adaptivePanel(theme::panelId("vp_hint"),
                                     ImRect(ImVec2(a.x, b.y - 40.f * S), b));
            theme::glassText(dl, nullptr, ImGui::GetFontSize(),
                             ImVec2((a.x + b.x) * 0.5f - ts.x * 0.5f, b.y - 28.f * S), hs, hint);
        }
    } else {
        dl->AddRectFilledMultiColor(a, b, TCOL(viewport0), TCOL(viewport0), TCOL(viewport1), TCOL(viewport1));
        ImGui::InvisibleButton("##vp", sz);
        imgHovered = ImGui::IsItemHovered();
        if (g_glVp) g_glVp->setBackdropEnabled(false);
        invalidateBackdrop();
    }

    if (r) {
        auto host = sketch::makeHost(s, r);
        host.yaw = g_vpState.yaw;
        host.pitch = g_vpState.pitch;
        host.zoom = g_vpState.zoom;
        host.pan[0] = g_vpState.pan[0];
        host.pan[1] = g_vpState.pan[1];
        host.pan[2] = g_vpState.pan[2];
        host.sketchPlanePickActive = s.inSketch;
        host.hasDocMesh = live;
        host.idPass = &g_glVp->idPass();

        // T-BL47/48: drain pending sketch commands from AppState. dispatch() pushes ribbon
        // command ids; the Feature Manager pushes an edit/reparent target fid. Both are executed
        // here — the only place SketchHost + the single s_sketch instance exist.
        bool drainedSketch = !s.pendingSketchCmds.empty()
                          || !s.pendingSketchEditFid.empty()
                          || !s.pendingSketchReparentFid.empty();
        if (drainedSketch) {
            // Propagate the current app selection so enterNewOnSelection can pick up a
            // pre-selected plane (e.g. "plane.front") and enter directly without plane-pick.
            host.selectedFeatureId = s.selectedFeature;
            for (const auto& cmd : s.pendingSketchCmds) {
                s_sketch.onRibbonCommand(host, cmd);
            }
            s.pendingSketchCmds.clear();
            // T-BL48: edit an existing sketch from the Feature Manager (double-click / ctx-menu).
            // enterForFeature opens the sketcher directly on the target fid (active_ = true).
            if (!s.pendingSketchEditFid.empty()) {
                s_sketch.enterForFeature(host, s.pendingSketchEditFid);
                s.pendingSketchEditFid.clear();
            }
            // T-BL48: "Edit Sketch Plane/Face" — arm plane-pick to reparent an existing sketch
            // (active_ stays false, planePicking_ becomes true until the user picks a plane).
            if (!s.pendingSketchReparentFid.empty()) {
                s_sketch.beginPlaneEdit(host, s.pendingSketchReparentFid);
                s.pendingSketchReparentFid.clear();
            }
            // T-BL47 write-back: enterForFeature sets host.yaw/pitch/zoom/pan on the copy;
            // push them back to g_vpState so syncCameraToGl picks them up this frame.
            g_vpState.yaw    = host.yaw;
            g_vpState.pitch  = host.pitch;
            g_vpState.zoom   = host.zoom;
            g_vpState.pan[0] = host.pan[0];
            g_vpState.pan[1] = host.pan[1];
            g_vpState.pan[2] = host.pan[2];
            // Keep inSketch in sync with the sketcher state. Include planePicking(): a
            // sketch_new with no pre-selected plane arms plane-pick (active_ still false),
            // and the plane-pick overlay only draws while inSketch — dropping to active()
            // alone would kill the overlay before the user can pick a plane (T-BL48).
            s.inSketch = s_sketch.active() || s_sketch.planePicking();
            s.syncViewModel();
        }

        s_sketch.drawGhosts(host, a, b, vw, vh);
        if (s.inSketch)
            s_sketch.drawOverlay(host, io, a, b, vw, vh, imgHovered);

        // T-BL44: Space orients the view normal-to the active sketch plane ("Normal To").
        // cameraLock() returns the fronto-parallel yaw/pitch that sketch entry computed from the
        // plane normal (sketch_mode.cpp enterForFeature); writing g_vpState re-orients the live
        // camera via syncCameraToGl() next frame. Instant by design — the app cancels camera tweens
        // while a sketch is active (cancelCameraAnimation, ~L626), and sketch entry itself snaps, so
        // a tween would be fought frame-by-frame. Runs after the orbit read-back above so it is not
        // clobbered. Space is otherwise free in sketch mode: the view-cube's Space handler is gated
        // off during sketch (line ~786 !s.inSketch) and drawing_panel's Space-pan is the 2D Drafting
        // workspace. Guarded on !WantTextInput so the Modify box keeps Space for text entry.
        if (s.inSketch && !io.WantTextInput && ImGui::IsKeyPressed(ImGuiKey_Space, false)) {
            float nYaw = 0.f, nPitch = 0.f;
            if (s_sketch.cameraLock(nYaw, nPitch)) {
                g_vpState.yaw = nYaw;
                g_vpState.pitch = nPitch;
            }
        }
    }

    s_shortcutBar.update(s, imgHovered, s.inSketch, a, b);
    s_shortcutBar.draw(s, s.inSketch);

    if (r && live && !s.inSketch) {
        if (!s_gestures.active() && !s_viewSelector.active())
            s_gestures.update(s, imgHovered, a, b);
        s_gestures.draw(dl);

        s_viewSelector.update(s, *r, imgHovered, s.inSketch, a, b);
        s_viewSelector.draw(s, *r, dl, a, b);

        s_sectionTool.updateAndDraw(s, *r, imgHovered, s.inSketch, a, b);
    }

    if (r)
        drawHeadsUpToolbar(s, io, dl, a, b, *r, s.inSketch);
    if (s.workspace == cmd::Workspace::Simulate) ui::simManager().drawEmbedded(s, a, b);
    drawLegend(s, dl, a, b);
    drawOrientationGizmo(dl, ImVec2(b.x - 52.f * S, a.y + 52.f * S));

    if (!s.docs.empty()) {
        ImFont* nameFont = theme::font(theme::Font::H2);
        const std::string& name = s.doc().name;
        ImVec2 pos(a.x + 14.f * S, a.y + sz.y - 30.f * S);
        ImVec2 ts = nameFont->CalcTextSizeA(nameFont->FontSize, 1e9f, 0.f, name.c_str());
        const theme::AdaptiveSurface& nameSurf = theme::adaptivePanel(
            theme::panelId("doc_name_overlay"), ImRect(pos, ImVec2(pos.x + ts.x, pos.y + ts.y)));
        theme::glassText(dl, nameFont, nameFont->FontSize, pos, nameSurf, name.c_str());
    }
}

}  // namespace ui
