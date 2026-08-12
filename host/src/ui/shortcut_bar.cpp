// Chudware 'S' shortcut bar (C25).
#include "shortcut_bar.h"

#include "imgui.h"
#include "icons.h"

#include "../core/app_state.h"
#include "../ui_theme.h"

namespace ui {

// ---- command tables -----------------------------------------------------------
struct SCmd { const char* id; const char* icon; const char* tip; };

// Model mode (Features tab commands).
static const SCmd kModelCmds[] = {
    { "sketch_new",   "sketch",       "New Sketch"  },
    { "extrude_boss", "extrude_boss", "Extrude"     },
    { "extrude_cut",  "extrude_cut",  "Extrude Cut" },
    { "revolve_boss", "revolve_boss", "Revolve"     },
    { "fillet",       "fillet",       "Fillet"      },
    { "chamfer",      "chamfer",      "Chamfer"     },
};

// Sketch mode commands.
static const SCmd kSketchCmds[] = {
    { "sk_line",        "line",        "Line"         },
    { "sk_circle",      "circle",      "Circle"       },
    { "sk_rect_corner", "rect_corner", "Rectangle"    },
    { "sk_arc_3pt",     "arc_3pt",     "3-Pt Arc"     },
    { "smart_dim",      "smart_dim",   "Smart Dim."   },
    { "sketch_exit",    "ok_check",    "Exit Sketch"  },
};

static constexpr int kModelCount  = (int)(sizeof(kModelCmds)  / sizeof(kModelCmds[0]));
static constexpr int kSketchCount = (int)(sizeof(kSketchCmds) / sizeof(kSketchCmds[0]));

// ---- geometry (logical px at 1x) ----------------------------------------------
// SINGLE source for the bar's metrics: update() previously placed/clamped the bar with a
// 30px-button/4px-pad footprint while draw() painted a 36/6 one, so the pill could hang past
// the viewport edge and sat off-center under the mouse.
static constexpr float kSBarBtn = 36.0f;   // square button hit size
static constexpr float kSBarPad = 6.0f;    // pill inset + inter-button gap

static void shortcutBarSize(int nCmds, float& barW, float& barH) {
    const float s = ui::uiScale;
    const float btnSz = kSBarBtn * s, pad = kSBarPad * s;
    barW = (float)nCmds * btnSz + (float)(nCmds - 1) * pad + pad * 2.0f;
    barH = btnSz + pad * 2.0f;
}

// ---- update ------------------------------------------------------------------
void ShortcutBar::update(AppState& st, bool imgHovered, bool inSketch,
                         ImVec2 vr0, ImVec2 vr1) {
    ImGuiIO& io = ImGui::GetIO();

    // Dismiss on Esc or when the viewport loses hover (click-outside handled in draw).
    if (m_visible) {
        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) { dismiss(); return; }
    }

    // Toggle on 'S' — only when viewport hovered and no text widget owns input.
    if (imgHovered && !io.WantTextInput && ImGui::IsKeyPressed(ImGuiKey_S)) {
        if (m_visible) {
            dismiss();
            return;
        }
        m_visible = true;

        const float s     = ui::uiScale;
        const int   nCmds = inSketch ? kSketchCount : kModelCount;
        float barW, barH;
        shortcutBarSize(nCmds, barW, barH);

        // Place top-left at mouse; clamp so bar stays inside the viewport.
        float px = io.MousePos.x - barW * 0.5f;
        float py = io.MousePos.y - barH - 4.0f * s;
        if (px < vr0.x)          px = vr0.x;
        if (px + barW > vr1.x)   px = vr1.x - barW;
        if (py < vr0.y)          py = vr0.y;
        if (py + barH > vr1.y)   py = vr1.y - barH;
        m_pos = ImVec2(px, py);
    }
}

// ---- draw --------------------------------------------------------------------
void ShortcutBar::draw(AppState& st, bool inSketch) {
    if (!m_visible) return;

    const SCmd* cmds  = inSketch ? kSketchCmds : kModelCmds;
    const int   nCmds = inSketch ? kSketchCount : kModelCount;
    const float s     = ui::uiScale;
    const auto& pal   = ui::palette;

    const float btnSz = kSBarBtn * s;
    const float pad   = kSBarPad * s;
    float barW, barH;
    shortcutBarSize(nCmds, barW, barH);

    ImVec2 p0 = m_pos;
    ImVec2 p1 = ImVec2(p0.x + barW, p0.y + barH);

    ImDrawList* dl = ImGui::GetForegroundDrawList();

    // Glass v2 pill over the live viewport (HUD group — per-group user overrides apply): the
    // legacy overlayBg/overlayBorder pair ignored the backdrop, washing out over dark parts.
    ImRect barRect(p0, p1);
    const ui::AdaptiveSurface& surf =
        ui::adaptivePanel(ui::panelId("host_shortcut_bar"), barRect, ui::GlassGroup::Hud);
    ui::glassPanel(dl, barRect, ui::GlassLevel::Regular, (kSBarBtn + kSBarPad * 2.0f) * 0.45f,
                   surf);

    // Buttons (InvisibleButton over draw-list art, same pattern as HUD toolbar).
    ImGui::PushID("##sbar");
    for (int i = 0; i < nCmds; ++i) {
        float bx = p0.x + pad + (float)i * (btnSz + pad);
        float by = p0.y + pad;
        ImGui::SetCursorScreenPos(ImVec2(bx, by));
        ImGui::PushID(i);
        bool clicked = ImGui::InvisibleButton("##sb", ImVec2(btnSz, btnSz));
        bool hov     = ImGui::IsItemHovered();
        if (hov) {
            // Accent wash (not buttonHovered — near-invisible over the frosted pill).
            dl->AddRectFilled(ImVec2(bx, by), ImVec2(bx + btnSz, by + btnSz),
                              ui::Palette::toU32(pal.accentSoft), 4.0f * s);
            ImGui::SetTooltip("%s", cmds[i].tip);
        }
        // SDF glyphs pair with the adaptive surface ink + contrast halo; multi-color raster
        // icons keep their pre-colored pixels (white tint = identity, no halo).
        const float iz = 22.0f * s;
        const ImVec2 ic(bx + btnSz * 0.5f, by + btnSz * 0.5f);
        if (ui::hasIconSdf(cmds[i].icon))
            ui::drawIconGlass(dl, cmds[i].icon, ic, iz,
                              ImGui::ColorConvertFloat4ToU32(surf.textPrimary),
                              ui::glassIconHalo(surf));
        else
            ui::drawIconGlass(dl, cmds[i].icon, ic, iz,
                              IM_COL32(255, 255, 255, 230), 0);  // gui-lint: allow identity tint at 90% — raster glyph keeps its own pixels
        if (clicked) {
            if (st.ribbonDispatch) st.ribbonDispatch(cmds[i].id);
            dismiss();
        }
        ImGui::PopID();
    }
    ImGui::PopID();

    // [workspace-tabs seam] Workspace-switch hint under the pill: the title-bar tabs are also
    // reachable via Ctrl+1..Ctrl+8 (key routing in app.cpp); surface that here since the shortcut
    // bar is exactly where a keyboard-first user looks. Small frosted chip, full mapping on hover.
    {
        const char* hint = "Ctrl+1\xe2\x80\xa6" "8  workspaces";   // "Ctrl+1…8  workspaces"
        ImVec2 hsz = ImGui::CalcTextSize(hint);
        const float hpx = 8.0f * s, hpy = 3.0f * s;
        float hx = p0.x + (barW - hsz.x) * 0.5f;
        float hy = p1.y + 4.0f * s;
        ImRect hintRect(ImVec2(hx - hpx, hy - hpy), ImVec2(hx + hsz.x + hpx, hy + hsz.y + hpy));
        const ui::AdaptiveSurface& hs =
            ui::adaptivePanel(ui::panelId("host_shortcut_bar_hint"), hintRect, ui::GlassGroup::Hud);
        ui::glassPanel(dl, hintRect, ui::GlassLevel::Regular,
                       (hsz.y + hpy * 2.0f) * 0.5f / s, hs);
        ImVec4 hc = hs.textSecondary;
        dl->AddText(ImVec2(hx, hy), ImGui::ColorConvertFloat4ToU32(hc), hint);
        ImVec2 mp = ImGui::GetIO().MousePos;
        if (mp.x >= hintRect.Min.x && mp.x <= hintRect.Max.x &&
            mp.y >= hintRect.Min.y && mp.y <= hintRect.Max.y)
            ImGui::SetTooltip("Switch workspace:\n"
                              "  Ctrl+1  Model\n"
                              "  Ctrl+2  Drawing\n"
                              "  Ctrl+3  Simulate\n"
                              "  Ctrl+4  Electronics\n"
                              "  Ctrl+5  Manufacture\n"
                              "  Ctrl+6  PLM\n"
                              "  Ctrl+7  Parts Library\n"
                              "  Ctrl+8  AI");
    }

    // Click-outside detection: if the mouse button was clicked while not hovering the bar or its
    // hint chip, dismiss (the hint chip sits just below the pill).
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        ImVec2 mp = ImGui::GetIO().MousePos;
        if (mp.x < p0.x || mp.x > p1.x || mp.y < p0.y || mp.y > p1.y + 30.0f * s) {
            dismiss();
        }
    }
}

}  // namespace ui
