// AI-CAD title bar — left-group chips (File/Search/Settings) + centered workspace tabs +
// window caption buttons. Publishes fine-grained chrome::HitLayout spans so every
// interactive element is click-through on Windows and Linux; the remaining area is draggable.
//
// LAYOUT STRATEGY (fd6830fa follow-up fix):
//   • All three left chips (File/Search/Settings) are placed via explicit SetCursorScreenPos +
//     InvisibleButton so they never fight ImGui's auto-advance after BeginMenu.
//   • File opens its popup via OpenPopup/BeginPopup rather than BeginMenu so it matches the
//     manual layout path; menu behavior is identical to the prior FilMenuDrawer.
//   • HitLayout spans are window-relative (what both Windows WndProc and Linux tick() expect)
//     so coords are consistently (screenX - winX).
//   • Workspace tabs have a THREE-TIER rest/hover/active background so every button is
//     obviously clickable at rest.
#define GLFW_INCLUDE_NONE
#include <GLFW/glfw3.h>

#include "imgui.h"
#include "imgui_internal.h"

#include "title_bar.h"

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"   // ui::uiScale, ui::palette
#include "metrics.h"           // ui::metrics::stroke / titleFramePad tokens
#include "doc_tabs.h"          // ui::openNewDocChooser (File > New)
#include "feature_tree.h"      // ui::featureTreeActivePath (Save vs. Save As gating)
#include "property_manager.h"  // PropertyManager::active() (used in View sub-menu)

#include <filesystem>      // Recent-files menu labels
#include <cstdio>          // snprintf

// ---------------- caption buttons (shared small ui helper) ----------------
bool windowButton(const char* id, WinBtn kind, float h, bool maximized) {
    // C25-W3G: button width tracks uiScale so buttons stay clickable at 1.5x/2.0x.
    const float btnW = h * (1.35f + 0.15f * (ui::uiScale - 1.0f));
    const ImVec2 size(btnW, h);
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    bool clicked = ImGui::InvisibleButton(id, size);
    bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (hovered) {
        // Close keeps the Windows-standard caption red (intentional literal); min/max hover
        // uses the palette role so the wash reads on the light theme too (a white wash on the
        // near-white light headerBg was invisible).
        ImU32 bg = kind == BTN_CLOSE ? IM_COL32(232, 17, 35, 255)
                                     : ui::Palette::toU32(ui::palette.buttonHovered);
        dl->AddRectFilled(p0, ImVec2(p0.x + size.x, p0.y + size.y), bg);
    }
    // Caption glyph color from palette so it adapts to light/dark themes.
    const unsigned char fgv = (unsigned char)(ui::palette.captionFg * 255.0f + 0.5f);
    const ImU32 fg = (hovered && kind == BTN_CLOSE) ? IM_COL32(255, 255, 255, 255)
                                                    : IM_COL32(fgv, fgv, fgv, 235);
    const ImVec2 c(p0.x + size.x * 0.5f, p0.y + size.y * 0.5f);
    const float s = h * 0.16f;  // glyph half-size
    const float th = ui::metrics::stroke(1.4f);  // hairline at 2x DPI
    if (kind == BTN_MIN) {
        dl->AddLine(ImVec2(c.x - s, c.y), ImVec2(c.x + s, c.y), fg, th);
    } else if (kind == BTN_MAX) {
        if (maximized) {  // restore: two stacked squares
            dl->AddRect(ImVec2(c.x - s + 2 * ui::uiScale, c.y - s), ImVec2(c.x + s, c.y + s - 2 * ui::uiScale), fg, 0, 0, th);
            dl->AddRect(ImVec2(c.x - s, c.y - s + 2 * ui::uiScale), ImVec2(c.x + s - 2 * ui::uiScale, c.y + s), fg, 0, 0, th);
        } else {
            dl->AddRect(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, 0, 0, th);
        }
    } else {  // BTN_CLOSE: an X
        dl->AddLine(ImVec2(c.x - s, c.y - s), ImVec2(c.x + s, c.y + s), fg, th);
        dl->AddLine(ImVec2(c.x - s, c.y + s), ImVec2(c.x + s, c.y - s), fg, th);
    }
    return clicked;
}

// Wave 1C: buildDefaultLayout() moved to ui/layout.cpp.

namespace ui {

// Glass v2.1: the title bar stays a SOLID chrome band (locked design) but subtly adopts the
// Global glass group's user tint — mirror of ribbon_chromeTint in widgets/ribbon.cpp.
static ImVec4 titleBar_chromeTint(const float base[4]) {
    const ui::GlassStyleResolved& gs = ui::glassStyleResolved(ui::GlassGroup::Global);
    ImVec4 c(base[0], base[1], base[2], base[3]);
    if (gs.tintStrength <= 0.001f || gs.tintSat <= 0.001f) return c;
    float h, sat, v;
    ImGui::ColorConvertRGBtoHSV(c.x, c.y, c.z, h, sat, v);
    float tr, tg, tb;
    ImGui::ColorConvertHSVtoRGB(gs.tintHue / 360.0f, gs.tintSat, v, tr, tg, tb);
    float m = gs.tintStrength * 0.35f;
    if (m > 1.0f) m = 1.0f;
    c.x += (tr - c.x) * m;
    c.y += (tg - c.y) * m;
    c.z += (tb - c.z) * m;
    return c;
}

// ---- procedural line-art glyphs for the title-bar chips -----------------
// Vector-drawn with ImDrawList so they stay crisp at any uiScale and adapt to
// both themes (stroke color = the chip's text color). Consistent stroke width
// (1.5px * uiScale) and visual weight across all glyphs.
enum class TbIcon {
    File, Search, Settings,
    Model, Drawing, Simulate, Electronics, Manufacture, PLM, PartsLibrary, AI,
};

static void drawTbIcon(ImDrawList* dl, TbIcon icon, ImVec2 c, float sz, ImU32 col) {
    const float th = ui::metrics::stroke(1.5f);
    const float r  = sz * 0.5f;
    switch (icon) {
    case TbIcon::File: {
        // Document sheet with a folded top-right corner.
        const float w = r * 0.72f, h = r * 0.95f, f = r * 0.45f;
        dl->PathLineTo(ImVec2(c.x + w - f, c.y - h));
        dl->PathLineTo(ImVec2(c.x - w,     c.y - h));
        dl->PathLineTo(ImVec2(c.x - w,     c.y + h));
        dl->PathLineTo(ImVec2(c.x + w,     c.y + h));
        dl->PathLineTo(ImVec2(c.x + w,     c.y - h + f));
        dl->PathLineTo(ImVec2(c.x + w - f, c.y - h + f));
        dl->PathLineTo(ImVec2(c.x + w - f, c.y - h));
        dl->PathStroke(col, 0, th);
        break;
    }
    case TbIcon::Search: {
        // Magnifier: circle + handle toward bottom-right.
        const ImVec2 cc(c.x - r * 0.22f, c.y - r * 0.22f);
        const float cr = r * 0.60f;
        dl->AddCircle(cc, cr, col, 0, th);
        const float k = 0.7071f;
        dl->AddLine(ImVec2(cc.x + cr * k, cc.y + cr * k),
                    ImVec2(c.x + r * 0.92f, c.y + r * 0.92f), col, th);
        break;
    }
    case TbIcon::Settings: {
        // Gear: circle + 6 radial teeth.
        dl->AddCircle(c, r * 0.48f, col, 0, th);
        for (int k = 0; k < 6; ++k) {
            const float a = (float)k * (IM_PI / 3.0f) + IM_PI / 6.0f;
            const float ca = ImCos(a), sa = ImSin(a);
            dl->AddLine(ImVec2(c.x + ca * r * 0.58f, c.y + sa * r * 0.58f),
                        ImVec2(c.x + ca * r * 0.95f, c.y + sa * r * 0.95f), col, th);
        }
        break;
    }
    case TbIcon::Model: {
        // Isometric cube: hexagon outline + Y-shaped inner edges.
        ImVec2 v[6];
        for (int k = 0; k < 6; ++k) {
            const float a = IM_PI / 6.0f + (float)k * (IM_PI / 3.0f);  // 30°,90°,…,330°
            v[k] = ImVec2(c.x + ImCos(a) * r * 0.95f, c.y - ImSin(a) * r * 0.95f);
        }
        for (int k = 0; k < 6; ++k) dl->PathLineTo(v[k]);
        dl->PathStroke(col, ImDrawFlags_Closed, th);
        dl->AddLine(c, v[0], col, th);  //  30° — top-right
        dl->AddLine(c, v[2], col, th);  // 150° — top-left
        dl->AddLine(c, v[4], col, th);  // 270° — straight down
        break;
    }
    case TbIcon::Drawing: {
        // Pencil: diagonal body with converging tip (points to bottom-left).
        const float k = 0.7071f;
        const ImVec2 u(-k, k);          // toward the tip
        const ImVec2 p(k, k);           // perpendicular
        const float bw = r * 0.30f;     // half body width
        const ImVec2 tail(c.x - u.x * r * 0.85f, c.y - u.y * r * 0.85f);
        const ImVec2 shld(c.x + u.x * r * 0.35f, c.y + u.y * r * 0.35f);
        const ImVec2 tip (c.x + u.x * r * 0.95f, c.y + u.y * r * 0.95f);
        const ImVec2 tA(tail.x + p.x * bw, tail.y + p.y * bw);
        const ImVec2 tB(tail.x - p.x * bw, tail.y - p.y * bw);
        const ImVec2 sA(shld.x + p.x * bw, shld.y + p.y * bw);
        const ImVec2 sB(shld.x - p.x * bw, shld.y - p.y * bw);
        dl->AddLine(tA, sA, col, th);
        dl->AddLine(tB, sB, col, th);
        dl->AddLine(tA, tB, col, th);   // tail cap
        dl->AddLine(sA, tip, col, th);  // tip edges
        dl->AddLine(sB, tip, col, th);
        break;
    }
    case TbIcon::Simulate: {
        // Pulse / waveform zigzag.
        dl->PathLineTo(ImVec2(c.x - r * 0.95f, c.y));
        dl->PathLineTo(ImVec2(c.x - r * 0.45f, c.y));
        dl->PathLineTo(ImVec2(c.x - r * 0.18f, c.y - r * 0.78f));
        dl->PathLineTo(ImVec2(c.x + r * 0.18f, c.y + r * 0.70f));
        dl->PathLineTo(ImVec2(c.x + r * 0.45f, c.y));
        dl->PathLineTo(ImVec2(c.x + r * 0.95f, c.y));
        dl->PathStroke(col, 0, th);
        break;
    }
    case TbIcon::Electronics: {
        // IC chip: body square + 2 pins per side.
        const float b = r * 0.52f;
        dl->AddRect(ImVec2(c.x - b, c.y - b), ImVec2(c.x + b, c.y + b), col, r * 0.12f, 0, th);
        const float o = r * 0.26f, pe = r * 0.95f;
        for (int sgn = -1; sgn <= 1; sgn += 2) {
            const float d = (float)sgn * o;
            dl->AddLine(ImVec2(c.x - b, c.y + d), ImVec2(c.x - pe, c.y + d), col, th);
            dl->AddLine(ImVec2(c.x + b, c.y + d), ImVec2(c.x + pe, c.y + d), col, th);
            dl->AddLine(ImVec2(c.x + d, c.y - b), ImVec2(c.x + d, c.y - pe), col, th);
            dl->AddLine(ImVec2(c.x + d, c.y + b), ImVec2(c.x + d, c.y + pe), col, th);
        }
        break;
    }
    case TbIcon::Manufacture: {
        // End mill: narrow shank into a fluted cutting body with a flat tip.
        const float sw = r * 0.18f;   // shank half-width
        const float bw = r * 0.42f;   // body half-width
        const float yTop = c.y - r * 0.95f, yShld = c.y - r * 0.25f, yTip = c.y + r * 0.95f;
        dl->PathLineTo(ImVec2(c.x - sw, yTop));
        dl->PathLineTo(ImVec2(c.x - sw, yShld));
        dl->PathLineTo(ImVec2(c.x - bw, yShld));
        dl->PathLineTo(ImVec2(c.x - bw, yTip));
        dl->PathLineTo(ImVec2(c.x + bw, yTip));
        dl->PathLineTo(ImVec2(c.x + bw, yShld));
        dl->PathLineTo(ImVec2(c.x + sw, yShld));
        dl->PathLineTo(ImVec2(c.x + sw, yTop));
        dl->PathStroke(col, 0, th);
        // Two diagonal flutes across the body.
        dl->AddLine(ImVec2(c.x - bw, c.y + r * 0.30f), ImVec2(c.x + bw, c.y - r * 0.05f), col, th);
        dl->AddLine(ImVec2(c.x - bw, c.y + r * 0.75f), ImVec2(c.x + bw, c.y + r * 0.40f), col, th);
        break;
    }
    case TbIcon::PLM: {
        // Version branch: trunk with two nodes + a fork to a third node.
        const float nr = r * 0.24f;
        const ImVec2 n1(c.x - r * 0.50f, c.y - r * 0.62f);
        const ImVec2 n2(c.x - r * 0.50f, c.y + r * 0.62f);
        const ImVec2 n3(c.x + r * 0.60f, c.y - r * 0.55f);
        dl->AddLine(ImVec2(n1.x, n1.y + nr), ImVec2(n2.x, n2.y - nr), col, th);
        // Fork: leaves the trunk mid-way, angles up to the branch node.
        const ImVec2 mid(n1.x, c.y + r * 0.10f);
        dl->AddLine(mid, ImVec2(n3.x - nr * 0.7071f, n3.y + nr * 0.7071f), col, th);
        dl->AddCircle(n1, nr, col, 0, th);
        dl->AddCircle(n2, nr, col, 0, th);
        dl->AddCircle(n3, nr, col, 0, th);
        break;
    }
    case TbIcon::PartsLibrary: {
        // Grid of parts: 2x2 squares.
        const float half = r * 0.38f, off = r * 0.52f, rn = r * 0.08f;
        for (int ix = -1; ix <= 1; ix += 2)
            for (int iy = -1; iy <= 1; iy += 2) {
                const ImVec2 q(c.x + (float)ix * off, c.y + (float)iy * off);
                dl->AddRect(ImVec2(q.x - half, q.y - half), ImVec2(q.x + half, q.y + half),
                            col, rn, 0, th);
            }
        break;
    }
    case TbIcon::AI: {
        // Sparkle: 4-point star (outer points N/E/S/W, pinched diagonals).
        const float ro = r * 0.98f, ri = r * 0.28f, k = 0.7071f;
        dl->PathLineTo(ImVec2(c.x,           c.y - ro));
        dl->PathLineTo(ImVec2(c.x + ri * k,  c.y - ri * k));
        dl->PathLineTo(ImVec2(c.x + ro,      c.y));
        dl->PathLineTo(ImVec2(c.x + ri * k,  c.y + ri * k));
        dl->PathLineTo(ImVec2(c.x,           c.y + ro));
        dl->PathLineTo(ImVec2(c.x - ri * k,  c.y + ri * k));
        dl->PathLineTo(ImVec2(c.x - ro,      c.y));
        dl->PathLineTo(ImVec2(c.x - ri * k,  c.y - ri * k));
        dl->PathStroke(col, ImDrawFlags_Closed, th);
        break;
    }
    }
}

// ---- chip drawing helper -----------------------------------------------
// Draws a chip-shaped button in the title bar at the given screen-space p0/size.
// THREE TIERS: rest (subtle fill + subtle outline), hover (brighter), active (bold accent).
// Returns {clicked, hovered} after the InvisibleButton.
// NOTE: caller places InvisibleButton; this draws fill/label on top of it.
struct ChipResult { bool clicked; bool hovered; };

static ChipResult drawChipAt(const char* btnId,
                              const char* label,
                              ImVec2 p0, float chipW, float chipH,
                              bool isActive, float rnd,
                              ImFont* boldFont,
                              TbIcon icon, float iconSz, float iconGap) {
    ImGui::SetCursorScreenPos(p0);
    bool clicked = ImGui::InvisibleButton(btnId, ImVec2(chipW, chipH));
    bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + chipW, p0.y + chipH);

    if (isActive) {
        // Active: stronger fill + bold accent outline.
        dl->AddRectFilled(p0, p1, ui::Palette::toU32(ui::palette.buttonActive), rnd);
        dl->AddRect(p0, p1, ui::Palette::toU32(ui::palette.accent), rnd, 0,
                    ui::metrics::stroke(1.8f));
    } else if (hovered) {
        // Hover: brighter fill + subtle border.
        dl->AddRectFilled(p0, p1, ui::Palette::toU32(ui::palette.buttonHovered), rnd);
        dl->AddRect(p0, p1, ui::Palette::toU32(ui::palette.border), rnd, 0,
                    ui::metrics::stroke(1.0f));
    } else {
        // REST: subtle fill + subtle border — so the chip is OBVIOUSLY a button at rest.
        dl->AddRectFilled(p0, p1, ui::Palette::toU32(ui::palette.button), rnd);
        dl->AddRect(p0, p1, ui::Palette::toU32(ui::palette.border), rnd, 0,
                    ui::metrics::stroke(1.0f));
    }

    // Icon + label group centered in chip. Width is measured with the same reference
    // font as chipMeasureW (boldFont when present) so chip widths never jitter between
    // active/inactive frames.
    ImFont* drawFont = (isActive && boldFont) ? boldFont : ImGui::GetFont();
    float textW;
    if (boldFont)
        textW = boldFont->CalcTextSizeA(boldFont->FontSize, FLT_MAX, 0, label).x;
    else
        textW = ImGui::CalcTextSize(label).x;

    const ImVec4 textColV(ui::palette.text[0], ui::palette.text[1],
                          ui::palette.text[2], ui::palette.text[3]);
    const ImU32 textU32 = ImGui::ColorConvertFloat4ToU32(textColV);
    const float contentW = iconSz + iconGap + textW;
    const float gx = p0.x + (chipW - contentW) * 0.5f;  // group left edge
    drawTbIcon(dl, icon, ImVec2(gx + iconSz * 0.5f, p0.y + chipH * 0.5f),
               iconSz, textU32);
    const float tx = gx + iconSz + iconGap;
    const float ty = p0.y + (chipH - drawFont->FontSize) * 0.5f;
    dl->AddText(drawFont, drawFont->FontSize, ImVec2(tx, ty), textU32, label);

    return {clicked, hovered};
}

// Measure chip width (same formula as drawChipAt so sizes are consistent).
static float chipMeasureW(const char* label, float padX, ImFont* boldFont,
                          float iconSz, float iconGap) {
    float textW;
    if (boldFont)
        textW = boldFont->CalcTextSizeA(boldFont->FontSize, FLT_MAX, 0, label).x;
    else
        textW = ImGui::CalcTextSize(label).x;
    return iconSz + iconGap + textW + padX * 2.0f;
}

// ---- File menu contents (self-contained; called inside BeginPopup) ----------
struct FilMenuDrawer {
    AppState& st;
    bool hasDoc;
    GLFWwindow* win;
    void draw() const {
        if (ImGui::BeginMenu("New")) {
            if (ImGui::MenuItem("Part", "Ctrl+N")) ui::createNewDoc(st, 0);
            if (ImGui::MenuItem("Assembly"))       ui::createNewDoc(st, 1);
            const bool canDraw = st.activeTab >= 0 && st.activeTab < (int)st.docTabs.size() &&
                (st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Part ||
                 st.docTabs[st.activeTab].kind == AppState::DocTab::TabKind::Assembly);
            if (ImGui::MenuItem("Drawing", nullptr, false, canDraw)) ui::createNewDoc(st, 2);
            ImGui::Separator();
            if (ImGui::MenuItem("Schematic"))      ui::createNewDoc(st, 3);
            if (ImGui::MenuItem("PCB"))            ui::createNewDoc(st, 4);
            ImGui::EndMenu();
        }
        if (ImGui::MenuItem("Open...", "Ctrl+O")) st.requestOpenDialog = true;
        if (ImGui::MenuItem("Import...")) st.requestOpenDialog = true;
        ImGui::Separator();
        if (ImGui::MenuItem("Save", "Ctrl+S", false, hasDoc)) st.requestSave = true;
        if (ImGui::MenuItem("Save As...", "Ctrl+Shift+S", false, hasDoc)) st.requestSaveAs = true;
        if (ImGui::MenuItem("Save All", nullptr, false, hasDoc)) st.requestSaveAll = true;
        ImGui::Separator();
        const bool canExport3d = hasDoc && !st.activeTabIsDrawing() && !st.activeTabIsEcad();
        auto openMeshExport = [&](const char* fmt) {
            st.exportTess.open = true;
            st.exportTess.fmt = fmt;
            st.exportTess.estimateDirty = true;
            st.exportTess.estimateValid = false;
        };
        if (ImGui::BeginMenu("Export", canExport3d)) {
            if (ImGui::MenuItem("STEP (.step)...")) st.requestExportFormat = "step";
            if (ImGui::MenuItem("IGES (.iges)...")) st.requestExportFormat = "iges";
            if (ImGui::MenuItem("BREP (.brep)...")) st.requestExportFormat = "brep";
            ImGui::Separator();
            if (ImGui::MenuItem("STL (.stl)..."))   openMeshExport("stl");
            if (ImGui::MenuItem("OBJ (.obj)..."))   openMeshExport("obj");
            if (ImGui::MenuItem("PLY (.ply)..."))   openMeshExport("ply");
            if (ImGui::MenuItem("3MF (.3mf)..."))   openMeshExport("3mf");
            if (ImGui::MenuItem("glTF (.gltf)...")) openMeshExport("gltf");
            if (ImGui::MenuItem("GLB (.glb)..."))   openMeshExport("glb");
            ImGui::EndMenu();
        }
        ImGui::Separator();
        if (ImGui::BeginMenu("Recent Files", !st.recentFiles.empty())) {
            for (const std::string& rp : st.recentFiles) {
                std::string name = std::filesystem::path(rp).filename().string();
                if (ImGui::MenuItem(name.c_str())) st.requestOpenPath = rp;
                if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", rp.c_str());
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Clear Recent Files")) {
                st.recentFiles.clear();
                st.settings.recentFiles.clear();
                saveSettings(st.settings);
            }
            ImGui::EndMenu();
        }
        ImGui::Separator();
        // ---- Formerly View menu: panel show/hide, theme toggle, layout reset ----
        if (ImGui::BeginMenu("View")) {
            ImGui::MenuItem("Ribbon", nullptr, &st.showRibbon);
            ImGui::Separator();
            auto leftPaneEntry = [&](const char* label, int tab, bool enabled = true) {
                const bool activeEntry = st.showFeatureTree && st.leftPaneTab == tab;
                if (ImGui::MenuItem(label, nullptr, activeEntry, enabled)) {
                    if (activeEntry) {
                        st.showFeatureTree = false;
                    } else {
                        st.showFeatureTree = true;
                        st.leftPaneTab = tab;
                    }
                }
            };
            leftPaneEntry("FeatureManager",     AppState::LP_FeatureManager);
            leftPaneEntry("PropertyManager",    AppState::LP_PropertyManager,
                          st.propertyManager && st.propertyManager->active());
            leftPaneEntry("ConfigurationManager", AppState::LP_Configurations);
            leftPaneEntry("Explorer",           AppState::LP_Explorer);
            ImGui::MenuItem("Design Table",     nullptr, &st.showDesignTablePanel);
            ImGui::MenuItem("Cut List",         nullptr, &st.showCutListPanel);
            ImGui::MenuItem("Equations",        nullptr, &st.showEquations);
            ImGui::MenuItem("AI Panel",         nullptr, &st.showAIPanel);
            ImGui::MenuItem("Viewport",         nullptr, &st.showViewport);
            ImGui::Separator();
            bool isLight = (ui::activeTheme == ui::Theme::Light);
            if (ImGui::MenuItem("Light theme (SolidWorks-style)", nullptr, isLight)) {
                ui::Theme next = isLight ? ui::Theme::Dark : ui::Theme::Light;
                ui::applyTheme(next, st.scale);
                st.settings.theme = (next == ui::Theme::Light) ? "light" : "dark";
                saveSettings(st.settings);
                if (st.renderer)
                    st.renderer->setBackground(
                        ui::palette.viewportTop[0], ui::palette.viewportTop[1],
                        ui::palette.viewportTop[2], ui::palette.viewportBottom[0],
                        ui::palette.viewportBottom[1], ui::palette.viewportBottom[2]);
            }
            ImGui::Separator();
            if (ImGui::MenuItem("Reset layout")) st.resetLayout = true;
            ImGui::EndMenu();
        }
        ImGui::Separator();
        // ---- Formerly Help menu essentials ----
        if (ImGui::MenuItem("AI Help")) {
            st.showAIPanel = true;
            st.aiInteraction = 2;  // Ask
        }
        if (ImGui::MenuItem("About ShopCraft")) st.showAbout = true;
        ImGui::Separator();
        // Repair/install runtime
        if (ImGui::MenuItem("Repair / install runtime", nullptr, false,
                            !st.setupRunning.load() && !st.job.running.load()))
            st.startSetup(true);
        ImGui::Separator();
        if (ImGui::MenuItem("Quit", "Alt+F4")) glfwSetWindowShouldClose(win, 1);
    }
};

void drawTitleBar(AppState& st, bool maximized, float& barHOut) {
    GLFWwindow* win = st.win;
    chrome::HitLayout& hitLayout = *st.hitLayout;

    float s = ui::uiScale;

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(ui::metrics::titleFramePadX(), ui::metrics::titleFramePadY()));
    const float barH = ImGui::GetFrameHeight();
    hitLayout.titleBarHeight = (int)barH;
    hitLayout.clearSpans();  // reset fine-grained spans each frame

    // Solid chrome band, subtly following the Global glass tint.
    ImGui::PushStyleColor(ImGuiCol_MenuBarBg, titleBar_chromeTint(ui::palette.headerBg));
    if (ImGui::BeginMainMenuBar()) {
        const float winX = ImGui::GetWindowPos().x;  // window origin in screen space
        const float winW = ImGui::GetWindowWidth();
        const float bw   = barH * (1.35f + 0.15f * (s - 1.0f));  // one caption button width
        const float captionZoneW = bw * 3.0f;

        // Right boundary of the usable zone (= where caption buttons begin), window-relative.
        const float rightGroupStartRelX = winW - captionZoneW - 4.0f * s;

        // ---- Brand wordmark / logo (left edge) -----------------------------------
        ImGui::SetCursorPosX(12.0f * s);
        const Tex& wm = (ui::activeTheme == ui::Theme::Light && st.wordmarkDark.id)
                            ? st.wordmarkDark : st.wordmark;
        float logoEndRelX = 12.0f * s;  // window-relative right edge of logo
        if (wm.id) {
            const float wmH = barH * 0.40f;
            float wmW = wmH * (float)wm.w / wm.h;
            if (ImGui::GetCursorPosX() + wmW + 18.0f * s < rightGroupStartRelX - 240.0f * s) {
                ImGui::SetCursorPosY((barH - wmH) * 0.5f);
                ImGui::Image((ImTextureID)(intptr_t)wm.id, ImVec2(wmW, wmH));
                // GetItemRectMax().x is the screen-right edge of the Image item — reliable even
                // after ImGui resets CursorPosX to indent. Convert to window-relative.
                logoEndRelX = ImGui::GetItemRectMax().x - winX;
                ImGui::SameLine(0, 16.0f * s);
            }
        } else if (st.logoIcon.id) {
            const float iconH = barH * 0.52f;
            if (ImGui::GetCursorPosX() + iconH + 18.0f * s < rightGroupStartRelX - 240.0f * s) {
                ImGui::SetCursorPosY((barH - iconH) * 0.5f);
                ImGui::Image((ImTextureID)(intptr_t)st.logoIcon.id, ImVec2(iconH, iconH));
                logoEndRelX = ImGui::GetItemRectMax().x - winX;
                ImGui::SameLine(0, 16.0f * s);
            }
        }

        // ---- LEFT GROUP: File | Search | Settings — ALL placed via explicit screen coords -----
        // This avoids BeginMenu's auto-advance fighting SameLine which caused the overlap bug.
        const bool hasDoc = (st.activeTab >= 0);
        const float chipVPad  = 4.0f * s;      // vertical inset from bar top/bottom
        const float chipRound = barH * 0.28f;  // moderately rounded corners
        const float chipH     = barH - chipVPad * 2.0f;
        const float padX      = 10.0f * s;     // horizontal text padding inside each chip
        const float chipGap   = 4.0f * s;      // gap between chips

        ImFont* boldFont = st.fonts.medium ? st.fonts.medium : nullptr;

        // Line-art icon metrics shared by every chip (left group + workspace tabs):
        // ~text-height glyph, small gap before the label.
        const float iconSz  = (boldFont ? boldFont->FontSize : ImGui::GetFontSize()) * 0.92f;
        const float iconGap = 5.0f * s;

        // Compute widths for all three left chips.
        const float fileW     = chipMeasureW("File",     padX, boldFont, iconSz, iconGap);
        const float searchW   = chipMeasureW("Search",   padX, boldFont, iconSz, iconGap);
        const float settingsW = chipMeasureW("Settings", padX, boldFont, iconSz, iconGap);

        // Start x: just after logo (screen-relative → then convert to screen-absolute for SetCursorScreenPos).
        // logoEndRelX is already window-relative cursor position; convert to screen.
        const float leftGroupStartScreenX = winX + logoEndRelX;
        // If logo wasn't drawn, cursor is at 12*s (rel). LogoEndRelX covers both cases.

        float cx = leftGroupStartScreenX;  // current screen x for left group placement
        const float barTopScreenY = ImGui::GetWindowPos().y;

        // -- File chip --
        {
            const ImVec2 chipP0(cx, barTopScreenY + chipVPad);
            auto res = drawChipAt("##tb_file", "File", chipP0, fileW, chipH,
                                  false /*never "active"*/, chipRound, boldFont,
                                  TbIcon::File, iconSz, iconGap);
            // Window-relative span for hit-testing (both Windows WndProc and Linux tick use relX).
            hitLayout.addSpan((int)(cx - winX), (int)(cx - winX + fileW));
            if (res.clicked) ImGui::OpenPopup("##file_popup");
            cx += fileW + chipGap;
        }

        // -- Search chip --
        {
            const ImVec2 chipP0(cx, barTopScreenY + chipVPad);
            auto res = drawChipAt("##tb_search", "Search", chipP0, searchW, chipH,
                                  false, chipRound, boldFont,
                                  TbIcon::Search, iconSz, iconGap);
            hitLayout.addSpan((int)(cx - winX), (int)(cx - winX + searchW));
            if (res.clicked) st.showCommandSearch = true;
            cx += searchW + chipGap;
        }

        // -- Settings chip --
        {
            const ImVec2 chipP0(cx, barTopScreenY + chipVPad);
            auto res = drawChipAt("##tb_settings", "Settings", chipP0, settingsW, chipH,
                                  false, chipRound, boldFont,
                                  TbIcon::Settings, iconSz, iconGap);
            hitLayout.addSpan((int)(cx - winX), (int)(cx - winX + settingsW));
            if (res.clicked) st.openSettings = true;
            cx += settingsW + chipGap;
        }

        // File popup (positioned below the File chip).
        const float filePopupScreenX = leftGroupStartScreenX;
        ImGui::SetNextWindowPos(ImVec2(filePopupScreenX, barTopScreenY + barH), ImGuiCond_Always);
        if (ImGui::BeginPopup("##file_popup")) {
            FilMenuDrawer fmd{st, hasDoc, win};
            fmd.draw();
            ImGui::EndPopup();
        }

        // Legacy coarse spans (for code that reads menuLeft/menuRight directly).
        hitLayout.menuLeft  = (int)(leftGroupStartScreenX - winX);
        hitLayout.menuRight = (int)(cx - winX);

        // ---- Subtle vertical separator between left group and workspace tabs ----
        {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const float sepScreenX = cx + 6.0f * s;
            const float sepY0 = barTopScreenY + barH * 0.20f;
            const float sepY1 = barTopScreenY + barH * 0.80f;
            ImU32 sepCol = ui::Palette::toU32(ui::palette.border);
            sepCol = (sepCol & 0x00FFFFFF) | 0x60000000u;
            dl->AddLine(ImVec2(sepScreenX, sepY0), ImVec2(sepScreenX, sepY1), sepCol, 1.5f * s);
        }

        // Left group ends here (screen-absolute); convert to window-relative for centering math.
        const float leftGroupEndRelX = (cx + 14.0f * s) - winX;  // +separator width

        // ---- CENTER GROUP: workspace tabs, centered between left group and caption buttons ----
        struct WsTab { const char* label; AppState::Workspace ws; TbIcon icon; };
        static const WsTab kTabs[] = {
            {"Model",        AppState::Workspace::Model,        TbIcon::Model},
            {"Drawing",      AppState::Workspace::Drawing,      TbIcon::Drawing},
            {"Simulate",     AppState::Workspace::Simulate,     TbIcon::Simulate},
            {"Electronics",  AppState::Workspace::Electronics,  TbIcon::Electronics},
            {"Manufacture",  AppState::Workspace::Manufacture,  TbIcon::Manufacture},
            {"PLM",          AppState::Workspace::PLM,          TbIcon::PLM},
            {"Parts Library",AppState::Workspace::PartsLibrary, TbIcon::PartsLibrary},
            {"AI",           AppState::Workspace::AI,           TbIcon::AI},
        };
        constexpr int kTabCount = (int)(sizeof(kTabs) / sizeof(kTabs[0]));

        const float tabRound = barH * 0.30f;
        const float tabGapX  = 3.0f * s;    // gap between tabs
        const float tabPadX  = 10.0f * s;   // horizontal text padding inside each tab

        // Measure total width of all tabs.
        float totalTabsW = 0.0f;
        for (int i = 0; i < kTabCount; ++i) {
            totalTabsW += chipMeasureW(kTabs[i].label, tabPadX, boldFont, iconSz, iconGap);
            if (i < kTabCount - 1) totalTabsW += tabGapX;
        }

        // Available center zone, window-relative.
        const float centerZoneW = rightGroupStartRelX - leftGroupEndRelX;

        // Compute where tabs start, window-relative, then convert to screen for drawing.
        float tabsStartRelX;
        if (totalTabsW + 8.0f * s <= centerZoneW) {
            // Centered: equal margin on each side.
            tabsStartRelX = leftGroupEndRelX + (centerZoneW - totalTabsW) * 0.5f;
        } else {
            // Compressed: start right after left group (no overlap with OS buttons due to clip below).
            tabsStartRelX = leftGroupEndRelX + 4.0f * s;
        }

        ImDrawList* dl = ImGui::GetWindowDrawList();
        float tx = winX + tabsStartRelX;  // current tab x, screen-absolute

        for (int i = 0; i < kTabCount; ++i) {
            const bool isActive = (st.activeWorkspace == kTabs[i].ws);
            const float chipW = chipMeasureW(kTabs[i].label, tabPadX, boldFont, iconSz, iconGap);

            // Clip tab if it would overlap the OS controls area (window-relative check).
            if ((tx - winX) + chipW > rightGroupStartRelX - 2.0f * s) break;

            const ImVec2 p0(tx, barTopScreenY + chipVPad);

            // drawChipAt places InvisibleButton + draws fill/label.
            char btnId[32];
            snprintf(btnId, sizeof(btnId), "##ws_%d", i);
            auto res = drawChipAt(btnId, kTabs[i].label, p0, chipW, chipH,
                                  isActive, tabRound, boldFont,
                                  kTabs[i].icon, iconSz, iconGap);

            // Register window-relative span.
            hitLayout.addSpan((int)(tx - winX), (int)(tx - winX + chipW));

            if (res.clicked) st.activeWorkspace = kTabs[i].ws;

            tx += chipW + tabGapX;
        }

        // ---- Window caption buttons (right-aligned, unchanged) ------------------
        const float bx = winW - captionZoneW;  // window-relative
        hitLayout.controlsLeft  = (int)bx;
        hitLayout.controlsRight = (int)(bx + captionZoneW);
        hitLayout.addSpan((int)bx, (int)(bx + captionZoneW));

        ImGui::SetCursorPos(ImVec2(bx, 0.0f));
        if (windowButton("##min", BTN_MIN, barH, false)) glfwIconifyWindow(win);
        ImGui::SetCursorPos(ImVec2(bx + bw, 0.0f));
        if (windowButton("##max", BTN_MAX, barH, maximized)) {
            if (maximized) glfwRestoreWindow(win);
            else glfwMaximizeWindow(win);
        }
        ImGui::SetCursorPos(ImVec2(bx + bw * 2.0f, 0.0f));
        if (windowButton("##close", BTN_CLOSE, barH, false)) glfwSetWindowShouldClose(win, 1);

        ImGui::EndMainMenuBar();
    }
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();  // FramePadding

    barHOut = barH;
}

}  // namespace ui
