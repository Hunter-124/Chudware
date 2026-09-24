#pragma once

#include <array>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>

#include <nlohmann/json.hpp>

namespace chudware::device {

enum class DeviceState {
    Disconnected,
    Connecting,
    Identifying,
    Idle,
    Running,
    Hold,
    Door,
    Probing,
    Homing,
    Alarm,
    Fault,
    Unknown,
};

inline std::string_view to_string(DeviceState value) {
    switch (value) {
        case DeviceState::Disconnected: return "disconnected";
        case DeviceState::Connecting: return "connecting";
        case DeviceState::Identifying: return "identifying";
        case DeviceState::Idle: return "idle";
        case DeviceState::Running: return "running";
        case DeviceState::Hold: return "hold";
        case DeviceState::Door: return "door";
        case DeviceState::Probing: return "probing";
        case DeviceState::Homing: return "homing";
        case DeviceState::Alarm: return "alarm";
        case DeviceState::Fault: return "fault";
        case DeviceState::Unknown: return "unknown";
    }
    throw std::invalid_argument("invalid DeviceState");
}

inline std::optional<DeviceState> parse_device_state(std::string_view text) {
    if (text == "disconnected") return DeviceState::Disconnected;
    if (text == "connecting") return DeviceState::Connecting;
    if (text == "identifying") return DeviceState::Identifying;
    if (text == "idle") return DeviceState::Idle;
    if (text == "running") return DeviceState::Running;
    if (text == "hold") return DeviceState::Hold;
    if (text == "door") return DeviceState::Door;
    if (text == "probing") return DeviceState::Probing;
    if (text == "homing") return DeviceState::Homing;
    if (text == "alarm") return DeviceState::Alarm;
    if (text == "fault") return DeviceState::Fault;
    if (text == "unknown") return DeviceState::Unknown;
    return std::nullopt;
}

enum class RunState {
    Draft,
    AwaitingApproval,
    Approved,
    Arming,
    Queued,
    Running,
    Holding,
    Held,
    Resuming,
    Completing,
    Completed,
    Aborting,
    Aborted,
    Faulted,
    Abandoned,
};

inline std::string_view to_string(RunState value) {
    switch (value) {
        case RunState::Draft: return "draft";
        case RunState::AwaitingApproval: return "awaiting_approval";
        case RunState::Approved: return "approved";
        case RunState::Arming: return "arming";
        case RunState::Queued: return "queued";
        case RunState::Running: return "running";
        case RunState::Holding: return "holding";
        case RunState::Held: return "held";
        case RunState::Resuming: return "resuming";
        case RunState::Completing: return "completing";
        case RunState::Completed: return "completed";
        case RunState::Aborting: return "aborting";
        case RunState::Aborted: return "aborted";
        case RunState::Faulted: return "faulted";
        case RunState::Abandoned: return "abandoned";
    }
    throw std::invalid_argument("invalid RunState");
}

inline std::optional<RunState> parse_run_state(std::string_view text) {
    if (text == "draft") return RunState::Draft;
    if (text == "awaiting_approval") return RunState::AwaitingApproval;
    if (text == "approved") return RunState::Approved;
    if (text == "arming") return RunState::Arming;
    if (text == "queued") return RunState::Queued;
    if (text == "running") return RunState::Running;
    if (text == "holding") return RunState::Holding;
    if (text == "held") return RunState::Held;
    if (text == "resuming") return RunState::Resuming;
    if (text == "completing") return RunState::Completing;
    if (text == "completed") return RunState::Completed;
    if (text == "aborting") return RunState::Aborting;
    if (text == "aborted") return RunState::Aborted;
    if (text == "faulted") return RunState::Faulted;
    if (text == "abandoned") return RunState::Abandoned;
    return std::nullopt;
}

enum class RunDiscontinuity { None, Reset, Alarm, HardLimit, Disconnect, PowerLoss };

struct TransitionValidation {
    bool allowed = false;
    std::string why;
};

inline TransitionValidation validate_run_transition(
        RunState from, RunState to, RunDiscontinuity since_hold = RunDiscontinuity::None) {
    if (from == RunState::Faulted || from == RunState::Aborted ||
        from == RunState::Abandoned || from == RunState::Completed) {
        return {false, "terminal run requires a new RunId"};
    }
    if (to == RunState::Resuming && since_hold != RunDiscontinuity::None)
        return {false, "resume forbidden after reset, alarm, hard limit, disconnect, or power loss"};

    bool allowed = false;
    switch (from) {
        case RunState::Draft: allowed = to == RunState::AwaitingApproval; break;
        case RunState::AwaitingApproval: allowed = to == RunState::Approved; break;
        case RunState::Approved: allowed = to == RunState::Arming; break;
        case RunState::Arming: allowed = to == RunState::Queued || to == RunState::Faulted; break;
        case RunState::Queued: allowed = to == RunState::Running || to == RunState::Faulted; break;
        case RunState::Running:
            allowed = to == RunState::Holding || to == RunState::Completing ||
                      to == RunState::Aborting || to == RunState::Faulted;
            break;
        case RunState::Holding:
            allowed = to == RunState::Held || to == RunState::Aborting ||
                      to == RunState::Faulted;
            break;
        case RunState::Held:
            allowed = to == RunState::Resuming || to == RunState::Abandoned ||
                      to == RunState::Aborting || to == RunState::Faulted;
            break;
        case RunState::Resuming: allowed = to == RunState::Running || to == RunState::Faulted; break;
        case RunState::Completing: allowed = to == RunState::Completed || to == RunState::Faulted; break;
        case RunState::Aborting: allowed = to == RunState::Aborted || to == RunState::Faulted; break;
        case RunState::Completed:
        case RunState::Aborted:
        case RunState::Faulted:
        case RunState::Abandoned: break;
    }
    return {allowed, allowed ? std::string{} : "transition is not in the canonical run state machine"};
}

struct FeedRpm {
    double feed = 0;
    double spindle = 0;
};

struct Overrides {
    int feed = 100;
    int rapid = 100;
    int spindle = 100;
};

struct ControllerInputs {
    bool x = false;
    bool y = false;
    bool z = false;
    bool probe = false;
    bool door = false;
    bool hold = false;
    bool reset = false;
    bool start = false;
};

struct ControllerBuffer {
    std::optional<int> planner;
    std::optional<int> rx;
};

struct ControllerSnapshot {
    std::string device_id;
    DeviceState state = DeviceState::Unknown;
    std::optional<std::string> substate;
    std::array<double, 3> mpos{};
    std::array<double, 3> wpos{};
    std::array<double, 3> wco{};
    FeedRpm feed_rpm;
    Overrides overrides;
    ControllerInputs inputs;
    ControllerBuffer buffer;
    std::optional<std::string> alarm;
    std::optional<uint64_t> line;
    std::string raw;
};

inline void to_json(nlohmann::json& json, const ControllerSnapshot& value) {
    json = {{"device_id", value.device_id}, {"state", to_string(value.state)},
            {"mpos", value.mpos}, {"wpos", value.wpos}, {"wco", value.wco},
            {"feed_rpm", {{"feed", value.feed_rpm.feed}, {"spindle", value.feed_rpm.spindle}}},
            {"overrides", {{"feed", value.overrides.feed}, {"rapid", value.overrides.rapid},
                           {"spindle", value.overrides.spindle}}},
            {"inputs", {{"x", value.inputs.x}, {"y", value.inputs.y}, {"z", value.inputs.z},
                        {"probe", value.inputs.probe}, {"door", value.inputs.door},
                        {"hold", value.inputs.hold}, {"reset", value.inputs.reset},
                        {"start", value.inputs.start}}},
            {"buffer", nlohmann::json::object()}, {"raw", value.raw}};
    json["substate"] = value.substate ? nlohmann::json(*value.substate) : nlohmann::json(nullptr);
    json["buffer"]["planner"] = value.buffer.planner
        ? nlohmann::json(*value.buffer.planner) : nlohmann::json(nullptr);
    json["buffer"]["rx"] = value.buffer.rx
        ? nlohmann::json(*value.buffer.rx) : nlohmann::json(nullptr);
    json["alarm"] = value.alarm ? nlohmann::json(*value.alarm) : nlohmann::json(nullptr);
    json["line"] = value.line ? nlohmann::json(*value.line) : nlohmann::json(nullptr);
}

inline void from_json(const nlohmann::json& json, ControllerSnapshot& value) {
    const auto state = parse_device_state(json.at("state").get<std::string>());
    if (!state) throw std::invalid_argument("invalid DeviceState string");
    value.device_id = json.at("device_id").get<std::string>();
    value.state = *state;
    value.substate.reset();
    if (json.contains("substate") && !json.at("substate").is_null())
        value.substate = json.at("substate").get<std::string>();
    value.mpos = json.at("mpos").get<std::array<double, 3>>();
    value.wpos = json.at("wpos").get<std::array<double, 3>>();
    value.wco = json.at("wco").get<std::array<double, 3>>();
    value.feed_rpm = {json.at("feed_rpm").at("feed").get<double>(),
                      json.at("feed_rpm").at("spindle").get<double>()};
    value.overrides = {json.at("overrides").at("feed").get<int>(),
                       json.at("overrides").at("rapid").get<int>(),
                       json.at("overrides").at("spindle").get<int>()};
    const auto& inputs = json.at("inputs");
    value.inputs = {inputs.at("x").get<bool>(), inputs.at("y").get<bool>(),
                    inputs.at("z").get<bool>(), inputs.at("probe").get<bool>(),
                    inputs.at("door").get<bool>(), inputs.at("hold").get<bool>(),
                    inputs.at("reset").get<bool>(), inputs.at("start").get<bool>()};
    value.buffer = {};
    if (json.contains("buffer")) {
        const auto& buffer = json.at("buffer");
        if (buffer.contains("planner") && !buffer.at("planner").is_null())
            value.buffer.planner = buffer.at("planner").get<int>();
        if (buffer.contains("rx") && !buffer.at("rx").is_null())
            value.buffer.rx = buffer.at("rx").get<int>();
    }
    value.alarm.reset();
    if (json.contains("alarm") && !json.at("alarm").is_null())
        value.alarm = json.at("alarm").get<std::string>();
    value.line.reset();
    if (json.contains("line") && !json.at("line").is_null())
        value.line = json.at("line").get<uint64_t>();
    value.raw = json.value("raw", std::string{});
}

}  // namespace chudware::device
