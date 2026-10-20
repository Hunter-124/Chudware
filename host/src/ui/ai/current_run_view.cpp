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
#ifdef __EMSCRIPTEN__
#include "core/jobs.h"
#endif
#include "ui/copilot/copilot_internal.h"
#include "ui/copilot/copilot.h"
#include "ui/ai/agent_dag.h"
#include "ui/ai/ai_prefs.h"
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
//
// The Sub-agents hub (drawSubagentHubView) reuses the same registry snapshot, request helpers
// and transcript/control block with its own scope-keyed selection: the active project always,
// optionally narrowed to the open session. Every registry detail/control request carries the
// inspector's scope (`project_dir`, plus `session_id` in session-only view) so the service
// refuses a sub-agent outside it.

struct SubMsg {
    std::string who;    // "user" | "subagent" | "system"
    std::string text;
    bool        queued = false;   // a steer mirrored into the transcript, not yet consumed
};

struct SubagentSession {
    std::string id, role, brief, status, statusNote, model, spawnKind;
    std::string createdTs, updatedTs;   // ISO-8601 UTC (registry created_ts / updated_ts)
    std::string modelOverride, providerOverride, effortOverride;
    std::string ownerSession;           // registry session_id (opaque cell handle on web)
    std::string parentNodeId;           // registry parent_node_id (spawning node, may be empty)
    int budgetTokens = 0, pendingSteers = 0, transcriptLen = 0;
    bool paused = false;
};

struct SubagentRegistry {
    std::vector<SubagentSession> sessions;
    std::string projectKey;
    std::string projectDir;             // convProjectDir() when projectKey was adopted
    unsigned long long generation = 0;
    unsigned long long rev = 0;         // bumps whenever `sessions` changes
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

    // Request scope + callback fence. `serial` changes on every reset, so a reply addressed to an
    // earlier selection/scope of this inspector is dropped. `scopeSession` is sent only by the
    // hub's session-only view; `expectOwner` rejects a reply whose record names another session.
    unsigned long long serial = 0;
    std::string scopeProject, scopeSession, expectOwner;
};
static SubChatState s_inspector;
static unsigned long long s_inspectorSerial = 0;

static void resetInspector(SubChatState& cs) {
    cs = SubChatState{};
    cs.serial = ++s_inspectorSerial;
}
static void bindRegistry(SubChatState& cs, const SubagentSession& reg, const std::string& project,
                         const std::string& session, const std::string& owner) {
    cs.cur = reg;
    cs.scopeProject = project;
    cs.scopeSession = session;
    cs.expectOwner = owner;
}
static void attachScope(const SubChatState& cs, nlohmann::json& p) {
    p["project_dir"] = cs.scopeProject;
#ifdef __EMSCRIPTEN__
    if (!cs.scopeSession.empty())
        p["session_id"] = cellAgentSessionHandle(cs.scopeSession);
#else
    if (!cs.scopeSession.empty()) p["session_id"] = cs.scopeSession;
#endif
}

static std::string registryConversationId(const AppState& st, const std::string& session) {
#ifdef __EMSCRIPTEN__
    if (session.empty()) return {};
    for (const auto& conversation : st.conversations)
        if (cellAgentSessionHandle(conversation.id) == session) return conversation.id;
    return {};
#else
    (void)st;
    return session;
#endif
}

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

// A matched trace's terminal outcome outlives registry polls. The registry can retain its
// last "running" snapshot after a stream stops; never revive that run in the live roster.
// Unmatched registry sessions are independent project work and keep their own controls/state.
static bool registryDisplayLive(const SubagentSession& s) {
    const AgentTraceAgent* tr = traceFor(s.id);
    return isLive(s) && (!tr || traceLive(*tr));
}
static std::string registryDisplayStatus(const SubagentSession& s) {
    const AgentTraceAgent* tr = traceFor(s.id);
    if (isLive(s) && tr && !traceLive(*tr)) return tr->status;
    return s.paused && isLive(s) ? std::string("paused") : s.status;
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

// Epoch seconds from an ISO-8601 UTC timestamp (registry _now(): timespec=milliseconds); 0 when
// it does not parse.
static double isoEpoch(const std::string& iso) {
    if (iso.size() < 19) return 0.0;
    int Y, M, D, h, m, s;
    if (std::sscanf(iso.c_str(), "%d-%d-%dT%d:%d:%d", &Y, &M, &D, &h, &m, &s) != 6)
        return 0.0;
    std::tm tm{};
    tm.tm_year = Y - 1900; tm.tm_mon = M - 1; tm.tm_mday = D;
    tm.tm_hour = h; tm.tm_min = m; tm.tm_sec = s;
#ifdef _WIN32
    const time_t tp = _mkgmtime(&tm);
#else
    const time_t tp = timegm(&tm);
#endif
    return tp < 0 ? 0.0 : (double)tp;
}

// "13m ago" from an ISO-8601 UTC timestamp.
static std::string fmtAge(const std::string& iso) {
    const double tp = isoEpoch(iso);
    if (tp <= 0.0) return std::string();
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
    o.ownerSession    = s.value("session_id", "");
    o.parentNodeId    = s.value("parent_node_id", "");
    return o;
}

// A project change invalidates both the registry snapshot and all callbacks already in flight.
static void pollSubagentList(AppState& st) {
    const std::string project = convProjectDir(st);
    const std::string key = convProjectKey(project);
    if (key != s_sub.projectKey) {
        s_sub.projectKey = key;
        s_sub.projectDir = project;
        ++s_sub.generation;
        ++s_sub.rev;
        s_sub.sessions.clear();
        s_sub.listPending = false;
        s_sub.listedOnce = false;
        s_sub.listErr.clear();
        s_sub.recentExpanded = false;
        resetInspector(s_inspector);
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
    // The limit covers every project session the hub may narrow to; filtering by owner session
    // happens on this one snapshot so Current Run and the hub never poll twice.
    st.rpc.call("subagent_list", {{"project_dir", project}, {"limit", 500}},
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
        ++s_sub.rev;
        if (r.result.contains("sessions") && r.result["sessions"].is_array())
            for (const auto& s : r.result["sessions"])
                if (s.is_object()) s_sub.sessions.push_back(parseSubSession(s));
        if (s_selId == "main" || s_selId.empty()) return;
        const SubagentSession* reg = registryFor(s_selId);
        if (!reg) {
            if (!s_inspector.cur.id.empty()) resetInspector(s_inspector);
        } else if (s_inspector.cur.id == s_selId) {
            s_inspector.cur = *reg;
        } else {   // a trace agent selected before the registry listed it gains its controls
            resetInspector(s_inspector);
            bindRegistry(s_inspector, *reg, s_sub.projectDir, std::string(), std::string());
        }
    });
}

// Refresh only the given inspector; a late response cannot retarget another scope/agent.
static void fetchSubagentTranscript(AppState& st, SubChatState& cs, bool force = false) {
    if (!st.workerStarted || cs.trPending || cs.cur.id.empty() || cs.scopeProject.empty())
        return;
    const double now = ImGui::GetTime();
    if (!force && now - cs.lastTrT < 2.0) return;
    cs.trPending = true;
    cs.lastTrT   = now;
    nlohmann::json p;
    p["subagent_id"] = cs.cur.id;
    attachScope(cs, p);
    SubChatState* target = &cs;
    const auto serial = cs.serial;
    const std::string want = cs.cur.id;
    const auto generation = s_sub.generation;
    st.rpc.call("subagent_transcript", p,
                [target, serial, want, generation](const core::RpcResult& r) {
        if (generation != s_sub.generation || target->serial != serial ||
            target->cur.id != want)
            return;
        SubChatState& cs = *target;
        cs.trPending = false;
        if (core::workerLaneBusy(r)) return;   // keep the transcript already shown
        if (!r.ok || !r.result.value("ok", false)) {
            cs.viewErr = userFacingRpcError(r, "The transcript isn't available right now.");
            return;
        }
        if (r.result.contains("session") && r.result["session"].is_object()) {
            const auto& session = r.result["session"];
            if (session.value("id", std::string()) != want) return;
            if (!cs.expectOwner.empty() &&
                session.value("session_id", std::string()) != cs.expectOwner) {
                cs.viewErr = "Sub-agent is outside the requested scope.";
                cs.msgs.clear();
                return;
            }
            cs.cur = parseSubSession(session);
        }
        cs.viewErr.clear();
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
// The registry record a control reply changed, in both the snapshot and the inspector copy.
static void applySubagentControlResult(SubagentSession& s, const nlohmann::json& result) {
    s.status           = result.value("session_status", s.status);
    s.paused           = result.value("paused", s.paused);
    s.modelOverride    = result.value("model_override", s.modelOverride);
    s.providerOverride = result.value("provider_override", s.providerOverride);
    s.effortOverride   = result.value("effort_override", s.effortOverride);
}

static void sendSubagentControl(AppState& st, SubChatState& cs, const char* op,
                                const nlohmann::json& extra = nlohmann::json::object()) {
    if (!st.workerStarted || cs.ctlPending || !isLive(cs.cur) || cs.scopeProject.empty()) return;
    cs.ctlPending = true;
    nlohmann::json p = extra;
    p["subagent_id"] = cs.cur.id;
    p["op"] = op;
    attachScope(cs, p);
    SubChatState* target = &cs;
    const auto serial = cs.serial;
    const std::string want = cs.cur.id;
    const auto generation = s_sub.generation;
    st.rpc.call("subagent_control", std::move(p),
                [&st, target, serial, want, generation](const core::RpcResult& r) {
        if (generation != s_sub.generation || target->serial != serial ||
            target->cur.id != want)
            return;
        SubChatState& cs = *target;
        cs.ctlPending = false;
        if (!r.ok || !r.result.value("ok", false)) {
            cs.steerNote = userFacingRpcError(r, "That change couldn't be applied.");
            return;
        }
        cs.steerNote = r.result.value("note", std::string("applied at the next turn boundary"));
        applySubagentControlResult(cs.cur, r.result);
        for (auto& session : s_sub.sessions)
            if (session.id == want) {
                applySubagentControlResult(session, r.result);
                ++s_sub.rev;
                break;
            }
        cs.runtimeSeeded = false;
        cs.lastTrT = 0.0;
        fetchSubagentTranscript(st, cs, true);
        s_sub.lastListT = 0.0;
    });
}


static void sendSubagentSteer(AppState& st, SubChatState& cs) {
    if (!st.workerStarted || cs.steerPending || !isLive(cs.cur) || cs.scopeProject.empty())
        return;
    std::string text(cs.steerBuf);
    if (text.find_first_not_of(" \t\r\n") == std::string::npos) return;
    cs.steerPending = true;
    nlohmann::json p;
    p["subagent_id"] = cs.cur.id;
    p["text"]        = text;
    attachScope(cs, p);
    SubChatState* target = &cs;
    const auto serial = cs.serial;
    const std::string want = cs.cur.id;
    const auto generation = s_sub.generation;
    st.rpc.call("subagent_steer", p, [target, serial, want, generation](const core::RpcResult& r) {
        if (generation != s_sub.generation || target->serial != serial ||
            target->cur.id != want)
            return;
        SubChatState& cs = *target;
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
    if (!st.workerStarted || cs.termPending || !isLive(cs.cur) || cs.scopeProject.empty())
        return;
    cs.termPending = true;
    nlohmann::json p;
    p["subagent_id"] = cs.cur.id;
    p["confirm"] = true;   // explicit inline confirmation in the inspector
    attachScope(cs, p);
    SubChatState* target = &cs;
    const auto serial = cs.serial;
    const std::string want = cs.cur.id;
    const auto generation = s_sub.generation;
    st.rpc.call("subagent_terminate", p,
                [target, serial, want, generation](const core::RpcResult& r) {
        if (generation != s_sub.generation || target->serial != serial ||
            target->cur.id != want)
            return;
        SubChatState& cs = *target;
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
    if (changed) { resetInspector(s_inspector); s_resetInspectorScroll = true; }
    s_selId = id;
    if (!st.activeConvId().empty()) s_selectedByConv[st.activeConvId()] = id;
    if (id == "main") return;
    const SubagentSession* reg = registryFor(id);
    if (!reg) { resetInspector(s_inspector); return; }
    if (hadRegistry && !changed) s_inspector.cur = *reg;
    else bindRegistry(s_inspector, *reg, s_sub.projectDir, std::string(), std::string());
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
    resetInspector(s_inspector);
    s_resetInspectorScroll = true;
    const auto it = s_selectedByConv.find(convId);
    s_selId = it == s_selectedByConv.end() ? "main" : it->second;
    if (s_selId != "main" && !traceFor(s_selId) && !registryFor(s_selId))
        s_selId = "main";
    if (!agentTraceSelectAgent(st, convId, s_selId))
        agentTraceSelectAgent(st, convId, "main");
    if (s_selId != "main") {
        if (const SubagentSession* reg = registryFor(s_selId)) {
            bindRegistry(s_inspector, *reg, s_sub.projectDir, std::string(), std::string());
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
        // Pause is a live session control, but a stale registry poll cannot reopen a stopped run.
        e.live = e.reg ? registryDisplayLive(*e.reg) : traceLive(a);
        out.push_back(e);
    }
    for (const auto& s : s_sub.sessions) {
        if (traceFor(s.id)) continue;
        RosterEntry e;
        e.reg = &s;
        e.live = registryDisplayLive(s);
        out.push_back(e);
    }
}

static std::string entryStatus(const RosterEntry& e) {
    if (e.reg) return registryDisplayStatus(*e.reg);
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

// Runtime override fields start from the record once its transcript settled.
static void seedRuntime(SubChatState& cs) {
    if (cs.runtimeSeeded || cs.trPending) return;
    std::snprintf(cs.modelBuf, sizeof(cs.modelBuf), "%s", cs.cur.modelOverride.c_str());
    cs.effortIdx = cs.cur.effortOverride == "low" ? 1 :
                   cs.cur.effortOverride == "medium" ? 2 :
                   cs.cur.effortOverride == "high" ? 3 :
                   cs.cur.effortOverride == "xhigh" ? 4 :
                   cs.cur.effortOverride == "max" ? 5 : 0;
    cs.runtimeSeeded = true;
}

// A registry sub-agent's transcript and its real turn-boundary controls. Shared by the Current
// Run inspector and the hub detail; every request carries `cs`'s own scope.
static void drawRegistryTranscriptAndControls(AppState& st, SubChatState& cs,
                                              const char* panelName, const char* stickRegion) {
    kit::sectionHeader("Transcript");
    const float logH = std::max(ui::metrics::px(120.0f),
        std::min(ui::metrics::px(260.0f), ImGui::GetContentRegionAvail().y * 0.45f));
    ImGui::BeginChild("##inspector_transcript", ImVec2(0, logH),
                      ImGuiChildFlags_AlwaysUseWindowPadding, ImGuiWindowFlags_NoScrollbar);
    const AdaptiveSurface& logSurf = adaptivePanel(panelId(panelName),
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
    chatLogStickToBottom(stickRegion);
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
        static const char* efforts[] = {"Default", "low", "medium", "high", "xhigh", "max"};
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
    seedRuntime(cs);
    const bool live = registryDisplayLive(cs.cur);
    const std::string status = registryDisplayStatus(cs.cur);
    kit::chip(status.empty() ? "unknown" : status.c_str(), toneFor(status, live), live);
    if (cs.cur.paused && status != "paused" && live) {
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
    if (tr) drawTraceFacts(*tr, live);
    if (!cs.cur.statusNote.empty()) kit::dimText(cs.cur.statusNote.c_str());
    if (!cs.cur.brief.empty()) {
        kit::sectionHeader("Prompt");
        kit::dimText(cs.cur.brief.c_str());
    }
    drawRegistryTranscriptAndControls(st, cs, "ai_ws_inspector_transcript",
                                      "aiws.inspector_transcript");
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
}

// ---- Sub-agents hub ------------------------------------------------------------------------------
// One roster for the active project: the trace sub-agents of EVERY conversation whose stored
// projectDir is the active root (the open chat contributes only when it is stored there) plus the
// project's registry records from the shared snapshot above. Identity is (conversation id,
// sub-agent id), so the same sub-agent id in two sessions is two rows with separate details.
// Selection lives here, independent of the open chat: inspecting never switches the main chat
// and never moves Current Run's trace selection. A scope change (project, session-only toggle,
// or — in session-only view — the open chat) drops the roster, selection, transcript, steer
// draft and every reply still in flight for the old scope.

struct HubEntry {
    std::string key;           // convId + '\x1f' + agent id
    std::string convId;        // owning conversation ("" when the registry names none)
    std::string convTitle;
    std::string id, title, status;
    std::string parentKey;     // spawning agent's entry in the same session; "" = top level
    bool hasTrace = false, hasReg = false, live = false;
    bool parentUnresolved = false;   // the trace names a parent this roster does not have
    AgentTraceAgent tr;
    SubagentSession reg;
    double activityTs = 0.0;
};
struct HubRow {
    int entry = -1;            // -1: session group header
    int depth = 0;
    std::string header;
    int count = 0;
};
struct HubState {
    std::string scopeKey;
    std::string projectDir, projectKey;
    std::string sessionId, sessionTitle;   // session-only view: the open chat, when in project
    bool sessionOnly = false;
    bool sessionClosed = false;  // session-only, but no open chat is stored under this project
    std::vector<HubEntry> entries;
    std::vector<HubRow> flatRows, treeRows;
    int sessionCount = 0;
    int live = 0;
    double builtT = -1.0;
    unsigned long long builtRev = 0;
    std::string selKey;
    SubChatState inspector;
    unsigned long long stepUid = 0;
    std::vector<AgentTraceStep> steps;
    bool resetDetailScroll = false;
};
static HubState s_hub;

static std::string hubKey(const std::string& conv, const std::string& id) {
    std::string k = conv;
    k.push_back('\x1f');
    k += id;
    return k;
}
static const HubEntry* hubFind(const std::string& key) {
    for (const auto& e : s_hub.entries) if (e.key == key) return &e;
    return nullptr;
}
static const Conversation* findConversation(const AppState& st, const std::string& id) {
    if (id.empty()) return nullptr;
    for (const auto& c : st.conversations) if (c.id == id) return &c;
    return nullptr;
}
static std::string sessionLabel(const Conversation& c) {
    return c.title.empty() ? std::string("Untitled session") : c.title;
}
static std::string hubModel(const HubEntry& e) {
    if (e.hasTrace && !e.tr.model.empty()) return e.tr.model;
    if (e.hasReg) return !e.reg.modelOverride.empty() ? e.reg.modelOverride : e.reg.model;
    return std::string();
}
static const std::string& hubTask(const HubEntry& e) {
    return e.hasReg && !e.reg.brief.empty() ? e.reg.brief : e.tr.brief;
}
static void hubBindRegistry(SubChatState& cs, const HubEntry& e) {
    bindRegistry(cs, e.reg, s_hub.projectDir,
                 s_hub.sessionOnly ? s_hub.sessionId : std::string(), e.reg.ownerSession);
}

static void hubSyncScope(AppState& st) {
    const std::string dir = convProjectDir(st);
    const std::string pkey = convProjectKey(dir);
    const bool sessionOnly = aiSubagentHubSessionOnly();
    const std::string open = st.activeConvId();
    std::string session, title;
    bool closed = false;
    if (sessionOnly) {
        // Fail closed: no open chat, or one stored under another project, narrows to nothing.
        const Conversation* c = findConversation(st, open);
        if (!pkey.empty() && c && convProjectKey(c->projectDir) == pkey) {
            session = open;
            title = sessionLabel(*c);
        } else {
            closed = true;
        }
    }
    std::string key = pkey;
    key.push_back('\x1f');
    key += !sessionOnly ? std::string("project") : (closed ? "closed:" : "session:") + open;
    s_hub.sessionTitle = title;
    if (key == s_hub.scopeKey) return;
    s_hub.scopeKey = key;
    s_hub.projectDir = dir;
    s_hub.projectKey = pkey;
    s_hub.sessionId = session;
    s_hub.sessionOnly = sessionOnly;
    s_hub.sessionClosed = closed;
    s_hub.entries.clear();
    s_hub.flatRows.clear();
    s_hub.treeRows.clear();
    s_hub.sessionCount = 0;
    s_hub.live = 0;
    s_hub.builtT = -1.0;
    s_hub.selKey.clear();
    resetInspector(s_hub.inspector);
    s_hub.stepUid = 0;
    s_hub.steps.clear();
    s_hub.resetDetailScroll = true;
}

static void hubOrderRows() {
    auto& entries = s_hub.entries;
    const int n = (int)entries.size();
    std::vector<int> flat(n);
    for (int i = 0; i < n; ++i) flat[(size_t)i] = i;
    std::stable_sort(flat.begin(), flat.end(), [&entries](int a, int b) {
        const HubEntry& x = entries[(size_t)a];
        const HubEntry& y = entries[(size_t)b];
        if (x.live != y.live) return x.live;
        return x.activityTs > y.activityTs;
    });
    for (int i : flat) { HubRow r; r.entry = i; s_hub.flatRows.push_back(r); }

    // By parent: one group per owning session (most recent activity first), each a tree.
    std::vector<std::string> groups;
    std::unordered_map<std::string, double> groupTs;
    std::unordered_map<std::string, int> groupCount;
    std::unordered_map<std::string, std::vector<int>> children;
    for (int i = 0; i < n; ++i) {
        const HubEntry& e = entries[(size_t)i];
        auto [it, fresh] = groupTs.try_emplace(e.convId, e.activityTs);
        if (fresh) groups.push_back(e.convId);
        else it->second = std::max(it->second, e.activityTs);
        ++groupCount[e.convId];
        if (!e.parentKey.empty()) children[e.parentKey].push_back(i);
    }
    std::stable_sort(groups.begin(), groups.end(), [&groupTs](const std::string& a,
                                                              const std::string& b) {
        return groupTs[a] > groupTs[b];
    });
    s_hub.sessionCount = (int)groups.size();
    std::vector<char> seen((size_t)n, 0);
    std::vector<std::pair<int, int>> stack;
    for (const std::string& conv : groups) {
        HubRow head;
        head.count = groupCount[conv];
        auto walk = [&](int root) {
            stack.clear();
            stack.emplace_back(root, 0);
            while (!stack.empty()) {
                const auto [idx, depth] = stack.back();
                stack.pop_back();
                if (seen[(size_t)idx]) continue;
                seen[(size_t)idx] = 1;
                HubRow r;
                r.entry = idx;
                r.depth = depth;
                s_hub.treeRows.push_back(r);
                const auto ch = children.find(entries[(size_t)idx].key);
                if (ch == children.end()) continue;
                for (auto c = ch->second.rbegin(); c != ch->second.rend(); ++c)
                    if (!seen[(size_t)*c]) stack.emplace_back(*c, depth + 1);
            }
        };
        for (int i = 0; i < n; ++i)
            if (entries[(size_t)i].convId == conv) { head.header = entries[(size_t)i].convTitle; break; }
        s_hub.treeRows.push_back(head);
        for (int i = 0; i < n; ++i)
            if (entries[(size_t)i].convId == conv && entries[(size_t)i].parentKey.empty()) walk(i);
        for (int i = 0; i < n; ++i)   // parent cycles: never drop a row
            if (entries[(size_t)i].convId == conv && !seen[(size_t)i]) walk(i);
    }
}

static void hubBuild(AppState& st) {
    const double now = ImGui::GetTime();
    const bool regChanged = s_hub.builtRev != s_sub.rev;
    if (s_hub.builtT >= 0.0 && !regChanged && now - s_hub.builtT < 0.25) return;
    s_hub.builtT = now;
    s_hub.builtRev = s_sub.rev;
    auto& entries = s_hub.entries;
    entries.clear();
    s_hub.flatRows.clear();
    s_hub.treeRows.clear();
    s_hub.sessionCount = 0;
    s_hub.live = 0;
    const bool inScope = !s_hub.projectKey.empty() && !(s_hub.sessionOnly && s_hub.sessionClosed);
    if (inScope) {
        static std::vector<AgentTraceAgent> agents;
        for (const Conversation& c : st.conversations) {
            if (convProjectKey(c.projectDir) != s_hub.projectKey) continue;
            if (s_hub.sessionOnly && c.id != s_hub.sessionId) continue;
            agentTraceSubagents(st, c.id, agents);
            for (const auto& a : agents) {
                HubEntry e;
                e.convId = c.id;
                e.convTitle = sessionLabel(c);
                e.id = a.id;
                e.key = hubKey(c.id, a.id);
                e.hasTrace = true;
                e.tr = a;
                entries.push_back(std::move(e));
            }
        }
        // The registry snapshot is project-filtered by the service; adopt it only for this key.
        if (s_sub.projectKey == s_hub.projectKey) {
            for (const auto& s : s_sub.sessions) {
                if (s.id.empty()) continue;
                const std::string ownerConv = registryConversationId(st, s.ownerSession);
                if (s_hub.sessionOnly && ownerConv != s_hub.sessionId) continue;
                const std::string key = hubKey(ownerConv.empty() ? s.ownerSession : ownerConv, s.id);
                auto it = std::find_if(entries.begin(), entries.end(),
                                       [&key](const HubEntry& e) { return e.key == key; });
                if (it != entries.end()) { it->hasReg = true; it->reg = s; continue; }
                HubEntry e;
                e.convId = ownerConv;
                e.id = s.id;
                e.key = key;
                e.hasReg = true;
                e.reg = s;
                const Conversation* c = findConversation(st, ownerConv);
                e.convTitle = s.ownerSession.empty() ? std::string("No owning session")
                    : c && convProjectKey(c->projectDir) == s_hub.projectKey ? sessionLabel(*c)
                    : std::string("Unlisted session");
                entries.push_back(std::move(e));
            }
        }
    }
    std::unordered_map<std::string, size_t> index;
    for (size_t i = 0; i < entries.size(); ++i) index[entries[i].key] = i;
    for (size_t i = 0; i < entries.size(); ++i) {
        HubEntry& e = entries[i];
        e.title = e.hasTrace ? e.tr.name
                : e.reg.role.empty() ? std::string("Sub-agent") : e.reg.role;
        if (e.hasReg) {
            // A matched trace's terminal outcome outlives a stale "running" registry snapshot.
            const bool regLive = isLive(e.reg);
            const bool traceEnded = e.hasTrace && !traceLive(e.tr);
            e.live = regLive && !traceEnded;
            e.status = regLive && traceEnded ? e.tr.status
                     : e.reg.paused && regLive ? std::string("paused") : e.reg.status;
        } else {
            e.live = traceLive(e.tr);
            e.status = e.tr.status;
        }
        if (e.status.empty()) e.status = "unknown";
        double ts = 0.0;
        if (e.hasTrace) ts = std::max(e.tr.lastStepTs, std::max(e.tr.endTs, e.tr.startTs));
        if (e.hasReg) ts = std::max(ts, isoEpoch(e.reg.updatedTs));
        e.activityTs = ts;
        const std::string& parent = e.hasTrace ? e.tr.parent : e.reg.parentNodeId;
        if (!parent.empty() && parent != "main") {
            const auto p = index.find(hubKey(e.convId, parent));
            if (p != index.end() && p->second != i) e.parentKey = p->first;
            else if (e.hasTrace) e.parentUnresolved = true;
        }
        s_hub.live += e.live ? 1 : 0;
    }
    hubOrderRows();

    // Keep the selection honest against the rebuilt roster.
    if (s_hub.selKey.empty()) return;
    SubChatState& cs = s_hub.inspector;
    const HubEntry* sel = hubFind(s_hub.selKey);
    if (!sel) {
        s_hub.selKey.clear();
        resetInspector(cs);
        s_hub.stepUid = 0;
        s_hub.steps.clear();
        s_hub.resetDetailScroll = true;
    } else if (!sel->hasReg) {
        if (!cs.cur.id.empty()) resetInspector(cs);
    } else if (cs.cur.id != sel->reg.id) {   // the registry listed it after it was selected
        resetInspector(cs);
        hubBindRegistry(cs, *sel);
        fetchSubagentTranscript(st, cs, true);
    } else if (regChanged) {
        cs.cur = sel->reg;
    }
}

static void hubPoll(AppState& st) {
    hubSyncScope(st);
    hubBuild(st);
}

static void hubSelect(AppState& st, const std::string& key) {
    if (key == s_hub.selKey) return;
    s_hub.selKey = key;
    resetInspector(s_hub.inspector);
    s_hub.stepUid = 0;
    s_hub.steps.clear();
    s_hub.resetDetailScroll = true;
    const HubEntry* e = hubFind(key);
    if (e && e->hasReg) {
        hubBindRegistry(s_hub.inspector, *e);
        fetchSubagentTranscript(st, s_hub.inspector, true);
    }
}

static void drawHubRoster(AppState& st, const ImVec2& size) {
    ImGui::BeginChild("##hub_roster", size, ImGuiChildFlags_AlwaysUseWindowPadding,
                      ImGuiWindowFlags_NoScrollbar);
    const AdaptiveSurface& surf = adaptivePanel(panelId("ai_ws_hub_roster"),
        ImRect(ImGui::GetWindowPos(), ImVec2(ImGui::GetWindowPos().x + ImGui::GetWindowSize().x,
                                            ImGui::GetWindowPos().y + ImGui::GetWindowSize().y)),
        GlassGroup::Global);
    const bool byParent = aiSubagentHubByParent();
    const std::vector<HubRow>& rows = byParent ? s_hub.treeRows : s_hub.flatRows;
    const float indentStep = ui::metrics::px(14.0f);
    std::string pick;
    bool firstHeader = true;
    for (const HubRow& r : rows) {
        if (r.entry < 0) {
            kit::sectionHeader(r.header.c_str(), nullptr, nullptr, r.count, firstHeader);
            firstHeader = false;
            continue;
        }
        const HubEntry& e = s_hub.entries[(size_t)r.entry];
        std::string secondary = e.status;
        const std::string model = hubModel(e);
        if (!model.empty()) secondary += " \xC2\xB7 " + model;
        const std::string& task = hubTask(e);
        if (!task.empty()) secondary += " \xC2\xB7 " + task;
        std::string meta;
        if (e.hasTrace) {
            meta = traceElapsed(e.tr, e.live);
            if (!meta.empty()) meta += " \xC2\xB7 ";
            meta += toolCountText(e.tr);
        } else {
            meta = fmtAge(e.reg.updatedTs);
        }
        std::string tip = "Session: " + e.convTitle;
        if (!task.empty()) tip += "\nTask: " + task;
        if (e.hasTrace && !e.tr.lastStep.empty()) tip += "\nLast step: " + e.tr.lastStep;
        kit::RowStyle style;
        style.secondary = secondary.c_str();
        style.meta = meta.empty() ? nullptr : meta.c_str();
        style.selected = s_hub.selKey == e.key;
        style.tooltip = tip.c_str();
        const float indent = indentStep * (float)std::min(r.depth, 6);
        if (indent > 0.0f) ImGui::Indent(indent);
        ImGui::PushID(e.key.c_str());
        if (kit::row("##hub_agent", e.title.c_str(), style)) pick = e.key;
        ImGui::PopID();
        if (indent > 0.0f) ImGui::Unindent(indent);
    }
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
    if (!pick.empty()) hubSelect(st, pick);
}

// One lineage row: ancestors, the inspected agent and its direct children, indented by depth.
static void hubLineageRow(const HubEntry& e, int depth, bool self, std::string& pick) {
    kit::RowStyle style;
    style.secondary = e.status.c_str();
    style.selected = self;
    const float indent = ui::metrics::px(14.0f) * (float)std::min(depth, 6);
    if (indent > 0.0f) ImGui::Indent(indent);
    ImGui::PushID(e.key.c_str());
    if (kit::row("##hub_lineage", e.title.c_str(), style) && !self) pick = e.key;
    ImGui::PopID();
    if (indent > 0.0f) ImGui::Unindent(indent);
}

static void drawHubDetail(AppState& st, const ImVec2& size) {
    ImGui::BeginChild("##hub_detail", size, ImGuiChildFlags_AlwaysUseWindowPadding,
                      ImGuiWindowFlags_NoScrollbar);
    if (s_hub.resetDetailScroll) {
        ImGui::SetScrollY(0.0f);
        s_hub.resetDetailScroll = false;
    }
    const AdaptiveSurface& surf = adaptivePanel(panelId("ai_ws_hub_detail"),
        ImRect(ImGui::GetWindowPos(), ImVec2(ImGui::GetWindowPos().x + ImGui::GetWindowSize().x,
                                            ImGui::GetWindowPos().y + ImGui::GetWindowSize().y)),
        GlassGroup::Global);
    const HubEntry* e = s_hub.selKey.empty() ? nullptr : hubFind(s_hub.selKey);
    if (!e) {
        kit::emptyState("ai_panel", "Select a sub-agent",
                        "Its model, task, lineage and recent activity appear here. Inspecting "
                        "a sub-agent never switches the open chat.");
        drawWindowPillScrollbarV(surf);
        ImGui::EndChild();
        return;
    }
    std::string pick;   // applied after drawing: `e` must stay this frame's selection
    kit::sectionHeader(e->title.c_str(), nullptr, nullptr, -1, true);
    kit::chip(e->status.c_str(), toneFor(e->status, e->live), e->live);
    if (e->hasReg && e->reg.paused && e->live && e->status != "paused") {
        ImGui::SameLine();
        kit::chip("paused", kit::Tone::Warn);
    }
    ImGui::SameLine();
    kit::chip(e->hasReg ? "Registry" : "Trace only", e->hasReg ? kit::Tone::Info : kit::Tone::Neutral,
              false,
              e->hasReg ? "Recorded in the project's sub-agent registry: transcript and "
                          "turn-boundary controls are available."
                        : "Seen in its session's execution trace. Directed by its parent agent, "
                          "so it is read-only here.");
    kit::keyValueRow("Session", e->convTitle.c_str());
    const std::string model = hubModel(*e);
    kit::keyValueRow("Model", !model.empty() ? model.c_str()
                              : e->hasReg ? "inherited" : "Not reported");
    if (e->hasReg) {
        kit::keyValueRow("Role", e->reg.role.empty() ? "sub-agent" : e->reg.role.c_str());
        if (e->reg.budgetTokens > 0)
            kit::keyValueRow("Budget", (std::to_string(e->reg.budgetTokens) + " tokens").c_str());
    }
    if (e->hasTrace && e->tr.startTs > 0.0) {
        kit::keyValueRow("Started", kit::fmtAgo(e->tr.startTs).c_str());
    } else if (e->hasReg) {
        const std::string started = fmtAge(e->reg.createdTs);
        if (!started.empty()) kit::keyValueRow("Started", started.c_str());
    }
    if (e->hasTrace) {
        drawTraceFacts(e->tr, e->live);
    } else {
        const std::string updated = fmtAge(e->reg.updatedTs);
        if (!updated.empty()) kit::keyValueRow("Last update", updated.c_str());
    }
    // Sub-agent token/cost usage is not part of either source; never estimate it.
    kit::keyValueRow("Token usage", "Not reported");
    if (e->hasReg && !e->reg.statusNote.empty()) kit::dimText(e->reg.statusNote.c_str());
    if (e->hasTrace && !e->tr.note.empty()) kit::dimText(e->tr.note.c_str());

    // Lineage: the root, each ancestor in this session, the agent, then its direct children.
    kit::sectionHeader("Lineage");
    std::vector<const HubEntry*> chain;
    for (const HubEntry* p = e; p && !p->parentKey.empty() && chain.size() < 32;) {
        p = hubFind(p->parentKey);
        if (p) chain.push_back(p);
    }
    const HubEntry* top = chain.empty() ? e : chain.back();
    kit::dimText(top->parentUnresolved ? "Spawned by an agent not recorded in this session"
                 : !top->hasTrace && !top->reg.parentNodeId.empty()
                     ? "Spawned from its session's run"
                     : "Main agent of the session");
    int depth = 1;
    for (auto it = chain.rbegin(); it != chain.rend(); ++it) hubLineageRow(**it, depth++, false, pick);
    hubLineageRow(*e, depth, true, pick);
    for (const auto& c : s_hub.entries)
        if (c.parentKey == e->key) hubLineageRow(c, depth + 1, false, pick);

    const std::string& task = hubTask(*e);
    if (!task.empty() && task != e->title) {
        kit::sectionHeader("Task");
        kit::dimText(task.c_str());
    }

    if (e->hasTrace) {
        agentTraceRecentSteps(st, e->convId, e->id, 12, s_hub.steps);
        kit::sectionHeader("Recent activity", nullptr, nullptr, (int)s_hub.steps.size());
        if (s_hub.steps.empty()) kit::dimText("No steps recorded yet.");
        for (auto it = s_hub.steps.rbegin(); it != s_hub.steps.rend(); ++it) {
            const AgentTraceStep& s = *it;
            std::string title = s.kind;
            if (!s.title.empty()) title += " \xC2\xB7 " + s.title;
            std::string secondary = s.status.empty() ? std::string()
                                  : s.status == "ok" ? std::string("done") : s.status;
            const std::string dur = stepDurationText(s.durationMs);
            if (!dur.empty()) secondary += (secondary.empty() ? "" : " \xC2\xB7 ") + dur;
            if (!s.target.empty()) secondary += (secondary.empty() ? "" : " \xC2\xB7 ") + s.target;
            const std::string when = s.ts > 0.0 ? kit::fmtAgo(s.ts) : std::string();
            kit::RowStyle style;
            style.secondary = secondary.empty() ? nullptr : secondary.c_str();
            style.meta = when.empty() ? nullptr : when.c_str();
            style.selected = s_hub.stepUid == s.uid;
            ImGui::PushID(std::to_string(s.uid).c_str());
            if (kit::row("##hub_step", title.c_str(), style))
                s_hub.stepUid = s_hub.stepUid == s.uid ? 0ull : s.uid;
            ImGui::PopID();
            if (s_hub.stepUid != s.uid) continue;
            if (!s.error.empty() && s.status != "ok") {
                ImGui::PushStyleColor(ImGuiCol_Text, kit::toneOn(kit::Tone::Err, surf));
                ImGui::TextWrapped("%s", s.error.c_str());
                ImGui::PopStyleColor();
            }
            if (!s.detail.empty()) {
                const size_t cap = 2000;
                const std::string shown = s.detail.size() > cap
                    ? s.detail.substr(0, cap) + "..." : s.detail;
                kit::dimText(shown.c_str());
            } else if (s.error.empty()) {
                kit::dimText("This step carries no further detail.");
            }
            kit::gapRow();
        }
    } else {
        kit::sectionHeader("Recent activity");
        kit::dimText("No execution trace was recorded for this sub-agent in its session.");
    }

    if (e->hasReg) {
        SubChatState& cs = s_hub.inspector;
        if (!st.workerStarted)
            kit::inlineStatus("AI service offline. The transcript and controls return when it "
                              "is running.", kit::Tone::Warn);
        if (cs.cur.id == e->reg.id) {
            fetchSubagentTranscript(st, cs);
            seedRuntime(cs);
            drawRegistryTranscriptAndControls(st, cs, "ai_ws_hub_transcript",
                                              "aiws.hub_transcript");
        }
    } else {
        kit::gapRow();
        kit::dimText("This sub-agent is directed by its parent agent, so it has no steering, "
                     "pause or terminate controls here.");
    }
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();
    if (!pick.empty()) hubSelect(st, pick);
}

static void drawHubToolbar(AppState& st) {
    static const char* const orgs[] = {"Flat", "By parent"};
    static const char* const orgTips[] = {
        "Every sub-agent in scope, live first, then most recent.",
        "Grouped by owning session and nested under the agent that spawned it."};
    int org = aiSubagentHubByParent() ? 1 : 0;
    if (kit::segmented("##hub_org", orgs, 2, &org, 0.0f, orgTips))
        setAiSubagentHubByParent(org == 1);
    const char* chipLabel = "Current session only";
    const float chipW = ImGui::CalcTextSize(chipLabel).x + kit::padX() * 2.0f;
    const float right = ImGui::GetWindowPos().x + ImGui::GetWindowContentRegionMax().x;
    if (right - ImGui::GetItemRectMax().x - kit::gutter() >= chipW)
        ImGui::SameLine(0.0f, kit::gutter());
    bool sessionOnly = aiSubagentHubSessionOnly();
    if (kit::toggleChip(chipLabel, &sessionOnly, kit::Tone::Accent,
                        "Narrow the roster to the open chat's session. Off: every session in "
                        "the active project.")) {
        setAiSubagentHubSessionOnly(sessionOnly);
        hubPoll(st);   // the new scope takes effect before anything is drawn from the old one
    }
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
    hubPoll(st);
}

int currentRunLiveCount() { return s_liveCount; }

int subagentHubLiveCount() { return s_hub.live; }

void drawSubagentHubView(AppState& st) {
    pollSubagentList(st);
    hubPoll(st);
    kit::sectionHeader("Sub-agents", nullptr, nullptr,
                       s_hub.entries.empty() ? -1 : (int)s_hub.entries.size(), true);
    if (s_hub.projectKey.empty()) {
        kit::emptyState("ai_panel", "No active project",
                        "Select a project folder to see its sub-agents.");
        return;
    }
    drawHubToolbar(st);
    std::string scope = "Active project";
    if (s_hub.sessionOnly)
        scope += s_hub.sessionClosed ? std::string(" \xC2\xB7 no open session here")
                                     : " \xC2\xB7 " + s_hub.sessionTitle;
    else
        scope += " \xC2\xB7 " + std::to_string(s_hub.sessionCount) +
                 (s_hub.sessionCount == 1 ? " session" : " sessions");
    kit::dimText(scope.c_str());
    if (!st.workerStarted)
        kit::inlineStatus("AI service offline. Recorded traces are shown; registry sub-agents "
                          "and their controls return when it is running.", kit::Tone::Warn);
    else if (!s_sub.listErr.empty())
        kit::inlineStatus(s_sub.listErr.c_str(), kit::Tone::Warn);
    kit::gapRow();
    if (s_hub.sessionOnly && s_hub.sessionClosed) {
        kit::emptyState("ai_panel", "No open session in this project",
                        "The open chat belongs to another project, or no chat is open. Open one "
                        "of this project's sessions, or turn off Current session only.");
        return;
    }
    if (s_hub.entries.empty()) {
        if (st.workerStarted && !s_sub.listedOnce) {
            kit::spinner("Loading sub-agents");
            return;
        }
        kit::emptyState("ai_panel", "No sub-agents yet",
                        s_hub.sessionOnly
                            ? "Sub-agents spawned in this session appear here."
                            : "Sub-agents spawned by any session in this project appear here.");
        return;
    }
    // Side by side when both panes keep a useful width; stacked (roster above) otherwise.
    const ImVec2 avail = ImGui::GetContentRegionAvail();
    if (avail.x >= ui::metrics::px(620.0f)) {
        const float rosterW = std::clamp(std::floor(avail.x * 0.42f), ui::metrics::px(260.0f),
                                         ui::metrics::px(440.0f));
        drawHubRoster(st, ImVec2(rosterW, 0.0f));
        ImGui::SameLine(0.0f, kit::gutter());
        drawHubDetail(st, ImVec2(0.0f, 0.0f));
        return;
    }
    drawHubRoster(st, ImVec2(0.0f, std::max(ui::metrics::px(140.0f), std::floor(avail.y * 0.40f))));
    kit::gapRow();
    drawHubDetail(st, ImVec2(0.0f, std::max(ui::metrics::px(220.0f),
                                            ImGui::GetContentRegionAvail().y)));
}

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
