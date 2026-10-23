// Chudware title bar — left-group chips (File/Search/Settings) + centered workspace tabs +
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
#ifdef __EMSCRIPTEN__
#include <emscripten.h>
#endif
#include "imgui.h"
#include "imgui_internal.h"

#include "title_bar.h"

#include "../core/app_state.h"
#include "../renderer.h"
#include "../ui_theme.h"   // ui::uiScale, ui::palette
#include "icons.h"             // ui::getIcon (raster workspace-tab tiles)
#include "metrics.h"           // ui::metrics::stroke / titleFramePad tokens
#include "doc_tabs.h"          // ui::openNewDocChooser (File > New)
#include "file_dialog.h"       // ui::openFolderDialog (File > Open Project Folder)
#include "firmware/fw_workspace.h"  // ui::firmwareTabVisible
#include "feature_tree.h"      // ui::featureTreeActivePath (Save vs. Save As gating)
#include "property_manager.h"  // PropertyManager::active() (used in View sub-menu)
#include "plm/plm_hub.h"       // ui::plmProjectName (active-project status control)
#include "tesseract.h"         // ui::drawTesseract (vector brand mark in the title bar)
#include "widgets/kit.h"       // ui::kit::elide (active-project label capping)

#include <filesystem>      // Recent-files menu labels
#include <cstdio>          // snprintf
#include <cstring>        // std::strcmp (workspace raster-tile gate)

#ifndef __EMSCRIPTEN__
// The Windows caption convention: the close button's hover fill is the platform red #E81123. It is
// an OS affordance, not a theme role — it must read as "close" identically in dark and light.
static const ImVec4 kCaptionCloseRed(232.0f / 255.0f, 17.0f / 255.0f, 35.0f / 255.0f, 1.0f);

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
        // min/max hover uses the palette role so the wash reads on the light theme too (a white
        // wash on the near-white light headerBg was invisible).
        ImU32 bg = kind == BTN_CLOSE ? ImGui::ColorConvertFloat4ToU32(kCaptionCloseRed)
                                     : ui::Palette::toU32(ui::palette.buttonHovered);
        dl->AddRectFilled(p0, ImVec2(p0.x + size.x, p0.y + size.y), bg);
    }
    // Caption glyph color from palette so it adapts to light/dark themes; over the close red the
    // ink is whatever contrasts with that fixed fill.
    const unsigned char fgv = (unsigned char)(ui::palette.captionFg * 255.0f + 0.5f);
    const ImU32 fg = (hovered && kind == BTN_CLOSE)
                         ? ImGui::ColorConvertFloat4ToU32(ui::maxContrastTextOn(kCaptionCloseRed))
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
#endif

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
    File, Search, Settings, Web,
    Model, Drawing, Simulate, Electronics, Manufacture, PLM, PartsLibrary, AI,
    Firmware,   // firmware workspace tab (progressive disclosure)
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
    case TbIcon::Web: {
        // Globe: circle + equator + one meridian (an ellipse squashed to a line).
        const float gr = r * 0.90f;
        dl->AddCircle(c, gr, col, 0, th);
        dl->AddLine(ImVec2(c.x - gr, c.y), ImVec2(c.x + gr, c.y), col, th);
        dl->AddEllipse(c, ImVec2(gr * 0.42f, gr), col, 0.0f, 0, th);
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
    case TbIcon::Firmware: {
        // MCU chip with a waveform through it (firmware = code on silicon).
        const float b = r * 0.55f;
        dl->AddRect(ImVec2(c.x - b, c.y - b), ImVec2(c.x + b, c.y + b), col, r * 0.12f, 0, th);
        const float o = r * 0.30f, pe = r * 0.95f;
        for (int sgn = -1; sgn <= 1; sgn += 2) {
            const float d = (float)sgn * o;
            dl->AddLine(ImVec2(c.x - b, c.y + d), ImVec2(c.x - pe, c.y + d), col, th);
            dl->AddLine(ImVec2(c.x + b, c.y + d), ImVec2(c.x + pe, c.y + d), col, th);
        }
        // Step waveform inside the body (GPIO trace motif).
        const float wy0 = c.y + b * 0.45f, wy1 = c.y - b * 0.45f;
        dl->PathLineTo(ImVec2(c.x - b * 0.70f, wy0));
        dl->PathLineTo(ImVec2(c.x - b * 0.25f, wy0));
        dl->PathLineTo(ImVec2(c.x - b * 0.25f, wy1));
        dl->PathLineTo(ImVec2(c.x + b * 0.25f, wy1));
        dl->PathLineTo(ImVec2(c.x + b * 0.25f, wy0));
        dl->PathLineTo(ImVec2(c.x + b * 0.70f, wy0));
        dl->PathStroke(col, 0, th);
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
                              TbIcon icon, float iconSz, float iconGap,
                              const char* rasterCell = nullptr) {
    ImGui::SetCursorScreenPos(p0);
    bool clicked = ImGui::InvisibleButton(btnId, ImVec2(chipW, chipH));
    bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + chipW, p0.y + chipH);

    // ONE continuous liquid-glass pill background across the whole chip: frosted base +
    // ui::coloredRowWash (single-mesh L→R gradient, painted once, no corner overlap).
    // Active glow is applied once below (glassRowBackground(selected=false) so we own the glow).
    // All three glass helpers take a LOGICAL dp radius and scale it internally; `rnd` arrives in
    // screen px, so convert ONCE here — passing px to glassRowBackground/glassSelectionGlow
    // double-scaled the corner radius at uiScale != 1.
    const float sChip = ui::uiScale;
    const float rndDp = rnd / sChip;
    float tint[3] = { ui::palette.accent[0], ui::palette.accent[1], ui::palette.accent[2] };
    ui::glassRowBackground(dl, p0, p1, rndDp, /*stripe*/false, /*selected*/false, /*washAlpha*/0.42f);
    {
        const float strength = isActive ? 0.40f : (hovered ? 0.30f : 0.18f);
        ui::coloredRowWash(dl, p0, p1, rndDp, tint, strength, /*selected*/false);
    }
    if (isActive)
        ui::glassSelectionGlow(dl, p0, p1, rndDp, ui::palette.accent);

    // Icon + label group centered in chip. Measure text ALWAYS with boldFont (when present)
    // so chipMeasureW and the layout share one width — previously active pills measured with
    // bold but inactive layout used the regular font, which shifted icon↔label spacing.
    // Draw weight still tracks active (bold) vs rest (regular) for the visual cue.
    const bool hasLabel = (label && label[0]);
    ImFont* measureFont = boldFont ? boldFont : ImGui::GetFont();
    ImFont* drawFont    = (isActive && boldFont) ? boldFont : ImGui::GetFont();
    const float textW = hasLabel
        ? measureFont->CalcTextSizeA(measureFont->FontSize, FLT_MAX, 0, label).x
        : 0.0f;

    const ImVec4 textColV(ui::palette.text[0], ui::palette.text[1],
                          ui::palette.text[2], ui::palette.text[3]);
    const ImU32 textU32 = ImGui::ColorConvertFloat4ToU32(textColV);
    const float contentW = hasLabel ? (iconSz + iconGap + textW) : iconSz;
    const float gx = p0.x + (chipW - contentW) * 0.5f;  // group left edge
    const ImVec2 iconCenter(gx + iconSz * 0.5f, p0.y + chipH * 0.5f);
    // Workspace chips are always drawn by the crisp procedural TbIcon path below. The atlas
    // workspace tiles are richer 32 px imagery and are intentionally not sampled at this size.
    // Do not probe the atlas here: a missing optional raster tile must not emit a runtime warning.
    (void)rasterCell;
    drawTbIcon(dl, icon, iconCenter, iconSz, textU32);
    if (hasLabel) {
        const float tx = gx + iconSz + iconGap;
        const float ty = p0.y + (chipH - drawFont->FontSize) * 0.5f;
        dl->AddText(drawFont, drawFont->FontSize, ImVec2(tx, ty), textU32, label);
    }

    return {clicked, hovered};
}

// ---- Active-project status control (title bar) --------------------------
// Distinct from the modeling-tab chips on purpose: this selects a project folder on desktop
// or reveals the workspace file browser on web, rather than a modeling page.
// It reads as a compact recessed "field" — tight corners, a leading status dot, an elided label
// and a trailing chevron that signals "click opens a switcher" — where the tabs
// are borderless pills that only wash on hover/active.
//
// COMPACTNESS IS A CONSTRAINT HERE, NOT A STYLE PREFERENCE. The centered tab strip
// pays for every pixel this control takes: once the strip no longer fits it
// collapses to icon-only. Web uses a simple "Workspace" label; native folder names
// are capped and elided, with the full name in the tooltip.
//
// "Project Folder" affordance (File > Open Project Folder..., and the title-bar
// active-project chip below) — same trigger, platform-appropriate target:
//   - Native: an OS folder picker choosing the CAD workspace dir.
//   - Web: the server hosts a single workspace, so this reveals the left pane's
//     Explorer tab, which browses that workspace's files.
static void openProjectFolderAction(AppState& st) {
#ifdef __EMSCRIPTEN__
    st.showFeatureTree = true;
    st.leftPaneTab = AppState::LP_Explorer;
#else
    std::string abs = ui::openFolderDialog();
    if (!abs.empty()) setActiveProjectDir(st, abs);
#endif
}

static ChipResult drawActiveProjectControl(AppState& st, ImVec2 p0, float h, ImFont* boldFont,
                                            float& outW) {
    const float S = ui::uiScale;
    const std::string project = ui::plmProjectName(st);
    const bool hasProject = !project.empty();
#ifdef __EMSCRIPTEN__
    const char* label = "Workspace";
#else
    const char* label = hasProject ? project.c_str() : "Open project\xe2\x80\xa6";
#endif

    ImFont* font = boldFont ? boldFont : ImGui::GetFont();
    const float padX    = 8.0f * S;
    const float dotSz   = ImFloor(5.0f * S);
    const float dotGap  = 7.0f * S;
    const float iconSz  = ImFloor(h * 0.40f);
    const float iconGap = 5.0f * S;
    const float chevGap = 7.0f * S;
    const float chevW   = ImFloor(h * 0.22f);

    // Hard width cap on the label: see the header comment — the workspace tab strip is
    // what pays for an unbounded project name. The tooltip carries the full string.
    ImGui::PushFont(font);
    const std::string shown = ui::kit::elide(label, 128.0f * S);
    const float textW = ImGui::CalcTextSize(shown.c_str()).x;
    ImGui::PopFont();

    const float w = padX * 2.0f + dotSz + dotGap + iconSz + iconGap + textW + chevGap + chevW;
    outW = w;

    ImGui::SetCursorScreenPos(p0);
    const bool clicked = ImGui::InvisibleButton("##tb_active_project", ImVec2(w, h));
    const bool hovered = ImGui::IsItemHovered();
    const bool held    = ImGui::IsItemActive();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 p1(p0.x + w, p0.y + h);
    const float rnd   = ImFloor(h * 0.24f);  // tighter than the tab pills -> reads as a field
    const float rndDp = rnd / S;             // glass helpers take logical dp and scale internally

    const float* A = ui::palette.accent;
    auto accentU32 = [&](const float c[4], float a) {
        return ImGui::GetColorU32(ImVec4(c[0], c[1], c[2], a));
    };

    // One soft accent halo, on interaction only. The previous always-on triple bloom made
    // a persistent, rarely-clicked control the loudest object in the title bar.
    if (hovered || held) {
        for (int i = 2; i >= 1; --i) {
            const float g = (float)i * S;
            dl->AddRect(ImVec2(p0.x - g, p0.y - g), ImVec2(p1.x + g, p1.y + g),
                        accentU32(A, 0.075f / (float)i), rnd + g, 0, ui::metrics::stroke(1.4f));
        }
    }

    // Shared glass vocabulary — the same frosted base + single-mesh accent wash the
    // title-bar chips use, so this control sits in one material family with its neighbours.
    ui::glassRowBackground(dl, p0, p1, rndDp, /*stripe*/false, /*selected*/false,
                           /*washAlpha*/0.42f);
    {
        float tint[3] = { A[0], A[1], A[2] };
        const float strength = held ? 0.40f : (hovered ? 0.32f : 0.22f);
        ui::coloredRowWash(dl, p0, p1, rndDp, tint, strength, /*selected*/false);
    }
    dl->AddRect(p0, p1, accentU32(A, hovered ? 0.88f : 0.52f), rnd, 0,
                ui::metrics::stroke(hovered ? 1.5f : 1.1f));

    const ImU32 textU32 = ui::Palette::toU32(ui::palette.text);
    const ImU32 dimU32  = ui::Palette::toU32(ui::palette.textDim);

    // Leading status dot: live indicator (ok = a project is open, dim = none). Replaces the
    // old full-height spine, which spent width the tab strip needed.
    float x = p0.x + padX;
    {
        const float* sc = hasProject ? ui::palette.statusOk : ui::palette.textDim;
        const ImVec2 c(x + dotSz * 0.5f, p0.y + h * 0.5f);
        dl->AddCircleFilled(c, dotSz * 0.5f, accentU32(sc, hasProject ? 0.95f : 0.50f), 0);
        if (hasProject)
            dl->AddCircle(c, dotSz * 0.95f, accentU32(sc, 0.26f), 0, ui::metrics::stroke(1.0f));
    }
    x += dotSz + dotGap;

    ui::drawIconGlass(dl, "body_folder", ImVec2(x + iconSz * 0.5f, p0.y + h * 0.5f), iconSz,
                      hasProject ? textU32 : dimU32, 0);
    x += iconSz + iconGap;

    dl->AddText(font, font->FontSize,
                ImVec2(x, ImFloor(p0.y + (h - font->FontSize) * 0.5f)),
                hasProject ? textU32 : dimU32, shown.c_str());
    x += textW + chevGap;
    {
        // Small chevron glyph — a switcher affordance, not a nav arrow.
        const float cy = p0.y + h * 0.5f;
        const float chh = chevW * 0.55f;
        const float th = ui::metrics::stroke(1.4f);
        dl->AddLine(ImVec2(x, cy - chh * 0.5f),
                    ImVec2(x + chevW * 0.5f, cy + chh * 0.5f), dimU32, th);
        dl->AddLine(ImVec2(x + chevW * 0.5f, cy + chh * 0.5f),
                    ImVec2(x + chevW, cy - chh * 0.5f), dimU32, th);
    }

    // Discoverability: a compact slab with no visible verb, and the label may be elided —
    // so the tooltip always names the destination in full plus the action.
    if (hovered) {
#ifdef __EMSCRIPTEN__
        ImGui::SetTooltip("Browse workspace files");
#else
        if (hasProject)
            ImGui::SetTooltip("Active project folder\n%s\n\nClick to change it", project.c_str());
        else
            ImGui::SetTooltip("No project folder is open\n\nClick to choose one");
#endif
    }
    return {clicked, hovered};
}

// Measure chip width (same formula as drawChipAt so sizes are consistent). An empty
// label measures as an icon-only chip (no label gap) — see drawChipAt's hasLabel path.
static float chipMeasureW(const char* label, float padX, ImFont* boldFont,
                          float iconSz, float iconGap) {
    if (!label || !label[0]) return iconSz + padX * 2.0f;
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
        if (ImGui::MenuItem("Open Project Folder...")) {
            openProjectFolderAction(st);
        }
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
            if (ImGui::MenuItem("ACIS SAT (.sat)...")) st.requestExportFormat = "sat";
            ImGui::Separator();
            if (ImGui::MenuItem("STL (.stl)..."))   openMeshExport("stl");
            if (ImGui::MenuItem("OBJ (.obj)..."))   openMeshExport("obj");
            if (ImGui::MenuItem("PLY (.ply)..."))   openMeshExport("ply");
            if (ImGui::MenuItem("3MF (.3mf)..."))   openMeshExport("3mf");
            if (ImGui::MenuItem("glTF (.gltf)...")) openMeshExport("gltf");
            if (ImGui::MenuItem("GLB (.glb)..."))   openMeshExport("glb");
            ImGui::Separator();
            if (ImGui::MenuItem("DXF (.dxf)..."))   openMeshExport("dxf");
            if (ImGui::MenuItem("DWG (.dwg)..."))   openMeshExport("dwg");
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
            if (ImGui::MenuItem("ConfigurationManager", nullptr,
                                st.openSettings && st.openSettingsTab == 1)) {
                st.openSettings = true;
                st.openSettingsTab = 1;  // Settings → General → Configurations
            }
            leftPaneEntry("Explorer",           AppState::LP_Explorer);
            ImGui::MenuItem("Design Table",     nullptr, &st.showDesignTablePanel);
            ImGui::MenuItem("Cut List",         nullptr, &st.showCutListPanel);
            ImGui::MenuItem("Equations",        nullptr, &st.showEquations);
            ImGui::MenuItem("AI Panel",         nullptr, &st.showAIPanel);
            ImGui::MenuItem("Viewport",         nullptr, &st.showViewport);
            ImGui::Separator();
            bool isLight = (ui::activeTheme == ui::Theme::Light);
            if (ImGui::MenuItem("Light theme", nullptr, isLight)) {
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
            st.aiInteraction = (int)CopilotMode::Ask;
            st.settings.copilotMode = (int)CopilotMode::Ask;
            saveSettings(st.settings);
        }
        if (ImGui::MenuItem("About Chudware")) st.showAbout = true;
        ImGui::Separator();
        // Repair/install runtime
        if (ImGui::MenuItem("Repair / install runtime", nullptr, false,
                            !st.setupRunning.load() && !st.anyRunActive()))
            st.startSetup(true);
        ImGui::Separator();
        if (ImGui::MenuItem("Quit", "Alt+F4")) glfwSetWindowShouldClose(win, 1);
    }
};

void drawTitleBar(AppState& st, bool maximized, float& barHOut) {
    GLFWwindow* win = st.win;
    chrome::HitLayout& hitLayout = *st.hitLayout;

    float s = ui::uiScale;
#ifdef __EMSCRIPTEN__
    const bool mobile = ImGui::GetIO().DisplaySize.x <= 900.0f ||
                        EM_ASM_INT({ return window.matchMedia('(pointer: coarse)').matches; });
#else
    const bool mobile = false;
#endif

    const float framePadY = mobile ? std::max(ui::metrics::titleFramePadY(),
                                              (ui::metrics::px(48.0f) - ImGui::GetFontSize()) * 0.5f)
                                   : ui::metrics::titleFramePadY();
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding,
                        ImVec2(ui::metrics::titleFramePadX(), framePadY));
    const float barH = ImGui::GetFrameHeight();
    hitLayout.titleBarHeight = (int)barH;
    hitLayout.clearSpans();  // reset fine-grained spans each frame

    // Solid chrome band, subtly following the Global glass tint.
    ImGui::PushStyleColor(ImGuiCol_MenuBarBg, titleBar_chromeTint(ui::palette.headerBg));
    if (ImGui::BeginMainMenuBar()) {
        const float winX = ImGui::GetWindowPos().x;  // window origin in screen space
        const float winW = ImGui::GetWindowWidth();
#ifdef __EMSCRIPTEN__
        // Browser chrome owns its window controls. Recover the full app-bar width and
        // publish no native caption hit span.
        const float rightGroupStartRelX = winW - 4.0f * s;
        hitLayout.controlsLeft = 0;
        hitLayout.controlsRight = 0;
        (void)maximized;
#else
        const float bw   = barH * (1.35f + 0.15f * (s - 1.0f));  // one caption button width
        const float captionZoneW = bw * 3.0f;  // min | max/restore | close

        // Right boundary of the usable zone (= where caption buttons begin), window-relative.
        const float rightGroupStartRelX = winW - captionZoneW - 4.0f * s;
#endif

        // ---- Brand lockup (left edge): vector mark + typed wordmark -----------------
        // This used to blit assets/logo_wordmark.png scaled to ~14 px tall. A 687x200 bitmap
        // minified that far is unrecoverable — the type broke into blocks and the mark turned
        // into a green smudge. The brand is drawn instead: the live tesseract (pure ImDrawList
        // geometry, crisp at every DPI, the same mark the copilot header paints) plus the
        // wordmark set in the app's own medium font. Nothing here samples a texture, so it is
        // resolution-independent. The About dialog still shows the PNG, at a size it suits.
        float logoEndRelX = 12.0f * s;  // window-relative right edge of the lockup
        {
            const float markSz = ImFloor(barH * 0.58f);
            const float markGap = 7.0f * s;
            ImFont* wordFont = st.fonts.medium ? st.fonts.medium : ImGui::GetFont();
            ImGui::PushFont(wordFont);
            const float wordW = ImGui::CalcTextSize("Chudware").x;
            const float wordH = ImGui::GetTextLineHeight();
            ImGui::PopFont();
            const float lockupW = markSz + markGap + wordW;
            if (!mobile && 12.0f * s + lockupW + 18.0f * s < rightGroupStartRelX - 240.0f * s) {
                ImDrawList* dl = ImGui::GetWindowDrawList();
                const ImVec2 markMin(winX + 12.0f * s,
                                     ImGui::GetWindowPos().y + ImFloor((barH - markSz) * 0.5f));
                ui::drawTesseract(dl, ImVec2(markMin.x + markSz * 0.5f, markMin.y + markSz * 0.5f),
                                  markSz, ImGui::GetTime(),
                                  ui::tesseractAnimFromInt(st.settings.brandAnim),
                                  ui::Palette::toU32(ui::palette.accent));
                ImGui::PushFont(wordFont);
                dl->AddText(ImVec2(markMin.x + markSz + markGap,
                                   ImGui::GetWindowPos().y + ImFloor((barH - wordH) * 0.5f)),
                            ui::Palette::toU32(ui::palette.text), "Chudware");
                ImGui::PopFont();
                logoEndRelX = 12.0f * s + lockupW;
            }
        }

        // ---- LEFT GROUP: File | Search | Settings — ALL placed via explicit screen coords -----
        // This avoids BeginMenu's auto-advance fighting SameLine which caused the overlap bug.
        const bool hasDoc = (st.activeTab >= 0);
        const float chipVPad  = 4.0f * s;      // vertical inset from bar top/bottom
        const float chipRound = barH * 0.28f;  // moderately rounded corners
        const float chipH     = barH - chipVPad * 2.0f;
        // Workspace tabs ride a slightly shorter pill than the left-group chips: their
        // hover/active wash spans the whole strip, and at full chip height it read as a
        // solid band rather than a set of pills. The extra inset is applied symmetrically
        // (tabVPad top AND bottom) so the smaller hit/highlight region stays centered in
        // the bar. Left-group chips (File/Search/Settings/Web) and the active-project
        // field keep chipVPad/chipH.
        const float tabVPad = chipVPad + 3.0f * s;
        const float tabH    = barH - tabVPad * 2.0f;
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
        const float webW      = chipMeasureW("Web",      padX, boldFont, iconSz, iconGap);

        // Start x: just after logo (screen-relative → then convert to screen-absolute for SetCursorScreenPos).
        // logoEndRelX is already window-relative cursor position; convert to screen.
        const float leftGroupStartScreenX = winX + (mobile ? ui::metrics::px(6.0f)
                                                                          : logoEndRelX + 14.0f * s);
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
        if (mobile) {
            const float moreW = chipMeasureW("More", padX, boldFont, iconSz, iconGap);
            auto res = drawChipAt("##tb_more", "More", ImVec2(cx, barTopScreenY + chipVPad),
                                  moreW, chipH, false, chipRound, boldFont,
                                  TbIcon::Settings, iconSz, iconGap);
            if (res.clicked) ImGui::OpenPopup("##toolbar_more");
            ImGui::SetNextWindowPos(ImVec2(cx, barTopScreenY + barH), ImGuiCond_Always);
            if (ImGui::BeginPopup("##toolbar_more")) {
                if (ImGui::MenuItem("Search commands")) st.showCommandSearch = true;
                if (ImGui::MenuItem("Settings")) st.openSettings = true;
                if (ImGui::MenuItem("Web", nullptr, st.showWeb)) st.showWeb = !st.showWeb;
                if (ImGui::MenuItem("Project folder")) openProjectFolderAction(st);
                ImGui::EndPopup();
            }
            cx += moreW + chipGap;
        } else {

        // Liquid-glass divider in the gap between File and Search.
        {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const float dividerX = cx - chipGap * 0.5f;
            const float dy0 = barTopScreenY + barH * 0.24f;
            const float dy1 = barTopScreenY + barH * 0.76f;
            ImU32 dHi  = (ui::Palette::toU32(ui::palette.accent) & 0x00FFFFFFu) | 0x22000000u;
            ImU32 dCol = (ui::Palette::toU32(ui::palette.border) & 0x00FFFFFFu) | 0x66000000u;
            dl->AddLine(ImVec2(dividerX - 0.5f * s, dy0), ImVec2(dividerX - 0.5f * s, dy1), dHi,  1.0f * s);
            dl->AddLine(ImVec2(dividerX + 0.5f * s, dy0), ImVec2(dividerX + 0.5f * s, dy1), dCol, 1.0f * s);
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

        // Liquid-glass divider in the gap between Search and Settings.
        {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const float dividerX = cx - chipGap * 0.5f;
            const float dy0 = barTopScreenY + barH * 0.24f;
            const float dy1 = barTopScreenY + barH * 0.76f;
            ImU32 dHi  = (ui::Palette::toU32(ui::palette.accent) & 0x00FFFFFFu) | 0x22000000u;
            ImU32 dCol = (ui::Palette::toU32(ui::palette.border) & 0x00FFFFFFu) | 0x66000000u;
            dl->AddLine(ImVec2(dividerX - 0.5f * s, dy0), ImVec2(dividerX - 0.5f * s, dy1), dHi,  1.0f * s);
            dl->AddLine(ImVec2(dividerX + 0.5f * s, dy0), ImVec2(dividerX + 0.5f * s, dy1), dCol, 1.0f * s);
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

        // Liquid-glass divider in the gap between Settings and Web.
        {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const float dividerX = cx - chipGap * 0.5f;
            const float dy0 = barTopScreenY + barH * 0.24f;
            const float dy1 = barTopScreenY + barH * 0.76f;
            ImU32 dHi  = (ui::Palette::toU32(ui::palette.accent) & 0x00FFFFFFu) | 0x22000000u;
            ImU32 dCol = (ui::Palette::toU32(ui::palette.border) & 0x00FFFFFFu) | 0x66000000u;
            dl->AddLine(ImVec2(dividerX - 0.5f * s, dy0), ImVec2(dividerX - 0.5f * s, dy1), dHi,  1.0f * s);
            dl->AddLine(ImVec2(dividerX + 0.5f * s, dy0), ImVec2(dividerX + 0.5f * s, dy1), dCol, 1.0f * s);
        }

        // -- Web chip -- toggles the full-window in-app CEF browser overlay (st.showWeb).
        // Lit while browsing so the user can see they're in the web view. Clicking it again,
        // a workspace tab, or Esc returns to modeling with the page kept alive underneath.
        {
            const ImVec2 chipP0(cx, barTopScreenY + chipVPad);
            auto res = drawChipAt("##tb_web", "Web", chipP0, webW, chipH,
                                  st.showWeb, chipRound, boldFont,
                                  TbIcon::Web, iconSz, iconGap);
            hitLayout.addSpan((int)(cx - winX), (int)(cx - winX + webW));
            if (res.clicked) st.showWeb = !st.showWeb;
            cx += webW + chipGap;
        }
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

        if (!mobile) {
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

        cx += 14.0f * s;  // clear the separator just drawn

        // ---- Active-project status control — deliberately NOT another chip in the tab
        // group; see drawActiveProjectControl for why (persistent state vs. page switch). ----
        {
            const ImVec2 ctrlP0(cx, barTopScreenY + chipVPad);
            float ctrlW = 0.0f;
            auto res = drawActiveProjectControl(st, ctrlP0, chipH, boldFont, ctrlW);
            hitLayout.addSpan((int)(cx - winX), (int)(cx - winX + ctrlW));
            if (res.clicked) {
                openProjectFolderAction(st);
            }
            cx += ctrlW + 10.0f * s;
        }

        // Second separator between the active-project control and the workspace tabs.
        {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const float sepScreenX = cx + 6.0f * s;
            const float sepY0 = barTopScreenY + barH * 0.20f;
            const float sepY1 = barTopScreenY + barH * 0.80f;
            ImU32 sepCol = ui::Palette::toU32(ui::palette.border);
            sepCol = (sepCol & 0x00FFFFFF) | 0x60000000u;
            dl->AddLine(ImVec2(sepScreenX, sepY0), ImVec2(sepScreenX, sepY1), sepCol, 1.5f * s);
        }
        }

        // Left group ends here (screen-absolute); convert to window-relative for centering math.
        const float leftGroupEndRelX = (cx + 14.0f * s) - winX;  // +separator width

        // ---- CENTER GROUP: workspace tabs, centered between left group and caption buttons ----
        // rasterCell: an engine-rendered atlas tile blitted instead of the procedural
        // glyph (exemplar wave — Model + Manufacture only; nullptr = keep the glyph).
        struct WsTab { const char* label; AppState::Workspace ws; TbIcon icon;
                       const char* rasterCell; };
        static const WsTab kTabs[] = {
            {"Model",        AppState::Workspace::Model,        TbIcon::Model,        "ws_model"},
            {"Drawing",      AppState::Workspace::Drawing,      TbIcon::Drawing,      "ws_drawing"},
            {"Simulate",     AppState::Workspace::Simulate,     TbIcon::Simulate,     "ws_simulate"},
            {"Electronics",  AppState::Workspace::Electronics,  TbIcon::Electronics,  "ws_electronics"},
            {"Manufacture",  AppState::Workspace::Manufacture,  TbIcon::Manufacture,  "ws_manufacture"},
            {"PLM",          AppState::Workspace::PLM,          TbIcon::PLM,          nullptr},
            {"Parts Library",AppState::Workspace::PartsLibrary, TbIcon::PartsLibrary, "ws_parts"},
            {"AI",           AppState::Workspace::AI,           TbIcon::AI,           nullptr},
            {"Reverse Eng",  AppState::Workspace::ReverseEng,   TbIcon::Search,       nullptr},
            {"Firmware",     AppState::Workspace::Firmware,     TbIcon::Firmware,     nullptr},
        };
        constexpr int kTabCount = (int)(sizeof(kTabs) / sizeof(kTabs[0]));

        const float tabRound = tabH * 0.38f;
        const float tabGapX  = 3.0f * s;    // gap between tabs
        const float tabPadX  = 10.0f * s;   // horizontal text padding inside each tab

        // Measure the full labelled strip. A workspace tab the user cannot see is a
        // workspace they cannot REACH (the tab strip is the only mouse route to it), so
        // the strip must never silently drop one: when labels don't fit, every chip
        // except the active one collapses to icon-only and gains a name tooltip.
        const bool fwTabVisible = ui::firmwareTabVisible(st);   // progressive disclosure
        auto tabVisible = [&](int i) {
            return kTabs[i].ws != AppState::Workspace::Firmware || fwTabVisible;
        };
        // The active tab keeps its label in icon-only mode so "where am I" stays readable.
        auto tabLabel = [&](int i, bool iconOnly) -> const char* {
            return (iconOnly && st.activeWorkspace != kTabs[i].ws) ? "" : kTabs[i].label;
        };
        auto measureStrip = [&](bool iconOnly) {
            float w = 0.0f;
            for (int i = 0; i < kTabCount; ++i) {
                if (!tabVisible(i)) continue;
                if (w > 0.0f) w += tabGapX;
                w += chipMeasureW(tabLabel(i, iconOnly), tabPadX, boldFont, iconSz, iconGap);
            }
            return w;
        };

        // Available center zone, window-relative.
        const float centerZoneW = rightGroupStartRelX - leftGroupEndRelX;

        float totalTabsW = measureStrip(/*iconOnly=*/false);
        const bool iconOnly = (totalTabsW + 8.0f * s > centerZoneW);
        if (iconOnly) totalTabsW = measureStrip(/*iconOnly=*/true);
        if (mobile) {
            const char* workspaceLabel = "Workspace";
            for (const auto& tab : kTabs)
                if (tab.ws == st.activeWorkspace) workspaceLabel = tab.label;
            const float pickerX = cx + chipGap;
            const float pickerW = std::max(ui::metrics::px(1.0f),
                                           winX + rightGroupStartRelX - pickerX);
            const std::string pickerLabel = ui::kit::elide(workspaceLabel,
                std::max(1.0f, pickerW - padX * 2.0f - iconSz - iconGap));
            auto res = drawChipAt("##workspace_picker", pickerLabel.c_str(),
                                  ImVec2(pickerX, barTopScreenY + chipVPad),
                                  pickerW, chipH, true, chipRound, boldFont,
                                  TbIcon::Model, iconSz, iconGap);
            if (res.clicked) ImGui::OpenPopup("##workspace_menu");
            ImGui::SetNextWindowPos(ImVec2(pickerX, barTopScreenY + barH), ImGuiCond_Always);
            ImGui::SetNextWindowSizeConstraints(ImVec2(0, 0),
                ImVec2(ImGui::GetMainViewport()->Size.x - ui::metrics::px(12.0f),
                       std::max(ui::metrics::px(44.0f), ImGui::GetMainViewport()->Size.y - barH)));
            if (ImGui::BeginPopup("##workspace_menu")) {
                for (int i = 0; i < kTabCount; ++i) {
                    if (!tabVisible(i)) continue;
                    if (ImGui::MenuItem(kTabs[i].label, nullptr, st.activeWorkspace == kTabs[i].ws)) {
                        st.activeWorkspace = kTabs[i].ws;
                        st.showWeb = false;
                    }
                }
                ImGui::EndPopup();
            }
        } else {

        // Compute where tabs start, window-relative, then convert to screen for drawing.
        float tabsStartRelX;
        if (totalTabsW + 8.0f * s <= centerZoneW) {
            // Centered: equal margin on each side.
            tabsStartRelX = leftGroupEndRelX + (centerZoneW - totalTabsW) * 0.5f;
        } else {
            // Even icon-only overflows (extremely narrow window): start right after the
            // left group and let the clip below drop the tail.
            tabsStartRelX = leftGroupEndRelX + 4.0f * s;
        }

        ImDrawList* dl = ImGui::GetWindowDrawList();
        float tx = winX + tabsStartRelX;  // current tab x, screen-absolute
        bool drewPrevTab = false;         // gate the between-tab dividers

        for (int i = 0; i < kTabCount; ++i) {
            // Progressive disclosure: the Firmware tab is offered only when a
            // firmware project exists / ECAD is in play (Simulate-tab disclosure rules).
            if (!tabVisible(i))
                continue;
            const bool isActive = (st.activeWorkspace == kTabs[i].ws);
            const char* tabText = tabLabel(i, iconOnly);
            const float chipW = chipMeasureW(tabText, tabPadX, boldFont, iconSz, iconGap);

            // Clip the tab at the build-specific usable right boundary.
            if ((tx - winX) + chipW > rightGroupStartRelX - 2.0f * s) break;

            // Liquid-glass divider centered in the gap before this tab (only between two
            // visible tabs — never before the first drawn tab or after the last).
            if (drewPrevTab) {
                const float dividerX = tx - tabGapX * 0.5f;
                const float dy0 = barTopScreenY + barH * 0.24f;
                const float dy1 = barTopScreenY + barH * 0.76f;
                ImU32 dHi  = (ui::Palette::toU32(ui::palette.accent) & 0x00FFFFFFu) | 0x22000000u;
                ImU32 dCol = (ui::Palette::toU32(ui::palette.border) & 0x00FFFFFFu) | 0x66000000u;
                dl->AddLine(ImVec2(dividerX - 0.5f * s, dy0), ImVec2(dividerX - 0.5f * s, dy1), dHi,  1.0f * s);
                dl->AddLine(ImVec2(dividerX + 0.5f * s, dy0), ImVec2(dividerX + 0.5f * s, dy1), dCol, 1.0f * s);
            }

            const ImVec2 p0(tx, barTopScreenY + tabVPad);

            // drawChipAt places InvisibleButton + draws fill/label.
            char btnId[32];
            snprintf(btnId, sizeof(btnId), "##ws_%d", i);
            auto res = drawChipAt(btnId, tabText, p0, chipW, tabH,
                                  isActive, tabRound, boldFont,
                                  kTabs[i].icon, iconSz, iconGap, kTabs[i].rasterCell);
            // Icon-only chips must still announce which workspace they are.
            if (res.hovered && (!tabText || !tabText[0]))
                ImGui::SetTooltip("%s", kTabs[i].label);

            // Register window-relative span.
            hitLayout.addSpan((int)(tx - winX), (int)(tx - winX + chipW));

            if (res.clicked) { st.activeWorkspace = kTabs[i].ws; st.showWeb = false; }

            tx += chipW + tabGapX;
            drewPrevTab = true;
        }
        }

#ifndef __EMSCRIPTEN__
        // ---- Window caption buttons (right-aligned): min | max/restore | close ----
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
#endif

        ImGui::EndMainMenuBar();
    }
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();  // FramePadding

    barHOut = barH;
}

}  // namespace ui
