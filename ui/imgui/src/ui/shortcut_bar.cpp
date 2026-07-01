#include "ui/shortcut_bar.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"

namespace ui {

using app::AppState;

namespace {

struct SCmd { const char* id; const char* icon; const char* tip; };

static const SCmd kModelCmds[] = {
    { "sketch_new",   "sketch",       "New Sketch"  },
    { "extrude_boss", "extrude_boss", "Extrude"     },
    { "extrude_cut",  "extrude_cut",  "Extrude Cut" },
    { "revolve_boss", "revolve_boss", "Revolve"     },
    { "fillet",       "fillet",       "Fillet"      },
    { "chamfer",      "chamfer",      "Chamfer"     },
};

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

} // namespace

void ShortcutBar::update(AppState& st, bool imgHovered, bool inSketch,
                         ImVec2 vr0, ImVec2 vr1) {
    (void)st;
    ImGuiIO& io = ImGui::GetIO();

    if (m_visible && ImGui::IsKeyPressed(ImGuiKey_Escape)) {
        dismiss();
        return;
    }

    if (imgHovered && !io.WantTextInput && ImGui::IsKeyPressed(ImGuiKey_S)) {
        if (m_visible) {
            dismiss();
            return;
        }
        m_visible = true;

        const float S     = theme::S();
        const int   nCmds = inSketch ? kSketchCount : kModelCount;
        const float btnSz = theme::U(3.75f);
        const float pad   = theme::U(0.5f);
        float barW = (float)nCmds * btnSz + (float)(nCmds - 1) * pad + pad * 2.f;
        float barH = btnSz + pad * 2.f;

        float px = io.MousePos.x - barW * 0.5f;
        float py = io.MousePos.y - barH - theme::U(0.5f);
        if (px < vr0.x)        px = vr0.x;
        if (px + barW > vr1.x) px = vr1.x - barW;
        if (py < vr0.y)        py = vr0.y;
        if (py + barH > vr1.y) py = vr1.y - barH;
        m_pos = ImVec2(px, py);
    }
}

void ShortcutBar::draw(AppState& st, bool inSketch) {
    if (!m_visible) return;

    const SCmd* cmds  = inSketch ? kSketchCmds : kModelCmds;
    const int   nCmds = inSketch ? kSketchCount : kModelCount;
    const float S     = theme::S();

    const float btnSz = theme::U(4.5f);
    const float pad   = theme::U(0.75f);
    float barW = (float)nCmds * btnSz + (float)(nCmds - 1) * pad + pad * 2.f;
    float barH = btnSz + pad * 2.f;

    ImVec2 p0 = m_pos;
    ImVec2 p1 = ImVec2(p0.x + barW, p0.y + barH);

    ImDrawList* dl = ImGui::GetForegroundDrawList();
    float rounding = barH * 0.45f;
    theme::glassPanel(dl, ImRect(p0, p1), theme::MaterialLevel::Regular, rounding);

    ImGui::PushID("##sbar");
    for (int i = 0; i < nCmds; ++i) {
        float bx = p0.x + pad + (float)i * (btnSz + pad);
        float by = p0.y + pad;
        ImGui::SetCursorScreenPos(ImVec2(bx, by));
        ImGui::PushID(i);
        bool clicked = ImGui::InvisibleButton("##sb", ImVec2(btnSz, btnSz));
        bool hov     = ImGui::IsItemHovered();
        if (hov) {
            theme::glassRowBackground(dl, ImVec2(bx, by), ImVec2(bx + btnSz, by + btnSz),
                                      theme::ctx().m.rounding, false, false,
                                      theme::rowWash::hover);
            ImGui::SetTooltip("%s", cmds[i].tip);
        }
        float iz  = theme::U(2.75f);
        float off = (btnSz - iz) * 0.5f;
        icons::draw(dl, cmds[i].icon,
                    ImVec2(bx + off + iz * 0.5f, by + off + iz * 0.5f),
                    iz, theme::roleU32(theme::pal().txt0, 0.9f));
        if (clicked) {
            st.dispatch(cmds[i].id);
            dismiss();
        }
        ImGui::PopID();
    }
    ImGui::PopID();

    if (ImGui::IsMouseClicked(ImGuiMouseButton_Left)) {
        ImVec2 mp = ImGui::GetIO().MousePos;
        if (mp.x < p0.x || mp.x > p1.x || mp.y < p0.y || mp.y > p1.y)
            dismiss();
    }
}

} // namespace ui
