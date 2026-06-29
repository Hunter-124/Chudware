#include "ui/panels.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include <cstring>
#include <cmath>
#include <algorithm>
#include <vector>
#include <string>

namespace ui {

using namespace app;

namespace {
struct Session {
    std::string title;
    std::vector<AgentBlock> blocks;
    int contextUsed = 0;
    bool running = false;
};
static std::vector<Session> gSessions;
static int gActiveSession = 0;
static bool gShowSessions = false;
static int gAgentMode = 0;       // 0 Design, 1 Edit, 2 Ask
static bool gOffline = false;
static int gThinking = 3;        // 0 off .. 3 high
static const char* kModes[] = {"Design", "Edit", "Ask"};
static const char* kThink[] = {"Off", "Low", "Medium", "High"};
static const int kCtxWin = 128000;

static void ensureSessions() {
    if (gSessions.empty()) {
        Session s; s.title = "Chat 1";
        gSessions.push_back(std::move(s));
        gActiveSession = 0;
    }
}

static void syncBlocksToSession(AppState& s) {
    ensureSessions();
    gSessions[gActiveSession].blocks = s.agent.blocks;
    gSessions[gActiveSession].running = s.agent.running;
}

static void loadSessionToAgent(AppState& s, int idx) {
    if (idx < 0 || idx >= (int)gSessions.size()) return;
    syncBlocksToSession(s);
    gActiveSession = idx;
    s.agent.blocks = gSessions[idx].blocks;
    s.agent.running = gSessions[idx].running;
    gShowSessions = false;
}

static void newSession(AppState& s) {
    ensureSessions();
    syncBlocksToSession(s);
    Session ns;
    ns.title = "Chat " + std::to_string((int)gSessions.size() + 1);
    gSessions.push_back(std::move(ns));
    gActiveSession = (int)gSessions.size() - 1;
    s.agent.blocks.clear();
    s.agent.running = false;
    gShowSessions = false;
}
} // namespace

static void spinner(ImDrawList* dl, ImVec2 c, float r, float t, ImU32 col) {
    dl->PathClear();
    dl->PathArcTo(c, r, t, t + 4.2f, 18);
    dl->PathStroke(col, 0, ImMax(1.5f, 2.f*theme::S()));
}

template <class F>
static void card(unsigned int bgHex, unsigned int borderHex, F&& content) {
    const float S = theme::S();
    ImGui::PushStyleColor(ImGuiCol_ChildBg, theme::vec(bgHex));
    ImGui::PushStyleColor(ImGuiCol_Border, theme::vec(borderHex));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 8*S);
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10*S, 8*S));
    ImGui::BeginChild(ImGui::GetID(&content), ImVec2(-FLT_MIN, 0),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_Borders, ImGuiWindowFlags_NoScrollbar);
    content();
    ImGui::EndChild();
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(2);
}

static void drawBlock(AppState& s, AgentBlock& b, int idx) {
    const float S = theme::S();
    ImGui::PushID(idx);
    ImGui::PushStyleVar(ImGuiStyleVar_Alpha, ImGui::GetStyle().Alpha * (0.15f + 0.85f*b.anim));
    float dy = (1.f - b.anim) * 8*S;
    ImGui::SetCursorPosY(ImGui::GetCursorPosY() + dy);
    ImDrawList* dl = ImGui::GetWindowDrawList();

    switch (b.kind) {
    case BlockKind::UserMsg:
        card(theme::pal().bg2, theme::pal().line, [&]{
            ImGui::PushFont(theme::font(theme::Font::Label));
            ImGui::TextColored(TVEC(txt1), "You"); ImGui::PopFont();
            ImGui::PushTextWrapPos(0);
            ImGui::TextColored(TVEC(txt0), "%s", b.body.c_str());
            ImGui::PopTextWrapPos();
        });
        break;
    case BlockKind::Thinking:
        if (s.agent.showThinking) {
            ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
            icons::inlineIcon("thinking", 14*S, TCOL(txt2)); ImGui::SameLine(0, 6*S);
            ImGui::TextColored(TVEC(txt2), "%s", b.body.c_str());
            ImGui::PopStyleColor();
        }
        break;
    case BlockKind::AssistantMsg:
        icons::inlineIcon("agent", 16*S, TCOL(accent)); ImGui::SameLine(0, 8*S);
        ImGui::PushTextWrapPos(0);
        ImGui::TextColored(TVEC(txt0), "%s", b.body.c_str());
        ImGui::PopTextWrapPos();
        break;
    default:
        ImGui::TextColored(TVEC(txt1), "%s", b.body.c_str());
        break;
    }
    ImGui::PopStyleVar();
    ImGui::Dummy(ImVec2(0, 7*S));
    ImGui::PopID();
}

static void drawTopBar(AppState& s) {
    const float S = theme::S();
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8*S, 3*S));
    if (ImGui::SmallButton(kModes[gAgentMode])) ImGui::OpenPopup("##mode");
    if (ImGui::BeginPopup("##mode")) {
        for (int i = 0; i < 3; ++i)
            if (ImGui::Selectable(kModes[i], gAgentMode == i)) gAgentMode = i;
        ImGui::EndPopup();
    }
    ImGui::SameLine();
    const char* pl = gOffline ? "Permissions: Offline" : "Permissions";
    if (ImGui::SmallButton(pl)) ImGui::OpenPopup("##perm");
    if (ImGui::BeginPopup("##perm")) {
        ImGui::Checkbox("Offline mode (local only)", &gOffline);
        ImGui::Checkbox("Show assistant thoughts", &s.agent.showThinking);
        ImGui::EndPopup();
    }
    ImGui::SameLine();
    char tb[48]; std::snprintf(tb, sizeof(tb), "Thinking: %s", kThink[gThinking]);
    if (ImGui::SmallButton(tb)) ImGui::OpenPopup("##think");
    if (ImGui::BeginPopup("##think")) {
        for (int i = 0; i < 4; ++i)
            if (ImGui::Selectable(kThink[i], gThinking == i)) gThinking = i;
        ImGui::EndPopup();
    }
    ImGui::PopStyleVar();
}

static void drawSessionsList(AppState& s) {
    ensureSessions();
    if (ImGui::Button("+ New session")) newSession(s);
    ImGui::SameLine();
    if (ImGui::SmallButton("Back to chat")) gShowSessions = false;
    ImGui::Separator();
    for (int i = 0; i < (int)gSessions.size(); ++i) {
        std::string label = gSessions[i].title;
        if (gSessions[i].running) label += "  • running";
        if (ImGui::Selectable(label.c_str(), i == gActiveSession))
            loadSessionToAgent(s, i);
    }
}

static void drawContextMeter() {
    const float S = theme::S();
    int used = gSessions.empty() ? 0 : gSessions[gActiveSession].contextUsed;
    char buf[32]; std::snprintf(buf, sizeof(buf), "%dk/%dk", (used+500)/1000, kCtxWin/1000);
    float w = ImGui::CalcTextSize(buf).x;
    ImGui::SetCursorPosX(ImGui::GetContentRegionMax().x - w);
    ImGui::TextColored(TVEC(txt2), "%s", buf);
}

void drawAgentPanel(AppState& s) {
    const float S = theme::S();
    AgentThread& a = s.agent;
    ensureSessions();

    ImVec2 wp = ImGui::GetWindowPos();
    ImVec2 ws = ImGui::GetWindowSize();
    theme::glassPanelBackground(ImGui::GetWindowDrawList(), wp, ImVec2(wp.x+ws.x, wp.y+ws.y));

    ImGui::PushFont(theme::font(theme::Font::H2));
    icons::inlineIcon("agent", 18*S, TCOL(accent)); ImGui::SameLine(0,8*S);
    ImGui::TextColored(TVEC(txt0), "Assistant");
    ImGui::PopFont();
    ImGui::SameLine();
    float bw = ImGui::CalcTextSize("Sessions").x + ImGui::GetStyle().FramePadding.x * 2;
    ImGui::SetCursorPosX(ImGui::GetContentRegionMax().x - bw);
    if (ImGui::SmallButton("Sessions")) gShowSessions = !gShowSessions;

    ImGui::Separator();
    drawTopBar(s);

    float composerH = 92*S;
    ImGui::BeginChild("##transcript", ImVec2(0, -composerH - 22*S), false);
    if (gShowSessions) drawSessionsList(s);
    else for (int i = 0; i < (int)a.blocks.size(); ++i) drawBlock(s, a.blocks[i], i);
    if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 40*S) ImGui::SetScrollHereY(1.f);
    ImGui::EndChild();

    drawContextMeter();
    ImGui::BeginChild("##composer", ImVec2(0, composerH), ImGuiChildFlags_Borders);
    static char buf[4096] = {0};
    ImGui::InputTextMultiline("##in", buf, sizeof(buf), ImVec2(-FLT_MIN, composerH-44*S));
    const char* models[] = {"Claude", "Gemini", "Local"};
    ImGui::TextColored(TVEC(txt2), "Enter to send");
    ImGui::SameLine();
    ImGui::SetNextItemWidth(84*S);
    ImGui::Combo("##model", &a.model, models, 3);
    ImGui::SameLine();
    float sendW = 64*S;
    ImGui::SetCursorPosX(ImGui::GetContentRegionMax().x - sendW);
    if (a.running) {
        if (ImGui::Button("Stop", ImVec2(sendW, 0))) { a.running = false; a.demoStep = -1; }
    } else if ((ImGui::Button("Send", ImVec2(sendW, 0)) || (ImGui::IsKeyPressed(ImGuiKey_Enter) && buf[0])) && buf[0]) {
        s.startDemoAgent(buf); buf[0] = 0;
        gSessions[gActiveSession].contextUsed = 1200 + (int)a.blocks.size() * 400;
    }
    ImGui::EndChild();
    syncBlocksToSession(s);
}

} // namespace ui
