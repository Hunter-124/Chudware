#include "ui/panels.h"
#include "app/app_state.h"
#include "theme/theme.h"
#include "theme/icons.h"
#include "imgui.h"
#include <cstring>
#include <cmath>
#include <algorithm>

namespace ui {

using namespace app;

static void spinner(ImDrawList* dl, ImVec2 c, float r, float t, ImU32 col) {
    dl->PathClear();
    dl->PathArcTo(c, r, t, t + 4.2f, 18);
    dl->PathStroke(col, 0, ImMax(1.5f, 2.f*theme::S()));
}

// A rounded card background behind `content`. Returns via lambda style.
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
    float aw = ImGui::GetContentRegionAvail().x;
    ImGui::PushID(idx);
    ImGui::PushStyleVar(ImGuiStyleVar_Alpha, ImGui::GetStyle().Alpha * (0.15f + 0.85f*b.anim));
    float dy = (1.f - b.anim) * 8*S;
    ImGui::SetCursorPosY(ImGui::GetCursorPosY() + dy);
    ImDrawList* dl = ImGui::GetWindowDrawList();

    switch (b.kind) {
    case BlockKind::UserMsg: {
        card(theme::pal().bg2, theme::pal().line, [&]{
            ImVec2 p = ImGui::GetCursorScreenPos();
            ImGui::GetWindowDrawList()->AddRectFilled(ImVec2(p.x-10*S, p.y-8*S),
                ImVec2(p.x-10*S+3*S, p.y+ImGui::GetTextLineHeight()*2.2f), TCOL(accent), 0);
            ImGui::PushFont(theme::font(theme::Font::Label));
            ImGui::TextColored(TVEC(txt1), "You"); ImGui::PopFont();
            ImGui::PushTextWrapPos(0);
            ImGui::TextColored(TVEC(txt0), "%s", b.body.c_str());
            ImGui::PopTextWrapPos();
        });
    } break;
    case BlockKind::Thinking: {
        // collapsible dim reasoning
        ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt2));
        bool open = s.agent.showThinking;
        icons::inlineIcon("thinking", 14*S, TCOL(txt2)); ImGui::SameLine(0, 6*S);
        ImGui::SetCursorPosY(ImGui::GetCursorPosY());
        if (ImGui::SmallButton(open ? "Thinking  [hide]" : "Thinking  [show]")) s.agent.showThinking = !s.agent.showThinking;
        if (open) {
            ImGui::Indent(20*S);
            ImGui::PushTextWrapPos(0);
            ImGui::PushFont(theme::font(theme::Font::Body));
            ImGui::TextColored(TVEC(txt2), "%s", b.body.c_str());
            ImGui::PopFont();
            ImGui::PopTextWrapPos();
            ImGui::Unindent(20*S);
        }
        ImGui::PopStyleColor();
    } break;
    case BlockKind::Action: {
        unsigned int border = b.status==ActionStatus::Failed ? theme::pal().bad
                            : b.status==ActionStatus::Running ? theme::pal().accent : theme::pal().line;
        card(theme::pal().bg2, border, [&]{
            // header row
            ImVec2 ip = ImGui::GetCursorScreenPos();
            icons::inlineIcon("action", 16*S, TCOL(accent));
            ImGui::SameLine(0, 8*S);
            ImGui::PushFont(theme::font(theme::Font::Label));
            ImGui::TextColored(TVEC(txt0), "%s", b.title.c_str());
            ImGui::PopFont();
            // status badge (right)
            float bx = ImGui::GetWindowWidth() - 26*S;
            ImVec2 bp(ImGui::GetWindowPos().x + bx, ip.y + 8*S);
            if (b.status == ActionStatus::Running) spinner(dl, bp, 7*S, b.spin, TCOL(accent));
            else if (b.status == ActionStatus::Ok)  icons::draw(dl, "success", bp, 16*S, TCOL(good));
            else                                    icons::draw(dl, "failure", bp, 16*S, TCOL(bad));
            // expander
            if (ImGui::SmallButton(b.expanded ? "Hide details" : "Details")) b.expanded = !b.expanded;
            if (b.expanded && !b.args.empty()) {
                ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg0));
                ImGui::BeginChild("##args", ImVec2(-FLT_MIN, 0),
                                  ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_Borders);
                ImGui::PushFont(theme::font(theme::Font::Mono));
                ImGui::PushStyleColor(ImGuiCol_Text, TVEC(txt1));
                ImGui::TextUnformatted(b.args.c_str());
                ImGui::PopStyleColor();
                ImGui::PopFont();
                ImGui::EndChild();
                ImGui::PopStyleColor();
            }
            if (!b.result.empty()) {
                ImGui::PushTextWrapPos(0);
                ImGui::TextColored(b.status==ActionStatus::Failed?TVEC(bad):TVEC(txt1), "%s", b.result.c_str());
                ImGui::PopTextWrapPos();
            }
        });
    } break;
    case BlockKind::AssistantMsg: {
        icons::inlineIcon("agent", 16*S, TCOL(accent));
        ImGui::SameLine(0, 8*S);
        ImGui::BeginGroup();
        ImGui::PushTextWrapPos(0);
        ImGui::TextColored(TVEC(txt0), "%s", b.body.c_str());
        ImGui::PopTextWrapPos();
        ImGui::EndGroup();
    } break;
    case BlockKind::Usage: {
        icons::inlineIcon("running", 12*S, TCOL(txt2)); ImGui::SameLine(0, 6*S);
        ImGui::TextColored(TVEC(txt2), "%s", b.body.c_str());
    } break;
    case BlockKind::Error: {
        card(theme::pal().bg2, theme::pal().bad, [&]{
            ImGui::TextColored(TVEC(bad), "Error");
            ImGui::PushTextWrapPos(0);
            ImGui::TextColored(TVEC(txt1), "%s", b.body.c_str());
            ImGui::PopTextWrapPos();
        });
    } break;
    case BlockKind::Plan: {
        card(theme::pal().bg2, theme::pal().line, [&]{
            ImGui::TextColored(TVEC(txt1), "Plan");
            for (size_t i=0;i<b.plan.size();++i) {
                bool d = i < b.planDone.size() && b.planDone[i];
                icons::inlineIcon(d?"success":"running", 13*S, d?TCOL(good):TCOL(txt2));
                ImGui::SameLine(0,6*S);
                ImGui::TextColored(d?TVEC(txt1):TVEC(txt0), "%s", b.plan[i].c_str());
            }
        });
    } break;
    }
    (void)aw;
    ImGui::PopStyleVar();
    ImGui::Dummy(ImVec2(0, 7*S));
    ImGui::PopID();
}

void drawAgentPanel(AppState& s) {
    const float S = theme::S();
    AgentThread& a = s.agent;

    // header
    ImGui::PushFont(theme::font(theme::Font::H2));
    icons::inlineIcon("agent", 18*S, TCOL(accent)); ImGui::SameLine(0,8*S);
    ImGui::TextColored(TVEC(txt0), "Assistant");
    ImGui::PopFont();

    const char* models[] = {"Claude", "Gemini", "Local"};
    const char* tlabel = a.showThinking ? "Thoughts on" : "Thoughts off";
    float rx = ImGui::GetContentRegionMax().x;
    float comboW = 84*S;
    float toggleW = ImGui::CalcTextSize(tlabel).x + ImGui::GetStyle().FramePadding.x*2 + 4*S;
    ImGui::SameLine();
    ImGui::SetCursorPosX(rx - comboW - toggleW - 6*S);
    if (ImGui::SmallButton(tlabel)) a.showThinking = !a.showThinking;
    ImGui::SameLine(0, 6*S);
    ImGui::SetNextItemWidth(comboW);
    ImGui::Combo("##model", &a.model, models, 3);
    ImGui::Separator();

    // transcript
    float composerH = 92*S;
    ImGui::BeginChild("##transcript", ImVec2(0, -composerH), false);
    for (int i = 0; i < (int)a.blocks.size(); ++i) drawBlock(s, a.blocks[i], i);
    if (a.running) {
        ImGui::Dummy(ImVec2(0, 2*S));
        icons::inlineIcon("running", 12*S, TCOL(txt2));
        ImGui::SameLine(0,6*S); ImGui::TextColored(TVEC(txt2), "working...");
    }
    // autoscroll
    if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 40*S) ImGui::SetScrollHereY(1.0f);
    ImGui::EndChild();

    // composer
    ImGui::PushStyleColor(ImGuiCol_ChildBg, TVEC(bg2));
    ImGui::PushStyleVar(ImGuiStyleVar_ChildRounding, 10*S);
    ImGui::BeginChild("##composer", ImVec2(0, composerH-6*S), ImGuiChildFlags_Borders);
    static char buf[4096] = {0};
    ImGui::PushStyleColor(ImGuiCol_FrameBg, ImVec4(0,0,0,0));
    ImGui::PushItemWidth(-FLT_MIN);
    ImGui::InputTextMultiline("##in", buf, sizeof(buf), ImVec2(-FLT_MIN, composerH-44*S),
                              ImGuiInputTextFlags_None);
    ImGui::PopItemWidth();
    ImGui::PopStyleColor();
    // send / stop
    float by = ImGui::GetCursorPosY();
    ImGui::SetCursorPosY(by);
    ImGui::TextColored(TVEC(txt2), "Enter to send");
    ImGui::SameLine();
    float bw = 64*S;
    ImGui::SetCursorPosX(ImGui::GetContentRegionMax().x - bw);
    if (a.running) {
        if (ImGui::Button("Stop", ImVec2(bw, 0))) { a.running = false; a.demoStep = -1; }
    } else {
        bool send = ImGui::Button("Send", ImVec2(bw, 0));
        bool enter = ImGui::IsKeyPressed(ImGuiKey_Enter) && !ImGui::GetIO().KeyShift && ImGui::IsWindowFocused(ImGuiFocusedFlags_ChildWindows);
        if ((send || enter) && buf[0]) { s.startDemoAgent(buf); buf[0] = 0; }
    }
    ImGui::EndChild();
    ImGui::PopStyleVar();
    ImGui::PopStyleColor();
}

} // namespace ui
