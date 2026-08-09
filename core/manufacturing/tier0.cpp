#include "manufacturing/tier0.h"

#include <stdexcept>

namespace chudware::mfg::tier0 {
namespace {

bool is_router_or_mill(device::MachineClass value) {
    return value == device::MachineClass::OpenFrameRouter ||
           value == device::MachineClass::OpenFrameMill ||
           value == device::MachineClass::EnclosedInterlockedMill;
}

bool stage_at_least(device::EmulatorStage actual, device::EmulatorStage required) {
    return static_cast<int>(actual) >= static_cast<int>(required);
}

Tier0Refusal refusal(const Context& context, const Requirement& requirement,
                     std::string why, std::vector<std::string> remedy = {}) {
    (void)context;
    Tier0Refusal result;
    result.action = requirement.action;
    result.prerequisite = requirement.prerequisite;
    result.why = std::move(why);
    if (requirement.capability_level)
        result.missing_level = std::string(device::to_string(*requirement.capability_level));
    result.remedy = std::move(remedy);
    result.artifact_dependent = requirement.artifact_dependent;
    return result;
}

}  // namespace

std::string_view to_string(Action value) {
    switch (value) {
        case Action::DeviceClaim: return "mfg_device_claim";
        case Action::RunSubmit: return "mfg_run_submit";
        case Action::RunStart: return "mfg_run_start";
        case Action::ApprovalIssue: return "mfg_approval_issue";
        case Action::RunResume: return "mfg_run_resume";
        case Action::AnyWrite: return "any_write";
    }
    throw std::invalid_argument("invalid tier0 Action");
}

std::string_view to_string(ArtifactKind value) {
    switch (value) {
        case ArtifactKind::Nc: return "nc";
        case ArtifactKind::Gcode: return "gcode";
        case ArtifactKind::PrintJob: return "print_job";
    }
    throw std::invalid_argument("invalid ArtifactKind");
}

std::vector<Requirement> classify(Action action, ArtifactKind artifact_kind,
                                  device::MachineClass machine_class) {
    const std::string action_name(to_string(action));
    std::vector<Requirement> rows;
    switch (action) {
        case Action::DeviceClaim:
            rows.push_back({action_name, "transport.available", false, device::CapabilityLevel::Transport});
            rows.push_back({action_name, "exclusive_port_acknowledged", false, std::nullopt});
            break;
        case Action::RunSubmit:
            rows.push_back({action_name, "artifact.present_and_hashed", false, std::nullopt});
            if (artifact_kind != ArtifactKind::PrintJob)
                rows.push_back({action_name, "post_profile.bound", true, device::CapabilityLevel::Post});
            rows.push_back({action_name, "machine_class.known", false, std::nullopt});
            break;
        case Action::RunStart:
            rows.push_back({action_name, "capability.execute", true, device::CapabilityLevel::Execute});
            rows.push_back({action_name, "approval.present_unexpired_unburned", false, std::nullopt});
            rows.push_back({action_name, "attendance.attested", true, std::nullopt});
            if (artifact_kind != ArtifactKind::PrintJob && is_router_or_mill(machine_class))
                rows.push_back({action_name, "spindle_authority.satisfied", true, std::nullopt});
            rows.push_back({action_name,
                            artifact_kind == ArtifactKind::PrintJob
                                ? "bed_origin.declared_by_firmware"
                                : "wcs.established_this_setup",
                            true, std::nullopt});
            rows.push_back({action_name, "emulator_stage.at_least", true, std::nullopt});
            rows.push_back({action_name, "lease.commander_held_by_caller", false, std::nullopt});
            break;
        case Action::ApprovalIssue:
            rows.push_back({action_name, "exercisable_by.within_policy_ceiling", false, std::nullopt});
            break;
        case Action::RunResume:
            rows.push_back({action_name, "no_alarm_since_hold", false, std::nullopt});
            rows.push_back({action_name, "lease.commander_held_by_caller", false, std::nullopt});
            break;
        case Action::AnyWrite:
            rows.push_back({action_name, "lease.commander_held_by_caller", false, std::nullopt});
            break;
    }
    return rows;
}

std::vector<Tier0Refusal> evaluate(const Context& context) {
    std::vector<Tier0Refusal> refusals;
    for (const auto& row : classify(context.action, context.artifact_kind, context.machine_class)) {
        if (row.prerequisite == "transport.available" && !context.transport_available) {
            refusals.push_back(refusal(context, row, "transport endpoint is unavailable",
                                       {"connect the configured endpoint", "probe transport again"}));
        } else if (row.prerequisite == "exclusive_port_acknowledged" &&
                   context.serial_transport && !context.exclusive_port_acknowledged) {
            refusals.push_back(refusal(context, row, "serial vendor host has not been confirmed closed",
                                       {"close the vendor controller application", "acknowledge exclusive serial ownership"}));
        } else if (row.prerequisite == "artifact.present_and_hashed" &&
                   (!context.artifact_present || !context.artifact_hashed)) {
            refusals.push_back(refusal(context, row,
                !context.artifact_present ? "artifact is missing" : "artifact has no immutable hash",
                {"produce the artifact", "hash and bind the artifact"}));
        } else if (row.prerequisite == "post_profile.bound" && !context.post_profile_bound) {
            refusals.push_back(refusal(context, row, "NC artifact is missing its bound post profile",
                                       {"select a post profile", "re-post and hash the artifact"}));
        } else if (row.prerequisite == "machine_class.known" && !context.machine_class_known) {
            refusals.push_back(refusal(context, row, "machine class has not been identified",
                                       {"identify the machine", "commission its machine-class policy"}));
        } else if (row.prerequisite == "capability.execute" &&
                   (context.execute_capability.truth != device::CapabilityTruthValue::Verified ||
                    !stage_at_least(context.execute_capability.emulator_stage,
                        context.machine_class == device::MachineClass::Emulated
                            ? device::EmulatorStage::E3 : device::EmulatorStage::E6))) {
            const auto required_stage = context.machine_class == device::MachineClass::Emulated
                ? device::EmulatorStage::E3 : device::EmulatorStage::E6;
            refusals.push_back(refusal(context, row,
                "capability.execute is " + std::string(device::to_string(context.execute_capability.truth)) +
                    " at " + std::string(device::to_string(context.execute_capability.emulator_stage)) +
                    "; required qualification is " + std::string(device::to_string(required_stage)) +
                    (context.execute_capability.why.empty() ? std::string{} : ": " + context.execute_capability.why),
                {"complete the required emulator or pilot qualification", "probe capabilities again"}));
        } else if (row.prerequisite == "approval.present_unexpired_unburned" &&
                   (!context.approval_present || !context.approval_unexpired || !context.approval_unburned)) {
            std::string why = !context.approval_present ? "approval is missing" :
                              !context.approval_unexpired ? "approval has expired" :
                              "approval nonce is already burned";
            refusals.push_back(refusal(context, row, std::move(why),
                                       {"author a new run", "issue a new human approval"}));
        } else if (row.prerequisite == "attendance.attested") {
            const auto decision = check_attendance(context.policy, context.attendance_mode,
                                                   context.attendance_attested);
            if (!decision.allowed)
                refusals.push_back(refusal(context, row, decision.code + ": " + decision.why,
                                           {"select an allowed attendance mode", "attest attendance for this run"}));
        } else if (row.prerequisite == "spindle_authority.satisfied") {
            const auto decision = check_spindle_authority(
                context.policy, context.spindle_request, context.manual_spindle_confirmation,
                context.spindle_commanded_state, context.spindle_observed_state);
            if (!decision.allowed)
                refusals.push_back(refusal(context, row, decision.code + ": " + decision.why,
                                           {"commission feedback control or confirm the spindle manually for this run"}));
        } else if (row.prerequisite == "wcs.established_this_setup" &&
                   !context.wcs_established_this_setup) {
            refusals.push_back(refusal(context, row, "WCS was not established for this setup revision",
                                       {"probe or establish the WCS", "repeat preflight"}));
        } else if (row.prerequisite == "bed_origin.declared_by_firmware" &&
                   !context.bed_origin_declared_by_firmware) {
            refusals.push_back(refusal(context, row, "printer firmware has not declared the bed origin",
                                       {"probe printer configuration", "bind the firmware bed origin"}));
        } else if (row.prerequisite == "emulator_stage.at_least") {
            const auto required = context.machine_class == device::MachineClass::Emulated
                                      ? device::EmulatorStage::E3 : device::EmulatorStage::E4;
            if (!stage_at_least(context.emulator_stage_passed, required)) {
                refusals.push_back(refusal(context, row,
                    "highest passed emulator stage is " +
                        std::string(device::to_string(context.emulator_stage_passed)) +
                        "; required stage is " + std::string(device::to_string(required)),
                    {"complete the required qualification stage"}));
            }
        } else if (row.prerequisite == "exercisable_by.within_policy_ceiling" &&
                   !permits(context.policy, context.requested_exercisable_by)) {
            refusals.push_back(refusal(context, row,
                "requested exercisable_by=" + std::string(mfg::to_string(context.requested_exercisable_by)) +
                    " exceeds policy ceiling=" + std::string(mfg::to_string(context.policy.max_exercisable_by)),
                {"request human_only or recommission the device policy"}));
        } else if (row.prerequisite == "no_alarm_since_hold" &&
                   !context.no_discontinuity_since_hold) {
            refusals.push_back(refusal(context, row,
                "resume forbidden after reset, alarm, hard limit, disconnect, or power loss",
                {"abandon this run", "author and approve a new run"}));
        } else if (row.prerequisite == "lease.commander_held_by_caller" &&
                   !context.commander_lease_held_by_caller) {
            refusals.push_back(refusal(context, row, "caller does not hold the commander lease",
                                       {"claim the device commander lease"}));
        }
    }
    return refusals;
}

}  // namespace chudware::mfg::tier0
