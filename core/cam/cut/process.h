#pragma once

#include <optional>
#include <string>
#include <variant>
#include <vector>

#include "cam/model.h"
#include "device/capability.h"

namespace chudware::cam::cut {

enum class ProcessKind { Laser, Plasma, Waterjet };
enum class CutMotion { Traverse, Pierce, Cut };
enum class PowerChannel { LaserFraction, PlasmaCurrentAmp, WaterjetPressureMpa, AbrasiveFlowGramMin };

struct ProcessSetpoint {
    PowerChannel channel = PowerChannel::LaserFraction;
    double value = 0;
    std::string unit;
};

struct LaserProfile {
    double power_fraction = 0;
    bool dynamic_power = true;
    double kerf_mm = 0;
    double feed_mm_min = 0;
};

struct PlasmaProfile {
    double current_a = 0;
    double pierce_delay_s = 0;
    double pierce_height_mm = 0;
    double cut_height_mm = 0;
    bool thc_requested = false;
    double feed_mm_min = 0;
};

struct WaterjetProfile {
    double pressure_mpa = 0;
    double abrasive_g_min = 0;
    double orifice_mm = 0;
    double mixing_tube_mm = 0;
    double kerf_mm = 0;
    double feed_mm_min = 0;
    bool request_jet_lag_compensation = false;
    bool request_taper_compensation = false;
};

using Profile = std::variant<LaserProfile, PlasmaProfile, WaterjetProfile>;

struct PlanMove {
    CutMotion motion = CutMotion::Traverse;
    Vec2 to;
    double feed_mm_min = 0;
    std::optional<ProcessSetpoint> setpoint;
};

struct VectorPlan {
    ProcessKind process = ProcessKind::Laser;
    std::vector<PlanMove> moves;
    std::string fidelity = "vector_geometry_only";
};

struct Result {
    bool ok = true;
    std::string reason;
};

Result validate_profile(const Profile&);
Result operation_construction_status(OpType);
VectorPlan plan_vector(const std::vector<Path2>&, const Profile&, Result* = nullptr);

struct PostResult {
    bool ok = true;
    std::string reason;
    std::string nc;
};

PostResult post_laser_grbl(const VectorPlan&, const LaserProfile&);
PostResult post_waterjet_linuxcnc(const VectorPlan&, const WaterjetProfile&);

struct ParseReport {
    bool ok = true;
    std::string reason;
    std::vector<Vec2> cut_path;
    bool power_on_during_traverse = false;
    bool laser_mode_prerequisite = false;
};

ParseReport parse_laser_grbl(const std::string&);

enum class PlasmaState { Idle, Probing, Igniting, Piercing, Cutting, Fault };
enum class PlasmaFault { None, ProbeNoContact, ArcLost, Breakaway, SignalUnknownOrStale };
enum class PlasmaEventKind {
    Arm, ProbeContact, ProbeNoContact, ArcOk, PierceElapsed, MotionStart,
    ThcEnable, CornerEnter, CornerExit, VoidEnter, VoidExit, Breakaway, ArcLost, Stop
};

struct PlasmaEvent {
    PlasmaEventKind kind = PlasmaEventKind::Arm;
    bool fresh = true;
};

struct PlasmaReplayFrame {
    PlasmaState state = PlasmaState::Idle;
    PlasmaFault fault = PlasmaFault::None;
    bool thc_enabled = false;
    bool thc_locked = false;
    bool breakaway_protective_function = false;
};

std::vector<PlasmaReplayFrame> replay_plasma(const std::vector<PlasmaEvent>&);
std::optional<PlasmaEventKind> plasma_event_from_string(const std::string&);

struct WaterjetRecipeKey {
    std::string material;
    double thickness_mm = 0;
    double orifice_mm = 0;
    double mixing_tube_mm = 0;
};

struct WaterjetRecipe {
    WaterjetRecipeKey key;
    WaterjetProfile profile;
    std::string provenance;
};

Result resolve_waterjet_recipe(const WaterjetRecipeKey&, const std::vector<WaterjetRecipe>&,
                               WaterjetRecipe* out);
device::CapabilityReport capability_report(ProcessKind, const std::string& subject_id);

}  // namespace chudware::cam::cut
