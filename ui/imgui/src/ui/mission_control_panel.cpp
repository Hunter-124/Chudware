// T-F17 Mission Control panel — see mission_control_panel.h.
//
// Architecture (pattern library §0/§1):
//   - View-model: aicad::ui::DagRunView (ui/abstract/mission_control_view.h, no ImGui includes)
//   - Scaffold: ui::drawPanelScaffold / ui::drawPanelEmptyState (T-U1 kit)
//   - Worker RPC: s.rpc.call("dag_approve" / "dag_abort" / "dag_status") via AsyncRpc
//     (services/dag/worker_rpc.py — thin wrappers over executor.resume(), the SAME
//     single implementation the dag_* MCP twins delegate to)
//   - Event projection: aicad.agent_events/1 (contracts/agent_events.md) JSONL →
//     run tree + event trail; every agent decision is visible (no silent actions)
//   - Row pills: theme::glassRowBackground (pattern §2)
//
// Honestly deferred (see mission_control_panel.h): design-brief flow and mid-run
// instruction injection (need T-BL09 schema + an executor inject seam).

#include "mission_control_panel.h"
#include "panel_scaffold.h"
#include "mission_control_view.h"  // ui/abstract/ (no ImGui includes)

#include "app/app_state.h"

#include "theme/theme.h"
#include "theme/tokens.h"

#include "imgui.h"
#include <nlohmann/json.hpp>

#include <cstdio>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

namespace ui {

namespace {

// ---- Panel state -----------------------------------------------------------------
bool gOpen = false;
ui::PanelScaffoldState gScaffoldState;
aicad::ui::DagRunView gView;
bool gShowThoughts = true;
bool gSteerBusy = false;      // an approve/abort RPC is in flight

// ---- JSON helpers ------------------------------------------------------------------

std::string jstr(const nlohmann::json& j, const char* key) {
    if (j.contains(key) && j[key].is_string()) return j[key].get<std::string>();
    return {};
}

// ---- Event projection (aicad.agent_events/1 → view-model) -------------------------

aicad::ui::EventEntryKind kindFromString(const std::string& k) {
    using K = aicad::ui::EventEntryKind;
    if (k == "dag.run.started")            return K::RunStarted;
    if (k == "dag.node.started")           return K::NodeStarted;
    if (k == "dag.node.done")              return K::NodeDone;
    if (k == "dag.node.failed")            return K::NodeFailed;
    if (k == "dag.node.paused")            return K::NodePaused;
    if (k == "dag.node.approval_required") return K::NodeApprovalRequired;
    if (k == "dag.node.manual_required")   return K::NodeManualRequired;
    if (k == "dag.run.done")               return K::RunDone;
    if (k == "agent.thought")              return K::AgentThought;
    if (k == "agent.tool_call")            return K::AgentToolCall;
    if (k == "agent.artifact")             return K::AgentArtifact;
    if (k == "agent.question")             return K::AgentQuestion;
    if (k == "agent.answer")               return K::AgentAnswer;
    if (k == "mission.brief.proposed")     return K::MissionBriefProposed;
    if (k == "mission.brief.signed")       return K::MissionBriefSigned;
    return K::Unknown;
}

std::string headlineFor(const std::string& k, const nlohmann::json& d) {
    char buf[160];
    if (k == "dag.run.started") {
        std::string name = jstr(d, "graph_name");
        if (name.empty()) name = jstr(d, "graph_id");
        std::snprintf(buf, sizeof(buf), "Mission started: %s", name.c_str());
        return buf;
    }
    if (k == "dag.node.started") {
        std::snprintf(buf, sizeof(buf), "[%s] %s - running",
                      jstr(d, "node_type").c_str(), jstr(d, "node_name").c_str());
        return buf;
    }
    if (k == "dag.node.done") {
        std::string sum = jstr(d, "output_summary");
        if (!sum.empty()) return sum;
        return jstr(d, "node_id") + " - done";
    }
    if (k == "dag.node.failed")            return "FAILED: " + jstr(d, "error");
    if (k == "dag.node.paused") {
        std::string r = jstr(d, "reason");
        return "Paused - " + (r.empty() ? std::string("gate") : r);
    }
    if (k == "dag.node.approval_required") return "Waiting for approval";
    if (k == "dag.node.manual_required")   return "Manual step required";
    if (k == "dag.run.done")               return "Mission " + jstr(d, "status");
    if (k == "agent.thought")              return jstr(d, "text");
    if (k == "agent.tool_call")            return "tool: " + jstr(d, "tool");
    if (k == "agent.artifact")             return "artifact: " + jstr(d, "path");
    if (k == "agent.question")             return "question: " + jstr(d, "text");
    if (k == "agent.answer")               return "answer: " + jstr(d, "text");
    return k;
}

void applyEvent(const nlohmann::json& ev) {
    using NS = aicad::ui::DagNodeStatus;
    // Support both bare events (kind at top level) and wrapped {"event":kind,"data":{}}.
    std::string k = jstr(ev, "kind");
    if (k.empty()) k = jstr(ev, "event");
    const nlohmann::json* d = &ev;
    if (ev.contains("data") && ev["data"].is_object()) d = &ev["data"];

    aicad::ui::EventEntry entry;
    entry.kindStr   = k;
    entry.kind      = kindFromString(k);
    entry.ts        = jstr(ev, "ts");
    entry.nodeId    = jstr(*d, "node_id");
    if (entry.nodeId.empty()) entry.nodeId = jstr(ev, "node_id");
    entry.isThought = (k == "agent.thought");
    entry.headline  = headlineFor(k, *d);
    entry.detail    = jstr(*d, "args_digest");

    if (k == "dag.run.started") {
        gView.runId = jstr(*d, "run_id");
        if (gView.runId.empty()) gView.runId = jstr(ev, "run_id");
        gView.graphName = jstr(*d, "graph_name");
        if (gView.graphName.empty()) gView.graphName = jstr(*d, "graph_id");
        gView.status = "running";
        gView.hasRun = true;
        gView.steering.mode  = aicad::ui::SteeringMode::Running;
        gView.steering.runId = gView.runId;
    } else if (k == "dag.node.started") {
        const std::string nid = jstr(*d, "node_id");
        bool found = false;
        for (auto& n : gView.nodes)
            if (n.nodeId == nid) { n.status = NS::Running; found = true; break; }
        if (!found) {
            aicad::ui::DagNodeRow row;
            row.nodeId   = nid;
            row.nodeType = jstr(*d, "node_type");
            row.nodeName = jstr(*d, "node_name");
            if (row.nodeName.empty()) row.nodeName = nid;
            row.status = NS::Running;
            gView.nodes.push_back(std::move(row));
        }
    } else if (k == "dag.node.done") {
        const std::string nid = jstr(*d, "node_id");
        for (auto& n : gView.nodes)
            if (n.nodeId == nid) {
                n.status = NS::Done;
                n.outputSummary = jstr(*d, "output_summary");
                break;
            }
        // A gate that was holding steering is resolved.
        if (gView.steering.pendingNodeId == nid) {
            gView.steering.mode = aicad::ui::SteeringMode::Running;
            gView.steering.pendingNodeId.clear();
            gView.steering.canApprove = gView.steering.canReject = false;
            gView.status = "running";
        }
    } else if (k == "dag.node.failed") {
        const std::string nid = jstr(*d, "node_id");
        for (auto& n : gView.nodes)
            if (n.nodeId == nid) {
                n.status = NS::Failed;
                n.errorText = jstr(*d, "error");
                break;
            }
    } else if (k == "dag.node.paused" || k == "dag.node.approval_required" ||
               k == "dag.node.manual_required") {
        std::string nid = jstr(*d, "node_id");
        if (nid.empty()) nid = entry.nodeId;
        for (auto& n : gView.nodes)
            if (n.nodeId == nid) {
                n.status = NS::Paused;
                const std::string reason = jstr(*d, "reason");
                if (k == "dag.node.manual_required" || reason == "manual")
                    n.pauseReason = aicad::ui::DagNodePauseReason::Manual;
                else if (reason == "fail")
                    n.pauseReason = aicad::ui::DagNodePauseReason::Fail;
                else
                    n.pauseReason = aicad::ui::DagNodePauseReason::Gate;
                break;
            }
        gView.status = "paused";
        if (k == "dag.node.approval_required") {
            gView.steering.mode          = aicad::ui::SteeringMode::Paused;
            gView.steering.pendingNodeId = nid;
            gView.steering.canApprove    = true;
            gView.steering.canReject     = true;
        }
    } else if (k == "mission.eta") {
        // T-DP18: backend estimator pushes whole-mission ETA.
        // Fields: label (string), eta_s (number), low_s (number), high_s (number).
        // Per-node ETAs: optional "nodes" object {node_id -> eta_label string}.
        gView.etaLabel   = jstr(*d, "label");
        gView.etaSeconds = d->contains("eta_s") && (*d)["eta_s"].is_number()
                           ? (float)(*d)["eta_s"].get<double>() : -1.f;
        gView.etaLowS    = d->contains("low_s") && (*d)["low_s"].is_number()
                           ? (float)(*d)["low_s"].get<double>() : -1.f;
        gView.etaHighS   = d->contains("high_s") && (*d)["high_s"].is_number()
                           ? (float)(*d)["high_s"].get<double>() : -1.f;
        // Per-node labels for the DAG widget.
        if (d->contains("nodes") && (*d)["nodes"].is_object()) {
            for (auto& [nid, nlabel] : (*d)["nodes"].items()) {
                const std::string ls = nlabel.is_string()
                                       ? nlabel.get<std::string>() : std::string();
                for (auto& n : gView.nodes)
                    if (n.nodeId == nid) { n.etaNodeLabel = ls; break; }
            }
        }
    } else if (k == "dag.run.done") {
        gView.status = jstr(*d, "status");
        if (gView.status.empty()) gView.status = "done";
        if (d->contains("node_states") && (*d)["node_states"].is_object()) {
            for (auto& [nid, ns] : (*d)["node_states"].items()) {
                const std::string st = ns.is_object() ? jstr(ns, "status")
                                     : (ns.is_string() ? ns.get<std::string>() : "");
                for (auto& n : gView.nodes)
                    if (n.nodeId == nid) {
                        if (st == "skipped") n.status = NS::Skipped;
                        break;
                    }
            }
        }
        gView.steering.mode = aicad::ui::SteeringMode::Done;
        gView.steering.canApprove = gView.steering.canReject = false;
    }

    gView.events.push_back(std::move(entry));
}

void seedFromJsonlText(const std::string& jsonl_text) {
    gView = aicad::ui::DagRunView{};
    std::istringstream in(jsonl_text);
    std::string line;
    size_t applied = 0;
    while (std::getline(in, line)) {
        if (line.empty()) continue;
        try {
            applyEvent(nlohmann::json::parse(line));
            ++applied;
        } catch (...) {}  // tolerate malformed lines (fail-open display)
    }
    if (applied > 0) gView.hasRun = true;
}

// ---- Steering RPC ------------------------------------------------------------------

void fireResume(app::AppState& s, bool approve) {
    if (gView.runId.empty() || gSteerBusy) return;
    gSteerBusy = true;
    gScaffoldState.statusText    = approve ? "Approving…" : "Rejecting…";
    gScaffoldState.statusIsError = false;
    const char* method = approve ? "dag_approve" : "dag_abort";
    s.rpc.call(method, {{"run_id", gView.runId}}, [approve](const core::RpcResult& r) {
        gSteerBusy = false;
        if (!r.ok) {
            gScaffoldState.statusText    = std::string(approve ? "Approve" : "Reject") +
                                           " failed: " + r.err;
            gScaffoldState.statusIsError = true;
            return;
        }
        const auto& res = r.result;
        if (!res.value("ok", false)) {
            gScaffoldState.statusText    = res.value("reason", std::string("steering failed"));
            gScaffoldState.statusIsError = true;
            return;
        }
        const std::string status = res.value("status", std::string("unknown"));
        gView.status = status;
        const bool interruptedAgain =
            res.contains("interrupted") && res["interrupted"].is_array() &&
            !res["interrupted"].empty();
        if (interruptedAgain) {
            gView.steering.mode = aicad::ui::SteeringMode::Paused;
            gScaffoldState.statusText = "Resumed - paused at the next gate";
        } else {
            gView.steering.mode = aicad::ui::SteeringMode::Done;
            gView.steering.canApprove = gView.steering.canReject = false;
            gScaffoldState.statusText =
                std::string(approve ? "Approved - run " : "Rejected - run ") + status;
        }
        gScaffoldState.statusIsError = false;
    });
}

void fireRefresh(app::AppState& s) {
    if (gView.runId.empty()) return;
    gScaffoldState.statusText    = "Refreshing…";
    gScaffoldState.statusIsError = false;
    s.rpc.call("dag_status", {{"run_id", gView.runId}}, [](const core::RpcResult& r) {
        if (!r.ok || !r.result.value("ok", false)) {
            gScaffoldState.statusText    = "Status unavailable";
            gScaffoldState.statusIsError = true;
            return;
        }
        // Replay the persisted event trail when the journal exists (restart-safe).
        const std::string jsonlPath = r.result.value("events_jsonl", std::string());
        if (!jsonlPath.empty()) {
            std::ifstream f(jsonlPath);
            if (f.is_open()) {
                std::ostringstream ss;
                ss << f.rdbuf();
                seedFromJsonlText(ss.str());
            }
        }
        gScaffoldState.statusText = "Status refreshed";
    });
}

// ---- Draw helpers ------------------------------------------------------------------

ImVec4 nodeStatusColor(aicad::ui::DagNodeStatus st) {
    using NS = aicad::ui::DagNodeStatus;
    switch (st) {
        case NS::Running: return TVEC(accent);
        case NS::Done:    return TVEC(good);
        case NS::Failed:  return TVEC(bad);
        case NS::Paused:  return TVEC(warn);
        case NS::Skipped: return TVEC(txt2);
        case NS::Pending: default: return TVEC(txt2);
    }
}

const char* nodeStatusGlyph(aicad::ui::DagNodeStatus st) {
    using NS = aicad::ui::DagNodeStatus;
    switch (st) {
        case NS::Running: return "[>]";
        case NS::Done:    return "[v]";
        case NS::Failed:  return "[x]";
        case NS::Paused:  return "[||]";
        case NS::Skipped: return "[-]";
        case NS::Pending: default: return "[ ]";
    }
}

ImVec4 eventColor(const aicad::ui::EventEntry& e) {
    using K = aicad::ui::EventEntryKind;
    switch (e.kind) {
        case K::RunStarted: case K::RunDone:       return TVEC(accent);
        case K::NodeDone:                          return TVEC(good);
        case K::NodeFailed:                        return TVEC(bad);
        case K::NodePaused: case K::NodeApprovalRequired:
        case K::NodeManualRequired:                return TVEC(warn);
        case K::AgentThought:                      return TVEC(txt2);
        default:                                   return TVEC(txt1);
    }
}

void drawRunTree() {
    const float S = theme::S();
    ImGui::TextColored(TVEC(txt2), "Nodes");
    ImGui::Dummy(ImVec2(0, 2.f * S));
    if (gView.nodes.empty()) {
        drawPanelEmptyState("No nodes yet", "the run tree fills as nodes start");
        return;
    }
    for (int i = 0; i < (int)gView.nodes.size(); ++i) {
        const auto& n = gView.nodes[i];
        ImGui::PushID(i);
        const ImVec2 rowMn = ImGui::GetCursorScreenPos();
        const float  rowH  = ImGui::GetTextLineHeightWithSpacing() + 4.f * S;
        const ImVec2 rowMx(rowMn.x + ImGui::GetContentRegionAvail().x, rowMn.y + rowH);
        theme::glassRowBackground(ImGui::GetWindowDrawList(), rowMn, rowMx,
                                  4.f * S, i, /*selected=*/false);
        ImGui::SetCursorScreenPos(ImVec2(rowMn.x + 6.f * S, rowMn.y + 2.f * S));
        ImGui::TextColored(nodeStatusColor(n.status), "%s", nodeStatusGlyph(n.status));
        ImGui::SameLine();
        ImGui::TextUnformatted(n.nodeName.c_str());
        if (!n.nodeType.empty()) {
            ImGui::SameLine();
            ImGui::TextColored(TVEC(txt2), "(%s)", n.nodeType.c_str());
        }
        if (n.status == aicad::ui::DagNodeStatus::Failed && !n.errorText.empty()) {
            ImGui::SameLine();
            ImGui::TextColored(TVEC(bad), "- %s", n.errorText.c_str());
        }
        ImGui::SetCursorScreenPos(ImVec2(rowMn.x, rowMx.y + 2.f * S));
        ImGui::PopID();
    }
}

void drawEventTrail() {
    const float S = theme::S();
    ImGui::TextColored(TVEC(txt2), "Event trail");
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - 110.f * S);
    if (ImGui::SmallButton(gShowThoughts ? "Hide thoughts" : "Show thoughts"))
        gShowThoughts = !gShowThoughts;
    ImGui::Dummy(ImVec2(0, 2.f * S));
    if (gView.events.empty()) {
        drawPanelEmptyState("No events yet", "every agent action appears here");
        return;
    }
    for (int i = 0; i < (int)gView.events.size(); ++i) {
        const auto& e = gView.events[i];
        if (e.isThought && !gShowThoughts) continue;
        ImGui::PushID(i);
        if (!e.ts.empty()) {
            // Show HH:MM:SS from the ISO timestamp when present.
            const size_t t = e.ts.find('T');
            std::string clock = (t != std::string::npos && e.ts.size() >= t + 9)
                                ? e.ts.substr(t + 1, 8) : e.ts;
            ImGui::TextColored(TVEC(txt2), "%s", clock.c_str());
            ImGui::SameLine();
        }
        ImGui::PushTextWrapPos(ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x);
        ImGui::TextColored(eventColor(e), "%s", e.headline.c_str());
        ImGui::PopTextWrapPos();
        if (!e.detail.empty())
            ImGui::TextColored(TVEC(txt2), "    %s", e.detail.c_str());
        ImGui::PopID();
    }
}

void drawSteering(app::AppState& s) {
    using SM = aicad::ui::SteeringMode;
    const float S = theme::S();
    ImGui::TextColored(TVEC(txt2), "Steering");
    ImGui::Dummy(ImVec2(0, 2.f * S));

    switch (gView.steering.mode) {
    case SM::Paused: {
        ImGui::TextColored(TVEC(warn), "Paused - waiting for your decision");
        if (!gView.steering.pendingNodeId.empty())
            ImGui::TextColored(TVEC(txt2), "gate: %s", gView.steering.pendingNodeId.c_str());
        ImGui::Dummy(ImVec2(0, 2.f * S));
        const bool disabled = gSteerBusy || !s.workerStarted;
        if (disabled) ImGui::BeginDisabled();
        if (ImGui::Button("Approve", ImVec2(120.f * S, 0.f)))
            fireResume(s, /*approve=*/true);
        ImGui::SameLine(0, 8.f * S);
        if (ImGui::Button("Reject", ImVec2(120.f * S, 0.f)))
            fireResume(s, /*approve=*/false);
        if (disabled) ImGui::EndDisabled();
        if (!s.workerStarted)
            ImGui::TextColored(TVEC(txt2), "(worker not running - steering unavailable)");
        break;
    }
    case SM::Running:
        ImGui::TextColored(TVEC(accent), "Running - will pause at the next gate");
        break;
    case SM::Done:
        ImGui::TextColored(TVEC(txt1), "Mission finished (status: %s)", gView.status.c_str());
        break;
    case SM::None: default:
        ImGui::TextColored(TVEC(txt2), "(no active mission)");
        break;
    }

    ImGui::Dummy(ImVec2(0, 2.f * S));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, 2.f * S));
    const bool canRefresh = s.workerStarted && !gView.runId.empty();
    if (!canRefresh) ImGui::BeginDisabled();
    if (ImGui::SmallButton("Refresh status")) fireRefresh(s);
    if (!canRefresh) ImGui::EndDisabled();
    ImGui::SameLine();
    // Honest deferral note (pattern §4: never pretend a control works).
    ImGui::TextColored(TVEC(txt2), "brief editing / mid-run inject: coming with T-BL09");
}

void drawContent(app::AppState& s) {
    const float S = theme::S();

    if (!gView.hasRun) {
        drawPanelEmptyState("No mission loaded",
                            "start a DAG run or open a past run's event trail");
        return;
    }

    // Run header line.
    ImGui::PushFont(theme::font(theme::Font::H2));
    ImGui::TextUnformatted(gView.graphName.empty() ? "Mission" : gView.graphName.c_str());
    ImGui::PopFont();
    ImVec4 stCol = TVEC(txt1);
    if (gView.status == "failed") stCol = TVEC(bad);
    else if (gView.status == "paused") stCol = TVEC(warn);
    else if (gView.status == "done") stCol = TVEC(good);
    else if (gView.status == "running") stCol = TVEC(accent);
    ImGui::TextColored(stCol, "status: %s", gView.status.c_str());
    if (!gView.runId.empty()) {
        ImGui::SameLine();
        ImGui::TextColored(TVEC(txt2), "  run %s", gView.runId.c_str());
    }
    ImGui::Dummy(ImVec2(0, 4.f * S));

    // Layout: run tree (top ~30%), event trail (middle ~38%), steering (rest —
    // sized so the Approve/Reject buttons are never clipped).
    const float avail = ImGui::GetContentRegionAvail().y;
    ImGui::BeginChild("##mc_nodes", ImVec2(0, avail * 0.30f), ImGuiChildFlags_None);
    drawRunTree();
    ImGui::EndChild();
    ImGui::Dummy(ImVec2(0, 2.f * S));
    ImGui::BeginChild("##mc_events", ImVec2(0, avail * 0.38f), ImGuiChildFlags_None);
    drawEventTrail();
    ImGui::EndChild();
    ImGui::Dummy(ImVec2(0, 2.f * S));
    drawSteering(s);
}

}  // namespace

// ---- Public API ------------------------------------------------------------------

void drawMissionControlPanel(app::AppState& s) {
    ui::PanelScaffoldSpec spec;
    spec.id    = "mission_control";
    spec.title = "Mission Control";
    spec.icon  = "activity";

    ui::drawPanelScaffold(spec, gScaffoldState, [&]() { drawContent(s); });
}

void syncMissionEtaToAppState(app::AppState& s) {
    // T-DP18: mirror whole-mission ETA from the panel view-model into AppState
    // so status_bar.cpp can display the chip without an additional data path.
    // Called unconditionally every frame from application.cpp so the chip
    // updates even when Mission Control panel is closed.
    s.agent.etaLabel   = gView.etaLabel;
    s.agent.etaSeconds = gView.etaSeconds;
    s.agent.etaLowS    = gView.etaLowS;
    s.agent.etaHighS   = gView.etaHighS;
}

void openMissionControlPanel() { gOpen = true; }

bool* missionControlPanelOpen() { return &gOpen; }

void seedMissionControlPanelFromJsonl(const std::string& jsonl_text) {
    seedFromJsonlText(jsonl_text);
    gScaffoldState.statusText    = gView.hasRun ? "Replay loaded" : "Replay empty";
    gScaffoldState.statusIsError = !gView.hasRun;
}

}  // namespace ui
