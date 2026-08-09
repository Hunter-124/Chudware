#include "approval_dialog.h"

#include <string>

#include "imgui.h"
#include "imgui_internal.h"

#include "manufacture_panel.h"
#include "mfg_mirror.h"
#include "../metrics.h"
#include "../../core/app_state.h"
#include "../../ui_theme.h"

namespace ui {
namespace {

std::string jsonText(const nlohmann::json& object, const char* key) {
    if (!object.is_object()) return {};
    const auto it = object.find(key);
    if (it == object.end() || it->is_null()) return {};
    return it->is_string() ? it->get<std::string>() : it->dump();
}

void bindingRow(const char* label, const std::string& value) {
    ImGui::TextDisabled("%s", label);
    ImGui::SameLine(0, metrics::U(2));
    ImGui::TextWrapped("%s", value.empty() ? "—" : value.c_str());
}

void immutableHashRow(const char* label, const std::string& value) {
    ImGui::TextDisabled("%s", label);
    std::string buffer = value.empty() ? "—" : value;
    ImGui::SetNextItemWidth(-1);
    ImGui::InputText("##immutable_hash", buffer.data(), buffer.size() + 1,
                     ImGuiInputTextFlags_ReadOnly);
}

void warning(const std::string& text) {
    if (text.empty()) return;
    ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(palette.statusWarn[0], palette.statusWarn[1],
                                                palette.statusWarn[2], palette.statusWarn[3]));
    ImGui::TextWrapped("%s", text.c_str());
    ImGui::PopStyleColor();
}

}  // namespace

void drawManufactureApprovalDialog(AppState& st) {
    if (!st.mfg.openApprovalDialog || !st.mfgMirror) return;

    const MfgRun* run = st.mfgMirror->run(st.mfg.selectedRunId);
    const MfgDevice* device = st.mfgMirror->device(st.mfg.selectedDeviceId);
    if (!device && run) device = st.mfgMirror->device(run->deviceId);
    const MfgApproval& approval = st.mfgMirror->approval();

    ImGui::SetNextWindowSize(ImVec2(metrics::U(150), metrics::U(130)), ImGuiCond_Appearing);
    ImGui::SetNextWindowPos(ImGui::GetMainViewport()->GetCenter(), ImGuiCond_Appearing,
                            ImVec2(0.5f, 0.5f));
    ImGui::PushStyleColor(ImGuiCol_WindowBg, ImVec4(palette.panelBg[0], palette.panelBg[1],
                                                    palette.panelBg[2], palette.panelBg[3]));
    bool open = st.mfg.openApprovalDialog;
    const bool visible = ImGui::Begin("Manufacturing approval binding", &open,
                                      ImGuiWindowFlags_NoCollapse | ImGuiWindowFlags_NoSavedSettings);
    ImGui::PopStyleColor();
    st.mfg.openApprovalDialog = open;
    if (!visible) {
        ImGui::End();
        return;
    }

    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding,
                        ImVec2(metrics::cpMenuGutter(), metrics::cpMenuGutter()));
    ImGui::BeginChild("##mfg_approval_scroll", ImVec2(0, -ImGui::GetFrameHeightWithSpacing()),
                      ImGuiChildFlags_AlwaysUseWindowPadding,
                      ImGuiWindowFlags_NoScrollbar);
    ImGui::PopStyleVar();
    const ImVec2 childPos = ImGui::GetWindowPos();
    const ImVec2 childSize = ImGui::GetWindowSize();
    const ImRect childRect(childPos,
                           ImVec2(childPos.x + childSize.x, childPos.y + childSize.y));
    const AdaptiveSurface& surf = adaptivePanel(panelId("mfg_approval_scroll"), childRect);
    ImGui::PushStyleColor(ImGuiCol_Text, surf.textPrimary);

    if (st.fonts.h2) ImGui::PushFont(st.fonts.h2);
    ImGui::TextUnformatted("Human approval");
    if (st.fonts.h2) ImGui::PopFont();
    ImGui::TextWrapped("This dialog authors a proposal and requests the arbiter's human approval act. The GUI does not validate the binding and cannot approve on its own.");

    ImGui::Spacing();
    ImGui::TextUnformatted("Who may exercise this single-use approval?");
    if (ImGui::RadioButton("you only", st.mfg.exercisableBy == 0)) st.mfg.exercisableBy = 0;
    const bool agentWithinCeiling = device && device->maxExercisableBy == "human_or_agent";
    ImGui::BeginDisabled(!agentWithinCeiling);
    if (ImGui::RadioButton("you, or the agent on your behalf", st.mfg.exercisableBy == 1))
        st.mfg.exercisableBy = 1;
    ImGui::EndDisabled();
    bindingRow("Commissioning ceiling", device ? device->maxExercisableBy : "unknown");
    if (!agentWithinCeiling)
        warning("Agent exercise unavailable — the commissioning ceiling is human_only. The request is not silently downgraded.");

    ImGui::Spacing();
    const bool cannotDraft = st.mfg.requestInFlight || !run || !device ||
                             (st.mfg.exercisableBy == 1 && !agentWithinCeiling);
    ImGui::BeginDisabled(cannotDraft);
    if (ImGui::Button("Author binding proposal")) {
        queueManufactureRequest(st, "mfg_approval_propose",
                                {{"run_id", run->runId}, {"device_id", device->deviceId},
                                 {"exercisable_by", st.mfg.exercisableBy == 0
                                                           ? "human_only" : "human_or_agent"}},
                                "approval_propose");
    }
    ImGui::EndDisabled();
    if (cannotDraft) {
        if (st.mfg.requestInFlight)
            warning("Binding proposal unavailable — another manufacturing request is in flight.");
        else if (!run || !device)
            warning("Binding proposal unavailable — select a run and its device.");
        else
            warning("Binding proposal unavailable — requested exercisable_by exceeds the commissioning ceiling.");
    }

    ImGui::Spacing();
    ImGui::Separator();
    ImGui::Spacing();
    bindingRow("Gateway status", approval.status);
    if (!approval.blockedReason.empty() && approval.status != "ready" &&
        approval.status != "waivers-present" && approval.status != "unapproved")
        warning(approval.blockedReason);

    const nlohmann::json& binding = approval.binding;
    std::string machine = jsonText(binding, "machine_fingerprint");
    if (machine.empty() && binding.contains("machine") && binding["machine"].is_object())
        machine = jsonText(binding["machine"], "fingerprint_sha256");
    if (machine.empty() && device) machine = device->fingerprint;
    std::string artifactHash = jsonText(binding, "artifact_hash");
    if (artifactHash.empty() && binding.contains("artifact") && binding["artifact"].is_object())
        artifactHash = jsonText(binding["artifact"], "hash");
    if (artifactHash.empty() && run) artifactHash = run->artifactHash;
    bindingRow("Machine fingerprint", machine);
    immutableHashRow("Artifact hash", artifactHash);
    bindingRow("Artifact kind", jsonText(binding, "artifact_kind"));
    bindingRow("Post profile", jsonText(binding, "post_profile"));
    bindingRow("Job revision", jsonText(binding, "job_revision"));
    bindingRow("Setup revision", jsonText(binding, "setup_revision"));
    bindingRow("Tool / fixture / WCS digest", jsonText(binding, "tool_fixture_wcs_digest"));
    bindingRow("Evidence-set digest", jsonText(binding, "evidence_set_digest"));
    bindingRow("Gate report", jsonText(binding, "gate_report_ref"));
    bindingRow("Expiry", jsonText(binding, "expires_at"));
    bindingRow("max_starts", jsonText(binding, "max_starts"));
    bindingRow("exercisable_by", jsonText(binding, "exercisable_by"));

    ImGui::PopStyleColor();
    drawWindowPillScrollbarV(surf);
    ImGui::EndChild();

    const bool serverReady = approval.status == "unapproved" ||
                             approval.status == "ready" ||
                             approval.status == "waivers-present";
    const bool approveDisabled = st.mfg.requestInFlight || !serverReady ||
                                 approval.proposalId.empty() || !device || device->leaseId.empty();
    ImGui::BeginDisabled(approveDisabled);
    if (ImGui::Button("Approve this binding")) {
        queueManufactureRequest(st, "mfg_approval_create",
                                {{"lease_id", device->leaseId},
                                 {"proposal_id", approval.proposalId},
                                 {"approver_auth", {{"identity", "desktop_operator"},
                                                    {"auth", "interactive"}}}},
                                "approval_create");
    }
    ImGui::EndDisabled();
    ImGui::SameLine(0, metrics::U(2));
    if (ImGui::Button("Close")) st.mfg.openApprovalDialog = false;

    if (approveDisabled) {
        if (st.mfg.requestInFlight)
            warning("Approve unavailable — another manufacturing request is in flight.");
        else if (!serverReady)
            warning(approval.blockedReason.empty()
                        ? "Approve unavailable — the gateway has not marked this binding ready."
                        : approval.blockedReason);
        else if (approval.proposalId.empty())
            warning("Approve unavailable — the binding has no proposal_id.");
        else
            warning("Approve unavailable — no command lease is bound to the selected device.");
    }

    ImGui::End();
}

}  // namespace ui
