// AI workspace — full-page AI command center for the AI title-bar tab (v3 rework).
//
//   +----------------------------------------------------------------+
//   | MAIN CHAT (left, ~46%)        |  UTILITY PANEL (right)          |
//   |  message log + composer       |  [nav]  Agent Hub | Plan | ...  |
//   |  (SAME state/send path as the |  [nav]  <selected view>         |
//   |   docked Copilot)             |                                 |
//   +----------------------------------------------------------------+
//
// v3 (Hunter's UI directive): the main conversation owns the left; EVERYTHING else lives in
// the right utility panel, switched by a vertical nav menu (Agent Hub | Session Plan |
// Active Work | Documents | Sessions | Leaderboard | Metrics | Options). No permanent
// sessions rail, no bottom options strip. "Agent Hub" (OMP-style) lists every registered
// sub-agent as
// role-color-coded rows; clicking a row POPS UP a floating, role-themed chat window for that
// sub-agent (several can be open at once) — the main chat is never replaced. New live
// sub-agents auto-pop their chat (agent.ui_follow_active_session).
//
// The conversation window renders the SAME state and send path as the docked Copilot
// (st.chat / st.conversations / st.prompt) via the ui/copilot/copilot.h reuse hooks — single
// chat implementation, two hosts. "Session Plan" is the plan board over persisted buildgraph
// runs; inference settings (model/effort/tokens/API key/privacy/vision) live ONLY on the main
// Settings page — the Options view keeps the run-behavior + workspace-visibility knobs.
// No fake data anywhere: every pane degrades to an honest empty-state.
#include "ai_workspace.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <ctime>
#include <filesystem>
#include <fstream>
#include <set>
#include <string>
#include <unordered_map>
#include <vector>

#include "imgui.h"
#include "imgui_internal.h"   // ImRect (glassPanel/adaptivePanel rects)

#include "core/app_state.h"
#include "ui/copilot/copilot.h"
#include "ui_theme.h"
#include "ui/leaderboard_ui.h"
#include "ui/ai/active_work.h"
#include "ui/ai/plan_board.h"
#include "ui/ai/docs_panel.h"
#include "ui/ai/ai_prefs.h"
#include "ui/settings_modal.h"     // drawHarnessSessionKnobs (shared Options-tab run-behavior grid)
#include "ui/ai/metrics_view.h"

namespace ui {

// ---- local state ----------------------------------------------------------------
namespace {

struct DagNode {
    std::string id, label, kind, status, assigned;
    std::vector<std::string> requires;
    int layer = 0, row = 0;
};

struct DagRun {
    std::string runId;
    std::string path;      // absolute path to the .buildgraph.json sidecar
    std::string mission;   // graph.mission ("" for older runs)
    long long   mtime = 0;
    int         nodeCount = 0;
};

struct AiWsState {
    // Which view the right utility panel shows (v3 vertical nav): 0 = Agent Hub (sub-agent
    // rows + live agent-execution DAG), 1 = Session Plan (plan board over persisted buildgraph
    // runs), 2 = Active Work, 3 = Documents, 4 = Sessions, 5 = Leaderboard, 6 = Metrics,
    // 7 = Options.
    // The conversation window is always visible on the left, never a tab.
    int  view = 0;     // default to Agent Hub

    // M8 plumbing: the chat session whose plan the "Session Plan" view is scoped to. Set from
    // the active conversation each frame; the per-session run_id aggregation is Phase-2 backend
    // data (Conversation has no run_id yet — see AI_WORKSPACE_V2_PLAN.md §2.1), so today the
    // view still lists every persisted run. Kept here so the scoping wiring is already in place.
    std::string selSession;

    // Autopopulate (agent.ui_follow_active_session): the last live run id that yanked the
    // panel to the Agent Hub view — a NEW run switches the view once, then the user is
    // free to pick another tab.
    std::string lastAutoRun;

    // DAG run catalog (scanned from the persisted runs dir; Refresh re-scans).
    bool                runsScanned = false;
    std::vector<DagRun> runs;
    int                 selRun = -1;

    // Loaded graph for the selected run.
    std::string          loadedRun;
    std::vector<DagNode> nodes;
    std::string          loadErr;

    // dag_status worker probe for the selected run.
    std::string statusRun;      // run id the probe/last steer was issued for
    bool        statusPending = false;
    bool        statusOk      = false;
    std::string statusLine;

    // canvas pan/zoom
    ImVec2 pan{0.0f, 0.0f};
    float  zoom = 1.0f;

    // Leaderboard cache (aicad.leaderboard/1).
    ui::leaderboard::Board lb;
    bool                   lbLoaded = false;
};

static AiWsState s_ai;

// ==================================================================================
// Phase 2 (M9/M10) — live Agent Workflow DAG store.
//
// A DIFFERENT graph from the buildgraph Session Plan (AI_WORKSPACE_V2_PLAN.md §3): this is the
// agent loop's own execution trace — tool calls (+ what they act on), context injections, thoughts,
// and (when a production sink lands) subagent/advisor branches. It is built purely from the drained
// `aicad.agent_events/2` stream by joining `parent_id`→`id` (§2.1). No buildgraph sidecar, no RPC.
//
// HONESTY: only agent.tool_call / agent.thought / agent.context_injected are emitted by a live
// production sink today. agent.spawn / agent.advisor are emit-wired in the backend but have NO
// production caller threaded yet (tracker M20) — the node kinds exist below so the tree lights up
// the moment they arrive, but nothing is fabricated: absent events => empty-state.
// (This block lives inside the file-level anonymous namespace opened above; the public
// feedAgentDagEvent() below temporarily closes/reopens it so it gets external ui:: linkage.)
// ==================================================================================

enum class AgKind { Root, Tool, Thought, Context, Spawn, Advisor, Other };

struct AgNode {
    std::string id;         // span id (§2.1) — THE tree key
    std::string parentId;   // join target
    AgKind      kind = AgKind::Other;
    std::string actor;      // emitting agent/role
    std::string title;      // primary line (tool name / "thought" / injection source)
    std::string detail;     // secondary line (target / label / verdict / text)
    std::string status;     // "ok" | "failed" | "" (drives the accent color, reuses statusColor)
    std::string refId;      // M11: subagent_id for Spawn/subagent-root nodes — click opens its chat
    int         latencyMs = -1;
    long long   seq = 0;    // arrival order (advisory ordering fallback per §2.1)
    // tree links (resolved each rebuild)
    std::vector<int>   children;
    int   depth = 0;        // BFS depth from a root (for indent/layout)
    bool  collapsed = false;// M10: user-collapsed — hides the subtree
};

struct AgentDagStore {
    std::string          runId;          // envelope run_id this tree belongs to
    std::vector<AgNode>  nodes;
    std::unordered_map<std::string,int> byId;   // span id -> index
    long long            seqCtr = 0;
    bool                 sawSpawn = false;       // ever received a real subagent branch?
    bool                 sawAdvisor = false;     // ever received a real advisor node?
    int                  toolCount = 0, thoughtCount = 0, ctxCount = 0;

    void reset(const std::string& rid) {
        runId = rid; nodes.clear(); byId.clear(); seqCtr = 0;
        sawSpawn = sawAdvisor = false; toolCount = thoughtCount = ctxCount = 0;
    }
    bool empty() const { return nodes.empty(); }
};

static AgentDagStore s_agdag;

static std::string agEnvStr(const nlohmann::json& d, const char* k) {
    auto it = d.find(k);
    return (it != d.end() && it->is_string()) ? it->get<std::string>() : std::string();
}
static int agEnvInt(const nlohmann::json& d, const char* k, int def) {
    auto it = d.find(k);
    return (it != d.end() && it->is_number_integer()) ? it->get<int>() : def;
}

// Insert or update a node keyed by span id. Events without an `id` (legacy /1 records) get a
// synthesized per-run id from actor+seq so they still land as leaves (§2.1 consumer fallback).
static AgNode& agUpsert(const std::string& id, const std::string& parentId) {
    auto it = s_agdag.byId.find(id);
    if (it != s_agdag.byId.end()) {
        AgNode& n = s_agdag.nodes[it->second];
        if (n.parentId.empty() && !parentId.empty()) n.parentId = parentId;
        return n;
    }
    AgNode n;
    n.id = id;
    n.parentId = parentId;
    n.seq = s_agdag.seqCtr++;
    s_agdag.byId[id] = (int)s_agdag.nodes.size();
    s_agdag.nodes.push_back(std::move(n));
    return s_agdag.nodes.back();
}

// Stable serialization names for AgKind (distinct from the display tags in agKindTag below).
static const char* agKindName(AgKind k) {
    switch (k) {
        case AgKind::Root:    return "root";
        case AgKind::Tool:    return "tool";
        case AgKind::Thought: return "thought";
        case AgKind::Context: return "context";
        case AgKind::Spawn:   return "spawn";
        case AgKind::Advisor: return "advisor";
        default:              return "other";
    }
}
static AgKind agKindFromName(const std::string& s) {
    if (s == "root")    return AgKind::Root;
    if (s == "tool")    return AgKind::Tool;
    if (s == "thought") return AgKind::Thought;
    if (s == "context") return AgKind::Context;
    if (s == "spawn")   return AgKind::Spawn;
    if (s == "advisor") return AgKind::Advisor;
    return AgKind::Other;
}

}  // close the file-level anonymous namespace so feedAgentDagEvent has external ui:: linkage

// Feed one drained orchestrator event into the live Agent Workflow DAG (GL thread only).
void feedAgentDagEvent(const std::string& kind, const nlohmann::json& data) {
    if (!data.is_object()) return;

    // Run boundary: a fresh run replaces the previous tree (mirrors the context-meter reset).
    const std::string runId = agEnvStr(data, "run_id");
    if (kind == "run_started" || kind == "dag.run.started") {
        s_agdag.reset(runId);
        return;
    }
    // If a new run_id arrives without a start marker (e.g. we joined mid-run), roll over cleanly.
    if (!runId.empty() && !s_agdag.runId.empty() && runId != s_agdag.runId)
        s_agdag.reset(runId);
    if (s_agdag.runId.empty() && !runId.empty()) s_agdag.runId = runId;

    // Only agent-level kinds carry §2.1 span linkage; everything else is ignored by this store
    // (the buildgraph/Session Plan view owns the dag.* lifecycle).
    if (kind.rfind("agent.", 0) != 0) return;

    const std::string id       = agEnvStr(data, "id");
    const std::string parentId = agEnvStr(data, "parent_id");
    const std::string actor    = agEnvStr(data, "actor");
    // A missing span id (legacy /1) can't seed the tree key — fall back to a synthetic leaf id so
    // the record is still visible without a parent join.
    const std::string key = !id.empty()
        ? id
        : ("_leaf" + std::to_string(s_agdag.seqCtr) + agEnvStr(data, "node_id"));

    if (kind == "agent.tool_call") {
        AgNode& n = agUpsert(key, parentId);
        n.kind   = AgKind::Tool;
        n.actor  = actor;
        n.title  = agEnvStr(data, "tool_name");
        if (n.title.empty()) n.title = "tool";
        n.detail = agEnvStr(data, "target");        // human-readable "what it acts on" (M14)
        bool ok  = data.value("ok", true);
        n.status = ok ? "ok" : "failed";
        n.latencyMs = agEnvInt(data, "latency_ms", -1);
        ++s_agdag.toolCount;
    } else if (kind == "agent.thought") {
        AgNode& n = agUpsert(key, parentId);
        n.kind   = AgKind::Thought;
        n.actor  = actor;
        n.title  = "thought";
        n.detail = agEnvStr(data, "text");          // completed reasoning block (not streamed)
        ++s_agdag.thoughtCount;
    } else if (kind == "agent.context_injected") {
        AgNode& n = agUpsert(key, parentId);
        n.kind   = AgKind::Context;
        n.actor  = actor;
        n.title  = "context: " + agEnvStr(data, "source");
        n.detail = agEnvStr(data, "label");
        int tok  = agEnvInt(data, "token_est", 0);
        if (tok > 0) n.detail += (n.detail.empty() ? "" : "  ") + ("~" + std::to_string(tok) + " tok");
        ++s_agdag.ctxCount;
    } else if (kind == "agent.spawn") {
        // Subagent / worker branch. Emit-wired backend, no production sink today (M20) — but if a
        // real one arrives we build the branch: the spawner span is `parent_id`, and the child's
        // own execution root is `subagent_id`.
        AgNode& n = agUpsert(key, parentId);
        n.kind   = AgKind::Spawn;
        n.actor  = actor.empty() ? agEnvStr(data, "role") : actor;
        n.title  = "spawn: " + agEnvStr(data, "role");
        n.detail = agEnvStr(data, "brief");
        // Seed the child's execution root so its later events attach under this spawn node.
        // Copy the actor to a local BEFORE the second agUpsert — it push_backs into the node
        // vector, which can reallocate and dangle the `n` reference above. (Only matters once a
        // real spawn sink lands; harmless on the empty path today, but correct now.)
        const std::string spawnActor = n.actor;
        const std::string childId = agEnvStr(data, "subagent_id");
        if (!childId.empty()) {
            s_agdag.nodes[s_agdag.byId[key]].refId = childId;  // M11: click → open its chat
            AgNode& c = agUpsert(childId, key);
            if (c.title.empty()) {
                c.kind = AgKind::Root; c.actor = spawnActor; c.title = "subagent " + spawnActor;
            }
            c.refId = childId;
        }
        s_agdag.sawSpawn = true;
    } else if (kind == "agent.advisor") {
        AgNode& n = agUpsert(key, parentId);
        n.kind   = AgKind::Advisor;
        n.actor  = actor;
        n.title  = "advisor: " + agEnvStr(data, "advisor_kind");
        n.detail = agEnvStr(data, "verdict_summary");
        s_agdag.sawAdvisor = true;
    }
    // Any other agent.* kind (artifact/question/answer) is tolerated but not graphed here.
}

// Serialize the live Agent Workflow DAG store (s_agdag) — every field the canvas draws.
nlohmann::json agentDagToJson() {
    nlohmann::json j;
    j["runId"]        = s_agdag.runId;
    j["seqCtr"]       = s_agdag.seqCtr;
    j["sawSpawn"]     = s_agdag.sawSpawn;
    j["sawAdvisor"]   = s_agdag.sawAdvisor;
    j["toolCount"]    = s_agdag.toolCount;
    j["thoughtCount"] = s_agdag.thoughtCount;
    j["ctxCount"]     = s_agdag.ctxCount;
    nlohmann::json arr = nlohmann::json::array();
    for (const AgNode& n : s_agdag.nodes) {
        nlohmann::json jn;
        jn["id"]        = n.id;
        jn["parentId"]  = n.parentId;
        jn["kind"]      = agKindName(n.kind);
        jn["actor"]     = n.actor;
        jn["title"]     = n.title;
        jn["detail"]    = n.detail;
        jn["status"]    = n.status;
        jn["refId"]     = n.refId;
        jn["latencyMs"] = n.latencyMs;
        jn["seq"]       = n.seq;
        arr.push_back(std::move(jn));
    }
    j["nodes"] = std::move(arr);
    return j;
}

// Restore a serialized tree (agentDagToJson) into the store. Rebuilds the id index; tree
// links (children/depth) are re-derived by agResolveTree at draw time.
void agentDagFromJson(const nlohmann::json& j) {
    if (!j.is_object()) return;
    s_agdag.reset(j.value("runId", std::string()));
    s_agdag.sawSpawn     = j.value("sawSpawn", false);
    s_agdag.sawAdvisor   = j.value("sawAdvisor", false);
    s_agdag.toolCount    = j.value("toolCount", 0);
    s_agdag.thoughtCount = j.value("thoughtCount", 0);
    s_agdag.ctxCount     = j.value("ctxCount", 0);
    if (j.contains("nodes") && j["nodes"].is_array()) {
        for (const auto& jn : j["nodes"]) {
            if (!jn.is_object()) continue;
            AgNode n;
            n.id        = jn.value("id", "");
            n.parentId  = jn.value("parentId", "");
            n.kind      = agKindFromName(jn.value("kind", std::string("other")));
            n.actor     = jn.value("actor", "");
            n.title     = jn.value("title", "");
            n.detail    = jn.value("detail", "");
            n.status    = jn.value("status", "");
            n.refId     = jn.value("refId", "");
            n.latencyMs = jn.value("latencyMs", -1);
            n.seq       = jn.value("seq", (long long)0);
            if (n.id.empty()) n.id = "_leaf" + std::to_string(n.seq);
            if (s_agdag.byId.count(n.id)) continue;   // corrupt duplicate id — skip
            s_agdag.byId[n.id] = (int)s_agdag.nodes.size();
            s_agdag.nodes.push_back(std::move(n));
        }
    }
    // Restore the arrival counter past every restored seq so new events keep ordering.
    long long maxSeq = -1;
    for (const AgNode& n : s_agdag.nodes) maxSeq = std::max(maxSeq, n.seq);
    s_agdag.seqCtr = std::max(j.value("seqCtr", (long long)0), maxSeq + 1);
}

// Reopen the file-level anonymous namespace for the remaining internal helpers/renderers.
namespace {

// ---- shared little helpers (same visual grammar as parts/plm workspaces) --------

static void sectionHeader(const char* label) {
    const float S = ui::uiScale;
    ImGui::TextUnformatted(label);
    ImVec2 p0 = ImGui::GetItemRectMin();
    ImVec2 p1 = ImGui::GetItemRectMax();
    ImGui::GetWindowDrawList()->AddLine(
        ImVec2(p0.x, p1.y + 1.0f * S), ImVec2(p1.x, p1.y + 1.0f * S),
        ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.60f)),
        1.0f * S);
    ImGui::Spacing();
}

static void emptyState(const char* icon, const char* msg, const char* sub = nullptr) {
    const float S = ui::uiScale;
    float avail = ImGui::GetContentRegionAvail().y;
    ImGui::Dummy(ImVec2(0, avail * 0.35f));

    float tw = ImGui::CalcTextSize(icon).x;
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                         (ImGui::GetContentRegionAvail().x - tw) * 0.5f);
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
               palette.textDisabled[2], 0.5f));
    ImGui::TextUnformatted(icon);
    ImGui::PopStyleColor();
    ImGui::Spacing();

    {
        float mw = ImGui::CalcTextSize(msg).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - mw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextUnformatted(msg);
        ImGui::PopStyleColor();
    }
    if (sub) {
        // Wrap-aware centering: long sub lines wrap inside the pane instead of clipping off the
        // left edge (M21 — the DAG pane is narrower now that the chat column sits beside it).
        float avail2 = ImGui::GetContentRegionAvail().x;
        float wrapW  = std::min(avail2 - 16.0f * S, 460.0f * S);
        float sw     = ImGui::CalcTextSize(sub, nullptr, false, wrapW).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             std::max(0.0f, (avail2 - sw) * 0.5f));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.8f));
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + wrapW);
        ImGui::TextWrapped("%s", sub);
        ImGui::PopTextWrapPos();
        ImGui::PopStyleColor();
    }
    ImGui::Dummy(ImVec2(0, 8.0f * S));
}

// ---- DAG run catalog (persisted sidecars — the store dag_status reads) ----------

static std::string runsDir() {
    // appdataAppDir() applies the Chudware alias rule on both platforms.
    return appdataAppDir() + "/runs";
}

static bool loadRunGraph(const DagRun& run);   // fwd

static void scanRuns() {
    namespace fs = std::filesystem;
    s_ai.runsScanned = true;
    s_ai.runs.clear();
    s_ai.selRun = -1;

    std::error_code ec;
    fs::path dir(runsDir());
    if (dir.empty() || !fs::exists(dir, ec)) return;

    static const char kSuffix[] = ".buildgraph.json";
    for (const auto& e : fs::directory_iterator(dir, ec)) {
        if (!e.is_regular_file(ec)) continue;
        std::string name = e.path().filename().string();
        if (name.size() <= sizeof(kSuffix) - 1 ||
            name.compare(name.size() - (sizeof(kSuffix) - 1), sizeof(kSuffix) - 1,
                         kSuffix) != 0)
            continue;
        DagRun r;
        r.runId = name.substr(0, name.size() - (sizeof(kSuffix) - 1));
        r.path  = e.path().string();
        auto t  = fs::last_write_time(e.path(), ec);
        r.mtime = (long long)t.time_since_epoch().count();
        r.nodeCount = -1;  // unknown until parsed
        s_ai.runs.push_back(std::move(r));
    }
    std::sort(s_ai.runs.begin(), s_ai.runs.end(),
              [](const DagRun& a, const DagRun& b) { return a.mtime > b.mtime; });
    if (s_ai.runs.size() > 25) s_ai.runs.resize(25);

    // Fill node counts (sidecars are small) and default-select the newest non-empty plan.
    for (size_t i = 0; i < s_ai.runs.size(); ++i) {
        try {
            std::ifstream f(s_ai.runs[i].path);
            if (!f) continue;
            nlohmann::json j = nlohmann::json::parse(f, nullptr, false);
            if (j.is_discarded()) continue;
            const auto& g = j.contains("graph") ? j["graph"] : j;
            if (g.is_object()) {
                s_ai.runs[i].mission   = g.value("mission", "");
                s_ai.runs[i].nodeCount = g.contains("nodes") && g["nodes"].is_array()
                                             ? (int)g["nodes"].size() : 0;
            }
        } catch (...) { /* corrupt sidecar — leave unknown */ }
        if (s_ai.selRun < 0 && s_ai.runs[i].nodeCount > 0) s_ai.selRun = (int)i;
    }
    if (s_ai.selRun >= 0) loadRunGraph(s_ai.runs[s_ai.selRun]);
}

// Longest-path layering (requires -> deps left of dependents), rows per layer.
static void layoutDag(std::vector<DagNode>& nodes) {
    std::unordered_map<std::string, int> idx;
    for (size_t i = 0; i < nodes.size(); ++i) idx[nodes[i].id] = (int)i;
    for (auto& n : nodes) n.layer = 0;
    // Relaxation passes (bounded — plan graphs are small; cycles just stop improving).
    for (size_t pass = 0; pass < nodes.size() + 1; ++pass) {
        bool changed = false;
        for (auto& n : nodes)
            for (const auto& dep : n.requires) {
                auto it = idx.find(dep);
                if (it == idx.end()) continue;
                int want = nodes[it->second].layer + 1;
                if (n.layer < want) { n.layer = want; changed = true; }
            }
        if (!changed) break;
    }
    std::unordered_map<int, int> rowInLayer;
    for (auto& n : nodes) n.row = rowInLayer[n.layer]++;
}

static bool loadRunGraph(const DagRun& run) {
    s_ai.loadedRun = run.runId;
    s_ai.nodes.clear();
    s_ai.loadErr.clear();
    s_ai.pan  = ImVec2(0, 0);
    s_ai.zoom = 1.0f;
    try {
        std::ifstream f(run.path);
        if (!f) { s_ai.loadErr = "Cannot open " + run.path; return false; }
        nlohmann::json j = nlohmann::json::parse(f, nullptr, false);
        if (j.is_discarded()) { s_ai.loadErr = "Corrupt plan sidecar."; return false; }
        const auto& g = j.contains("graph") ? j["graph"] : j;
        if (!g.is_object() || !g.contains("nodes") || !g["nodes"].is_array()) {
            s_ai.loadErr = "Plan sidecar has no nodes.";
            return false;
        }
        for (const auto& jn : g["nodes"]) {
            DagNode n;
            n.id     = jn.value("id",     "");
            n.kind   = jn.value("kind",   "");
            n.label  = jn.value("label",  n.id);
            n.status = jn.value("status", "pending");
            if (jn.contains("assigned") && jn["assigned"].is_string())
                n.assigned = jn["assigned"].get<std::string>();
            if (jn.contains("requires") && jn["requires"].is_array())
                for (const auto& r : jn["requires"])
                    if (r.is_string()) n.requires.push_back(r.get<std::string>());
            s_ai.nodes.push_back(std::move(n));
        }
        layoutDag(s_ai.nodes);
        return true;
    } catch (const std::exception& e) {
        s_ai.loadErr = std::string("Plan load failed: ") + e.what();
        return false;
    }
}

// Ask the worker about the selected run (proves the dag_status seam; surfaces the
// events trail + checkpoint presence). Async — result lands in s_ai.statusLine.
static void probeDagStatus(AppState& st, const std::string& runId) {
    if (!st.workerStarted || s_ai.statusPending) return;
    s_ai.statusRun     = runId;
    s_ai.statusPending = true;
    s_ai.statusOk      = false;
    s_ai.statusLine    = "Asking worker (dag_status)...";
    nlohmann::json p;
    p["run_id"] = runId;
    st.rpc.call("dag_status", p, [runId](const core::RpcResult& r) {
        if (s_ai.statusRun != runId) return;  // stale — user switched runs
        s_ai.statusPending = false;
        if (!r.ok) { s_ai.statusLine = "Worker: " + (r.err.empty() ? "unreachable" : r.err); return; }
        if (r.result.value("ok", false)) {
            s_ai.statusOk = true;
            int n = r.result.value("node_count", 0);
            bool trail = !r.result.value("events_jsonl", std::string()).empty();
            char buf[160];
            std::snprintf(buf, sizeof(buf), "Worker: run live — %d node%s%s", n,
                          n == 1 ? "" : "s", trail ? ", event trail on disk" : "");
            s_ai.statusLine = buf;
        } else {
            s_ai.statusLine = "Worker: " + r.result.value("reason", std::string("unknown run"));
        }
    });
}

static void steerRun(AppState& st, const std::string& runId, bool approve) {
    if (!st.workerStarted) return;
    s_ai.statusRun  = runId;
    s_ai.statusLine = approve ? "Approving gate..." : "Aborting run...";
    nlohmann::json p;
    p["run_id"] = runId;
    st.rpc.call(approve ? "dag_approve" : "dag_abort", p,
                [runId](const core::RpcResult& r) {
        if (s_ai.statusRun != runId) return;
        if (r.ok && r.result.value("ok", false))
            s_ai.statusLine = "Worker: " + r.result.value("status", std::string("resumed"));
        else {
            std::string why = r.ok ? r.result.value("reason", std::string("steer failed"))
                                   : r.err;
            s_ai.statusLine = "Worker: " + (why.empty() ? "steer failed" : why);
        }
        // Statuses may have advanced on disk — reload the sidecar next frame.
        s_ai.loadedRun.clear();
    });
}

static const float* statusColor(const std::string& s) {
    if (s == "done" || s == "complete" || s == "succeeded") return palette.statusOk;
    if (s == "failed" || s == "error")                      return palette.statusErr;
    if (s == "running" || s == "in_progress")               return palette.accent;
    if (s == "blocked" || s == "paused" || s == "waiting")  return palette.statusWarn;
    return palette.textDisabled;  // pending / unknown
}

// ==================================================================================
// v3 Agent Hub — sub-agent rows + POPUP sub-agent chats.
//
// Data source: the file-backed sub-agent session registry served by the worker's
// subagent_list / subagent_transcript / subagent_steer / subagent_terminate RPCs
// (services/agent/subagent_rpc.py) — ALL async via st.rpc (never a sync worker.call
// in a draw path). Steering semantics are queued-and-consumed-at-turn-boundary
// (agent_turn.run_turn drains the queue before each model call); the UI says so.
// Each OPEN sub-agent chat is an independent floating window keyed by sub-agent id —
// several can be open at once ("have them all"), each themed by its role color.
// Closing a window NEVER ends the sub-agent; terminate stays confirm-gated (M16).
// HONESTY: with no spawned sub-agents the registry is empty and every surface shows
// its empty-state — nothing is fabricated.
// ==================================================================================

struct SubMsg {
    std::string who;    // "user" | "subagent" | "system"
    std::string text;
    bool        queued = false;   // a steer mirrored into the transcript, not yet consumed
};

struct SubagentSession {
    std::string id, role, brief, status, model, spawnKind;
    std::string createdTs, updatedTs;   // ISO-8601 UTC (registry created_ts / updated_ts)
    int budgetTokens = 0, pendingSteers = 0, transcriptLen = 0;
};

// Registry snapshot (the Agent Hub row list) — global, one poll loop.
struct SubagentRegistry {
    std::vector<SubagentSession> sessions;
    bool   listPending = false;
    bool   listedOnce  = false;
    double lastListT   = 0.0;
    std::string listErr;
    // Ids seen by the auto-pop logic — a NEW live sub-agent pops its chat window once
    // (agent.ui_follow_active_session); parked/finished ones never auto-pop.
    std::set<std::string> seenIds;
    int                 autoPopped  = 0;      // cap auto-popups so a big fan-out stays usable
    bool                needAutoPop = false;  // set by the list callback; consumed on draw
};
static SubagentRegistry s_sub;

// One floating chat window per open sub-agent.
struct SubChatState {
    SubagentSession cur;
    std::vector<SubMsg> msgs;
    bool   trPending = false;
    double lastTrT   = 0.0;
    std::string viewErr;

    // steering composer
    char        steerBuf[2048] = {0};
    bool        steerPending   = false;
    std::string steerNote;      // the worker's honest injection-semantics note

    bool termPending = false;
};
static std::unordered_map<std::string, SubChatState> s_subChats;

// Role color (v3): a stable hue per role string so a sub-agent's row, popup chrome and chat
// accents all share one identity color. FNV-1a -> hue; fixed sat/val keeps it legible on glass.
static void roleColorRgb(const std::string& role, float out[3]) {
    uint32_t h = 2166136261u;
    for (unsigned char c : role) { h ^= c; h *= 16777619u; }
    const float hue = (float)(h % 360u) / 360.0f;
    ImGui::ColorConvertHSVtoRGB(hue, 0.58f, 0.92f, out[0], out[1], out[2]);
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
    o.model         = s.value("model", "");
    o.spawnKind     = s.value("spawn_kind", "");
    o.createdTs     = s.value("created_ts", "");
    o.updatedTs     = s.value("updated_ts", "");
    o.budgetTokens  = s.value("budget_tokens", 0);
    o.pendingSteers = s.value("pending_steers", 0);
    o.transcriptLen = s.value("transcript_len", 0);
    return o;
}

// fwd — the popup opener is defined with the popup windows below; the auto-pop pass in
// pollSubagentList needs it.
static void openSubagentView(AppState& st, const std::string& subagentId,
                             const std::string& roleHint = std::string());

// Auto-pop pass (runs on the draw thread after a completed list poll): a sub-agent that is
// NEW since the last poll AND live pops its chat window on its own ("pop up chat views for
// the active subagents … have them all"). Parked/finished history is only marked seen.
static void autoPopNewSubagents(AppState& st) {
    for (const auto& s : s_sub.sessions) {
        if (s_sub.seenIds.count(s.id)) continue;
        s_sub.seenIds.insert(s.id);
        const bool live = s.status == "running" || s.status == "pending" ||
                          s.status == "in_progress" || s.status == "waiting";
        if (!live) continue;                      // parked history never auto-pops
        if (!aiFollowActiveSession()) continue;   // user turned autopopulate off
        if (s_subChats.count(s.id)) continue;     // already open
        if (s_sub.autoPopped >= 6) continue;      // cap — a huge fan-out stays usable
        ++s_sub.autoPopped;
        openSubagentView(st, s.id, s.role);
    }
}

// Poll the registry snapshot (Agent Hub rows). Cheap file-glob on the worker side;
// throttled to one in-flight call every few seconds while the workspace is drawn.
static void pollSubagentList(AppState& st) {
    if (s_sub.needAutoPop && !s_sub.listPending) {
        s_sub.needAutoPop = false;
        autoPopNewSubagents(st);
    }
    if (!st.workerStarted || s_sub.listPending) return;
    const double now = ImGui::GetTime();
    if (s_sub.listedOnce && now - s_sub.lastListT < 4.0) return;
    s_sub.listPending = true;
    s_sub.lastListT   = now;
    st.rpc.call("subagent_list", nlohmann::json::object(),
                [](const core::RpcResult& r) {
        s_sub.listPending = false;
        s_sub.listedOnce  = true;
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
        s_sub.needAutoPop = true;   // auto-pop pass runs on the next draw (has AppState)
    });
}

// Fetch one open popup's transcript (+ refreshed session meta). Throttled per window.
static void fetchSubagentTranscript(AppState& st, SubChatState& cs, bool force = false) {
    if (!st.workerStarted || cs.trPending) return;
    const double now = ImGui::GetTime();
    if (!force && now - cs.lastTrT < 2.0) return;
    cs.trPending = true;
    cs.lastTrT   = now;
    nlohmann::json p;
    p["subagent_id"] = cs.cur.id;
    const std::string want = cs.cur.id;
    st.rpc.call("subagent_transcript", p, [want](const core::RpcResult& r) {
        auto it = s_subChats.find(want);
        if (it == s_subChats.end()) return;   // window closed while in flight
        SubChatState& cs = it->second;
        cs.trPending = false;
        if (!r.ok) { cs.viewErr = r.err.empty() ? "worker unreachable" : r.err; return; }
        if (!r.result.value("ok", false)) {
            cs.viewErr = r.result.value("reason", std::string("transcript unavailable"));
            return;
        }
        cs.viewErr.clear();
        if (r.result.contains("session") && r.result["session"].is_object())
            cs.cur = parseSubSession(r.result["session"]);
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

static void sendSubagentSteer(AppState& st, SubChatState& cs) {
    if (!st.workerStarted || cs.steerPending) return;
    std::string text(cs.steerBuf);
    if (text.find_first_not_of(" \t\r\n") == std::string::npos) return;
    cs.steerPending = true;
    nlohmann::json p;
    p["subagent_id"] = cs.cur.id;
    p["text"]        = text;
    const std::string want = cs.cur.id;
    st.rpc.call("subagent_steer", p, [want](const core::RpcResult& r) {
        auto it = s_subChats.find(want);
        if (it == s_subChats.end()) return;
        SubChatState& cs = it->second;
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
    if (!st.workerStarted || cs.termPending) return;
    cs.termPending = true;
    nlohmann::json p;
    p["subagent_id"] = cs.cur.id;
    p["confirm"]     = true;   // the GUI confirm dialog IS the M16 gate; the RPC re-checks it
    const std::string want = cs.cur.id;
    st.rpc.call("subagent_terminate", p, [want](const core::RpcResult& r) {
        auto it = s_subChats.find(want);
        if (it == s_subChats.end()) return;
        SubChatState& cs = it->second;
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

// ==================================================================================
// v3 popup sub-agent chats — one floating, role-themed window per open sub-agent.
// Several can be open at once; they hover over the utility-panel area (s_popupAnchor).
// Closing a window NEVER ends the sub-agent (M16); terminate stays confirm-gated.
// ==================================================================================

static ImVec2     s_popupAnchor{120.0f, 120.0f};  // set each frame by drawAiWorkspace
static std::string s_focusNext;                   // window to raise next frame

// v3: open (or raise) a sub-agent's POPUP chat window. Never touches the main chat.
static void openSubagentView(AppState& st, const std::string& subagentId,
                             const std::string& roleHint) {
    if (subagentId.empty()) return;
    auto [it, inserted] = s_subChats.try_emplace(subagentId);
    SubChatState& cs = it->second;
    if (inserted) {
        cs.cur.id   = subagentId;
        cs.cur.role = roleHint;
    } else if (cs.cur.role.empty() && !roleHint.empty()) {
        cs.cur.role = roleHint;
    }
    // Prefer the registry snapshot for instant header data.
    for (const auto& s : s_sub.sessions)
        if (s.id == subagentId) { cs.cur = s; break; }
    fetchSubagentTranscript(st, cs, /*force=*/true);
    s_focusNext = subagentId;   // raise even if the window was already open
}

// One popup's full content: role-tinted chrome, read-only run parameters (M17), live
// transcript, steering composer (queued injections), confirm-gated terminate (M16).
// Returns false when the user closed the window (erase after the draw loop).
static bool drawSubagentChatWindow(AppState& st, SubChatState& cs, int cascadeIdx) {
    const float S = ui::uiScale;
    const std::string roleKey = cs.cur.role.empty() ? cs.cur.id : cs.cur.role;
    float rc[3];
    roleColorRgb(roleKey, rc);
    const float* sc = statusColor(cs.cur.status);

    fetchSubagentTranscript(st, cs);

    // Role-themed chrome: the window bg + title bar carry the role identity color (M15
    // evolved — unmistakable WHICH sub-agent a chat belongs to, several open at once).
    auto mix = [&](const float base[4], float k, float a) {
        return ImVec4(base[0] * (1.0f - k) + rc[0] * k,
                      base[1] * (1.0f - k) + rc[1] * k,
                      base[2] * (1.0f - k) + rc[2] * k, a);
    };
    ImGui::PushStyleColor(ImGuiCol_WindowBg,       mix(palette.popupBg, 0.16f, 0.98f));
    ImGui::PushStyleColor(ImGuiCol_TitleBg,        mix(palette.headerBg, 0.30f, 0.92f));
    ImGui::PushStyleColor(ImGuiCol_TitleBgActive,  mix(palette.headerBg, 0.45f, 1.0f));
    ImGui::PushStyleColor(ImGuiCol_Border,         ImVec4(rc[0], rc[1], rc[2], 0.55f));

    std::string shortId = cs.cur.id.size() > 10 ? cs.cur.id.substr(0, 10) : cs.cur.id;
    std::string title = roleKey + "  ·  " + shortId + "###subchat_" + cs.cur.id;
    const float step  = 28.0f * S;
    ImGui::SetNextWindowPos(
        ImVec2(s_popupAnchor.x + step * (float)(cascadeIdx % 6),
               s_popupAnchor.y + step * (float)(cascadeIdx % 6)),
        ImGuiCond_Appearing);
    ImGui::SetNextWindowSize(ImVec2(470.0f * S, 540.0f * S), ImGuiCond_FirstUseEver);

    bool open = true;
    if (ImGui::Begin(title.c_str(), &open, ImGuiWindowFlags_NoCollapse)) {
        if (s_focusNext == cs.cur.id) { ImGui::SetWindowFocus(); s_focusNext.clear(); }

        // --- header strip: status chip + status + age ------------------------------
        {
            ImVec2 p0 = ImGui::GetCursorScreenPos();
            ImVec2 p1(p0.x + ImGui::GetContentRegionAvail().x,
                      p0.y + ImGui::GetFrameHeight() + 6.0f * S);
            ImDrawList* dl = ImGui::GetWindowDrawList();
            dl->AddRectFilled(p0, p1, ImGui::ColorConvertFloat4ToU32(
                ImVec4(rc[0], rc[1], rc[2], 0.14f)), 6.0f * S);
            dl->AddRect(p0, p1, ImGui::ColorConvertFloat4ToU32(
                ImVec4(rc[0], rc[1], rc[2], 0.55f)), 6.0f * S, 0, 1.0f * S);
            ImGui::SetCursorScreenPos(ImVec2(p0.x + 8.0f * S, p0.y + 4.0f * S));
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(rc[0], rc[1], rc[2], 1.0f));
            ImGui::TextUnformatted("SUB-AGENT");
            ImGui::PopStyleColor();
            ImGui::SameLine(0, 10.0f * S);
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(sc[0], sc[1], sc[2], 1.0f));
            ImGui::TextUnformatted(cs.cur.status.empty() ? "status unknown"
                                                         : cs.cur.status.c_str());
            ImGui::PopStyleColor();
            const std::string age = fmtAge(cs.cur.updatedTs);
            if (!age.empty()) {
                ImGui::SameLine(0, 10.0f * S);
                ImGui::PushStyleColor(ImGuiCol_Text,
                    ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                           palette.textDisabled[2], 1.0f));
                ImGui::TextUnformatted(age.c_str());
                ImGui::PopStyleColor();
            }
            ImGui::SetCursorScreenPos(ImVec2(p0.x, p1.y + 6.0f * S));
        }

        // --- read-only run parameters (M17) ----------------------------------------
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::Text("Model: %s", cs.cur.model.empty()
                                     ? "inherited from parent mission (read-only)"
                                     : (cs.cur.model + "  (read-only)").c_str());
        ImGui::Text("Effort / budget: %s%s",
                    cs.cur.budgetTokens > 0
                        ? (std::to_string(cs.cur.budgetTokens) + " tokens").c_str()
                        : "inherited",
                    "  (read-only)");
        if (!cs.cur.brief.empty()) {
            ImGui::TextUnformatted("Starting prompt (read-only):");
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 1.0f));
            ImGui::TextWrapped("%s", cs.cur.brief.c_str());
            ImGui::PopStyleColor();
        }
        ImGui::PopStyleColor();
        ImGui::Spacing();

        // --- transcript -------------------------------------------------------------
        const float composerH = ImGui::GetFrameHeightWithSpacing() * 4.6f;
        float logH = ImGui::GetContentRegionAvail().y - composerH;
        if (logH < 90.0f * S) logH = 90.0f * S;
        ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(rc[0], rc[1], rc[2], 0.06f));
        ImGui::BeginChild("##subagent_log", ImVec2(0, logH), ImGuiChildFlags_None);
        if (!cs.viewErr.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.statusErr[0], palette.statusErr[1],
                       palette.statusErr[2], 1.0f));
            ImGui::TextWrapped("%s", cs.viewErr.c_str());
            ImGui::PopStyleColor();
        }
        if (cs.msgs.empty() && cs.viewErr.empty()) {
            emptyState("[sa]", cs.trPending ? "Loading transcript\xE2\x80\xA6" : "No transcript yet",
                       "This sub-agent has not produced any messages yet. Its replies and your "
                       "steering messages appear here.");
        }
        for (const auto& m : cs.msgs) {
            const float* c = palette.text;
            const char*  tag = "agent";
            if (m.who == "user")        { c = palette.accent;       tag = "you"; }
            else if (m.who == "system") { c = palette.textDisabled; tag = "sys"; }
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(rc[0], rc[1], rc[2], m.who == "system" ? 0.6f : 1.0f));
            ImGui::TextUnformatted(tag);
            ImGui::PopStyleColor();
            ImGui::SameLine(0, 6.0f * S);
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(c[0], c[1], c[2], m.who == "system" ? 0.9f : 1.0f));
            if (m.queued) {
                ImGui::TextDisabled("(queued)");
                ImGui::SameLine(0, 6.0f * S);
            }
            ImGui::TextWrapped("%s", m.text.c_str());
            ImGui::PopStyleColor();
            ImGui::Spacing();
        }
        if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 12) ImGui::SetScrollHereY(1.0f);
        ImGui::EndChild();
        ImGui::PopStyleColor();

        // --- steering composer (queued injections) ----------------------------------
        ImGui::Spacing();
        ImGui::InputTextMultiline("##subagent_steer", cs.steerBuf, sizeof(cs.steerBuf),
                                  ImVec2(-1.0f, ImGui::GetFrameHeight() * 1.9f));
        const bool canSteer = st.workerStarted && !cs.steerPending &&
                              cs.steerBuf[0] != '\0';
        ImGui::BeginDisabled(!canSteer);
        if (ImGui::SmallButton(cs.steerPending ? "Queuing\xE2\x80\xA6" : "Steer"))
            sendSubagentSteer(st, cs);
        ImGui::EndDisabled();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Inject a steering message. It is queued and read by the sub-agent "
                              "at its next turn boundary (before its next model call).");
        ImGui::SameLine();
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        if (cs.cur.pendingSteers > 0)
            ImGui::Text("%d steer%s pending", cs.cur.pendingSteers,
                        cs.cur.pendingSteers == 1 ? "" : "s");
        else
            ImGui::TextUnformatted("applied at the sub-agent's next turn boundary");
        ImGui::PopStyleColor();
        // M16: closing the window never ends the sub-agent; terminate is deliberately small,
        // off to the side, and gated behind an explicit confirm dialog.
        ImGui::SameLine();
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             std::max(0.0f, ImGui::GetContentRegionAvail().x - 88.0f * S));
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        if (ImGui::SmallButton("Terminate\xE2\x80\xA6")) ImGui::OpenPopup("##subagent_term_confirm");
        ImGui::PopStyleColor();
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Stop this sub-agent (requires confirmation). It halts cleanly at "
                              "its next turn boundary — never mid-action.");
        if (ImGui::BeginPopupModal("##subagent_term_confirm", nullptr,
                                   ImGuiWindowFlags_AlwaysAutoResize)) {
            ImGui::TextUnformatted("Terminate this sub-agent?");
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::TextWrapped("The sub-agent stops cleanly at its next turn boundary. Work it has "
                               "already handed off is kept.");
            ImGui::PopStyleColor();
            ImGui::Spacing();
            if (ImGui::Button("Terminate")) { requestSubagentTerminate(st, cs); ImGui::CloseCurrentPopup(); }
            ImGui::SameLine();
            if (ImGui::Button("Keep running")) ImGui::CloseCurrentPopup();
            ImGui::EndPopup();
        }
        if (!cs.steerNote.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text,
                ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
            ImGui::TextWrapped("%s", cs.steerNote.c_str());
            ImGui::PopStyleColor();
        }
        // Honest affordance note: the X closes the VIEW only.
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.9f));
        ImGui::TextUnformatted("Closing this window never stops the sub-agent.");
        ImGui::PopStyleColor();
    }
    ImGui::End();
    ImGui::PopStyleColor(4);
    return open;
}

// Draw every open popup; erase the ones closed this frame (after the loop — never mutate
// the map mid-iteration).
static void drawSubagentPopups(AppState& st) {
    if (s_subChats.empty()) return;
    std::vector<std::string> closeIds;
    int idx = 0;
    for (auto& kv : s_subChats) {
        if (!drawSubagentChatWindow(st, kv.second, idx++))
            closeIds.push_back(kv.first);
    }
    for (const auto& id : closeIds) s_subChats.erase(id);
}

// ---- Agent Workflow DAG (Phase 2, M9/M10) ---------------------------------------
// A collapsible, scalable execution tree built from the live event store (s_agdag). Reuses the
// pan/zoom/clip/Bézier scaffolding grammar of drawDagCanvas but with its OWN layout so M10 can
// hide collapsed subtrees BEFORE laying out (the buildgraph canvas has no collapse concept).

static const float* agNodeColor(const AgNode& n) {
    switch (n.kind) {
        case AgKind::Tool:    return n.status == "failed" ? palette.statusErr : palette.statusOk;
        case AgKind::Thought: return palette.accent;
        case AgKind::Context: return palette.statusWarn;
        case AgKind::Spawn:   return palette.accent;
        case AgKind::Advisor: return palette.accent;
        case AgKind::Root:    return palette.textDim;
        default:              return palette.textDisabled;
    }
}

static const char* agKindTag(AgKind k) {
    switch (k) {
        case AgKind::Tool:    return "tool";
        case AgKind::Thought: return "think";
        case AgKind::Context: return "context";
        case AgKind::Spawn:   return "subagent";
        case AgKind::Advisor: return "advisor";
        case AgKind::Root:    return "run";
        default:              return "node";
    }
}

// Resolve parent->children links + depth from the flat node list (join parent_id -> id, §2.1).
// Nodes whose parent is absent become roots. Cheap enough to run every frame (tens of nodes).
static void agResolveTree(std::vector<int>& roots) {
    for (auto& n : s_agdag.nodes) { n.children.clear(); n.depth = 0; }
    roots.clear();
    for (size_t i = 0; i < s_agdag.nodes.size(); ++i) {
        const std::string& pid = s_agdag.nodes[i].parentId;
        auto it = pid.empty() ? s_agdag.byId.end() : s_agdag.byId.find(pid);
        if (it == s_agdag.byId.end() || it->second == (int)i) roots.push_back((int)i);
        else s_agdag.nodes[it->second].children.push_back((int)i);
    }
    // Depth via BFS from roots (advisory ordering; ts/seq keep siblings stable).
    std::vector<int> stack(roots.begin(), roots.end());
    std::vector<char> seen(s_agdag.nodes.size(), 0);
    for (int r : roots) seen[r] = 1;
    while (!stack.empty()) {
        int cur = stack.back(); stack.pop_back();
        for (int c : s_agdag.nodes[cur].children) {
            if (seen[c]) continue;
            seen[c] = 1;
            s_agdag.nodes[c].depth = s_agdag.nodes[cur].depth + 1;
            stack.push_back(c);
        }
    }
}

// M10: flatten to the VISIBLE set (stop descending at a collapsed node) in tree order, assigning
// each visible node a (layer=depth, row=running counter) grid slot. Returns visible node indices.
static void agFlattenVisible(const std::vector<int>& roots, std::vector<int>& visible,
                             std::unordered_map<int,int>& rowOf) {
    visible.clear(); rowOf.clear();
    // Sort roots + children by seq for stable, arrival-ordered rows.
    auto bySeq = [](const std::vector<int>&) {};
    (void)bySeq;
    std::vector<int> order;
    // Iterative pre-order DFS honoring collapse.
    std::vector<int> stack(roots.rbegin(), roots.rend());
    int row = 0;
    while (!stack.empty()) {
        int cur = stack.back(); stack.pop_back();
        visible.push_back(cur);
        rowOf[cur] = row++;
        if (!s_agdag.nodes[cur].collapsed) {
            // push children in reverse-seq so they pop in seq order
            std::vector<int> kids = s_agdag.nodes[cur].children;
            std::sort(kids.begin(), kids.end(), [](int a, int b) {
                return s_agdag.nodes[a].seq < s_agdag.nodes[b].seq;
            });
            for (auto rit = kids.rbegin(); rit != kids.rend(); ++rit) stack.push_back(*rit);
        }
    }
}

// verbosity (agent.ui_verbosity): 0 = title only, 1 = + detail line, 2 = + actor / latency /
// generous detail budget (the footer counts live in the view header above the canvas).
static void drawAgentDagCanvas(AppState& st, int verbosity) {
    const float S = ui::uiScale;

    std::vector<int> roots;
    agResolveTree(roots);
    std::sort(roots.begin(), roots.end(),
              [](int a, int b) { return s_agdag.nodes[a].seq < s_agdag.nodes[b].seq; });
    std::vector<int> visible;
    std::unordered_map<int,int> rowOf;
    agFlattenVisible(roots, visible, rowOf);

    ImGui::BeginChild("##ag_dag_canvas", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    const ImVec2 cMin = ImGui::GetWindowPos();
    const ImVec2 cSz  = ImGui::GetWindowSize();
    const ImVec2 cMax(cMin.x + cSz.x, cMin.y + cSz.y);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(cMin, cMax, true);

    // Whole-canvas pan/zoom surface (reuses the s_ai.pan/zoom camera — shared between DAG views).
    ImGui::InvisibleButton("##ag_dag_nav", cSz,
                           ImGuiButtonFlags_MouseButtonLeft | ImGuiButtonFlags_MouseButtonMiddle);
    bool navActive = ImGui::IsItemActive();
    if (navActive) {
        s_ai.pan.x += ImGui::GetIO().MouseDelta.x;
        s_ai.pan.y += ImGui::GetIO().MouseDelta.y;
    }
    if (ImGui::IsItemHovered()) {
        float wheel = ImGui::GetIO().MouseWheel;
        if (wheel != 0.0f) {
            float oldZ = s_ai.zoom;
            s_ai.zoom = std::min(2.5f, std::max(0.35f, s_ai.zoom * (1.0f + wheel * 0.1f)));
            ImVec2 m = ImGui::GetIO().MousePos;
            ImVec2 local(m.x - cMin.x - s_ai.pan.x, m.y - cMin.y - s_ai.pan.y);
            s_ai.pan.x -= local.x * (s_ai.zoom / oldZ - 1.0f);
            s_ai.pan.y -= local.y * (s_ai.zoom / oldZ - 1.0f);
        }
    }

    const float z     = s_ai.zoom;
    const float nodeW = 200.0f * S * z, nodeH = 44.0f * S * z;
    const float gapX  = 46.0f * S * z,  gapY  = 12.0f * S * z;
    // layer = depth (indent by tree level), row = pre-order counter.
    auto slotPos = [&](int nodeIdx) {
        const AgNode& n = s_agdag.nodes[nodeIdx];
        int rr = rowOf.count(nodeIdx) ? rowOf[nodeIdx] : 0;
        return ImVec2(cMin.x + s_ai.pan.x + 16.0f * S + n.depth * (32.0f * S * z),
                      cMin.y + s_ai.pan.y + 16.0f * S + rr * (nodeH + gapY));
    };

    // Edges (parent right/bottom -> child left) behind boxes.
    const ImU32 edgeCol = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 0.55f));
    for (int idx : visible) {
        const AgNode& n = s_agdag.nodes[idx];
        auto pit = n.parentId.empty() ? s_agdag.byId.end() : s_agdag.byId.find(n.parentId);
        if (pit == s_agdag.byId.end()) continue;
        if (!rowOf.count(pit->second)) continue;   // parent not visible (shouldn't happen)
        ImVec2 pC = slotPos(idx);
        ImVec2 pP = slotPos(pit->second);
        ImVec2 a(pP.x + 14.0f * S * z, pP.y + nodeH);          // out the bottom-left of the parent
        ImVec2 b(pC.x,                 pC.y + nodeH * 0.5f);    // into the child's left edge
        float  cy = std::max(12.0f * S * z, (b.y - a.y) * 0.5f);
        dl->AddBezierCubic(a, ImVec2(a.x, a.y + cy), ImVec2(b.x - 20.0f * S * z, b.y), b,
                           edgeCol, 1.4f * S * z);
    }

    // Nodes.
    (void)navActive;
    for (int idx : visible) {
        const AgNode& n = s_agdag.nodes[idx];
        ImVec2 p0 = slotPos(idx);
        ImVec2 p1(p0.x + nodeW, p0.y + nodeH);
        if (p1.y < cMin.y || p0.y > cMax.y) continue;   // vertical cull
        const float* sc = agNodeColor(n);
        const float rr  = 6.0f * S * z;
        dl->AddRectFilled(p0, p1,
            ImGui::ColorConvertFloat4ToU32(
                ImVec4(palette.panelBg[0], palette.panelBg[1], palette.panelBg[2], 0.92f)), rr);
        dl->AddRectFilled(p0, ImVec2(p0.x + 4.0f * S * z, p1.y),
            ImGui::ColorConvertFloat4ToU32(ImVec4(sc[0], sc[1], sc[2], 0.95f)), rr,
            ImDrawFlags_RoundCornersLeft);
        dl->AddRect(p0, p1,
            ImGui::ColorConvertFloat4ToU32(ImVec4(sc[0], sc[1], sc[2], 0.65f)), rr, 0, 1.2f * S * z);

        // Collapse toggle: a [-]/[+] hit-box at the node's right edge when it has children.
        bool hasKids = !n.children.empty();
        if (hasKids && z > 0.5f) {
            ImVec2 tg0(p1.x - 20.0f * S * z, p0.y + (nodeH - 16.0f * S * z) * 0.5f);
            ImVec2 tg1(tg0.x + 16.0f * S * z, tg0.y + 16.0f * S * z);
            bool hov = ImGui::IsMouseHoveringRect(tg0, tg1);
            dl->AddRectFilled(tg0, tg1,
                ImGui::ColorConvertFloat4ToU32(ImVec4(sc[0], sc[1], sc[2], hov ? 0.5f : 0.28f)),
                3.0f * S * z);
            dl->AddText(ImVec2(tg0.x + 4.0f * S * z, tg0.y + 1.0f * S * z),
                ImGui::ColorConvertFloat4ToU32(ImVec4(palette.text[0], palette.text[1],
                                                      palette.text[2], 1.0f)),
                s_agdag.nodes[idx].collapsed ? "+" : "-");
            // Toggle on RELEASE of a tap (not press): the full-canvas nav InvisibleButton claims
            // the click on press, so `navActive` is true the same frame IsMouseClicked fires —
            // gating on click-down can never pass. A release with near-zero drag = a tap (a drag
            // pans instead). Threshold keeps a slightly-jittery click from being read as a pan.
            if (hov && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                ImVec2 dd = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left, 0.0f);
                if (dd.x * dd.x + dd.y * dd.y < (4.0f * S) * (4.0f * S))
                    s_agdag.nodes[idx].collapsed = !s_agdag.nodes[idx].collapsed;
            }
        }

        if (z > 0.5f) {
            char head[160];
            if (verbosity >= 2) {
                const std::string lat = n.latencyMs >= 0
                    ? (std::to_string(n.latencyMs) + " ms") : std::string();
                std::snprintf(head, sizeof(head), "%s%s%s%s%s", agKindTag(n.kind),
                              n.actor.empty() ? "" : "  \xC2\xB7  ", n.actor.c_str(),
                              lat.empty() ? "" : "  \xC2\xB7  ", lat.c_str());
            } else {
                std::snprintf(head, sizeof(head), "%s", agKindTag(n.kind));
            }
            dl->AddText(ImVec2(p0.x + 10.0f * S * z, p0.y + 4.0f * S * z),
                ImGui::ColorConvertFloat4ToU32(ImVec4(sc[0], sc[1], sc[2], 1.0f)), head);
            // primary line = title; verbosity 1+ appends the dim detail (target/label/verdict/
            // thought) with a wider budget at full verbosity (tooltip always carries ALL text)
            std::string lbl = n.title;
            if (verbosity >= 1 && !n.detail.empty()) lbl += "  " + n.detail;
            if (s_agdag.nodes[idx].collapsed && hasKids)
                lbl += "  (+" + std::to_string((int)n.children.size()) + ")";
            const size_t maxCh = (size_t)std::max(10.0f, (verbosity >= 2 ? 72.0f : 34.0f) * z);
            if (lbl.size() > maxCh) lbl = lbl.substr(0, maxCh - 1) + "\xE2\x80\xA6";
            dl->AddText(ImVec2(p0.x + 10.0f * S * z, p0.y + nodeH * 0.46f),
                ImGui::ColorConvertFloat4ToU32(
                    ImVec4(palette.text[0], palette.text[1], palette.text[2], 1.0f)), lbl.c_str());
        }
        if (ImGui::IsMouseHoveringRect(p0, p1)) {
            ImGui::BeginTooltip();
            ImGui::TextUnformatted(n.title.c_str());
            if (!n.detail.empty()) { ImGui::PushTextWrapPos(340.0f * S); ImGui::TextDisabled("%s", n.detail.c_str()); ImGui::PopTextWrapPos(); }
            ImGui::TextDisabled("%s%s%s%s", agKindTag(n.kind),
                n.actor.empty() ? "" : " · ", n.actor.c_str(),
                n.latencyMs >= 0 ? (" · " + std::to_string(n.latencyMs) + " ms").c_str() : "");
            if (!n.refId.empty())
                ImGui::TextDisabled("click to open this sub-agent's chat");
            ImGui::EndTooltip();
            // M11: tapping a sub-agent node (spawn OR its execution root, including a live one)
            // opens/steers its chat in the main conversation column. Same tap-vs-pan release
            // discipline as the collapse toggle (the nav InvisibleButton owns the press).
            if (!n.refId.empty() && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                ImVec2 dd = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left, 0.0f);
                bool overToggle = false;
                if (hasKids && z > 0.5f) {
                    ImVec2 tg0(p1.x - 20.0f * S * z, p0.y + (nodeH - 16.0f * S * z) * 0.5f);
                    ImVec2 tg1(tg0.x + 16.0f * S * z, tg0.y + 16.0f * S * z);
                    overToggle = ImGui::IsMouseHoveringRect(tg0, tg1);
                }
                if (!overToggle && dd.x * dd.x + dd.y * dd.y < (4.0f * S) * (4.0f * S))
                    openSubagentView(st, n.refId, n.actor);
            }
        }
    }

    dl->AddText(ImVec2(cMin.x + 8.0f * S, cMax.y - ImGui::GetTextLineHeight() - 6.0f * S),
        ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.8f)),
        "drag to pan · wheel to zoom · [-] collapse a branch");

    dl->PopClipRect();
    ImGui::EndChild();
}

// ---- Agent Hub rows -------------------------------------------------------------
// The sub-agent registry (s_sub.sessions, populated by pollSubagentList) rendered as
// OMP-style rows: role-color wash + left bar, status glyph, role, right-aligned
// model · age meta, and a dim brief line. Full visibility: EVERY registered agent shows,
// live ones sorted first. Click pops that sub-agent's role-themed chat window (v3).
static void drawAgentHubRows(AppState& st, const AdaptiveSurface& surf, float listH) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(8 * S, 6 * S));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##ag_hub_rows", ImVec2(0, listH), ImGuiChildFlags_None);
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();

    ImDrawList* dl = ImGui::GetWindowDrawList();
    const float lineH = ImGui::GetTextLineHeightWithSpacing();
    const float rowH  = lineH * 2.0f + 12.0f * S;
    const float gap   = 6.0f * S;

    // Live first, then most-recently-updated (registry order is stable; updated_ts desc).
    std::vector<int> order(s_sub.sessions.size());
    for (int i = 0; i < (int)order.size(); ++i) order[i] = i;
    auto isLive = [](const SubagentSession& s) {
        return s.status == "running" || s.status == "pending" ||
               s.status == "in_progress" || s.status == "waiting";
    };
    std::sort(order.begin(), order.end(), [&](int a, int b) {
        const SubagentSession& A = s_sub.sessions[a];
        const SubagentSession& B = s_sub.sessions[b];
        const bool la = isLive(A), lb = isLive(B);
        if (la != lb) return la > lb;
        return A.updatedTs > B.updatedTs;   // ISO-8601 strings compare chronologically
    });

    const double tsec = ImGui::GetTime();
    for (int oi : order) {
        const SubagentSession& s = s_sub.sessions[oi];
        ImGui::PushID(s.id.c_str());

        const ImVec2 r0 = ImGui::GetCursorScreenPos();
        const float  rw = ImGui::GetContentRegionAvail().x;
        const ImVec2 r1(r0.x + rw, r0.y + rowH);
        const bool open = s_subChats.count(s.id) > 0;
        const bool hov  = ImGui::IsMouseHoveringRect(r0, r1);

        const std::string roleKey = s.role.empty() ? s.id : s.role;
        float rc[3];
        roleColorRgb(roleKey, rc);

        // Role-colored wash + left identity bar; an open chat gets the glow border.
        coloredRowWash(dl, r0, r1, 8.0f * S, rc,
                       open ? 0.38f : (hov ? 0.30f : 0.20f), open);
        dl->AddRectFilled(r0, ImVec2(r0.x + 3.0f * S, r1.y),
            ImGui::ColorConvertFloat4ToU32(ImVec4(rc[0], rc[1], rc[2], 0.95f)),
            8.0f * S, ImDrawFlags_RoundCornersLeft);

        if (ImGui::InvisibleButton("##hubrow", ImVec2(rw, rowH)))
            openSubagentView(st, s.id, s.role);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("%s this sub-agent's chat (view + steer).",
                              open ? "Raise" : "Pop up");

        // line 1: status glyph (pulsing while running) + role ····· model · age
        const float* sc = statusColor(s.status);
        float glyphA = 1.0f;
        if (s.status == "running")
            glyphA = 0.55f + 0.45f * (float)std::sin(tsec * 4.0f);
        const char* glyph = s.status == "running"    ? "[>]" :
                            s.status == "failed"     ? "[x]" :
                            s.status == "done"       ? "[v]" :
                            s.status == "terminated" ? "[o]" : "[ ]";
        float y = r0.y + 5.0f * S;
        float x = r0.x + 10.0f * S;
        dl->AddText(ImVec2(x, y), ImGui::ColorConvertFloat4ToU32(
            ImVec4(sc[0], sc[1], sc[2], glyphA)), glyph);
        x += ImGui::CalcTextSize(glyph).x + 8.0f * S;
        dl->AddText(ImVec2(x, y), ImGui::ColorConvertFloat4ToU32(surf.textPrimary),
                    roleKey.c_str());
        // right-aligned meta: model · age
        std::string meta;
        if (!s.model.empty()) meta = s.model;
        const std::string age = fmtAge(s.updatedTs);
        if (!age.empty()) { if (!meta.empty()) meta += "  "; meta += age; }
        if (!meta.empty()) {
            const float mw = ImGui::CalcTextSize(meta.c_str()).x;
            dl->AddText(ImVec2(r1.x - mw - 10.0f * S, y),
                        ImGui::ColorConvertFloat4ToU32(surf.textTertiary), meta.c_str());
        }

        // line 2: dim brief (or status), truncated to the row width; pending-steer badge
        y += lineH;
        std::string sub = s.brief.empty() ? s.status : s.brief;
        const float subMaxW = rw - 20.0f * S - 90.0f * S;
        while (!sub.empty() && ImGui::CalcTextSize(sub.c_str()).x > subMaxW)
            sub.pop_back();
        if (sub.size() < (s.brief.empty() ? s.status : s.brief).size())
            sub += "\xE2\x80\xA6";
        dl->AddText(ImVec2(r0.x + 10.0f * S, y),
                    ImGui::ColorConvertFloat4ToU32(surf.textSecondary), sub.c_str());
        if (s.pendingSteers > 0) {
            char badge[24];
            std::snprintf(badge, sizeof(badge), "%d queued", s.pendingSteers);
            const float bw = ImGui::CalcTextSize(badge).x + 10.0f * S;
            const ImVec2 b0(r1.x - bw - 8.0f * S, y - 1.0f * S);
            const ImVec2 b1(r1.x - 8.0f * S, y + lineH - 2.0f * S);
            dl->AddRectFilled(b0, b1, ImGui::ColorConvertFloat4ToU32(
                ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.22f)),
                4.0f * S);
            dl->AddText(ImVec2(b0.x + 5.0f * S, b0.y + 1.0f * S),
                        ImGui::ColorConvertFloat4ToU32(
                            ImVec4(palette.accent[0], palette.accent[1],
                                   palette.accent[2], 1.0f)), badge);
        }

        ImGui::SetCursorScreenPos(ImVec2(r0.x, r1.y + gap));
        ImGui::PopID();
    }
    ImGui::EndChild();
}

// The Agent Hub view: OMP-style roster header + rows + the live execution DAG below.
// Honors agent.ui_verbosity:
//   0 = node titles only · 1 = + detail lines · 2 = + actor/latency, counts, full text.
static void drawAgentHubView(AppState& st) {
    const float S = ui::uiScale;
    const bool  busy = st.job.running.load();
    const int   verbosity = aiUiVerbosity();

    pollSubagentList(st);   // registry snapshot + auto-pop (throttled, single in-flight)

    // Text tones come from the pane's adaptive surface (same id the host draws the slab with).
    const ImVec2 w0 = ImGui::GetWindowPos();
    const ImRect wRect(w0, ImVec2(w0.x + ImGui::GetWindowSize().x,
                                  w0.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& surf =
        adaptivePanel(panelId("ai_ws_view_glass"), wRect, GlassGroup::Global);

    // OMP-grammar header: "Agent Hub — N active · M parked" + live trace counts (verbosity 2).
    int live = 0;
    for (const auto& s : s_sub.sessions)
        if (s.status == "running" || s.status == "pending" ||
            s.status == "in_progress" || s.status == "waiting")
            ++live;
    const int total = (int)s_sub.sessions.size();
    {
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);
        ImGui::TextUnformatted("Agent Hub");
        ImGui::PopStyleColor();
        ImGui::SameLine(0, 10.0f * S);
        ImGui::PushStyleColor(ImGuiCol_Text, surf.textSecondary);
        if (total == 0)
            ImGui::TextUnformatted(st.workerStarted
                ? "— no sub-agents yet"
                : "— worker service not running");
        else
            ImGui::Text("— %d active · %d parked", live, total - live);
        ImGui::PopStyleColor();
        if (verbosity >= 2 && !s_agdag.empty()) {
            ImGui::SameLine(0, 14.0f * S);
            ImGui::PushStyleColor(ImGuiCol_Text, surf.textTertiary);
            ImGui::Text("%d tool call%s · %d thought%s · %d context inject%s",
                        s_agdag.toolCount,   s_agdag.toolCount == 1 ? "" : "s",
                        s_agdag.thoughtCount,s_agdag.thoughtCount == 1 ? "" : "s",
                        s_agdag.ctxCount,    s_agdag.ctxCount == 1 ? "" : "s");
            ImGui::PopStyleColor();
        }
    }
    ImGui::Spacing();

    const bool haveDag  = !s_agdag.empty();
    const bool haveSubs = !s_sub.sessions.empty();

    if (!haveDag && !haveSubs) {
        if (busy)
            emptyState("[>]", "Agent is starting\xE2\x80\xA6",
                       "Tool calls, thoughts and context injections will appear here as the run streams.");
        else
            emptyState("[/]", "No agent activity yet",
                       "Send a message in the chat — delegated sub-agents list here live, each with a "
                       "popup chat of its own, and the execution trace builds below.");
        return;
    }

    const float availH = ImGui::GetContentRegionAvail().y;
    if (haveSubs) {
        // Rows get what they need up to ~45% of the pane when the trace also shows; the
        // child scrolls when the roster outgrows it.
        const float lineH  = ImGui::GetTextLineHeightWithSpacing();
        const float wantH  = total * (lineH * 2.0f + 12.0f * S + 6.0f * S) + 8.0f * S;
        const float listH  = haveDag ? std::min(wantH, std::max(120.0f * S, availH * 0.45f))
                                     : availH;
        drawAgentHubRows(st, surf, listH);
        ImGui::Spacing();
    }
    if (haveDag) {
        sectionHeader("Execution trace");
        drawAgentDagCanvas(st, verbosity);
    } else if (!haveSubs) {
        // Registry has agents but the event stream hasn't produced spans yet — honest, not blank.
        emptyState("[>]", busy ? "Agent is starting\xE2\x80\xA6" : "No agent events yet",
                   "Tool calls, thoughts and context injections appear here as the run streams.");
    }
}

// ---- DAG canvas ------------------------------------------------------------------

static void drawDagCanvas(AppState& st) {
    const float S = ui::uiScale;
    ImGui::BeginChild("##ai_dag_canvas", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    const ImVec2 cMin = ImGui::GetWindowPos();
    const ImVec2 cSz  = ImGui::GetWindowSize();
    const ImVec2 cMax(cMin.x + cSz.x, cMin.y + cSz.y);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(cMin, cMax, true);

    // Whole-canvas interaction surface: drag pans, wheel zooms (about the cursor).
    ImGui::InvisibleButton("##dag_nav", cSz,
                           ImGuiButtonFlags_MouseButtonLeft |
                           ImGuiButtonFlags_MouseButtonMiddle);
    if (ImGui::IsItemActive()) {
        s_ai.pan.x += ImGui::GetIO().MouseDelta.x;
        s_ai.pan.y += ImGui::GetIO().MouseDelta.y;
    }
    if (ImGui::IsItemHovered()) {
        float wheel = ImGui::GetIO().MouseWheel;
        if (wheel != 0.0f) {
            float oldZ = s_ai.zoom;
            s_ai.zoom = std::min(2.5f, std::max(0.35f, s_ai.zoom * (1.0f + wheel * 0.1f)));
            // Keep the point under the cursor fixed while zooming.
            ImVec2 m = ImGui::GetIO().MousePos;
            ImVec2 local(m.x - cMin.x - s_ai.pan.x, m.y - cMin.y - s_ai.pan.y);
            s_ai.pan.x -= local.x * (s_ai.zoom / oldZ - 1.0f);
            s_ai.pan.y -= local.y * (s_ai.zoom / oldZ - 1.0f);
        }
    }

    const float z     = s_ai.zoom;
    const float nodeW = 190.0f * S * z, nodeH = 46.0f * S * z;
    const float gapX  = 70.0f * S * z,  gapY  = 22.0f * S * z;
    auto nodePos = [&](const DagNode& n) {
        return ImVec2(cMin.x + s_ai.pan.x + 16.0f * S + n.layer * (nodeW + gapX),
                      cMin.y + s_ai.pan.y + 16.0f * S + n.row   * (nodeH + gapY));
    };

    std::unordered_map<std::string, const DagNode*> byId;
    for (const auto& n : s_ai.nodes) byId[n.id] = &n;

    // Edges first (behind the boxes): dependency right-edge -> dependent left-edge beziers.
    const ImU32 edgeCol = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 0.55f));
    for (const auto& n : s_ai.nodes) {
        ImVec2 pN = nodePos(n);
        for (const auto& dep : n.requires) {
            auto it = byId.find(dep);
            if (it == byId.end()) continue;
            ImVec2 pD = nodePos(*it->second);
            ImVec2 a(pD.x + nodeW, pD.y + nodeH * 0.5f);   // out of the dependency
            ImVec2 b(pN.x,         pN.y + nodeH * 0.5f);   // into the dependent
            float  cx = std::max(24.0f * S * z, (b.x - a.x) * 0.5f);
            dl->AddBezierCubic(a, ImVec2(a.x + cx, a.y), ImVec2(b.x - cx, b.y), b,
                               edgeCol, 1.5f * S * z);
        }
    }

    // Nodes: rounded status-colored boxes with kind + label.
    for (const auto& n : s_ai.nodes) {
        ImVec2 p0 = nodePos(n);
        ImVec2 p1(p0.x + nodeW, p0.y + nodeH);
        if (p1.x < cMin.x || p0.x > cMax.x || p1.y < cMin.y || p0.y > cMax.y) continue;
        const float* sc = statusColor(n.status);
        const float rr  = 6.0f * S * z;
        dl->AddRectFilled(p0, p1,
            ImGui::ColorConvertFloat4ToU32(
                ImVec4(palette.panelBg[0], palette.panelBg[1], palette.panelBg[2], 0.92f)), rr);
        dl->AddRectFilled(p0, ImVec2(p0.x + 4.0f * S * z, p1.y),
            ImGui::ColorConvertFloat4ToU32(ImVec4(sc[0], sc[1], sc[2], 0.95f)), rr,
            ImDrawFlags_RoundCornersLeft);
        dl->AddRect(p0, p1,
            ImGui::ColorConvertFloat4ToU32(ImVec4(sc[0], sc[1], sc[2], 0.65f)), rr, 0,
            1.2f * S * z);

        if (z > 0.55f) {
            char head[64];
            std::snprintf(head, sizeof(head), "%s  ·  %s",
                          n.kind.empty() ? "node" : n.kind.c_str(), n.status.c_str());
            dl->AddText(ImVec2(p0.x + 10.0f * S * z, p0.y + 5.0f * S * z),
                ImGui::ColorConvertFloat4ToU32(ImVec4(sc[0], sc[1], sc[2], 1.0f)), head);
            std::string lbl = n.label;
            const size_t maxCh = (size_t)std::max(8.0f, 30.0f * z);
            if (lbl.size() > maxCh) lbl = lbl.substr(0, maxCh - 1) + "\xE2\x80\xA6";
            dl->AddText(ImVec2(p0.x + 10.0f * S * z, p0.y + nodeH * 0.48f),
                ImGui::ColorConvertFloat4ToU32(
                    ImVec4(palette.text[0], palette.text[1], palette.text[2], 1.0f)),
                lbl.c_str());
        }
        // Full label + assignee on hover.
        if (ImGui::IsMouseHoveringRect(p0, p1)) {
            ImGui::BeginTooltip();
            ImGui::TextUnformatted(n.label.c_str());
            ImGui::TextDisabled("%s · %s%s%s", n.kind.c_str(), n.status.c_str(),
                                n.assigned.empty() ? "" : " · agent: ",
                                n.assigned.c_str());
            ImGui::EndTooltip();
        }
    }

    // Hint (bottom-left of the canvas).
    dl->AddText(ImVec2(cMin.x + 8.0f * S, cMax.y - ImGui::GetTextLineHeight() - 6.0f * S),
        ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.8f)),
        "drag to pan · wheel to zoom");

    dl->PopClipRect();
    ImGui::EndChild();
}

// Kept for reference: the legacy buildgraph canvas view. The Session Plan tab now dispatches
// to drawPlanBoardView (ui/ai/plan_board.h); this path is no longer wired into the tab strip.
[[maybe_unused]] static void drawDagView(AppState& st) {
    const float S = ui::uiScale;
    if (!s_ai.runsScanned) scanRuns();

    // M8 (per-session scoping): the Session Plan should show only the run(s) belonging to the
    // active chat session. The selection plumbing is here (s_ai.selSession tracks the active
    // conversation), but Conversation has no run_id yet (AI_WORKSPACE_V2_PLAN.md §2.1), so there
    // is no session->run index to filter on. Until Phase 2 adds it, the picker still lists every
    // persisted run.  TODO(Phase 2): filter s_ai.runs to runs whose session == s_ai.selSession.

    // ---- toolbar: run picker + refresh + steer + worker status ----
    ImGui::SetNextItemWidth(300.0f * S);
    const char* preview = "No plans found";
    std::string previewStr;
    if (s_ai.selRun >= 0 && s_ai.selRun < (int)s_ai.runs.size()) {
        const DagRun& r = s_ai.runs[s_ai.selRun];
        previewStr = (r.mission.empty() ? r.runId.substr(0, 8) : r.mission);
        preview = previewStr.c_str();
    }
    if (ImGui::BeginCombo("##ai_dag_run", preview)) {
        for (int i = 0; i < (int)s_ai.runs.size(); ++i) {
            const DagRun& r = s_ai.runs[i];
            char item[192];
            std::snprintf(item, sizeof(item), "%s  (%d node%s)##run%d",
                          r.mission.empty() ? r.runId.substr(0, 8).c_str()
                                            : r.mission.c_str(),
                          std::max(0, r.nodeCount), r.nodeCount == 1 ? "" : "s", i);
            if (ImGui::Selectable(item, i == s_ai.selRun)) {
                s_ai.selRun = i;
                loadRunGraph(r);
                probeDagStatus(st, r.runId);
            }
        }
        ImGui::EndCombo();
    }
    ImGui::SameLine();
    if (ImGui::SmallButton("Refresh")) { s_ai.runsScanned = false; s_ai.loadedRun.clear(); }
    if (s_ai.selRun >= 0) {
        const std::string& rid = s_ai.runs[s_ai.selRun].runId;
        ImGui::SameLine();
        if (ImGui::SmallButton("Approve gate")) steerRun(st, rid, true);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("dag_approve — resume a gate-paused run");
        ImGui::SameLine();
        if (ImGui::SmallButton("Abort")) steerRun(st, rid, false);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("dag_abort — reject the pending gate");
        if (s_ai.statusRun != rid && !s_ai.statusPending) probeDagStatus(st, rid);
    }
    if (!s_ai.statusLine.empty()) {
        const float* c = s_ai.statusOk ? palette.statusOk : palette.textDim;
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], 1.0f));
        ImGui::TextUnformatted(s_ai.statusLine.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::Spacing();

    // Reload the sidecar if selection changed or a steer invalidated it.
    if (s_ai.selRun >= 0 && s_ai.loadedRun != s_ai.runs[s_ai.selRun].runId)
        loadRunGraph(s_ai.runs[s_ai.selRun]);

    if (s_ai.runs.empty()) {
        emptyState("[#]", "No session plan yet",
                   "Start a build in the conversation — this session's plan appears here as it runs.");
        return;
    }
    if (!s_ai.loadErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1],
                   palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("%s", s_ai.loadErr.c_str());
        ImGui::PopStyleColor();
        return;
    }
    if (s_ai.nodes.empty()) {
        emptyState("[#]", "This plan has no nodes",
                   "Pick another run, or start a new mission from Copilot.");
        return;
    }
    drawDagCanvas(st);
}

// ---- right-panel vertical nav ----------------------------------------------------
// One row per utility-panel view. Selected = accent wash + glow (the same color-coded
// grammar as the Layers/PLM rows); hover gets a glass wash. Returns true on click.
static bool navRow(int id, const char* label, bool active, const AdaptiveSurface& surf,
                   int badge = 0) {
    const float S = ui::uiScale;
    ImDrawList* dl = ImGui::GetWindowDrawList();
    ImGui::PushID(id);
    const ImVec2 r0 = ImGui::GetCursorScreenPos();
    const float  rw = ImGui::GetContentRegionAvail().x;
    const float  rh = ImGui::GetFrameHeight() + 8.0f * S;
    const ImVec2 r1(r0.x + rw, r0.y + rh);
    const bool hov = ImGui::IsMouseHoveringRect(r0, r1);
    bool hit = ImGui::InvisibleButton("##navrow", ImVec2(rw, rh));

    if (active) {
        const float acc[3] = {palette.accent[0], palette.accent[1], palette.accent[2]};
        coloredRowWash(dl, r0, r1, 8.0f * S, acc, 0.40f, true);
    } else if (hov) {
        glassRowBackground(dl, r0, r1, 8.0f * S, false, false, 0.32f);
    }
    const float textH = ImGui::GetTextLineHeight();
    dl->AddText(ImVec2(r0.x + 12.0f * S, r0.y + (rh - textH) * 0.5f),
                ImGui::ColorConvertFloat4ToU32(active ? surf.textPrimary
                                                      : surf.textSecondary),
                label);
    if (badge > 0) {
        // Live-count chip pinned to the row's right edge (Agent Hub active agents).
        char b[16];
        std::snprintf(b, sizeof(b), "%d", badge);
        const float bw = ImGui::CalcTextSize(b).x + 12.0f * S;
        const ImVec2 b0(r1.x - bw - 8.0f * S, r0.y + (rh - textH - 4.0f * S) * 0.5f);
        const ImVec2 b1(b0.x + bw, b0.y + textH + 4.0f * S);
        dl->AddRectFilled(b0, b1, ImGui::ColorConvertFloat4ToU32(
            ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.26f)),
            (b1.y - b0.y) * 0.5f);
        dl->AddText(ImVec2(b0.x + 6.0f * S, b0.y + 2.0f * S),
                    ImGui::ColorConvertFloat4ToU32(
                        ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 1.0f)),
                    b);
    }
    ImGui::SetCursorScreenPos(ImVec2(r0.x, r1.y + 4.0f * S));
    ImGui::PopID();
    return hit;
}

static void drawNavColumn(AppState& st, float navW) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(8 * S, 8 * S));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##ai_ws_nav", ImVec2(navW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();

    // Liquid-glass slab behind the nav (same recipe as the chat column).
    ImDrawList* dl = ImGui::GetWindowDrawList();
    const ImVec2 w0 = ImGui::GetWindowPos();
    const ImRect wRect(w0, ImVec2(w0.x + ImGui::GetWindowSize().x,
                                  w0.y + ImGui::GetWindowSize().y));
    const AdaptiveSurface& surf =
        adaptivePanel(panelId("ai_ws_nav_glass"), wRect, GlassGroup::Global);
    glassPanel(dl, wRect, GlassLevel::Thin, 10.0f * S, surf);

    ImGui::Dummy(ImVec2(0, 4.0f * S));

    // Live-agent badge for the Agent Hub row; in-flight doc badge for Active Work.
    int live = 0;
    for (const auto& s : s_sub.sessions)
        if (s.status == "running" || s.status == "pending" ||
            s.status == "in_progress" || s.status == "waiting")
            ++live;
    int wip = 0;
    for (const auto& t : st.docTabs)
        if (t.aiHandoff || t.aiPulse || t.agentDone) ++wip;

    if (navRow(0, "Agent Hub",    s_ai.view == 0, surf, live)) s_ai.view = 0;
    if (navRow(1, "Session Plan", s_ai.view == 1, surf))       s_ai.view = 1;
    if (navRow(2, "Active Work",  s_ai.view == 2, surf, wip))  s_ai.view = 2;
    if (navRow(3, "Documents",    s_ai.view == 3, surf))       s_ai.view = 3;
    if (navRow(4, "Sessions",     s_ai.view == 4, surf))       s_ai.view = 4;
    if (navRow(5, "Leaderboard",  s_ai.view == 5, surf))       s_ai.view = 5;
    if (navRow(6, "Metrics",      s_ai.view == 6, surf))       s_ai.view = 6;

    // Hairline + the utility section.
    ImGui::Dummy(ImVec2(0, 4.0f * S));
    {
        const ImVec2 c = ImGui::GetCursorScreenPos();
        dl->AddLine(ImVec2(c.x + 6.0f * S, c.y), ImVec2(c.x + navW - 22.0f * S, c.y),
                    ui::Palette::toU32(palette.glassBorder), 1.0f * S);
    }
    ImGui::Dummy(ImVec2(0, 8.0f * S));

    if (navRow(7, "Options",      s_ai.view == 7, surf))       s_ai.view = 7;

    ImGui::EndChild();
}

// ---- center: expanded chat --------------------------------------------------------

static void drawChatCenter(AppState& st, bool busy, bool canRun) {
    const float S = ui::uiScale;

    // v3: the main conversation is NEVER replaced — sub-agent chats live in their own popup
    // windows (drawSubagentPopups), opened from the Agent Hub rows or the trace's spawn nodes.

    // Reserve the composer height measured last frame so the log fills the rest
    // (same one-frame-lag trick as the docked Copilot).
    static float s_composerH = 0.0f;
    float reserve = (s_composerH > 0.0f ? s_composerH
                                        : ImGui::GetFrameHeightWithSpacing() * 4.0f) +
                    14.0f * S;
    float logH = ImGui::GetContentRegionAvail().y - reserve;
    if (logH < 120.0f * S) logH = 120.0f * S;

    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##ai_ws_chatlog", ImVec2(0, logH), ImGuiChildFlags_None);
    if (st.chat.empty()) {
        emptyState("[AI]", "Start a conversation",
                   "Describe what to build — the copilot plans, models and verifies it.");
    } else {
        chatDrawLog(st);
        chatLogStickToBottom();
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();

    ImGui::Dummy(ImVec2(0, 4.0f * S));
    ImGui::BeginGroup();
    chatDrawComposer(st, busy, canRun);   // the docked Copilot's real composer + send path
    ImGui::EndGroup();
    s_composerH = ImGui::GetItemRectSize().y + 6.0f * S;
}

// ---- Options view (v3): run-behavior + workspace-visibility knobs -------------------
// The model / reasoning-effort / API key / privacy controls are reachable ONLY on the main
// Settings page (Settings -> AI). This view keeps the run-behavior knobs (same config.json
// fields, no parallel store, shared verbatim with Settings -> AI -> Harness via
// drawHarnessSessionKnobs) + a couple of generation knobs + the command-center visibility
// prefs (agent.ui_*). 2-col stretch-same tables keep every label/control pair aligned
// regardless of label length (a fixed SameLine(labelW) crowded longer labels against their
// control) and a left indent keeps content off the panel's glass edge.
static void drawOptionsView(AppState& st) {
    const float S = ui::uiScale;
    bool changed = false;
    const ImGuiTableFlags gridFlags =
        ImGuiTableFlags_SizingStretchSame | ImGuiTableFlags_NoPadOuterX;

    sectionHeader("Workspace");
    if (ImGui::BeginTable("##ai_ws_workspace", 2, gridFlags)) {
        ImGui::TableNextColumn();
        {
            int v = aiUiVerbosity();
            static const char* kVerb[] = {"Quiet", "Normal", "Full"};
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted("Detail level");
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::Combo("##ai_ws_verb", &v, kVerb, 3)) setAiUiVerbosity(v);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("How much the Agent Hub / Active Work views show: titles "
                                  "only, last operation, or full tickers + counts.");
        }
        ImGui::TableNextColumn();
        {
            bool follow = aiFollowActiveSession();
            if (ImGui::Checkbox("Follow live runs", &follow)) setAiFollowActiveSession(follow);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("New runs yank the panel to the Agent Hub, and new "
                                  "sub-agents pop their chat windows automatically.");
        }
        ImGui::TableNextColumn();
        {
            bool prev = aiActiveWorkPreviews();
            if (ImGui::Checkbox("Active Work previews", &prev)) setAiActiveWorkPreviews(prev);
            if (ImGui::IsItemHovered())
                ImGui::SetTooltip("Render iso-preview thumbnails for the docs agents are "
                                  "editing (worker render jobs; cached per saved revision).");
        }
        ImGui::TableNextColumn();
        ImGui::Dummy(ImVec2(1, ImGui::GetFrameHeight()));
        ImGui::EndTable();
    }
    ImGui::Dummy(ImVec2(0, 8.0f * S));

    // Shared with Settings -> AI -> Harness: the exact same widgets/fields, one source of
    // truth. Covers plan/subagent/advisor preference, role adoption, proactive advisor,
    // company auto-route, vision, offline mode, show-thoughts, project memory, redaction,
    // refine-pass cap, max subagent nesting, memory budget, Eng. Firm budget.
    sectionHeader("Workflow");
    if (ui::drawHarnessSessionKnobs(st, S)) saveSettings(st.settings);
    ImGui::Dummy(ImVec2(0, 8.0f * S));

    sectionHeader("Generation");
    if (ImGui::BeginTable("##ai_ws_gen", 2, gridFlags)) {
        auto intKnob = [&](const char* label, const char* id, int& v, const char* tip) {
            ImGui::AlignTextToFramePadding();
            ImGui::TextUnformatted(label);
            ImGui::SetNextItemWidth(-1.0f);
            if (ImGui::InputInt(id, &v, 0, 0)) { v = std::max(0, v); changed = true; }
            if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", tip);
        };
        ImGui::TableNextColumn();
        intKnob("Max output tokens", "##ai_ws_maxtok", st.settings.maxTokens,
                "Max output tokens per model call. 0 = provider default.");
        ImGui::TableNextColumn();
        intKnob("Context window", "##ai_ws_ctx", st.settings.contextWindow,
                "Model's total token window. 0 = automatic (cloud large, local 8192).");
        ImGui::EndTable();
    }
    if (changed) saveSettings(st.settings);
    ImGui::Dummy(ImVec2(0, 8.0f * S));

    sectionHeader("More");
    if (ImGui::Button("Open full AI settings\xE2\x80\xA6")) {
        st.openSettings    = true;
        st.openSettingsTab = 2;   // Settings modal tab order: Appearance 0, General 1, AI 2
    }
    ImGui::Dummy(ImVec2(0, 4.0f * S));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
               palette.textDisabled[2], 1.0f));
    ImGui::TextWrapped("Workflow knobs persist to config.json (shared with the docked Copilot). "
                       "Model, reasoning effort, API key and privacy live on the main Settings "
                       "page (Settings -> AI).");
    ImGui::PopStyleColor();
}

}  // anonymous namespace

// ---- public entry point ------------------------------------------------------------

// The right-panel content view: one glass pane hosting the nav-selected view (Agent Hub |
// Session Plan | Active Work | Documents | Sessions | Leaderboard | Metrics | Options).
static void drawViewPane(AppState& st, bool busy) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * S, 10 * S));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##ai_ws_view", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();

    // Liquid-glass slab behind the whole pane (shared adaptive surface — the views fetch the
    // same panelId for their text tones).
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 w0 = ImGui::GetWindowPos();
        const ImRect wRect(w0, ImVec2(w0.x + ImGui::GetWindowSize().x,
                                      w0.y + ImGui::GetWindowSize().y));
        const AdaptiveSurface& surf =
            adaptivePanel(panelId("ai_ws_view_glass"), wRect, GlassGroup::Global);
        glassPanel(dl, wRect, GlassLevel::Thin, 10.0f * S, surf);
    }

    // Autopopulate (agent.ui_follow_active_session): a NEW live run yanks the panel to the
    // Agent Hub view so the streaming roster + execution trace show without manual picking.
    if (aiFollowActiveSession() && !s_agdag.runId.empty() &&
        s_agdag.runId != s_ai.lastAutoRun) {
        s_ai.lastAutoRun = s_agdag.runId;
        s_ai.view        = 0;
    }

    // Left margin off the glass edge — one indent for every nav view so Agent Hub / Session
    // Plan / Active Work / Documents / Sessions / Leaderboard / Metrics / Options all sit the
    // same distance in (previously only Options/Metrics indented themselves, so switching
    // views shifted content left/right).
    ImGui::Indent(6.0f * S);

    switch (s_ai.view) {
        case 0:  // Agent Hub: sub-agent roster + live agent-execution DAG.
            drawAgentHubView(st);
            break;
        case 1:  // Session Plan: the plan board over persisted buildgraph runs.
            if (!st.workerStarted)
                emptyState("[#]", "Worker not running",
                           "The Chudware worker service is required to load and steer plans.");
            else
                drawPlanBoardView(st);
            break;
        case 2:  // Active Work: live render grid of the docs agents are editing.
            drawActiveWorkView(st);
            break;
        case 3:  // Documents: in-tab office-document attach / preview / edit panel.
            drawDocsPanel(st);
            break;
        case 4:  // Sessions: the docked Copilot's exact session list, in a wider pane.
            chatDrawSessionsList(st, busy);
            break;
        case 5: {  // Leaderboard (aicad.leaderboard/1).
            if (!s_ai.lbLoaded) {
                s_ai.lb = ui::leaderboard::load();
                s_ai.lbLoaded = true;
            }
            bool wantRefresh = false;
            ui::leaderboard::drawFullPane(s_ai.lb, wantRefresh);
            if (wantRefresh) {
                s_ai.lb = ui::leaderboard::load();
                s_ai.lbLoaded = true;
            }
            break;
        }
        case 6:  // Metrics: token spend / usage aggregation across every session.
            drawMetricsView(st);
            break;
        case 7:  // Options: run-behavior + workspace-visibility knobs.
            drawOptionsView(st);
            break;
        default:
            s_ai.view = 0;
            break;
    }
    ImGui::Unindent(6.0f * S);
    ImGui::EndChild();
}

void drawAiWorkspace(AppState& st) {
    const float S      = ui::uiScale;
    const bool  busy   = st.job.running.load();
    const bool  canRun = st.workerStarted && !busy;

    // Keep model discovery + the doc-bound conversation set fresh even when the docked
    // Copilot column is not drawn (this hub replaces it in the AI workspace).
    refreshModelAvailability(st, false);
    chatWorkspaceSync(st);
    activeWorkPump(st);  // drive at most one Active Work preview render per frame (thumb queue)
    // Registry poll lives at the workspace level (throttled, single in-flight) so the nav
    // badge, the popup transcripts and the auto-pop of NEW live sub-agents work no matter
    // which view is selected — not just while the Agent Hub is showing.
    pollSubagentList(st);
    // Resume-safe autosave: the docked Copilot column (which normally drives this) is not
    // drawn while the AI tab is active, so flush streaming chat here too — a crash mid-turn
    // keeps the assistant text on disk (drafts ride the shared composer's savePromptDraft).
    maybePersistChatHistoryDebounced(st, /*force=*/false);

    // M8 plumbing: bind the Session Plan scope to the active chat session's id.
    s_ai.selSession = (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size())
                          ? st.conversations[st.activeConv].id
                          : std::string();

    // v3 layout (Hunter's directive): MAIN CHAT on the left, everything else in the right
    // utility panel — a vertical nav menu + the selected view. No sessions rail, no bottom
    // options strip. A hairline marks the chat/utility boundary (layout.cpp grammar).
    const float totalW = ImGui::GetContentRegionAvail().x;
    const float gap    = 10.0f * S;
    const float navW   = 168.0f * S;

    // Chat column: a generous main-chat slice — 44% with clean bounds so it never starves
    // (min) or sprawls on ultrawide monitors (max). Full height: log + composer inside.
    const float chatW = std::max(360.0f * S, std::min(totalW * 0.44f, 780.0f * S));

    const ImVec2 origin = ImGui::GetCursorScreenPos();
    const float  bodyH  = ImGui::GetContentRegionAvail().y;
    const ImU32  hairline = ui::Palette::toU32(palette.glassBorder);

    // ---- left: main chat ----------------------------------------------------------
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(12 * S, 10 * S));
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(0, 0, 0, 0));
    ImGui::BeginChild("##ai_ws_conv", ImVec2(chatW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleColor();
    ImGui::PopStyleVar();
    {
        // Liquid-glass slab behind the conversation (same recipe as the view pane).
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const ImVec2 w0 = ImGui::GetWindowPos();
        const ImRect wRect(w0, ImVec2(w0.x + ImGui::GetWindowSize().x,
                                      w0.y + ImGui::GetWindowSize().y));
        const AdaptiveSurface& surf =
            adaptivePanel(panelId("ai_ws_conv_glass"), wRect, GlassGroup::Global);
        glassPanel(dl, wRect, GlassLevel::Thin, 10.0f * S, surf);
    }
    drawChatCenter(st, busy, canRun);   // log + composer (same state/send path as the dock)
    ImGui::EndChild();

    ImGui::SameLine(0.0f, gap);

    // ---- right: utility panel (nav + view) -----------------------------------------
    const ImVec2 rightPos = ImGui::GetCursorScreenPos();
    // Popup sub-agent chats cascade from the view pane's top-left corner.
    s_popupAnchor = ImVec2(rightPos.x + navW + gap + 16.0f * S, rightPos.y + 8.0f * S);

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##ai_ws_right", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    drawNavColumn(st, navW);
    ImGui::SameLine(0.0f, gap);
    drawViewPane(st, busy);

    ImGui::EndChild();  // ##ai_ws_right

    // Vertical hairline along the chat/utility boundary (full height).
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float x = origin.x + chatW + gap * 0.5f;
        dl->AddLine(ImVec2(x, origin.y), ImVec2(x, origin.y + bodyH), hairline, 1.0f * S);
    }

    // Floating sub-agent chat windows draw last (above the whole workspace).
    drawSubagentPopups(st);
}

}  // namespace ui
