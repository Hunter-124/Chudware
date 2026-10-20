#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <filesystem>
#include <fstream>
#include <set>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

#include <nlohmann/json.hpp>

#include "device/capability.h"
#include "device/controller_snapshot.h"
#include "device/device_id.h"
#include "device/transport.h"
#include "manufacturing/fresh_value.h"
#include "manufacturing/invalidation.h"
#include "manufacturing/journal.h"
#include "manufacturing/result_registry.h"

namespace {

struct TempDirectory {
    std::filesystem::path path = std::filesystem::temp_directory_path() /
        ("chudware-mfg-foundation-" + chudware::device::detail::mint_ulid());
    TempDirectory() { std::filesystem::create_directories(path); }
    ~TempDirectory() {
        std::error_code ignored;
        std::filesystem::remove_all(path, ignored);
    }
};

}  // namespace

TEST_CASE("identity canonicalization and typed ids round trip") {
    using namespace chudware::device;
    const nlohmann::json first = {{"b", 2}, {"a", 1}};
    const nlohmann::json second = {{"a", 1}, {"b", 2}};
    const auto fingerprint = MachineFingerprint::from_identity(first);
    CHECK(fingerprint == MachineFingerprint::from_identity(second));
    CHECK(fingerprint.str() == "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777");
    CHECK(MachineFingerprint::parse(fingerprint.str()).has_value());

    const auto device = DeviceId::mint(MachineClass::OpenFrameRouter, fingerprint);
    REQUIRE(DeviceId::parse(device.str()).has_value());
    CHECK(DeviceId::parse(device.str())->machine_class() == MachineClass::OpenFrameRouter);
    CHECK_FALSE(DeviceId::parse("dev:unknown:43258cff783f").has_value());

    const auto lease = LeaseId::mint();
    const auto run = RunId::mint();
    const auto approval = ApprovalId::mint();
    const auto result = ResultRef::mint("preflight_evidence");
    CHECK(LeaseId::parse(lease.str()).has_value());
    CHECK(RunId::parse(run.str()).has_value());
    CHECK(ApprovalId::parse(approval.str()).has_value());
    REQUIRE(ResultRef::parse(result.str()).has_value());
    CHECK(ResultRef::parse(result.str())->kind() == "preflight_evidence");

    const auto epoch = LaneEpoch::mint(device.str() + ":cmd", 7);
    const nlohmann::json encoded = epoch;
    REQUIRE(LaneEpoch::parse(encoded).has_value());
    CHECK(*LaneEpoch::parse(encoded) == epoch);
}

TEST_CASE("capability truth is closed and assumed disables") {
    using namespace chudware::device;
    CapabilityTruth assumed{CapabilityLevel::Execute, CapabilityTruthValue::Assumed, {},
                            EmulatorStage::E3, CapabilityLevel::Execute,
                            "physical execute requires E6"};
    CHECK(assumed.valid());
    CHECK_FALSE(assumed.enables_action());
    nlohmann::json encoded = assumed;
    CHECK(encoded.at("missing_level") == "execute");

    assumed.missing_level.reset();
    std::string why;
    CHECK_FALSE(assumed.valid(&why));
    CHECK(why.find("missing_level") != std::string::npos);
    CHECK(std::size(capability_levels) == 9);
}

TEST_CASE("read-only transport is structurally separate from command transport") {
    using namespace chudware::device;
    static_assert(std::is_base_of_v<IReadTransport, ITransport>);
    static_assert(!std::is_same_v<IReadTransport, ITransport>);
    CHECK(to_string(TransportKind::OpcUaRead) == "opcua_read");
}

TEST_CASE("FreshValue enforces null unless ok and gaps force unknown") {
    using namespace chudware::mfg;
    FreshValue<int> stale;
    stale.v = 42;
    stale.state = FreshState::Stale;
    stale.seq = 9;
    stale.why = "ttl expired";
    nlohmann::json encoded = stale;
    CHECK(encoded.at("v").is_null());

    nlohmann::json hostile = {{"v", 99}, {"state", "refused"}, {"t_mono_ns", 1},
                              {"seq", 10}, {"age_ms", 0}, {"ttl_ms", nullptr},
                              {"src", "declared"}, {"why", "not admitted"}};
    auto decoded = hostile.get<FreshValue<int>>();
    CHECK_FALSE(decoded.v.has_value());
    CHECK(decoded.state == FreshState::Refused);

    decoded.v = 7;
    decoded.state = FreshState::Ok;
    decoded.force_sequence_gap(11, 14);
    CHECK(decoded.state == FreshState::Unknown);
    CHECK_FALSE(decoded.v.has_value());
    CHECK(decoded.why == "sequence gap 11..13");
}

TEST_CASE("journal seq is monotonic across reopen and replay is idempotent") {
    using namespace chudware;
    TempDirectory temporary;
    device::Seq second = 0;
    {
        mfg::Journal journal(temporary.path, device::LaneEpoch::mint("gateway", 1));
        CHECK(journal.append("device.snapshot", "dev:emulated:000000000000", {{"n", 1}}) == 1);
        second = journal.append("run.transition", "run:00000000000000000000000000", {{"n", 2}});
        CHECK(second == 2);
        const auto first_replay = journal.replay(1);
        const auto second_replay = journal.replay(1);
        CHECK(first_replay == second_replay);
        REQUIRE(first_replay.size() == 2);
        CHECK(first_replay[0].seq < first_replay[1].seq);
        CHECK_THROWS_AS(mfg::Journal(temporary.path, device::LaneEpoch::mint("contender", 1)),
                        std::runtime_error);
    }
    {
        mfg::Journal reopened(temporary.path, device::LaneEpoch::mint("gateway", 2));
        CHECK(reopened.head() == second);
        const auto third = reopened.append("run.transition", "run:00000000000000000000000000",
                                           {{"n", 3}});
        CHECK(third == second + 1);
        CHECK(reopened.epochFloor() == third);
        const auto replay = reopened.replay(1);
        REQUIRE(replay.size() == 3);
        CHECK(replay.back().seq == third);
        CHECK(reopened.replay(third).size() == 1);
    }
    {
        std::ofstream torn(temporary.path / "journal.jsonl", std::ios::binary | std::ios::app);
        torn << "{\"seq\":999";
    }
    {
        mfg::Journal repaired(temporary.path, device::LaneEpoch::mint("gateway", 3));
        CHECK(repaired.head() == second + 1);
        CHECK(repaired.replay(1).size() == 3);
        CHECK(repaired.append("run.transition", "run:00000000000000000000000000",
                              {{"n", 4}}) == second + 2);
    }
}

TEST_CASE("journal ingestion quotas refuse without consuming durable seq or bytes") {
    using namespace chudware;
    TempDirectory temporary;
    const mfg::JournalLimits limits{1024, 4096, 2, 2, 2048};
    const auto epoch = device::LaneEpoch::mint("quota");
    {
        mfg::Journal journal(temporary.path, epoch, limits);
        CHECK(journal.append("event", "subject", {{"n", 1}}) == 1);
        const auto bytes = std::filesystem::file_size(temporary.path / "journal.jsonl");
        CHECK_THROWS(journal.append("event", "subject", {{"payload", std::string(1024, 'x')}}));
        CHECK(journal.head() == 1);
        CHECK(std::filesystem::file_size(temporary.path / "journal.jsonl") == bytes);
        CHECK(journal.append("event", "subject", {{"n", 2}}) == 2);
        const auto full_bytes = std::filesystem::file_size(temporary.path / "journal.jsonl");
        CHECK_THROWS_AS(journal.append("event", "subject", {{"n", 3}}), std::length_error);
        CHECK(journal.head() == 2);
        CHECK(std::filesystem::file_size(temporary.path / "journal.jsonl") == full_bytes);
    }
    mfg::Journal reopened(temporary.path, epoch, limits);
    CHECK(reopened.head() == 2);
    CHECK(reopened.epochFloor() == 1);
    CHECK(reopened.replay().size() == 2);
    CHECK_THROWS_AS(reopened.append("event", "subject", {{"n", 3}}), std::length_error);
}

TEST_CASE("journal total byte quota refuses before checkpoint reservation") {
    using namespace chudware;
    TempDirectory temporary;
    mfg::Journal journal(temporary.path, device::LaneEpoch::mint("bytes"),
                         {1024, 1024, 100, 100, 1024});
    while (true) {
        const auto head = journal.head();
        const auto bytes = std::filesystem::file_size(temporary.path / "journal.jsonl");
        try {
            journal.append("event", "subject", {{"payload", std::string(200, 'x')}});
        } catch (const std::exception&) {
            CHECK(head > 0);
            CHECK(journal.head() == head);
            CHECK(std::filesystem::file_size(temporary.path / "journal.jsonl") == bytes);
            std::ifstream checkpoint(temporary.path / "seq.head");
            device::Seq reserved = 0;
            checkpoint >> reserved;
            CHECK(reserved == head);
            break;
        }
        REQUIRE(journal.head() <= 3);
    }
}

TEST_CASE("journal startup rejects oversized records totals and counts without truncation") {
    using namespace chudware;
    const mfg::JournalLimits limits{1024, 2048, 2, 2, 2048};
    for (size_t size : {size_t{1025}, size_t{2049}}) {
        TempDirectory temporary;
        {
            std::ofstream hostile(temporary.path / "journal.jsonl", std::ios::binary);
            hostile << std::string(size - 1, 'x') << '\n';
        }
        CHECK_THROWS_AS(mfg::Journal(temporary.path, device::LaneEpoch::mint("hostile"), limits),
                        std::length_error);
        CHECK(std::filesystem::file_size(temporary.path / "journal.jsonl") == size);
        // A failed constructor must release the writer lock, not mask the refusal.
        CHECK_THROWS_AS(mfg::Journal(temporary.path, device::LaneEpoch::mint("again"), limits),
                        std::length_error);
    }
    TempDirectory temporary;
    {
        mfg::Journal journal(temporary.path, device::LaneEpoch::mint("seed"));
        for (int n = 0; n < 3; ++n) journal.append("event", "subject", {{"n", n}});
    }
    const auto bytes = std::filesystem::file_size(temporary.path / "journal.jsonl");
    CHECK_THROWS_AS(mfg::Journal(temporary.path, device::LaneEpoch::mint("count"), limits),
                    std::length_error);
    CHECK(std::filesystem::file_size(temporary.path / "journal.jsonl") == bytes);
}

TEST_CASE("journal replay is bounded while streaming restore retains every record") {
    using namespace chudware;
    TempDirectory temporary;
    const auto epoch = device::LaneEpoch::mint("stream");
    const mfg::JournalLimits limits{1024, 8192, 8, 1, 1024};
    {
        mfg::Journal journal(temporary.path, epoch, limits);
        for (int n = 1; n <= 3; ++n) journal.append("event", "subject", {{"n", n}});
        CHECK_THROWS_AS(journal.replay(), std::length_error);
        REQUIRE(journal.replay(3).size() == 1);
        size_t visited = 0;
        journal.visit(1, [&](const mfg::JournalRecord& row) {
            CHECK(row.seq == ++visited);
            return true;
        });
        CHECK(visited == 3);
        visited = 0;
        journal.visit(1, [&](const mfg::JournalRecord&) { ++visited; return false; });
        CHECK(visited == 1);
    }
    mfg::Journal reopened(temporary.path, epoch, limits);
    CHECK(reopened.head() == 3);
    CHECK(reopened.epochFloor() == 1);
    CHECK_THROWS_AS(reopened.replay(), std::length_error);
}

TEST_CASE("journal byte-bounded replay and torn tail preserve reserved sequence gaps") {
    using namespace chudware;
    TempDirectory temporary;
    const auto epoch = device::LaneEpoch::mint("gap");
    const mfg::JournalLimits limits{1024, 8192, 8, 8, 1024};
    {
        mfg::Journal journal(temporary.path, epoch, limits);
        journal.append("event", "subject", {{"payload", std::string(400, 'x')}});
        journal.append("event", "subject", {{"payload", std::string(400, 'y')}});
        CHECK_THROWS_AS(journal.replay(), std::length_error);
    }
    {
        std::ofstream checkpoint(temporary.path / "seq.head");
        checkpoint << "3\n";
        std::ofstream torn(temporary.path / "journal.jsonl", std::ios::binary | std::ios::app);
        torn << "{\"seq\":3";
    }
    mfg::Journal repaired(temporary.path, epoch, limits);
    CHECK(repaired.head() == 3);
    CHECK(repaired.epochFloor() == 1);
    CHECK(repaired.append("event", "subject", {{"n", 4}}) == 4);
    const auto last = repaired.replay(4);
    REQUIRE(last.size() == 1);
    CHECK(last.at(0).seq == 4);
}

TEST_CASE("result registry is total and refuses unregistered kinds") {
    using namespace chudware::mfg;
    const auto registry = ResultRegistry::manufacturingDefaults();
    CHECK(registry.coversAllManufacturingDomains());
    CHECK(registry.all().size() >= 80);
    for (const auto& row : registry.all()) {
        CAPTURE(row.kind);
        CHECK_FALSE(row.domain.empty());
        CHECK_FALSE(row.runtime_invalidation_triggers.empty());
        CHECK_FALSE(to_string(row.epoch_policy).empty());
    }

    const std::set<std::string> capability_kinds = {
        "capability_report", "twin_capability", "metrology.capability", "print.capability",
        "perception.capability", "monitor_capability", "cam.cut.capability", "cam.turn.capability",
        "pnp.capability", "robot_capability"};
    for (const auto& kind : capability_kinds) {
        CAPTURE(kind);
        REQUIRE(registry.find(kind) != nullptr);
        CHECK_FALSE(registry.find(kind)->supersede_with_parent);
    }

    const auto refusal = registry.validate("unregistered.paintable_result");
    CHECK_FALSE(refusal.accepted);
    CHECK(refusal.code == "mfg_unregistered_result_kind");
}

TEST_CASE("invalidation is seq-scoped and idempotent for both classes") {
    using namespace chudware::mfg;
    InvalidationEngine engine;

    std::vector<DurableResultState> falsified = {
        {"res:x", 10, {{"value", 1}}}, {"res:x", 11, {{"value", 2}}}};
    InvalidationNotice falsify{InvalidationClass::Falsified, "artifact_rehashed",
                               "artifact bytes changed", {"res:x"}, 10, std::nullopt};
    const auto first = engine.apply(falsify, falsified);
    CHECK(first.matched == 1);
    CHECK(first.changed == 1);
    CHECK(falsified[0].cleared);
    CHECK(falsified[0].value.is_null());
    CHECK(falsified[1].current);
    CHECK(engine.apply(falsify, falsified).changed == 0);

    std::vector<DurableResultState> superseded = {
        {"res:y", 20, {{"value", 1}}}, {"res:y", 21, {{"value", 2}}}};
    InvalidationNotice supersede{InvalidationClass::Superseded, "newer_measurement",
                                 "a newer qualified result exists", {"res:y"}, 20,
                                 std::string("res:y:new")};
    const auto second = engine.apply(supersede, superseded);
    CHECK(second.matched == 1);
    CHECK(second.changed == 1);
    CHECK_FALSE(superseded[0].current);
    CHECK_FALSE(superseded[0].cleared);
    CHECK_FALSE(superseded[0].value.is_null());
    CHECK(superseded[0].superseded_by == std::optional<std::string>("res:y:new"));
    CHECK(superseded[1].current);
    CHECK(engine.apply(supersede, superseded).changed == 0);
}

TEST_CASE("run state validator rejects transparent resume after alarm") {
    using namespace chudware::device;
    const auto illegal = validate_run_transition(RunState::Held, RunState::Resuming,
                                                 RunDiscontinuity::Alarm);
    CHECK_FALSE(illegal.allowed);
    CHECK(illegal.why.find("resume forbidden") != std::string::npos);
    CHECK(validate_run_transition(RunState::Held, RunState::Resuming).allowed);
    CHECK(validate_run_transition(RunState::Holding, RunState::Aborting).allowed);
    CHECK_FALSE(validate_run_transition(RunState::Faulted, RunState::Running).allowed);
}
