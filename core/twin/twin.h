#pragma once

#include "device/controller_snapshot.h"
#include "device/grbl_protocol.h"

#include <array>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <vector>

namespace chudware::twin {

using Tick = std::int64_t;
using Nm = std::int64_t;
using NmPerS = std::int64_t;
using NmPerS2 = std::int64_t;

inline constexpr Nm kNmPerMm = 1'000'000;

struct Vec3Nm {
    Nm x = 0;
    Nm y = 0;
    Nm z = 0;
    constexpr bool operator==(const Vec3Nm&) const = default;
};

struct TwinClock {
    std::int64_t dt_ns = 1'000'000;
    Tick tick = 0;
    [[nodiscard]] constexpr std::int64_t sim_ns() const noexcept { return tick * dt_ns; }
};

struct AxisSpec {
    std::string name;
    Nm start_nm = 0;
    Nm travel_min_nm = 0;
    Nm travel_max_nm = 0;
    NmPerS max_feed_nm_s = 1;
    NmPerS2 accel_nm_s2 = 1;
};

struct MachineSpec {
    std::string id;
    std::array<AxisSpec, 3> axes;
};

struct FixtureBox {
    std::uint32_t id = 0;
    std::string name;
    Vec3Nm min_nm;
    Vec3Nm max_nm;
};

struct ToolGeometry {
    std::string id;
    Nm cutter_radius_nm = 0;
    Nm flute_length_nm = 0;
    Nm holder_radius_nm = 0;
    Nm holder_bottom_from_tip_nm = 0;
    Nm holder_height_nm = 0;
};

struct HeightfieldSpec {
    std::uint32_t cells_x = 0;
    std::uint32_t cells_y = 0;
    Nm resolution_nm = 0;
    Vec3Nm origin_nm;
    Nm bottom_nm = 0;
    Nm top_nm = 0;
};

struct SensorSpec {
    std::uint32_t id = 0;
    std::string name;
    std::int64_t period_ticks = 1;
};

struct CameraSpec {
    std::uint32_t id = 0;
    std::string name;
    std::int64_t period_ticks = 1;
    std::uint32_t width = 1;
    std::uint32_t height = 1;
};

enum class CollisionClass : std::uint8_t {
    RapidIntoStock,
    ToolHolderIntoFixture,
    TravelLimitViolation,
};

struct CollisionEvent {
    Tick tick = 0;
    CollisionClass type = CollisionClass::RapidIntoStock;
    Vec3Nm position_nm;
    std::uint32_t fixture_id = 0;
    Nm penetration_nm = 0;
};

struct FaultSpec {
    Tick at_tick = 0;
    device::RunDiscontinuity type = device::RunDiscontinuity::None;
};

enum class HaltCause : std::uint8_t {
    None,
    Collision,
    ControllerReset,
    ControllerAlarm,
    HardLimit,
    TransportDisconnect,
    PowerLoss,
    ProgramEnd,
};

struct FaultEvent {
    Tick tick = 0;
    device::RunDiscontinuity type = device::RunDiscontinuity::None;
    HaltCause halt = HaltCause::None;
};

enum class ObservationKind : std::uint8_t { Controller, Sensor, Camera };

struct VirtualObservation {
    Tick tick = 0;
    ObservationKind kind = ObservationKind::Sensor;
    std::uint32_t entity_id = 0;
    std::string src;
    std::vector<std::int64_t> values_q;
    std::vector<std::uint8_t> image;
    std::uint64_t content_hash = 0;
};

struct FidelityDeclaration {
    enum class Level : std::uint8_t { Modeled, Approximated };
    struct Aspect {
        std::string name;
        Level level = Level::Modeled;
        std::string implementation;
        std::string known_error_bound;
    };
    std::string id;
    std::vector<Aspect> aspects;
};

struct TwinConfig {
    std::uint32_t schema = 1;
    std::int64_t dt_ns = 1'000'000;
    std::uint64_t seed = 0;
    std::string artifact_hash;
    MachineSpec machine;
    ToolGeometry tool;
    HeightfieldSpec stock;
    std::vector<FixtureBox> fixtures;
    std::vector<SensorSpec> sensors;
    std::vector<CameraSpec> cameras;
    std::vector<FaultSpec> faults;
    std::vector<CollisionClass> halt_on;
};

struct ResetReport {
    bool ok = false;
    std::string refusal;
    std::uint64_t config_hash = 0;
    std::uint64_t state_hash = 0;
    FidelityDeclaration fidelity;
};

struct TwinAction {
    enum class Kind : std::uint8_t { GcodeLine, Fault };
    Tick at_tick = 0;
    Kind kind = Kind::GcodeLine;
    std::string line;
    FaultSpec fault;
    std::uint64_t submission_order = 0;
};

struct StepResult {
    Tick tick = 0;
    std::int64_t sim_ns = 0;
    std::uint64_t state_hash = 0;
    bool halted = false;
    HaltCause halt = HaltCause::None;
    bool program_complete = false;
    std::vector<CollisionEvent> collisions;
    std::vector<FaultEvent> faults;
    std::vector<VirtualObservation> observations;
    std::vector<std::string> grbl_wire;
};

struct TwinSnapshot {
    std::uint32_t schema = 1;
    std::uint64_t config_hash = 0;
    std::vector<std::uint8_t> bytes;
    [[nodiscard]] bool operator==(const TwinSnapshot&) const = default;
};

struct ProceduralFact {
    std::string check_name;
    std::string artifact_hash;
    std::uint64_t config_hash = 0;
    std::uint64_t snapshot_hash = 0;
    std::string statement;
};

class DeterministicTwin {
public:
    DeterministicTwin();
    ~DeterministicTwin();
    DeterministicTwin(DeterministicTwin&&) noexcept;
    DeterministicTwin& operator=(DeterministicTwin&&) noexcept;
    DeterministicTwin(const DeterministicTwin&) = delete;
    DeterministicTwin& operator=(const DeterministicTwin&) = delete;

    ResetReport reset(const TwinConfig& config);
    bool submit(TwinAction action);
    bool submitGcode(std::string_view line, Tick at_tick = -1);
    bool injectFault(device::RunDiscontinuity type, Tick at_tick);
    StepResult step(std::int64_t ticks = 1);
    [[nodiscard]] TwinSnapshot snapshot() const;
    bool restore(const TwinSnapshot& snapshot);
    [[nodiscard]] std::uint64_t stateHash() const;
    [[nodiscard]] Tick tick() const;
    [[nodiscard]] Vec3Nm toolTip() const;
    [[nodiscard]] std::int64_t stockHeightSumNm() const;
    [[nodiscard]] const FidelityDeclaration& fidelity() const;
    [[nodiscard]] ProceduralFact proceduralFact(std::string check_name) const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};

struct TwinTrace {
    std::uint32_t schema = 1;
    TwinSnapshot initial;
    std::vector<TwinAction> actions;
    std::vector<std::uint64_t> tick_hashes;
    std::uint64_t final_hash = 0;

    [[nodiscard]] std::uint64_t determinismHash() const;
    [[nodiscard]] std::string serialize() const;
    static TwinTrace deserialize(std::string_view data);
};

struct ReplayResult {
    bool matched = false;
    Tick divergent_tick = -1;
    std::uint64_t expected = 0;
    std::uint64_t actual = 0;
};

TwinTrace record(DeterministicTwin& twin, std::vector<TwinAction> actions, std::int64_t ticks);
ReplayResult replay(DeterministicTwin& twin, const TwinTrace& trace);

class TwinEnv {
public:
    struct Observation {
        Tick tick = 0;
        Vec3Nm axis_position_nm;
        std::int64_t stock_height_sum_nm = 0;
        std::uint64_t state_hash = 0;
        std::string src;
    };
    struct StepOut {
        Observation observation;
        bool terminated = false;
        bool truncated = false;
        HaltCause halt = HaltCause::None;
    };

    explicit TwinEnv(TwinConfig config, Tick tick_cap = 100'000);
    void seed(std::uint64_t value);
    Observation reset();
    StepOut step(TwinAction action);
    [[nodiscard]] Observation observe() const;
    [[nodiscard]] const FidelityDeclaration& fidelity() const;

private:
    TwinConfig config_;
    Tick tick_cap_ = 0;
    DeterministicTwin twin_;
};

std::string_view to_string(CollisionClass value) noexcept;
std::string_view to_string(HaltCause value) noexcept;

}  // namespace chudware::twin
