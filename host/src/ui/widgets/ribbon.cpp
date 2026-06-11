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
static constexpr float kTabH        = 28.0f;   // C24: taller tab strip, clear of the title bar
static constexpr float kBigIconSz   = 32.0f;
static constexpr float kSmIconSz    = 20.0f;
static constexpr float kBigBtnW     = 52.0f;   // minimum big-button width (label may widen it)
static constexpr float kBigBtnMaxW  = 96.0f;   // C24: cap so one long label can't bloat a group
static constexpr float kBigLblPad   = 6.0f;    // horizontal padding around the two-line label
static constexpr float kGroupPadX   = 9.0f;   // C24: more inter-group breathing room (no bleed)
static constexpr float kBodyH       = 76.0f;   // C24: taller body so 2-line labels + caption fit
static constexpr float kCaptionH    = 15.0f;   // C24: reserved strip at the bottom for group caption
static constexpr float kSmRowH      = 22.0f;
static constexpr float kArrowW      = 12.0f;
static constexpr float kDropArrowW  = 14.0f;

// C24: split a big-button label into up to two lines so it never overflows the button.
// Prefers an existing '\n' (later cards may add it), else breaks at the last space that keeps
// line 1 within budget; a single over-long word is left on one line (the width measure widens the
// button to fit it). Returns the wider of the two measured line widths (font px, unscaled).
static float ribbon_wrapLabel(const char* label, std::string& l1, std::string& l2) {
    l1 = label; l2.clear();
    std::string s(label);
    size_t nl = s.find('\n');
    if (nl != std::string::npos) {
        l1 = s.substr(0, nl);
        l2 = s.substr(nl + 1);
    } else {
        size_t sp = s.rfind(' ');
        // Only wrap when there's a space and the whole label is reasonably long.
        if (sp != std::string::npos && s.size() > 7) {
            l1 = s.substr(0, sp);
            l2 = s.substr(sp + 1);
        }
    }
    float w1 = ImGui::CalcTextSize(l1.c_str()).x;
    float w2 = l2.empty() ? 0.0f : ImGui::CalcTextSize(l2.c_str()).x;
    return w1 > w2 ? w1 : w2;
}

// C24: the rendered width of a big button = max(min width, label width + pad), clamped to a cap,
// plus the dropdown arrow column when present.
static float ribbon_bigBtnW(const char* label, bool hasDrop) {
    float s = ImGui::GetIO().FontGlobalScale;
    std::string l1, l2;
    float lblW = ribbon_wrapLabel(label, l1, l2) + kBigLblPad * 2.0f;
    float base = kBigBtnW;
    if (lblW > base) base = lblW;
    if (base > kBigBtnMaxW) base = kBigBtnMaxW;
    return base * s + (hasDrop ? kDropArrowW * s : 0.0f);
}

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
    float bw  = ribbon_bigBtnW(label, hasDrop);   // C24: width from label measure (no overlap)
    float bh  = (kBodyH - kCaptionH) * s;          // C24: leave the caption strip free at the bottom

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

        // C24: up-to-two-line label centered below the icon, clipped to the button so a stray
        // wide word is cropped rather than spilling into the next button.
        std::string l1, l2;
        ribbon_wrapLabel(label, l1, l2);
        ImU32 tCol = enabled ? ui::Palette::toU32(ui::palette.text)
                             : ui::Palette::toU32(ui::palette.textDisabled);
        float lineH = ImGui::GetTextLineHeight();
        float ty    = iy + iconSz + 2.0f * s;
        dl->PushClipRect(ImVec2(p0.x, p0.y), ImVec2(p0.x + mainW, p0.y + bh), true);
        {
            ImVec2 t1 = ImGui::CalcTextSize(l1.c_str());
            dl->AddText(nullptr, 0.0f, ImVec2(cx - t1.x * 0.5f, ty), tCol, l1.c_str());
            if (!l2.empty()) {
                ImVec2 t2 = ImGui::CalcTextSize(l2.c_str());
                dl->AddText(nullptr, 0.0f, ImVec2(cx - t2.x * 0.5f, ty + lineH), tCol, l2.c_str());
            }
        }
        dl->PopClipRect();
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
    // C24: small top inset + a little more horizontal breathing room per tab so the strip sits
    // clear of the title bar above and the tabs read like SolidWorks's command tabs.
    float tabTopPad = 3.0f * s;
    float tabBtnH   = tabH - tabTopPad;
    float tabX = wPos.x + 6.0f * s;
    float tabY = wPos.y + tabTopPad;

    for (int i = 0; i < kRibbonTabCount; ++i) {
        const char* tabLabel = kRibbonTabs[i];
        ImVec2 tsize  = ImGui::CalcTextSize(tabLabel);
        float  tw     = tsize.x + 22.0f * s;
        bool   isAct  = (activeTab == i);

        ImVec2 tp0(tabX, tabY);
        ImVec2 tp1(tabX + tw, tabY + tabBtnH);

        ImGui::SetCursorScreenPos(tp0);
        ImGui::PushID(i + 1000);
        if (ImGui::InvisibleButton(tabLabel, ImVec2(tw, tabBtnH))) activeTab = i;
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
                           tp0.y + (tabBtnH - tsize.y) * 0.5f),
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

        // Measure big column width (per-button width from its own label, so nothing overlaps)
        float bigColW = 0.0f;
        for (const auto* cmd : grp.bigCmds) {
            bool drop = ribbon_hasSubs(grp, cmd);
            bigColW += ribbon_bigBtnW(cmd->label, drop) + 2.0f * s;
        }

        // Measure small column width (icon + 4px gap + label + trailing pad; matches ribbon_smBtn
        // plus a little slack so the widest label never bleeds into the next group)
        float smColW = 0.0f;
        for (const auto* cmd : grp.smCmds) {
            float rw = kSmIconSz * s + 4.0f * s + ImGui::CalcTextSize(cmd->label).x + 14.0f * s;
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

            float cmdW = ribbon_bigBtnW(cmd->label, drop) + 2.0f * s;
            bx += cmdW;
        }

        // --- Small buttons (stacked vertically; wrap above the reserved caption strip) ---
        if (!grp.smCmds.empty()) {
            float smZoneH = bodyH2 - kCaptionH * s;
            float sx = bx;
            float sy = 0.0f;
            for (const auto* cmd : grp.smCmds) {
                ImGui::SetCursorPos(ImVec2(sx, sy));
                ribbon_smBtn(cmd->cmd_id, cmd->icon, cmd->label, !cmd->stretch);
                sy += kSmRowH * s;
                if (sy + kSmRowH * s > smZoneH) { sy = 0.0f; sx += smColW; }
            }
        }

        // --- Group separator + label ---
        {
            ImVec2 cwinPos = ImGui::GetWindowPos();
            float  sepX    = cwinPos.x + curX + grpW - kGroupPadX * s * 0.5f;
            ImU32  sepCol  = Palette::toU32(palette.border);
            dl->AddLine(ImVec2(sepX, cwinPos.y + 4),
                        ImVec2(sepX, cwinPos.y + bodyH2 - 4), sepCol, 1.0f);

            // C24: clip the group caption to the group's own width so adjacent captions never
            // collide and a long caption is cropped instead of bleeding past the separator.
            ImVec2 gts = ImGui::CalcTextSize(grp.label);
            float  capL = cwinPos.x + curX + kGroupPadX * s * 0.5f;
            float  capR = cwinPos.x + curX + grpW - kGroupPadX * s * 1.5f;
            float  glx = capL + ((capR - capL) - gts.x) * 0.5f;
            if (glx < capL) glx = capL;
            float  gly = cwinPos.y + bodyH2 - gts.y - 2.0f * s;
            dl->PushClipRect(ImVec2(capL, gly), ImVec2(capR, gly + gts.y), true);
            dl->AddText(nullptr, 0.0f, ImVec2(glx, gly),
                        Palette::toU32(palette.textDim), grp.label);
            dl->PopClipRect();
        }

        curX += grpW;
    }

    ImGui::EndChild();
    ImGui::End();  // ##RibbonBar

    return totalH;
}

}  // namespace ui
