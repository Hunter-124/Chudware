#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <filesystem>
#include <string>
#include <vector>

#include "hostd/mfg_lane.h"

namespace {

using nlohmann::json;
using chudware::mfg::ManufacturingLane;

struct TempDirectory {
    std::filesystem::path path = std::filesystem::temp_directory_path() /
        ("chudware-mfg-lane-" + chudware::device::detail::mint_ulid());
    TempDirectory() { std::filesystem::create_directories(path); }
    ~TempDirectory() {
        std::error_code ignored;
        std::filesystem::remove_all(path, ignored);
    }
};

json call(ManufacturingLane& lane, std::string_view method, json params = json::object(),
          bool expect_error = false) {
    const auto handled = lane.handle(method, params);
    REQUIRE(handled.has_value());
    INFO("method=" << method << " body=" << handled->body.dump());
    CHECK(handled->is_error == expect_error);
    return handled->body;
}

struct LaneRig {
    TempDirectory temp;
    ManufacturingLane lane{temp.path};
    std::string device_id;
    std::string lease_id;

    LaneRig() {
        const auto list = call(lane, "mfg_device_list");
        device_id = list.at("devices").at(0).at("device_id").get<std::string>();
    }

    void claim(std::string session = "one") {
        const auto claimed = call(lane, "mfg_device_claim",
            {{"device_id", device_id},
             {"owner", {{"client_kind", "test"}, {"seat", "test-seat"},
                        {"session", std::move(session)}}}});
        lease_id = claimed.at("lease").at("lease_id").get<std::string>();
    }

    void connect() {
        call(lane, "mfg_device_connect", {{"device_id", device_id}, {"lease_id", lease_id}});
        const auto telemetry = call(lane, "mfg_telemetry_snapshot", {{"device_id", device_id}});
        REQUIRE(telemetry.at("snapshot").at("v").is_object());
        CHECK(telemetry.at("snapshot").at("v").at("device_id") == device_id);
    }

    std::string registerArtifact() {
        const auto registered = call(lane, "mfg_artifact_register",
            {{"device_id", device_id}, {"lease_id", lease_id},
             {"artifact", {{"kind", "nc"},
                           {"lines", json::array({"G90", "G0 X1", "G1 X2 F100", "G0 X0"})},
                           {"post_profile_id", "emulator"}}}});
        return registered.at("artifact").at("artifact_id").get<std::string>();
    }

    json preflight(const std::string& artifact_id, const std::string& run_id = {}) {
        json params{{"device_id", device_id}, {"lease_id", lease_id},
                    {"artifact_id", artifact_id},
                    {"job", {{"job_id", "job:test"}, {"setup_revision", 1}, {"quantity", 1}}}};
        if (!run_id.empty()) params["run_id"] = run_id;
        return call(lane, "mfg_preflight_run", std::move(params));
    }

    std::string approval(const std::string& run_id, std::string exercisable_by) {
        const auto proposed = call(lane, "mfg_approval_propose",
            {{"device_id", device_id}, {"lease_id", lease_id}, {"run_id", run_id},
             {"exercisable_by", std::move(exercisable_by)}, {"expiry_ttl_ms", 60000},
             {"attendance", {{"mode", "physical_attended"}, {"attested_by", "operator"}}}});
        const auto proposal_id = proposed.at("proposal").at("proposal_id").get<std::string>();
        const auto created = call(lane, "mfg_approval_create",
            {{"device_id", device_id}, {"lease_id", lease_id}, {"proposal_id", proposal_id},
             {"approver_auth", {{"identity", "operator"}, {"auth", "os_seat+pin"}}}});
        return created.at("approval").at("approval_id").get<std::string>();
    }

    json start(const std::string& run_id, const std::string& approval_id,
               std::string kind, bool expect_error = false) {
        json initiator{{"kind", kind}, {"identity", kind == "agent" ? "planner" : "operator"},
                       {"seat", kind == "agent" ? "agent-seat" : "human-seat"}};
        if (kind == "agent") initiator["agent_run_id"] = "agent-run:test";
        return call(lane, "mfg_run_start",
            {{"device_id", device_id}, {"lease_id", lease_id}, {"run_id", run_id},
             {"approval_id", approval_id}, {"initiator", std::move(initiator)}}, expect_error);
    }
};

json humanInitiator() {
    return {{"kind", "human"}, {"identity", "operator"}, {"seat", "human-seat"}};
}

}  // namespace

TEST_CASE("manufacturing lane does not intercept non manufacturing methods") {
    TempDirectory temp;
    ManufacturingLane lane(temp.path);
    CHECK_FALSE(lane.handle("doc_list", json::object()).has_value());
    const auto unknown = lane.handle("mfg_not_a_method", json::object());
    REQUIRE(unknown.has_value());
    CHECK(unknown->is_error);
    CHECK(unknown->body.at("code") == "mfg_unknown_method");
}

TEST_CASE("commander claim is exclusive and every write requires the lease") {
    LaneRig rig;
    rig.claim("first");
    const auto refused = call(rig.lane, "mfg_device_claim",
        {{"device_id", rig.device_id},
         {"owner", {{"client_kind", "test"}, {"seat", "other"}, {"session", "second"}}}},
        true);
    CHECK(refused.at("code") == "lease.takeover_refused");
    CHECK(refused.at("detail").at("current_owner").at("session") == "first");

    const auto unclaimed = call(rig.lane, "mfg_artifact_register",
        {{"device_id", rig.device_id},
         {"artifact", {{"kind", "nc"}, {"content", "G0 X0\n"}}}}, true);
    CHECK(unclaimed.at("code") == "lease.commander_held_by_caller");
    CHECK(unclaimed.at("detail").at("prerequisite") == "lease.commander_held_by_caller");
    const auto released = call(rig.lane, "mfg_device_release",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}});
    CHECK(released.at("released"));
    const auto takeover = call(rig.lane, "mfg_device_claim",
        {{"device_id", rig.device_id},
         {"owner", {{"client_kind", "test"}, {"seat", "other"}, {"session", "second"}}}});
    CHECK(takeover.at("lease").at("owner").at("session") == "second");
}

TEST_CASE("reads before connect are explicit unknown FreshValues") {
    LaneRig rig;
    const auto read = call(rig.lane, "mfg_telemetry_snapshot", {{"device_id", rig.device_id}});
    const auto& snapshot = read.at("snapshot");
    CHECK(snapshot.at("state") == "unknown");
    CHECK(snapshot.at("v").is_null());
    CHECK(snapshot.at("why").get<std::string>().find("not connected") != std::string::npos);
    CHECK(snapshot.at("src") == "declared");
}
TEST_CASE("disconnect clears telemetry instead of repainting last good state") {
    LaneRig rig;
    rig.claim();
    rig.connect();
    const auto disconnected = call(rig.lane, "mfg_device_disconnect",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}});
    CHECK(disconnected.at("disconnected"));
    const auto telemetry = call(rig.lane, "mfg_telemetry_snapshot",
                                {{"device_id", rig.device_id}});
    CHECK(telemetry.at("snapshot").at("state") == "unknown");
    CHECK(telemetry.at("snapshot").at("v").is_null());
    CHECK(telemetry.at("snapshot").at("why").get<std::string>().find(
              "not connected") != std::string::npos);
}


TEST_CASE("preflight is seq stamped, complete, and reproducible") {
    LaneRig rig;
    rig.claim();
    rig.connect();
    const auto artifact_id = rig.registerArtifact();
    const auto first = rig.preflight(artifact_id);
    const auto run_id = first.at("run_id").get<std::string>();
    const auto second = rig.preflight(artifact_id, run_id);
    const auto& a = first.at("gate_report");
    const auto& b = second.at("gate_report");
    CHECK(a.at("seq").get<uint64_t>() > 0);
    CHECK(b.at("seq").get<uint64_t>() > a.at("seq").get<uint64_t>());
    CHECK(a.at("ref") != b.at("ref"));
    CHECK(a.at("stage1") == b.at("stage1"));
    CHECK(a.at("stage2") == b.at("stage2"));
    CHECK(a.at("required_set") == b.at("required_set"));
    CHECK(a.at("rejecting") == b.at("rejecting"));
    CHECK(a.at("verdict") == b.at("verdict"));
    CHECK(a.at("stage2").size() == a.at("required_set").size());

    const auto reread = call(rig.lane, "mfg_gate_report",
                             {{"gate_report_ref", b.at("ref")}});
    CHECK(reread.at("gate_report") == b);
}

TEST_CASE("arbiter names human only agent refusal and nonce is single use") {
    LaneRig rig;
    rig.claim();
    rig.connect();
    const auto artifact_id = rig.registerArtifact();
    const auto preflight = rig.preflight(artifact_id);
    const auto run_id = preflight.at("run_id").get<std::string>();
    const auto approval_id = rig.approval(run_id, "human_only");

    const auto refused = rig.start(run_id, approval_id, "agent", true);
    CHECK(refused.at("code").get<std::string>().find(
              "exercisable_by_permits_initiator") != std::string::npos);
    CHECK(refused.at("detail").at("gate_report").at("verdict") == "fail");

    const auto started = rig.start(run_id, approval_id, "human");
    CHECK(started.at("state") == "running");
    CHECK(started.at("nonce_burned_at_seq").get<uint64_t>() > 0);
    const auto described = call(rig.lane, "mfg_run_describe", {{"run_id", run_id}});
    CHECK(described.at("run").at("initiator").at("kind") == "human");
    CHECK(described.at("run").at("approval_id") == approval_id);
    const auto listed = call(rig.lane, "mfg_run_list",
                             {{"device_id", rig.device_id}, {"state", "running"}});
    REQUIRE(listed.at("runs").size() == 1);
    CHECK(listed.at("runs").at(0).at("run_id") == run_id);
    const auto replay = rig.start(run_id, approval_id, "human", true);
    CHECK(replay.at("code").get<std::string>().find("approval.present_unexpired_unburned") !=
          std::string::npos);
}

TEST_CASE("hold and abort are accepted but alarm permanently forbids resume") {
    LaneRig rig;
    rig.claim();
    rig.connect();
    const auto artifact_id = rig.registerArtifact();
    const auto preflight = rig.preflight(artifact_id);
    const auto run_id = preflight.at("run_id").get<std::string>();
    const auto approval_id = rig.approval(run_id, "human_or_agent");
    REQUIRE(rig.start(run_id, approval_id, "agent").at("state") == "running");

    const auto held = call(rig.lane, "mfg_run_hold",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}, {"run_id", run_id},
         {"kind", "feed_hold"}, {"initiator", humanInitiator()}});
    CHECK(held.at("state") == "held");
    const auto resume_without_discontinuity = call(rig.lane, "mfg_run_resume",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}, {"run_id", run_id},
         {"initiator", humanInitiator()}}, true);
    CHECK(resume_without_discontinuity.at("code") == "resume_requires_new_authored_run");
    CHECK(resume_without_discontinuity.at("detail").at("discontinuity") == "none");
    CHECK(resume_without_discontinuity.at("message").get<std::string>().find(
              "fresh inspection, preflight, and approval") != std::string::npos);


    const auto alarm = call(rig.lane, "mfg_emulator_fault_inject",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}, {"fault", "alarm"}});
    CHECK(alarm.at("snapshot").at("v").at("state") == "alarm");
    const auto resume = call(rig.lane, "mfg_run_resume",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}, {"run_id", run_id},
         {"initiator", humanInitiator()}}, true);
    CHECK(resume.at("code") == "no_alarm_since_hold");
    CHECK(resume.at("message").get<std::string>().find("author a new run") != std::string::npos);

    const auto aborted = call(rig.lane, "mfg_run_abort",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}, {"run_id", run_id},
         {"initiator", humanInitiator()}});
    CHECK(aborted.at("state") == "aborting");
}
TEST_CASE("emulator fault injection is structurally barred from physical endpoints") {
    TempDirectory temp;
    ManufacturingLane lane(temp.path, true);
    const auto list = call(lane, "mfg_device_list");
    const auto device_id = list.at("devices").at(0).at("device_id").get<std::string>();
    const auto described = call(lane, "mfg_device_describe", {{"device_id", device_id}});
    CHECK(described.at("transport").at("endpoint_is_physical"));
    const auto capability = call(lane, "mfg_device_capability", {{"device_id", device_id}});
    CHECK(capability.at("capability").at("max_advertised") == "transport");
    CHECK(capability.at("capability").at("levels").at("execute").at("truth") == "unsupported");
    CHECK_FALSE(capability.at("capability").at("levels").at("execute").at("why")
                    .get<std::string>().empty());
    const auto claimed = call(lane, "mfg_device_claim",
        {{"device_id", device_id},
         {"owner", {{"client_kind", "test"}, {"seat", "test-seat"}, {"session", "physical"}}}});
    const auto lease_id = claimed.at("lease").at("lease_id").get<std::string>();
    const auto refused = call(lane, "mfg_emulator_fault_inject",
        {{"device_id", device_id}, {"lease_id", lease_id}, {"fault", "alarm"}}, true);
    CHECK(refused.at("code") == "mfg_emulator_fault_physical_endpoint_forbidden");
}


TEST_CASE("events and invalidation query preserve class and at_seq") {
    LaneRig rig;
    std::vector<json> events;
    rig.lane.setEventSink([&](const std::string& line) { events.push_back(json::parse(line)); });
    rig.claim();
    rig.connect();
    const auto artifact_id = rig.registerArtifact();
    const auto preflight = rig.preflight(artifact_id);
    const auto run_id = preflight.at("run_id").get<std::string>();
    const auto approval_id = rig.approval(run_id, "human_only");
    const auto revoked = call(rig.lane, "mfg_approval_revoke",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"approval_id", approval_id}, {"reason", "operator cancelled setup"}});
    const auto at_seq = revoked.at("at_seq").get<uint64_t>();
    const auto inspected = call(rig.lane, "mfg_approval_inspect",
                                {{"approval_id", approval_id}});
    CHECK(inspected.at("approval").at("status") == "revoked");

    const auto result = call(rig.lane, "mfg_result_invalidations",
                             {{"affected", approval_id}});
    REQUIRE(result.at("invalidations").size() == 1);
    const auto& notice = result.at("invalidations").at(0);
    CHECK(notice.at("class") == "falsified");
    CHECK(notice.at("at_seq") == at_seq);
    CHECK(notice.at("affected").at(0) == approval_id);
    const auto journal = call(rig.lane, "mfg_journal_query",
        {{"from_seq", at_seq}, {"kinds", json::array({"mfg.result.falsified"})}});
    REQUIRE(journal.at("records").size() == 1);
    CHECK(journal.at("records").at(0).at("seq") == at_seq);
    CHECK(journal.at("records").at(0).at("body").at("class") == "falsified");
    CHECK(std::any_of(events.begin(), events.end(), [&](const json& event) {
        return event.at("event") == "mfg.result.falsified" &&
               event.at("data").at("at_seq") == at_seq;
    }));
}
