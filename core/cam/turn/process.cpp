#include "cam/turn/process.h"

#include <algorithm>
#include <cmath>

namespace chudware::cam::turn {
namespace {

ValidationResult refusal(const char* reason) { return {false, reason}; }

bool finite_positive(double value) { return std::isfinite(value) && value > 0; }

void set_level(device::CapabilityReport& report, device::CapabilityLevel level,
               device::CapabilityTruthValue truth, const char* why) {
    device::CapabilityTruth value;
    value.level = level;
    value.truth = truth;
    value.emulator_stage = device::EmulatorStage::E0;
    value.why = why;
    report.levels[level] = std::move(value);
}

}  // namespace

ValidationResult validate(const Machine& machine, const WCS& wcs, const Process& process) {
    if (machine.kind != MachineKind::Lathe)
        return refusal("turn_single_spindle_lathe_required");
    if (std::fabs(machine.travel.y) > 1e-9)
        return refusal("turn_single_spindle_xz_topology_required");
    if (std::fabs(wcs.z_dir.x) > 1e-9 || std::fabs(wcs.z_dir.y) > 1e-9 ||
        std::fabs(std::fabs(wcs.z_dir.z) - 1.0) > 1e-9)
        return refusal("turn_g18_spindle_axis_misaligned");
    if (!process.workholding.declared)
        return refusal("turn_workholding_not_declared");
    if (!finite_positive(process.workholding.grip_radius_mm))
        return refusal("turn_workholding_grip_radius_invalid");
    if (!std::isfinite(process.workholding.jaw_length_mm) ||
        process.workholding.jaw_length_mm < 0)
        return refusal("turn_workholding_jaw_length_invalid");
    if (!finite_positive(process.tool.nose_radius_mm))
        return refusal("turn_tool_nose_radius_required");
    if (!finite_positive(process.tool.insert_width_mm))
        return refusal("turn_tool_insert_width_invalid");
    if (process.tool.nose_radius_mm > process.tool.insert_width_mm * 0.5 + 1e-9)
        return refusal("turn_nose_radius_exceeds_insert");
    const bool internal_orientation =
        process.tool.orientation == ToolOrientation::InternalRight ||
        process.tool.orientation == ToolOrientation::InternalLeft;
    if (internal_orientation != process.internal_cut)
        return refusal("turn_tool_orientation_incompatible");
    if (process.feed_mode == FeedMode::PerRevolution && !finite_positive(process.feed_mm_rev))
        return refusal("turn_feed_per_rev_required");
    if (process.feed_mode == FeedMode::PerMinute && !finite_positive(process.feed_mm_min))
        return refusal("turn_feed_per_min_required");
    if (process.spindle_mode == SpindleMode::ConstantSurfaceSpeed) {
        if (!finite_positive(process.css_m_min)) return refusal("turn_css_surface_speed_required");
        if (!finite_positive(process.max_rpm)) return refusal("turn_css_rpm_clamp_required");
    } else if (!finite_positive(process.rpm)) {
        return refusal("turn_fixed_rpm_required");
    }
    return {};
}

ValidationResult operation_construction_status(OpType type) {
    switch (type) {
        case OpType::TurnFace:
        case OpType::TurnRough:
        case OpType::TurnFinish:
        case OpType::TurnGroove:
        case OpType::TurnThread:
        case OpType::TurnPart:
        case OpType::TurnDrill:
            return {};
        default:
            return refusal("turn_operation_not_advertised");
    }
}

double minimum_safe_z(const Workholding& workholding) {
    if (workholding.kind == WorkholdingKind::ThreeJawChuck ||
        workholding.kind == WorkholdingKind::FourJawChuck)
        return workholding.body_face_z_mm + workholding.jaw_length_mm +
               workholding.safety_margin_mm;
    return workholding.body_face_z_mm + workholding.safety_margin_mm;
}

double program_x(double internal_radius_mm, XMode mode) {
    return mode == XMode::Diameter ? internal_radius_mm * 2.0 : internal_radius_mm;
}

double internal_radius(double program_x_mm, XMode mode) {
    return mode == XMode::Diameter ? program_x_mm * 0.5 : program_x_mm;
}

double rpm_at_radius(const Process& process, double radius_mm) {
    if (process.spindle_mode == SpindleMode::FixedRpm) return process.rpm;
    if (!(radius_mm > 0) || !(process.css_m_min > 0) || !(process.max_rpm > 0)) return 0;
    const double css_rpm = 1000.0 * process.css_m_min / (2.0 * 3.14159265358979323846 * radius_mm);
    return std::min(process.max_rpm, css_rpm);
}

std::vector<ThreadPass> generate_thread_passes(const ThreadSpec& spec, ValidationResult* status) {
    auto finish = [&](ValidationResult result) {
        if (status) *status = std::move(result);
        return std::vector<ThreadPass>{};
    };
    if (!finite_positive(spec.major_radius_mm)) return finish(refusal("turn_thread_major_radius_invalid"));
    if (!finite_positive(spec.pitch_mm)) return finish(refusal("turn_thread_pitch_required"));
    if (!finite_positive(spec.depth_mm)) return finish(refusal("turn_thread_depth_required"));
    if (!finite_positive(spec.first_infeed_mm)) return finish(refusal("turn_thread_infeed_required"));
    if (!std::isfinite(spec.z_start_mm) || !std::isfinite(spec.z_end_mm) ||
        std::fabs(spec.z_start_mm - spec.z_end_mm) < 1e-9)
        return finish(refusal("turn_thread_axial_span_invalid"));
    if (spec.passes < 0 || spec.spring_passes < 0)
        return finish(refusal("turn_thread_pass_count_invalid"));

    int cutting_passes = spec.passes;
    if (cutting_passes == 0) {
        cutting_passes = 1;
        while (spec.first_infeed_mm * std::sqrt(static_cast<double>(cutting_passes)) <
                   spec.depth_mm - 1e-9 &&
               cutting_passes < 1000)
            ++cutting_passes;
    }

    std::vector<ThreadPass> result;
    result.reserve(static_cast<size_t>(cutting_passes + spec.spring_passes));
    for (int i = 1; i <= cutting_passes; ++i) {
        const double depth = i == cutting_passes
                                 ? spec.depth_mm
                                 : std::min(spec.depth_mm, spec.first_infeed_mm *
                                                               std::sqrt(static_cast<double>(i)));
        const double radius = spec.internal ? spec.major_radius_mm + depth
                                            : spec.major_radius_mm - depth;
        if (radius < 0) return finish(refusal("turn_thread_depth_exceeds_radius"));
        result.push_back({i, radius, spec.z_start_mm, spec.z_end_mm,
                          std::fabs(spec.z_end_mm - spec.z_start_mm), false});
    }
    for (int i = 0; i < spec.spring_passes; ++i) {
        const double radius = spec.internal ? spec.major_radius_mm + spec.depth_mm
                                            : spec.major_radius_mm - spec.depth_mm;
        result.push_back({cutting_passes + i + 1, radius, spec.z_start_mm, spec.z_end_mm,
                          std::fabs(spec.z_end_mm - spec.z_start_mm), true});
    }
    if (status) *status = {};
    return result;
}

device::CapabilityReport capability_report(const std::string& subject_id) {
    device::CapabilityReport report;
    report.subject_id = subject_id;
    report.max_advertised = device::CapabilityLevel::Simulate;
    report.emulator_stage_passed = device::EmulatorStage::E0;
    constexpr const char* offline =
        "offline implementation requires a gateway-stamped procedural fact before verification";
    set_level(report, device::CapabilityLevel::Plan, device::CapabilityTruthValue::Declared, offline);
    set_level(report, device::CapabilityLevel::Generate, device::CapabilityTruthValue::Declared, offline);
    set_level(report, device::CapabilityLevel::Post, device::CapabilityTruthValue::Declared, offline);
    set_level(report, device::CapabilityLevel::Parse, device::CapabilityTruthValue::Unsupported,
              "no independent lathe NC parser is implemented");
    set_level(report, device::CapabilityLevel::Simulate, device::CapabilityTruthValue::Declared,
              "axisymmetric stock simulation only; collision physics are not modelled");
    set_level(report, device::CapabilityLevel::Monitor, device::CapabilityTruthValue::Unsupported,
              "turning has no observation transport");
    set_level(report, device::CapabilityLevel::Transport, device::CapabilityTruthValue::Unsupported,
              "turning is offline-only");
    set_level(report, device::CapabilityLevel::Execute, device::CapabilityTruthValue::Unsupported,
              "no physical turning execution path exists");
    set_level(report, device::CapabilityLevel::SafetyIntegration,
              device::CapabilityTruthValue::Unsupported,
              "no safety integration is implemented");
    return report;
}

}  // namespace chudware::cam::turn
