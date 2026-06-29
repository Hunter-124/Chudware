#include "ui/panels.h"
#include "ui/command_registry.h"
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

using namespace app;

static std::string lower(const char* s) {
    std::string o; if (!s) return o; for (; *s; ++s) o += (char)std::tolower((unsigned char)*s); return o;
}
static bool matches(const cmd::Command* c, const std::string& q, int& score) {
    if (q.empty()) { score = 0; return true; }
    std::string lab = lower(c->label), id = lower(c->id), kw = lower(c->keywords?c->keywords:"");
    auto pos = lab.find(q);
    if (pos != std::string::npos) { score = 100 - (int)pos; return true; }
    if (id.find(q) != std::string::npos) { score = 60; return true; }
    if (!kw.empty() && kw.find(q) != std::string::npos) { score = 40; return true; }
    return false;
}

void drawCommandPalette(AppState& s) {
    const float S = theme::S();
    ImGuiViewport* vp = ImGui::GetMainViewport();
    float w = 560*S, h = 440*S;
    ImGui::SetNextWindowPos(ImVec2(vp->Pos.x + (vp->Size.x-w)*0.5f, vp->Pos.y + vp->Size.y*0.16f));
    ImGui::SetNextWindowSize(ImVec2(w, h));
    ImGui::PushStyleColor(ImGuiCol_WindowBg, TVEC(bg2));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, 12*S);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12*S, 12*S));
    ImGuiWindowFlags fl = ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                          ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoDocking;
    ImGui::Begin("##palette", &s.paletteOpen, fl);

    // search field
    icons::inlineIcon("search", 18*S, TCOL(txt2)); ImGui::SameLine(0,8*S);
    ImGui::SetNextItemWidth(-FLT_MIN);
    if (ImGui::IsWindowAppearing()) {
        s.paletteQuery[0] = '\0'; s.paletteSel = 0;
        ImGui::SetKeyboardFocusHere();
    }
    ImGui::PushStyleColor(ImGuiCol_FrameBg, ImVec4(0,0,0,0));
    ImGui::InputTextWithHint("##q", "Search every command...", s.paletteQuery, sizeof(s.paletteQuery));
    ImGui::PopStyleColor();
    ImGui::Separator();

    // build filtered list
    std::string q = lower(s.paletteQuery);
    std::vector<std::pair<int,const cmd::Command*>> hits;
    for (const cmd::Command* c : cmd::palette()) {
        int sc; if (matches(c, q, sc)) hits.push_back({sc, c});
    }
    std::stable_sort(hits.begin(), hits.end(), [](auto&a, auto&b){ return a.first > b.first; });
    const int nHits = (int)hits.size();
    if (nHits == 0) s.paletteSel = 0;
    else {
        if (s.paletteSel < 0) s.paletteSel = 0;
        if (s.paletteSel >= nHits) s.paletteSel = nHits - 1;
        // keyboard nav
        if (ImGui::IsKeyPressed(ImGuiKey_DownArrow)) s.paletteSel++;
        if (ImGui::IsKeyPressed(ImGuiKey_UpArrow))   s.paletteSel--;
        s.paletteSel = std::max(0, std::min(s.paletteSel, nHits - 1));
    }
    bool runEnter = ImGui::IsKeyPressed(ImGuiKey_Enter);
    if (ImGui::IsKeyPressed(ImGuiKey_Escape)) s.paletteOpen = false;

    ImGui::BeginChild("##results", ImVec2(0,0), false);
    for (int i = 0; i < (int)hits.size(); ++i) {
        const cmd::Command* c = hits[i].second;
        ImGui::PushID(i);
        bool sel = (i == s.paletteSel);
        float rowH = ImGui::GetTextLineHeight() + 12*S;
        ImVec2 p = ImGui::GetCursorScreenPos();
        if (ImGui::Selectable("##r", sel, 0, ImVec2(0, rowH)) || (runEnter && sel)) {
            s.dispatch(c->id); s.paletteOpen = false;
        }
        if (sel && (runEnter || ImGui::IsKeyPressed(ImGuiKey_DownArrow) || ImGui::IsKeyPressed(ImGuiKey_UpArrow)))
            ImGui::SetScrollHereY();
        ImDrawList* dl = ImGui::GetWindowDrawList();
        float cy = p.y + rowH*0.5f;
        icons::draw(dl, c->icon, ImVec2(p.x+16*S, cy), 18*S, sel?TCOL(accent):TCOL(txt1));
        dl->AddText(ImVec2(p.x+34*S, cy - ImGui::GetTextLineHeight()*0.5f), TCOL(txt0), c->label);
        // workspace + group hint (right)
        char hint[64]; snprintf(hint, sizeof(hint), "%s / %s",
                                cmd::workspaceLabel(c->ws), cmd::groupLabel(c->group));
        ImVec2 ts = ImGui::CalcTextSize(hint);
        dl->AddText(ImVec2(p.x + ImGui::GetContentRegionAvail().x - ts.x - 6*S, cy - ts.y*0.5f), TCOL(txt2), hint);
        ImGui::PopID();
    }
    if (hits.empty()) ImGui::TextColored(TVEC(txt2), "No matching command.");
    ImGui::EndChild();

    ImGui::End();
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor();
}

} // namespace ui
