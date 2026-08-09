#include "cam/cut/process.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <iomanip>
#include <sstream>

namespace chudware::cam::cut {
namespace {

Result refusal(const char* reason) { return {false, reason}; }
bool positive(double value) { return std::isfinite(value) && value > 0; }
bool same(double a, double b) { return std::fabs(a - b) <= 1e-9; }

ProcessKind kind_of(const Profile& profile) {
    if (std::holds_alternative<LaserProfile>(profile)) return ProcessKind::Laser;
    if (std::holds_alternative<PlasmaProfile>(profile)) return ProcessKind::Plasma;
    return ProcessKind::Waterjet;
}

double feed_of(const Profile& profile) {
    return std::visit([](const auto& p) { return p.feed_mm_min; }, profile);
}

double kerf_of(const Profile& profile) {
    if (const auto* laser = std::get_if<LaserProfile>(&profile)) return laser->kerf_mm;
    if (const auto* waterjet = std::get_if<WaterjetProfile>(&profile)) return waterjet->kerf_mm;
    return 0;
}

ProcessSetpoint setpoint_of(const Profile& profile) {
    if (const auto* laser = std::get_if<LaserProfile>(&profile))
        return {PowerChannel::LaserFraction, laser->power_fraction, "fraction"};
    if (const auto* plasma = std::get_if<PlasmaProfile>(&profile))
        return {PowerChannel::PlasmaCurrentAmp, plasma->current_a, "A"};
    const auto& waterjet = std::get<WaterjetProfile>(profile);
    return {PowerChannel::WaterjetPressureMpa, waterjet.pressure_mpa, "MPa"};
}

double signed_area(const Path2& path) {
    double area = 0;
    for (size_t i = 0; i < path.size(); ++i) {
        const Vec2& a = path[i];
        const Vec2& b = path[(i + 1) % path.size()];
        area += a.x * b.y - b.x * a.y;
    }
    return area * 0.5;
}

Path2 offset_closed(const Path2& path, double distance) {
    if (path.size() < 3 || distance == 0) return path;
    const double orientation = signed_area(path) >= 0 ? 1.0 : -1.0;
    Path2 out;
    out.reserve(path.size());
    for (size_t i = 0; i < path.size(); ++i) {
        const Vec2& prev = path[(i + path.size() - 1) % path.size()];
        const Vec2& cur = path[i];
        const Vec2& next = path[(i + 1) % path.size()];
        const double l1 = std::hypot(cur.x - prev.x, cur.y - prev.y);
        const double l2 = std::hypot(next.x - cur.x, next.y - cur.y);
        if (l1 <= 1e-12 || l2 <= 1e-12) return {};
        const Vec2 n1{orientation * (cur.y - prev.y) / l1,
                      orientation * (prev.x - cur.x) / l1};
        const Vec2 n2{orientation * (next.y - cur.y) / l2,
                      orientation * (cur.x - next.x) / l2};
        const double dot = n1.x * n2.x + n1.y * n2.y;
        const double denom = 1.0 + dot;
        if (denom <= 1e-6) return {};
        out.push_back({cur.x + distance * (n1.x + n2.x) / denom,
                       cur.y + distance * (n1.y + n2.y) / denom});
    }
    return out;
}

std::string number(double value) {
    std::ostringstream stream;
    stream << std::fixed << std::setprecision(3) << value;
    std::string out = stream.str();
    while (out.size() > 1 && out.back() == '0') out.pop_back();
    if (!out.empty() && out.back() == '.') out.pop_back();
    return out;
}

std::optional<double> word_value(const std::string& line, char word) {
    for (size_t i = 0; i < line.size(); ++i) {
        if (std::toupper(static_cast<unsigned char>(line[i])) != word) continue;
        size_t end = i + 1;
        while (end < line.size() && (std::isdigit(static_cast<unsigned char>(line[end])) ||
               line[end] == '-' || line[end] == '+' || line[end] == '.')) ++end;
        if (end == i + 1) continue;
        try { return std::stod(line.substr(i + 1, end - i - 1)); }
        catch (...) { return std::nullopt; }
    }
    return std::nullopt;
}

void set_level(device::CapabilityReport& report, device::CapabilityLevel level,
               device::CapabilityTruthValue truth, const char* why) {
    device::CapabilityTruth row;
    row.level = level;
    row.truth = truth;
    row.emulator_stage = device::EmulatorStage::E0;
    row.why = why;
    report.levels[level] = std::move(row);
}

}  // namespace

Result validate_profile(const Profile& profile) {
    if (const auto* laser = std::get_if<LaserProfile>(&profile)) {
        if (!(laser->power_fraction > 0 && laser->power_fraction <= 1))
            return refusal("laser_power_fraction_out_of_range");
        if (!positive(laser->feed_mm_min)) return refusal("laser_feed_required");
        if (!std::isfinite(laser->kerf_mm) || laser->kerf_mm < 0)
            return refusal("laser_kerf_invalid");
        return {};
    }
    if (const auto* plasma = std::get_if<PlasmaProfile>(&profile)) {
        if (!positive(plasma->current_a)) return refusal("plasma_current_required");
        if (!positive(plasma->feed_mm_min)) return refusal("plasma_feed_required");
        if (!std::isfinite(plasma->pierce_delay_s) || plasma->pierce_delay_s < 0)
            return refusal("plasma_pierce_delay_invalid");
        if (plasma->pierce_height_mm < plasma->cut_height_mm)
            return refusal("plasma_pierce_height_below_cut_height");
        return {};
    }
    const auto& waterjet = std::get<WaterjetProfile>(profile);
    if (waterjet.request_jet_lag_compensation)
        return refusal("waterjet_jet_lag_unmodelled");
    if (waterjet.request_taper_compensation)
        return refusal("waterjet_taper_compensation_unmodelled");
    if (!positive(waterjet.pressure_mpa)) return refusal("waterjet_pressure_required");
    if (!positive(waterjet.orifice_mm) || !positive(waterjet.mixing_tube_mm))
        return refusal("waterjet_nozzle_geometry_required");
    if (!positive(waterjet.feed_mm_min)) return refusal("waterjet_feed_required");
    if (!std::isfinite(waterjet.abrasive_g_min) || waterjet.abrasive_g_min < 0)
        return refusal("waterjet_abrasive_flow_invalid");
    if (!std::isfinite(waterjet.kerf_mm) || waterjet.kerf_mm < 0)
        return refusal("waterjet_kerf_invalid");
    return {};
}

Result operation_construction_status(OpType type) {
    if (type == OpType::Cut2D) return {};
    return refusal("cut_operation_not_advertised");
}

VectorPlan plan_vector(const std::vector<Path2>& paths, const Profile& profile, Result* status) {
    const Result valid = validate_profile(profile);
    if (!valid.ok) {
        if (status) *status = valid;
        return {};
    }
    if (paths.empty()) {
        if (status) *status = refusal("cut_vector_path_required");
        return {};
    }
    VectorPlan plan;
    plan.process = kind_of(profile);
    const double feed = feed_of(profile);
    const double offset = kerf_of(profile) * 0.5;
    for (const Path2& input : paths) {
        if (input.size() < 2) {
            if (status) *status = refusal("cut_vector_path_too_short");
            return {};
        }
        const bool closed = input.size() >= 3 && std::fabs(signed_area(input)) > 1e-9;
        Path2 path = closed && offset > 0 ? offset_closed(input, offset) : input;
        if (path.size() != input.size()) {
            if (status) *status = refusal("cut_kerf_offset_degenerate");
            return {};
        }
        plan.moves.push_back({CutMotion::Traverse, path.front(), 0, std::nullopt});
        plan.moves.push_back({CutMotion::Pierce, path.front(), 0, setpoint_of(profile)});
        plan.moves.push_back({CutMotion::Cut, path.front(), feed, std::nullopt});
        for (size_t i = 1; i < path.size(); ++i)
            plan.moves.push_back({CutMotion::Cut, path[i], feed, std::nullopt});
        if (closed) plan.moves.push_back({CutMotion::Cut, path.front(), feed, std::nullopt});
    }
    if (status) *status = {};
    return plan;
}

PostResult post_laser_grbl(const VectorPlan& plan, const LaserProfile& profile) {
    const Result valid = validate_profile(Profile{profile});
    if (!valid.ok) return {false, valid.reason, {}};
    if (plan.process != ProcessKind::Laser) return {false, "laser_post_process_mismatch", {}};
    std::ostringstream nc;
    nc << "; requires GRBL $32=1 laser mode\nG21\nG90\nM5\n";
    bool powered = false;
    for (const auto& move : plan.moves) {
        if (move.motion == CutMotion::Traverse) {
            if (powered) { nc << "M5\n"; powered = false; }
            nc << "G0 X" << number(move.to.x) << " Y" << number(move.to.y) << "\n";
        } else if (move.motion == CutMotion::Pierce) {
            nc << (profile.dynamic_power ? "M4" : "M3") << " S"
               << number(profile.power_fraction * 1000.0) << "\n";
            powered = true;
        } else {
            if (!powered) return {false, "laser_post_cut_without_power_state", {}};
            nc << "G1 X" << number(move.to.x) << " Y" << number(move.to.y)
               << " F" << number(move.feed_mm_min) << "\n";
        }
    }
    nc << "M5\nM2\n";
    return {true, {}, nc.str()};
}

PostResult post_waterjet_linuxcnc(const VectorPlan& plan, const WaterjetProfile& profile) {
    const Result valid = validate_profile(Profile{profile});
    if (!valid.ok) return {false, valid.reason, {}};
    if (plan.process != ProcessKind::Waterjet)
        return {false, "waterjet_post_process_mismatch", {}};
    std::ostringstream nc;
    nc << "G21\nG90\nM67 E0 Q" << number(profile.pressure_mpa) << "\n";
    bool process_on = false;
    for (const auto& move : plan.moves) {
        if (move.motion == CutMotion::Traverse) {
            if (process_on) { nc << "M63 P0\nM65 P1\n"; process_on = false; }
            nc << "G0 X" << number(move.to.x) << " Y" << number(move.to.y) << "\n";
        } else if (move.motion == CutMotion::Pierce) {
            nc << "M62 P0\n";
            if (profile.abrasive_g_min > 0) nc << "M64 P1\n";
            process_on = true;
        } else {
            if (!process_on) return {false, "waterjet_post_cut_without_process_state", {}};
            nc << "G1 X" << number(move.to.x) << " Y" << number(move.to.y)
               << " F" << number(move.feed_mm_min) << "\n";
        }
    }
    if (process_on) nc << "M63 P0\nM65 P1\n";
    nc << "M2\n";
    return {true, {}, nc.str()};
}

ParseReport parse_laser_grbl(const std::string& nc) {
    ParseReport report;
    std::istringstream input(nc);
    std::string line;
    bool powered = false;
    Vec2 pos{};
    while (std::getline(input, line)) {
        if (line.find("$32=1") != std::string::npos) report.laser_mode_prerequisite = true;
        std::string code = line.substr(0, line.find(';'));
        if (code.find("M5") != std::string::npos) powered = false;
        if (code.find("M3") != std::string::npos || code.find("M4") != std::string::npos)
            powered = true;
        const bool rapid = code.find("G0") != std::string::npos;
        const bool cut = code.find("G1") != std::string::npos;
        if (!rapid && !cut) continue;
        if (const auto x = word_value(code, 'X')) pos.x = *x;
        if (const auto y = word_value(code, 'Y')) pos.y = *y;
        if (rapid && powered) report.power_on_during_traverse = true;
        if (cut) {
            if (!powered) {
                report.ok = false;
                report.reason = "laser_parse_cut_with_power_off";
                return report;
            }
            report.cut_path.push_back(pos);
        }
    }
    if (!report.laser_mode_prerequisite) {
        report.ok = false;
        report.reason = "laser_grbl_32_prerequisite_missing";
    } else if (report.power_on_during_traverse) {
        report.ok = false;
        report.reason = "laser_power_on_during_traverse";
    }
    return report;
}

std::vector<PlasmaReplayFrame> replay_plasma(const std::vector<PlasmaEvent>& events) {
    PlasmaReplayFrame frame;
    bool thc_requested = false;
    std::vector<PlasmaReplayFrame> frames;
    frames.reserve(events.size());
    for (const auto& event : events) {
        if (!event.fresh) {
            frame.state = PlasmaState::Fault;
            frame.fault = PlasmaFault::SignalUnknownOrStale;
            frame.thc_enabled = false;
            frame.thc_locked = true;
            frames.push_back(frame);
            continue;
        }
        switch (event.kind) {
            case PlasmaEventKind::Arm:
                frame = {};
                frame.state = PlasmaState::Probing;
                break;
            case PlasmaEventKind::ProbeContact:
                if (frame.state == PlasmaState::Probing) frame.state = PlasmaState::Igniting;
                break;
            case PlasmaEventKind::ProbeNoContact:
                frame.state = PlasmaState::Fault; frame.fault = PlasmaFault::ProbeNoContact; break;
            case PlasmaEventKind::ArcOk:
                if (frame.state == PlasmaState::Igniting) frame.state = PlasmaState::Piercing;
                break;
            case PlasmaEventKind::PierceElapsed:
            case PlasmaEventKind::MotionStart:
                if (frame.state == PlasmaState::Piercing) frame.state = PlasmaState::Cutting;
                break;
            case PlasmaEventKind::ThcEnable:
                thc_requested = true;
                frame.thc_enabled = frame.state == PlasmaState::Cutting && !frame.thc_locked;
                break;
            case PlasmaEventKind::CornerEnter:
            case PlasmaEventKind::VoidEnter:
                frame.thc_locked = true; frame.thc_enabled = false; break;
            case PlasmaEventKind::CornerExit:
            case PlasmaEventKind::VoidExit:
                frame.thc_locked = false;
                frame.thc_enabled = thc_requested && frame.state == PlasmaState::Cutting;
                break;
            case PlasmaEventKind::Breakaway:
                frame.state = PlasmaState::Fault; frame.fault = PlasmaFault::Breakaway;
                frame.thc_enabled = false; frame.thc_locked = true;
                frame.breakaway_protective_function = false;
                break;
            case PlasmaEventKind::ArcLost:
                frame.state = PlasmaState::Fault; frame.fault = PlasmaFault::ArcLost;
                frame.thc_enabled = false; frame.thc_locked = true; break;
            case PlasmaEventKind::Stop:
                frame = {}; thc_requested = false; break;
        }
        frames.push_back(frame);
    }
    return frames;
}

std::optional<PlasmaEventKind> plasma_event_from_string(const std::string& value) {
    if (value == "arm") return PlasmaEventKind::Arm;
    if (value == "probe_contact") return PlasmaEventKind::ProbeContact;
    if (value == "probe_no_contact") return PlasmaEventKind::ProbeNoContact;
    if (value == "arc_ok") return PlasmaEventKind::ArcOk;
    if (value == "pierce_elapsed") return PlasmaEventKind::PierceElapsed;
    if (value == "motion_start") return PlasmaEventKind::MotionStart;
    if (value == "thc_enable") return PlasmaEventKind::ThcEnable;
    if (value == "corner_enter") return PlasmaEventKind::CornerEnter;
    if (value == "corner_exit") return PlasmaEventKind::CornerExit;
    if (value == "void_enter") return PlasmaEventKind::VoidEnter;
    if (value == "void_exit") return PlasmaEventKind::VoidExit;
    if (value == "breakaway") return PlasmaEventKind::Breakaway;
    if (value == "arc_lost") return PlasmaEventKind::ArcLost;
    if (value == "stop") return PlasmaEventKind::Stop;
    return std::nullopt;
}

Result resolve_waterjet_recipe(const WaterjetRecipeKey& key,
                               const std::vector<WaterjetRecipe>& recipes,
                               WaterjetRecipe* out) {
    for (const auto& recipe : recipes) {
        if (recipe.key.material == key.material &&
            same(recipe.key.thickness_mm, key.thickness_mm) &&
            same(recipe.key.orifice_mm, key.orifice_mm) &&
            same(recipe.key.mixing_tube_mm, key.mixing_tube_mm)) {
            const Result valid = validate_profile(Profile{recipe.profile});
            if (!valid.ok) return valid;
            if (recipe.provenance.empty()) return refusal("waterjet_recipe_provenance_required");
            if (out) *out = recipe;
            return {};
        }
    }
    return refusal("waterjet_recipe_exact_row_missing");
}

device::CapabilityReport capability_report(ProcessKind kind, const std::string& subject_id) {
    device::CapabilityReport report;
    report.subject_id = subject_id;
    report.emulator_stage_passed = device::EmulatorStage::E0;
    const char* pending =
        "offline implementation requires a gateway-stamped procedural fact before verification";
    set_level(report, device::CapabilityLevel::Plan, device::CapabilityTruthValue::Declared, pending);
    set_level(report, device::CapabilityLevel::Generate, device::CapabilityTruthValue::Declared, pending);
    if (kind == ProcessKind::Plasma) {
        report.max_advertised = device::CapabilityLevel::Generate;
        set_level(report, device::CapabilityLevel::Post, device::CapabilityTruthValue::Unsupported,
                  "no qualified plasma controller dialect is implemented");
        set_level(report, device::CapabilityLevel::Parse, device::CapabilityTruthValue::Unsupported,
                  "plasma replay parses signal ledgers, not controller NC");
    } else {
        report.max_advertised = kind == ProcessKind::Laser ? device::CapabilityLevel::Parse
                                                           : device::CapabilityLevel::Post;
        set_level(report, device::CapabilityLevel::Post, device::CapabilityTruthValue::Declared, pending);
        set_level(report, device::CapabilityLevel::Parse,
                  kind == ProcessKind::Laser ? device::CapabilityTruthValue::Declared
                                             : device::CapabilityTruthValue::Unsupported,
                  kind == ProcessKind::Laser ? pending
                                             : "waterjet NC parser is not implemented");
    }
    set_level(report, device::CapabilityLevel::Simulate, device::CapabilityTruthValue::Unsupported,
              "the manufacturing twin does not model cut-process physics");
    set_level(report, device::CapabilityLevel::Monitor, device::CapabilityTruthValue::Unsupported,
              "cut processes have no observation transport");
    set_level(report, device::CapabilityLevel::Transport, device::CapabilityTruthValue::Unsupported,
              "cut processes are offline-only");
    set_level(report, device::CapabilityLevel::Execute, device::CapabilityTruthValue::Unsupported,
              "no physical cut execution path exists");
    set_level(report, device::CapabilityLevel::SafetyIntegration,
              device::CapabilityTruthValue::Unsupported,
              "no safety integration is implemented");
    return report;
}

}  // namespace chudware::cam::cut
