#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <filesystem>
#include <string>
#include <map>
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

chudware::mfg::gateway::AllowlistEntry commissioned(
    chudware::device::TransportKind kind,
    chudware::device::MachineClass machine_class,
    std::string suffix = {}) {
    using namespace chudware;
    mfg::gateway::AllowlistEntry entry;
    entry.machine_class = machine_class;
    entry.operator_note = "test commissioning";
    if (kind == device::TransportKind::Serial) {
        entry.endpoint = mfg::gateway::EndpointIdentity::serial(
            "/tmp/chudware-test-serial" + suffix);
        entry.connection["baud"] = "115200";
    } else {
        entry.endpoint = mfg::gateway::EndpointIdentity::network(
            kind, "127.0.0.1", static_cast<std::uint16_t>(39000 + suffix.size()));
        if (kind == device::TransportKind::WebSocket) entry.connection["path"] = "/ws";
    }
    return entry;
}

void seedAllowlist(const std::filesystem::path& directory,
                   const chudware::mfg::gateway::AllowlistEntry& entry) {
    chudware::mfg::gateway::DeviceAllowlist allowlist(
        directory / "device_allowlist.tsv");
    std::string why;
    REQUIRE(allowlist.upsert(entry, &why));
    INFO(why);
}

json findTransport(const json& listed, std::string_view kind) {
    for (const auto& row : listed.at("devices"))
        if (row.at("transport").at("kind") == kind) return row;
    FAIL("transport candidate not found: " << kind);
    return nullptr;
}

json call(ManufacturingLane& lane, std::string_view method, json params = json::object(),
          bool expect_error = false,
          chudware::mfg::LeaseOwner caller =
              {"test", "test-seat", "test-session"}) {
    const auto handled = lane.handle(method, params, caller);
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
            {{"device_id", device_id}, {"lease_id", lease_id},
             {"proposal_id", proposal_id}});
        return created.at("approval").at("approval_id").get<std::string>();
    }

    json start(const std::string& run_id, const std::string& approval_id,
               std::string kind, bool expect_error = false) {
        json params{{"device_id", device_id}, {"lease_id", lease_id},
                    {"run_id", run_id}, {"approval_id", approval_id}};
        if (kind == "agent")
            params["initiator"] = {{"kind", "agent"}, {"identity", "forged-agent"},
                                   {"seat", "forged-seat"},
                                   {"agent_run_id", "forged-agent-run"}};
        return call(lane, "mfg_run_start", std::move(params), expect_error);
    }
};


struct StartedPrinterRun {
    std::string run_id;
    std::string approval_id;
};

StartedPrinterRun startPrinterRun(ManufacturingLane& lane,
                                  const std::string& device_id,
                                  const std::string& lease_id) {
    const auto artifact = call(
        lane, "mfg_artifact_register",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"artifact", {{"kind", "print_job"}, {"lines", json::array({"G28", "G1 X1"})},
                       {"slicer_profile_hash", "slicer:terminal"}}}});
    const auto preflight = call(
        lane, "mfg_preflight_run",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"artifact_id", artifact.at("artifact").at("artifact_id")},
         {"job", {{"job_id", "job:terminal"}, {"setup_revision", 1}, {"quantity", 1}}}});
    const std::string run_id = preflight.at("run_id");
    const auto proposal = call(
        lane, "mfg_approval_propose",
        {{"device_id", device_id}, {"lease_id", lease_id}, {"run_id", run_id},
         {"exercisable_by", "human_only"}, {"expiry_ttl_ms", 60000},
         {"attendance", {{"mode", "physical_attended"}, {"attested_by", "operator"}}}});
    const auto approval = call(
        lane, "mfg_approval_create",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"proposal_id", proposal.at("proposal").at("proposal_id")}});
    const std::string approval_id = approval.at("approval").at("approval_id");
    const auto started = call(
        lane, "mfg_run_start",
        {{"device_id", device_id}, {"lease_id", lease_id}, {"run_id", run_id},
         {"approval_id", approval_id}});
    REQUIRE(started.at("state") == "running");
    return {run_id, approval_id};
}

}  // namespace

TEST_CASE("manufacturing lane does not intercept non manufacturing methods") {
    TempDirectory temp;
    ManufacturingLane lane(temp.path);
    const chudware::mfg::LeaseOwner caller{"test", "seat", "session"};
    CHECK_FALSE(lane.handle("doc_list", json::object(), caller).has_value());
    const auto unknown = lane.handle("mfg_not_a_method", json::object(), caller);
    REQUIRE(unknown.has_value());
    CHECK(unknown->is_error);
    CHECK(unknown->body.at("code") == "mfg_unknown_method");
}

TEST_CASE("commander claim is exclusive and every write requires the lease") {
    LaneRig rig;
    const chudware::mfg::LeaseOwner first{"test", "test-seat", "first"};
    const chudware::mfg::LeaseOwner second{"test", "other", "second"};
    const auto first_claim = call(
        rig.lane, "mfg_device_claim", {{"device_id", rig.device_id}}, false, first);
    rig.lease_id = first_claim.at("lease").at("lease_id");
    const auto refused = call(rig.lane, "mfg_device_claim",
                              {{"device_id", rig.device_id}}, true, second);
    CHECK(refused.at("code") == "lease.takeover_refused");
    CHECK(refused.at("detail").at("current_owner").at("session") == "first");

    const auto unclaimed = call(rig.lane, "mfg_artifact_register",
        {{"device_id", rig.device_id},
         {"artifact", {{"kind", "nc"}, {"content", "G0 X0\n"}}}}, true, second);
    CHECK(unclaimed.at("code") == "lease.commander_held_by_caller");
    CHECK(unclaimed.at("detail").at("prerequisite") == "lease.commander_held_by_caller");
    const auto released = call(rig.lane, "mfg_device_release",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}}, false, first);
    CHECK(released.at("released"));
    const auto takeover = call(rig.lane, "mfg_device_claim",
                               {{"device_id", rig.device_id}}, false, second);
    CHECK(takeover.at("lease").at("owner").at("session") == "second");
}

TEST_CASE("lease bearer value is caller-bound and redacted from observers") {
    TempDirectory temp;
    ManufacturingLane lane(temp.path);
    const chudware::mfg::LeaseOwner human{"gui", "operator", "human-session"};
    const chudware::mfg::LeaseOwner attacker{"mcp", "agent", "agent-session"};
    const auto listed = call(lane, "mfg_device_list", json::object(), false, human);
    const std::string device_id = listed.at("devices").at(0).at("device_id");
    std::vector<json> events;
    lane.setEventSink([&](const std::string& line) {
        events.push_back(json::parse(line));
    });
    const auto claimed = call(
        lane, "mfg_device_claim", {{"device_id", device_id}}, false, human);
    const std::string lease_id = claimed.at("lease").at("lease_id");
    REQUIRE(events.size() == 1);
    CHECK_FALSE(events.at(0).at("data").at("lease").contains("lease_id"));

    const auto owner_view =
        call(lane, "mfg_device_list", json::object(), false, human);
    CHECK(owner_view.at("devices").at(0).at("lease").at("lease_id") == lease_id);
    const auto attacker_view =
        call(lane, "mfg_device_list", json::object(), false, attacker);
    CHECK_FALSE(attacker_view.at("devices").at(0).at("lease").contains("lease_id"));

    const auto stolen_write = call(
        lane, "mfg_artifact_register",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"artifact", {{"kind", "nc"}, {"content", "G0 X0\n"}}}},
        true, attacker);
    CHECK(stolen_write.at("code") == "lease.commander_held_by_caller");
    const auto stolen_release = call(
        lane, "mfg_device_release",
        {{"device_id", device_id}, {"lease_id", lease_id}}, true, attacker);
    CHECK(stolen_release.at("code") == "lease.commander_held_by_caller");
    const auto forged_attention = call(
        lane, "mfg_attention_raise",
        {{"reason", "forged"}, {"why", "not the commander"}}, true, attacker);
    CHECK(forged_attention.at("code") == "lease.commander_held_by_caller");
    const auto journal_read =
        call(lane, "mfg_journal_query", json::object(), true, attacker);
    CHECK(journal_read.at("code") == "lease.commander_held_by_caller");
}

TEST_CASE("artifact registration rejects filesystem path sources") {
    LaneRig rig;
    rig.claim();
    const auto refused = call(
        rig.lane, "mfg_artifact_register",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"artifact", {{"kind", "nc"}, {"path", "/etc/passwd"}}}},
        true);
    CHECK(refused.at("code") == "mfg_artifact_content_missing");
    CHECK(refused.at("message").get<std::string>().find("content or lines") !=
          std::string::npos);
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

TEST_CASE("request-asserted initiator is refused and authenticated nonce is single use") {
    LaneRig rig;
    rig.claim();
    rig.connect();
    const auto artifact_id = rig.registerArtifact();
    const auto preflight = rig.preflight(artifact_id);
    const auto run_id = preflight.at("run_id").get<std::string>();
    const auto approval_id = rig.approval(run_id, "human_only");

    const auto refused = rig.start(run_id, approval_id, "agent", true);
    CHECK(refused.at("code") == "mfg_initiator_identity_assertion_forbidden");

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
    REQUIRE(rig.start(run_id, approval_id, "human").at("state") == "running");

    const auto held = call(rig.lane, "mfg_run_hold",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"run_id", run_id}, {"kind", "feed_hold"}});
    CHECK(held.at("state") == "held");
    const auto resume_without_discontinuity = call(rig.lane, "mfg_run_resume",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"run_id", run_id}}, true);
    CHECK(resume_without_discontinuity.at("code") == "resume_requires_new_authored_run");
    CHECK(resume_without_discontinuity.at("detail").at("discontinuity") == "none");
    CHECK(resume_without_discontinuity.at("message").get<std::string>().find(
              "fresh inspection, preflight, and approval") != std::string::npos);


    const auto alarm = call(rig.lane, "mfg_emulator_fault_inject",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}, {"fault", "alarm"}});
    CHECK(alarm.at("snapshot").at("v").at("state") == "alarm");
    const auto resume = call(rig.lane, "mfg_run_resume",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"run_id", run_id}}, true);
    CHECK(resume.at("code") == "no_alarm_since_hold");
    CHECK(resume.at("message").get<std::string>().find("author a new run") != std::string::npos);

    const auto aborted = call(rig.lane, "mfg_run_abort",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"run_id", run_id}});
    CHECK(aborted.at("state") == "aborting");
}

TEST_CASE("run describe deterministically advances a completed emulator stream") {
    LaneRig rig;
    rig.claim();
    rig.connect();
    const auto artifact_id = rig.registerArtifact();
    const auto preflight = rig.preflight(artifact_id);
    const std::string run_id = preflight.at("run_id");
    const auto approval_id = rig.approval(run_id, "human_only");
    REQUIRE(rig.start(run_id, approval_id, "human").at("state") == "running");

    json described;
    for (int poll = 0; poll < 64; ++poll) {
        described = call(rig.lane, "mfg_run_describe", {{"run_id", run_id}});
        if (described.at("run").at("state") == "completed") break;
    }
    CHECK(described.at("run").at("state") == "completed");
    CHECK(described.at("run").at("discontinuity") == "none");
    const auto terminal_events = call(
        rig.lane, "mfg_journal_query",
        {{"subject", run_id}, {"kinds", json::array({"mfg.run.state"})}});
    CHECK(std::any_of(
        terminal_events.at("records").begin(), terminal_events.at("records").end(),
        [](const json& record) {
            return record.at("body").value("state", std::string{}) == "completed" &&
                   record.at("body").value("terminal", false);
        }));
}

TEST_CASE("alarm hard limit and disconnect terminate a running emulator stream") {
    struct FaultCase {
        const char* fault;
        const char* discontinuity;
        const char* resume_code;
    };
    const FaultCase cases[] = {
        {"alarm", "alarm", "no_alarm_since_hold"},
        {"hard_limit", "hard_limit", "resume_forbidden_after_hard_limit"},
        {"disconnect", "disconnect", "resume_forbidden_after_disconnect"},
    };
    for (const auto& fault_case : cases) {
        CAPTURE(fault_case.fault);
        LaneRig rig;
        rig.claim();
        rig.connect();
        const auto artifact_id = rig.registerArtifact();
        const auto preflight = rig.preflight(artifact_id);
        const std::string run_id = preflight.at("run_id");
        const auto approval_id = rig.approval(run_id, "human_or_agent");
        REQUIRE(rig.start(run_id, approval_id, "human").at("state") == "running");

        call(rig.lane, "mfg_emulator_fault_inject",
             {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
              {"fault", fault_case.fault}});
        const auto described =
            call(rig.lane, "mfg_run_describe", {{"run_id", run_id}});
        CHECK(described.at("run").at("state") == "faulted");
        CHECK(described.at("run").at("discontinuity") == fault_case.discontinuity);
        const auto resume = call(
            rig.lane, "mfg_run_resume",
            {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
             {"run_id", run_id}},
            true);
        CHECK(resume.at("code") == fault_case.resume_code);
        CHECK(resume.at("detail").at("discontinuity") ==
              fault_case.discontinuity);
        const auto reopen = call(
            rig.lane, "mfg_device_connect",
            {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}}, true);
        CHECK(reopen.at("code") == "mfg_transport_terminal");
    }
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
    CHECK(capability.at("capability").at("levels").at("execute").at("truth") == "assumed");
    CHECK(capability.at("capability").at("levels").at("execute").at("missing_level") ==
          "execute");
    CHECK_FALSE(capability.at("capability").at("levels").at("execute").at("why")
                    .get<std::string>().empty());
    const auto claimed = call(lane, "mfg_device_claim",
        {{"device_id", device_id},
         {"owner", {{"client_kind", "test"}, {"seat", "test-seat"}, {"session", "physical"}}}});
    const auto lease_id = claimed.at("lease").at("lease_id").get<std::string>();
    const auto refused = call(lane, "mfg_emulator_fault_inject",
        {{"device_id", device_id}, {"lease_id", lease_id}, {"fault", "alarm"}}, true);
    CHECK(refused.at("code") == "mfg_emulator_fault_physical_endpoint_forbidden");
    const auto start_refused = call(
        lane, "mfg_run_start",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"run_id", "run:physical"}, {"approval_id", "approval:physical"}},
        true);
    CHECK(start_refused.at("code") == "mfg_physical_execute_not_qualified");
}

TEST_CASE("approval identity assertions are refused and host identity is stamped") {
    LaneRig rig;
    rig.claim();
    rig.connect();
    const auto artifact_id = rig.registerArtifact();
    const auto preflight = rig.preflight(artifact_id);
    const std::string run_id = preflight.at("run_id");
    const auto proposed = call(
        rig.lane, "mfg_approval_propose",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"run_id", run_id}, {"exercisable_by", "human_only"}});
    const std::string proposal_id = proposed.at("proposal").at("proposal_id");
    const auto forged = call(
        rig.lane, "mfg_approval_create",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"proposal_id", proposal_id},
         {"approver_auth", {{"identity", "administrator"},
                            {"auth", "caller-asserted"}}}},
        true);
    CHECK(forged.at("code") == "mfg_approval_identity_assertion_forbidden");

    const auto created = call(
        rig.lane, "mfg_approval_create",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"proposal_id", proposal_id}});
    CHECK(created.at("approval").at("approver").at("identity") == "test-seat");
    CHECK(created.at("approval").at("approver").at("auth") ==
          "hostd_authenticated_connection");
}

TEST_CASE("heartbeat renews the authenticated lease and expiry fails the active run closed") {
    TempDirectory temp;
    std::uint64_t now_ns = 1'000'000'000ULL;
    ManufacturingLane lane(temp.path, false, [&] { return now_ns; });
    const std::string device_id =
        call(lane, "mfg_device_list").at("devices").at(0).at("device_id");
    const auto claimed = call(
        lane, "mfg_device_claim",
        {{"device_id", device_id}, {"ttl_ms", 1000ULL}});
    const std::string lease_id = claimed.at("lease").at("lease_id");
    CHECK(claimed.at("lease").at("heartbeat_ms") == 333);
    call(lane, "mfg_device_connect",
         {{"device_id", device_id}, {"lease_id", lease_id}});
    const auto artifact = call(
        lane, "mfg_artifact_register",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"artifact", {{"kind", "nc"},
                       {"lines", json::array({"G90", "G1 X1 F100", "G1 X2"})}}}});
    const auto preflight = call(
        lane, "mfg_preflight_run",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"artifact_id", artifact.at("artifact").at("artifact_id")}});
    const std::string run_id = preflight.at("run_id");
    const auto proposal = call(
        lane, "mfg_approval_propose",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"run_id", run_id}, {"exercisable_by", "human_only"}});
    const auto approval = call(
        lane, "mfg_approval_create",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"proposal_id", proposal.at("proposal").at("proposal_id")}});
    call(lane, "mfg_run_start",
         {{"device_id", device_id}, {"lease_id", lease_id},
          {"run_id", run_id},
          {"approval_id", approval.at("approval").at("approval_id")}});

    now_ns += 300'000'000ULL;
    call(lane, "mfg_device_heartbeat",
         {{"device_id", device_id}, {"lease_id", lease_id}});
    now_ns += 700'000'000ULL;
    const auto survived = call(
        lane, "mfg_device_heartbeat",
        {{"device_id", device_id}, {"lease_id", lease_id}});
    CHECK(survived.at("lease").at("lease_id") == lease_id);

    now_ns += 1'001'000'000ULL;
    const auto expired = call(
        lane, "mfg_device_heartbeat",
        {{"device_id", device_id}, {"lease_id", lease_id}}, true);
    CHECK(expired.at("code") == "lease.not_found_or_expired");
    const auto run = call(lane, "mfg_run_describe", {{"run_id", run_id}});
    CHECK(run.at("run").at("state") == "faulted");
    CHECK(run.at("run").at("discontinuity") == "disconnect");
    const auto device = call(lane, "mfg_device_describe",
                             {{"device_id", device_id}});
    CHECK_FALSE(device.at("transport").at("connected"));
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

TEST_CASE("emulator is the default candidate without commissioning") {
    TempDirectory temp;
    ManufacturingLane lane(temp.path);
    const auto listed = call(lane, "mfg_device_list");
    const auto emulator = findTransport(listed, "emulator");
    CHECK(emulator.at("allowlisted"));
    const auto claimed = call(
        lane, "mfg_device_claim",
        {{"device_id", emulator.at("device_id")},
         {"owner", {{"client_kind", "test"}, {"seat", "seat"}, {"session", "default"}}}});
    CHECK(claimed.at("transport") == "emulator");
}

TEST_CASE("commissioned claims select serial tcp and websocket transports") {
    const std::vector<std::pair<chudware::device::TransportKind, std::string>> transports{
        {chudware::device::TransportKind::Serial, "serial"},
        {chudware::device::TransportKind::Tcp, "tcp"},
        {chudware::device::TransportKind::WebSocket, "websocket"}};
    for (const auto& [kind, name] : transports) {
        CAPTURE(name);
        TempDirectory temp;
        seedAllowlist(temp.path, commissioned(
            kind, chudware::device::MachineClass::OpenFrameMill, name));
        ManufacturingLane lane(temp.path);
        const auto candidate = findTransport(call(lane, "mfg_device_list"), name);
        REQUIRE(candidate.at("allowlisted"));
        const auto claimed = call(
            lane, "mfg_device_claim",
            {{"device_id", candidate.at("device_id")},
             {"owner", {{"client_kind", "test"}, {"seat", "seat"}, {"session", name}}}});
        CHECK(claimed.at("transport") == name);
        const auto described = call(
            lane, "mfg_device_describe", {{"device_id", candidate.at("device_id")}});
        CHECK(described.at("transport").at("kind") == name);
        CHECK(described.at("transport").at("endpoint_is_physical"));
    }
}

TEST_CASE("unknown non allowlisted claim is refused by commissioning name") {
    TempDirectory temp;
    ManufacturingLane lane(temp.path);
    const auto refused = call(
        lane, "mfg_device_claim",
        {{"device_id", "dev:open_frame_mill:000000000000"},
         {"owner", {{"client_kind", "test"}, {"seat", "seat"}, {"session", "denied"}}}},
        true);
    CHECK(refused.at("code") == "mfg_device_not_allowlisted");
    CHECK(refused.at("message").get<std::string>().find("allowlisted") != std::string::npos);
}

TEST_CASE("observed controller fingerprint change falsifies its approval") {
    TempDirectory temp;
    auto entry = commissioned(chudware::device::TransportKind::Tcp,
                              chudware::device::MachineClass::FdmPrinterEnclosed,
                              "fingerprint");
    entry.connection["adapter_kind"] = "moonraker";
    entry.connection["origin"] = "virtual";
    seedAllowlist(temp.path, entry);
    ManufacturingLane lane(temp.path);
    std::string subject = "printer:controller-a";
    lane.setWorkerCall([&](const std::string& method, const json&) {
        if (method == "mfg_printer_adapter_probe")
            return json{{"ok", true},
                        {"report", {{"subject_id", subject},
                                    {"max_advertised", "execute"},
                                    {"levels", json::array()}}}};
        return json{{"ok", true}};
    });
    const auto candidate = findTransport(call(lane, "mfg_device_list"), "tcp");
    const std::string device_id = candidate.at("device_id");
    const auto claimed = call(lane, "mfg_device_claim",
                              {{"device_id", device_id}});
    const std::string lease_id = claimed.at("lease").at("lease_id");
    const auto first_connect = call(
        lane, "mfg_device_connect",
        {{"device_id", device_id}, {"lease_id", lease_id}});
    const std::string first_fingerprint = first_connect.at("fingerprint_sha256");
    const auto artifact = call(
        lane, "mfg_artifact_register",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"artifact", {{"kind", "print_job"}, {"content", "G28\n"},
                       {"slicer_profile_hash", "slicer:fingerprint"}}}});
    const auto preflight = call(
        lane, "mfg_preflight_run",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"artifact_id", artifact.at("artifact").at("artifact_id")}});
    const std::string run_id = preflight.at("run_id");
    const auto proposal = call(
        lane, "mfg_approval_propose",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"run_id", run_id}});
    const auto approval = call(
        lane, "mfg_approval_create",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"proposal_id", proposal.at("proposal").at("proposal_id")}});
    const std::string approval_id = approval.at("approval").at("approval_id");
    CHECK(approval.at("approval").at("machine").at("fingerprint_sha256") ==
          first_fingerprint);

    call(lane, "mfg_device_disconnect",
         {{"device_id", device_id}, {"lease_id", lease_id}});
    subject = "printer:controller-b";
    const auto second_connect = call(
        lane, "mfg_device_connect",
        {{"device_id", device_id}, {"lease_id", lease_id}});
    CHECK(second_connect.at("fingerprint_sha256") != first_fingerprint);
    const auto inspected = call(
        lane, "mfg_approval_inspect", {{"approval_id", approval_id}});
    CHECK(inspected.at("approval").at("status") == "revoked");
    const auto invalidations = call(
        lane, "mfg_result_invalidations", {{"affected", approval_id}});
    REQUIRE(invalidations.at("invalidations").size() == 1);
    CHECK(invalidations.at("invalidations").at(0).at("reason") ==
          "observed_machine_fingerprint_changed");
}

TEST_CASE("run artifact and approval cannot cross the selected device boundary") {
    TempDirectory temp;
    auto entry = commissioned(chudware::device::TransportKind::Tcp,
                              chudware::device::MachineClass::FdmPrinterEnclosed,
                              "cross-device");
    entry.connection["adapter_kind"] = "moonraker";
    entry.connection["origin"] = "virtual";
    seedAllowlist(temp.path, entry);
    ManufacturingLane lane(temp.path);
    lane.setWorkerCall([](const std::string& method, const json&) {
        if (method == "mfg_printer_adapter_probe")
            return json{{"ok", true},
                        {"report", {{"subject_id", "printer:device-b"},
                                    {"max_advertised", "execute"},
                                    {"levels", json::array()}}}};
        return json{{"ok", true}};
    });
    const auto listed = call(lane, "mfg_device_list");
    const std::string device_a = findTransport(listed, "emulator").at("device_id");
    const std::string device_b = findTransport(listed, "tcp").at("device_id");
    const auto claim_a = call(lane, "mfg_device_claim",
                              {{"device_id", device_a}});
    const std::string lease_a = claim_a.at("lease").at("lease_id");
    call(lane, "mfg_device_connect",
         {{"device_id", device_a}, {"lease_id", lease_a}});
    const auto artifact = call(
        lane, "mfg_artifact_register",
        {{"device_id", device_a}, {"lease_id", lease_a},
         {"artifact", {{"kind", "nc"}, {"content", "G0 X0\n"}}}});
    const auto preflight = call(
        lane, "mfg_preflight_run",
        {{"device_id", device_a}, {"lease_id", lease_a},
         {"artifact_id", artifact.at("artifact").at("artifact_id")}});
    const std::string run_id = preflight.at("run_id");
    const auto proposal = call(
        lane, "mfg_approval_propose",
        {{"device_id", device_a}, {"lease_id", lease_a},
         {"run_id", run_id}});
    const auto approval = call(
        lane, "mfg_approval_create",
        {{"device_id", device_a}, {"lease_id", lease_a},
         {"proposal_id", proposal.at("proposal").at("proposal_id")}});
    const std::string approval_id = approval.at("approval").at("approval_id");
    call(lane, "mfg_device_release",
         {{"device_id", device_a}, {"lease_id", lease_a}});

    const auto claim_b = call(lane, "mfg_device_claim",
                              {{"device_id", device_b}});
    const std::string lease_b = claim_b.at("lease").at("lease_id");
    call(lane, "mfg_device_connect",
         {{"device_id", device_b}, {"lease_id", lease_b}});
    const auto refused = call(
        lane, "mfg_run_start",
        {{"device_id", device_b}, {"lease_id", lease_b},
         {"run_id", run_id}, {"approval_id", approval_id}},
        true);
    CHECK(refused.at("code") == "mfg_run_device_mismatch");
}

TEST_CASE("allowlist epoch change closes and falsifies an active claim") {
    LaneRig rig;
    rig.claim();
    rig.connect();
    const auto before = call(rig.lane, "mfg_device_describe",
                             {{"device_id", rig.device_id}});
    const auto epoch_before = before.at("epoch").at("epoch").get<unsigned>();

    seedAllowlist(rig.temp.path, commissioned(
        chudware::device::TransportKind::Tcp,
        chudware::device::MachineClass::OpenFrameMill, "epoch"));
    const auto listed = call(rig.lane, "mfg_device_list");
    CHECK(listed.at("epoch").at("epoch").get<unsigned>() == epoch_before + 1);
    const auto telemetry = call(rig.lane, "mfg_telemetry_snapshot",
                                {{"device_id", rig.device_id}});
    CHECK(telemetry.at("snapshot").at("state") == "unknown");
    const auto stale_write = call(
        rig.lane, "mfg_artifact_register",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"artifact", {{"kind", "nc"}, {"content", "G0 X0\n"}}}},
        true);
    CHECK(stale_write.at("code") == "lease.commander_held_by_caller");
    const auto invalidations = call(
        rig.lane, "mfg_result_invalidations", {{"affected", rig.lease_id}});
    REQUIRE(invalidations.at("invalidations").size() == 1);
    CHECK(invalidations.at("invalidations").at(0).at("reason") ==
          "allowlist_epoch_changed");
}

TEST_CASE("printer adapters route through the injected worker after arbitration") {
    TempDirectory temp;
    auto entry = commissioned(chudware::device::TransportKind::Tcp,
                              chudware::device::MachineClass::FdmPrinterEnclosed,
                              "printer");
    entry.connection["adapter_kind"] = "moonraker";
    entry.connection["auth_mode"] = "trusted_host";
    entry.connection["origin"] = "virtual";
    seedAllowlist(temp.path, entry);
    ManufacturingLane lane(temp.path);
    std::vector<std::string> methods;
    std::map<std::string, json, std::less<>> arguments;
    lane.setWorkerCall([&](const std::string& method, const json& params) {
        methods.push_back(method);
        arguments[method] = params;
        if (method == "mfg_printer_adapter_probe")
            return json{{"ok", true},
                        {"report",
                         {{"subject_id", "worker-controlled"},
                          {"max_advertised", "execute"},
                          {"levels",
                           json::array(
                               {{{"level", "execute"}, {"truth", "verified"}},
                                {{"level", "safety_integration"},
                                 {"truth", "verified"}}})}}}};
        if (method == "mfg_printer_adapter_snapshot")
            return json{{"ok", true},
                        {"rows", json::array(
                             {{{"field", "job_state"}, {"v", "printing"}, {"seq", 999},
                               {"src", "simulated"}, {"approval_id", "worker-forged"}}})}};
        if (method == "mfg_printer_adapter_upload")
            return json{{"ok", true},
                        {"remote_artifact", {{"path", "remote/print.gcode"}}}};
        return json{{"ok", true}};
    });

    const auto candidate = findTransport(call(lane, "mfg_device_list"), "tcp");
    const std::string device_id = candidate.at("device_id");
    const auto claimed = call(
        lane, "mfg_device_claim",
        {{"device_id", device_id},
         {"owner", {{"client_kind", "test"}, {"seat", "seat"}, {"session", "printer"}}}});
    const std::string lease_id = claimed.at("lease").at("lease_id");
    call(lane, "mfg_device_connect",
         {{"device_id", device_id}, {"lease_id", lease_id}});
    CHECK(methods.at(0) == "mfg_printer_adapter_open");
    CHECK(methods.at(1) == "mfg_printer_adapter_probe");
    CHECK(arguments.at("mfg_printer_adapter_open").at("endpoint").at("base_url") ==
          arguments.at("mfg_printer_adapter_open").at("endpoint").at(
              "expected_base_url"));
    CHECK(arguments.at("mfg_printer_adapter_open").at("endpoint").at("base_url") ==
          "http://127.0.0.1:39007");
    const auto capability = call(lane, "mfg_device_capability",
                                 {{"device_id", device_id}});
    CHECK_FALSE(candidate.at("transport").at("endpoint_is_physical"));
    CHECK(capability.at("capability").at("max_advertised") == "execute");
    CHECK(capability.at("capability").at("levels").at("execute").at("truth") ==
          "verified");

    const auto telemetry = call(lane, "mfg_telemetry_snapshot",
                                {{"device_id", device_id}});
    CHECK(methods.back() == "mfg_printer_adapter_snapshot");
    const auto& row = telemetry.at("snapshot").at("v").at("rows").at(0);
    CHECK(row.at("seq") == telemetry.at("snapshot").at("seq"));
    CHECK(row.at("seq") != 999);
    CHECK_FALSE(row.contains("approval_id"));

    const auto artifact = call(
        lane, "mfg_artifact_register",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"artifact", {{"kind", "print_job"}, {"lines", json::array({"G28", "G1 X1"})},
                       {"slicer_profile_hash", "slicer:test"}}}});
    const std::string artifact_id = artifact.at("artifact").at("artifact_id");
    const auto preflight = call(
        lane, "mfg_preflight_run",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"artifact_id", artifact_id},
         {"job", {{"job_id", "job:printer"}, {"setup_revision", 1}, {"quantity", 1}}}});
    const std::string run_id = preflight.at("run_id");
    const auto proposal = call(
        lane, "mfg_approval_propose",
        {{"device_id", device_id}, {"lease_id", lease_id}, {"run_id", run_id},
         {"exercisable_by", "human_only"}, {"expiry_ttl_ms", 60000},
         {"attendance", {{"mode", "physical_attended"}, {"attested_by", "operator"}}}});
    const auto approval = call(
        lane, "mfg_approval_create",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"proposal_id", proposal.at("proposal").at("proposal_id")}});
    const std::string approval_id = approval.at("approval").at("approval_id");

    const auto refused = call(
        lane, "mfg_run_start",
        {{"device_id", device_id}, {"lease_id", lease_id}, {"run_id", run_id},
         {"approval_id", approval_id},
         {"initiator", {{"kind", "agent"}, {"identity", "planner"},
                        {"seat", "agent-seat"}, {"agent_run_id", "agent:test"}}}},
        true);
    CHECK(refused.at("code") == "mfg_initiator_identity_assertion_forbidden");
    CHECK(std::find(methods.begin(), methods.end(), "mfg_printer_adapter_start") ==
          methods.end());

    const auto started = call(
        lane, "mfg_run_start",
        {{"device_id", device_id}, {"lease_id", lease_id}, {"run_id", run_id},
         {"approval_id", approval_id}});
    CHECK(started.at("state") == "running");
    REQUIRE(arguments.count("mfg_printer_adapter_start") == 1);
    const auto& token =
        arguments.at("mfg_printer_adapter_start").at("authorization_token");
    CHECK(token.is_string());
    CHECK(token.get<std::string>().rfind("run-token:", 0) == 0);
    CHECK_FALSE(arguments.at("mfg_printer_adapter_start").contains("approval_id"));
    CHECK_FALSE(arguments.at("mfg_printer_adapter_start").contains("lease_id"));

    const auto held = call(
        lane, "mfg_run_hold",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"run_id", run_id}, {"kind", "feed_hold"}});
    CHECK(held.at("state") == "held");
    CHECK(methods.back() == "mfg_printer_adapter_pause");
    const auto aborted = call(
        lane, "mfg_run_abort",
        {{"device_id", device_id}, {"lease_id", lease_id}, {"run_id", run_id}});
    CHECK(aborted.at("state") == "aborting");
    CHECK(methods.back() == "mfg_printer_adapter_cancel");
    call(lane, "mfg_device_disconnect",
         {{"device_id", device_id}, {"lease_id", lease_id}});
    CHECK(methods.back() == "mfg_printer_adapter_close");
}

TEST_CASE("printer terminal telemetry faults the run and raises attention") {
    TempDirectory temp;
    auto entry = commissioned(chudware::device::TransportKind::Tcp,
                              chudware::device::MachineClass::FdmPrinterEnclosed,
                              "terminal-printer");
    entry.connection["adapter_kind"] = "moonraker";
    entry.connection["auth_mode"] = "trusted_host";
    entry.connection["origin"] = "virtual";
    seedAllowlist(temp.path, entry);
    ManufacturingLane lane(temp.path);
    bool terminal_snapshot = false;
    lane.setWorkerCall([&](const std::string& method, const json&) {
        if (method == "mfg_printer_adapter_probe")
            return json{{"ok", true},
                        {"report",
                         {{"subject_id", "printer:real-shape"},
                          {"max_advertised", "transport"},
                          {"levels",
                           json::array(
                               {{{"level", "monitor"}, {"truth", "verified"}},
                                {{"level", "transport"}, {"truth", "verified"}},
                                {{"level", "execute"}, {"truth", "verified"}}})}}}};
        if (method == "mfg_printer_adapter_snapshot")
            return json{
                {"ok", true},
                {"rows",
                 json::array(
                     {{{"field", "device_state"},
                       {"v", terminal_snapshot ? "alarm" : "idle"},
                       {"state", "ok"}, {"t_mono_ns", 123}, {"seq", 999},
                       {"age_ms", 0}, {"ttl_ms", 5000}, {"src", "simulated"},
                       {"why", nullptr}, {"last_ok", nullptr},
                       {"raw", json::object()}}})}};
        if (method == "mfg_printer_adapter_upload")
            return json{{"ok", true},
                        {"remote_artifact", {{"path", "remote/terminal.gcode"}}}};
        return json{{"ok", true}};
    });
    const auto candidate = findTransport(call(lane, "mfg_device_list"), "tcp");
    const std::string device_id = candidate.at("device_id");
    const auto claimed = call(
        lane, "mfg_device_claim",
        {{"device_id", device_id},
         {"owner", {{"client_kind", "test"}, {"seat", "seat"},
                    {"session", "terminal-printer"}}}});
    const std::string lease_id = claimed.at("lease").at("lease_id");
    call(lane, "mfg_device_connect",
         {{"device_id", device_id}, {"lease_id", lease_id}});
    const auto run = startPrinterRun(lane, device_id, lease_id);

    std::vector<json> events;
    lane.setEventSink([&](const std::string& line) {
        events.push_back(json::parse(line));
    });
    terminal_snapshot = true;
    const auto telemetry = call(
        lane, "mfg_telemetry_snapshot", {{"device_id", device_id}});
    const auto& row = telemetry.at("snapshot").at("v").at("rows").at(0);
    CHECK(telemetry.at("snapshot").at("src") == "simulated");
    CHECK(row.at("seq") == telemetry.at("snapshot").at("seq"));
    CHECK(row.at("seq") != 999);
    const auto described =
        call(lane, "mfg_run_describe", {{"run_id", run.run_id}});
    CHECK(described.at("run").at("state") == "faulted");
    CHECK(described.at("run").at("discontinuity") == "alarm");
    const auto attention = call(lane, "mfg_attention_list");
    REQUIRE(attention.at("attention").size() == 1);
    CHECK(attention.at("attention").at(0).at("reason") ==
          "printer_terminal_fault");
    CHECK(std::any_of(events.begin(), events.end(), [](const json& event) {
        return event.at("event") == "mfg.attention" &&
               event.at("data").at("action") == "raised";
    }));
    const auto resume = call(
        lane, "mfg_run_resume",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"run_id", run.run_id}},
        true);
    CHECK(resume.at("code") == "no_alarm_since_hold");
    const auto hold = call(
        lane, "mfg_run_hold",
        {{"device_id", device_id}, {"lease_id", lease_id},
         {"run_id", run.run_id}, {"kind", "feed_hold"}},
        true);
    CHECK(hold.at("code") == "run.terminal");
    const auto reconnect = call(
        lane, "mfg_device_connect",
        {{"device_id", device_id}, {"lease_id", lease_id}}, true);
    CHECK(reconnect.at("code") == "mfg_transport_terminal");
}

TEST_CASE("printer connect preserves worker origin mismatch refusal") {
    TempDirectory temp;
    auto entry = commissioned(chudware::device::TransportKind::Tcp,
                              chudware::device::MachineClass::FdmPrinterOpen,
                              "origin-mismatch");
    entry.connection["adapter_kind"] = "moonraker";
    entry.connection["auth_mode"] = "trusted_host";
    seedAllowlist(temp.path, entry);
    ManufacturingLane lane(temp.path);
    lane.setWorkerCall([](const std::string& method, const json& params) {
        if (method == "mfg_printer_adapter_open") {
            CHECK(params.at("endpoint").at("base_url") ==
                  params.at("endpoint").at("expected_base_url"));
            return json{{"ok", false}, {"error", "OriginMismatch"},
                        {"message",
                         "base_url does not match the lane-supplied expected endpoint"}};
        }
        return json{{"ok", true}};
    });
    const auto candidate = findTransport(call(lane, "mfg_device_list"), "tcp");
    const auto claimed =
        call(lane, "mfg_device_claim", {{"device_id", candidate.at("device_id")}});
    const auto refused = call(
        lane, "mfg_device_connect",
        {{"device_id", candidate.at("device_id")},
         {"lease_id", claimed.at("lease").at("lease_id")}},
        true);
    CHECK(refused.at("code") == "mfg_printer_worker_refused");
    CHECK(refused.at("detail").at("method") == "mfg_printer_adapter_open");
    CHECK(refused.at("detail").at("why").get<std::string>().find(
              "expected endpoint") != std::string::npos);
}

TEST_CASE("printer connect names the missing worker method") {
    TempDirectory temp;
    seedAllowlist(temp.path, commissioned(
        chudware::device::TransportKind::Tcp,
        chudware::device::MachineClass::FdmPrinterOpen, "missing-worker"));
    ManufacturingLane lane(temp.path);
    const auto candidate = findTransport(call(lane, "mfg_device_list"), "tcp");
    const auto claimed = call(
        lane, "mfg_device_claim",
        {{"device_id", candidate.at("device_id")},
         {"owner", {{"client_kind", "test"}, {"seat", "seat"}, {"session", "printer"}}}});
    const auto refused = call(
        lane, "mfg_device_connect",
        {{"device_id", candidate.at("device_id")},
         {"lease_id", claimed.at("lease").at("lease_id")}},
        true);
    CHECK(refused.at("code") == "mfg_printer_worker_unavailable");
    CHECK(refused.at("detail").at("method") == "mfg_printer_adapter_open");
}

TEST_CASE("attention mutations enforce text and delivery history bounds") {
    LaneRig rig;
    rig.claim();
    const auto oversized = call(
        rig.lane, "mfg_attention_raise",
        {{"id", "attention:oversized"}, {"reason", std::string(129, 'r')},
         {"why", "bounded"}}, true);
    CHECK(oversized.at("code") == "mfg_attention_invalid");
    call(rig.lane, "mfg_attention_raise",
         {{"id", "attention:bounded"}, {"reason", "delivery_test"},
          {"why", "exercise the per-item delivery bound"}});
    for (int attempt = 1; attempt <= 64; ++attempt) {
        call(rig.lane, "mfg_attention_delivery",
             {{"id", "attention:bounded"},
              {"delivery", {{"channel", "desktop"}, {"attempt", attempt},
                            {"status", "delivered"}}}});
    }
    const auto overflow = call(
        rig.lane, "mfg_attention_delivery",
        {{"id", "attention:bounded"},
         {"delivery", {{"channel", "desktop"}, {"attempt", 65},
                       {"status", "delivered"}}}},
        true);
    CHECK(overflow.at("code") == "mfg_attention_delivery_capacity");
}
TEST_CASE("attention lifecycle is durable and published on the lane stream") {

    TempDirectory temp;
    ManufacturingLane lane(temp.path);
    const auto listed_devices = call(lane, "mfg_device_list");
    const auto device_id =
        listed_devices.at("devices").at(0).at("device_id").get<std::string>();
    call(lane, "mfg_device_claim", {{"device_id", device_id}});
    std::vector<json> events;
    lane.setEventSink([&](const std::string& line) {
        events.push_back(json::parse(line));
    });
    const auto raised = call(
        lane, "mfg_attention_raise",
        {{"id", "attention:test"}, {"severity", "critical"}, {"device", "dev:test"},
         {"run", "run:test"}, {"reason", "operator_required"},
         {"why", "clear the build plate"}});
    CHECK(raised.at("attention").at("raised_seq").get<std::uint64_t>() > 0);
    const auto delivered = call(
        lane, "mfg_attention_delivery",
        {{"id", "attention:test"},
         {"delivery", {{"channel", "desktop"}, {"attempt", 1},
                       {"status", "delivered"}, {"seq", 999}}}});
    REQUIRE(delivered.at("attention").at("deliveries").size() == 1);
    CHECK(delivered.at("attention").at("deliveries").at(0).at("seq") != 999);
    const auto acknowledged = call(
        lane, "mfg_attention_ack", {{"id", "attention:test"}, {"by", "operator"}});
    CHECK(acknowledged.at("attention").at("acknowledged_by") == "test-seat");
    const auto resolved = call(
        lane, "mfg_attention_resolve", {{"id", "attention:test"}});
    CHECK(resolved.at("attention").at("resolved_seq").get<std::uint64_t>() >
          acknowledged.at("attention").at("acknowledged_seq").get<std::uint64_t>());
    const auto listed = call(lane, "mfg_attention_list");
    REQUIRE(listed.at("attention").size() == 1);
    CHECK(listed.at("attention").at(0).at("id") == "attention:test");
    const auto journal = call(
        lane, "mfg_journal_query", {{"kinds", json::array({"mfg.attention"})}});
    REQUIRE(journal.at("records").size() == 4);
    CHECK(journal.at("records").at(0).at("body").at("action") == "raised");
    CHECK(journal.at("records").at(1).at("body").at("action") == "delivery");
    CHECK(journal.at("records").at(1).at("body").at("delivery").at("channel") ==
          "desktop");
    CHECK(journal.at("records").at(2).at("body").at("action") == "acknowledged");
    CHECK(journal.at("records").at(3).at("body").at("action") == "resolved");
    REQUIRE(events.size() == 4);
    CHECK(events.at(0).at("event") == "mfg.attention");
    CHECK(events.at(0).at("data").at("action") == "raised");
    CHECK(events.at(1).at("data").at("action") == "delivery");
    CHECK(events.at(2).at("data").at("action") == "acknowledged");
    CHECK(events.at(3).at("data").at("action") == "resolved");
}
