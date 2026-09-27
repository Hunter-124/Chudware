#include "agent_dag.h"
#include <algorithm>
#include <cstdio>
#include <unordered_map>
#include <vector>
#include "imgui.h"
#include "core/app_state.h"
#include "ui_theme.h"
#include "ui/metrics.h"
namespace ui {
namespace {
// Agent execution trace: per-conversation event store and persisted canvas camera.
// Nodes are built only from drained agent events, never synthesized as activity.

enum class AgKind { Root, Tool, Thought, Context, Spawn, Advisor, Other };

struct AgNode {
    std::string id;         // span id (§2.1) — THE tree key
    std::string parentId;   // join target
    AgKind      kind = AgKind::Other;
    std::string actor;      // emitting agent/role
    std::string title;      // primary line (tool name / "thought" / injection source)
    std::string detail;     // secondary line (target / label / verdict / text)
    std::string status;     // "ok" | "failed" | "" (drives node accent)
    std::string refId;      // subagent_id for spawn/root nodes: selects the docked inspector
    int         latencyMs = -1;
    long long   seq = 0;    // arrival order (advisory ordering fallback per §2.1)
    // tree links (resolved each rebuild)
    std::vector<int>   children;
    int   depth = 0;        // BFS depth from a root (for indent/layout)
    bool  collapsed = false;// M10: user-collapsed — hides the subtree
    bool  descFailed = false;// M10: a DESCENDANT failed — surfaced on a COLLAPSED ancestor
};

struct AgentDagStore {
    std::string          runId;          // envelope run_id this tree belongs to
    std::vector<AgNode>  nodes;
    std::unordered_map<std::string,int> byId;   // span id -> index
    long long            seqCtr = 0;
    bool                 sawSpawn = false;       // ever received a real subagent branch?
    bool                 sawAdvisor = false;     // ever received a real advisor node?
    int                  toolCount = 0, thoughtCount = 0, ctxCount = 0;
    ImVec2 pan{0.0f, 0.0f};
    float zoom = 1.0f;

    void reset(const std::string& rid) {
        runId = rid; nodes.clear(); byId.clear(); seqCtr = 0;
        sawSpawn = sawAdvisor = false; toolCount = thoughtCount = ctxCount = 0;
    }
    bool empty() const { return nodes.empty(); }
};

// The live DAG is PER CONVERSATION: two chats can run at once, so each owns its own execution
// trace instead of one global tree that the other run blanks or hijacks on every run boundary.
static std::unordered_map<std::string, AgentDagStore> s_agdags;   // conversation id -> tree

// The tree for `convId`, created empty on first touch. GL thread only (no mutex).
static AgentDagStore& agStore(const std::string& convId) { return s_agdags[convId]; }

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
static AgNode& agUpsert(AgentDagStore& dag, const std::string& id, const std::string& parentId) {
    auto it = dag.byId.find(id);
    if (it != dag.byId.end()) {
        AgNode& n = dag.nodes[it->second];
        if (n.parentId.empty() && !parentId.empty()) n.parentId = parentId;
        return n;
    }
    AgNode n;
    n.id = id;
    n.parentId = parentId;
    n.seq = dag.seqCtr++;
    dag.byId[id] = (int)dag.nodes.size();
    dag.nodes.push_back(std::move(n));
    return dag.nodes.back();
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

// Feed one drained orchestrator event into the OWNING conversation's Agent Workflow DAG (GL
// thread only). `convId` is the conversation whose run emitted the event — every chat builds
// its own tree, so a second concurrent run never resets or hijacks the visible one.
void feedAgentDagEvent(const std::string& convId, const std::string& kind,
                       const nlohmann::json& data) {
    if (!data.is_object()) return;
    AgentDagStore& dag = agStore(convId);

    // Run boundary: a fresh run replaces THIS conversation's previous tree (mirrors the
    // context-meter reset).
    const std::string runId = agEnvStr(data, "run_id");
    if (kind == "run_started" || kind == "dag.run.started") {
        dag.reset(runId);
        return;
    }
    // If a new run_id arrives without a start marker (e.g. we joined mid-run), roll over cleanly.
    if (!runId.empty() && !dag.runId.empty() && runId != dag.runId)
        dag.reset(runId);
    if (dag.runId.empty() && !runId.empty()) dag.runId = runId;

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
        : ("_leaf" + std::to_string(dag.seqCtr) + agEnvStr(data, "node_id"));

    if (kind == "agent.tool_call") {
        AgNode& n = agUpsert(dag, key, parentId);
        n.kind   = AgKind::Tool;
        n.actor  = actor;
        n.title  = agEnvStr(data, "tool_name");
        if (n.title.empty()) n.title = "tool";
        n.detail = agEnvStr(data, "target");        // human-readable "what it acts on" (M14)
        bool ok  = data.value("ok", true);
        n.status = ok ? "ok" : "failed";
        n.latencyMs = agEnvInt(data, "latency_ms", -1);
        ++dag.toolCount;
    } else if (kind == "agent.thought") {
        AgNode& n = agUpsert(dag, key, parentId);
        n.kind   = AgKind::Thought;
        n.actor  = actor;
        n.title  = "thought";
        n.detail = agEnvStr(data, "text");          // completed reasoning block (not streamed)
        ++dag.thoughtCount;
    } else if (kind == "agent.context_injected") {
        AgNode& n = agUpsert(dag, key, parentId);
        n.kind   = AgKind::Context;
        n.actor  = actor;
        n.title  = "context: " + agEnvStr(data, "source");
        n.detail = agEnvStr(data, "label");
        int tok  = agEnvInt(data, "token_est", 0);
        if (tok > 0) n.detail += (n.detail.empty() ? "" : "  ") + ("~" + std::to_string(tok) + " tok");
        ++dag.ctxCount;
    } else if (kind == "agent.spawn") {
        // Subagent / worker branch. Emit-wired backend, no production sink today (M20) — but if a
        // real one arrives we build the branch: the spawner span is `parent_id`, and the child's
        // own execution root is `subagent_id`.
        AgNode& n = agUpsert(dag, key, parentId);
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
            dag.nodes[dag.byId[key]].refId = childId;  // M11: click → open its chat
            AgNode& c = agUpsert(dag, childId, key);
            if (c.title.empty()) {
                c.kind = AgKind::Root; c.actor = spawnActor; c.title = "subagent " + spawnActor;
            }
            c.refId = childId;
        }
        dag.sawSpawn = true;
    } else if (kind == "agent.advisor") {
        AgNode& n = agUpsert(dag, key, parentId);
        n.kind   = AgKind::Advisor;
        n.actor  = actor;
        n.title  = "advisor: " + agEnvStr(data, "advisor_kind");
        n.detail = agEnvStr(data, "verdict_summary");
        dag.sawAdvisor = true;
    }
    // Any other agent.* kind (artifact/question/answer) is tolerated but not graphed here.
}

// Serialize one conversation's Agent Workflow DAG — every field the canvas draws. Null json
// when that conversation never ran, so there is nothing to snapshot.
nlohmann::json agentDagToJson(const std::string& convId) {
    auto sit = s_agdags.find(convId);
    if (sit == s_agdags.end()) return nlohmann::json();
    const AgentDagStore& dag = sit->second;
    nlohmann::json j;
    j["runId"]        = dag.runId;
    j["seqCtr"]       = dag.seqCtr;
    j["sawSpawn"]     = dag.sawSpawn;
    j["sawAdvisor"]   = dag.sawAdvisor;
    j["toolCount"]    = dag.toolCount;
    j["thoughtCount"] = dag.thoughtCount;
    j["ctxCount"]     = dag.ctxCount;
    j["pan"] = {dag.pan.x, dag.pan.y};
    j["zoom"] = dag.zoom;
    nlohmann::json arr = nlohmann::json::array();
    for (const AgNode& n : dag.nodes) {
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

// Restore a serialized tree (agentDagToJson) into `convId`'s store. Rebuilds the id index; tree
// links (children/depth/failure roll-up) are re-derived by agResolveTree at draw time.
void agentDagFromJson(const std::string& convId, const nlohmann::json& j) {
    if (!j.is_object()) return;
    AgentDagStore& dag = agStore(convId);
    dag.reset(j.value("runId", std::string()));
    dag.sawSpawn     = j.value("sawSpawn", false);
    dag.sawAdvisor   = j.value("sawAdvisor", false);
    dag.toolCount    = j.value("toolCount", 0);
    dag.thoughtCount = j.value("thoughtCount", 0);
    dag.ctxCount     = j.value("ctxCount", 0);
    if (j.contains("pan") && j["pan"].is_array() && j["pan"].size() == 2 &&
        j["pan"][0].is_number() && j["pan"][1].is_number())
        dag.pan = ImVec2(j["pan"][0].get<float>(), j["pan"][1].get<float>());
    if (j.contains("zoom") && j["zoom"].is_number())
        dag.zoom = std::clamp(j["zoom"].get<float>(), 0.35f, 2.5f);
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
            if (dag.byId.count(n.id)) continue;   // corrupt duplicate id — skip
            dag.byId[n.id] = (int)dag.nodes.size();
            dag.nodes.push_back(std::move(n));
        }
    }
    // Restore the arrival counter past every restored seq so new events keep ordering.
    long long maxSeq = -1;
    for (const AgNode& n : dag.nodes) maxSeq = std::max(maxSeq, n.seq);
    dag.seqCtr = std::max(j.value("seqCtr", (long long)0), maxSeq + 1);
}

AgentDagSummary agentDagSummary(const std::string& convId) {
    const auto it = s_agdags.find(convId);
    if (it == s_agdags.end()) return {};
    const AgentDagStore& dag = it->second;
    return {dag.runId, dag.toolCount, dag.thoughtCount, dag.ctxCount, dag.empty()};
}
// ---- Agent Workflow DAG (Phase 2, M9/M10) ---------------------------------------
// A collapsible, scalable execution tree over the VIEWED conversation's event store
// (s_agdags[convId]). Its own layout so M10 can hide collapsed subtrees BEFORE laying out —
// and roll a descendant failure up onto the collapsed ancestor that hides it.

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
// Nodes whose parent is absent become roots. Also rolls a "something under me failed" bit up
// every ancestor chain, so a COLLAPSED branch can still show the failure hiding inside it.
// Cheap enough to run every frame (tens of nodes).
static void agResolveTree(AgentDagStore& dag, std::vector<int>& roots) {
    for (auto& n : dag.nodes) { n.children.clear(); n.depth = 0; n.descFailed = false; }
    roots.clear();
    for (size_t i = 0; i < dag.nodes.size(); ++i) {
        const std::string& pid = dag.nodes[i].parentId;
        auto it = pid.empty() ? dag.byId.end() : dag.byId.find(pid);
        if (it == dag.byId.end() || it->second == (int)i) roots.push_back((int)i);
        else dag.nodes[it->second].children.push_back((int)i);
    }
    // Depth via BFS from roots (advisory ordering; ts/seq keep siblings stable).
    std::vector<int> stack(roots.begin(), roots.end());
    std::vector<char> seen(dag.nodes.size(), 0);
    for (int r : roots) seen[r] = 1;
    while (!stack.empty()) {
        int cur = stack.back(); stack.pop_back();
        for (int c : dag.nodes[cur].children) {
            if (seen[c]) continue;
            seen[c] = 1;
            dag.nodes[c].depth = dag.nodes[cur].depth + 1;
            stack.push_back(c);
        }
    }
    // Failure roll-up: walk each failed node's ancestor chain, stopping at the first ancestor
    // already marked — which is also what terminates a malformed cyclic parent chain.
    for (const AgNode& n : dag.nodes) {
        if (n.status != "failed") continue;
        std::string pid = n.parentId;
        while (!pid.empty()) {
            auto it = dag.byId.find(pid);
            if (it == dag.byId.end()) break;
            AgNode& p = dag.nodes[it->second];
            if (p.descFailed) break;
            p.descFailed = true;
            pid = p.parentId;
        }
    }
}

// M10: flatten to the VISIBLE set (stop descending at a collapsed node) in tree order, assigning
// each visible node a (layer=depth, row=running counter) grid slot. Returns visible node indices.
static void agFlattenVisible(const AgentDagStore& dag, const std::vector<int>& roots,
                             std::vector<int>& visible, std::unordered_map<int,int>& rowOf) {
    visible.clear(); rowOf.clear();
    // Iterative pre-order DFS honoring collapse; children in seq order (arrival-ordered rows).
    std::vector<int> stack(roots.rbegin(), roots.rend());
    int row = 0;
    while (!stack.empty()) {
        int cur = stack.back(); stack.pop_back();
        visible.push_back(cur);
        rowOf[cur] = row++;
        if (!dag.nodes[cur].collapsed) {
            // push children in reverse-seq so they pop in seq order
            std::vector<int> kids = dag.nodes[cur].children;
            std::sort(kids.begin(), kids.end(), [&dag](int a, int b) {
                return dag.nodes[a].seq < dag.nodes[b].seq;
            });
            for (auto rit = kids.rbegin(); rit != kids.rend(); ++rit) stack.push_back(*rit);
        }
    }
}

// verbosity: 0 = title only, 1 = detail, 2 = actor / latency.
void drawAgentDagCanvas(AppState& st, const std::string& convId, int verbosity,
                        void (*selectAgent)(AppState&, const std::string&, const std::string&)) {
    AgentDagStore& dag = agStore(convId);
    const float S = ui::uiScale;

    std::vector<int> roots;
    agResolveTree(dag, roots);
    std::sort(roots.begin(), roots.end(),
              [&dag](int a, int b) { return dag.nodes[a].seq < dag.nodes[b].seq; });
    std::vector<int> visible;
    std::unordered_map<int,int> rowOf;
    agFlattenVisible(dag, roots, visible, rowOf);

    ImGui::BeginChild("##ag_dag_canvas", ImVec2(0, 0), ImGuiChildFlags_None,
                      ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse);
    const ImVec2 cMin = ImGui::GetWindowPos();
    const ImVec2 cSz  = ImGui::GetWindowSize();
    const ImVec2 cMax(cMin.x + cSz.x, cMin.y + cSz.y);
    ImDrawList* dl = ImGui::GetWindowDrawList();
    dl->PushClipRect(cMin, cMax, true);

    // Camera state belongs to this conversation, not to the workspace shell.
    ImGui::InvisibleButton("##ag_dag_nav", cSz,
                           ImGuiButtonFlags_MouseButtonLeft | ImGuiButtonFlags_MouseButtonMiddle);
    bool navActive = ImGui::IsItemActive();
    if (navActive) {
        dag.pan.x += ImGui::GetIO().MouseDelta.x;
        dag.pan.y += ImGui::GetIO().MouseDelta.y;
    }
    if (ImGui::IsItemHovered()) {
        float wheel = ImGui::GetIO().MouseWheel;
        if (wheel != 0.0f) {
            float oldZ = dag.zoom;
            dag.zoom = std::min(2.5f, std::max(0.35f, dag.zoom * (1.0f + wheel * 0.1f)));
            ImVec2 m = ImGui::GetIO().MousePos;
            ImVec2 local(m.x - cMin.x - dag.pan.x, m.y - cMin.y - dag.pan.y);
            dag.pan.x -= local.x * (dag.zoom / oldZ - 1.0f);
            dag.pan.y -= local.y * (dag.zoom / oldZ - 1.0f);
        }
    }

    const float z     = dag.zoom;
    const float nodeW = 200.0f * S * z, nodeH = 44.0f * S * z;
    const float gapY  = 12.0f * S * z;
    // layer = depth (indent by tree level), row = pre-order counter.
    auto slotPos = [&](int nodeIdx) {
        const AgNode& n = dag.nodes[nodeIdx];
        int rr = rowOf.count(nodeIdx) ? rowOf[nodeIdx] : 0;
        return ImVec2(cMin.x + dag.pan.x + 16.0f * S + n.depth * (32.0f * S * z),
                      cMin.y + dag.pan.y + 16.0f * S + rr * (nodeH + gapY));
    };

    // Edges (parent right/bottom -> child left) behind boxes.
    const ImU32 edgeCol = ImGui::ColorConvertFloat4ToU32(
        ImVec4(palette.textDim[0], palette.textDim[1], palette.textDim[2], 0.55f));
    for (int idx : visible) {
        const AgNode& n = dag.nodes[idx];
        auto pit = n.parentId.empty() ? dag.byId.end() : dag.byId.find(n.parentId);
        if (pit == dag.byId.end()) continue;
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
        const AgNode& n = dag.nodes[idx];
        ImVec2 p0 = slotPos(idx);
        ImVec2 p1(p0.x + nodeW, p0.y + nodeH);
        if (p1.y < cMin.y || p0.y > cMax.y) continue;   // vertical cull
        // A COLLAPSED branch hiding a failed step takes the error accent (and the "!" badge
        // below), so a failure deep inside it stays visible without expanding.
        const bool hidFail = n.collapsed && n.descFailed;
        const float* sc = hidFail ? palette.statusErr : agNodeColor(n);
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
                n.collapsed ? "+" : "-");
            // Toggle on RELEASE of a tap (not press): the full-canvas nav InvisibleButton claims
            // the click on press, so `navActive` is true the same frame IsMouseClicked fires —
            // gating on click-down can never pass. A release with near-zero drag = a tap (a drag
            // pans instead). Threshold keeps a slightly-jittery click from being read as a pan.
            if (hov && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                ImVec2 dd = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left, 0.0f);
                if (dd.x * dd.x + dd.y * dd.y < (4.0f * S) * (4.0f * S))
                    dag.nodes[idx].collapsed = !dag.nodes[idx].collapsed;
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
            // The collapse marker is budgeted OUT of the ellipsis so "(+N)" and the failure
            // badge survive truncation of a long title.
            std::string tail;
            if (n.collapsed && hasKids) {
                tail = "  (+" + std::to_string((int)n.children.size()) + ")";
                if (n.descFailed) tail += " !";
            }
            const size_t maxCh  = (size_t)std::max(10.0f, (verbosity >= 2 ? 72.0f : 34.0f) * z);
            const size_t budget = maxCh > tail.size() ? maxCh - tail.size() : 1;
            if (lbl.size() > budget) lbl = lbl.substr(0, budget - 1) + "\xE2\x80\xA6";
            lbl += tail;
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
            if (hidFail)
                ImGui::TextDisabled("a step inside this collapsed branch failed");
            if (!n.refId.empty())
                ImGui::TextDisabled("click to inspect this sub-agent");
            ImGui::EndTooltip();
            // Tapping a spawn or its execution root selects the docked inspector.
            if (!n.refId.empty() && ImGui::IsMouseReleased(ImGuiMouseButton_Left)) {
                ImVec2 dd = ImGui::GetMouseDragDelta(ImGuiMouseButton_Left, 0.0f);
                bool overToggle = false;
                if (hasKids && z > 0.5f) {
                    ImVec2 tg0(p1.x - 20.0f * S * z, p0.y + (nodeH - 16.0f * S * z) * 0.5f);
                    ImVec2 tg1(tg0.x + 16.0f * S * z, tg0.y + 16.0f * S * z);
                    overToggle = ImGui::IsMouseHoveringRect(tg0, tg1);
                }
                if (!overToggle && dd.x * dd.x + dd.y * dd.y < (4.0f * S) * (4.0f * S))
                    selectAgent(st, n.refId, n.actor);
            }
        }
    }

    // The hint floats over the canvas, so without a backing it overprints whatever node card
    // happens to sit in the bottom-left corner (it collided with a `tool` card in the aiws
    // capture). Wash a plate behind it, the way the graph tab haloes its labels.
    {
        const char* hint = "drag to pan · wheel to zoom · [-] collapse a branch";
        const ImVec2 hs = ImGui::CalcTextSize(hint);
        const ImVec2 hp(cMin.x + 8.0f * S, cMax.y - ImGui::GetTextLineHeight() - 6.0f * S);
        dl->AddRectFilled(ImVec2(hp.x - 4.0f * S, hp.y - 2.0f * S),
                          ImVec2(hp.x + hs.x + 4.0f * S, hp.y + hs.y + 2.0f * S),
                          ui::Palette::toU32(palette.panelBg), 3.0f * S);
        dl->AddText(hp,
            ImGui::ColorConvertFloat4ToU32(
                ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                       palette.textDisabled[2], 0.9f)),
            hint);
    }

    dl->PopClipRect();
    ImGui::EndChild();
}

}  // namespace ui
