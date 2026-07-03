// Mission-control panel (T-F17) — live DAG run stream + steering controls.
// Parses dag.* / agent.* events from events.jsonl into the DagRunView model
// and renders them with ImGui.  The panel can show a live run (onEvent calls)
// or replay a past run from disk.
//
// Shot-harness scenario: AI_CAD_SHOT=mission_control
//   A pre-written capstone events.jsonl is loaded via loadReplay() before the
//   screenshot is taken (static one-shot capture — avoids the issue-#7 deadlock).

#include "mission_control_panel.h"
#include "../core/app_state.h"
#include "../ui_theme.h"

#include "imgui.h"

#include <algorithm>
#include <chrono>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

namespace ui {

namespace fs = std::filesystem;

namespace {

MissionControlPanel gPanel;

static long long nowMs() {
    return std::chrono::duration_cast<std::chrono::milliseconds>(
               std::chrono::steady_clock::now().time_since_epoch())
        .count();
}

static std::string jstr(const nlohmann::json& o, const char* k) {
    if (o.contains(k) && o[k].is_string()) return o[k].get<std::string>();
    return {};
}
static int jint(const nlohmann::json& o, const char* k) {
    if (o.contains(k) && o[k].is_number()) return o[k].get<int>();
    return 0;
}

}  // namespace

MissionControlPanel& missionControlPanel() { return gPanel; }

// -------------------------------------------------------------------------
// Event ingestion
// -------------------------------------------------------------------------

void MissionControlPanel::onEvent(const std::string& kind, const nlohmann::json& data) {
    view_.hasRun = true;
    applyEvent(data.contains("kind") ? data : nlohmann::json{{"kind", kind}, {"data", data}});
}

void MissionControlPanel::clearLive() {
    view_ = aicad::ui::DagRunView{};
}

bool MissionControlPanel::loadReplay(const std::string& jsonlPath) {
    std::ifstream f(jsonlPath);
    if (!f.is_open()) return false;

    std::vector<nlohmann::json> events;
    std::string line;
    while (std::getline(f, line)) {
        if (line.empty()) continue;
        try { events.push_back(nlohmann::json::parse(line)); } catch (...) {}
    }
    if (events.empty()) return false;

    clearLive();
    parseJsonl(events);
    replayLoaded_ = true;
    return true;
}

void MissionControlPanel::parseJsonl(const std::vector<nlohmann::json>& events) {
    for (const auto& ev : events) {
        applyEvent(ev);
    }
}

void MissionControlPanel::applyEvent(const nlohmann::json& ev) {
    // Support both bare events (kind at top level) and wrapped {"event":kind, "data":{}}
    std::string k = jstr(ev, "kind");
    if (k.empty()) k = jstr(ev, "event");
    const nlohmann::json* d = &ev;
    if (ev.contains("data") && ev["data"].is_object()) d = &ev["data"];

    // Build stream entry
    aicad::ui::EventEntry entry;
    entry.kindStr = k;
    entry.kind    = kindFromString(k);
    entry.ts      = jstr(ev, "ts");
    entry.nodeId  = jstr(*d, "node_id");
    if (entry.nodeId.empty()) entry.nodeId = jstr(ev, "node_id");
    entry.isThought = (k == "agent.thought");
    entry.headline  = headlineFor(k, *d);
    entry.detail    = jstr(*d, "args_digest");  // for tool_call events

    // Update run-level state
    if (k == "dag.run.started") {
        view_.runId     = jstr(*d, "run_id");
        if (view_.runId.empty()) view_.runId = jstr(ev, "run_id");
        view_.graphName = jstr(*d, "graph_name");
        if (view_.graphName.empty()) view_.graphName = jstr(*d, "graph_id");
        view_.status    = "running";
        view_.hasRun    = true;
        // Pre-seed node rows from the event if present (optional)
        int nc = jint(*d, "node_count");
        (void)nc;  // used for display only; nodes are seeded on dag.node.started
    } else if (k == "dag.node.started") {
        std::string nid = jstr(*d, "node_id");
        bool found = false;
        for (auto& n : view_.nodes) {
            if (n.nodeId == nid) {
                n.status = aicad::ui::DagNodeStatus::Running;
                found = true; break;
            }
        }
        if (!found) {
            aicad::ui::DagNodeRow row;
            row.nodeId   = nid;
            row.nodeType = jstr(*d, "node_type");
            row.nodeName = jstr(*d, "node_name");
            if (row.nodeName.empty()) row.nodeName = nid;
            row.status   = aicad::ui::DagNodeStatus::Running;
            view_.nodes.push_back(std::move(row));
        }
    } else if (k == "dag.node.done") {
        std::string nid = jstr(*d, "node_id");
        for (auto& n : view_.nodes) {
            if (n.nodeId == nid) {
                n.status = aicad::ui::DagNodeStatus::Done;
                n.outputSummary = jstr(*d, "output_summary");
                break;
            }
        }
    } else if (k == "dag.node.failed") {
        std::string nid = jstr(*d, "node_id");
        for (auto& n : view_.nodes) {
            if (n.nodeId == nid) {
                n.status    = aicad::ui::DagNodeStatus::Failed;
                n.errorText = jstr(*d, "error");
                break;
            }
        }
    } else if (k == "dag.node.paused" || k == "dag.node.approval_required" ||
               k == "dag.node.manual_required") {
        std::string nid = jstr(*d, "node_id");
        if (nid.empty()) nid = entry.nodeId;
        for (auto& n : view_.nodes) {
            if (n.nodeId == nid) {
                n.status = aicad::ui::DagNodeStatus::Paused;
                std::string reason = jstr(*d, "reason");
                if (k == "dag.node.manual_required" || reason == "manual")
                    n.pauseReason = aicad::ui::DagNodePauseReason::Manual;
                else if (reason == "fail")
                    n.pauseReason = aicad::ui::DagNodePauseReason::Fail;
                else
                    n.pauseReason = aicad::ui::DagNodePauseReason::Gate;
                break;
            }
        }
        view_.status = "paused";
        // Update steering
        if (k == "dag.node.approval_required") {
            view_.steering.mode        = aicad::ui::SteeringMode::Paused;
            view_.steering.pendingNodeId = nid;
            view_.steering.canApprove  = true;
            view_.steering.canReject   = true;
            view_.steering.canInject   = true;
            view_.steering.canPause    = false;
        }
    } else if (k == "dag.run.done") {
        view_.status = jstr(*d, "status");
        if (view_.status.empty()) view_.status = "done";
        // Reconcile any node statuses from the node_states summary
        if (d->contains("node_states") && (*d)["node_states"].is_object()) {
            for (auto& [nid, ns] : (*d)["node_states"].items()) {
                std::string st = ns.is_object() ? jstr(ns, "status") :
                                 (ns.is_string() ? ns.get<std::string>() : "");
                for (auto& n : view_.nodes) {
                    if (n.nodeId == nid) {
                        if (st == "skipped") n.status = aicad::ui::DagNodeStatus::Skipped;
                        break;
                    }
                }
            }
        }
        view_.steering.mode = aicad::ui::SteeringMode::Done;
        view_.steering.canApprove = view_.steering.canReject =
            view_.steering.canInject = view_.steering.canPause = false;
    }

    view_.events.push_back(std::move(entry));
}

// -------------------------------------------------------------------------
// Helpers
// -------------------------------------------------------------------------

aicad::ui::DagNodeStatus MissionControlPanel::statusFromString(const std::string& s) {
    if (s == "running") return aicad::ui::DagNodeStatus::Running;
    if (s == "done")    return aicad::ui::DagNodeStatus::Done;
    if (s == "failed")  return aicad::ui::DagNodeStatus::Failed;
    if (s == "paused")  return aicad::ui::DagNodeStatus::Paused;
    if (s == "skipped") return aicad::ui::DagNodeStatus::Skipped;
    return aicad::ui::DagNodeStatus::Pending;
}

aicad::ui::EventEntryKind MissionControlPanel::kindFromString(const std::string& k) {
    if (k == "dag.run.started")          return aicad::ui::EventEntryKind::RunStarted;
    if (k == "dag.node.started")         return aicad::ui::EventEntryKind::NodeStarted;
    if (k == "dag.node.done")            return aicad::ui::EventEntryKind::NodeDone;
    if (k == "dag.node.failed")          return aicad::ui::EventEntryKind::NodeFailed;
    if (k == "dag.node.paused")          return aicad::ui::EventEntryKind::NodePaused;
    if (k == "dag.node.approval_required") return aicad::ui::EventEntryKind::NodeApprovalRequired;
    if (k == "dag.node.manual_required") return aicad::ui::EventEntryKind::NodeManualRequired;
    if (k == "dag.run.done")             return aicad::ui::EventEntryKind::RunDone;
    if (k == "agent.thought")            return aicad::ui::EventEntryKind::AgentThought;
    if (k == "agent.tool_call")          return aicad::ui::EventEntryKind::AgentToolCall;
    if (k == "agent.artifact")           return aicad::ui::EventEntryKind::AgentArtifact;
    if (k == "agent.question")           return aicad::ui::EventEntryKind::AgentQuestion;
    if (k == "agent.answer")             return aicad::ui::EventEntryKind::AgentAnswer;
    if (k == "mission.brief.proposed")   return aicad::ui::EventEntryKind::MissionBriefProposed;
    if (k == "mission.brief.signed")     return aicad::ui::EventEntryKind::MissionBriefSigned;
    return aicad::ui::EventEntryKind::Unknown;
}

std::string MissionControlPanel::headlineFor(const std::string& k, const nlohmann::json& d) {
    if (k == "dag.run.started") {
        std::string name = jstr(d, "graph_name");
        if (name.empty()) name = jstr(d, "graph_id");
        char buf[128]; std::snprintf(buf, sizeof(buf), "Mission started: %s", name.c_str());
        return buf;
    }
    if (k == "dag.node.started") {
        char buf[128]; std::snprintf(buf, sizeof(buf), "[%s] %s — running",
            jstr(d, "node_type").c_str(), jstr(d, "node_name").c_str());
        return buf;
    }
    if (k == "dag.node.done") {
        std::string sum = jstr(d, "output_summary");
        if (!sum.empty()) return sum;
        return std::string(jstr(d, "node_id")) + " — done";
    }
    if (k == "dag.node.failed") {
        return std::string("FAILED: ") + jstr(d, "error");
    }
    if (k == "dag.node.paused") {
        std::string r = jstr(d, "reason");
        return std::string("Paused — ") + (r.empty() ? "gate" : r);
    }
    if (k == "dag.node.approval_required") return "Waiting for approval";
    if (k == "dag.node.manual_required")   return "Manual step required";
    if (k == "dag.run.done") {
        return std::string("Mission ") + jstr(d, "status");
    }
    if (k == "agent.thought") {
        std::string t = jstr(d, "text");
        if (t.size() > 80) t = t.substr(0, 80) + "…";
        return t;
    }
    if (k == "agent.tool_call") {
        return std::string("Tool: ") + jstr(d, "tool_name");
    }
    if (k == "agent.artifact") {
        return std::string("Artifact: ") + jstr(d, "kind") + " — " + jstr(d, "summary");
    }
    if (k == "agent.question") {
        return std::string("Question: ") + jstr(d, "prompt");
    }
    if (k == "agent.answer") {
        return std::string("Answer received (") + jstr(d, "source") + ")";
    }
    if (k == "mission.brief.proposed") return "Design brief proposed";
    if (k == "mission.brief.signed")   return "Design brief signed";
    return k;
}

// -------------------------------------------------------------------------
// Replay list (scans cache/runs/ for events.jsonl files)
// -------------------------------------------------------------------------

void MissionControlPanel::refreshReplayList() {
    long long now = nowMs();
    if (now - lastListMs_ < 3000) return;
    lastListMs_ = now;
    replayList_.clear();

    std::error_code ec;
    fs::path runsDir = fs::path(appdataDir()) / "AI-CAD" / "cache" / "runs";
    if (!fs::is_directory(runsDir, ec)) return;

    struct Entry { std::string id; std::string jsonlPath; fs::file_time_type ft; };
    std::vector<Entry> entries;
    for (fs::directory_iterator it(runsDir, ec), end; it != end; it.increment(ec)) {
        if (ec) break;
        if (!it->is_directory(ec)) continue;
        std::string runId = it->path().filename().string();
        fs::path jp = it->path() / "events.jsonl";
        if (!fs::exists(jp, ec)) continue;
        Entry e;
        e.id = runId;
        e.jsonlPath = jp.string();
        e.ft = fs::last_write_time(it->path(), ec);
        entries.push_back(std::move(e));
    }
    std::sort(entries.begin(), entries.end(),
              [](const Entry& a, const Entry& b) { return a.ft > b.ft; });
    for (auto& e : entries) {
        aicad::ui::ReplayEntry re;
        re.runId     = e.id;
        re.jsonlPath = e.jsonlPath;
        re.label     = e.id.size() > 16 ? e.id.substr(0, 16) + "..." : e.id;
        replayList_.push_back(std::move(re));
    }
}

// -------------------------------------------------------------------------
// Rendering
// -------------------------------------------------------------------------

void MissionControlPanel::draw(AppState& st) {
    // In shot mode force a large, centered, always-on-top window so the
    // screenshot actually shows the panel content rather than a collapsed or
    // off-screen window behind the docked layout.
    if (st.wantMissionControl) {
        ImGuiIO& io = ImGui::GetIO();
        float W = io.DisplaySize.x > 0 ? io.DisplaySize.x : 1340.f;
        float H = io.DisplaySize.y > 0 ? io.DisplaySize.y : 838.f;
        ImGui::SetNextWindowPos(ImVec2(W * 0.05f, H * 0.05f), ImGuiCond_Always);
        ImGui::SetNextWindowSize(ImVec2(W * 0.90f, H * 0.90f), ImGuiCond_Always);
        ImGui::SetNextWindowFocus();
    } else {
        ImGui::SetNextWindowSize(ImVec2(520, 600), ImGuiCond_FirstUseEver);
    }
    ImGuiWindowFlags flags = st.wantMissionControl
        ? (ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoResize)
        : ImGuiWindowFlags_None;
    if (!ImGui::Begin("Mission Control##mc_panel", nullptr, flags)) { ImGui::End(); return; }
    drawBody(st);
    ImGui::End();
}

void MissionControlPanel::drawBody(AppState& st, const theme::AdaptiveSurface* /*surf*/) {
    refreshReplayList();

    const auto& p = ui::palette;

    // ---- Header ----
    ImGui::TextColored(ImVec4(p.accent[0], p.accent[1], p.accent[2], 1.f),
                       "Mission Control");
    if (!view_.graphName.empty()) {
        ImGui::SameLine();
        ImGui::TextDisabled("— %s", view_.graphName.c_str());
    }
    ImGui::SameLine(ImGui::GetContentRegionAvail().x - 120.f);
    if (ImGui::SmallButton(showThoughts_ ? "Hide thoughts" : "Show thoughts"))
        showThoughts_ = !showThoughts_;

    ImGui::Separator();

    if (!view_.hasRun) {
        // No active run — show replay selector
        drawReplaySelector();
        return;
    }

    // Status badge
    const char* statusLabel = view_.status.c_str();
    ImVec4 statusCol = ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1.f);
    if (view_.status == "running")
        statusCol = ImVec4(p.accent[0], p.accent[1], p.accent[2], 1.f);
    else if (view_.status == "done")
        statusCol = ImVec4(p.statusOk[0], p.statusOk[1], p.statusOk[2], 1.f);
    else if (view_.status == "failed")
        statusCol = ImVec4(p.statusErr[0], p.statusErr[1], p.statusErr[2], 1.f);
    else if (view_.status == "paused")
        statusCol = ImVec4(p.statusWarn[0], p.statusWarn[1], p.statusWarn[2], 1.f);

    ImGui::TextColored(statusCol, "Status: %s", statusLabel);
    if (!view_.runId.empty()) {
        ImGui::SameLine();
        ImGui::TextDisabled("run %s", view_.runId.substr(0, 8).c_str());
    }

    ImGui::Spacing();

    // Split the panel: top half = run tree; bottom half = event stream
    float availH = ImGui::GetContentRegionAvail().y;
    float treeH  = availH * 0.38f;
    float streamH = availH * 0.38f;
    float steerH  = availH - treeH - streamH - 8.f;

    // ---- Run tree ----
    ImGui::TextDisabled("Nodes");
    if (ImGui::BeginChild("##mc_tree", ImVec2(0, treeH), true)) {
        drawRunTree();
    }
    ImGui::EndChild();

    ImGui::Spacing();

    // ---- Event stream ----
    ImGui::TextDisabled("Event trail");
    if (ImGui::BeginChild("##mc_stream", ImVec2(0, streamH), true)) {
        drawEventStream();
    }
    ImGui::EndChild();

    ImGui::Spacing();

    // ---- Steering controls ----
    if (steerH > 30.f) {
        if (ImGui::BeginChild("##mc_steer", ImVec2(0, steerH), true)) {
            drawSteering(st);
        }
        ImGui::EndChild();
    }
}

void MissionControlPanel::drawRunTree() {
    const auto& p = ui::palette;
    if (view_.nodes.empty()) {
        ImGui::TextDisabled("(waiting for first node)");
        return;
    }
    for (auto& node : view_.nodes) {
        const char* icon = " ";
        ImVec4 col = ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1.f);
        switch (node.status) {
            case aicad::ui::DagNodeStatus::Running:
                icon = ">";
                col = ImVec4(p.accent[0], p.accent[1], p.accent[2], 1.f);
                break;
            case aicad::ui::DagNodeStatus::Done:
                icon = "v";
                col = ImVec4(p.statusOk[0], p.statusOk[1], p.statusOk[2], 1.f);
                break;
            case aicad::ui::DagNodeStatus::Failed:
                icon = "x";
                col = ImVec4(p.statusErr[0], p.statusErr[1], p.statusErr[2], 1.f);
                break;
            case aicad::ui::DagNodeStatus::Paused:
                icon = "!";
                col = ImVec4(p.statusWarn[0], p.statusWarn[1], p.statusWarn[2], 1.f);
                break;
            case aicad::ui::DagNodeStatus::Skipped:
                icon = "-";
                break;
            default:
                icon = ".";
                break;
        }

        ImGui::PushStyleColor(ImGuiCol_Text, col);
        bool open = ImGui::TreeNodeEx(
            node.nodeId.c_str(),
            ImGuiTreeNodeFlags_SpanAvailWidth | (node.expanded ? ImGuiTreeNodeFlags_DefaultOpen : 0),
            "[%s] %s (%s)", icon, node.nodeName.empty() ? node.nodeId.c_str() : node.nodeName.c_str(),
            node.nodeType.c_str());
        ImGui::PopStyleColor();

        if (open) {
            node.expanded = true;
            if (!node.outputSummary.empty())
                ImGui::TextWrapped("%s", node.outputSummary.c_str());
            if (!node.errorText.empty()) {
                ImGui::TextColored(
                    ImVec4(p.statusErr[0], p.statusErr[1], p.statusErr[2], 1.f),
                    "Error: %s", node.errorText.c_str());
            }
            if (node.status == aicad::ui::DagNodeStatus::Paused) {
                ImGui::TextColored(
                    ImVec4(p.statusWarn[0], p.statusWarn[1], p.statusWarn[2], 1.f),
                    "Paused — awaiting human decision");
            }
            ImGui::TreePop();
        } else {
            node.expanded = false;
        }
    }
}

void MissionControlPanel::drawEventStream() {
    const auto& p = ui::palette;
    if (view_.events.empty()) {
        ImGui::TextDisabled("(no events yet)");
        return;
    }

    // Scroll to bottom when new events arrive
    bool scrollToBottom = (view_.status == "running");

    for (const auto& ev : view_.events) {
        if (ev.isThought && !showThoughts_) continue;

        ImVec4 col = ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 1.f);
        switch (ev.kind) {
            case aicad::ui::EventEntryKind::RunStarted:
            case aicad::ui::EventEntryKind::RunDone:
                col = ImVec4(p.accent[0], p.accent[1], p.accent[2], 1.f);
                break;
            case aicad::ui::EventEntryKind::NodeDone:
                col = ImVec4(p.statusOk[0], p.statusOk[1], p.statusOk[2], 1.f);
                break;
            case aicad::ui::EventEntryKind::NodeFailed:
                col = ImVec4(p.statusErr[0], p.statusErr[1], p.statusErr[2], 1.f);
                break;
            case aicad::ui::EventEntryKind::NodePaused:
            case aicad::ui::EventEntryKind::NodeApprovalRequired:
            case aicad::ui::EventEntryKind::NodeManualRequired:
                col = ImVec4(p.statusWarn[0], p.statusWarn[1], p.statusWarn[2], 1.f);
                break;
            case aicad::ui::EventEntryKind::AgentThought:
                col = ImVec4(p.textDim[0], p.textDim[1], p.textDim[2], 0.75f);
                break;
            default:
                break;
        }

        // Timestamp prefix (short)
        if (!ev.ts.empty() && ev.ts.size() >= 19) {
            ImGui::TextDisabled("%s", ev.ts.substr(11, 8).c_str());  // HH:MM:SS
            ImGui::SameLine();
        }

        ImGui::TextColored(col, "%s", ev.headline.c_str());
    }

    if (scrollToBottom)
        ImGui::SetScrollHereY(1.0f);
}

void MissionControlPanel::drawSteering(AppState& /*st*/) {
    const auto& p = ui::palette;
    auto& steer = view_.steering;

    ImGui::TextDisabled("Steering");
    ImGui::Separator();

    if (steer.mode == aicad::ui::SteeringMode::None ||
        steer.mode == aicad::ui::SteeringMode::Done) {
        ImGui::TextDisabled("(no active mission)");
        return;
    }

    if (steer.mode == aicad::ui::SteeringMode::Running) {
        if (ImGui::Button("Pause")) {
            // TODO: emit dag_pause RPC call (T-F17 worker RPC seam)
            std::printf("mission_control: user pause requested for run %s\n",
                        view_.runId.c_str());
        }
        // Autonomy dial
        ImGui::SameLine();
        const char* levels[] = {"Auto", "Approve", "Manual"};
        ImGui::SetNextItemWidth(100.f);
        if (ImGui::Combo("Autonomy##dial", &steer.autonomyLevel, levels, 3)) {
            std::printf("mission_control: autonomy changed to %s for run %s\n",
                        levels[steer.autonomyLevel], view_.runId.c_str());
        }
        return;
    }

    if (steer.mode == aicad::ui::SteeringMode::Paused) {
        ImGui::TextColored(
            ImVec4(p.statusWarn[0], p.statusWarn[1], p.statusWarn[2], 1.f),
            "Gate paused — node: %s", steer.pendingNodeId.c_str());
        ImGui::Spacing();

        if (steer.canApprove && ImGui::Button("Approve")) {
            std::printf("mission_control: approve for run %s node %s\n",
                        view_.runId.c_str(), steer.pendingNodeId.c_str());
            // TODO: emit dag_approve RPC (worker_rpc.py)
        }
        if (steer.canReject) {
            ImGui::SameLine();
            if (ImGui::Button("Reject")) {
                std::printf("mission_control: reject for run %s\n", view_.runId.c_str());
            }
        }

        if (steer.canInject) {
            ImGui::Spacing();
            ImGui::TextDisabled("Inject instruction at next node boundary:");
            char buf[256];
            std::strncpy(buf, steer.injectBuffer.c_str(), sizeof(buf) - 1);
            buf[sizeof(buf) - 1] = '\0';
            ImGui::SetNextItemWidth(-1.f);
            if (ImGui::InputText("##inject", buf, sizeof(buf)))
                steer.injectBuffer = buf;
            ImGui::SameLine();
            if (ImGui::SmallButton("Send##inject")) {
                std::printf("mission_control: inject '%s' for run %s\n",
                            steer.injectBuffer.c_str(), view_.runId.c_str());
                steer.injectBuffer.clear();
            }
        }

        // Autonomy dial
        ImGui::Spacing();
        const char* levels[] = {"Auto", "Approve", "Manual"};
        ImGui::SetNextItemWidth(120.f);
        if (ImGui::Combo("Autonomy##dial2", &steer.autonomyLevel, levels, 3)) {
            std::printf("mission_control: autonomy changed to %s\n",
                        levels[steer.autonomyLevel]);
        }
    }
}

void MissionControlPanel::drawReplaySelector() {
    const auto& p = ui::palette;
    ImGui::TextDisabled("No active mission.");
    ImGui::Spacing();

    if (replayList_.empty()) {
        ImGui::TextDisabled("(no past runs in cache/runs/)");
        return;
    }

    ImGui::TextDisabled("Load a past run:");
    const char* label = (selectedReplay_ >= 0 && selectedReplay_ < (int)replayList_.size())
        ? replayList_[selectedReplay_].label.c_str()
        : "(select)";
    if (ImGui::BeginCombo("##mc_replay", label)) {
        for (int i = 0; i < (int)replayList_.size(); ++i) {
            bool sel = (selectedReplay_ == i);
            if (ImGui::Selectable(replayList_[i].label.c_str(), sel)) {
                selectedReplay_ = i;
                loadReplay(replayList_[i].jsonlPath);
            }
        }
        ImGui::EndCombo();
    }

    if (replayLoaded_ && selectedReplay_ >= 0 && !replayList_.empty()) {
        ImGui::TextColored(ImVec4(p.statusOk[0], p.statusOk[1], p.statusOk[2], 1.f),
                           "Loaded: %s (%zu events)",
                           replayList_[selectedReplay_].label.c_str(),
                           view_.events.size());
    } else if (!replayLoaded_ && selectedReplay_ >= 0) {
        ImGui::TextColored(ImVec4(p.statusErr[0], p.statusErr[1], p.statusErr[2], 1.f),
                           "Failed to load events.jsonl");
    }
}

}  // namespace ui
