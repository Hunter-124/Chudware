#include "manufacturing/policy.h"

#include <algorithm>
#include <stdexcept>

namespace chudware::mfg {

std::string_view to_string(AttendanceMode value) {
    switch (value) {
        case AttendanceMode::PhysicalAttended: return "physical_attended";
        case AttendanceMode::RemoteAttended: return "remote_attended";
        case AttendanceMode::Unattended: return "unattended";
    }
    throw std::invalid_argument("invalid AttendanceMode");
}

std::string_view to_string(SpindleAuthority value) {
    switch (value) {
        case SpindleAuthority::NotApplicable: return "not_applicable";
        case SpindleAuthority::ManualConfirmed: return "manual_confirmed";
        case SpindleAuthority::CommandedFeedback: return "commanded_feedback";
    }
    throw std::invalid_argument("invalid SpindleAuthority");
}

std::string_view to_string(ExercisableBy value) {
    switch (value) {
        case ExercisableBy::HumanOnly: return "human_only";
        case ExercisableBy::HumanOrAgent: return "human_or_agent";
    }
    throw std::invalid_argument("invalid ExercisableBy");
}

MachinePolicy default_policy(device::MachineClass machine_class) {
    MachinePolicy policy;
    policy.machine_class = machine_class;
    policy.revision = 1;
    switch (machine_class) {
        case device::MachineClass::OpenFrameRouter:
        case device::MachineClass::OpenFrameMill:
            policy.attendance_allowed = {AttendanceMode::PhysicalAttended};
            policy.spindle_guard_required = true;
            break;
        case device::MachineClass::EnclosedInterlockedMill:
            // Remote and unattended operation are added only by commissioning evidence.
            policy.attendance_allowed = {AttendanceMode::PhysicalAttended};
            policy.spindle_guard_required = true;
            break;
        case device::MachineClass::FdmPrinterOpen:
            policy.attendance_allowed = {AttendanceMode::PhysicalAttended,
                                         AttendanceMode::RemoteAttended};
            break;
        case device::MachineClass::FdmPrinterEnclosed:
            // Unattended operation is added only by commissioning evidence.
            policy.attendance_allowed = {AttendanceMode::PhysicalAttended,
                                         AttendanceMode::RemoteAttended};
            break;
        case device::MachineClass::Emulated:
            policy.attendance_allowed = {AttendanceMode::PhysicalAttended};
            break;
    }
    return policy;
}

bool attendance_allowed(const MachinePolicy& policy, AttendanceMode mode) {
    return std::find(policy.attendance_allowed.begin(), policy.attendance_allowed.end(), mode) !=
           policy.attendance_allowed.end();
}

bool permits(const MachinePolicy& policy, ExercisableBy requested) {
    return requested == ExercisableBy::HumanOnly ||
           policy.max_exercisable_by == ExercisableBy::HumanOrAgent;
}

PolicyDecision check_attendance(const MachinePolicy& policy, AttendanceMode mode, bool attested) {
    if (!attendance_allowed(policy, mode)) {
        return {false, "attendance.mode_not_allowed_for_machine_class",
                "attendance mode " + std::string(to_string(mode)) + " is not commissioned for " +
                    std::string(device::to_string(policy.machine_class))};
    }
    if (!attested) {
        return {false, "attendance.attestation_missing",
                "attendance must be attested for this run"};
    }
    return {true, {}, {}};
}

PolicyDecision check_spindle_authority(const MachinePolicy& policy, SpindleRequest request,
                                       bool manual_confirmation_for_run,
                                       bool commanded_state, bool observed_state) {
    if (!policy.spindle_guard_required || request == SpindleRequest::NotApplicable)
        return {true, {}, {}};

    if (request == SpindleRequest::AutomatedControl) {
        if (!policy.feedback_relay_commissioned) {
            return {false, "spindle.feedback_authority_not_commissioned",
                    "automated spindle control requires a commissioned feedback-capable relay or VFD"};
        }
        if (!commanded_state || !observed_state) {
            return {false, "spindle.commanded_and_observed_state_required",
                    "automated spindle authority requires both commanded and independently observed on-state"};
        }
        return {true, {}, {}};
    }

    if (!manual_confirmation_for_run) {
        return {false, "spindle.manual_confirmation_missing",
                "manual spindle authority requires explicit confirmation bound to this run"};
    }
    return {true, {}, {}};
}

}  // namespace chudware::mfg
