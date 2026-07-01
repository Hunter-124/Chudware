#include "ui/panels.h"
#include "ui/command_registry.h"
#include "ui/command_search.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include <algorithm>
#include <cctype>
#include <cstring>
#include <cstdio>
#include <string>
#include <vector>

namespace ui {

using app::AppState;

namespace {

static bool icontains(const char* hay, const char* needle) {
    if (!needle || !needle[0]) return true;
    if (!hay) return false;
    const char* h = hay;
    while (*h) {
        const char* ph = h, *pn = needle;
        while (*ph && *pn &&
               std::tolower((unsigned char)*ph) == std::tolower((unsigned char)*pn)) {
            ++ph; ++pn;
        }
        if (!*pn) return true;
        ++h;
    }
    return false;
}

static std::string lower(const char* s) {
    std::string o;
    if (!s) return o;
    for (; *s; ++s) o += (char)std::tolower((unsigned char)*s);
    return o;
}

struct Hit {
    int score;
    enum Kind { Command, Settings } kind;
    const cmd::Command* cmd = nullptr;
    int settingsIdx = -1;
};

static bool matchesCommand(const cmd::Command* c, const std::string& q, int& score) {
    if (q.empty()) { score = 0; return true; }
    std::string lab = lower(c->label);
    std::string id  = lower(c->id);
    std::string ws  = lower(cmd::workspaceLabel(c->ws));
    std::string grp = lower(cmd::groupLabel(c->group));
    std::string kw  = lower(c->keywords);

    auto pos = lab.find(q);
    if (pos != std::string::npos) { score = 100 - (int)pos; return true; }
    if (id.find(q) != std::string::npos) { score = 60; return true; }
    if (ws.find(q) != std::string::npos) { score = 50; return true; }
    if (grp.find(q) != std::string::npos) { score = 45; return true; }
    if (!kw.empty() && kw.find(q) != std::string::npos) { score = 40; return true; }
    return false;
}

static bool matchesSettings(int idx, const std::string& q, int& score) {
    if (q.empty()) { score = 0; return true; }
    const SettingsEntry& e = kSettingsEntries[idx];
    std::string lab = lower(e.label);
    std::string tab = lower(e.tab);
    auto pos = lab.find(q);
    if (pos != std::string::npos) { score = 35 - (int)pos; return true; }
    if (tab.find(q) != std::string::npos) { score = 20; return true; }
    return icontains(e.label, q.c_str()) || icontains(e.tab, q.c_str());
}

} // namespace

void drawCommandPalette(AppState& s) {
    const float S = theme::S();
    ImGuiViewport* vp = ImGui::GetMainViewport();
    float w = 560.f * S, h = 460.f * S;
    ImGui::SetNextWindowPos(vp->GetCenter(), ImGuiCond_Always, ImVec2(0.5f, 0.5f));
    ImGui::SetNextWindowSize(ImVec2(w, h));

    theme::pushGlassWindowStyle();
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, theme::ctx().m.roundingLg * S);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(theme::U(1.5f), theme::U(1.5f)));
    ImGuiWindowFlags fl = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                          ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse |
                          ImGuiWindowFlags_NoDocking;
    ImGui::Begin("##palette", &s.paletteOpen, fl);

    icons::inlineIcon("search", theme::U(2.25f), TCOL(txt2));
    ImGui::SameLine(0, theme::U(1.f));
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::IsWindowAppearing()) {
        s.paletteQuery[0] = '\0';
        s.paletteSel = 0;
        ImGui::SetKeyboardFocusHere();
    }
    ImGui::PushStyleColor(ImGuiCol_FrameBg, ImVec4(0, 0, 0, 0));
    ImGui::InputTextWithHint("##q", "Search commands & settings...", s.paletteQuery,
                             sizeof(s.paletteQuery));
    ImGui::PopStyleColor();
    ImGui::Separator();

    std::string q = lower(s.paletteQuery);
    std::vector<Hit> hits;
    hits.reserve(64);

    for (const cmd::Command* c : cmd::palette()) {
        int sc = 0;
        if (matchesCommand(c, q, sc))
            hits.push_back({sc, Hit::Command, c, -1});
    }
    for (int i = 0; i < kSettingsEntryCount; ++i) {
        int sc = 0;
        if (matchesSettings(i, q, sc))
            hits.push_back({sc, Hit::Settings, nullptr, i});
    }

    std::stable_sort(hits.begin(), hits.end(),
                     [](const Hit& a, const Hit& b) { return a.score > b.score; });

    const int nHits = (int)hits.size();
    if (nHits == 0) s.paletteSel = 0;
    else {
        if (s.paletteSel < 0) s.paletteSel = 0;
        if (s.paletteSel >= nHits) s.paletteSel = nHits - 1;
        if (ImGui::IsKeyPressed(ImGuiKey_DownArrow)) s.paletteSel++;
        if (ImGui::IsKeyPressed(ImGuiKey_UpArrow))   s.paletteSel--;
        s.paletteSel = std::max(0, std::min(s.paletteSel, nHits - 1));
    }

    bool runEnter = ImGui::IsKeyPressed(ImGuiKey_Enter);
    if (ImGui::IsKeyPressed(ImGuiKey_Escape)) s.paletteOpen = false;

    auto activate = [&](const Hit& h) {
        if (h.kind == Hit::Command && h.cmd)
            s.dispatch(h.cmd->id);
        else if (h.kind == Hit::Settings && h.settingsIdx >= 0)
            requestOpenSettings(kSettingsEntries[h.settingsIdx].tabIndex);
        s.paletteOpen = false;
    };

    if (runEnter && nHits > 0 && s.paletteSel >= 0 && s.paletteSel < nHits)
        activate(hits[(size_t)s.paletteSel]);

    ImGui::BeginChild("##results", ImVec2(0, 0), false);

    const float iconSz = theme::U(2.25f);
    const float rowH   = iconSz + theme::U(0.75f);
    const float lblOff = theme::U(0.5f) + iconSz + theme::U(0.75f);

    const char* lastSection = nullptr;
    for (int i = 0; i < nHits; ++i) {
        const Hit& h = hits[(size_t)i];
        const char* section = h.kind == Hit::Command ? "Modeling Tools" : "Settings";
        if (section != lastSection) {
            if (lastSection) ImGui::Dummy(ImVec2(0.f, theme::U(0.5f)));
            ImGui::TextColored(TVEC(txt2), "%s", section);
            lastSection = section;
        }

        ImGui::PushID(i);
        bool sel = (i == s.paletteSel);
        ImVec2 pos = ImGui::GetCursorScreenPos();
        float avail = ImGui::GetContentRegionAvail().x;
        bool rowHit = ImGui::Selectable("##r", sel, 0, ImVec2(avail, rowH));
        if (sel || rowHit) {
            theme::glassRowBackground(ImGui::GetWindowDrawList(),
                pos, ImVec2(pos.x + avail, pos.y + rowH),
                theme::ctx().m.rounding, false, sel, sel ? theme::rowWash::selected
                                                           : theme::rowWash::hover);
        } else if (ImGui::IsItemHovered()) {
            theme::glassRowBackground(ImGui::GetWindowDrawList(),
                pos, ImVec2(pos.x + avail, pos.y + rowH),
                theme::ctx().m.rounding, false, false, theme::rowWash::hover);
        }
        if (rowHit) activate(h);
        if (sel && (runEnter || ImGui::IsKeyPressed(ImGuiKey_DownArrow) ||
                    ImGui::IsKeyPressed(ImGuiKey_UpArrow)))
            ImGui::SetScrollHereY();

        ImDrawList* dl = ImGui::GetWindowDrawList();
        float ty = pos.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f;

        if (h.kind == Hit::Command && h.cmd) {
            icons::draw(dl, h.cmd->icon,
                        ImVec2(pos.x + theme::U(0.5f) + iconSz * 0.5f,
                               pos.y + (rowH - iconSz) * 0.5f + iconSz * 0.5f),
                        iconSz, sel ? TCOL(accent) : TCOL(txt0));
            dl->AddText(ImVec2(pos.x + lblOff, ty), TCOL(txt0), h.cmd->label);
            char hint[64];
            std::snprintf(hint, sizeof(hint), "%s / %s",
                          cmd::workspaceLabel(h.cmd->ws), cmd::groupLabel(h.cmd->group));
            ImVec2 ts = ImGui::CalcTextSize(hint);
            dl->AddText(ImVec2(pos.x + avail - ts.x - theme::U(0.75f), ty), TCOL(txt2), hint);
        } else if (h.kind == Hit::Settings && h.settingsIdx >= 0) {
            const SettingsEntry& e = kSettingsEntries[h.settingsIdx];
            dl->AddText(ImVec2(pos.x + lblOff, ty), TCOL(txt0), e.label);
            float dw = ImGui::CalcTextSize(e.tab).x;
            dl->AddText(ImVec2(pos.x + avail - dw - theme::U(0.75f), ty), TCOL(txt2), e.tab);
        }
        ImGui::PopID();
    }

    if (hits.empty() && s.paletteQuery[0])
        ImGui::TextColored(TVEC(txt2), "No results for \"%s\"", s.paletteQuery);

    ImGui::EndChild();
    ImGui::End();
    ImGui::PopStyleVar(2);
    theme::popGlassWindowStyle();
}

} // namespace ui
