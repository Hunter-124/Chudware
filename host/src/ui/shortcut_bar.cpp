// AI-CAD 'S' shortcut bar (C25).
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
        const float btnSz = 30.0f * s;
        const float pad   = 4.0f  * s;
        float barW = nCmds * btnSz + (nCmds - 1) * pad + pad * 2.0f;
        float barH = btnSz + pad * 2.0f;

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

    const float btnSz = 36.0f * s;
    const float pad   = 6.0f  * s;
    float barW = (float)nCmds * btnSz + ((float)(nCmds - 1)) * pad + pad * 2.0f;
    float barH = btnSz + pad * 2.0f;

    ImVec2 p0 = m_pos;
    ImVec2 p1 = ImVec2(p0.x + barW, p0.y + barH);

    ImDrawList* dl = ImGui::GetForegroundDrawList();

    // Pill background + border.
    float rounding = barH * 0.45f;
    dl->AddRectFilled(p0, p1, ui::Palette::toU32(pal.overlayBg), rounding);
    dl->AddRect(p0, p1, ui::Palette::toU32(pal.overlayBorder), rounding, 0, 1.5f);

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
            dl->AddRectFilled(ImVec2(bx, by), ImVec2(bx + btnSz, by + btnSz),
                              ui::Palette::toU32(pal.buttonHovered), 4.0f * s);
            ImGui::SetTooltip("%s", cmds[i].tip);
        }
        IconUV iv = ui::getIcon(cmds[i].icon, 24);
        float iz  = 22.0f * s;
        float off = (btnSz - iz) * 0.5f;
        dl->AddImage(iv.texId,
                     ImVec2(bx + off, by + off),
                     ImVec2(bx + off + iz, by + off + iz),
                     iv.uv0, iv.uv1,
                     IM_COL32(255, 255, 255, 230));
        if (clicked) {
            if (st.ribbonDispatch) st.ribbonDispatch(cmds[i].id);
            dismiss();
        }
        ImGui::PopID();
    }
    ImGui::PopID();

    // Click-outside detection: if the mouse button was clicked while not hovering the bar, dismiss.
    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        ImVec2 mp = ImGui::GetIO().MousePos;
        if (mp.x < p0.x || mp.x > p1.x || mp.y < p0.y || mp.y > p1.y) {
            dismiss();
        }
    }
}

}  // namespace ui
