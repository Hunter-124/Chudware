#include "ui/command_search.h"
#include "ui/command_registry.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <vector>

namespace ui {

using app::AppState;

const SettingsEntry kSettingsEntries[] = {
    { "Units",                    "General",      0 },
    { "MMGS units",               "General",      0 },
    { "IPS units",                "General",      0 },
    { "Light theme",              "General",      0 },
    { "Dark theme",               "General",      0 },
    { "Theme",                    "General",      0 },
    { "Engine routing",           "General",      0 },
    { "C++ core",                 "General",      0 },
    { "Performance profiler",     "General",      0 },
    { "Profiler",                 "General",      0 },
    { "AI provider",              "AI",           1 },
    { "Model",                    "AI",           1 },
    { "API key",                  "AI",           1 },
    { "Gemini key",               "AI",           1 },
    { "Anthropic key",            "AI",           1 },
    { "OpenAI key",               "AI",           1 },
    { "Cloud API key",            "AI",           1 },
    { "Vision",                   "AI",           1 },
    { "Offline mode",             "AI",           1 },
    { "Reasoning effort",         "AI",           1 },
    { "Local endpoint",           "AI",           1 },
    { "Manage models",            "AI",           1 },
    { "Hidden models",            "AI",           1 },
    { "Role prompts",             "Prompts",      2 },
    { "Agent prompts",            "Prompts",      2 },
    { "Prompt editor",            "Prompts",      2 },
    { "Plan before building",     "Advanced",     3 },
    { "Plan mode",                "Advanced",     3 },
    { "Refine-pass cap",          "Advanced",     3 },
    { "Max iters",                "Advanced",     3 },
    { "Iteration budget",         "Advanced",     3 },
    { "System prompt",            "Advanced",     3 },
    { "System prompt override",   "Advanced",     3 },
    { "Vision review model",      "Experimental", 4 },
    { "Model routing",            "Experimental", 4 },
    { "Seek-CAD split",           "Experimental", 4 },
    { "Parametric sequence mode", "Experimental", 4 },
    { "CAD sequence mode",        "Experimental", 4 },
    { "Feature sequence",         "Experimental", 4 },
};
const int kSettingsEntryCount =
    (int)(sizeof(kSettingsEntries) / sizeof(kSettingsEntries[0]));

namespace {

static char  s_buf[256]      = {};
static bool  s_wantFocus     = false;
static bool  s_open          = false;
static bool  s_pendingSettings = false;
static int   s_pendingTab    = -1;

bool icontains(const char* hay, const char* needle) {
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

} // namespace

void setCommandSearchOpen(bool open) {
    if (open && !s_open) {
        s_buf[0]    = '\0';
        s_wantFocus = true;
    }
    s_open = open;
}

bool commandSearchOpen() { return s_open; }

void requestOpenSettings(int tabIndex) {
    s_pendingSettings = true;
    s_pendingTab      = tabIndex;
}

bool takeOpenSettingsRequest(int* tabIndexOut) {
    if (!s_pendingSettings) return false;
    s_pendingSettings = false;
    if (tabIndexOut) *tabIndexOut = s_pendingTab;
    s_pendingTab = -1;
    return true;
}

void drawCommandSearch(AppState& st) {
    static bool s_wasShown = false;
    if (s_open && !s_wasShown) {
        s_buf[0]    = '\0';
        s_wantFocus = true;
    }
    s_wasShown = s_open;
    if (!s_open) return;

    ImGuiViewport* vp = ImGui::GetMainViewport();
    const float S   = theme::S();
    const float popW = 560.f * S;
    const float popH = 460.f * S;
    ImGui::SetNextWindowSize(ImVec2(popW, popH), ImGuiCond_Always);
    ImGui::SetNextWindowPos(vp->GetCenter(), ImGuiCond_Always, ImVec2(0.5f, 0.5f));

    bool shouldClose = false;

    theme::pushGlassWindowStyle();
    ImGui::PushStyleVar(ImGuiStyleVar_WindowRounding, theme::ctx().m.roundingLg * S);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(theme::U(1.5f), theme::U(1.5f)));
    if (ImGui::Begin("##CommandSearch", nullptr,
                     ImGuiWindowFlags_NoTitleBar | ImGuiWindowFlags_NoResize |
                     ImGuiWindowFlags_NoMove | ImGuiWindowFlags_NoDocking |
                     ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse |
                     ImGuiWindowFlags_NoCollapse)) {

        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(theme::U(1.f), theme::U(0.75f)));
        if (s_wantFocus) {
            ImGui::SetKeyboardFocusHere();
            s_wantFocus = false;
        }
        ImGui::SetNextItemWidth(-1.f);
        const bool entered = ImGui::InputText("##csq", s_buf, sizeof(s_buf),
                                              ImGuiInputTextFlags_EnterReturnsTrue);
        ImGui::PopStyleVar();

        if (ImGui::IsKeyPressed(ImGuiKey_Escape)) shouldClose = true;
        ImGui::Separator();

        std::vector<const cmd::Command*> cmdHits;
        std::vector<int> setIdx;
        cmdHits.reserve(32);
        setIdx.reserve(16);

        for (const cmd::Command* c : cmd::palette()) {
            if (icontains(c->label, s_buf) ||
                icontains(cmd::workspaceLabel(c->ws), s_buf) ||
                icontains(cmd::groupLabel(c->group), s_buf) ||
                icontains(c->keywords, s_buf))
                cmdHits.push_back(c);
        }
        for (int i = 0; i < kSettingsEntryCount; ++i) {
            const SettingsEntry& e = kSettingsEntries[i];
            if (icontains(e.label, s_buf) || icontains(e.tab, s_buf))
                setIdx.push_back(i);
        }

        if (entered && !shouldClose) {
            if (!cmdHits.empty()) {
                st.dispatch(cmdHits[0]->id);
                shouldClose = true;
            } else if (!setIdx.empty()) {
                requestOpenSettings(kSettingsEntries[setIdx[0]].tabIndex);
                shouldClose = true;
            }
        }

        ImGui::BeginChild("##csqlist", ImVec2(0.f, 0.f), ImGuiChildFlags_None);

        auto secLabel = [&](const char* name) {
            ImGui::Dummy(ImVec2(0.f, theme::U(0.25f)));
            ImGui::TextColored(TVEC(txt2), "%s", name);
        };

        if (!cmdHits.empty()) {
            secLabel("Modeling Tools");
            for (size_t i = 0; i < cmdHits.size(); ++i) {
                const cmd::Command* c = cmdHits[i];
                char dimstr[128];
                std::snprintf(dimstr, sizeof(dimstr), "%s / %s",
                              cmd::workspaceLabel(c->ws), cmd::groupLabel(c->group));
                ImGui::PushID((int)i);
                ImVec2 pos  = ImGui::GetCursorScreenPos();
                float avail = ImGui::GetContentRegionAvail().x;
                const float rowH = theme::U(3.25f);
                bool hit = ImGui::Selectable("##row", false, 0, ImVec2(avail, rowH));
                if (hit) {
                    st.dispatch(c->id);
                    shouldClose = true;
                }
                if (ImGui::IsItemHovered()) {
                    theme::glassRowBackground(ImGui::GetWindowDrawList(),
                        pos, ImVec2(pos.x + avail, pos.y + rowH),
                        theme::ctx().m.rounding, false, false, theme::rowWash::hover);
                }
                float ty = pos.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f;
                const float iconSz = theme::U(2.5f);
                const float lblOff = theme::U(0.5f) + iconSz + theme::U(0.75f);
                icons::draw(ImGui::GetWindowDrawList(), c->icon,
                            ImVec2(pos.x + theme::U(0.5f) + iconSz * 0.5f,
                                   pos.y + (rowH - iconSz) * 0.5f + iconSz * 0.5f),
                            iconSz, TCOL(txt0));
                ImGui::GetWindowDrawList()->AddText(
                    ImVec2(pos.x + lblOff, ty), TCOL(txt0), c->label);
                float dw = ImGui::CalcTextSize(dimstr).x;
                ImGui::GetWindowDrawList()->AddText(
                    ImVec2(pos.x + avail - dw - theme::U(1.f), ty), TCOL(txt2), dimstr);
                ImGui::PopID();
            }
        }

        if (!setIdx.empty()) {
            if (!cmdHits.empty()) ImGui::Dummy(ImVec2(0.f, theme::U(0.5f)));
            secLabel("Settings");
            for (int si : setIdx) {
                const SettingsEntry& e = kSettingsEntries[si];
                ImGui::PushID(1000 + si);
                ImVec2 pos  = ImGui::GetCursorScreenPos();
                float avail = ImGui::GetContentRegionAvail().x;
                const float rowH = theme::U(3.25f);
                bool hit = ImGui::Selectable("##row", false, 0, ImVec2(avail, rowH));
                if (hit) {
                    requestOpenSettings(e.tabIndex);
                    shouldClose = true;
                }
                if (ImGui::IsItemHovered()) {
                    theme::glassRowBackground(ImGui::GetWindowDrawList(),
                        pos, ImVec2(pos.x + avail, pos.y + rowH),
                        theme::ctx().m.rounding, false, false, theme::rowWash::hover);
                }
                float ty = pos.y + (rowH - ImGui::GetTextLineHeight()) * 0.5f;
                const float lblOff = theme::U(0.5f) + theme::U(2.5f) + theme::U(0.75f);
                ImGui::GetWindowDrawList()->AddText(
                    ImVec2(pos.x + lblOff, ty), TCOL(txt0), e.label);
                float dw = ImGui::CalcTextSize(e.tab).x;
                ImGui::GetWindowDrawList()->AddText(
                    ImVec2(pos.x + avail - dw - theme::U(1.f), ty), TCOL(txt2), e.tab);
                ImGui::PopID();
            }
        }

        if (cmdHits.empty() && setIdx.empty() && s_buf[0]) {
            ImGui::Dummy(ImVec2(0.f, theme::U(1.f)));
            char msg[320];
            std::snprintf(msg, sizeof(msg), "No results for \"%s\"", s_buf);
            ImGui::TextColored(TVEC(txt2), "%s", msg);
        }

        ImGui::EndChild();
    }
    ImGui::End();
    ImGui::PopStyleVar(2);
    theme::popGlassWindowStyle();

    if (shouldClose) {
        s_open = false;
        s_buf[0] = '\0';
    }
}

} // namespace ui
