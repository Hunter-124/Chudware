#include "current_run_view.h"
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>
#include <string>
#include <vector>
#include <unordered_map>
#include "imgui.h"
#include "imgui_internal.h"
#include "core/app_state.h"
#include "ui/copilot/copilot_internal.h"
#include "ui/copilot/copilot.h"
#include "ui/ai/agent_dag.h"
#include "ui/metrics.h"
#include "ui/widgets/kit.h"
#include "ui_theme.h"
namespace ui {
namespace {
// Current Run roster and one docked inspector. The roster merges two sources: the project's
// sub-agent registry (subagent_list — sub-agents with transcript, steering and pause/terminate
// controls) and the viewed session's execution trace (every sub-agent the run's events name,
// including Chudcode `task` sub-agents that have no registry record; those are read-only here).
// Registry calls are asynchronous; steering takes effect at turn boundaries. A refusal because
// a run is holding the service keeps the last-known roster/transcript and never shows an error.

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

// A trace step opened in the docked inspector. It takes the inspector over until the user goes
// back to the agent (or selects another agent); the copy survives the step leaving the trace.
struct StepSelection {
    bool active = false;
    bool stale = false;
    AgentTraceStep step;
};
static StepSelection s_step;

// The viewed session's trace sub-agents (tree order), refreshed every frame by pollCurrentRun.
static std::vector<AgentTraceAgent> s_trace;
// Inspector selection is per conversation, just like the trace timeline's own selection.
static std::unordered_map<std::string, std::string> s_selectedByConv;
static std::string s_viewConv;
// "main" is a real inspector selection, not a registry sub-agent.
static std::string s_selId = "main";
static int s_liveCount = 0;
static bool s_resetInspectorScroll = false;

// Registry LIVE_STATUSES (subagent_registry.py): a paused or approved session is still live and
// must keep its Resume / Terminate controls.
static bool isLive(const SubagentSession& s) {
    return s.status == "running" || s.status == "pending" || s.status == "approved" ||
           s.status == "in_progress" || s.status == "waiting" || s.status == "paused";
}
static bool traceLive(const AgentTraceAgent& a) {
    return a.status == "running" || a.status == "pending";
}
static const SubagentSession* registryFor(const std::string& id) {
    for (const auto& s : s_sub.sessions) if (s.id == id) return &s;
    return nullptr;
}
static const AgentTraceAgent* traceFor(const std::string& id) {
    for (const auto& a : s_trace) if (a.id == id) return &a;
    return nullptr;
}

// One meaning→tone map for registry and trace statuses alike.
static kit::Tone toneFor(const std::string& status, bool live) {
    if (status == "abandoned" || status == "stopped" || status == "cancelled")
        return kit::Tone::Neutral;
    if (status == "failed" || status == "error") return kit::Tone::Err;
    if (status == "paused") return kit::Tone::Warn;
    if (status == "done" || status == "complete" || status == "succeeded") return kit::Tone::Ok;
    return live ? kit::Tone::Accent : kit::Tone::Neutral;
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
        s_step = StepSelection{};
        s_selectedByConv.clear();
        s_viewConv.clear();
        s_selId = "main";
        s_resetInspectorScroll = true;
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
        // A run holding the service: keep the last roster and retry on the normal cadence.
        if (core::workerLaneBusy(r)) return;
        if (!r.ok || !r.result.value("ok", false)) {
            s_sub.listErr = userFacingRpcError(r, "Sub-agents couldn't be listed right now.");
            return;
        }
        s_sub.listErr.clear();
        s_sub.sessions.clear();
        if (r.result.contains("sessions") && r.result["sessions"].is_array())
            for (const auto& s : r.result["sessions"])
                if (s.is_object()) s_sub.sessions.push_back(parseSubSession(s));
        if (s_selId == "main" || s_selId.empty()) return;
        const SubagentSession* reg = registryFor(s_selId);
        if (!reg) {
            if (!s_inspector.cur.id.empty()) s_inspector = SubChatState{};
        } else if (s_inspector.cur.id == s_selId) {
            s_inspector.cur = *reg;
        } else {   // a trace agent selected before the registry listed it gains its controls
            s_inspector = SubChatState{};
            s_inspector.cur = *reg;
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
        if (core::workerLaneBusy(r)) return;   // keep the transcript already shown
        if (!r.ok || !r.result.value("ok", false)) {
            cs.viewErr = userFacingRpcError(r, "The transcript isn't available right now.");
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
            cs.steerNote = userFacingRpcError(r, "That change couldn't be applied.");
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
        if (!r.ok || !r.result.value("ok", false)) {
            cs.steerNote = userFacingRpcError(r, "The steer couldn't be queued.");
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
            cs.steerNote = userFacingRpcError(r, "Termination couldn't be requested.");
        cs.lastTrT = 0.0;
    });
}

// Inspect one agent: its registry record (transcript + controls) when the registry knows it,
// else its trace state read-only. Main is the left-hand conversation, never a registry lookup.
static void inspectAgent(AppState& st, const std::string& id) {
    s_step.active = false;
    const bool changed = s_selId != id;
    const bool hadRegistry = s_inspector.cur.id == id;
    if (changed) { s_inspector = SubChatState{}; s_resetInspectorScroll = true; }
    s_selId = id;
    if (!st.activeConvId().empty()) s_selectedByConv[st.activeConvId()] = id;
    if (id == "main") return;
    const SubagentSession* reg = registryFor(id);
    if (!reg) { s_inspector = SubChatState{}; return; }
    s_inspector.cur = *reg;
    if (changed || !hadRegistry) fetchSubagentTranscript(st, s_inspector, true);
}

// Trace-tree click: the trace has already selected the agent's timeline.
static void selectSubagent(AppState& st, const std::string& id, const std::string&) {
    inspectAgent(st, id);
}

// Roster click: also show the agent's steps in the trace when they exist.
static void selectRosterAgent(AppState& st, const std::string& id) {
    if (!agentTraceSelectAgent(st, st.activeConvId(), id))
        agentTraceSelectAgent(st, st.activeConvId(), "main");
    inspectAgent(st, id);
}

static void selectTraceStep(AppState&, const AgentTraceStep& step) {
    s_step.active = true;
    s_resetInspectorScroll = true;
    s_step.stale = false;
    s_step.step = step;
}

// Session changes restore that session's own inspector/trace selection, never the last
// session's step or transcript. An agent removed from both sources falls back to main.
static void syncViewedConversation(AppState& st) {
    const std::string convId = st.activeConvId();
    if (s_viewConv == convId) return;
    s_viewConv = convId;
    s_step = StepSelection{};
    s_inspector = SubChatState{};
    s_resetInspectorScroll = true;
    const auto it = s_selectedByConv.find(convId);
    s_selId = it == s_selectedByConv.end() ? "main" : it->second;
    if (s_selId != "main" && !traceFor(s_selId) && !registryFor(s_selId))
        s_selId = "main";
    if (!agentTraceSelectAgent(st, convId, s_selId))
        agentTraceSelectAgent(st, convId, "main");
    if (s_selId != "main") {
        if (const SubagentSession* reg = registryFor(s_selId)) {
            s_inspector.cur = *reg;
            fetchSubagentTranscript(st, s_inspector, true);
        }
    }
    if (!convId.empty()) s_selectedByConv[convId] = s_selId;
}

static kit::Tone stepTone(const std::string& status) {
    if (status == "ok") return kit::Tone::Ok;
    if (status == "failed") return kit::Tone::Err;
    if (status == "retry") return kit::Tone::Warn;
    if (status == "running") return kit::Tone::Accent;
    return kit::Tone::Neutral;
}

static std::string stepDurationText(int ms) {
    if (ms < 0) return std::string();
    if (ms < 1000) return std::to_string(ms) + " ms";
    return kit::fmtDuration(ms / 1000.0);
}

// Step mode of the docked inspector: the full record of one trace step.
static void drawStepInspector(AppState& st, const AdaptiveSurface& surf) {
    AgentTraceStep& s = s_step.step;
    s_step.stale = !agentTraceRefreshStep(st, s);
    kit::sectionHeader("Selected step", nullptr, nullptr, -1, true);
    if (kit::secondaryButton(s_selId == "main" ? "Back to main agent" : "Back to selected agent")) {
        s_step.active = false;
        s_resetInspectorScroll = true;
    }
    kit::chip(s.kind.c_str(), kit::Tone::Info);
    if (!s.status.empty()) {
        ImGui::SameLine();
        kit::chip(s.status == "ok" ? "done" : s.status.c_str(), stepTone(s.status),
                  s.status == "running");
    }
    kit::keyValueRow("Agent", s.agent.c_str());
    if (!s.title.empty())
        kit::keyValueRow(s.kind == "Tool call" ? "Tool" : "Title", s.title.c_str());
    if (!s.target.empty()) kit::keyValueRow("Target", s.target.c_str());
    const std::string dur = stepDurationText(s.durationMs);
    if (!dur.empty()) kit::keyValueRow("Duration", dur.c_str());
    if (s.ts > 0.0) kit::keyValueRow("When", kit::fmtAgo(s.ts).c_str());
    if (s_step.stale)
        kit::inlineStatus("No longer in the live trace; showing the last copy.",
                          kit::Tone::Neutral);
    const bool failed = !s.error.empty() && s.status != "ok";
    if (failed) {
        kit::sectionHeader("Error");
        ImGui::PushStyleColor(ImGuiCol_Text, kit::toneOn(kit::Tone::Err, surf));
        ImGui::TextWrapped("%s", s.error.c_str());
        ImGui::PopStyleColor();
    }
    if (!s.detail.empty()) {
        kit::sectionHeader(s.kind == "Tool call" ? "Arguments" : "Content");
        ImGui::BeginChild("##step_detail", ImVec2(0, 0), ImGuiChildFlags_AlwaysUseWindowPadding,
                          ImGuiWindowFlags_NoScrollbar);
        const ImVec2 wp = ImGui::GetWindowPos(), ws = ImGui::GetWindowSize();
        const AdaptiveSurface& dSurf = adaptivePanel(panelId("ai_ws_step_detail"),
            ImRect(wp, ImVec2(wp.x + ws.x, wp.y + ws.y)), GlassGroup::Global);
        ImGui::TextWrapped("%s", s.detail.c_str());
        drawWindowPillScrollbarV(dSurf);
        ImGui::EndChild();
    } else if (!failed) {
        kit::dimText("This step carries no further detail.");
    }
}

// One roster row: a trace agent, a registry session, or both when they are the same sub-agent
// (registry sub-agents' events carry their registry id as `subagent_id`).
struct RosterEntry {
    const AgentTraceAgent* tr = nullptr;
    const SubagentSession* reg = nullptr;
    bool live = false;
};
static void rosterEntries(std::vector<RosterEntry>& out) {
    out.clear();
    for (const auto& a : s_trace) {
        RosterEntry e;
        e.tr = &a;
        e.reg = registryFor(a.id);
        // The registry's status is authoritative (pause holds); the trace covers the rest.
        e.live = e.reg ? isLive(*e.reg) : traceLive(a);
        out.push_back(e);
    }
    for (const auto& s : s_sub.sessions) {
        if (traceFor(s.id)) continue;
        RosterEntry e;
        e.reg = &s;
        e.live = isLive(s);
        out.push_back(e);
    }
}

static std::string entryStatus(const RosterEntry& e) {
    if (e.reg) return e.reg->paused && e.live ? std::string("paused") : e.reg->status;
    return e.tr->status;
}

// Wall time the trace saw the agent working: running agents count up to now.
static std::string traceElapsed(const AgentTraceAgent& a, bool live) {
    if (a.startTs <= 0.0) return std::string();
    const double end = live ? chatNowEpoch()
                     : a.endTs > 0.0 ? a.endTs
                     : a.lastStepTs > 0.0 ? a.lastStepTs : a.startTs;
    return kit::fmtDuration(std::max(0.0, end - a.startTs));
}

static std::string toolCountText(const AgentTraceAgent& a) {
    std::string s = std::to_string(a.toolCount) + (a.toolCount == 1 ? " tool" : " tools");
    if (a.failCount > 0) s += " (" + std::to_string(a.failCount) + " failed)";
    return s;
}

static void drawRoster(AppState& st, float height) {
    ImGui::BeginChild("##current_roster", ImVec2(0, height),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    const AdaptiveSurface& surf = adaptivePanel(panelId("ai_ws_current_roster"),
        ImRect(ImGui::GetWindowPos(), ImVec2(ImGui::GetWindowPos().x + ImGui::GetWindowSize().x,
                                            ImGui::GetWindowPos().y + ImGui::GetWindowSize().y)),
        GlassGroup::Global);
    static std::vector<RosterEntry> entries;
    rosterEntries(entries);
    int live = 0;
    for (const auto& e : entries) live += e.live ? 1 : 0;
    kit::sectionHeader("Live", nullptr, nullptr, live, true);
    if (!st.workerStarted && entries.empty())
        kit::inlineStatus("AI service offline. Sub-agents appear here once it is running.",
                          kit::Tone::Warn);
    else if (live == 0) kit::dimText("No sub-agents are running.");

    auto drawRow = [&st](const RosterEntry& e) {
        const std::string& id = e.tr ? e.tr->id : e.reg->id;
        const std::string title = e.tr ? e.tr->name
                                : e.reg->role.empty() ? std::string("Sub-agent") : e.reg->role;
        std::string detail;
        if (e.tr && !e.tr->lastStep.empty()) detail = e.tr->lastStep;
        else if (e.reg) detail = !e.reg->statusNote.empty() ? e.reg->statusNote : e.reg->brief;
        else detail = e.tr->brief;
        std::string secondary = entryStatus(e);
        if (!detail.empty()) secondary += " · " + detail;
        std::string meta;
        if (e.tr) {
            meta = traceElapsed(*e.tr, e.live);
            if (!meta.empty()) meta += " · ";
            meta += toolCountText(*e.tr);
        } else {
            meta = fmtAge(e.reg->updatedTs);
        }
        const std::string& brief = e.reg && !e.reg->brief.empty() ? e.reg->brief
                                 : e.tr ? e.tr->brief : e.reg->brief;
        kit::RowStyle style;
        style.secondary = secondary.c_str();
        style.meta = meta.c_str();
        style.selected = s_selId == id;
        style.tooltip = brief.empty() ? nullptr : brief.c_str();
        ImGui::PushID(id.c_str());
        if (kit::row("##agent", title.c_str(), style)) selectRosterAgent(st, id);
        ImGui::PopID();
    };
    for (const auto& e : entries) if (e.live) drawRow(e);
    const int recent = static_cast<int>(entries.size()) - live;
    if (recent) {
        kit::sectionHeader("Recent", nullptr, nullptr, recent);
        if (kit::secondaryButton(s_sub.recentExpanded ? "Hide recent" : "Show recent"))
            s_sub.recentExpanded = !s_sub.recentExpanded;
        if (s_sub.recentExpanded)
            for (const auto& e : entries) if (!e.live) drawRow(e);
    }
    if (!s_sub.listErr.empty()) kit::inlineStatus(s_sub.listErr.c_str(), kit::Tone::Warn);
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
}

// Trace facts for the inspected agent (both kinds): how long, how much, what it did last.
static void drawTraceFacts(const AgentTraceAgent& a, bool live) {
    const std::string elapsed = traceElapsed(a, live);
    if (!elapsed.empty()) kit::keyValueRow(live ? "Running for" : "Ran for", elapsed.c_str());
    kit::keyValueRow("Tool calls", toolCountText(a).c_str(),
                     a.failCount > 0 ? kit::Tone::Warn : kit::Tone::Neutral);
    if (!a.lastStep.empty()) {
        std::string last = a.lastStep;
        if (a.lastStepTs > 0.0) last += " · " + kit::fmtAgo(a.lastStepTs);
        kit::keyValueRow("Last step", last.c_str());
    }
}

// A sub-agent the trace knows but the registry does not (e.g. a Chudcode `task` sub-agent):
// its state only. Its steps are the trace's timeline for it.
static void drawTraceAgentInspector(const AgentTraceAgent& a) {
    const bool live = traceLive(a);
    kit::chip(a.status.c_str(), toneFor(a.status, live), live);
    kit::keyValueRow("Agent", a.name.c_str());
    if (!a.model.empty()) kit::keyValueRow("Model", a.model.c_str());
    if (a.startTs > 0.0) kit::keyValueRow("Started", kit::fmtAgo(a.startTs).c_str());
    drawTraceFacts(a, live);
    if (!a.note.empty()) kit::dimText(a.note.c_str());
    if (!a.brief.empty() && a.brief != a.name) {
        kit::sectionHeader("Task");
        kit::dimText(a.brief.c_str());
    }
    kit::gapRow();
    kit::dimText("Its steps are shown in the execution trace. This sub-agent is directed by the "
                 "main agent, so it has no steering controls here.");
}

static void drawMainAgentInspector(AppState& st) {
    const bool running = st.convRunning(st.activeConvId());
    kit::chip(running ? "running" : "main chat", running ? kit::Tone::Accent : kit::Tone::Neutral,
              running);
    kit::keyValueRow("Agent", "Main agent");
    const std::vector<ChatMsg>* msgs = conversationMsgs(st, st.activeConvId());
    if (msgs) {
        char count[32];
        std::snprintf(count, sizeof(count), "%zu messages", msgs->size());
        kit::keyValueRow("Conversation", count);
        for (auto it = msgs->rbegin(); it != msgs->rend(); ++it) {
            if (!it->fromUser || it->text.empty()) continue;
            kit::sectionHeader("Latest request");
            ImGui::TextWrapped("%.*s%s", (int)std::min<size_t>(it->text.size(), 260),
                               it->text.c_str(), it->text.size() > 260 ? "..." : "");
            break;
        }
    }
    kit::gapRow();
    kit::dimText("The full main-agent conversation and message composer are in the left chat "
                 "pane. Its steps are shown in the execution trace.");
}

static void drawInspector(AppState& st) {
    ImGui::BeginChild("##current_inspector", ImVec2(0, 0),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    if (s_resetInspectorScroll) {
        ImGui::SetScrollY(0.0f);
        s_resetInspectorScroll = false;
    }
    const AdaptiveSurface& surf = adaptivePanel(panelId("ai_ws_current_inspector"),
        ImRect(ImGui::GetWindowPos(), ImVec2(ImGui::GetWindowPos().x + ImGui::GetWindowSize().x,
                                            ImGui::GetWindowPos().y + ImGui::GetWindowSize().y)),
        GlassGroup::Global);
    if (s_step.active && s_step.step.convId != st.activeConvId()) s_step.active = false;
    if (s_step.active) {
        drawStepInspector(st, surf);
        drawWindowPillScrollbarV(surf);
        ImGui::EndChild();
        return;
    }
    if (s_selId != "main" && s_inspector.cur.id.empty() && !traceFor(s_selId)) {
        agentTraceSelectAgent(st, st.activeConvId(), "main");
        inspectAgent(st, "main");
    }
    kit::sectionHeader("Selected agent", nullptr, nullptr, -1, true);
    if (s_selId != "main" && kit::secondaryButton("Back to main agent")) {
        agentTraceSelectAgent(st, st.activeConvId(), "main");
        inspectAgent(st, "main");
    }
    if (s_selId == "main") {
        drawMainAgentInspector(st);
        drawWindowPillScrollbarV(surf);
        ImGui::EndChild();
        return;
    }
    SubChatState& cs = s_inspector;
    const AgentTraceAgent* tr = traceFor(s_selId);
    if (cs.cur.id.empty()) {
        drawTraceAgentInspector(*tr);
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
              toneFor(cs.cur.status, isLive(cs.cur)), isLive(cs.cur));
    if (cs.cur.paused && cs.cur.status != "paused") {
        ImGui::SameLine();
        kit::chip("paused", kit::Tone::Warn);
    }
    if (tr) kit::keyValueRow("Agent", tr->name.c_str());
    else kit::inlineStatus("No steps recorded for this sub-agent in the selected session; "
                           "the trace remains on the main agent.", kit::Tone::Neutral);
    kit::keyValueRow("Role", cs.cur.role.empty() ? "sub-agent" : cs.cur.role.c_str());
    kit::keyValueRow("Model", cs.cur.model.empty() ? "inherited" : cs.cur.model.c_str());
    const std::string budget = cs.cur.budgetTokens > 0
        ? std::to_string(cs.cur.budgetTokens) + " tokens" : "inherited";
    kit::keyValueRow("Budget", budget.c_str());
    const std::string started = fmtAge(cs.cur.createdTs);
    kit::keyValueRow("Started", started.empty() ? "unknown" : started.c_str());
    if (tr) drawTraceFacts(*tr, isLive(cs.cur));
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
    if (!cs.viewErr.empty()) kit::inlineStatus(cs.viewErr.c_str(), kit::Tone::Warn);
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
        const char* offline = st.workerStarted ? nullptr : "AI service offline";
        ImGui::InputTextMultiline("##agent_steer", cs.steerBuf, sizeof(cs.steerBuf),
                                  ImVec2(-1.0f, ImGui::GetFrameHeightWithSpacing() * 2.0f));
        if (kit::primaryButton(cs.steerPending ? "Queuing" : "Steer",
                               st.workerStarted && !cs.steerPending && cs.steerBuf[0] != '\0',
                               "Steering is queued for the next turn boundary"))
            sendSubagentSteer(st, cs);
        ImGui::SameLine();
        if (kit::secondaryButton(cs.cur.paused ? "Resume" : "Pause",
                                 st.workerStarted && !cs.ctlPending, offline))
            sendSubagentControl(st, cs, cs.cur.paused ? "resume" : "pause");
        ImGui::SameLine();
        if (kit::dangerButton("Terminate", st.workerStarted && !cs.termPending, offline))
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
        if (kit::secondaryButton("Apply", st.workerStarted && !cs.ctlPending, offline))
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

// Every frame the AI workspace is drawn: registry poll + the viewed session's trace sub-agents,
// so the roster and the nav's live badge follow the run as its events stream in.
void pollCurrentRun(AppState& st) {
    pollSubagentList(st);
    agentTraceSubagents(st, st.activeConvId(), s_trace);
    syncViewedConversation(st);
    static std::vector<RosterEntry> entries;
    rosterEntries(entries);
    s_liveCount = 0;
    for (const auto& e : entries) s_liveCount += e.live ? 1 : 0;
}

int currentRunLiveCount() { return s_liveCount; }

void drawCurrentRunView(AppState& st) {
    pollSubagentList(st);
    kit::sectionHeader("Current Run", nullptr, nullptr, -1, true);
    if (s_sub.projectKey.empty()) {
        kit::emptyState("ai_panel", "No active project",
                        "Select a project folder to see its runs and sub-agents.");
        return;
    }
    const std::string convId = st.activeConvId();
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    auto drawTrace = [&st, &convId]() {
        kit::sectionHeader("Execution trace", nullptr, nullptr, -1, true);
        drawAgentTrace(st, convId, selectSubagent, selectTraceStep,
                       s_step.active ? s_step.step.uid : 0ull);
    };
    // Wide: trace on the left, roster + inspector on the right. Narrow: stacked.
    if (avail.x >= ui::metrics::px(760.0f)) {
        const float leftW = std::floor(avail.x * 0.56f);
        ImGui::BeginChild("##current_trace", ImVec2(leftW, 0), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar);
        drawTrace();
        ImGui::EndChild();
        ImGui::SameLine(0.0f, kit::gutter());
        ImGui::BeginChild("##current_side", ImVec2(0, 0), ImGuiChildFlags_None,
                          ImGuiWindowFlags_NoScrollbar);
        drawRoster(st, std::max(ui::metrics::px(90.0f), avail.y * 0.30f));
        kit::gapRow();
        drawInspector(st);
        ImGui::EndChild();
        return;
    }
    // Stacked: the trace (agent tree + step timeline) is the primary content, so it gets most of
    // the column; the roster and inspector share the rest.
    ImGui::BeginChild("##current_trace", ImVec2(0, std::max(ui::metrics::px(260.0f), avail.y * 0.62f)),
                      ImGuiChildFlags_None, ImGuiWindowFlags_NoScrollbar);
    drawTrace();
    ImGui::EndChild();
    kit::gapRow();
    const float remaining = ImGui::GetContentRegionAvail().y;
    drawRoster(st, std::max(ui::metrics::px(90.0f), remaining * 0.33f));
    kit::gapRow();
    drawInspector(st);
}
}  // namespace ui
