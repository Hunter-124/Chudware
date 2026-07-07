// AI workspace — full-page AI hub for the AI title-bar tab (v2 redesign, tracker M1-M8).
//
//   +----------------------------------------------------------------+
//   | [rail] |  DAG pane   [ Agent Workflow | Session Plan ]  [gear] |   <- upper main
//   |        |                                                       |
//   +----------------------------------------------------------------+
//   | conversation (log + composer)      |  workflow settings        |   <- lower zone
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
    // Which DAG the upper pane shows (M7): 0 = Agent Workflow, 1 = Session Plan.
    // Phase 1 renders the buildgraph view for BOTH; Phase 2 swaps a live agent-execution DAG
    // into Agent Workflow. The conversation window is always visible below (M6), never a tab.
    int  dagView      = 1;     // default to Session Plan (the only view with real data today)
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
};

static AiWsState s_ai;

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
        float sw = ImGui::CalcTextSize(sub).x;
        ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                             (ImGui::GetContentRegionAvail().x - sw) * 0.5f);
        ImGui::PushStyleColor(ImGuiCol_Text,
            ImVec4(palette.textDisabled[0], palette.textDisabled[1],
                   palette.textDisabled[2], 0.8f));
        ImGui::TextUnformatted(sub);
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
#ifdef _WIN32
    const char* ad = std::getenv("APPDATA");
    return ad && *ad ? std::string(ad) + "\\AI-CAD\\runs" : std::string();
#else
    const char* xdg = std::getenv("XDG_DATA_HOME");
    std::string base;
    if (xdg && *xdg) base = xdg;
    else {
        const char* home = std::getenv("HOME");
        base = std::string(home ? home : "") + "/.local/share";
    }
    return base + "/AI-CAD/runs";
#endif
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
    // Honest data only: nodes of the selected plan that carry an assignee. The worker seam
    // exposes no fleet/agent listing yet — until it does, an unassigned plan shows none.
    int shown = 0;
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
static void drawSettingsPane(AppState& st, float paneW, bool /*busy*/) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##ai_ws_settings", ImVec2(paneW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    bool changed = false;

    sectionHeader("Workflow");
    int pm = st.settings.planMode ? 0 : 1;
    if (ImGui::RadioButton("Supervised — plan + clarify first", &pm, 0)) changed = true;
    if (ImGui::RadioButton("Autopilot — model straight from the prompt", &pm, 1))
        changed = true;
    st.settings.planMode = (pm == 0);

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

// The upper DAG pane: tab strip (Agent Workflow | Session Plan) + gear, then the graph canvas.
static void drawDagPane(AppState& st, bool /*busy*/) {
    const float S = ui::uiScale;
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##ai_ws_dag", ImVec2(0, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    // Tab strip switches ONLY this pane (M7). Both tabs render the buildgraph view in Phase 1;
    // the live agent-execution DAG lands under "Agent Workflow" in Phase 2.
    if (viewChip("Agent Workflow", s_ai.dagView == 0)) s_ai.dagView = 0;
    ImGui::SameLine();
    if (viewChip("Session Plan", s_ai.dagView == 1)) s_ai.dagView = 1;
    ImGui::SameLine();
    ImGui::SetCursorPosX(ImGui::GetCursorPosX() +
                         std::max(0.0f, ImGui::GetContentRegionAvail().x - 84.0f * S));
    if (viewChip(s_ai.showSettings ? "Options \xE2\x96\xB8" : "\xE2\x97\x82 Options",
                 s_ai.showSettings))
        s_ai.showSettings = !s_ai.showSettings;
    if (ImGui::IsItemHovered())
        ImGui::SetTooltip("Show / hide the AI workflow options pane.");
    ImGui::Spacing();

    if (!st.workerStarted) {
        emptyState("[#]", "Worker not running",
                   "The AI-CAD worker service is required to load and steer plans.");
    } else if (s_ai.dagView == 0) {
        // Phase 1: Agent Workflow reuses the buildgraph view. Phase 2 swaps in the live
        // agent-execution DAG (tool calls / context injections / subagent branches).
        drawDagView(st);
    } else {
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

    // v2 layout (M1 compact):
    //   [rail] | +-----------------------------------------------+
    //           |   DAG pane (upper, M4)   [tabs] [Options]       |
    //           +-----------------------------------------------+
    //           |  conversation (M5, lower-left) | workflow (M3) |
    //           +-----------------------------------------------+
    const float totalW = ImGui::GetContentRegionAvail().x;
    const float railW  = 210.0f * S;                             // compacted from 235
    const float bodyW  = totalW - railW - 8.0f * S;

    drawSessionsRail(st, railW, busy);
    ImGui::SameLine();

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(0, 0));
    ImGui::BeginChild("##ai_ws_body", ImVec2(bodyW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();

    // Upper region: the DAG pane occupies the top ~52% of the body height (M4).
    const float bodyH  = ImGui::GetContentRegionAvail().y;
    const float dagH   = std::max(160.0f * S, bodyH * 0.52f);

    ImGui::BeginChild("##ai_ws_upper", ImVec2(0, dagH), ImGuiChildFlags_None);
    drawDagPane(st, busy);
    ImGui::EndChild();

    // Lower region: conversation (left, always visible — M5/M6) + workflow settings (right — M3).
    const float lowerW = ImGui::GetContentRegionAvail().x;
    const float setW   = s_ai.showSettings ? std::min(280.0f * S, lowerW * 0.34f) : 0.0f;
    const float convW  = lowerW - setW - (s_ai.showSettings ? 8.0f * S : 0.0f);

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(10 * S, 8 * S));
    ImGui::BeginChild("##ai_ws_conv", ImVec2(convW, 0), ImGuiChildFlags_None);
    ImGui::PopStyleVar();
    sectionHeader("Conversation");
    drawChatCenter(st, busy, canRun);
    ImGui::EndChild();

    if (s_ai.showSettings) {
        ImGui::SameLine();
        drawSettingsPane(st, setW, busy);
    }

    ImGui::EndChild();  // ##ai_ws_body
}

}  // namespace ui
