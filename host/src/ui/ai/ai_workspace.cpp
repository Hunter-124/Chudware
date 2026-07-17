// AI workspace — full-page AI hub for the AI title-bar tab (v2 redesign, tracker M1-M8, M21).
//
//   +----------------------------------------------------------------+
//   | [rail] | chat        |  DAG pane [Agent Workflow|Session Plan] |   <- M21 layout
//   |        | (narrow,    |                                         |
//   |        |  full       +-----------------------------------------+
//   |        |  height)    |  workflow options (re-flowed, M21)      |
//   +----------------------------------------------------------------+
//
// The conversation window is ALWAYS visible (M6 — no "Conversation" tab); it renders the SAME
// state and send path as the docked Copilot (st.chat / st.conversations / st.prompt) via the
// legacy_chat.h reuse hooks — single chat implementation, two hosts. The tab strip switches only
// the DAG pane (M7): "Session Plan" is the existing buildgraph view (persisted run sidecars the
// worker's dag_status serves — ~/.local/share/AI-CAD/runs/*.buildgraph.json, the ONLY run
// enumeration source), steered via dag_status / dag_approve / dag_abort over st.rpc (async).
// "Agent Workflow" is reserved for the Phase-2 live agent-execution DAG; in Phase 1 it renders
// the same buildgraph view. The right pane keeps ONLY AI workflow knobs (M3); the inference
// block (model/effort/tokens/API key/privacy/vision reviewer) was WIPED (M2) and now lives ONLY
// on the main Settings page (Settings -> AI + Experimental). No fake data anywhere: every pane
// degrades to an honest empty-state.
#include "ai_workspace.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <string>
#include <unordered_map>
#include <vector>

#include "imgui.h"

#include "core/app_state.h"
#include "ui/legacy_chat.h"
#include "ui_theme.h"
#include "ui/leaderboard_ui.h"

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
    // Which DAG the upper pane shows (M7): 0 = Agent Workflow, 1 = Session Plan, 2 = Leaderboard.
    // Phase 1 renders the buildgraph view for BOTH workflow tabs; Phase 2 swaps a live
    // agent-execution DAG into Agent Workflow. The conversation window is always visible below
    // (M6), never a tab. Leaderboard reads benchmarks/leaderboard/leaderboard.json.
    int  dagView      = 0;     // default to Agent Workflow (Phase 2 live execution DAG)
    bool showSettings = true;

    // M8 plumbing: the chat session whose plan the "Session Plan" view is scoped to. Set from
    // the active conversation each frame; the per-session run_id aggregation is Phase-2 backend
    // data (Conversation has no run_id yet — see AI_WORKSPACE_V2_PLAN.md §2.1), so today the
    // view still lists every persisted run. Kept here so the scoping wiring is already in place.
    std::string selSession;

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

// Segmented view chip (Conversation | Project Plan) — accent card when active.
static bool viewChip(const char* label, bool active) {
    ImGui::PushStyleColor(ImGuiCol_Button,
        active ? ImVec4(palette.accent[0], palette.accent[1], palette.accent[2], 0.30f)
               : ImVec4(palette.button[0], palette.button[1], palette.button[2], 0.60f));
    ImGui::PushStyleColor(ImGuiCol_ButtonHovered,
        ImVec4(palette.buttonHovered[0], palette.buttonHovered[1],
               palette.buttonHovered[2], 1.0f));
    ImGui::PushStyleColor(ImGuiCol_Text,
        active ? ImVec4(palette.text[0], palette.text[1], palette.text[2], 1.0f)
               : ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    bool hit = ImGui::SmallButton(label);
    ImGui::PopStyleColor(3);
    return hit;
}

// ---- DAG run catalog (persisted sidecars — the store dag_status reads) ----------

static std::string runsDir() {
    // appdataAppDir() applies the AI-CAD/Billet alias rule on both platforms.
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
// Phase 4 (M11/M15–M17) — sub-agent chat viewing + LIVE steering.
//
// Data source: the file-backed sub-agent session registry served by the worker's
// subagent_list / subagent_transcript / subagent_steer / subagent_terminate RPCs
// (services/agent/subagent_rpc.py) — ALL async via st.rpc (never a sync worker.call
// in a draw path). Steering semantics are queued-and-consumed-at-turn-boundary
// (agent_turn.run_turn drains the queue before each model call); the UI says so.
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
    int budgetTokens = 0, pendingSteers = 0, transcriptLen = 0;
};

struct SubagentViewState {
    // registry snapshot (sidebar list)
    std::vector<SubagentSession> sessions;
    bool   listPending = false;
    bool   listedOnce  = false;
    double lastListT   = 0.0;
    std::string listErr;

    // the open sub-agent chat (replaces the conversation column content, M11)
    bool            active = false;
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

static SubagentViewState s_sub;

static SubagentSession parseSubSession(const nlohmann::json& s) {
    SubagentSession o;
    o.id            = s.value("id", "");
    o.role          = s.value("role", "");
    o.brief         = s.value("brief", "");
    o.status        = s.value("status", "");
    o.model         = s.value("model", "");
    o.spawnKind     = s.value("spawn_kind", "");
    o.budgetTokens  = s.value("budget_tokens", 0);
    o.pendingSteers = s.value("pending_steers", 0);
    o.transcriptLen = s.value("transcript_len", 0);
    return o;
}

// Poll the registry snapshot (sidebar list). Cheap file-glob on the worker side;
// throttled to one in-flight call every few seconds while the workspace is drawn.
static void pollSubagentList(AppState& st) {
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
    });
}

// Fetch the open sub-agent's transcript (+ refreshed session meta). Throttled.
static void fetchSubagentTranscript(AppState& st, bool force = false) {
    if (!st.workerStarted || !s_sub.active || s_sub.trPending) return;
    const double now = ImGui::GetTime();
    if (!force && now - s_sub.lastTrT < 2.0) return;
    s_sub.trPending = true;
    s_sub.lastTrT   = now;
    nlohmann::json p;
    p["subagent_id"] = s_sub.cur.id;
    const std::string want = s_sub.cur.id;
    st.rpc.call("subagent_transcript", p, [want](const core::RpcResult& r) {
        s_sub.trPending = false;
        if (!s_sub.active || s_sub.cur.id != want) return;  // stale — user left/switched
        if (!r.ok) { s_sub.viewErr = r.err.empty() ? "worker unreachable" : r.err; return; }
        if (!r.result.value("ok", false)) {
            s_sub.viewErr = r.result.value("reason", std::string("transcript unavailable"));
            return;
        }
        s_sub.viewErr.clear();
        if (r.result.contains("session") && r.result["session"].is_object())
            s_sub.cur = parseSubSession(r.result["session"]);
        s_sub.cur.pendingSteers = r.result.contains("pending_steers") &&
                                  r.result["pending_steers"].is_array()
                                      ? (int)r.result["pending_steers"].size() : 0;
        s_sub.msgs.clear();
        if (r.result.contains("messages") && r.result["messages"].is_array())
            for (const auto& m : r.result["messages"]) {
                if (!m.is_object()) continue;
                SubMsg sm;
                sm.who    = m.value("who", "");
                sm.text   = m.value("text", "");
                sm.queued = m.value("queued", false);
                s_sub.msgs.push_back(std::move(sm));
            }
    });
}

// M11: open a sub-agent's chat in the main conversation column.
static void openSubagentView(AppState& st, const std::string& subagentId,
                             const std::string& roleHint = std::string()) {
    if (subagentId.empty()) return;
    s_sub.active = true;
    if (s_sub.cur.id != subagentId) {
        s_sub.cur = SubagentSession{};
        s_sub.cur.id   = subagentId;
        s_sub.cur.role = roleHint;
        s_sub.msgs.clear();
        s_sub.viewErr.clear();
        s_sub.steerNote.clear();
        s_sub.steerBuf[0] = '\0';
    }
    // Prefer the registry snapshot for instant header data.
    for (const auto& s : s_sub.sessions)
        if (s.id == subagentId) { s_sub.cur = s; break; }
    fetchSubagentTranscript(st, /*force=*/true);
}

static void sendSubagentSteer(AppState& st) {
    if (!st.workerStarted || s_sub.steerPending || !s_sub.active) return;
    std::string text(s_sub.steerBuf);
    if (text.find_first_not_of(" \t\r\n") == std::string::npos) return;
    s_sub.steerPending = true;
    nlohmann::json p;
    p["subagent_id"] = s_sub.cur.id;
    p["text"]        = text;
    const std::string want = s_sub.cur.id;
    st.rpc.call("subagent_steer", p, [want](const core::RpcResult& r) {
        s_sub.steerPending = false;
        if (!s_sub.active || s_sub.cur.id != want) return;
        if (!r.ok) { s_sub.steerNote = r.err.empty() ? "worker unreachable" : r.err; return; }
        if (!r.result.value("ok", false)) {
            s_sub.steerNote = r.result.value("reason", std::string("steer failed"));
            return;
        }
        s_sub.steerNote   = r.result.value("note", std::string("queued"));
        s_sub.steerBuf[0] = '\0';
        s_sub.lastTrT     = 0.0;   // refresh the transcript promptly (shows the queued steer)
    });
}

static void requestSubagentTerminate(AppState& st) {
    if (!st.workerStarted || s_sub.termPending || !s_sub.active) return;
    s_sub.termPending = true;
    nlohmann::json p;
    p["subagent_id"] = s_sub.cur.id;
    p["confirm"]     = true;   // the GUI confirm dialog IS the M16 gate; the RPC re-checks it
    const std::string want = s_sub.cur.id;
    st.rpc.call("subagent_terminate", p, [want](const core::RpcResult& r) {
        s_sub.termPending = false;
        if (!s_sub.active || s_sub.cur.id != want) return;
        if (r.ok && r.result.value("ok", false))
            s_sub.steerNote = r.result.value(
                "note", std::string("termination requested — honored at the next turn boundary"));
        else
            s_sub.steerNote = r.ok ? r.result.value("reason", std::string("terminate failed"))
                                   : (r.err.empty() ? "worker unreachable" : r.err);
        s_sub.lastTrT = 0.0;
    });
}

// The sub-agent conversation panel — swapped into the main conversation column (M11) with an
// unmistakable recolor (M15), no easy end (M16), and read-only run parameters (M17).
static void drawSubagentChat(AppState& st) {
    const float S = ui::uiScale;
    const float* wc = palette.statusWarn;   // the "you're in a sub-agent" accent hue (M15)

    fetchSubagentTranscript(st);

    // --- header banner (M15): solid tinted strip naming the mode ------------------
    {
        ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImVec2 p1(p0.x + ImGui::GetContentRegionAvail().x,
                  p0.y + ImGui::GetFrameHeight() * 1.9f);
        ImGui::GetWindowDrawList()->AddRectFilled(p0, p1,
            ImGui::ColorConvertFloat4ToU32(ImVec4(wc[0], wc[1], wc[2], 0.22f)), 5.0f * S);
        ImGui::GetWindowDrawList()->AddRect(p0, p1,
            ImGui::ColorConvertFloat4ToU32(ImVec4(wc[0], wc[1], wc[2], 0.85f)), 5.0f * S,
            0, 1.5f * S);
        ImGui::SetCursorScreenPos(ImVec2(p0.x + 8.0f * S, p0.y + 4.0f * S));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(wc[0], wc[1], wc[2], 1.0f));
        ImGui::Text("SUB-AGENT  ·  %s",
                    s_sub.cur.role.empty() ? s_sub.cur.id.c_str() : s_sub.cur.role.c_str());
        ImGui::PopStyleColor();
        const float* sc = statusColor(s_sub.cur.status);
        ImGui::SetCursorScreenPos(ImVec2(p0.x + 8.0f * S,
                                         p0.y + 4.0f * S + ImGui::GetTextLineHeight()));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(sc[0], sc[1], sc[2], 1.0f));
        ImGui::TextUnformatted(s_sub.cur.status.empty() ? "status unknown"
                                                        : s_sub.cur.status.c_str());
        ImGui::PopStyleColor();
        // M16: the ONLY exit is the explicit "Back to main chat" affordance — leaving the view
        // never ends the sub-agent.
        const char* backLbl = "Back to main chat";
        float bw = ImGui::CalcTextSize(backLbl).x + 16.0f * S;
        ImGui::SetCursorScreenPos(ImVec2(p1.x - bw - 6.0f * S,
                                         p0.y + (p1.y - p0.y - ImGui::GetFrameHeight()) * 0.5f));
        if (ImGui::SmallButton(backLbl)) { s_sub.active = false; }
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Return to the main conversation. The sub-agent keeps running.");
        ImGui::SetCursorScreenPos(ImVec2(p0.x, p1.y + 4.0f * S));
    }

    // --- read-only run parameters (M17) --------------------------------------------
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
    ImGui::Text("Model: %s", s_sub.cur.model.empty()
                                 ? "inherited from parent mission (read-only)"
                                 : (s_sub.cur.model + "  (read-only)").c_str());
    ImGui::Text("Effort / budget: %s%s",
                s_sub.cur.budgetTokens > 0
                    ? (std::to_string(s_sub.cur.budgetTokens) + " tokens").c_str()
                    : "inherited",
                "  (read-only)");
    if (!s_sub.cur.brief.empty()) {
        ImGui::TextUnformatted("Starting prompt (read-only):");
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextWrapped("%s", s_sub.cur.brief.c_str());
        ImGui::PopStyleColor();
    }
    ImGui::PopStyleColor();
    ImGui::Spacing();

    // --- transcript ------------------------------------------------------------------
    const float composerH = ImGui::GetFrameHeightWithSpacing() * 4.6f;
    float logH = ImGui::GetContentRegionAvail().y - composerH;
    if (logH < 90.0f * S) logH = 90.0f * S;
    ImGui::PushStyleColor(ImGuiCol_ChildBg, ImVec4(wc[0], wc[1], wc[2], 0.05f));
    ImGui::BeginChild("##subagent_log", ImVec2(0, logH), ImGuiChildFlags_None);
    if (!s_sub.viewErr.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.statusErr[0], palette.statusErr[1], palette.statusErr[2], 1.0f));
        ImGui::TextWrapped("%s", s_sub.viewErr.c_str());
        ImGui::PopStyleColor();
    }
    if (s_sub.msgs.empty() && s_sub.viewErr.empty()) {
        emptyState("[sa]", s_sub.trPending ? "Loading transcript…" : "No transcript yet",
                   "This sub-agent has not produced any messages yet. Its replies and your "
                   "steering messages appear here.");
    }
    for (const auto& m : s_sub.msgs) {
        const float* c = palette.text;
        const char*  tag = "";
        if (m.who == "user")        { c = palette.accent;       tag = "you"; }
        else if (m.who == "system") { c = palette.textDisabled; tag = "sys"; }
        else                        { c = palette.text;         tag = "agent"; }
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(c[0], c[1], c[2], m.who == "system" ? 0.9f : 1.0f));
        ImGui::TextWrapped("[%s]%s %s", tag, m.queued ? " (queued)" : "", m.text.c_str());
        ImGui::PopStyleColor();
        ImGui::Spacing();
    }
    if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 12) ImGui::SetScrollHereY(1.0f);
    ImGui::EndChild();
    ImGui::PopStyleColor();

    // --- steering composer (M11: injections via subagent_steer) ----------------------
    ImGui::Spacing();
    ImGui::InputTextMultiline("##subagent_steer", s_sub.steerBuf, sizeof(s_sub.steerBuf),
                              ImVec2(-1.0f, ImGui::GetFrameHeight() * 1.9f));
    const bool canSteer = st.workerStarted && !s_sub.steerPending &&
                          s_sub.steerBuf[0] != '\0';
    ImGui::BeginDisabled(!canSteer);
    if (ImGui::SmallButton(s_sub.steerPending ? "Queuing…" : "Steer")) sendSubagentSteer(st);
    ImGui::EndDisabled();
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Inject a steering message. It is queued and read by the sub-agent "
                          "at its next turn boundary (before its next model call).");
    ImGui::SameLine();
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
               palette.textDisabled[2], 1.0f));
    if (s_sub.cur.pendingSteers > 0)
        ImGui::Text("%d steer%s pending", s_sub.cur.pendingSteers,
                    s_sub.cur.pendingSteers == 1 ? "" : "s");
    else
        ImGui::TextUnformatted("applied at the sub-agent's next turn boundary");
    ImGui::PopStyleColor();
    // M16: no close/end button anywhere in this view; terminate is deliberately small, off to the
    // side, and gated behind an explicit confirm dialog.
    ImGui::SameLine();
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                         std::max(0.0f, ImGui::GetContentRegionAvail().x - 88.0f * S));
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
               palette.textDisabled[2], 1.0f));
    if (ImGui::SmallButton("Terminate…")) ImGui::OpenPopup("##subagent_term_confirm");
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
        if (ImGui::Button("Terminate")) { requestSubagentTerminate(st); ImGui::CloseCurrentPopup(); }
        ImGui::SameLine();
        if (ImGui::Button("Keep running")) ImGui::CloseCurrentPopup();
        ImGui::EndPopup();
    }
    if (!s_sub.steerNote.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextWrapped("%s", s_sub.steerNote.c_str());
        ImGui::PopStyleColor();
    }
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

static void drawAgentDagCanvas(AppState& st) {
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
            char head[96];
            std::snprintf(head, sizeof(head), "%s%s%s", agKindTag(n.kind),
                          n.latencyMs >= 0 ? "  ·  " : "",
                          n.latencyMs >= 0 ? (std::to_string(n.latencyMs) + " ms").c_str() : "");
            dl->AddText(ImVec2(p0.x + 10.0f * S * z, p0.y + 4.0f * S * z),
                ImGui::ColorConvertFloat4ToU32(ImVec4(sc[0], sc[1], sc[2], 1.0f)), head);
            // primary line = title, dim secondary = detail (target/label/verdict/thought)
            std::string lbl = n.title;
            if (!n.detail.empty()) lbl += "  " + n.detail;
            if (s_agdag.nodes[idx].collapsed && hasKids)
                lbl += "  (+" + std::to_string((int)n.children.size()) + ")";
            const size_t maxCh = (size_t)std::max(10.0f, 34.0f * z);
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

// The Agent Workflow tab body: live tree from the event store, honest empty-states throughout.
static void drawAgentWorkflowView(AppState& st) {
    const float S = ui::uiScale;
    const bool  busy = st.job.running.load();

    // Header strip: live counts + the honest state of the currently-unsourced branch kinds (M20).
    if (!s_agdag.empty()) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::Text("%d tool call%s · %d thought%s · %d context inject%s",
                    s_agdag.toolCount,   s_agdag.toolCount == 1 ? "" : "s",
                    s_agdag.thoughtCount,s_agdag.thoughtCount == 1 ? "" : "s",
                    s_agdag.ctxCount,    s_agdag.ctxCount == 1 ? "" : "s");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        // Sub-agent / advisor branches: present only when a real production sink emits them. Until
        // then this is an HONEST status, not a fabricated node (tracker M20).
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        if (!s_agdag.sawSpawn && !s_agdag.sawAdvisor)
            ImGui::TextUnformatted("   · no sub-agent / advisor branches yet");
        ImGui::PopStyleColor();
        ImGui::Spacing();
    }

    if (s_agdag.empty()) {
        if (busy)
            emptyState("[>]", "Agent is starting…",
                       "Tool calls, thoughts and context injections will appear here as the run streams.");
        else
            emptyState("[/]", "No agent activity yet",
                       "Send a message below — this graph traces the agent's tool calls, thoughts, "
                       "context injections and (when they run) sub-agent / advisor branches, live.");
        return;
    }
    drawAgentDagCanvas(st);
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

static void drawDagView(AppState& st) {
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

// ---- left rail: sessions + sub-agents --------------------------------------------

static void drawSessionsRail(AppState& st, float railW, bool busy) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##ai_ws_rail", ImVec2(railW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    sectionHeader("Sessions");
    // The docked Copilot's exact session list (same rows, same switch/close/new behavior).
    chatDrawSessionsList(st, busy);

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    sectionHeader("Sub-agents");
    // Live registry sessions first (subagent_list, async — Phase 4 M11): every spawned
    // sub-agent, clickable to open/steer its chat in the conversation column. Honest: the
    // registry is empty until a mission actually delegates — no fabricated rows.
    pollSubagentList(st);
    int shown = 0;
    for (const auto& s : s_sub.sessions) {
        ImGui::PushID(s.id.c_str());
        ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImVec2 p1(p0.x + ImGui::GetContentRegionAvail().x,
                  p0.y + ImGui::GetFrameHeight() * 1.6f);
        const bool open = s_sub.active && s_sub.cur.id == s.id;
        ui::glassRowBackground(ImGui::GetWindowDrawList(), p0, p1, 4.0f,
                               ImGui::IsMouseHoveringRect(p0, p1), open, 0.24f);
        if (ImGui::InvisibleButton("##subrow", ImVec2(p1.x - p0.x, p1.y - p0.y)))
            openSubagentView(st, s.id, s.role);
        if (ImGui::IsItemHovered())
            ImGui::SetTooltip("Open this sub-agent's chat (view + steer).");
        ImGui::SetCursorScreenPos(ImVec2(p0.x + 4.0f * S, p0.y + 2.0f * S));
        ImGui::BeginGroup();
        const float* sc = statusColor(s.status);
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(sc[0], sc[1], sc[2], 1.0f));
        ImGui::TextUnformatted(s.status == "running" ? "[>]" : "[ ]");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::TextUnformatted(s.role.empty() ? s.id.c_str() : s.role.c_str());
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextUnformatted(("  " + (s.brief.empty() ? s.status : s.brief).substr(0, 46))
                                   .c_str());
        ImGui::PopStyleColor();
        ImGui::EndGroup();
        ImGui::SetCursorScreenPos(ImVec2(p0.x, p1.y));
        ImGui::Spacing();
        ImGui::PopID();
        ++shown;
    }
    // Plan-assignee rows (the selected Session Plan's assigned nodes) — a different, older
    // surface kept beneath the live registry list.
    for (const auto& n : s_ai.nodes) {
        if (n.assigned.empty()) continue;
        ImVec2 p0 = ImGui::GetCursorScreenPos();
        ImVec2 p1(p0.x + ImGui::GetContentRegionAvail().x,
                  p0.y + ImGui::GetFrameHeight() * 1.6f);
        ui::glassRowBackground(ImGui::GetWindowDrawList(), p0, p1, 4.0f, false, false, 0.24f);
        ImGui::BeginGroup();
        const float* sc = statusColor(n.status);
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(sc[0], sc[1], sc[2], 1.0f));
        ImGui::TextUnformatted(n.status == "running" ? "[>]" : "[ ]");
        ImGui::PopStyleColor();
        ImGui::SameLine();
        ImGui::TextUnformatted(n.assigned.c_str());
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 1.0f));
        ImGui::TextWrapped("  %s", n.label.c_str());
        ImGui::PopStyleColor();
        ImGui::EndGroup();
        ImGui::Spacing();
        ++shown;
    }
    if (shown == 0) {
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 1.0f));
        ImGui::TextWrapped("None running. Sub-agents appear here while a mission plan is "
                           "executing.");
        ImGui::PopStyleColor();
    }

    ImGui::EndChild();
}

// ---- center: expanded chat --------------------------------------------------------

static void drawChatCenter(AppState& st, bool busy, bool canRun) {
    const float S = ui::uiScale;

    // Phase 4 (M11): while a sub-agent chat is open it REPLACES the main conversation in this
    // column — recolored (M15), no easy end (M16), read-only run parameters (M17). The main
    // chat state is untouched; "Back to main chat" restores it instantly.
    if (s_sub.active) {
        drawSubagentChat(st);
        return;
    }

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
        if (ImGui::GetScrollY() >= ImGui::GetScrollMaxY() - 12) ImGui::SetScrollHereY(1.0f);
    }
    ImGui::EndChild();
    ImGui::PopStyleColor();

    ImGui::Dummy(ImVec2(0, 4.0f * S));
    ImGui::BeginGroup();
    chatDrawComposer(st, busy, canRun);   // the docked Copilot's real composer + send path
    ImGui::EndGroup();
    s_composerH = ImGui::GetItemRectSize().y + 6.0f * S;
}

// ---- right: AI *workflow* settings ONLY (M2 wiped the inference block; M3 keeps these) ------
// The model / reasoning-effort / max-tokens / context-window / API key / privacy / vision-
// reviewer controls were REMOVED from this pane (M2). They are reachable ONLY on the main
// Settings page: Settings -> AI (ui::drawProviderModelPicker, which covers provider, model,
// reasoning effort, max response tokens, model context window, API key, base URL, vision, and
// the Privacy/Offline toggle) and Settings -> Experimental (the separate vision reviewer). This
// pane keeps ONLY the run-behavior knobs — the same config.json fields, no parallel store.
// M21: this pane now lives lower-RIGHT (below the DAG, beside the chat column) as a WIDE strip,
// so the content re-flows into two side-by-side groups (Workflow mode | Limits) with the
// persistence footnote spanning underneath; on a narrow strip it degrades to a single stack.
static void drawSettingsPane(AppState& st, float paneH, bool /*busy*/) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##ai_ws_settings", ImVec2(0, paneH), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    bool changed = false;
    const bool twoCol = ImGui::GetContentRegionAvail().x > 480.0f * S;

    ImGui::BeginGroup();
    sectionHeader("Workflow");
    int pm = st.settings.planMode ? 0 : 1;
    if (ImGui::RadioButton("Supervised — plan + clarify first", &pm, 0)) changed = true;
    if (ImGui::RadioButton("Autopilot — model straight from the prompt", &pm, 1))
        changed = true;
    st.settings.planMode = (pm == 0);
    ImGui::EndGroup();

    if (twoCol) ImGui::SameLine(0.0f, 28.0f * S);

    ImGui::BeginGroup();
    sectionHeader("Limits");
    auto intKnob = [&](const char* label, int& v, const char* tip) {
        ImGui::SetNextItemWidth(90.0f * S);
        if (ImGui::InputInt(label, &v, 0, 0)) { v = std::max(0, v); changed = true; }
        if (ImGui::IsItemHovered()) ImGui::SetTooltip("%s", tip);
    };
    intKnob("Refine passes##ai_ws_iters", st.settings.maxIters,
            "Refine-pass cap per turn. 0 = automatic.");
    intKnob("Max output tokens##ai_ws_maxtok", st.settings.maxTokens,
            "Max output tokens per model call. 0 = provider default.");
    intKnob("Context window##ai_ws_ctx", st.settings.contextWindow,
            "Model's total token window. 0 = automatic (cloud large, local 8192).");
    ImGui::EndGroup();

    if (changed) saveSettings(st.settings);

    ImGui::Spacing();
    ImGui::PushStyleColor(ImGuiCol_Text,
        ImVec4(palette.textDisabled[0], palette.textDisabled[1],
               palette.textDisabled[2], 1.0f));
    ImGui::TextWrapped("Workflow knobs persist to config.json (shared with the docked Copilot). "
                       "Model, reasoning effort, tokens, API key, privacy and the vision reviewer "
                       "live on the main Settings page (Settings \xE2\x86\x92 AI).");
    ImGui::PopStyleColor();

    ImGui::EndChild();
}

}  // anonymous namespace

// ---- public entry point ------------------------------------------------------------

// The upper DAG pane: tab strip (Agent Workflow | Session Plan | Leaderboard) + gear.
static void drawDagPane(AppState& st, bool /*busy*/) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##ai_ws_dag", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    // Tab strip switches ONLY this pane (M7). Leaderboard is the aicad.leaderboard/1 consumer.
    if (viewChip("Agent Workflow", s_ai.dagView == 0)) s_ai.dagView = 0;
    ImGui::SameLine();
    if (viewChip("Session Plan", s_ai.dagView == 1)) s_ai.dagView = 1;
    ImGui::SameLine();
    if (viewChip("Leaderboard", s_ai.dagView == 2)) s_ai.dagView = 2;
    ImGui::SameLine();
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                         std::max(0.0f, ImGui::GetContentRegionAvail().x - 84.0f * S));
    if (viewChip(s_ai.showSettings ? "Options \xE2\x96\xB8" : "\xE2\x97\x82 Options",
                 s_ai.showSettings))
        s_ai.showSettings = !s_ai.showSettings;
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Show / hide the AI workflow options pane.");
    ImGui::Spacing();

    if (s_ai.dagView == 2) {
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
    } else if (s_ai.dagView == 0) {
        // Phase 2 (M9/M10): the LIVE agent-execution DAG.
        drawAgentWorkflowView(st);
    } else if (!st.workerStarted) {
        emptyState("[#]", "Worker not running",
                   "The Chudware worker service is required to load and steer plans.");
    } else {
        // Session Plan: the persisted buildgraph view.
        drawDagView(st);
    }
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

    // M8 plumbing: bind the Session Plan scope to the active chat session's id (see the TODO in
    // drawDagView — the run filter itself waits on Phase-2 session->run_id data).
    s_ai.selSession = (st.activeConv >= 0 && st.activeConv < (int)st.conversations.size())
                          ? st.conversations[st.activeConv].id
                          : std::string();

    // M21 layout (Hunter's annotated screenshot):
    //   [rail] | chat (narrow FULL-height left column) | DAG pane         (upper right)
    //          |                                       | workflow options (lower right)
    // Hairline dividers (theme glassBorder, same grammar as layout.cpp pane separators) mark the
    // chat/right boundary and the DAG/options boundary.
    const float totalW = ImGui::GetContentRegionAvail().x;
    const float railW  = 210.0f * S;                             // compacted from 235
    const float bodyW  = totalW - railW - 8.0f * S;

    drawSessionsRail(st, railW, busy);
    ImGui::SameLine();

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##ai_ws_body", ImVec2(bodyW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    const ImVec2 bodyPos  = ImGui::GetCursorScreenPos();
    const float  bodyH    = ImGui::GetContentRegionAvail().y;
    const float  gap      = 8.0f * S;
    const ImU32  hairline = ui::Palette::toU32(palette.glassBorder);

    // Chat column: a small, consistent slice of the body — 30% with clean bounds so it never
    // starves (min) or sprawls on wide monitors (max). Full height: log + composer inside.
    const float chatW = std::max(300.0f * S, std::min(bodyW * 0.30f, 440.0f * S));

    // M15: while viewing a sub-agent, the WHOLE conversation column recolors (tinted background
    // + warm border) so there is no mistaking which chat you are in.
    const bool subView = s_sub.active;
    if (subView)
        ImGui::PushStyleColor(ImGuiCol_ChildBg,
            ImVec4(palette.statusWarn[0], palette.statusWarn[1], palette.statusWarn[2], 0.07f));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##ai_ws_conv", ImVec2(chatW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    drawChatCenter(st, busy, canRun);   // log + composer (mode tabs + prompt); no stray header
    ImGui::EndChild();
    if (subView) {
        ImGui::PopStyleColor();
        ImDrawList* dl = ImGui::GetWindowDrawList();
        ImVec2 c0 = ImGui::GetItemRectMin();
        ImVec2 c1 = ImGui::GetItemRectMax();
        dl->AddRect(c0, c1,
            ImGui::ColorConvertFloat4ToU32(
                ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                       palette.statusWarn[2], 0.75f)),
            4.0f * S, 0, 2.0f * S);
    }

    ImGui::SameLine(0.0f, gap);

    // Right stack: DAG pane on top, workflow options strip below (when shown).
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##ai_ws_right", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    const float rightH = ImGui::GetContentRegionAvail().y;
    const float optH   = s_ai.showSettings
                             ? std::max(150.0f * S, std::min(rightH * 0.34f, 260.0f * S))
                             : 0.0f;
    const float dagH   = std::max(160.0f * S,
                                  rightH - (s_ai.showSettings ? optH + gap : 0.0f));

    ImGui::BeginChild("##ai_ws_upper", ImVec2(0, dagH), ImGuiChildFlags_None);
    drawDagPane(st, busy);
    ImGui::EndChild();

    if (s_ai.showSettings) {
        // Horizontal hairline between the DAG pane and the options strip.
        {
            ImDrawList* dl = ImGui::GetWindowDrawList();
            const float x0 = bodyPos.x + chatW + gap;
            const float y  = bodyPos.y + dagH + gap * 0.5f;
            dl->AddLine(ImVec2(x0, y), ImVec2(bodyPos.x + bodyW, y), hairline, 1.0f * S);
        }
        drawSettingsPane(st, ImGui::GetContentRegionAvail().y, busy);
    }

    ImGui::EndChild();  // ##ai_ws_right

    // Vertical hairline along the chat/right boundary (full body height).
    {
        ImDrawList* dl = ImGui::GetWindowDrawList();
        const float x = bodyPos.x + chatW + gap * 0.5f;
        dl->AddLine(ImVec2(x, bodyPos.y), ImVec2(x, bodyPos.y + bodyH), hairline, 1.0f * S);
    }

    ImGui::EndChild();  // ##ai_ws_body
}

}  // namespace ui
