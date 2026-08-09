#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

#include "device/device_id.h"

namespace chudware::mfg {

enum class AttendanceMode { PhysicalAttended, RemoteAttended, Unattended };
enum class SpindleAuthority { NotApplicable, ManualConfirmed, CommandedFeedback };
enum class ExercisableBy { HumanOnly, HumanOrAgent };

enum class SpindleRequest { NotApplicable, ManualMotion, AutomatedControl };

std::string_view to_string(AttendanceMode value);
std::string_view to_string(SpindleAuthority value);
std::string_view to_string(ExercisableBy value);

struct MachinePolicy {
    device::MachineClass machine_class = device::MachineClass::Emulated;
    std::vector<AttendanceMode> attendance_allowed;
    ExercisableBy max_exercisable_by = ExercisableBy::HumanOnly;
    bool spindle_guard_required = false;
    bool feedback_relay_commissioned = false;
    uint64_t revision = 0;
};

struct PolicyDecision {
    bool allowed = false;
    std::string code;
    std::string why;
};

MachinePolicy default_policy(device::MachineClass machine_class);
bool attendance_allowed(const MachinePolicy& policy, AttendanceMode mode);
bool permits(const MachinePolicy& policy, ExercisableBy requested);

PolicyDecision check_attendance(const MachinePolicy& policy, AttendanceMode mode,
                                bool attested);
PolicyDecision check_spindle_authority(const MachinePolicy& policy, SpindleRequest request,
                                       bool manual_confirmation_for_run,
                                       bool commanded_state, bool observed_state);

}  // namespace chudware::mfg
