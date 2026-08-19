#include "run_panel.h"

#include <cstddef>
#include <string>

#include "imgui.h"

#include "manufacture_panel.h"
#include "mfg_mirror.h"
#include "../metrics.h"
#include "../widgets/kit.h"
#include "../../core/app_state.h"

namespace ui {
namespace {

// Stage-2 guard verdict -> semantic tone. Exactly the mapping the old verdictColor() carried:
// a pass is healthy, a fail blocks, not_applicable is no judgement at all, and any verdict this
// build does not recognise is surfaced as degraded rather than quietly painted as a pass.
kit::Tone verdictTone(const std::string& verdict) {
    if (verdict == "pass")           return kit::Tone::Ok;
    if (verdict == "fail")           return kit::Tone::Err;
    if (verdict == "not_applicable") return kit::Tone::Neutral;
    return kit::Tone::Warn;
}

// Stage-1 admission -> tone. Only a literal "admitted" earns the healthy tone; an explicit
// refusal is blocking; a missing or composite admission string stays neutral/degraded so the
// panel never invents an admission the gateway did not report.
kit::Tone admissionTone(const std::string& stage1) {
    if (stage1 == "admitted") return kit::Tone::Ok;
    if (stage1 == "refused" || stage1 == "denied" || stage1 == "rejected") return kit::Tone::Err;
    if (stage1.empty() || stage1 == "unknown") return kit::Tone::Neutral;
    return kit::Tone::Warn;
}

void drawGateReport(const MfgGateReport& report) {
    if (!kit::cardBegin("mfg_gate", "Gate report", "check")) {
        kit::cardEnd();
        return;
    }
    if (report.seq == 0) {
        kit::dimText("No seq-stamped gate report. Run preflight to establish one.");
        kit::cardEnd();
        return;
    }

    kit::keyValueRow("seq", std::to_string(static_cast<unsigned long long>(report.seq)).c_str());
    if (!report.reportRef.empty()) kit::keyValueRow("Report ref", report.reportRef.c_str());

    const std::string stage1 = report.stage1.empty() ? std::string("unknown") : report.stage1;
    kit::gapRow();
    kit::dimText("Stage 1 admission");
    kit::chip(stage1.c_str(), admissionTone(report.stage1), /*dot=*/true);
    if (!report.stage1Why.empty()) kit::dimText(report.stage1Why.c_str());

    kit::sectionHeader("Stage 2 guards", nullptr, nullptr,
                       static_cast<int>(report.guards.size()));
    if (report.guards.empty()) {
        kit::dimText("No Stage-2 guard rows were returned.");
        kit::cardEnd();
        return;
    }

    // Guard rows, not a 3-column table and not a nested card stack: at ~360 logical px the
    // reason column collapsed to a few characters, and a card inside the report card spends
    // its whole width on chrome. One frosted row carries name + verdict; the reason and the
    // waiver stay full-width dim text underneath it.
    for (std::size_t i = 0; i < report.guards.size(); ++i) {
        const MfgGateGuard& guard = report.guards[i];
        const std::string   verdict = guard.verdict.empty() ? std::string("unknown")
                                                            : guard.verdict;
        const std::string   name    = guard.name.empty() ? std::string("unnamed guard")
                                                         : guard.name;
        ImGui::PushID(static_cast<int>(i));

        // Chip geometry mirrors kit::chip: padX*2 (U3) + dot (U1.5) + gap (U1).
        const float rowW  = ImGui::GetContentRegionAvail().x;
        const float chipW = ImGui::CalcTextSize(verdict.c_str()).x + metrics::U(5.5f);
        const float chipH = ImGui::GetTextLineHeight() + metrics::U(1.0f);
        kit::rowBegin("mfg_guard", chipH + kit::padY() * 2.0f);
        const ImVec2 origin  = ImGui::GetCursorScreenPos();
        const float  contentW = rowW - kit::padX() * 2.0f;
        const std::string shown =
            kit::elide(name.c_str(), contentW - chipW - metrics::U(1.5f));
        ImGui::SetCursorScreenPos(
            ImVec2(origin.x, origin.y + (chipH - ImGui::GetTextLineHeight()) * 0.5f));
        ImGui::TextUnformatted(shown.c_str());
        if (ImGui::IsItemHovered() && shown != name) ImGui::SetTooltip("%s", name.c_str());
        ImGui::SetCursorScreenPos(ImVec2(origin.x + contentW - chipW, origin.y));
        kit::chip(verdict.c_str(), verdictTone(verdict), /*dot=*/true);
        kit::rowEnd();

        if (!guard.why.empty()) kit::dimText(guard.why.c_str());
        if (guard.waiver) {
            const std::string waiver =
                "Waiver by " +
                (guard.waiver->by.empty() ? std::string("unnamed actor") : guard.waiver->by) +
                " at seq " +
                std::to_string(static_cast<unsigned long long>(guard.waiver->atSeq)) + " — " +
                (guard.waiver->reason.empty() ? std::string("no reason supplied")
                                              : guard.waiver->reason);
            kit::inlineStatus(waiver.c_str(), kit::Tone::Warn);
        } else if (guard.why.empty()) {
            kit::dimText("—");
        }
        ImGui::PopID();
        kit::gapRow();
    }
    kit::cardEnd();
}

}  // namespace

void drawManufactureRunPanel(AppState& st) {
    if (!st.mfgMirror) {
        kit::blockedState("Manufacturing mirror unavailable.", nullptr);
        return;
    }
    MfgMirror& mirror = *st.mfgMirror;

    auto refreshRuns = [&st]() {
        nlohmann::json params = nlohmann::json::object();
        if (!st.mfg.selectedDeviceId.empty()) params["device_id"] = st.mfg.selectedDeviceId;
        queueManufactureRequest(st, "mfg_run_list", std::move(params), "refresh_runs");
    };

    // Section header with its action right-aligned ON the header line: the pinned rail is ~360dp
    // and cannot spend a whole row on a Refresh button. The action is sized to the header's own
    // text line so it never eats into the head gap below it.
    const float  railW   = ImGui::GetContentRegionAvail().x;
    const ImVec2 headTop = ImGui::GetCursorScreenPos();
    kit::sectionHeader("Run control", "run_study", nullptr,
                       static_cast<int>(mirror.runs().size()), /*first=*/true);
    const ImVec2 afterHead = ImGui::GetCursorScreenPos();
    const float  actionSz  = ImGui::GetTextLineHeight();
    ImGui::SetCursorScreenPos(ImVec2(headTop.x + railW - actionSz, headTop.y));
    if (kit::iconButton("mfg_run_refresh", "refresh", "Refresh runs", true, kit::Tone::Accent,
                        actionSz)) {
        refreshRuns();
    }
    ImGui::SetCursorScreenPos(afterHead);

    if (mirror.runs().empty()) {
        if (kit::emptyState("run_study", "No runs proposed",
                            "No runs have been proposed. Generate and register an artifact, then run preflight.",
                            "Refresh runs")) {
            refreshRuns();
        }
        return;
    }

    for (const MfgRun& run : mirror.runs()) {
        const MfgFreshPresentation freshState = presentMfgFresh(run.state);
        const std::string state = run.stateLabel.empty() ? freshState.current : run.stateLabel;
        // Rail-width row: the id leads and elides, the run state rides the dim second line and
        // the owning device takes the right-hand meta slot.
        std::string tooltip = run.runId + "\n" + state;
        if (!freshState.detail.empty()) tooltip += "\n" + freshState.detail;

        kit::RowStyle style;
        style.icon      = "run_study";
        style.iconTone  = freshState.ok ? kit::Tone::Ok
                        : freshState.stale ? kit::Tone::Warn : kit::Tone::Neutral;
        style.secondary = state.c_str();
        style.meta      = run.deviceId.empty() ? nullptr : run.deviceId.c_str();
        style.tooltip   = tooltip.c_str();
        style.selected  = st.mfg.selectedRunId == run.runId;
        if (kit::row(run.runId.c_str(), run.runId.c_str(), style)) {
            st.mfg.selectedRunId = run.runId;
            queueManufactureRequest(st, "mfg_run_describe", {{"run_id", run.runId}});
            if (!run.gates.reportRef.empty())
                queueManufactureRequest(st, "mfg_gate_report", {{"ref", run.gates.reportRef}});
        }
        kit::gapRow();
    }

    const MfgRun* run = mirror.run(st.mfg.selectedRunId);
    if (!run) {
        kit::gapRow();
        kit::dimText("Select a run to inspect immutable approval and gate evidence.");
        return;
    }

    const MfgFreshPresentation progress = presentMfgFresh(run->progress);
    const MfgFreshPresentation snapshot = presentMfgFresh(run->snapshot);

    kit::gapSection();
    if (kit::cardBegin("mfg_run_detail", run->runId.c_str(), "run_study")) {
        kit::keyValueRow("Run", run->runId.c_str());
        kit::keyValueRow("Device", run->deviceId.empty() ? "—" : run->deviceId.c_str());
        kit::keyValueRow("Artifact", run->artifactHash.empty() ? "—" : run->artifactHash.c_str());
        if (!run->initiatedBy.empty()) kit::keyValueRow("initiated_by", run->initiatedBy.c_str());
        kit::keyValueRow("Run state",
                         run->stateLabel.empty() ? "unknown" : run->stateLabel.c_str());

        // The reported value ALWAYS renders as text (an em dash plus its why when the sample is
        // not current). The bar is painted only when the gateway actually reported a fraction —
        // an unknown progress must never look like a run sitting at 0 %.
        kit::keyValueRow("Progress", progress.current.c_str(),
                         progress.ok ? kit::Tone::Ok
                       : progress.stale ? kit::Tone::Warn : kit::Tone::Neutral);
        if (run->progress.current() && run->progress.value.is_number()) {
            const double frac = run->progress.value.get<double>();
            if (frac >= 0.0 && frac <= 1.0)
                kit::progressBar(static_cast<float>(frac), nullptr, kit::Tone::Accent);
        }
        kit::dimText(progress.detail.c_str());

        kit::keyValueRow("Controller snapshot", snapshot.current.c_str(),
                         snapshot.ok ? kit::Tone::Ok
                       : snapshot.stale ? kit::Tone::Warn : kit::Tone::Neutral);
        kit::dimText(snapshot.detail.c_str());
    }
    kit::cardEnd();

    kit::gapSection();
    drawGateReport(run->gates);

    kit::gapSection();
    const bool        busy = st.mfg.requestInFlight;
    const char* const startBusyWhy =
        "Start unavailable — another manufacturing request is in flight.";
    const char* const holdBusyWhy =
        "Hold and abort unavailable — another manufacturing request is in flight.";
    const char* const preflightBusyWhy =
        "Preflight and approval review unavailable — another manufacturing request is in flight.";

    if (kit::primaryButton("Start approved run", !busy && run->start.enabled,
                           busy ? startBusyWhy : run->start.reason.c_str())) {
        queueManufactureRequest(st, "mfg_run_start",
                                {{"run_id", run->runId}, {"approval_id", run->approvalId}},
                                "start_run");
    }

    kit::gapRow();
    if (kit::secondaryButton("Request hold", !busy && run->hold.enabled,
                             busy ? holdBusyWhy : run->hold.reason.c_str())) {
        queueManufactureRequest(st, "mfg_run_hold", {{"run_id", run->runId}}, "hold_run");
    }
    ImGui::SameLine(0, metrics::camBtnGap());
    if (kit::dangerButton("Abort run", !busy, holdBusyWhy))
        queueManufactureRequest(st, "mfg_run_abort", {{"run_id", run->runId}}, "abort_run");

    kit::gapRow();
    kit::inlineStatus("Software stop is a request to the controller. It is not an emergency stop. Use the machine's E-stop for emergencies.",
                      kit::Tone::Err);

    kit::gapRow();
    if (kit::secondaryButton("Run preflight", !busy, preflightBusyWhy)) {
        queueManufactureRequest(st, "mfg_preflight_run",
                                {{"run_id", run->runId}, {"device_id", run->deviceId}},
                                "preflight_run");
    }
    kit::gapRow();
    if (kit::secondaryButton("Review approval binding", !busy, preflightBusyWhy)) {
        st.mfg.openApprovalDialog = true;
        st.mfg.exercisableBy = 0;
        queueManufactureRequest(st, "mfg_approval_propose",
                                {{"run_id", run->runId}, {"device_id", run->deviceId},
                                 {"exercisable_by", "human_only"}},
                                "approval_propose_open");
    }
}

}  // namespace ui
