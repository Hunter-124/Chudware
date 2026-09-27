#include "current_run_view.h"
#include <algorithm>
#include <cstdio>
#include <ctime>
#include <string>
#include <vector>
#include "imgui.h"
#include "imgui_internal.h"
#include "core/app_state.h"
#include "ui/copilot/copilot_internal.h"
#include "ui/copilot/copilot.h"
#include "ui/ai/ai_prefs.h"
#include "ui/ai/agent_dag.h"
#include "ui/metrics.h"
#include "ui/widgets/kit.h"
#include "ui_theme.h"
namespace ui {
namespace {
// Project-scoped Current Run roster and one docked inspector. Transcript and controls
// use the existing asynchronous worker RPCs; steering takes effect at turn boundaries.

struct SubMsg {
    std::string who;    // "user" | "subagent" | "system"
    std::string text;
    bool        queued = false;   // a steer mirrored into the transcript, not yet consumed
};

struct SubagentSession {
    std::string id, role, brief, status, statusNote, model, spawnKind;
    std::string createdTs, updatedTs;   // ISO-8601 UTC (registry created_ts / updated_ts)
    std::string modelOverride, providerOverride, effortOverride;
    int budgetTokens = 0, pendingSteers = 0, transcriptLen = 0;
    bool paused = false;
};

struct SubagentRegistry {
    std::vector<SubagentSession> sessions;
    std::string projectKey;
    unsigned long long generation = 0;
    bool listPending = false;
    bool listedOnce = false;
    double lastListT = 0.0;
    std::string listErr;
    bool recentExpanded = false;
};
static SubagentRegistry s_sub;

struct SubChatState {
    SubagentSession cur;
    std::vector<SubMsg> msgs;
    bool   trPending = false;
    double lastTrT   = 0.0;
    std::string viewErr;

    // steering + turn-boundary runtime controls
    char        steerBuf[2048] = {0};
    char        modelBuf[256]   = {0};
    int         effortIdx       = 0;
    bool        runtimeSeeded   = false;
    bool        steerPending    = false;
    bool        ctlPending      = false;
    std::string steerNote;      // the worker's honest turn-boundary semantics note

    bool termPending = false;
    bool confirmTerminate = false;
};
static SubChatState s_inspector;

static bool isLive(const SubagentSession& s) {
    return s.status == "running" || s.status == "pending" ||
           s.status == "in_progress" || s.status == "waiting";
}

static kit::Tone statusTone(const SubagentSession& s) {
    if (s.status == "abandoned") return kit::Tone::Neutral;
    if (s.status == "failed" || s.status == "error") return kit::Tone::Err;
    if (s.status == "done" || s.status == "complete" || s.status == "succeeded")
        return kit::Tone::Ok;
    return isLive(s) ? kit::Tone::Accent : kit::Tone::Neutral;
}

// "13m ago" from an ISO-8601 UTC timestamp (registry _now(): timespec=milliseconds).
static std::string fmtAge(const std::string& iso) {
    if (iso.size() < 19) return std::string();
    int Y, M, D, h, m, s;
    if (std::sscanf(iso.c_str(), "%d-%d-%dT%d:%d:%d", &Y, &M, &D, &h, &m, &s) != 6)
        return std::string();
    std::tm tm{};
    tm.tm_year = Y - 1900; tm.tm_mon = M - 1; tm.tm_mday = D;
    tm.tm_hour = h; tm.tm_min = m; tm.tm_sec = s;
#ifdef _WIN32
    const time_t tp = _mkgmtime(&tm);
#else
    const time_t tp = timegm(&tm);
#endif
    if (tp < 0) return std::string();
    long long age = (long long)std::time(nullptr) - (long long)tp;
    if (age < 0) age = 0;
    char buf[32];
    if (age < 60)        std::snprintf(buf, sizeof(buf), "%lds ago", (long)age);
    else if (age < 3600) std::snprintf(buf, sizeof(buf), "%ldm ago", (long)(age / 60));
    else if (age < 86400) std::snprintf(buf, sizeof(buf), "%ldh ago", (long)(age / 3600));
    else                 std::snprintf(buf, sizeof(buf), "%ldd ago", (long)(age / 86400));
    return buf;
}

static SubagentSession parseSubSession(const nlohmann::json& s) {
    SubagentSession o;
    o.id            = s.value("id", "");
    o.role          = s.value("role", "");
    o.brief         = s.value("brief", "");
    o.status        = s.value("status", "");
    o.statusNote    = s.value("status_note", "");
    o.model         = s.value("model", "");
    o.spawnKind     = s.value("spawn_kind", "");
    o.createdTs     = s.value("created_ts", "");
    o.updatedTs     = s.value("updated_ts", "");
    o.budgetTokens  = s.value("budget_tokens", 0);
    o.pendingSteers = s.value("pending_steers", 0);
    o.transcriptLen = s.value("transcript_len", 0);
    o.paused          = s.value("paused", false);
    o.modelOverride   = s.value("model_override", "");
    o.providerOverride = s.value("provider_override", "");
    o.effortOverride  = s.value("effort_override", "");
    return o;
}

// A project change invalidates both the registry snapshot and all callbacks already in flight.
static void pollSubagentList(AppState& st) {
    const std::string project = convProjectDir(st);
    const std::string key = convProjectKey(project);
    if (key != s_sub.projectKey) {
        s_sub.projectKey = key;
        ++s_sub.generation;
        s_sub.sessions.clear();
        s_sub.listPending = false;
        s_sub.listedOnce = false;
        s_sub.listErr.clear();
        s_sub.recentExpanded = false;
        s_inspector = SubChatState{};
    }
    if (key.empty() || !st.workerStarted || s_sub.listPending) return;
    const double now = ImGui::GetTime();
    if (s_sub.listedOnce && now - s_sub.lastListT < 4.0) return;
    s_sub.listPending = true;
    s_sub.lastListT = now;
    const auto generation = s_sub.generation;
    st.rpc.call("subagent_list", {{"project_dir", project}},
                [key, generation](const core::RpcResult& r) {
        if (generation != s_sub.generation || key != s_sub.projectKey) return;
        s_sub.listPending = false;
        s_sub.listedOnce = true;
        if (!r.ok) { s_sub.listErr = r.err.empty() ? "worker unreachable" : r.err; return; }
        if (!r.result.value("ok", false)) {
            s_sub.listErr = r.result.value("reason", std::string("subagent_list failed"));
            return;
        }
        s_sub.listErr.clear();
        s_sub.sessions.clear();
        if (r.result.contains("sessions") && r.result["sessions"].is_array())
            for (const auto& s : r.result["sessions"])
                if (s.is_object()) s_sub.sessions.push_back(parseSubSession(s));
        if (!s_inspector.cur.id.empty()) {
            auto it = std::find_if(s_sub.sessions.begin(), s_sub.sessions.end(),
                [](const SubagentSession& s) { return s.id == s_inspector.cur.id; });
            if (it == s_sub.sessions.end()) s_inspector = SubChatState{};
            else s_inspector.cur = *it;
        }
    });
}

// Refresh only the selected inspector; a late response cannot retarget another project/agent.
static void fetchSubagentTranscript(AppState& st, SubChatState& cs, bool force = false) {
    if (!st.workerStarted || cs.trPending) return;
    const double now = ImGui::GetTime();
    if (!force && now - cs.lastTrT < 2.0) return;
    cs.trPending = true;
    cs.lastTrT   = now;
    nlohmann::json p;
    p["subagent_id"] = cs.cur.id;
    const std::string want = cs.cur.id;
    const auto generation = s_sub.generation;
    st.rpc.call("subagent_transcript", p, [want, generation](const core::RpcResult& r) {
        if (generation != s_sub.generation || s_inspector.cur.id != want) return;
        SubChatState& cs = s_inspector;
        cs.trPending = false;
        if (!r.ok) { cs.viewErr = r.err.empty() ? "worker unreachable" : r.err; return; }
        if (!r.result.value("ok", false)) {
            cs.viewErr = r.result.value("reason", std::string("transcript unavailable"));
            return;
        }
        cs.viewErr.clear();
        if (r.result.contains("session") && r.result["session"].is_object()) {
            const auto& session = r.result["session"];
            if (session.value("id", std::string()) != want) return;
            cs.cur = parseSubSession(session);
        }
        cs.cur.pendingSteers = r.result.contains("pending_steers") &&
                               r.result["pending_steers"].is_array()
                                   ? (int)r.result["pending_steers"].size() : 0;
        cs.msgs.clear();
        if (r.result.contains("messages") && r.result["messages"].is_array())
            for (const auto& m : r.result["messages"]) {
                if (!m.is_object()) continue;
                SubMsg sm;
                sm.who    = m.value("who", "");
                sm.text   = m.value("text", "");
                sm.queued = m.value("queued", false);
                cs.msgs.push_back(std::move(sm));
            }
    });
}
static void applySubagentControlResult(SubagentSession& s, const nlohmann::json& result) {
    s.status           = result.value("session_status", s.status);
    s.paused           = result.value("paused", s.paused);
    s.modelOverride    = result.value("model_override", s.modelOverride);
    s.providerOverride = result.value("provider_override", s.providerOverride);
    s.effortOverride   = result.value("effort_override", s.effortOverride);
}

static void sendSubagentControl(AppState& st, SubChatState& cs, const char* op,
                                const nlohmann::json& extra = nlohmann::json::object()) {
    if (!st.workerStarted || cs.ctlPending || !isLive(cs.cur)) return;
    cs.ctlPending = true;
    nlohmann::json p = extra;
    p["subagent_id"] = cs.cur.id;
    p["op"] = op;
    const std::string want = cs.cur.id;
    const auto generation = s_sub.generation;
    st.rpc.call("subagent_control", std::move(p),
                [&st, want, generation](const core::RpcResult& r) {
        if (generation != s_sub.generation || s_inspector.cur.id != want) return;
        SubChatState& cs = s_inspector;
        cs.ctlPending = false;
        if (!r.ok || !r.result.value("ok", false)) {
            cs.steerNote = r.ok ? r.result.value("reason", std::string("sub-agent control failed"))
                                : (r.err.empty() ? "worker unreachable" : r.err);
            return;
        }
        cs.steerNote = r.result.value("note", std::string("applied at the next turn boundary"));
        applySubagentControlResult(cs.cur, r.result);
        for (auto& session : s_sub.sessions)
            if (session.id == want) { applySubagentControlResult(session, r.result); break; }
        cs.runtimeSeeded = false;
        cs.lastTrT = 0.0;
        fetchSubagentTranscript(st, cs, true);
        s_sub.lastListT = 0.0;
    });
}


static void sendSubagentSteer(AppState& st, SubChatState& cs) {
    if (!st.workerStarted || cs.steerPending || !isLive(cs.cur)) return;
    std::string text(cs.steerBuf);
    if (text.find_first_not_of(" \t\r\n") == std::string::npos) return;
    cs.steerPending = true;
    nlohmann::json p;
    p["subagent_id"] = cs.cur.id;
    p["text"]        = text;
    const std::string want = cs.cur.id;
    const auto generation = s_sub.generation;
    st.rpc.call("subagent_steer", p, [want, generation](const core::RpcResult& r) {
        if (generation != s_sub.generation || s_inspector.cur.id != want) return;
        SubChatState& cs = s_inspector;
        cs.steerPending = false;
        if (!r.ok) { cs.steerNote = r.err.empty() ? "worker unreachable" : r.err; return; }
        if (!r.result.value("ok", false)) {
            cs.steerNote = r.result.value("reason", std::string("steer failed"));
            return;
        }
        cs.steerNote   = r.result.value("note", std::string("queued"));
        cs.steerBuf[0] = '\0';
        cs.lastTrT     = 0.0;   // refresh the transcript promptly (shows the queued steer)
    });
}

static void requestSubagentTerminate(AppState& st, SubChatState& cs) {
    if (!st.workerStarted || cs.termPending || !isLive(cs.cur)) return;
    cs.termPending = true;
    nlohmann::json p;
    p["subagent_id"] = cs.cur.id;
    p["confirm"] = true;   // explicit inline confirmation in the inspector
    const std::string want = cs.cur.id;
    const auto generation = s_sub.generation;
    st.rpc.call("subagent_terminate", p, [want, generation](const core::RpcResult& r) {
        if (generation != s_sub.generation || s_inspector.cur.id != want) return;
        SubChatState& cs = s_inspector;
        cs.termPending = false;
        if (r.ok && r.result.value("ok", false))
            cs.steerNote = r.result.value(
                "note", std::string("termination requested — honored at the next turn boundary"));
        else
            cs.steerNote = r.ok ? r.result.value("reason", std::string("terminate failed"))
                                : (r.err.empty() ? "worker unreachable" : r.err);
        cs.lastTrT = 0.0;
    });
}

// A graph node is selectable only if its agent belongs to this project's registry.
static void selectSubagent(AppState& st, const std::string& id, const std::string&) {
    const auto it = std::find_if(s_sub.sessions.begin(), s_sub.sessions.end(),
                                 [&id](const SubagentSession& s) { return s.id == id; });
    if (it == s_sub.sessions.end()) return;
    if (s_inspector.cur.id != id) s_inspector = SubChatState{};
    s_inspector.cur = *it;
    fetchSubagentTranscript(st, s_inspector, true);
}

static void drawRoster(AppState& st, float height) {
    ImGui::BeginChild("##current_roster", ImVec2(0, height),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    const AdaptiveSurface& surf = adaptivePanel(panelId("ai_ws_current_roster"),
        ImRect(ImGui::GetWindowPos(), ImVec2(ImGui::GetWindowPos().x + ImGui::GetWindowSize().x,
                                            ImGui::GetWindowPos().y + ImGui::GetWindowSize().y)),
        GlassGroup::Global);
    int live = 0;
    for (const auto& s : s_sub.sessions) live += isLive(s) ? 1 : 0;
    kit::sectionHeader("Live", nullptr, nullptr, live, true);
    if (live == 0) kit::dimText("No active sub-agents for this project.");

    auto drawRow = [&st](const SubagentSession& s) {
        const std::string secondary = s.status +
            (!s.statusNote.empty() ? " · " + s.statusNote :
             s.brief.empty() ? "" : " · " + s.brief);
        std::string meta = s.model;
        const std::string age = fmtAge(s.updatedTs);
        if (!age.empty()) { if (!meta.empty()) meta += " · "; meta += age; }
        kit::RowStyle style;
        style.secondary = secondary.c_str();
        style.meta = meta.c_str();
        style.selected = s_inspector.cur.id == s.id;
        style.tooltip = s.brief.empty() ? nullptr : s.brief.c_str();
        ImGui::PushID(s.id.c_str());
        if (kit::row("##agent", s.role.empty() ? s.id.c_str() : s.role.c_str(), style))
            selectSubagent(st, s.id, s.role);
        ImGui::PopID();
    };
    for (const auto& s : s_sub.sessions) if (isLive(s)) drawRow(s);
    const int recent = static_cast<int>(s_sub.sessions.size()) - live;
    if (recent) {
        kit::sectionHeader("Recent", nullptr, nullptr, recent);
        if (kit::secondaryButton(s_sub.recentExpanded ? "Hide recent" : "Show recent"))
            s_sub.recentExpanded = !s_sub.recentExpanded;
        if (s_sub.recentExpanded)
            for (const auto& s : s_sub.sessions) if (!isLive(s)) drawRow(s);
    }
    if (!s_sub.listErr.empty()) kit::inlineStatus(s_sub.listErr.c_str(), kit::Tone::Err);
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
}

static void drawInspector(AppState& st) {
    ImGui::BeginChild("##current_inspector", ImVec2(0, 0),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    const AdaptiveSurface& surf = adaptivePanel(panelId("ai_ws_current_inspector"),
        ImRect(ImGui::GetWindowPos(), ImVec2(ImGui::GetWindowPos().x + ImGui::GetWindowSize().x,
                                            ImGui::GetWindowPos().y + ImGui::GetWindowSize().y)),
        GlassGroup::Global);
    kit::sectionHeader("Selected agent", nullptr, nullptr, -1, true);
    SubChatState& cs = s_inspector;
    if (cs.cur.id.empty()) {
        kit::emptyState("ai_panel", "No agent selected",
                        "Choose a roster row or a sub-agent node in the execution trace.");
        drawWindowPillScrollbarV(surf);
        ImGui::EndChild();
        return;
    }
    fetchSubagentTranscript(st, cs);
    if (!cs.runtimeSeeded && !cs.trPending) {
        std::snprintf(cs.modelBuf, sizeof(cs.modelBuf), "%s", cs.cur.modelOverride.c_str());
        cs.effortIdx = cs.cur.effortOverride == "low" ? 1 :
                       cs.cur.effortOverride == "medium" ? 2 :
                       cs.cur.effortOverride == "high" ? 3 : 0;
        cs.runtimeSeeded = true;
    }
    kit::chip(cs.cur.status.empty() ? "unknown" : cs.cur.status.c_str(),
              statusTone(cs.cur), isLive(cs.cur));
    if (cs.cur.paused) {
        ImGui::SameLine();
        kit::chip("paused", kit::Tone::Warn);
    }
    kit::keyValueRow("Role", cs.cur.role.empty() ? "sub-agent" : cs.cur.role.c_str());
    kit::keyValueRow("ID", cs.cur.id.c_str(), kit::Tone::Neutral, true);
    kit::keyValueRow("Model", cs.cur.model.empty() ? "inherited" : cs.cur.model.c_str());
    const std::string budget = cs.cur.budgetTokens > 0
        ? std::to_string(cs.cur.budgetTokens) + " tokens" : "inherited";
    kit::keyValueRow("Budget", budget.c_str());
    kit::keyValueRow("Started", cs.cur.createdTs.empty() ? "unknown" : cs.cur.createdTs.c_str());
    if (!cs.cur.statusNote.empty()) kit::dimText(cs.cur.statusNote.c_str());
    if (!cs.cur.brief.empty()) {
        kit::sectionHeader("Prompt");
        kit::dimText(cs.cur.brief.c_str());
    }
    kit::sectionHeader("Transcript");
    const float logH = std::max(ui::metrics::px(120.0f),
        std::min(ui::metrics::px(260.0f), ImGui::GetContentRegionAvail().y * 0.45f));
    ImGui::BeginChild("##inspector_transcript", ImVec2(0, logH),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    const AdaptiveSurface& logSurf = adaptivePanel(panelId("ai_ws_inspector_transcript"),
        ImRect(ImGui::GetWindowPos(), ImVec2(ImGui::GetWindowPos().x + ImGui::GetWindowSize().x,
                                            ImGui::GetWindowPos().y + ImGui::GetWindowSize().y)),
        GlassGroup::Global);
    if (!cs.viewErr.empty()) kit::inlineStatus(cs.viewErr.c_str(), kit::Tone::Err);
    if (cs.msgs.empty() && cs.viewErr.empty())
        kit::emptyState("file_doc", cs.trPending ? "Loading transcript" : "No transcript yet",
                        "Sub-agent messages and queued steering appear here.");
    for (const auto& msg : cs.msgs) {
        kit::chip(msg.who == "user" ? "You" : msg.who == "system" ? "System" : "Agent",
                  msg.who == "user" ? kit::Tone::Accent : kit::Tone::Neutral);
        if (msg.queued) { ImGui::SameLine(); kit::chip("queued", kit::Tone::Warn); }
        ImGui::TextWrapped("%s", msg.text.c_str());
        kit::gapRow();
    }
    chatLogStickToBottom("aiws.inspector_transcript");
    drawWindowPillScrollbarV(logSurf);
    ImGui::EndChild();
    if (isLive(cs.cur)) {
        kit::sectionHeader("Steer & control");
        ImGui::InputTextMultiline("##agent_steer", cs.steerBuf, sizeof(cs.steerBuf),
                                  ImVec2(-1.0f, ImGui::GetFrameHeightWithSpacing() * 2.0f));
        if (kit::primaryButton(cs.steerPending ? "Queuing" : "Steer",
                               st.workerStarted && !cs.steerPending && cs.steerBuf[0] != '\0',
                               "Steering is queued for the next turn boundary"))
            sendSubagentSteer(st, cs);
        ImGui::SameLine();
        if (kit::secondaryButton(cs.cur.paused ? "Resume" : "Pause",
                                 st.workerStarted && !cs.ctlPending))
            sendSubagentControl(st, cs, cs.cur.paused ? "resume" : "pause");
        ImGui::SameLine();
        if (kit::dangerButton("Terminate", st.workerStarted && !cs.termPending))
            cs.confirmTerminate = true;
        if (cs.confirmTerminate) {
            kit::inlineStatus("Stop at the next turn boundary? Existing work is kept.",
                              kit::Tone::Warn);
            if (kit::dangerButton("Confirm termination")) {
                requestSubagentTerminate(st, cs);
                cs.confirmTerminate = false;
            }
            ImGui::SameLine();
            if (kit::secondaryButton("Keep running")) cs.confirmTerminate = false;
        }
        ImGui::SetNextItemWidth(ImGui::GetContentRegionAvail().x * 0.50f);
        ImGui::InputTextWithHint("##agent_model", "Model override", cs.modelBuf, sizeof(cs.modelBuf));
        ImGui::SameLine();
        static const char* efforts[] = {"Default", "low", "medium", "high"};
        ImGui::SetNextItemWidth(ui::metrics::px(94.0f));
        ImGui::Combo("##agent_effort", &cs.effortIdx, efforts, IM_ARRAYSIZE(efforts));
        ImGui::SameLine();
        if (kit::secondaryButton("Apply", st.workerStarted && !cs.ctlPending))
            sendSubagentControl(st, cs, "set_runtime", {
                {"model", std::string(cs.modelBuf)},
                {"effort", cs.effortIdx == 0 ? "" : efforts[cs.effortIdx]}});
        if (cs.cur.pendingSteers > 0)
            kit::inlineStatus("Steering queued for the next turn boundary", kit::Tone::Warn);
    }
    if (!cs.steerNote.empty()) kit::inlineStatus(cs.steerNote.c_str(), kit::Tone::Neutral);
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
}

}  // anonymous namespace

void pollCurrentRun(AppState& st) { pollSubagentList(st); }

int currentRunLiveCount() {
    int count = 0;
    for (const auto& session : s_sub.sessions) if (isLive(session)) ++count;
    return count;
}

void drawCurrentRunView(AppState& st) {
    pollSubagentList(st);
    kit::sectionHeader("Current Run", nullptr, nullptr, -1, true);
    if (s_sub.projectKey.empty()) {
        kit::emptyState("ai_panel", "No active project",
                        "Select a project folder to see its runs and sub-agents.");
        return;
    }
    if (!st.workerStarted && s_sub.sessions.empty()) {
        kit::blockedState("Worker unavailable", "Cannot load the agent roster",
                          "Start the Chudware worker service.");
        return;
    }
    const std::string convId = st.activeConvId();
    const auto dag = agentDagSummary(convId);
    const float available = ImGui::GetContentRegionAvail().y;
    const float graphH = std::max(ui::metrics::px(110.0f), available * 0.29f);
    ImGui::BeginChild("##current_graph", ImVec2(0, graphH),
                      ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
    kit::sectionHeader("Execution trace", nullptr, nullptr, -1, true);
    if (dag.empty)
        kit::emptyState("activity", st.convRunning(convId) ? "Agent is starting" : "No events yet",
                        "Tool calls, thoughts and context injections appear as this run streams.");
    else
        drawAgentDagCanvas(st, convId, aiUiVerbosity(), selectSubagent);
    ImGui::EndChild();
    kit::gapRow();
    const float remaining = ImGui::GetContentRegionAvail().y;
    const float rosterH = std::max(ui::metrics::px(90.0f), remaining * 0.33f);
    drawRoster(st, rosterH);
    kit::gapRow();
    drawInspector(st);
}
}  // namespace ui
