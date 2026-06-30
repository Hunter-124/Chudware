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
static bool gSupervised = false;
static int gThinking = 3;        // 0 off .. 3 high
static int gModel = 0;           // 0 Claude, 1 Gemini, 2 Local
static const char* kModes[] = {"Design", "Edit", "Ask"};
static const char* kModels[] = {"Claude Sonnet", "Gemini Pro", "Local (llama.cpp)"};
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

static bool modelSupportsThinking(int model) { return model != 2; }  // local demo: no effort knob
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
    const float frac = kCtxWin > 0 ? std::min(1.f, (float)used / (float)kCtxWin) : 0.f;
    const float barW = 40 * S, gap = 5 * S;
    char full[32]; std::snprintf(full, sizeof(full), "%dk/%dk", (used+500)/1000, kCtxWin/1000);
    char lean[40]; std::snprintf(lean, sizeof(lean), "ctx %s", full);
    const float fullTw = ImGui::CalcTextSize(full).x;
    const float room = ImGui::GetContentRegionAvail().x;
    const bool showBar = room >= barW + gap + fullTw;
    const float fh = ImGui::GetFrameHeight();
    ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
    if (showBar) {
        ImVec2 p = ImGui::GetCursorScreenPos();
        float by0 = p.y + fh * 0.5f - 3 * S, by1 = p.y + fh * 0.5f + 3 * S;
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImU32 fg = frac < 0.5f ? IM_COL32(120, 200, 120, 255)
                 : frac < 0.85f ? IM_COL32(220, 170, 90, 255)
                                : IM_COL32(220, 110, 100, 255);
        dl->AddRectFilled(ImVec2(p.x, by0), ImVec2(p.x + barW, by1), TCOL(accentDim), 2 * S);
        if (frac > 0)
            dl->AddRectFilled(ImVec2(p.x, by0), ImVec2(p.x + barW * frac, by1), fg, 2 * S);
        dl->AddRect(ImVec2(p.x, by0), ImVec2(p.x + barW, by1), TCOL(txt2), 2 * S);
        ImGui::Dummy(ImVec2(barW, fh));
        ImGui::SameLine(0, gap);
    }
    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(showBar ? full : lean);
    ImGui::PopStyleColor();
}

static void drawComposerPrelude(AppState& s) {
    const float S = theme::S();
    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float rowH = ImGui::GetFrameHeight() + 4 * S;
    ImVec2 rowMax(rowMin.x + ImGui::GetContentRegionAvail().x, rowMin.y + rowH);
    theme::glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, 4.f * S, false, false);

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8*S, 3*S));
    if (ImGui::SmallButton(kModes[gAgentMode])) ImGui::OpenPopup("##cmpmode");
    if (ImGui::BeginPopup("##cmpmode")) {
        for (int i = 0; i < 3; ++i)
            if (ImGui::Selectable(kModes[i], gAgentMode == i)) gAgentMode = i;
        ImGui::EndPopup();
    }
    ImGui::SameLine();
    const char* pl = gOffline ? "Permissions: Offline" : "Permissions";
    if (ImGui::SmallButton(pl)) ImGui::OpenPopup("##cmpperm");
    if (ImGui::BeginPopup("##cmpperm")) {
        ImGui::Checkbox("Offline mode (local only)", &gOffline);
        ImGui::Checkbox("Supervised (plan + clarify before modeling)", &gSupervised);
        ImGui::Checkbox("Show assistant thoughts", &s.agent.showThinking);
        ImGui::EndPopup();
    }
    const float room = ImGui::GetContentRegionAvail().x;
    char full[32];
    int used = gSessions.empty() ? 0 : gSessions[gActiveSession].contextUsed;
    std::snprintf(full, sizeof(full), "%dk/%dk", (used+500)/1000, kCtxWin/1000);
    const float barW = 40 * S, gap = 5 * S;
    const float w = (room >= barW + gap + ImGui::CalcTextSize(full).x)
                        ? barW + gap + ImGui::CalcTextSize(full).x
                        : ImGui::CalcTextSize(("ctx " + std::string(full)).c_str()).x;
    const float x = ImGui::GetContentRegionMax().x - w;
    if (x > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(x);
    drawContextMeter();
    ImGui::PopStyleVar();
    ImGui::Dummy(ImVec2(0, 4*S));
}

static void drawComposerToolbar(AppState& s, bool canSend, bool& submit) {
    const float S = theme::S();
    AgentThread& a = s.agent;
    ImVec2 rowMin = ImGui::GetCursorScreenPos();
    const float rowH = ImGui::GetFrameHeight() + 4 * S;
    ImVec2 rowMax(rowMin.x + ImGui::GetContentRegionAvail().x, rowMin.y + rowH);
    theme::glassRowBackground(ImGui::GetWindowDrawList(), rowMin, rowMax, 4.f * S, false, false);

    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(8*S, 4*S));
    ImGui::PushStyleColor(ImGuiCol_Button, TVEC(bg2));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered, TVEC(bg3));
    ImGui::PushStyleColor(ImGuiCol_ButtonActive, TVEC(line));
    if (ImGui::Button(kModels[gModel])) ImGui::OpenPopup("##modeldropdown");
    if (ImGui::BeginPopup("##modeldropdown")) {
        for (int i = 0; i < 3; ++i)
            if (ImGui::Selectable(kModels[i], gModel == i)) { gModel = i; a.model = i; }
        ImGui::EndPopup();
    }
    ImGui::SameLine();
    if (modelSupportsThinking(gModel)) {
        char tb[48]; std::snprintf(tb, sizeof(tb), "Thinking: %s", kThink[gThinking]);
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
        if (ImGui::Button(tb)) ImGui::OpenPopup("##cmpeffort");
        ImGui::PopStyleColor();
        if (ImGui::BeginPopup("##cmpeffort")) {
            for (int i = 0; i < 4; ++i)
                if (ImGui::Selectable(kThink[i], gThinking == i)) gThinking = i;
            ImGui::EndPopup();
        }
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
        ImGui::BeginDisabled();
        ImGui::Button("Thinking: n/a");
        ImGui::EndDisabled();
        ImGui::PopStyleColor();
    }
    const float sendSz = ImGui::GetFrameHeight();
    const float gearSz = sendSz;
    ImGui::SameLine();
    float rightX = ImGui::GetContentRegionMax().x - sendSz - 6*S - gearSz;
    if (rightX > ImGui::GetCursorPosX()) ImGui::SetCursorPosX(rightX);
    if (ImGui::SmallButton("set")) { /* settings tab placeholder */ }
    ImGui::SameLine(0, 6*S);
    if (a.running) {
        ImGui::PushStyleColor(ImGuiCol_Button, TVEC(bad));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, TVEC(bad));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, TVEC(bad));
        if (ImGui::Button("Stop", ImVec2(sendSz, sendSz))) { a.running = false; a.demoStep = -1; }
        ImGui::PopStyleColor(3);
    } else {
        ImGui::PushStyleColor(ImGuiCol_Button, canSend ? TVEC(accent) : TVEC(bg2));
        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, canSend ? TVEC(accentDim) : TVEC(bg3));
        ImGui::PushStyleColor(ImGuiCol_ButtonActive, TVEC(accentDim));
        if (ImGui::Button("Send", ImVec2(sendSz, sendSz)) && canSend) submit = true;
        ImGui::PopStyleColor(3);
    }
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar();
}

void drawAgentPanel(AppState& s) {
    const float S = theme::S();
    AgentThread& a = s.agent;
    ensureSessions();
    gModel = a.model;

    ImGui::PushFont(theme::font(theme::Font::H2));
    icons::inlineIcon("agent", 18*S, TCOL(accent)); ImGui::SameLine(0,8*S);
    ImGui::TextColored(TVEC(txt0), "Assistant");
    ImGui::PopFont();
    ImGui::SameLine();
    float bw = ImGui::CalcTextSize("Sessions").x + ImGui::GetStyle().FramePadding.x * 2;
    ImGui::SetCursorPosX(ImGui::GetContentRegionMax().x - bw);
    if (ImGui::SmallButton("Sessions")) gShowSessions = !gShowSessions;

    ImGui::Separator();

    static float sComposerH = 0.f;
    float reserve = (sComposerH > 0 ? sComposerH : 110*S) + 8*S;
    ImGui::BeginChild("##transcript", ImVec2(0, -reserve), false);
    if (gShowSessions) drawSessionsList(s);
    else for (int i = 0; i < (int)a.blocks.size(); ++i) drawBlock(s, a.blocks[i], i);
    if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 40*S) ImGui::SetScrollHereY(1.f);
    ImGui::EndChild();

    ImGui::BeginGroup();
    static char buf[4096] = {0};
    drawComposerPrelude(s);
    ImGui::PushStyleColor(ImGuiCol_FrameBg, ImVec4(0,0,0,0));
    ImGui::PushStyleColor(ImGuiCol_Border, TVEC(line));
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, 1.f);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, 8*S);
    ImGui::InputTextMultiline("##in", buf, sizeof(buf), ImVec2(-FLT_MIN, 56*S));
    bool enterSend = ImGui::IsKeyPressed(ImGuiKey_Enter) && !ImGui::GetIO().KeyShift && buf[0];
    ImGui::PopStyleVar(2);
    ImGui::PopStyleColor(2);
    ImGui::Dummy(ImVec2(0, 4*S));
    bool submit = false;
    bool canSend = buf[0] != 0 && !a.running;
    drawComposerToolbar(s, canSend, submit);
    if ((submit || enterSend) && canSend) {
        s.startDemoAgent(buf); buf[0] = 0;
        gSessions[gActiveSession].contextUsed = 1200 + (int)a.blocks.size() * 400;
    }
    ImGui::EndGroup();
    sComposerH = ImGui::GetItemRectSize().y;

    syncBlocksToSession(s);
}

} // namespace ui
