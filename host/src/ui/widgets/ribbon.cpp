// AI-CAD ribbon widget — C14.
// SolidWorks-style: tab strip on top, grouped big/small buttons, group separators,
// dropdown-split arrows, disabled state, horizontal overflow scroll.
// All colors from ui::palette (theme-role only — no hardcoded ImVec4/ImU32 colors).
#include "ribbon.h"

#include "imgui.h"
#include "imgui_internal.h"

#include "../icons.h"
#include "../../ui_theme.h"   // ui::palette, ui::Palette
#include "../ribbon_def.h"    // RibbonCmd, getRibbonCmds

#include <cstdio>
#include <cstring>
#include <functional>
#include <string>
#include <vector>

// ---------------------------------------------------------------------------
// Module-level state (not in a namespace block to avoid MSVC local-struct issues)
// ---------------------------------------------------------------------------

static std::function<void(const char*)> s_RibbonCb;

static const char* kRibbonTabs[] = {"Features", "Sketch", "Evaluate", "Simulation", "Assembly"};
static constexpr int kRibbonTabCount = 5;  // C20b: + Assembly tab (appended to keep indices stable)

// Ribbon geometry constants
static constexpr float kTabH        = 24.0f;
static constexpr float kBigIconSz   = 32.0f;
static constexpr float kSmIconSz    = 20.0f;
static constexpr float kBigBtnW     = 52.0f;
static constexpr float kGroupPadX   = 6.0f;
static constexpr float kBodyH       = 62.0f;
static constexpr float kSmRowH      = 22.0f;
static constexpr float kArrowW      = 12.0f;
static constexpr float kDropArrowW  = 14.0f;

// Group descriptor built each frame for the active tab
struct RibbonGroup {
    const char*                        label;
    std::vector<const ui::RibbonCmd*>  bigCmds;
    std::vector<const ui::RibbonCmd*>  smCmds;
    std::vector<const ui::RibbonCmd*>  subCmds;  // dropdown children
};

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

static void ribbon_fire(const char* cmd_id) {
    if (s_RibbonCb) s_RibbonCb(cmd_id);
    else { std::printf("[ribbon] %s\n", cmd_id); std::fflush(stdout); }
}

// Locate or create a group by label in the list.
static RibbonGroup& ribbon_findOrAddGroup(std::vector<RibbonGroup>& groups,
                                          const char* gLabel) {
    for (auto& g : groups)
        if (std::strcmp(g.label, gLabel) == 0) return g;
    RibbonGroup ng;
    ng.label = gLabel;
    groups.push_back(ng);
    return groups.back();
}

// Does this big command have sub-commands in the group?
static bool ribbon_hasSubs(const RibbonGroup& grp, const ui::RibbonCmd* cmd) {
    for (const auto* sc : grp.subCmds)
        if (std::strcmp(sc->sub_of, cmd->cmd_id) == 0) return true;
    return false;
}

// Draw a SW big button (32px icon over label). Returns true if main area clicked.
// hasDrop = true: right third is an arrow zone that opens a popup on click.
static bool ribbon_bigBtn(const char* cmd_id, const char* iconName,
                           const char* label, bool enabled, bool hasDrop) {
    float s   = ImGui::GetIO().FontGlobalScale;
    float bw  = kBigBtnW * s + (hasDrop ? kDropArrowW * s : 0.0f);
    float bh  = kBodyH * s;

    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::PushID(cmd_id);

    bool clicked     = false;
    bool dropClicked = false;

    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(0, 0));
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));

    float mainW = hasDrop ? (bw - kArrowW * s) : bw;
    clicked = ImGui::InvisibleButton("##main", ImVec2(mainW, bh));
    bool mainHov = ImGui::IsItemHovered();

    if (hasDrop) {
        ImGui::SameLine(0, 0);
        dropClicked = ImGui::InvisibleButton("##drop", ImVec2(kArrowW * s, bh));
        bool dropHov = ImGui::IsItemHovered();

        // Separator line
        ImDrawList* dl = ImGui::GetWindowDrawList();
        if ((mainHov || dropHov) && enabled) {
            ImU32 hBg = ui::Palette::toU32(ui::palette.buttonHovered);
            dl->AddRectFilled(p0, ImVec2(p0.x + bw, p0.y + bh), hBg, 3.0f);
        }
        {
            float lx = p0.x + mainW;
            dl->AddLine(ImVec2(lx, p0.y + 4), ImVec2(lx, p0.y + bh - 4),
                        ui::Palette::toU32(ui::palette.border), 1.0f);
        }
        // Arrow glyph
        {
            float ax = p0.x + mainW + kArrowW * s * 0.5f;
            float ay = p0.y + bh - 10.0f * s;
            ImU32 aCol = enabled ? ui::Palette::toU32(ui::palette.text)
                                 : ui::Palette::toU32(ui::palette.textDisabled);
            float hs = 3.5f * s;
            dl->AddTriangleFilled(ImVec2(ax - hs, ay - hs * 0.5f),
                                  ImVec2(ax + hs, ay - hs * 0.5f),
                                  ImVec2(ax,      ay + hs * 0.5f), aCol);
        }

        if (dropClicked && enabled) {
            std::string popId = std::string(cmd_id) + "_drop";
            ImGui::OpenPopup(popId.c_str());
        }
    } else {
        if (mainHov && enabled) {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            ImU32 hBg = ui::Palette::toU32(ui::palette.buttonHovered);
            dl->AddRectFilled(p0, ImVec2(p0.x + bw, p0.y + bh), hBg, 3.0f);
        }
    }

    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(2);

    // Draw icon + label over the invisible button area
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        float iconSz   = kBigIconSz * s;
        float cx       = p0.x + mainW * 0.5f;
        float iy       = p0.y + 4.0f * s;

        ui::IconUV ic = ui::getIcon(iconName, 32);
        ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
        dl->AddImage(ic.texId,
                     ImVec2(cx - iconSz * 0.5f, iy),
                     ImVec2(cx + iconSz * 0.5f, iy + iconSz),
                     ic.uv0, ic.uv1, tint);

        // Label centered below icon
        ImVec2 ts  = ImGui::CalcTextSize(label);
        float  tx  = cx - ts.x * 0.5f;
        float  ty  = iy + iconSz + 3.0f * s;
        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                             : ui::Palette::toU32(ui::palette.textDisabled);
        dl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, label);
    }

    if (clicked && enabled) ribbon_fire(cmd_id);

    ImGui::PopID();
    return clicked && enabled;
}

// Draw a small button (20px icon + label on same row). Returns true if clicked.
static bool ribbon_smBtn(const char* cmd_id, const char* iconName,
                          const char* label, bool enabled) {
    float s      = ImGui::GetIO().FontGlobalScale;
    float rowH   = kSmRowH * s;
    float iconSz = kSmIconSz * s;
    float totalW = iconSz + ImGui::CalcTextSize(label).x + 10.0f * s;

    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::PushID(cmd_id);

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_Button,        ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive,  ImVec4(0,0,0,0));
    bool clicked = ImGui::InvisibleButton("##sm", ImVec2(totalW, rowH));
    bool hov     = ImGui::IsItemHovered();
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();

    if (hov && enabled) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->AddRectFilled(p0, ImVec2(p0.x + totalW, p0.y + rowH),
                          ui::Palette::toU32(ui::palette.buttonHovered), 3.0f);
    }

    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        float iy  = p0.y + (rowH - iconSz) * 0.5f;
        ui::IconUV ic = ui::getIcon(iconName, 20);
        ImU32 tint = enabled ? IM_COL32(255,255,255,255) : IM_COL32(128,128,128,160);
        dl->AddImage(ic.texId, ImVec2(p0.x, iy), ImVec2(p0.x + iconSz, iy + iconSz),
                     ic.uv0, ic.uv1, tint);
        float tx  = p0.x + iconSz + 4.0f * s;
        float ty  = p0.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f;
        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                             : ui::Palette::toU32(ui::palette.textDisabled);
        dl->AddText(nullptr, 0.0f, ImVec2(tx, ty), tCol, label);
    }

    if (clicked && enabled) ribbon_fire(cmd_id);
    ImGui::PopID();
    return clicked && enabled;
}

// ---------------------------------------------------------------------------
// Public API (wrapped in namespace ui)
// ---------------------------------------------------------------------------

namespace ui {

void setRibbonCallback(std::function<void(const char* cmd_id)> cb) {
    s_RibbonCb = std::move(cb);
}

float ribbonHeight() {
    float s = ImGui::GetIO().FontGlobalScale;
    return (kTabH + kBodyH + 8.0f) * s;
}

float drawRibbon(int& activeTab, bool visible) {
    if (!visible) return 0.0f;

    float s      = ImGui::GetIO().FontGlobalScale;
    float tabH   = kTabH  * s;
    float bodyH2 = kBodyH * s;
    float totalH = tabH + bodyH2 + 8.0f * s;

    ImGuiViewport* vp = ImGui::GetMainViewport();
    float titleBarH   = ImGui::GetFrameHeight();

    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x, vp->Pos.y + titleBarH));
    ImGui::SetNextWindowSize(ImVec2(vp->Size.x, totalH));
    ImGui::SetNextWindowViewport(vp->ID);

    ImGuiWindowFlags flags =
        ImGuiWindowFlags_NoTitleBar        | ImGuiWindowFlags_NoCollapse |
        ImGuiWindowFlags_NoResize          | ImGuiWindowFlags_NoMove     |
        ImGuiWindowFlags_NoBringToFrontOnFocus | ImGuiWindowFlags_NoNavFocus |
        ImGuiWindowFlags_NoDocking         | ImGuiWindowFlags_NoScrollbar |
        ImGuiWindowFlags_NoScrollWithMouse;

    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding,   0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowBorderSize, 0.0f);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,    ImVec2(0, 0));
    ImGui::PushStyleColor(ImGuiCol_WindowBg,
        ImVec4(palette.ribbonBg[0], palette.ribbonBg[1],
               palette.ribbonBg[2], palette.ribbonBg[3]));

    ImGui::Begin("##RibbonBar", nullptr, flags);
    ImGui::PopStyleVar(3);
    ImGui::PopStyleColor();

    ImDrawList* dl   = ImGui::GetWindowDrawList();
    ImVec2      wPos = ImGui::GetWindowPos();

    // --- Tab strip ---
    float tabX = wPos.x + 4.0f * s;
    float tabY = wPos.y;

    for (int i = 0; i < kRibbonTabCount; ++i) {
        const char* tabLabel = kRibbonTabs[i];
        ImVec2 tsize  = ImGui::CalcTextSize(tabLabel);
        float  tw     = tsize.x + 16.0f * s;
        bool   isAct  = (activeTab == i);

        ImVec2 tp0(tabX, tabY);
        ImVec2 tp1(tabX + tw, tabY + tabH);

        ImGui::SetCursorScreenPos(tp0);
        ImGui::PushID(i + 1000);
        if (ImGui::InvisibleButton(tabLabel, ImVec2(tw, tabH))) activeTab = i;
        bool tabHov = ImGui::IsItemHovered();
        ImGui::PopID();

        if (isAct || tabHov) {
            ImU32 bg = isAct ? Palette::toU32(palette.ribbonTabActive)
                             : Palette::toU32(palette.buttonHovered);
            dl->AddRectFilled(tp0, tp1, bg, 3.0f,
                isAct ? ImDrawFlags_RoundCornersTop : 0);
        }
        if (isAct) {
            dl->AddLine(ImVec2(tp0.x + 2, tp1.y - 1),
                        ImVec2(tp1.x - 2, tp1.y - 1),
                        Palette::toU32(palette.accent), 2.0f);
        }
        ImVec4 lc = (isAct || tabHov)
            ? ImVec4(palette.text[0],    palette.text[1],    palette.text[2],    palette.text[3])
            : ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], palette.textDim[3]);
        dl->AddText(nullptr, 0.0f,
                    ImVec2(tp0.x + (tw - tsize.x) * 0.5f,
                           tp0.y + (tabH - tsize.y) * 0.5f),
                    ImGui::ColorConvertFloat4ToU32(lc), tabLabel);

        tabX += tw + 2.0f * s;
    }

    // Tab strip bottom border
    dl->AddLine(ImVec2(wPos.x,              wPos.y + tabH),
                ImVec2(wPos.x + vp->Size.x, wPos.y + tabH),
                Palette::toU32(palette.border), 1.0f);

    // --- Build group list for the active tab ---
    int cmdCount = 0;
    const RibbonCmd* cmds = getRibbonCmds(cmdCount);
    const char* actTabName = kRibbonTabs[activeTab];

    std::vector<RibbonGroup> groups;
    groups.reserve(16);

    for (int i = 0; i < cmdCount; ++i) {
        const RibbonCmd& c = cmds[i];
        if (std::strcmp(c.tab, actTabName) != 0) continue;
        RibbonGroup& grp = ribbon_findOrAddGroup(groups, c.group);
        if (c.sub_of)  grp.subCmds.push_back(&c);
        else if (c.big) grp.bigCmds.push_back(&c);
        else            grp.smCmds.push_back(&c);
    }

    // --- Body child window (horizontal scroll for overflow) ---
    ImGui::SetCursorScreenPos(ImVec2(wPos.x + 4.0f * s, wPos.y + tabH + 2.0f * s));
    ImGui::BeginChild("##RibbonBody",
                      ImVec2(vp->Size.x - 8.0f * s, bodyH2 + 4.0f * s),
                      false,
                      ImGuiWindowFlags_HorizontalScrollbar |
                      ImGuiWindowFlags_NoScrollWithMouse);

    float curX = 0.0f;

    for (int gi = 0; gi < (int)groups.size(); ++gi) {
        const RibbonGroup& grp = groups[gi];

        // Measure big column width
        float bigColW = 0.0f;
        for (const auto* cmd : grp.bigCmds) {
            bool drop = ribbon_hasSubs(grp, cmd);
            bigColW += kBigBtnW * s + (drop ? kDropArrowW * s : 0.0f) + 2.0f * s;
        }

        // Measure small column width
        float smColW = 0.0f;
        for (const auto* cmd : grp.smCmds) {
            float rw = kSmIconSz * s + ImGui::CalcTextSize(cmd->label).x + 12.0f * s;
            if (rw > smColW) smColW = rw;
        }

        float grpW = kGroupPadX * s + bigColW
                     + (smColW > 0 ? smColW + kGroupPadX * s : 0.0f)
                     + kGroupPadX * s;

        // --- Big buttons ---
        float bx = curX + kGroupPadX * s;
        for (const auto* cmd : grp.bigCmds) {
            bool drop = ribbon_hasSubs(grp, cmd);
            ImGui::SetCursorPos(ImVec2(bx, 0.0f));
            ribbon_bigBtn(cmd->cmd_id, cmd->icon, cmd->label, !cmd->stretch, drop);

            // Dropdown popup for sub-commands
            if (drop) {
                std::string popId = std::string(cmd->cmd_id) + "_drop";
                if (ImGui::BeginPopup(popId.c_str())) {
                    for (const auto* sc : grp.subCmds) {
                        if (std::strcmp(sc->sub_of, cmd->cmd_id) != 0) continue;
                        IconUV ic = getIcon(sc->icon, 20);
                        float  iz = 20.0f * s;
                        ImGui::Image(ic.texId, ImVec2(iz, iz), ic.uv0, ic.uv1);
                        ImGui::SameLine();
                        ImGuiSelectableFlags sf =
                            sc->stretch ? ImGuiSelectableFlags_Disabled : 0;
                        if (ImGui::Selectable(sc->label, false, sf))
                            ribbon_fire(sc->cmd_id);
                    }
                    ImGui::EndPopup();
                }
            }

            float cmdW = kBigBtnW * s + (drop ? kDropArrowW * s : 0.0f) + 2.0f * s;
            bx += cmdW;
        }

        // --- Small buttons (stacked vertically) ---
        if (!grp.smCmds.empty()) {
            float sx = bx;
            float sy = 0.0f;
            for (const auto* cmd : grp.smCmds) {
                ImGui::SetCursorPos(ImVec2(sx, sy));
                ribbon_smBtn(cmd->cmd_id, cmd->icon, cmd->label, !cmd->stretch);
                sy += kSmRowH * s;
                if (sy + kSmRowH * s > bodyH2) { sy = 0.0f; sx += smColW; }
            }
        }

        // --- Group separator + label ---
        {
            ImVec2 cwinPos = ImGui::GetWindowPos();
            float  sepX    = cwinPos.x + curX + grpW - kGroupPadX * s * 0.5f;
            ImU32  sepCol  = Palette::toU32(palette.border);
            dl->AddLine(ImVec2(sepX, cwinPos.y + 4),
                        ImVec2(sepX, cwinPos.y + bodyH2 - 4), sepCol, 1.0f);

            ImVec2 gts = ImGui::CalcTextSize(grp.label);
            float  glx = cwinPos.x + curX + (grpW - gts.x) * 0.5f;
            float  gly = cwinPos.y + bodyH2 - gts.y - 2.0f * s;
            dl->AddText(nullptr, 0.0f, ImVec2(glx, gly),
                        Palette::toU32(palette.textDim), grp.label);
        }

        curX += grpW;
    }

    ImGui::EndChild();
    ImGui::End();  // ##RibbonBar

    return totalH;
}

}  // namespace ui
