// Copilot agent overview — see agent_overview.h. Reads the AgentDag trace and the transcript's
// PlanState; owns no run state of its own beyond per-conversation disclosure choices.

#include "agent_overview.h"

#include "copilot_internal.h"
#include "ui/ai/agent_dag.h"

namespace ui {
namespace {

constexpr int    kAgentRows   = 3;      // compact overview; expand for the full roster
constexpr int    kTodoRows    = 3;
constexpr size_t kInlineSteps = 6;
constexpr float  kMaxHeightFrac = 0.30f;

struct ConvUi {
    int  openOverride = -1;    // -1 follow the run (open while it runs), 0 closed, 1 open
    bool wasRunning   = false;
    bool agentsAll    = false;
    bool todosAll     = false;
    std::string inspect;       // sub-agent whose recent steps show inline
};
std::unordered_map<std::string, ConvUi> s_ui;
std::vector<AgentTraceAgent>           s_agents;   // reused every frame (no per-frame allocs)
std::vector<AgentTraceStep>            s_steps;

struct Todo {
    const PlanState*             plan = nullptr;
    std::vector<const PlanItem*> items;
    int  done          = 0;
    int  active        = -1;     // the running item, else the next open one
    bool activeRunning = false;
    bool running = false;
};
Todo s_todo;

bool itemClosed(const std::string& s) {
    return s == "done" || s == "pass" || s == "abandoned" || s == "skipped";
}

// The newest plan in the viewed transcript that has real (non-sub-agent) items. Pointers stay
// valid for this frame: nothing below mutates the transcript.
void findTodo(const AppState& st, Todo& t) {
    t.plan = nullptr;
    t.items.clear();
    t.done = 0;
    t.active = -1;
    t.activeRunning = false;
    t.running = false;
    for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it) {
        if (it->fromUser || !it->planState.present) continue;
        for (const PlanItem& p : it->planState.items)
            if (p.kind != "subagent" && !p.title.empty()) t.items.push_back(&p);
        if (t.items.empty()) continue;
        t.plan = &it->planState;
        t.running = it->running;
        break;
    }
    for (int i = 0; i < (int)t.items.size(); ++i) {
        const std::string& s = t.items[(size_t)i]->status;
        if (itemClosed(s)) ++t.done;
        if (t.running && s == "running" && !t.activeRunning) {
            t.active = i;
            t.activeRunning = true;
        }
    }
    if (t.running && t.active < 0)
        for (int i = 0; i < (int)t.items.size(); ++i)
            if (!itemClosed(t.items[(size_t)i]->status)) { t.active = i; break; }
}

kit::Status agentStatus(const std::string& s) {
    if (s == "running")                 return kit::Status::Running;
    if (s == "pending" || s == "idle")  return kit::Status::Pending;
    if (s == "done")                    return kit::Status::Done;
    if (s == "failed" || s == "error")  return kit::Status::Failed;
    return kit::Status::Skipped;        // cancelled / stopped
}

// Trace step vocabulary (running | ok | failed | retry | skipped | stopped | cancelled | "").
bool stepStatus(const std::string& s, kit::Status* out) {
    if (s.empty()) return false;
    if (s == "running")     *out = kit::Status::Running;
    else if (s == "ok")     *out = kit::Status::Done;
    else if (s == "failed") *out = kit::Status::Failed;
    else if (s == "retry")  *out = kit::Status::Retry;
    else                    *out = kit::Status::Skipped;
    return true;
}

ImU32 ink(const ImVec4& c) { return ImGui::ColorConvertFloat4ToU32(c); }

std::string elapsedText(double seconds) { return kit::fmtDuration(seconds); }

// Wall time the trace saw the agent working; < 0 when unknown. A running agent counts up to
// now; a finished one spans start -> end (else its last step). A finished agent whose whole
// life fell inside one event batch has no measurable span — unknown, not "0.0 s".
double agentElapsed(const AgentTraceAgent& a, double now) {
    if (a.startTs <= 0.0) return -1.0;
    if (a.status == "running") return std::max(0.0, now - a.startTs);
    const double end = a.endTs > 0.0 ? a.endTs : a.lastStepTs;
    const double span = end - a.startTs;
    return span >= 0.05 ? span : -1.0;
}

float glyphR(float lineH) { return lineH * 0.30f; }   // kit's status-mark ratio (cell * 0.30)

// "… N more — expand" / "Show less" toggle row.
void moreRow(const char* id, int hidden, bool* all) {
    char label[64];
    if (*all) std::snprintf(label, sizeof(label), "Show less");
    else      std::snprintf(label, sizeof(label), "\xE2\x80\xA6 %d more \xE2\x80\x94 expand", hidden);
    const AdaptiveSurface& surf = chatSurf();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float h = ImGui::GetTextLineHeight() + kit::padY();
    if (kit::rowBegin(id, h, false)) *all = !*all;
    ImGui::GetWindowDrawList()->AddText(
        ImVec2(p0.x + kit::padX(), p0.y + kit::padY() * 0.5f),
        ink(kit::toneOn(kit::Tone::Accent, surf)), label);
    kit::rowEnd();
}


// ---- todo checklist --------------------------------------------------------------------------
void drawTodoRow(const PlanItem& it, int idx, bool active, const ui::Fonts& fonts) {
    const AdaptiveSurface& surf = chatSurf();
    const bool closed = itemClosed(it.status);
    const float lineH = ImGui::GetTextLineHeight();
    const float h = lineH + kit::padY();
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float w = std::max(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    ImGui::PushID(idx);
    kit::rowBegin(it.id.empty() ? "todo" : it.id.c_str(), h, active);
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float cy = p0.y + h * 0.5f;
    float x = p0.x + kit::padX();
    const kit::Status status = it.status == "running" && !s_todo.running
                                  ? kit::Status::Skipped : chatRowStatus(it.status);
    kit::statusGlyph(dl, ImVec2(x + lineH * 0.5f, cy), glyphR(lineH), status, surf);
    x += lineH + ui::metrics::U(1.5f);
    const float right = p0.x + w - kit::padX();
    if (active) ImGui::PushFont(fonts.medium);
    const std::string title = kit::elide(it.title.c_str(), std::max(right - x, lineH));
    const ImVec2 ts = ImGui::CalcTextSize(title.c_str());
    const ImVec4 col = active ? kit::toneOn(kit::Tone::Accent, surf)
                     : closed ? surf.textTertiary
                              : surf.textSecondary;
    dl->AddText(ImVec2(x, cy - ts.y * 0.5f), ink(col), title.c_str());
    if (closed)   // struck through: done work stays legible but reads as finished
        dl->AddLine(ImVec2(x, cy), ImVec2(x + ts.x, cy), ink(surf.textTertiary),
                    ui::metrics::stroke(1.0f));
    if (active) ImGui::PopFont();
    kit::rowEnd();
    if (hovered && !it.desc.empty()) kit::tooltip(it.desc.c_str());
    else if (hovered && title != it.title) kit::tooltip(it.title.c_str());
    ImGui::PopID();
}

void drawTodos(ConvUi& cu, const ui::Fonts& fonts, bool first) {
    const Todo& t = s_todo;
    const int total = (int)t.items.size();
    char trailing[32];
    std::snprintf(trailing, sizeof(trailing), "%d/%d", t.done, total);
    kit::sectionHeader("Tasks", nullptr, trailing, -1, first);

    // Collapsed: a window that starts one row above the active item, so the done/next boundary
    // is always in view.
    int from = 0, to = total;
    if (!cu.todosAll && total > kTodoRows) {
        from = std::clamp(t.active - 1, 0, total - kTodoRows);
        to = from + kTodoRows;
    }
    for (int i = from; i < to; ++i) drawTodoRow(*t.items[(size_t)i], i, i == t.active, fonts);
    if (total > kTodoRows) moreRow("##todo_more", total - (to - from), &cu.todosAll);
}

// ---- sub-agent roster ------------------------------------------------------------------------
void drawStepLines(AppState& st, const std::string& convId, const AgentTraceAgent& a,
                   float indent) {
    agentTraceRecentSteps(st, convId, a.id, kInlineSteps, s_steps);
    const AdaptiveSurface& surf = chatSurf();
    const float lineH = ImGui::GetTextLineHeight();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    if (s_steps.empty()) {
        ImGui::Indent(indent);
        kit::dimText("No steps recorded yet.");
        ImGui::Unindent(indent);
        return;
    }
    const double now = chatNowEpoch();
    for (const AgentTraceStep& s : s_steps) {
        const ImVec2 p0 = ImGui::GetCursorScreenPos();
        const float w = std::max(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
        ImGui::Dummy(ImVec2(w, lineH));
        const bool hovered = ImGui::IsItemHovered();
        const float cy = p0.y + lineH * 0.5f;
        float x = p0.x + indent;
        kit::Status gs = kit::Status::Pending;
        if (stepStatus(s.status, &gs))
            kit::statusGlyph(dl, ImVec2(x + lineH * 0.4f, cy), glyphR(lineH) * 0.8f, gs, surf);
        x += lineH;
        float right = p0.x + w - kit::padX();
        std::string dur;
        if (s.status == "running" && s.ts > 0.0)  dur = elapsedText(now - s.ts);
        else if (s.durationMs >= 0)               dur = kit::fmtDuration(s.durationMs / 1000.0);
        if (!dur.empty()) {
            const float dw = ImGui::CalcTextSize(dur.c_str()).x;
            dl->AddText(ImVec2(right - dw, cy - lineH * 0.5f), ink(surf.textTertiary), dur.c_str());
            right -= dw + ui::metrics::U(2);
        }
        std::string label = s.kind;
        if (!s.title.empty()) label += " \xC2\xB7 " + s.title;
        const std::string head = kit::elide(label.c_str(), std::max(right - x, lineH));
        dl->AddText(ImVec2(x, cy - lineH * 0.5f), ink(surf.textSecondary), head.c_str());
        const float hx = x + ImGui::CalcTextSize(head.c_str()).x + ui::metrics::U(1.5f);
        if (!s.target.empty() && hx < right) {
            const std::string tg = kit::elide(s.target.c_str(), right - hx);
            dl->AddText(ImVec2(hx, cy - lineH * 0.5f), ink(surf.textTertiary), tg.c_str());
        }
        if (hovered) {
            const std::string& tip = !s.error.empty() ? s.error : !s.detail.empty() ? s.detail
                                                                                    : label;
            kit::tooltip(tip.c_str());
        }
    }
}

void drawAgentRow(AppState& st, const std::string& convId, ConvUi& cu,
                  const AgentTraceAgent& a, const ui::Fonts& fonts, double now) {
    const AdaptiveSurface& surf = chatSurf();
    const float lineH = ImGui::GetTextLineHeight();
    const bool live = a.status == "running";
    const bool twoLines = !a.lastStep.empty() && (live || a.status == "failed");
    const float lineGap = ui::metrics::U(0.5f);
    const float h = lineH * (twoLines ? 2.0f : 1.0f) + (twoLines ? lineGap : 0.0f) + kit::padY();
    const bool inspecting = cu.inspect == a.id;
    const ImVec2 p0 = ImGui::GetCursorScreenPos();
    const float w = std::max(ImGui::GetContentRegionAvail().x, ui::metrics::stroke(1.0f));
    const float indent = ui::metrics::U(3) * (float)std::max(0, a.depth - 1);

    const bool clicked = kit::rowBegin(a.id.c_str(), h, inspecting);
    const bool hovered = ImGui::IsItemHovered();
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float top = p0.y + kit::padY() * 0.5f;
    const float cy = top + lineH * 0.5f;
    float x = p0.x + kit::padX() + indent;
    kit::statusGlyph(dl, ImVec2(x + lineH * 0.5f, cy), glyphR(lineH), agentStatus(a.status), surf);
    x += lineH + ui::metrics::U(1.5f);
    const float textX = x;

    // Priority on the first line: name > elapsed > tool count > brief. Right-aligned numbers
    // first (elapsed ticks while the agent runs), then the name takes what it needs and the dim
    // brief only gets what is left.
    float right = p0.x + w - kit::padX();
    const float gap = ui::metrics::U(1.5f);
    const double secs = agentElapsed(a, now);
    if (secs >= 0.0 || a.status == "stopped") {
        const std::string el = a.status == "stopped" ? "Stopped" : elapsedText(secs);
        const float ew = ImGui::CalcTextSize(el.c_str()).x;
        dl->AddText(ImVec2(right - ew, top), ink(live ? surf.textSecondary : surf.textTertiary),
                    el.c_str());
        right -= ew + ui::metrics::U(2);
    }
    ImGui::PushFont(fonts.medium);
    const float nameW = ImGui::CalcTextSize(a.name.c_str()).x;
    ImGui::PopFont();
    if (a.toolCount > 0) {
        char tools[32];
        std::snprintf(tools, sizeof(tools), "%d tool%s", a.toolCount, a.toolCount == 1 ? "" : "s");
        const float tw = ImGui::CalcTextSize(tools).x;
        if (right - tw - ui::metrics::U(2) >= x + nameW) {   // only when the name still fits whole
            dl->AddText(ImVec2(right - tw, top),
                        ink(a.failCount > 0 ? kit::toneOn(kit::Tone::Err, surf) : surf.textTertiary),
                        tools);
            right -= tw + ui::metrics::U(2);
        }
    }

    ImGui::PushFont(fonts.medium);
    const std::string name = kit::elide(a.name.c_str(), std::max(right - x, lineH));
    dl->AddText(ImVec2(x, top), ink(surf.textPrimary), name.c_str());
    x += ImGui::CalcTextSize(name.c_str()).x + gap;
    ImGui::PopFont();
    if (!a.brief.empty() && right - x >= lineH * 2.0f) {   // a brief stub shorter than this is noise
        const size_t nl = a.brief.find('\n');
        const std::string brief = kit::elide(
            (nl == std::string::npos ? a.brief : a.brief.substr(0, nl)).c_str(), right - x);
        dl->AddText(ImVec2(x, top), ink(surf.textTertiary), brief.c_str());
    }
    if (twoLines) {
        const std::string step = kit::elide(a.lastStep.c_str(), std::max(p0.x + w - kit::padX() -
                                                                         textX, lineH));
        dl->AddText(ImVec2(textX, top + lineH + lineGap),
                    ink(a.status == "failed" ? kit::toneOn(kit::Tone::Err, surf)
                                             : surf.textSecondary),
                    step.c_str());
    }
    kit::rowEnd();

    if (hovered) {
        std::string tip = a.name;
        if (!a.brief.empty()) tip += "\n" + a.brief;
        tip += "\nStatus: " + a.status;
        if (a.toolCount > 0) {
            tip += "\n" + std::to_string(a.toolCount) + (a.toolCount == 1 ? " tool call" : " tool calls");
            if (a.failCount > 0) tip += " (" + std::to_string(a.failCount) + " failed)";
        }
        if (!a.model.empty()) tip += "\nModel: " + a.model;
        if (!a.note.empty())  tip += "\n" + a.note;
        tip += inspecting ? "\nClick to hide its recent steps." : "\nClick to show its recent steps.";
        kit::tooltip(tip.c_str());
    }
    if (clicked) {
        cu.inspect = inspecting ? std::string() : a.id;
        // Keep the AI workspace's trace on the same agent the user is looking at here.
        if (!inspecting) agentTraceSelectAgent(st, convId, a.id);
    }
    if (cu.inspect == a.id) drawStepLines(st, convId, a, kit::padX() + indent + lineH);
}

void drawAgents(AppState& st, const std::string& convId, ConvUi& cu, const ui::Fonts& fonts,
                int running, bool first) {
    char trailing[64];
    if (running > 0) std::snprintf(trailing, sizeof(trailing), "%d running", running);
    else             trailing[0] = '\0';
    kit::sectionHeader("Agents", nullptr, trailing[0] ? trailing : nullptr,
                       (int)s_agents.size(), first);
    const int total = (int)s_agents.size();
    // Collapsed: kAgentRows rows, live agents first claim, the rest fill from the top — always
    // drawn in tree order so a child stays under its parent.
    static std::vector<char> keep;
    keep.assign((size_t)total, 1);
    int shown = total;
    if (!cu.agentsAll && total > kAgentRows) {
        std::fill(keep.begin(), keep.end(), 0);
        shown = 0;
        for (int i = 0; i < total && shown < kAgentRows; ++i)
            if (s_agents[(size_t)i].status == "running") { keep[(size_t)i] = 1; ++shown; }
        for (int i = 0; i < total && shown < kAgentRows; ++i)
            if (!keep[(size_t)i]) { keep[(size_t)i] = 1; ++shown; }
    }
    const double now = chatNowEpoch();
    for (int i = 0; i < total; ++i)
        if (keep[(size_t)i]) drawAgentRow(st, convId, cu, s_agents[(size_t)i], fonts, now);
    if (total > kAgentRows) moreRow("##agent_more", total - shown, &cu.agentsAll);
}

}  // namespace

void drawAgentOverview(AppState& st, float width) {
    const std::string convId = st.activeConvId();
    if (convId.empty() || width <= 0.0f) return;
    const bool running = st.convRunning(convId);

    agentTraceSubagents(st, convId, s_agents);
    findTodo(st, s_todo);
    const bool todoOpen = s_todo.plan && (running || s_todo.done < (int)s_todo.items.size());
    if (s_agents.empty() && !todoOpen) return;

    ConvUi& cu = s_ui[convId];
    if (running && !cu.wasRunning) cu.openOverride = -1;   // a new run re-opens the block
    cu.wasRunning = running;

    int nRun = 0, nDone = 0, nFail = 0;
    double lastEnd = 0.0;
    for (const AgentTraceAgent& a : s_agents) {
        if (a.status == "running") ++nRun;
        else if (a.status == "done") ++nDone;
        else if (a.status == "failed" || a.status == "error") ++nFail;
        lastEnd = std::max(lastEnd, std::max(a.endTs, a.lastStepTs));
    }

    bool open = cu.openOverride >= 0 ? cu.openOverride == 1 : running;

    // Card title: the whole state on one line, so the collapsed block is still a summary.
    std::string title;
    char buf[96];
    if (!s_agents.empty()) {
        std::snprintf(buf, sizeof(buf), "%d agent%s", (int)s_agents.size(),
                      s_agents.size() == 1 ? "" : "s");
        title = buf;
        if (nRun > 0 && !todoOpen) {
            std::snprintf(buf, sizeof(buf), " \xC2\xB7 %d running", nRun);
            title += buf;
        }
        if (nRun == 0 && nFail == 0 && nDone == (int)s_agents.size()) title += " \xC2\xB7 done";
    }
    if (todoOpen) {
        std::snprintf(buf, sizeof(buf), "%s%d/%d tasks", title.empty() ? "" : " \xC2\xB7 ",
                      s_todo.done, (int)s_todo.items.size());
        title += buf;
        // The active task belongs in the body, not in a title that became one long ellipsis.
    }

    // Header right: the run's ticking elapsed while live, else how long ago the agents finished.
    std::string meta;
    if (running) {
        for (auto it = st.chat.rbegin(); it != st.chat.rend(); ++it)
            if (!it->fromUser && it->running && it->ts > 0.0) {
                meta = elapsedText(chatNowEpoch() - it->ts);
                break;
            }
    } else if (lastEnd > 0.0) {
        meta = kit::fmtAgo(lastEnd);
    }

    const ui::Fonts& fonts = st.fonts;
    // The block never takes more than kMaxHeightFrac of the host column (the window this is
    // drawn into): past that its body scrolls, so the composer below always stays in view.
    const float hostH = ImGui::GetWindowHeight();
    ImGui::PushID("agent_overview");
    kit::pushDensity(kit::Density::Compact);
    ImGui::BeginChild("##agent_overview_host", ImVec2(width, 0.0f),
                      ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse |
                          ImGuiWindowFlags_NoBackground);
    ImGui::PushFont(fonts.body);
    // Pre-elide the title against the header's right meta so the two never overlap (the card
    // elides only against its own right edge).
    const float lineH = ImGui::GetTextLineHeight();
    const float metaW = meta.empty() ? 0.0f
                      : ImGui::CalcTextSize(meta.c_str()).x +
                            (running ? lineH + ui::metrics::U(1) : 0.0f);
    const float titleW = width - kit::padX() * 2.0f - lineH - ui::metrics::U(1.5f) - metaW -
                         (metaW > 0.0f ? ui::metrics::U(2) : 0.0f);
    title = kit::elide(title.c_str(), std::max(titleW, lineH));
    const bool wasOpen = open;
    const bool body = kit::cardBegin("##agent_overview_card", title.c_str(), nullptr, &open);
    if (open != wasOpen) cu.openOverride = open ? 1 : 0;
    if (!meta.empty()) {
        kit::cardHeaderRight(metaW);
        if (running) {
            kit::statusGlyphInline(kit::Status::Running);
            ImGui::SameLine(0.0f, ui::metrics::U(1));
        }
        ImGui::PushStyleColor(ImGuiCol_Text, chatSurf().textTertiary);
        ImGui::TextUnformatted(meta.c_str());
        ImGui::PopStyleColor();
    }
    if (body) {
        // Header (title row + head gap + card padding) stays pinned; only the body scrolls.
        const float headerBlock = lineH + kit::headGap() + kit::padY() * 2.0f + ui::metrics::U(1);
        const float bodyMax = std::max(hostH * kMaxHeightFrac - headerBlock, lineH * 3.0f);
        ImGui::SetNextWindowSizeConstraints(ImVec2(0.0f, 0.0f), ImVec2(FLT_MAX, bodyMax));
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                            ImVec2(kit::padX() + ui::metrics::U(2), kit::padY()));
        if (ImGui::BeginChild("##agent_overview_body", ImVec2(0.0f, 0.0f),
                              ImGuiChildFlags_AutoResizeY | ImGuiChildFlags_AlwaysAutoResize |
                                  ImGuiChildFlags_AlwaysUseWindowPadding,
                              ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoBackground)) {
            // The roster is the point of this overview: never bury it below a long checklist.
            bool first = true;
            if (!s_agents.empty()) {
                drawAgents(st, convId, cu, fonts, nRun, first);
                first = false;
            }
            if (todoOpen) drawTodos(cu, fonts, first);
            drawWindowPillScrollbarV(chatSurf());
        }
        ImGui::EndChild();
        ImGui::PopStyleVar();
    }
    kit::cardEnd();
    ImGui::PopFont();
    ImGui::EndChild();
    kit::popDensity();
    ImGui::PopID();
    ImGui::Dummy(ImVec2(0.0f, ui::metrics::U(1)));
}

}  // namespace ui
