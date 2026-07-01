// Registry-driven ribbon body (ported from host/widgets/ribbon.cpp, restyled per GLASS_DESIGN_SPEC).
#include "ui/ribbon.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include "imgui_internal.h"

#include <cmath>
#include <cstdlib>
#include <cstring>
#include <string>
#include <unordered_map>
#include <vector>

namespace ui::ribbon {

using app::AppState;

namespace {

constexpr float kBigIconFrac  = 0.42f;
constexpr float kLblPadFrac   = 0.12f;
constexpr float kGroupPadFrac = 0.11f;
constexpr float kGapFrac      = 0.04f;
constexpr float kDropArrowFrac= 0.18f;

static std::unordered_map<std::string, std::string> s_lastUsed;
static std::string s_flyoutOpenReq;

struct ParentDefault { const char* parentId; const char* defaultCmd; const char* defaultLabel; };
static const ParentDefault kParentDefaults[] = {
    {"sk_ellipse_big", "sk_ellipse",  "Ellipse"},
    {"sk_polygon",     "sk_polygon",  "Polygon"},
    {"view_style",     "view_style_shaded", "Shaded"},
};

static const ParentDefault* parentDefault(const char* parentId) {
    for (const auto& pd : kParentDefaults)
        if (std::strcmp(pd.parentId, parentId) == 0) return &pd;
    return nullptr;
}

static void seedDefaults() {
    static bool seeded = false;
    if (seeded) return;
    seeded = true;
    for (const auto& pd : kParentDefaults)
        if (s_lastUsed.find(pd.parentId) == s_lastUsed.end())
            s_lastUsed[pd.parentId] = pd.defaultCmd;
}

static float wrapLabel(const char* label, std::string& l1, std::string& l2) {
    l1 = label ? label : "";
    l2.clear();
    if (!label) return 0.f;
    std::string s(label);
    size_t nl = s.find('\n');
    if (nl != std::string::npos) {
        l1 = s.substr(0, nl);
        l2 = s.substr(nl + 1);
    } else {
        size_t sp = s.rfind(' ');
        if (sp != std::string::npos && s.size() > 7) {
            l1 = s.substr(0, sp);
            l2 = s.substr(sp + 1);
        }
    }
    ImFont* f = theme::font(theme::Font::Body);
    float fs = theme::ctx().m.type.caption * theme::S();
    float w1 = f->CalcTextSizeA(fs, FLT_MAX, 0, l1.c_str()).x;
    float w2 = l2.empty() ? 0.f : f->CalcTextSizeA(fs, FLT_MAX, 0, l2.c_str()).x;
    return w1 > w2 ? w1 : w2;
}

static const char* displayLabel(const char* cmdId, const char* label) {
    if (cmdId && std::strcmp(cmdId, "sketch_new") == 0) return "";
    return label ? label : "";
}

static float bigBtnW(const char* cmdId, const char* label, bool hasDrop, float btnH, float S) {
    std::string l1, l2;
    const char* vis = displayLabel(cmdId, label);
    float lblW = wrapLabel(vis, l1, l2) + kLblPadFrac * btnH;
    float base = (vis[0] == '\0' ? btnH * 0.88f : btnH);
    if (lblW > base) base = lblW;
    float maxW = btnH * 1.85f;
    if (base > maxW) base = maxW;
    return base + (hasDrop ? btnH * kDropArrowFrac : 0.f);
}

static bool hasFlyouts(const char* id) {
    return !cmd::flyoutChildren(id).empty();
}

static bool isActive(AppState& s, const char* id) {
    return !s.activeTool.empty() && id && s.activeTool == id;
}

static void resolveFace(const char* parentId, const cmd::Command* cmd, bool drop,
                        const char*& outIcon, const char*& outLabel) {
    outIcon = cmd->icon;
    outLabel = cmd->label;
    if (!drop) return;
    auto it = s_lastUsed.find(parentId);
    if (it == s_lastUsed.end() || it->second.empty()) return;
    const cmd::Command* ch = cmd::find(it->second.c_str());
    if (ch) { outIcon = ch->icon; outLabel = ch->label; }
}

static void flyoutPopup(const char* parentId, AppState& s) {
    const float S = theme::S();
    std::string popId = std::string(parentId) + "_drop";

    if (s_flyoutOpenReq == parentId) {
        ImGui::OpenPopup(popId.c_str());
        s_flyoutOpenReq.clear();
    }
    if (!ImGui::BeginPopup(popId.c_str(), ImGuiWindowFlags_NoMove))
        return;

    theme::pushGlassTooltipStyle();
    ImDrawList* pdl = ImGui::GetWindowDrawList();
    ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
    theme::glassPanel(pdl, ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)),
                      theme::MaterialLevel::Regular, theme::ctx().m.roundingLg * S);

    float rowH = 28.f * S;
    float iconSz = 20.f * S;
    float padX = 8.f * S;
    float gapX = 6.f * S;

    std::vector<const cmd::Command*> rows;
    if (const ParentDefault* pd = parentDefault(parentId)) {
        if (const cmd::Command* dc = cmd::find(pd->defaultCmd))
            rows.push_back(dc);
    }
    for (const cmd::Command* ch : cmd::flyoutChildren(parentId))
        rows.push_back(ch);

    float maxLblW = 0.f;
    ImFont* f = theme::font(theme::Font::Body);
    float fs = theme::ctx().m.type.body * S;
    for (const cmd::Command* c : rows)
        maxLblW = ImMax(maxLblW, f->CalcTextSizeA(fs, FLT_MAX, 0, c->label).x);
    float popW = padX + iconSz + gapX + maxLblW + padX;

    for (const cmd::Command* c : rows) {
        ImVec2 cp0 = ImGui::GetCursorScreenPos();
        ImGui::PushID(c->id);
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0, 0));
        bool rowClicked = ImGui::InvisibleButton("##fr", ImVec2(popW, rowH));
        bool rowHov = ImGui::IsItemHovered();
        ImGui::PopStyleVar();

        ImDrawList* dl = ImGui::GetWindowDrawList();
        if (rowHov)
            theme::glassRowBackground(dl, cp0, ImVec2(cp0.x + popW, cp0.y + rowH),
                                      theme::ctx().m.rounding * S, false, false,
                                      theme::rowWash::hover);

        icons::draw(dl, c->icon, ImVec2(cp0.x + padX + iconSz * 0.5f, cp0.y + rowH * 0.5f),
                    iconSz, TCOL(txt0));
        dl->AddText(f, fs, ImVec2(cp0.x + padX + iconSz + gapX,
                                  cp0.y + (rowH - fs) * 0.5f), TCOL(txt0), c->label);

        if (rowClicked) {
            s_lastUsed[parentId] = c->id;
            s.dispatch(c->id);
            ImGui::CloseCurrentPopup();
        }
        ImGui::PopID();
    }

    ImGui::EndPopup();
    theme::popGlassTooltipStyle();
}

static bool bigBtn(AppState& s, const char* cmdId, const char* icon, const char* label,
                   float btnH, bool hasDrop) {
    const float S = theme::S();
    float bw = bigBtnW(cmdId, label, hasDrop, btnH, S);
    ImVec2 p0 = ImGui::GetCursorScreenPos();
    ImGui::PushID(cmdId);

    float mainW = hasDrop ? (bw - btnH * kDropArrowFrac) : bw;
    bool clicked = false;
    bool dropClicked = false;

    ImGui::SetNextItemAllowOverlap();
    clicked = ImGui::InvisibleButton("##main", ImVec2(mainW, btnH));
    bool mainHov = ImGui::IsItemHovered();
    bool mainTip = ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal);

    if (hasDrop) {
        ImGui::SameLine(0, 0);
        dropClicked = ImGui::InvisibleButton("##drop", ImVec2(btnH * kDropArrowFrac, btnH));
        bool dropHov = ImGui::IsItemHovered();
        if (dropClicked) s_flyoutOpenReq = cmdId;

        if (mainHov || dropHov) {
            theme::glassRowBackground(ImGui::GetWindowDrawList(), p0,
                                      ImVec2(p0.x + bw, p0.y + btnH),
                                      theme::ctx().m.rounding * S, false, false,
                                      theme::rowWash::hover);
        }
        ImDrawList* dl = ImGui::GetWindowDrawList();
        float ax = p0.x + mainW + btnH * kDropArrowFrac * 0.5f;
        float ay = p0.y + btnH - btnH * 0.14f;
        float hs = btnH * 0.05f;
        dl->AddTriangleFilled(ImVec2(ax - hs, ay - hs * 0.5f), ImVec2(ax + hs, ay - hs * 0.5f),
                              ImVec2(ax, ay + hs * 0.7f), TCOL(txt1));
        (void)dropHov;
    } else if (mainHov) {
        theme::glassRowBackground(ImGui::GetWindowDrawList(), p0,
                                  ImVec2(p0.x + bw, p0.y + btnH),
                                  theme::ctx().m.rounding * S, false, false,
                                  theme::rowWash::hover);
    }

    if (isActive(s, cmdId)) {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        dl->AddRect(p0, ImVec2(p0.x + bw, p0.y + btnH), TCOL(accent),
                    theme::ctx().m.rounding * S, 0, ImMax(1.f, 1.5f * S));
    }

    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const char* vis = displayLabel(cmdId, label);
        float iconSz = btnH * kBigIconFrac;
        float cx = p0.x + mainW * 0.5f;
        float iy = p0.y + (vis[0] == '\0' ? (btnH - iconSz) * 0.5f : btnH * 0.08f);
        icons::draw(dl, icon, ImVec2(cx, iy + iconSz * 0.5f), iconSz,
                    (mainHov || isActive(s, cmdId)) ? TCOL(txt0) : TCOL(txt1));

        if (vis[0] != '\0') {
            std::string l1, l2;
            wrapLabel(vis, l1, l2);
            ImFont* f = theme::font(theme::Font::Body);
            float fs = theme::ctx().m.type.caption * S;
            ImU32 tc = (mainHov || isActive(s, cmdId)) ? TCOL(txt0) : TCOL(txt1);
            float ty = iy + iconSz + btnH * 0.04f;
            ImVec2 t1s = f->CalcTextSizeA(fs, FLT_MAX, 0, l1.c_str());
            dl->AddText(f, fs, ImVec2(cx - t1s.x * 0.5f, ty), tc, l1.c_str());
            if (!l2.empty()) {
                ImVec2 t2s = f->CalcTextSizeA(fs, FLT_MAX, 0, l2.c_str());
                dl->AddText(f, fs, ImVec2(cx - t2s.x * 0.5f, ty + t1s.y + 1.f), tc, l2.c_str());
            }
        }
    }

    if (mainTip && !hasDrop) ImGui::SetTooltip("%s", label);

    if (clicked && !dropClicked) {
        if (hasDrop) {
            auto it = s_lastUsed.find(cmdId);
            if (it != s_lastUsed.end() && !it->second.empty())
                s.dispatch(it->second.c_str());
            else
                s.dispatch(cmdId);
        } else {
            s.dispatch(cmdId);
        }
    }

    ImGui::PopID();
    return clicked && !dropClicked;
}

struct RibbonGroup {
    cmd::Group group;
    std::vector<const cmd::Command*> cmds;
};

static std::vector<RibbonGroup> buildGroups(cmd::Workspace ws, AppState& s) {
    std::vector<RibbonGroup> groups;
    auto tops = cmd::topCommands(ws);
    for (cmd::Group g : cmd::groupsFor(ws)) {
        RibbonGroup rg;
        rg.group = g;
        for (const cmd::Command* c : tops) {
            if (c->group != g) continue;
            if (ws == cmd::Workspace::Sketch && !s.inSketch && std::strcmp(c->id, "sketch_exit") == 0) {
                if (const cmd::Command* sk = cmd::find("sketch_new"))
                    rg.cmds.push_back(sk);
                continue;
            }
            if (ws == cmd::Workspace::Sketch && s.inSketch && std::strcmp(c->id, "sketch_new") == 0)
                continue;
            rg.cmds.push_back(c);
        }
        if (!rg.cmds.empty()) groups.push_back(std::move(rg));
    }
    return groups;
}

static float groupNaturalW(const RibbonGroup& g, float btnH, float S) {
    float pad = btnH * kGroupPadFrac;
    float gap = btnH * kGapFrac;
    float w = pad;
    for (const cmd::Command* c : g.cmds) {
        bool drop = hasFlyouts(c->id);
        w += bigBtnW(c->id, c->label, drop, btnH, S) + gap;
    }
    w += pad;
    ImFont* f = theme::font(theme::Font::Body);
    float fs = theme::ctx().m.type.caption * S;
    float capW = f->CalcTextSizeA(fs, FLT_MAX, 0, cmd::groupLabel(g.group)).x + pad * 2.f;
    return ImMax(w, capW);
}

enum class Collapse { Visible, Hidden };

} // namespace

void applyShotEnv(AppState& s) {
    const char* tabEnv = std::getenv("AI_CAD_TAB");
    if (!tabEnv || !*tabEnv) return;
    int t = std::atoi(tabEnv);
    if (t < 0) return;

    s.inSketch = false;
    switch (t) {
    case 0:  s.workspace = cmd::Workspace::Model; break;
    case 1:  s.inSketch = true; s.workspace = cmd::Workspace::Sketch; break;
    case 2:  s.workspace = cmd::Workspace::Model; break;
    case 3:  s.workspace = cmd::Workspace::Simulate; break;
    case 4:  s.workspace = cmd::Workspace::Assemble; break;
    case 5:  s.workspace = cmd::Workspace::SheetMetal; break;
    case 6:  s.workspace = cmd::Workspace::Weldment; break;
    case 7:  s.workspace = cmd::Workspace::Schematic; break;
    case 8:  s.workspace = cmd::Workspace::Surface; break;
    case 9: case 10:
        s.workspace = cmd::Workspace::Drafting;
        if (!s.docs.empty()) s.doc().type = cmd::DocType::Drawing;
        break;
    case 11: s.workspace = cmd::Workspace::Model; break;
    case 12: s.workspace = cmd::Workspace::Model; break;
    default: break;
    }
    s.activeTool.clear();
}

void drawBody(AppState& s, float height) {
    seedDefaults();
    applyShotEnv(s);

    const float S = theme::S();
    const float capH = 14.f * S;
    const float pad = theme::ctx().m.space.sm * S;
    const float btnH = height - capH - 2.f * pad;
    const float gap = btnH * kGapFrac;

    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##toolbar", ImVec2(0, height), false, ImGuiWindowFlags_NoScrollbar);

    ImDrawList* wdl = ImGui::GetWindowDrawList();
    ImVec2 wp = ImGui::GetWindowPos();
    ImVec2 wsz = ImGui::GetWindowSize();
    theme::glassPanel(wdl, ImRect(wp, ImVec2(wp.x + wsz.x, wp.y + wsz.y)),
                      theme::MaterialLevel::Thin, theme::ctx().m.roundingLg * S);

    cmd::Workspace ws = s.effectiveWorkspace();
    auto groups = buildGroups(ws, s);

    float availW = wsz.x - pad * 2.f - 24.f * S;
    std::vector<float> natW(groups.size());
    for (size_t i = 0; i < groups.size(); ++i)
        natW[i] = groupNaturalW(groups[i], btnH, S);

    float totalW = 0.f;
    for (float w : natW) totalW += w;
    std::vector<Collapse> collapse(groups.size(), Collapse::Visible);
    if (totalW > availW) {
        for (int gi = (int)groups.size() - 1; gi >= 0 && totalW > availW; --gi) {
            totalW -= natW[gi];
            collapse[gi] = Collapse::Hidden;
            natW[gi] = 0.f;
        }
    }

    bool anyHidden = false;
    for (Collapse c : collapse) if (c == Collapse::Hidden) { anyHidden = true; break; }

    ImGui::SetCursorPos(ImVec2(pad, pad));
    bool firstGroup = true;

    for (size_t gi = 0; gi < groups.size(); ++gi) {
        if (collapse[gi] == Collapse::Hidden) continue;

        if (!firstGroup) {
            ImGui::SameLine(0, gap * 2.f);
            ImVec2 sp = ImGui::GetCursorScreenPos();
            wdl->AddLine(ImVec2(sp.x, sp.y + 2.f * S), ImVec2(sp.x, sp.y + btnH - 2.f * S),
                         TCOL(line), ImMax(1.f, S));
            ImGui::SameLine(0, gap * 2.f);
        }
        firstGroup = false;

        ImGui::BeginGroup();
        bool firstBtn = true;
        for (const cmd::Command* c : groups[gi].cmds) {
            if (!firstBtn) ImGui::SameLine(0, gap);
            firstBtn = false;
            bool drop = hasFlyouts(c->id);
            const char* showIcon = c->icon;
            const char* showLabel = c->label;
            resolveFace(c->id, c, drop, showIcon, showLabel);
            bigBtn(s, c->id, showIcon, showLabel, btnH, drop);
            if (drop) flyoutPopup(c->id, s);
        }
        ImGui::EndGroup();

        ImVec2 gmin = ImGui::GetItemRectMin(), gmax = ImGui::GetItemRectMax();
        const char* gl = cmd::groupLabel(groups[gi].group);
        ImFont* f = theme::font(theme::Font::Body);
        float fs = theme::ctx().m.type.caption * S;
        ImVec2 ts = f->CalcTextSizeA(fs, FLT_MAX, 0, gl);
        float cx = (gmin.x + gmax.x) * 0.5f - ts.x * 0.5f;
        wdl->AddText(f, fs, ImVec2(cx, gmin.y + btnH + pad * 0.35f), TCOL(txt2), gl);
    }

    if (anyHidden) {
        ImGui::SameLine(0, gap);
        ImGui::PushID("overflow");
        bool chev = ImGui::InvisibleButton("##ov", ImVec2(20.f * S, btnH));
        if (chev) ImGui::OpenPopup("##ribbon_overflow");
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("More commands");
        if (ImGui::BeginPopup("##ribbon_overflow")) {
            for (size_t gi = 0; gi < groups.size(); ++gi) {
                if (collapse[gi] != Collapse::Hidden) continue;
                ImGui::TextColored(TVEC(txt2), "%s", cmd::groupLabel(groups[gi].group));
                for (const cmd::Command* c : groups[gi].cmds)
                    if (ImGui::MenuItem(c->label)) s.dispatch(c->id);
                ImGui::Separator();
            }
            ImGui::EndPopup();
        }
        ImGui::PopID();
    }

    wdl->AddLine(ImVec2(wp.x, wp.y + wsz.y - 0.5f), ImVec2(wp.x + wsz.x, wp.y + wsz.y - 0.5f),
                 TCOL(line), 1.f);

    ImGui::EndChild();
    ImGui::PopStyleColor();
}

} // namespace ui::ribbon
