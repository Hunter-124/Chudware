#pragma once

#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "device/capability.h"
#include "device/device_id.h"
#include "manufacturing/policy.h"

namespace chudware::mfg::tier0 {

enum class Action { DeviceClaim, RunSubmit, RunStart, ApprovalIssue, RunResume, AnyWrite };
enum class ArtifactKind { Nc, Gcode, PrintJob };

std::string_view to_string(Action value);
std::string_view to_string(ArtifactKind value);

struct Requirement {
    std::string action;
    std::string prerequisite;
    bool artifact_dependent = false;
    std::optional<device::CapabilityLevel> capability_level;
};

struct Tier0Refusal {
    std::string action;
    std::string prerequisite;
    std::string why;
    std::string missing_level;
    std::vector<std::string> remedy;
    bool artifact_dependent = false;
};

struct Context {
    Action action = Action::RunStart;
    ArtifactKind artifact_kind = ArtifactKind::Nc;
    device::MachineClass machine_class = device::MachineClass::Emulated;
    bool machine_class_known = true;

    bool transport_available = true;
    bool serial_transport = false;
    bool exclusive_port_acknowledged = true;
    bool artifact_present = true;
    bool artifact_hashed = true;
    bool post_profile_bound = true;
    device::CapabilityTruth execute_capability{
        device::CapabilityLevel::Execute, device::CapabilityTruthValue::Assumed, {},
        device::EmulatorStage::None, device::CapabilityLevel::Execute,
        "execute capability has not been verified"};
    bool approval_present = true;
    bool approval_unexpired = true;
    bool approval_unburned = true;
    MachinePolicy policy = default_policy(device::MachineClass::Emulated);
    AttendanceMode attendance_mode = AttendanceMode::PhysicalAttended;
    bool attendance_attested = true;
    SpindleRequest spindle_request = SpindleRequest::NotApplicable;
    bool manual_spindle_confirmation = false;
    bool spindle_commanded_state = false;
    bool spindle_observed_state = false;
    bool wcs_established_this_setup = true;
    bool bed_origin_declared_by_firmware = true;
    device::EmulatorStage emulator_stage_passed = device::EmulatorStage::None;
    ExercisableBy requested_exercisable_by = ExercisableBy::HumanOnly;
    bool no_discontinuity_since_hold = true;
    bool commander_lease_held_by_caller = true;
};

std::vector<Requirement> classify(Action action, ArtifactKind artifact_kind,
                                  device::MachineClass machine_class);
std::vector<Tier0Refusal> evaluate(const Context& context);

}  // namespace chudware::mfg::tier0
