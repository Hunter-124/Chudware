#include "run_panel.h"

#include <cstddef>
#include <string>

#include "imgui.h"

#include "approval_dialog.h"
#include "manufacture_panel.h"
#include "mfg_mirror.h"
#include "../metrics.h"
#include "../../core/app_state.h"
#include "../../ui_theme.h"

namespace ui {
namespace {

// Narrow-rail label/value row: the value stays on the label's line when it fits, otherwise it
// drops to a wrapped line so long ids and hashes never run off the pinned column.
void kvRow(const char* label, const std::string& value) {
    const char* text = value.empty() ? "—" : value.c_str();
    const float gap = metrics::U(1);
    const float inlineBudget = ImGui::GetContentRegionAvail().x -
                               ImGui::CalcTextSize(label).x - gap;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                palette.textDim[2], palette.textDim[3]));
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
    if (ImGui::CalcTextSize(text).x <= inlineBudget) {
        ImGui::SameLine(0, gap);
        ImGui::TextUnformatted(text);
        return;
    }
    ImGui::Indent(metrics::U(2));
    ImGui::TextWrapped("%s", text);
    ImGui::Unindent(metrics::U(2));
}

void drawFreshRow(const char* label, const MfgFreshValue& value, const std::string& unit = {}) {
    const MfgFreshPresentation display = presentMfgFresh(value, unit);
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                palette.textDim[2], palette.textDim[3]));
    ImGui::TextUnformatted(label);
    ImGui::PopStyleColor();
    ImGui::SameLine(0, metrics::U(1));
    const float* color = display.ok ? palette.statusOk
                       : display.stale ? palette.statusWarn : palette.textDim;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(color[0], color[1], color[2], color[3]));
    ImGui::TextWrapped("%s", display.current.c_str());
    ImGui::PopStyleColor();
    if (!display.detail.empty()) {
        ImGui::Indent(metrics::U(2));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                    palette.textDim[2], palette.textDim[3]));
        ImGui::TextWrapped("%s", display.detail.c_str());
        ImGui::PopStyleColor();
        ImGui::Unindent(metrics::U(2));
    }
}

const float* verdictColor(const std::string& verdict) {
    if (verdict == "pass") return palette.statusOk;
    if (verdict == "fail") return palette.statusErr;
    if (verdict == "not_applicable") return palette.textDim;
    return palette.statusWarn;
}

void drawGateReport(const MfgGateReport& report) {
    ImGui::TextUnformatted("Gate report");
    if (report.seq == 0) {
        ImGui::TextDisabled("No seq-stamped gate report. Run preflight to establish one.");
        return;
    }
    ImGui::Text("seq %llu", static_cast<unsigned long long>(report.seq));
    if (!report.reportRef.empty()) {
        ImGui::Indent(metrics::U(2));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                    palette.textDim[2], palette.textDim[3]));
        ImGui::TextWrapped("%s", report.reportRef.c_str());
        ImGui::PopStyleColor();
        ImGui::Unindent(metrics::U(2));
    }

    ImGui::Spacing();
    kvRow("Stage 1 admission", report.stage1.empty() ? std::string("unknown") : report.stage1);
    if (!report.stage1Why.empty()) {
        ImGui::Indent(metrics::U(2));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                    palette.textDim[2], palette.textDim[3]));
        ImGui::TextWrapped("%s", report.stage1Why.c_str());
        ImGui::PopStyleColor();
        ImGui::Unindent(metrics::U(2));
    }

    ImGui::Spacing();
    ImGui::TextUnformatted("Stage 2 guards");
    if (report.guards.empty()) {
        ImGui::TextDisabled("No Stage-2 guard rows were returned.");
        return;
    }
    // Stacked guard cards, not a 3-column table: at ~360 logical px the reason column
    // collapsed to a few characters and the waiver line was unreadable.
    for (std::size_t i = 0; i < report.guards.size(); ++i) {
        const MfgGateGuard& guard = report.guards[i];
        ImGui::PushID(static_cast<int>(i));
        const std::string verdict = guard.verdict.empty() ? "unknown" : guard.verdict;
        const float* color = verdictColor(verdict);
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(color[0], color[1], color[2], color[3]));
        ImGui::TextUnformatted(verdict.c_str());
        ImGui::PopStyleColor();
        ImGui::SameLine(0, metrics::U(1));
        ImGui::TextWrapped("%s", guard.name.empty() ? "unnamed guard" : guard.name.c_str());
        ImGui::Indent(metrics::U(2));
        if (!guard.why.empty()) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                        palette.textDim[2], palette.textDim[3]));
            ImGui::TextWrapped("%s", guard.why.c_str());
            ImGui::PopStyleColor();
        }
        if (guard.waiver) {
            ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                                        palette.statusWarn[2], palette.statusWarn[3]));
            ImGui::TextWrapped("Waiver by %s at seq %llu — %s",
                               guard.waiver->by.empty() ? "unnamed actor" : guard.waiver->by.c_str(),
                               static_cast<unsigned long long>(guard.waiver->atSeq),
                               guard.waiver->reason.empty() ? "no reason supplied"
                                                            : guard.waiver->reason.c_str());
            ImGui::PopStyleColor();
        } else if (guard.why.empty()) {
            ImGui::TextDisabled("—");
        }
        ImGui::Unindent(metrics::U(2));
        ImGui::PopID();
    }
}

void drawDisabledReason(bool disabled, const std::string& reason) {
    if (!disabled) return;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                                palette.statusWarn[2], palette.statusWarn[3]));
    ImGui::TextWrapped("%s", reason.c_str());
    ImGui::PopStyleColor();
}

}  // namespace

void drawManufactureRunPanel(AppState& st) {
    if (!st.mfgMirror) {
        ImGui::TextDisabled("Manufacturing mirror unavailable.");
        return;
    }
    MfgMirror& mirror = *st.mfgMirror;

    if (st.fonts.h2) ImGui::PushFont(st.fonts.h2);
    ImGui::TextUnformatted("Run control");
    if (st.fonts.h2) ImGui::PopFont();
    ImGui::SameLine(0, metrics::U(2));
    if (ImGui::SmallButton("Refresh runs")) {
        nlohmann::json params = nlohmann::json::object();
        if (!st.mfg.selectedDeviceId.empty()) params["device_id"] = st.mfg.selectedDeviceId;
        queueManufactureRequest(st, "mfg_run_list", std::move(params), "refresh_runs");
    }

    if (mirror.runs().empty()) {
        ImGui::Spacing();
        ImGui::TextWrapped("No runs have been proposed. Generate and register an artifact, then run preflight.");
        return;
    }

    for (const MfgRun& row : mirror.runs()) {
        ImGui::PushID(row.runId.c_str());
        const MfgFreshPresentation freshState = presentMfgFresh(row.state);
        const std::string state = row.stateLabel.empty() ? freshState.current : row.stateLabel;
        // Rail-width row: the id is the selectable, the state wraps underneath it.
        if (ImGui::Selectable(row.runId.c_str(), st.mfg.selectedRunId == row.runId)) {
            st.mfg.selectedRunId = row.runId;
            queueManufactureRequest(st, "mfg_run_describe", {{"run_id", row.runId}});
            if (!row.gates.reportRef.empty())
                queueManufactureRequest(st, "mfg_gate_report", {{"ref", row.gates.reportRef}});
        }
        ImGui::Indent(metrics::U(2));
        ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.textDim[0], palette.textDim[1],
                                                    palette.textDim[2], palette.textDim[3]));
        ImGui::TextWrapped("%s", state.c_str());
        ImGui::PopStyleColor();
        ImGui::Unindent(metrics::U(2));
        ImGui::PopID();
    }

    const MfgRun* run = mirror.run(st.mfg.selectedRunId);
    if (!run) {
        ImGui::Spacing();
        ImGui::TextDisabled("Select a run to inspect immutable approval and gate evidence.");
        return;
    }

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    kvRow("Run", run->runId);
    kvRow("Device", run->deviceId);
    kvRow("Artifact", run->artifactHash);
    if (!run->initiatedBy.empty()) kvRow("initiated_by", run->initiatedBy);
    kvRow("Run state", run->stateLabel.empty() ? std::string("unknown") : run->stateLabel);
    drawFreshRow("Progress", run->progress);
    drawFreshRow("Controller snapshot", run->snapshot);

    ImGui::Spacing();
    drawGateReport(run->gates);

    ImGui::Spacing();
    const bool busy = st.mfg.requestInFlight;
    const bool startDisabled = busy || !run->start.enabled;
    ImGui::BeginDisabled(startDisabled);
    if (ImGui::Button("Start approved run")) {
        queueManufactureRequest(st, "mfg_run_start",
                                {{"run_id", run->runId}, {"approval_id", run->approvalId}},
                                "start_run");
    }
    ImGui::EndDisabled();
    drawDisabledReason(startDisabled, busy
        ? "Start unavailable — another manufacturing request is in flight."
        : run->start.reason);

    ImGui::BeginDisabled(busy || !run->hold.enabled);
    if (ImGui::Button("Request hold"))
        queueManufactureRequest(st, "mfg_run_hold", {{"run_id", run->runId}}, "hold_run");
    ImGui::EndDisabled();
    ImGui::SameLine(0, metrics::U(2));
    ImGui::BeginDisabled(busy);
    if (ImGui::Button("Abort run"))
        queueManufactureRequest(st, "mfg_run_abort", {{"run_id", run->runId}}, "abort_run");
    ImGui::EndDisabled();
    drawDisabledReason(busy || !run->hold.enabled, busy
        ? "Hold and abort unavailable — another manufacturing request is in flight."
        : run->hold.reason);

    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusErr[0], palette.statusErr[1],
                                                palette.statusErr[2], palette.statusErr[3]));
    ImGui::TextWrapped("Software stop is a request to the controller. It is not an emergency stop. Use the machine's E-stop for emergencies.");
    ImGui::PopStyleColor();

    ImGui::Spacing();
    ImGui::BeginDisabled(busy);
    if (ImGui::Button("Run preflight")) {
        queueManufactureRequest(st, "mfg_preflight_run",
                                {{"run_id", run->runId}, {"device_id", run->deviceId}},
                                "preflight_run");
    }
    ImGui::Spacing();
    if (ImGui::Button("Review approval binding")) {
        st.mfg.openApprovalDialog = true;
        st.mfg.exercisableBy = 0;
        queueManufactureRequest(st, "mfg_approval_propose",
                                {{"run_id", run->runId}, {"device_id", run->deviceId},
                                 {"exercisable_by", "human_only"}},
                                "approval_propose_open");
    }
    ImGui::EndDisabled();
    drawDisabledReason(busy, "Preflight and approval review unavailable — another manufacturing request is in flight.");
}

}  // namespace ui
