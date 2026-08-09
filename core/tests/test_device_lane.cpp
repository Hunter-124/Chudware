#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "device/emulator_transport.h"
#include "device/grbl_adapter.h"

#include <algorithm>
#include <cstdint>
#include <fstream>
#include <string>
#include <string_view>
#include <vector>

#ifndef CHUDWARE_SOURCE_DIR
#define CHUDWARE_SOURCE_DIR "."
#endif

namespace {
using namespace chudware::device;
using chudware::mfg::FreshSource;
using chudware::mfg::FreshState;

std::vector<std::string> readLines(std::string_view relative_path) {
    const std::string path = std::string(CHUDWARE_SOURCE_DIR) + "/" + std::string(relative_path);
    std::ifstream input(path);
    REQUIRE_MESSAGE(input.is_open(), "unable to open fixture: ", path);
    std::vector<std::string> lines;
    for (std::string line; std::getline(input, line);) {
        if (!line.empty() && line.back() == '\r') line.pop_back();
        if (!line.empty()) lines.push_back(std::move(line));
    }
    return lines;
}

struct LaneHarness {
    std::uint64_t now_ns = 0;
    Seq seq = 0;
    EmulatorTransport emulator;
    GrblAdapter adapter;
    Endpoint endpoint{"fixture-grbl", "inproc://fixture-grbl", false};

    LaneHarness()
        : emulator(),
          adapter(emulator, [this] { return now_ns; }) {}

    void tick(std::uint64_t advance_ms = 200) {
        now_ns += advance_ms * 1'000'000;
        adapter.service(++seq);
    }

    void openAndIdentify() {
        REQUIRE(adapter.open(endpoint).ok);
        for (int i = 0; i < 12 && !adapter.fingerprint(); ++i) tick();
        REQUIRE(adapter.identity());
        REQUIRE(adapter.fingerprint());
        for (int i = 0; i < 4; ++i) {
            const auto value = adapter.snapshot();
            if (value.v && value.v->state == DeviceState::Idle && !value.v->device_id.empty()) return;
            tick();
        }
        FAIL("emulator did not produce a fresh identified idle snapshot");
    }
};

TEST_CASE("identify binds banner, build options, settings, and a stable reconnect fingerprint") {
    LaneHarness lane;
    lane.openAndIdentify();
    const auto expected = readLines("fixtures/mfg/gateway/grbl/emulator_identity.txt");
    REQUIRE(expected.size() == 11);

    const auto first_fingerprint = lane.adapter.fingerprint()->str();
    const auto first_device_id = lane.adapter.deviceId()->str();
    const auto first_epoch = lane.adapter.laneEpoch();
    const auto& identity = *lane.adapter.identity();
    CHECK(identity.firmware_banner == expected[0]);
    CHECK(identity.build_options.find(expected[1]) != std::string::npos);
    CHECK(identity.build_options.find(expected[2]) != std::string::npos);
    for (std::size_t i = 3; i < expected.size(); ++i) {
        const auto equals = expected[i].find('=');
        REQUIRE(equals != std::string::npos);
        CHECK(identity.settings.at(expected[i].substr(0, equals)) == expected[i].substr(equals + 1));
    }
    CHECK(identity.axis_count == 3);
    CHECK(identity.transport_kind == "emulator");

    lane.openAndIdentify();
    CHECK(lane.adapter.fingerprint()->str() == first_fingerprint);
    CHECK(lane.adapter.deviceId()->str() == first_device_id);
    CHECK(lane.adapter.laneEpoch().process_start_id == first_epoch.process_start_id);
    CHECK(lane.adapter.laneEpoch().epoch == first_epoch.epoch + 1);

    const auto snapshot = lane.adapter.snapshot();
    REQUIRE(snapshot.v);
    CHECK(snapshot.src == FreshSource::Simulated);
    CHECK(snapshot.v->raw.starts_with("<Idle"));
}

TEST_CASE("character-counted streaming fills but never exceeds RX and acknowledges the full program") {
    LaneHarness lane;
    lane.openAndIdentify();
    const auto program = readLines("fixtures/mfg/gateway/grbl/character_count_stream.nc");
    REQUIRE(program.size() == 18);
    REQUIRE(lane.adapter.startProgram(program));

    auto progress = lane.adapter.streamProgress();
    CHECK(progress.sent_lines < progress.total_lines);
    CHECK(progress.outstanding_characters > 0);
    CHECK(progress.outstanding_characters <= GrblRxAccounting::kDefaultRxCapacity);
    std::size_t peak_outstanding = progress.outstanding_characters;

    for (int i = 0; i < 80 && !progress.complete; ++i) {
        lane.tick();
        progress = lane.adapter.streamProgress();
        peak_outstanding = std::max(peak_outstanding, progress.outstanding_characters);
        CHECK(progress.outstanding_characters <= GrblRxAccounting::kDefaultRxCapacity);
        CHECK(lane.emulator.rxUsed() <= GrblRxAccounting::kDefaultRxCapacity);
    }
    REQUIRE(progress.complete);
    CHECK_FALSE(progress.halted);
    CHECK(progress.sent_lines == program.size());
    CHECK(progress.acknowledged_lines == program.size());
    CHECK(progress.outstanding_characters == 0);
    CHECK(peak_outstanding > 100);
    CHECK(lane.emulator.acceptedProgramLines() == program.size());
    CHECK(lane.emulator.acknowledgedProgramLines() == program.size());

    lane.tick();
    const auto snapshot = lane.adapter.snapshot();
    REQUIRE(snapshot.v);
    CHECK(snapshot.v->state == DeviceState::Idle);
    CHECK(snapshot.v->mpos[0] == doctest::Approx(0.0));
    CHECK(snapshot.v->mpos[1] == doctest::Approx(0.0));
    CHECK(snapshot.v->mpos[2] == doctest::Approx(5.0));
}

TEST_CASE("realtime hold resume abort operations are real Grbl bytes and polling stays at five hertz") {
    LaneHarness lane;
    lane.openAndIdentify();

    const std::size_t polls = lane.emulator.statusQueryCount();
    for (int i = 0; i < 19; ++i) lane.tick(10);
    CHECK(lane.emulator.statusQueryCount() == polls);
    lane.tick(10);
    CHECK(lane.emulator.statusQueryCount() == polls + 1);

    const auto program = readLines("fixtures/mfg/gateway/grbl/character_count_stream.nc");
    REQUIRE(lane.adapter.startProgram(program));
    REQUIRE(lane.adapter.requestHold());
    CHECK(lane.emulator.holdRequestCount() == 1);
    lane.tick();
    REQUIRE(lane.adapter.snapshot().v);
    CHECK(lane.adapter.snapshot().v->state == DeviceState::Hold);

    REQUIRE(lane.adapter.requestResume());
    CHECK(lane.emulator.resumeRequestCount() == 1);
    lane.tick();
    REQUIRE(lane.adapter.snapshot().v);
    CHECK(lane.adapter.snapshot().v->state == DeviceState::Running);

    const auto epoch = lane.adapter.laneEpoch().epoch;
    REQUIRE(lane.adapter.abort());
    CHECK(lane.emulator.holdRequestCount() == 2);
    CHECK(lane.emulator.resetRequestCount() == 1);
    CHECK(lane.adapter.laneEpoch().epoch == epoch + 1);
    CHECK(lane.adapter.streamProgress().halted);
    CHECK(lane.adapter.terminalReason().find("abort requested") != std::string::npos);
    CHECK_FALSE(lane.adapter.requestResume());
}

TEST_CASE("missing status polls force unknown and retain last-good only for display") {
    LaneHarness lane;
    lane.openAndIdentify();
    const auto good = lane.adapter.snapshot();
    REQUIRE(good.state == FreshState::Ok);
    REQUIRE(good.v);

    lane.emulator.setStatusResponsesEnabled(false);
    lane.now_ns += 1'000'000'000;
    const auto gap = lane.adapter.snapshot();
    CHECK(gap.state == FreshState::Unknown);
    CHECK_FALSE(gap.v);
    REQUIRE(gap.last_ok);
    CHECK(gap.last_ok->v.raw == good.v->raw);
    CHECK(gap.why.find("status poll gap") != std::string::npos);
    CHECK(gap.src == FreshSource::Simulated);
}

TEST_CASE("injected controller and wire faults normalize without hiding the vendor line") {
    struct FaultCase {
        EmulatorFault fault;
        DeviceState expected_state;
        RunDiscontinuity expected_discontinuity;
        std::string_view reason;
        std::string_view raw;
    };
    const std::vector<FaultCase> cases = {
        {EmulatorFault::Alarm, DeviceState::Alarm, RunDiscontinuity::Alarm, "alarm ALARM:2", "ALARM:2"},
        {EmulatorFault::HardLimit, DeviceState::Alarm, RunDiscontinuity::HardLimit, "hard limit", "ALARM:1"},
        {EmulatorFault::Door, DeviceState::Door, RunDiscontinuity::None, "", "<Door:0"},
        {EmulatorFault::Disconnect, DeviceState::Disconnected, RunDiscontinuity::Disconnect, "disconnect", ""},
        {EmulatorFault::GarbageLine, DeviceState::Fault, RunDiscontinuity::None, "unparseable", "%%%not-grbl%%%"},
        {EmulatorFault::RxOverflow, DeviceState::Fault, RunDiscontinuity::None, "RX overflow", "error:11"},
    };

    for (const auto& test : cases) {
        LaneHarness lane;
        lane.openAndIdentify();
        lane.emulator.injectFault(test.fault);
        lane.tick();
        INFO("fault=" << static_cast<int>(test.fault));
        const auto snapshot = lane.adapter.snapshot();
        REQUIRE(snapshot.v);
        CHECK(snapshot.v->state == test.expected_state);
        CHECK(lane.adapter.discontinuity() == test.expected_discontinuity);
        if (!test.reason.empty()) CHECK(lane.adapter.terminalReason().find(test.reason) != std::string::npos);
        if (!test.raw.empty()) CHECK(snapshot.v->raw.find(test.raw) != std::string::npos);
    }
}

TEST_CASE("alarm reset and disconnect name why transparent resume is forbidden") {
    SUBCASE("alarm") {
        LaneHarness lane;
        lane.openAndIdentify();
        lane.emulator.injectFault(EmulatorFault::Alarm);
        lane.tick();
        const auto resume = lane.adapter.requestResume();
        CHECK_FALSE(resume);
        CHECK(resume.why.find("alarm ALARM:2") != std::string::npos);
    }
    SUBCASE("reset bumps the lane epoch") {
        LaneHarness lane;
        lane.openAndIdentify();
        const auto epoch = lane.adapter.laneEpoch().epoch;
        REQUIRE(lane.adapter.reset());
        CHECK(lane.adapter.laneEpoch().epoch == epoch + 1);
        const auto resume = lane.adapter.requestResume();
        CHECK_FALSE(resume);
        CHECK(resume.why.find("reset") != std::string::npos);
    }
    SUBCASE("disconnect") {
        LaneHarness lane;
        lane.openAndIdentify();
        lane.emulator.injectFault(EmulatorFault::Disconnect);
        lane.tick();
        const auto resume = lane.adapter.requestResume();
        CHECK_FALSE(resume);
        CHECK(resume.why.find("disconnect") != std::string::npos);
    }
}

TEST_CASE("hardware-free provenance and adapter capability evidence cannot claim execution or safety") {
    LaneHarness lane;
    lane.endpoint.endpoint_is_physical = true;  // Emulator kind still forces simulated provenance.
    lane.openAndIdentify();

    const auto snapshot = lane.adapter.snapshot();
    CHECK(snapshot.src == FreshSource::Simulated);
    const auto report = lane.adapter.capabilityEvidence();
    REQUIRE(report.complete());
    CHECK(report.max_advertised == CapabilityLevel::Transport);
    CHECK(report.levels.at(CapabilityLevel::Parse).truth == CapabilityTruthValue::Verified);
    CHECK(report.levels.at(CapabilityLevel::Transport).truth == CapabilityTruthValue::Verified);
    CHECK_FALSE(report.levels.at(CapabilityLevel::Execute).enables_action());
    CHECK_FALSE(report.levels.at(CapabilityLevel::SafetyIntegration).enables_action());
    CHECK(report.levels.at(CapabilityLevel::Execute).truth == CapabilityTruthValue::Unsupported);
    CHECK(report.levels.at(CapabilityLevel::SafetyIntegration).truth == CapabilityTruthValue::Unsupported);
    for (const auto& [level, truth] : report.levels) {
        if (truth.truth == CapabilityTruthValue::Verified)
            CHECK((level == CapabilityLevel::Parse || level == CapabilityLevel::Transport));
    }
}

}  // namespace
