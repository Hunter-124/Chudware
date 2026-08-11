#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>

#include "twin/twin.h"
#include "twin/wide_math.h"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <limits>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#ifndef CHUDWARE_SOURCE_DIR
#define CHUDWARE_SOURCE_DIR "."
#endif

namespace {
using namespace chudware::twin;
namespace device = chudware::device;

std::string executable_path;

TwinConfig config() {
    TwinConfig value;
    value.artifact_hash = "sha256:fixture-program";
    value.machine.id = "mill3-fixture";
    value.machine.axes = {
        AxisSpec{"X", 1 * kNmPerMm, -10 * kNmPerMm, 100 * kNmPerMm, 1'000 * kNmPerMm, 1'000'000 * kNmPerMm},
        AxisSpec{"Y", 1 * kNmPerMm, -10 * kNmPerMm, 100 * kNmPerMm, 1'000 * kNmPerMm, 1'000'000 * kNmPerMm},
        AxisSpec{"Z", 20 * kNmPerMm, -10 * kNmPerMm, 100 * kNmPerMm, 1'000 * kNmPerMm, 1'000'000 * kNmPerMm},
    };
    value.tool = {"endmill-1", 750'000, 10 * kNmPerMm, 2 * kNmPerMm, 5 * kNmPerMm, 10 * kNmPerMm};
    value.stock = {20, 20, 1 * kNmPerMm, {0, 0, 0}, 0, 10 * kNmPerMm};
    value.sensors = {{7, "xyz-encoder", 1}};
    value.cameras = {{9, "overhead", 2, 5, 4}};
    return value;
}

std::vector<std::string> fixtureProgram() {
    std::ifstream input(std::string(CHUDWARE_SOURCE_DIR) + "/fixtures/mfg/twin/replay/program.nc");
    if (!input) throw std::runtime_error("unable to read twin replay fixture");
    std::vector<std::string> lines;
    for (std::string line; std::getline(input, line);) if (!line.empty()) lines.push_back(std::move(line));
    return lines;
}

std::vector<TwinAction> fixtureActions() {
    std::vector<TwinAction> actions;
    Tick at = 1;
    for (const auto& line : fixtureProgram()) {
        TwinAction action;
        action.at_tick = at++;
        action.line = line;
        actions.push_back(std::move(action));
    }
    return actions;
}

std::uint64_t scenarioHash() {
    DeterministicTwin twin;
    const auto reset = twin.reset(config());
    if (!reset.ok) throw std::runtime_error(reset.refusal);
    const auto trace = record(twin, fixtureActions(), 40);
    return trace.determinismHash();
}

std::string shellQuote(std::string_view text) {
    std::string result = "'";
    for (const char c : text) {
        if (c == '\'') result += "'\\''";
        else result.push_back(c);
    }
    result.push_back('\'');
    return result;
}

std::string subprocessHash() {
    const std::string command = shellQuote(executable_path) + " --emit-twin-hash";
    FILE* stream = popen(command.c_str(), "r");
    if (!stream) return {};
    std::string output;
    std::array<char, 128> buffer{};
    while (std::fgets(buffer.data(), static_cast<int>(buffer.size()), stream)) output += buffer.data();
    if (pclose(stream) != 0) return {};
    return output;
}

struct CollisionRun {
    StepResult last;
    std::vector<CollisionEvent> events;
};

CollisionRun runUntilStop(DeterministicTwin& twin, std::int64_t cap = 1'000) {
    CollisionRun run;
    for (std::int64_t i = 0; i < cap; ++i) {
        run.last = twin.step();
        run.events.insert(run.events.end(), run.last.collisions.begin(), run.last.collisions.end());
        if (run.last.halted || run.last.program_complete) break;
    }
    return run;
}

bool hasCollision(const std::vector<CollisionEvent>& events, CollisionClass type) {
    return std::any_of(events.begin(), events.end(), [type](const auto& event) { return event.type == type; });
}

std::string lower(std::string text) {
    std::transform(text.begin(), text.end(), text.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return text;
}

TEST_CASE("portable wide arithmetic preserves exact overflow headroom") {
    namespace wide = chudware::twin::wide_math;
    constexpr auto i64_min = std::numeric_limits<std::int64_t>::min();
    constexpr auto i64_max = std::numeric_limits<std::int64_t>::max();
    constexpr auto u64_max = std::numeric_limits<std::uint64_t>::max();

    CHECK(wide::absoluteDistance(i64_min, i64_max) == u64_max);
    // Both products exceed uint64; the second case also forces a full-width
    // denominator rather than MSVC's single-word `_udiv128` fast path.
    CHECK(wide::ceilDivProductsPositive(4'000'000'000ull, 4'000'000'000ull,
                                        3'000'000'000ull, 2ull) ==
          2'666'666'667ll);
    CHECK(wide::ceilDivProductsPositive(u64_max, 100, u64_max, 3) == 34);

    REQUIRE(wide::parseScaledDecimal("9223372036.854775807", 1'000'000'000)
                .has_value());
    CHECK(*wide::parseScaledDecimal("9223372036.854775807", 1'000'000'000) ==
          i64_max);
    CHECK(*wide::parseScaledDecimal("-9223372036.854775808", 1'000'000'000) ==
          i64_min);
    CHECK_FALSE(
        wide::parseScaledDecimal("9223372036.854775808", 1'000'000'000)
            .has_value());
    CHECK_FALSE(
        wide::parseScaledDecimal("184467440737095516160", 1).has_value());

    CHECK(wide::withinSquaredRadius(4'000'000'000ll, 3'000'000'000ll,
                                    0, 0, 5'000'000'000ll));
    CHECK_FALSE(wide::withinSquaredRadius(i64_min, i64_min, i64_max, i64_max,
                                          i64_max));

    CHECK(wide::interpolate(i64_min, i64_max, 1, 2) == -1);
    CHECK(wide::interpolate(i64_max, i64_min, 1, 2) == 0);
    CHECK(wide::scaleDifference(i64_max, i64_min, 1, 2) == i64_max);
    CHECK(wide::scaleDifference(i64_min, i64_max, 1, 2) == -i64_max);
    CHECK(wide::saturatingDifferenceSum({i64_max, i64_max}, i64_min) ==
          i64_max);
}

TEST_CASE("snapshot restore reproduces every tick hash and the final snapshot bytes") {
    DeterministicTwin twin;
    REQUIRE(twin.reset(config()).ok);
    REQUIRE(twin.submitGcode("M3", 1));
    REQUIRE(twin.submitGcode("G21 G90 G1 X15 Y5 Z5 F60000", 2));
    twin.step(6);
    const TwinSnapshot midpoint = twin.snapshot();

    std::vector<std::uint64_t> first_hashes;
    for (int i = 0; i < 30; ++i) first_hashes.push_back(twin.step().state_hash);
    const TwinSnapshot first_final = twin.snapshot();

    REQUIRE(twin.restore(midpoint));
    CHECK(twin.snapshot() == midpoint);
    std::vector<std::uint64_t> second_hashes;
    for (int i = 0; i < 30; ++i) second_hashes.push_back(twin.step().state_hash);
    CHECK(second_hashes == first_hashes);
    CHECK(twin.snapshot() == first_final);

    const TwinSnapshot before_refusal = twin.snapshot();
    TwinSnapshot corrupt = midpoint;
    corrupt.bytes.front() ^= 0x01;
    CHECK_FALSE(twin.restore(corrupt));
    CHECK(twin.snapshot() == before_refusal);
    TwinSnapshot wrong_config = midpoint;
    ++wrong_config.config_hash;
    CHECK_FALSE(twin.restore(wrong_config));
    CHECK(twin.snapshot() == before_refusal);
}

TEST_CASE("record serialization and replay preserve the determinism hash") {
    DeterministicTwin recorder;
    REQUIRE(recorder.reset(config()).ok);
    const TwinTrace trace = record(recorder, fixtureActions(), 40);
    const std::string encoded = trace.serialize();
    const TwinTrace decoded = TwinTrace::deserialize(encoded);
    CHECK(decoded.serialize() == encoded);
    CHECK(decoded.determinismHash() == trace.determinismHash());

    DeterministicTwin player;
    REQUIRE(player.reset(config()).ok);
    const ReplayResult replayed = replay(player, decoded);
    CHECK(replayed.matched);
    CHECK(replayed.divergent_tick == -1);
    CHECK(replayed.actual == trace.final_hash);
}

TEST_CASE("determinism hash is stable in separate process executions") {
    const std::string first = subprocessHash();
    const std::string second = subprocessHash();
    REQUIRE_FALSE(first.empty());
    CHECK(first == second);
    CHECK(first == std::to_string(scenarioHash()) + "\n");
}

TEST_CASE("host clock offset cannot affect tick-domain output") {
    const char* previous = std::getenv("TZ");
    const std::string saved = previous ? previous : "";
    const bool had_previous = previous != nullptr;

    REQUIRE(setenv("TZ", "Pacific/Kiritimati", 1) == 0);
    tzset();
    const auto east = scenarioHash();
    REQUIRE(setenv("TZ", "Etc/GMT+12", 1) == 0);
    tzset();
    const auto west = scenarioHash();

    if (had_previous) setenv("TZ", saved.c_str(), 1);
    else unsetenv("TZ");
    tzset();
    CHECK(east == west);
}

TEST_CASE("rapid motion into remnant stock emits its typed collision") {
    auto cfg = config();
    cfg.halt_on = {CollisionClass::RapidIntoStock};
    DeterministicTwin twin;
    REQUIRE(twin.reset(cfg).ok);
    REQUIRE(twin.submitGcode("G0 Z5", 1));
    const auto run = runUntilStop(twin);
    REQUIRE(run.last.halted);
    CHECK(run.last.halt == HaltCause::Collision);
    CHECK(hasCollision(run.events, CollisionClass::RapidIntoStock));
}

TEST_CASE("holder envelope contact with a fixture emits its typed collision") {
    auto cfg = config();
    cfg.fixtures = {{42, "vise-jaw", {4 * kNmPerMm, 4 * kNmPerMm, 8 * kNmPerMm}, {6 * kNmPerMm, 6 * kNmPerMm, 14 * kNmPerMm}}};
    cfg.halt_on = {CollisionClass::ToolHolderIntoFixture};
    DeterministicTwin twin;
    REQUIRE(twin.reset(cfg).ok);
    REQUIRE(twin.submitGcode("G0 X5 Y5 Z5", 1));
    const auto run = runUntilStop(twin);
    REQUIRE(run.last.halted);
    CHECK(hasCollision(run.events, CollisionClass::ToolHolderIntoFixture));
    const auto event = std::find_if(run.events.begin(), run.events.end(), [](const auto& item) { return item.type == CollisionClass::ToolHolderIntoFixture; });
    REQUIRE(event != run.events.end());
    CHECK(event->fixture_id == 42);
    CHECK(event->penetration_nm > 0);
}

TEST_CASE("post-step axis bounds emit a travel violation") {
    auto cfg = config();
    cfg.halt_on = {CollisionClass::TravelLimitViolation};
    DeterministicTwin twin;
    REQUIRE(twin.reset(cfg).ok);
    REQUIRE(twin.submitGcode("G0 X105", 1));
    const auto run = runUntilStop(twin);
    REQUIRE(run.last.halted);
    CHECK(hasCollision(run.events, CollisionClass::TravelLimitViolation));
}

TEST_CASE("device run discontinuities map one-to-one onto typed twin halts") {
    const std::array cases{
        std::pair{device::RunDiscontinuity::Reset, HaltCause::ControllerReset},
        std::pair{device::RunDiscontinuity::Alarm, HaltCause::ControllerAlarm},
        std::pair{device::RunDiscontinuity::HardLimit, HaltCause::HardLimit},
        std::pair{device::RunDiscontinuity::Disconnect, HaltCause::TransportDisconnect},
        std::pair{device::RunDiscontinuity::PowerLoss, HaltCause::PowerLoss},
    };
    for (const auto& [fault, expected] : cases) {
        DeterministicTwin twin;
        REQUIRE(twin.reset(config()).ok);
        REQUIRE(twin.injectFault(fault, 1));
        const auto result = twin.step();
        CAPTURE(static_cast<int>(fault));
        REQUIRE(result.halted);
        CHECK(result.halt == expected);
        REQUIRE(result.faults.size() == 1);
        CHECK(result.faults.front().type == fault);
        CHECK(result.faults.front().halt == expected);
        CHECK_FALSE(twin.submitGcode("G0 X2"));
    }
}

TEST_CASE("heightfield remnant volume decreases monotonically during cutting") {
    DeterministicTwin twin;
    REQUIRE(twin.reset(config()).ok);
    REQUIRE(twin.submitGcode("M3", 1));
    REQUIRE(twin.submitGcode("G1 X15 Y5 Z5 F60000", 2));
    std::int64_t prior = twin.stockHeightSumNm();
    bool removed = false;
    for (int i = 0; i < 50; ++i) {
        twin.step();
        const auto current = twin.stockHeightSumNm();
        CHECK(current <= prior);
        removed = removed || current < prior;
        prior = current;
    }
    CHECK(removed);
}

TEST_CASE("all virtual controller sensor camera and environment observations are simulated") {
    DeterministicTwin twin;
    REQUIRE(twin.reset(config()).ok);
    const auto result = twin.step(4);
    REQUIRE_FALSE(result.observations.empty());
    bool controller = false;
    bool sensor = false;
    bool camera = false;
    for (const auto& observation : result.observations) {
        CHECK(observation.src == "simulated");
        controller = controller || observation.kind == ObservationKind::Controller;
        sensor = sensor || observation.kind == ObservationKind::Sensor;
        camera = camera || observation.kind == ObservationKind::Camera;
    }
    CHECK(controller);
    CHECK(sensor);
    CHECK(camera);

    TwinEnv env(config(), 100);
    env.seed(77);
    CHECK(env.reset().src == "simulated");
    TwinAction action;
    action.line = "G0 X2";
    const auto step = env.step(action);
    CHECK(step.observation.src == "simulated");
    CHECK(env.observe().state_hash == step.observation.state_hash);
}

TEST_CASE("fidelity declaration separates modeled approximations and quantitative limits") {
    DeterministicTwin twin;
    const auto reset = twin.reset(config());
    REQUIRE(reset.ok);
    bool modeled = false;
    bool approximated = false;
    for (const auto& aspect : reset.fidelity.aspects) {
        CHECK_FALSE(aspect.name.empty());
        CHECK_FALSE(aspect.implementation.empty());
        CHECK_FALSE(aspect.known_error_bound.empty());
        modeled = modeled || aspect.level == FidelityDeclaration::Level::Modeled;
        approximated = approximated || aspect.level == FidelityDeclaration::Level::Approximated;
    }
    CHECK(modeled);
    CHECK(approximated);
    const auto fact = twin.proceduralFact("fixture-envelope-check");
    CHECK(fact.artifact_hash == config().artifact_hash);
    CHECK(fact.snapshot_hash == twin.stateHash());
    CHECK(fact.statement.find("ran against") != std::string::npos);
}

TEST_CASE("twin-authored source and strings contain no prohibited claim phrase") {
    const std::array<std::string_view, 10> prohibited{
        "certified", "certification", "verified safe", "provably safe", "safe to run",
        "crash-proof", "collision-free", "virtual commissioning", "step-accurate", "grbl-exact",
    };
    std::string source;
    for (const std::string relative : {"core/twin/twin.h", "core/twin/twin.cpp"}) {
        std::ifstream input(std::string(CHUDWARE_SOURCE_DIR) + "/" + relative);
        REQUIRE(input.is_open());
        source.append(std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>());
    }
    source = lower(std::move(source));
    for (const auto phrase : prohibited) {
        CAPTURE(phrase);
        CHECK(source.find(phrase) == std::string::npos);
    }

    DeterministicTwin twin;
    const auto report = twin.reset(config());
    REQUIRE(report.ok);
    std::string authored = report.fidelity.id;
    for (const auto& aspect : report.fidelity.aspects) authored += aspect.name + aspect.implementation + aspect.known_error_bound;
    authored += twin.proceduralFact("check").statement;
    authored = lower(std::move(authored));
    for (const auto phrase : prohibited) CHECK(authored.find(phrase) == std::string::npos);
}

TEST_CASE("shared Grbl codec bounds input and parses every emitted wire record") {
    DeterministicTwin twin;
    REQUIRE(twin.reset(config()).ok);
    CHECK_FALSE(twin.submitGcode(std::string(2'000, 'X')));
    REQUIRE(twin.submitGcode("G0 X2", 1));
    const auto result = twin.step();
    REQUIRE_FALSE(result.grbl_wire.empty());
    bool ack = false;
    bool status = false;
    for (const auto& line : result.grbl_wire) {
        const auto push = device::parsePushMessage(line);
        const auto frame = device::parseStatusReport(line);
        ack = ack || (push && push.value.kind == device::PushKind::acknowledgment);
        status = status || static_cast<bool>(frame);
    }
    CHECK(ack);
    CHECK(status);
}

}  // namespace

int main(int argc, char** argv) {
    executable_path = argc > 0 ? argv[0] : "";
    if (argc == 2 && std::string_view(argv[1]) == "--emit-twin-hash") {
        std::cout << scenarioHash() << '\n';
        return 0;
    }
    doctest::Context context(argc, argv);
    return context.run();
}
