#pragma once

#include <string>
#include <vector>

#include "cam/model.h"
#include "device/capability.h"

namespace chudware::cam::turn {

enum class XMode { Diameter, Radius };
enum class WorkholdingKind { ThreeJawChuck, FourJawChuck, Collet, BetweenCenters };
enum class ToolOrientation { ExternalRight, ExternalLeft, InternalRight, InternalLeft };
enum class FeedMode { PerMinute, PerRevolution };
enum class SpindleMode { FixedRpm, ConstantSurfaceSpeed };

struct Workholding {
    bool declared = false;
    WorkholdingKind kind = WorkholdingKind::ThreeJawChuck;
    double grip_radius_mm = 0;
    double body_face_z_mm = 0;
    double jaw_length_mm = 0;
    double safety_margin_mm = 1;
};

struct ToolGeometry {
    ToolOrientation orientation = ToolOrientation::ExternalRight;
    double nose_radius_mm = 0;
    double insert_width_mm = 0;
};

struct Process {
    XMode x_mode = XMode::Diameter;
    FeedMode feed_mode = FeedMode::PerMinute;
    SpindleMode spindle_mode = SpindleMode::FixedRpm;
    double feed_mm_min = 0;
    double feed_mm_rev = 0;
    double rpm = 0;
    double css_m_min = 0;
    double max_rpm = 0;
    bool internal_cut = false;
    Workholding workholding;
    ToolGeometry tool;
};

struct ValidationResult {
    bool ok = true;
    std::string reason;
};

struct ThreadSpec {
    double major_radius_mm = 0;
    double z_start_mm = 0;
    double z_end_mm = 0;
    double pitch_mm = 0;
    double depth_mm = 0;
    double first_infeed_mm = 0;
    int passes = 0;
    int spring_passes = 1;
    bool internal = false;
};

struct ThreadPass {
    int ordinal = 0;
    double cut_radius_mm = 0;
    double z_start_mm = 0;
    double z_end_mm = 0;
    double lead_mm = 0;
    bool spring = false;
};

ValidationResult validate(const Machine&, const WCS&, const Process&);
ValidationResult operation_construction_status(OpType);
double minimum_safe_z(const Workholding&);
double program_x(double internal_radius_mm, XMode);
double internal_radius(double program_x_mm, XMode);
double rpm_at_radius(const Process&, double radius_mm);
std::vector<ThreadPass> generate_thread_passes(const ThreadSpec&, ValidationResult* = nullptr);
device::CapabilityReport capability_report(const std::string& subject_id);

}  // namespace chudware::cam::turn
