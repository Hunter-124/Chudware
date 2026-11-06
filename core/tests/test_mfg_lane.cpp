#define DOCTEST_CONFIG_IMPLEMENT
#include <doctest/doctest.h>

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
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
            "/tmp/chudware-test-serial" + suffix, "1a86", "7523", "test-controller");
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
        if (row.at("transport").at("kind").get_ref<const std::string&>() == kind) return row;
    FAIL("transport candidate not found: " << kind);
    return nullptr;
}

json call(ManufacturingLane& lane, std::string_view method, json params = json::object(),
          bool expect_error = false,
          chudware::mfg::LeaseOwner caller =
              {"human", "test-seat", "test-session"}) {
    const auto handled = lane.handle(method, params, caller);
    REQUIRE(handled.has_value());
    INFO("method=" << method << " body=" << handled->body.dump());
    CHECK(handled->is_error == expect_error);
    return handled->body;
}

struct LaneRig {
    TempDirectory temp;
    ManufacturingLane lane;
    std::string device_id;
    std::string lease_id;

    explicit LaneRig(chudware::mfg::ArtifactLimits limits = {},
                     std::function<uint64_t()> clock = {})
        : lane(temp.path, false, std::move(clock), limits) {
        const auto list = call(lane, "mfg_device_list");
        device_id = list.at("devices").at(0).at("device_id").get<std::string>();
    }

    void claim(std::string session = "one") {
        const auto claimed = call(lane, "mfg_device_claim",
            {{"device_id", device_id},
             {"owner", {{"client_kind", "human"}, {"seat", "test-seat"},
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

// Python drives only this test executable, consumes the lane's actual worker
// requests with the real printer handlers, and returns their real API results.
int printerWorkerFixture() {
    std::string line;
    if (!std::getline(std::cin, line)) throw std::runtime_error("missing fixture endpoint");
    const auto input = json::parse(line);
    const std::string host = input.at("host").get<std::string>();
    if (host != "127.0.0.1" && host != "printer.fixture.invalid")
        throw std::runtime_error("fixture requires a disposable loopback endpoint");
    const auto port = input.at("port").get<unsigned>();
    if (port == 0 || port > 65535) throw std::runtime_error("invalid fixture port");
    TempDirectory temp;
    auto entry = commissioned(chudware::device::TransportKind::Tcp,
                              chudware::device::MachineClass::FdmPrinterEnclosed);
    entry.endpoint = chudware::mfg::gateway::EndpointIdentity::network(
        chudware::device::TransportKind::Tcp, host, static_cast<std::uint16_t>(port));
    entry.connection["adapter_kind"] = "moonraker";
    entry.connection["auth_mode"] = "trusted_host";
    if (input.contains("address_class"))
        entry.connection["address_class"] = input.at("address_class").get<std::string>();
    {
        chudware::mfg::gateway::DeviceAllowlist allowlist(temp.path / "device_allowlist.tsv");
        std::string why;
        if (!allowlist.upsert(entry, &why)) throw std::runtime_error(why);
    }
    ManufacturingLane lane(temp.path);
    lane.setWorkerCall([](const std::string& method, const json& params) {
        std::cout << json{{"worker_method", method}, {"params", params}}.dump() << std::endl;
        std::string response;
        if (!std::getline(std::cin, response))
            throw std::runtime_error("printer fixture consumer disconnected");
        return json::parse(response);
    });
    const chudware::mfg::LeaseOwner owner{"human", "fixture-seat", "fixture-session"};
    const auto dispatch = [&](std::string_view method, const json& params) {
        auto result = lane.handle(method, params, owner);
        if (!result) throw std::runtime_error("fixture method was not handled");
        std::cout << json{{"lane_method", method}, {"is_error", result->is_error},
                          {"body", result->body}}.dump() << std::endl;
        return std::move(*result);
    };
    const auto listed = dispatch("mfg_device_list", json::object());
    std::string device_id;
    for (const auto& candidate : listed.body.at("devices")) {
        if (candidate.at("transport").at("kind") == "tcp")
            device_id = candidate.at("device_id").get<std::string>();
    }
    if (device_id.empty()) throw std::runtime_error("commissioned fixture printer was not listed");
    const auto claimed = dispatch("mfg_device_claim", {{"device_id", device_id}});
    if (claimed.is_error) throw std::runtime_error("fixture printer claim failed");
    const json leased{{"device_id", device_id},
                      {"lease_id", claimed.body.at("lease").at("lease_id")}};
    const auto connected = dispatch("mfg_device_connect", leased);
    if (!connected.is_error) {
        dispatch("mfg_telemetry_snapshot", {{"device_id", device_id}});
        dispatch("mfg_telemetry_snapshot", {{"device_id", device_id}});
        dispatch("mfg_device_disconnect", leased);
    }
    dispatch("mfg_device_release", leased);
    return 0;
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

TEST_CASE("tupleless allowlist migration names the invalid line and remains fail closed") {
    TempDirectory temp;
    ManufacturingLane lane(temp.path);
    const auto file = temp.path / "device_allowlist.tsv";
    const auto network = commissioned(chudware::device::TransportKind::Tcp,
                                     chudware::device::MachineClass::OpenFrameMill);
    const auto writeAllowlist = [&](std::string_view serial) {
        std::ofstream output(file);
        REQUIRE(output.is_open());
        output << "# disposable migration fixture\n" << serial << "\n"
               << chudware::mfg::gateway::DeviceAllowlist::formatLine(network) << "\n";
        output.close();
        REQUIRE_FALSE(output.fail());
    };
    writeAllowlist("serial\taddress=/dev/serial/by-id/legacy-fixture"
                   "\tmachine_class=open_frame_mill\tbaud=115200\tnote=obsolete fixture");
    const std::string diagnosis = file.string() +
        ":2: serial USB identity requires vid, pid, and serial together";
    TempDirectory startup;
    const auto startup_file = startup.path / "device_allowlist.tsv";
    std::filesystem::copy_file(file, startup_file);
    const std::string startup_diagnosis = "unable to load device allowlist: " +
        startup_file.string() + ":2: serial USB identity requires vid, pid, and serial together";
    CHECK_THROWS_WITH(ManufacturingLane(startup.path), startup_diagnosis.c_str());
    for (const auto method : {"mfg_device_list", "mfg_device_describe", "mfg_device_claim"}) {
        const auto refused = call(lane, method, json::object(), true);
        CHECK(refused.at("code") == "mfg_device_allowlist_invalid");
        CHECK(refused.at("message") == diagnosis);
    }
    // Commissioning uses a complete observed tuple, never infers one from the
    // legacy path. Only this disposable fixture supplies synthetic identity data.
    const auto serial = commissioned(chudware::device::TransportKind::Serial,
                                    chudware::device::MachineClass::OpenFrameMill);
    writeAllowlist(chudware::mfg::gateway::DeviceAllowlist::formatLine(serial));
    const auto listed = call(lane, "mfg_device_list");
    CHECK(findTransport(listed, "serial").at("machine_class") == "open_frame_mill");
    CHECK(findTransport(listed, "tcp").at("machine_class") == "open_frame_mill");
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


TEST_CASE("client-supplied GUI label cannot create human-only approval") {
    LaneRig rig;
    const chudware::mfg::LeaseOwner spoofed_gui{"gui", "uid:1000", "raw-socket"};
    const auto claimed = call(rig.lane, "mfg_device_claim",
                              {{"device_id", rig.device_id}}, false, spoofed_gui);
    rig.lease_id = claimed.at("lease").at("lease_id");
    call(rig.lane, "mfg_device_connect",
         {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}}, false, spoofed_gui);
    const auto artifact = call(
        rig.lane, "mfg_artifact_register",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"artifact", {{"kind", "nc"}, {"content", "G0 X0\n"}}}}, false, spoofed_gui);
    const auto preflight = call(
        rig.lane, "mfg_preflight_run",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"artifact_id", artifact.at("artifact").at("artifact_id")}}, false, spoofed_gui);
    const auto proposed = call(
        rig.lane, "mfg_approval_propose",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"run_id", preflight.at("run_id")}, {"exercisable_by", "human_only"}},
        false, spoofed_gui);
    const auto refused = call(
        rig.lane, "mfg_approval_create",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"proposal_id", proposed.at("proposal").at("proposal_id")}}, true, spoofed_gui);
    CHECK(refused.at("code") == "mfg_approval_human_auth_required");
}

TEST_CASE("lane rejects unbounded lease TTL before granting ownership") {
    LaneRig rig;
    const auto refused = call(rig.lane, "mfg_device_claim",
                              {{"device_id", rig.device_id}, {"ttl_ms", UINT64_MAX}}, true);
    CHECK(refused.at("code") == "lease.invalid_ttl");
    const auto bounded = call(rig.lane, "mfg_device_claim",
                              {{"device_id", rig.device_id},
                               {"ttl_ms", chudware::mfg::CommandLease::max_ttl_ms}});
    CHECK(bounded.at("lease").at("ttl_ms") == chudware::mfg::CommandLease::max_ttl_ms);
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

TEST_CASE("artifact admission bounds bytes and physical lines before retaining a program") {
    chudware::mfg::ArtifactLimits limits;
    limits.max_artifact_bytes = 8;
    limits.max_artifact_lines = 3;
    LaneRig rig(limits);
    rig.claim();
    const auto registerInput = [&](json artifact, bool refused = false) {
        artifact["kind"] = "nc";
        return call(rig.lane, "mfg_artifact_register",
                    {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
                     {"artifact", std::move(artifact)}}, refused);
    };
    CHECK(registerInput({{"content", std::string(8, 'x')}}).at("artifact").at("bytes") == 8);
    CHECK(registerInput({{"content", std::string(9, 'x')}}, true).at("code") ==
          "mfg_artifact_byte_limit");
    CHECK(registerInput({{"lines", json::array({std::string(7, 'x')})}})
              .at("artifact").at("bytes") == 8);
    CHECK(registerInput({{"lines", json::array({std::string(8, 'x')})}}, true).at("code") ==
          "mfg_artifact_byte_limit");
    CHECK(registerInput({{"lines", json::array({"", "", ""})}})
              .at("artifact").at("program_lines") == 3);
    CHECK(registerInput({{"lines", json::array({"", "", "", ""})}}, true).at("code") ==
          "mfg_artifact_line_limit");
    CHECK(registerInput({{"content", "\n\n\n\n"}}, true).at("code") ==
          "mfg_artifact_line_limit");
    const auto text = registerInput({{"content", "G90\n"}});
    const auto array = registerInput({{"lines", json::array({"G90"})}});
    CHECK(text.at("artifact").at("hash_sha256") == array.at("artifact").at("hash_sha256"));
    CHECK(registerInput({{"content", "\r\nG90\r\n"}}).at("artifact").at("program_lines") == 1);
    CHECK(registerInput({{"content", "G90"}, {"post_profile_id", std::string(4097, 'x')}},
                        true).at("code") == "mfg_artifact_metadata_limit");
}

TEST_CASE("repeated small artifact registrations stop at the cumulative object quota") {
    chudware::mfg::ArtifactLimits limits;
    limits.max_objects = 3;
    LaneRig rig(limits);
    rig.claim();
    for (size_t index = 0; index < limits.max_objects; ++index) rig.registerArtifact();
    for (int attempt = 0; attempt < 10; ++attempt) {
        const auto refused = call(
            rig.lane, "mfg_artifact_register",
            {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
             {"artifact", {{"content", "G90\n"}}}}, true);
        CHECK(refused.at("code") == "mfg_artifact_quota_exceeded");
        CHECK(refused.at("detail").at("retained_objects") == limits.max_objects);
    }
    call(rig.lane, "mfg_device_release",
         {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}});
    rig.claim();
    for (size_t index = 0; index < limits.max_objects; ++index) rig.registerArtifact();
}

TEST_CASE("cumulative artifact byte quota includes storage and is reclaimed on lease release") {
    chudware::mfg::ArtifactLimits limits;
    limits.max_objects = 64;
    limits.max_retained_bytes = 2048;
    limits.max_artifact_bytes = 128;
    LaneRig rig(limits);
    rig.claim();
    const json payload{{"kind", "nc"}, {"content", std::string(128, 'x')}};
    size_t admitted = 0;
    json refusal;
    for (int attempt = 0; attempt < 8; ++attempt) {
        const auto result = rig.lane.handle(
            "mfg_artifact_register",
            {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}, {"artifact", payload}},
            {"human", "test-seat", "test-session"});
        REQUIRE(result.has_value());
        if (result->is_error) {
            refusal = result->body;
            break;
        }
        ++admitted;
    }
    REQUIRE_FALSE(refusal.is_null());
    CHECK(admitted > 0);
    CHECK(admitted < limits.max_objects);
    CHECK(refusal.at("code") == "mfg_artifact_quota_exceeded");
    const auto& detail = refusal.at("detail");
    CHECK(detail.at("retained_objects") == admitted);
    CHECK(detail.at("retained_bytes").get<uint64_t>() +
          detail.at("requested_bytes").get<uint64_t>() > limits.max_retained_bytes);
    call(rig.lane, "mfg_device_release",
         {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}});
    rig.claim();
    for (size_t index = 0; index < admitted; ++index)
        call(rig.lane, "mfg_artifact_register",
             {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}, {"artifact", payload}});
    CHECK(call(rig.lane, "mfg_artifact_register",
               {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}, {"artifact", payload}},
               true).at("code") == "mfg_artifact_quota_exceeded");
}

TEST_CASE("device release and disconnect reclaim only artifacts with no run references") {
    chudware::mfg::ArtifactLimits limits;
    limits.max_objects = 2;
    LaneRig rig(limits);
    rig.claim();
    rig.connect();
    const auto linked = rig.registerArtifact();
    const auto unreferenced = rig.registerArtifact();
    const auto preflight = rig.preflight(linked);
    const std::string run_id = preflight.at("run_id");
    const auto before = call(rig.lane, "mfg_run_describe", {{"run_id", run_id}});
    SUBCASE("commander release") {
        call(rig.lane, "mfg_device_release",
             {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}});
        rig.claim();
    }
    SUBCASE("transport disconnect") {
        call(rig.lane, "mfg_device_disconnect",
             {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}});
    }
    rig.connect();
    const auto missing = call(
        rig.lane, "mfg_preflight_run",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"artifact_id", unreferenced}}, true);
    CHECK(missing.at("code") == "mfg_artifact_not_found");
    CHECK(rig.preflight(linked, run_id).at("run_id") == run_id);
    const auto after = call(rig.lane, "mfg_run_describe", {{"run_id", run_id}});
    CHECK(after.at("run").at("artifact_hash") == before.at("run").at("artifact_hash"));
    rig.registerArtifact();
    CHECK(call(rig.lane, "mfg_artifact_register",
               {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
                {"artifact", {{"content", "G90\n"}}}}, true).at("code") ==
          "mfg_artifact_quota_exceeded");
}

TEST_CASE("expired commander reclaims unused artifacts without losing active run evidence") {
    uint64_t now_ns = 1'000'000'000ULL;
    chudware::mfg::ArtifactLimits limits;
    limits.max_objects = 2;
    LaneRig rig(limits, [&] { return now_ns; });
    rig.claim();
    rig.connect();
    const auto linked = rig.registerArtifact();
    const auto unreferenced = rig.registerArtifact();
    const std::string run_id = rig.preflight(linked).at("run_id");
    const auto approval_id = rig.approval(run_id, "human_only");
    CHECK(rig.start(run_id, approval_id, "human").at("state") == "running");
    now_ns += 16'000'000'000ULL;
    CHECK(call(rig.lane, "mfg_device_heartbeat",
               {{"device_id", rig.device_id}, {"lease_id", rig.lease_id}}, true).at("code") ==
          "lease.not_found_or_expired");
    const auto faulted = call(rig.lane, "mfg_run_describe", {{"run_id", run_id}});
    CHECK(faulted.at("run").at("state") == "faulted");
    CHECK(faulted.at("run").at("artifact_id") == linked);
    CHECK(faulted.at("run").at("artifact_hash").get<std::string>().size() == 64);
    rig.claim();
    rig.connect();
    CHECK(call(rig.lane, "mfg_preflight_run",
               {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
                {"artifact_id", unreferenced}}, true).at("code") == "mfg_artifact_not_found");
    rig.registerArtifact();
    CHECK(call(rig.lane, "mfg_artifact_register",
               {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
                {"artifact", {{"content", "G90\n"}}}}, true).at("code") ==
          "mfg_artifact_quota_exceeded");
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
    CHECK(replay.at("code") == "mfg_run_authorization_refresh_refused");
}

TEST_CASE("post-burn stream refusal faults the run and requires operator attention") {
    LaneRig rig;
    rig.claim();
    rig.connect();
    const auto artifact = call(
        rig.lane, "mfg_artifact_register",
        {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
         {"artifact", {{"kind", "nc"},
                       {"lines", json::array({"G1 X" + std::string(200, '9')})},
                       {"post_profile_id", "emulator"}}}});
    const std::string run_id = rig.preflight(artifact.at("artifact").at("artifact_id")).at("run_id");
    const std::string approval_id = rig.approval(run_id, "human_only");
    const auto refused = rig.start(run_id, approval_id, "human", true);
    CHECK(refused.at("code") == "mfg_program_start_refused_after_nonce_burn");
    CHECK(refused.at("detail").at("nonce_burned_at_seq").get<uint64_t>() > 0);
    CHECK(call(rig.lane, "mfg_run_describe", {{"run_id", run_id}})
              .at("run").at("state") == "faulted");
    CHECK(call(rig.lane, "mfg_approval_inspect", {{"approval_id", approval_id}})
              .at("approval").at("status") == "burned");
    const auto attention = call(rig.lane, "mfg_attention_list");
    REQUIRE(attention.at("attention").size() == 1);
    CHECK(attention.at("attention").at(0).at("reason") == "command_outcome_unknown");
    const auto transitions = call(rig.lane, "mfg_journal_query",
        {{"subject", run_id}, {"kinds", json::array({"mfg.run.state"})}});
    CHECK(std::any_of(transitions.at("records").begin(),
                      transitions.at("records").end(), [](const json& record) {
        return record.at("body").value("state", std::string{}) == "faulted" &&
               record.at("body").value("cause", std::string{}) == "command_outcome_unknown";
    }));
}

TEST_CASE("a commander cannot release while a run is active or holding") {
    LaneRig rig;
    rig.claim();
    rig.connect();
    const auto artifact_id = rig.registerArtifact();
    const std::string run_id = rig.preflight(artifact_id).at("run_id");
    const auto approval_id = rig.approval(run_id, "human_only");
    REQUIRE(rig.start(run_id, approval_id, "human").at("state") == "running");
    const json lease{{"device_id", rig.device_id}, {"lease_id", rig.lease_id}};
    CHECK(call(rig.lane, "mfg_device_release", lease, true).at("code") ==
          "mfg_device_release_active_run");
    CHECK(call(rig.lane, "mfg_run_hold",
          {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
           {"run_id", run_id}}).at("state") == "holding");
    const auto transitions = call(rig.lane, "mfg_journal_query",
        {{"subject", run_id}, {"kinds", json::array({"mfg.run.state"})}});
    CHECK(std::none_of(transitions.at("records").begin(),
                       transitions.at("records").end(), [](const json& record) {
        return record.at("body").value("state", std::string{}) == "held";
    }));
    CHECK(call(rig.lane, "mfg_device_release", lease, true).at("code") ==
          "mfg_device_release_active_run");
    CHECK(call(rig.lane, "mfg_run_abort",
          {{"device_id", rig.device_id}, {"lease_id", rig.lease_id},
           {"run_id", run_id}}).at("state") == "aborting");
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
    CHECK(held.at("state") == "holding");
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

TEST_CASE("allowlist rejects metadata that claims a physical transport is virtual") {
    TempDirectory temp;
    auto entry = commissioned(chudware::device::TransportKind::Serial,
                              chudware::device::MachineClass::OpenFrameMill,
                              "origin-virtual");
    entry.connection["origin"] = "virtual";
    chudware::mfg::gateway::DeviceAllowlist allowlist(
        temp.path / "device_allowlist.tsv");
    std::string why;
    CHECK_FALSE(allowlist.upsert(entry, &why));
    CHECK(why.find("physicality is server-attested") != std::string::npos);
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

TEST_CASE("physical printer worker cannot promote execute authority") {
    TempDirectory temp;
    auto entry = commissioned(chudware::device::TransportKind::Tcp,
                              chudware::device::MachineClass::FdmPrinterEnclosed,
                              "printer");
    entry.connection["adapter_kind"] = "moonraker";
    entry.connection["auth_mode"] = "trusted_host";
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
    CHECK_FALSE(arguments.at("mfg_printer_adapter_open").at("endpoint").contains("address_class"));
    const auto capability = call(lane, "mfg_device_capability",
                                 {{"device_id", device_id}});
    CHECK(candidate.at("transport").at("endpoint_is_physical"));
    CHECK(capability.at("capability").at("max_advertised") == "transport");
    CHECK(capability.at("capability").at("levels").at("execute").at("truth") ==
          "assumed");

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
         {"approval_id", approval_id}}, true);
    CHECK(refused.at("code") == "mfg_physical_execute_not_qualified");
    CHECK(std::find(methods.begin(), methods.end(), "mfg_printer_adapter_upload") ==
          methods.end());
    CHECK(std::find(methods.begin(), methods.end(), "mfg_printer_adapter_start") ==
          methods.end());
    const auto inspected = call(lane, "mfg_approval_inspect", {{"approval_id", approval_id}});
    CHECK(inspected.at("approval").at("status") == "active");
    call(lane, "mfg_device_disconnect",
         {{"device_id", device_id}, {"lease_id", lease_id}});
    CHECK(methods.back() == "mfg_printer_adapter_close");
}

TEST_CASE("printer terminal telemetry closes the claim and raises attention") {
    TempDirectory temp;
    auto entry = commissioned(chudware::device::TransportKind::Tcp,
                              chudware::device::MachineClass::FdmPrinterEnclosed,
                              "terminal-printer");
    entry.connection["adapter_kind"] = "moonraker";
    entry.connection["auth_mode"] = "trusted_host";
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
    const auto attention = call(lane, "mfg_attention_list");
    REQUIRE(attention.at("attention").size() == 1);
    CHECK(attention.at("attention").at(0).at("reason") ==
          "printer_terminal_fault");
    CHECK(std::any_of(events.begin(), events.end(), [](const json& event) {
        return event.at("event") == "mfg.attention" &&
               event.at("data").at("action") == "raised";
    }));
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
    call(rig.lane, "mfg_attention_resolve", {{"id", "attention:bounded"}});
    const auto reraised = call(rig.lane, "mfg_attention_raise",
        {{"id", "attention:bounded"}, {"reason", "delivery_test"}, {"why", "still inspect"}});
    CHECK(reraised.at("attention").at("occurrence_count") == 2);
    CHECK(reraised.at("attention").at("delivery_count") == 64);
    REQUIRE(reraised.at("attention").at("deliveries").size() == 64);
    const auto head = call(rig.lane, "mfg_journal_query").at("head_seq");
    const auto bytes = std::filesystem::file_size(rig.temp.path / "journal.jsonl");
    call(rig.lane, "mfg_attention_delivery",
         {{"id", "attention:bounded"},
          {"delivery", {{"channel", "desktop"}, {"attempt", 64}, {"status", "delivered"}}}});
    CHECK(call(rig.lane, "mfg_attention_delivery",
         {{"id", "attention:bounded"},
          {"delivery", {{"channel", "desktop"}, {"attempt", 65}, {"status", "delivered"}}}},
         true).at("code") == "mfg_attention_delivery_capacity");
    CHECK(call(rig.lane, "mfg_journal_query").at("head_seq") == head);
    CHECK(std::filesystem::file_size(rig.temp.path / "journal.jsonl") == bytes);
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
                       {"status", "delivered"}}}});
    REQUIRE(delivered.at("attention").at("deliveries").size() == 1);
    CHECK(delivered.at("attention").at("deliveries").at(0).at("seq").get<uint64_t>() > 0);
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

TEST_CASE("attention retries do not append and re-raise retains cumulative accounting") {
    TempDirectory temp;
    json final_item;
    {
        ManufacturingLane lane(temp.path);
        const auto device = call(lane, "mfg_device_list").at("devices").at(0).at("device_id");
        call(lane, "mfg_device_claim", {{"device_id", device}});
        const json raise_request{{"id", "attention:retry"}, {"reason", "fault"}, {"why", "inspect"}};
        call(lane, "mfg_attention_raise", raise_request);
        const json request{{"id", "attention:retry"},
                           {"delivery", {{"channel", "desktop"}, {"attempt", 1},
                                         {"status", "delivered"}}}};
        const auto delivery = call(lane, "mfg_attention_delivery", request);
        const auto ack = call(lane, "mfg_attention_ack", {{"id", "attention:retry"}});
        const auto resolved = call(lane, "mfg_attention_resolve", {{"id", "attention:retry"}});
        const auto before = call(lane, "mfg_journal_query");
        const auto file_bytes = std::filesystem::file_size(temp.path / "journal.jsonl");
        CHECK(call(lane, "mfg_attention_delivery", request).at("attention") ==
              resolved.at("attention"));
        CHECK(call(lane, "mfg_attention_ack", {{"id", "attention:retry"}})
                  .at("attention").at("acknowledged_seq") ==
              ack.at("attention").at("acknowledged_seq"));
        CHECK(call(lane, "mfg_attention_resolve", {{"id", "attention:retry"}})
                  .at("attention").at("resolved_seq") ==
              resolved.at("attention").at("resolved_seq"));
        CHECK(call(lane, "mfg_journal_query").at("head_seq") == before.at("head_seq"));
        CHECK(std::filesystem::file_size(temp.path / "journal.jsonl") == file_bytes);
        final_item = call(lane, "mfg_attention_raise", raise_request).at("attention");
        CHECK(final_item.at("occurrence_count") == 2);
        CHECK(final_item.at("delivery_count") == 1);
        CHECK(final_item.at("deliveries") == delivery.at("attention").at("deliveries"));
        CHECK_FALSE(final_item.contains("resolved_seq"));
        CHECK_FALSE(final_item.contains("acknowledged_seq"));
    }
    {
        ManufacturingLane reopened(temp.path);
        const auto items = call(reopened, "mfg_attention_list").at("attention");
        REQUIRE(items.size() == 1);
        CHECK(items.at(0) == final_item);
    }
}

TEST_CASE("attention delivery schema rejects hostile fields before journal writes") {
    LaneRig rig;
    rig.claim();
    call(rig.lane, "mfg_attention_raise",
         {{"id", "attention:schema"}, {"reason", "fault"}, {"why", "inspect"}});
    const auto head = call(rig.lane, "mfg_journal_query").at("head_seq");
    const auto bytes = std::filesystem::file_size(rig.temp.path / "journal.jsonl");
    for (const auto& hostile : std::vector<json>{
             {{"channel", "desktop"}, {"status", "delivered"}, {"unknown", json::array({1})}},
             {{"channel", "desktop"}, {"status", "delivered"}, {"seq", 999}},
             {{"channel", "desktop"}, {"status", "delivered"}, {"error", std::string(513, 'e')}},
             {{"channel", std::string(65, 'c')}, {"status", "delivered"}},
             {{"channel", "desktop"}, {"status", json::object()}},
             {{"channel", "desktop"}, {"status", "delivered"}, {"attempt", -1}},
             {{"channel", "desktop"}, {"status", "delivered"}, {"attempt", 1025}},
             json::array({1})}) {
        const auto refused = call(rig.lane, "mfg_attention_delivery",
            {{"id", "attention:schema"}, {"delivery", hostile}}, true);
        CHECK(refused.at("code") == "mfg_attention_delivery_invalid");
    }
    CHECK(call(rig.lane, "mfg_journal_query").at("head_seq") == head);
    CHECK(std::filesystem::file_size(rig.temp.path / "journal.jsonl") == bytes);
    CHECK(call(rig.lane, "mfg_journal_query", {{"limit", 0}}).at("records").empty());
    const auto page = call(rig.lane, "mfg_journal_query", {{"limit", 1}});
    REQUIRE(page.at("records").size() == 1);
    CHECK(page.at("has_more") == true);
    const auto next = call(rig.lane, "mfg_journal_query", {{"from_seq", page.at("next_seq")}});
    REQUIRE_FALSE(next.at("records").empty());
    CHECK(next.at("records").at(0).at("seq") > page.at("records").at(0).at("seq"));
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string_view(argv[1]) == "--printer-worker-fixture") {
        try {
            return printerWorkerFixture();
        } catch (const std::exception& error) {
            std::cerr << error.what() << '\n';
            return 2;
        }
    }
    doctest::Context context(argc, argv);
    return context.run();
}
