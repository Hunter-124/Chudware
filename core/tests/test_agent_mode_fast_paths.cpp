#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "api/libchudware.h"
#include "environment_test_support.h"

#include <chrono>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace {
json rpc(const char* method, const json& params = json::object()) {
    char* raw = nullptr;
    const std::string encoded = params.dump();
    const int rc = chudware_call(method, encoded.c_str(), &raw);
    const std::string response = raw ? raw : "null";
    chudware_free(raw);
    INFO("method=" << method << " rc=" << rc << " response=" << response);
    REQUIRE(rc == 0);
    REQUIRE(response != "null");
    return json::parse(response);
}

struct Environment {
    std::vector<std::pair<std::string, std::optional<std::string>>> old;
    void set(const char* key, const fs::path& path) {
        const char* value = std::getenv(key);
        old.emplace_back(key, value ? std::optional<std::string>(value) : std::nullopt);
        REQUIRE(chudware::test::setEnvironment(key, path.string().c_str()) == 0);
    }
    ~Environment() {
        for (auto it = old.rbegin(); it != old.rend(); ++it)
            chudware::test::setOrUnsetEnvironment(
                it->first.c_str(), it->second ? it->second->c_str() : nullptr);
    }
};

struct Fixture {
    fs::path root = fs::temp_directory_path() /
        ("chudware_agent_modes_" + std::to_string(
            std::chrono::steady_clock::now().time_since_epoch().count()));
    fs::path output = root / "output";
    Environment env;
    std::string id;
    std::vector<std::string> drawings;
    std::vector<std::string> programs;

    Fixture() {
        fs::create_directories(output);
        env.set("TMPDIR", output);
        env.set("TMP", output);
        env.set("TEMP", output);
        env.set(chudware::test::appDataEnvironmentVariable(), root / "data");
        id = rpc("doc_new", {{"kind", "part"}, {"name", "ModeProof"}}).at("doc_id");
        json dimensions = json::object();
        for (auto [name, value] : {std::pair{"length", 20}, {"width", 15}, {"height", 5}})
            dimensions[name] = {{"value", value}, {"expr", nullptr}};
        const json built = rpc("doc_apply_ops", {{"doc_id", id}, {"base_revision", -1},
            {"ops", json::array({{{"op", "add_feature"}, {"feature", {
                {"type", "box"}, {"name", "Block"}, {"params", dimensions}}}}})}});
        REQUIRE(built.value("volume", 0.0) > 0.0);
    }
    ~Fixture() {
        for (const auto& drawing : drawings) {
            char* raw = nullptr;
            const auto params = json{{"doc_id", drawing}}.dump();
            chudware_call("drawing_close", params.c_str(), &raw);
            chudware_free(raw);
        }
        for (const auto& program : programs) {
            char* raw = nullptr;
            const auto params = json{{"doc_id", program}}.dump();
            chudware_call("cam_close", params.c_str(), &raw);
            chudware_free(raw);
        }
        if (!id.empty()) {
            char* raw = nullptr;
            const auto params = json{{"doc_id", id}}.dump();
            chudware_call("doc_close", params.c_str(), &raw);
            chudware_free(raw);
        }
        std::error_code ec;
        fs::remove_all(root, ec);
    }
    std::map<std::string, std::string> files() const {
        std::map<std::string, std::string> result;
        for (const auto& entry : fs::recursive_directory_iterator(output)) {
            if (!entry.is_regular_file()) continue;
            std::ifstream stream(entry.path(), std::ios::binary);
            result.emplace(fs::relative(entry.path(), output).generic_string(),
                std::string(std::istreambuf_iterator<char>(stream), {}));
        }
        return result;
    }
    json run(const char* method, const char* request, json options,
             const json& model_ops = json::array()) {
        options["offline"] = true;
        options["advisor"] = false;
        options["workspace_dir"] = output.string();
        const json plan = {{"name", "ModeProof"}, {"summary", "Review the requested output"},
            {"parts", json::array({{{"name", "ModeProof"}, {"purpose", "Document the part"},
                {"dimensions", json::object()}, {"features", json::array()}}})}};
        const json intake = {{"object", "ModeProof"}, {"key_dims", json::object()},
            {"features", json::array({"Document the existing part"})},
            {"constraints", json::array()}};
        const json scripts = {
            {"intake", json::array({intake.dump()})},
            {"planner", json::array({plan.dump()})},
            {"modeler", json::array({model_ops.dump()})},
            {"reviewer", json::array({"{\"done\":true,\"issues\":[]}"})},
            {"memory", json::array({"{\"name\":\"ModeProof\",\"summary\":\"Approved part edit\","
                                   "\"decisions\":[],\"open_issues\":[]}"})}};
        // Native design uses replies:{role:[...]}; the existing Ask assist uses
        // by_role:{role:[...]}. Exercise each real consumer with its own descriptor.
        const json provider = {{"name", "mock"}, {"replies", scripts}, {"by_role", {
            {"ask", json::array({"Read-only guidance: inspect the part before producing output."})}}}};
        json result = rpc(method, {{"request", request}, {"doc_id", id},
            {"provider", provider}, {"options", options}});
        if (result.contains("drawing_doc_id"))
            drawings.push_back(result.at("drawing_doc_id").get<std::string>());
        if (result.contains("cam_doc_id"))
            programs.push_back(result.at("cam_doc_id").get<std::string>());
        return result;
    }
};

constexpr const char* requests[] = {
    "Make a drawing of this part", "Generate a CNC machining program for this part"};
}  // namespace

TEST_CASE("native agent CABI Ask Plan and plan_only cannot commit drawing or CAM fast paths") {
    Fixture fixture;
    const std::vector<json> options = {
        {{"mode", "ask"}}, {{"mode", " ASK "}}, {{"mode", "plan"}}, {{"mode", " PlAn "}},
        {{"plan_only", true}}, {{"mode", "AgEnT"}, {"plan_only", true}},
        {{"mode", " ASK "}, {"plan_only", true}},
        {{"mode", " ASK "}, {"approved_plan", {
            {"parts", json::array({{{"name", "Stale approval"}, {"features", json::array()}}})}}}}};
    for (const char* method : {"agent_run", "agent_run_v2"}) {
        for (const json& mode : options) {
            for (const char* request : requests) {
                INFO("method=" << method << " options=" << mode << " request=" << request);
                const auto docs = rpc("doc_list");
                const auto tree = rpc("doc_tree", {{"doc_id", fixture.id}});
                const auto files = fixture.files();
                const json result = fixture.run(method, request, mode);
                INFO("result=" << result);
                CHECK_FALSE(result.value("created_drawing", false));
                CHECK_FALSE(result.contains("drawing_doc_id"));
                CHECK_FALSE(result.contains("gcode_path"));
                CHECK_FALSE(result.contains("cam_doc_id"));
                CHECK(rpc("doc_list") == docs);
                CHECK(rpc("doc_tree", {{"doc_id", fixture.id}}) == tree);
                CHECK(fixture.files() == files);
                if (mode.value("mode", std::string()) == "ask" ||
                    mode.value("mode", std::string()) == " ASK ") {
                    CHECK(result.value("status", std::string()) == "done");
                    CHECK(result.value("stop_reason", std::string()) == "ask");
                    CHECK(result.value("answer", std::string()) ==
                        "Read-only guidance: inspect the part before producing output.");
                } else {
                    CHECK(result.value("status", std::string()) == "plan_ready");
                    const bool plan = mode.value("mode", std::string()) == "plan" ||
                                      mode.value("mode", std::string()) == " PlAn ";
                    CHECK(result.value("stop_reason", std::string()) ==
                          (plan ? "awaiting_plan_approval" : "plan_only"));
                    CHECK(result.value("engaged", std::string()) == "design");
                    REQUIRE(result.contains("plan"));
                    REQUIRE(result["plan"].is_object());
                    REQUIRE(result["plan"].contains("parts"));
                    CHECK(result["plan"]["parts"].size() == 1);
                }
            }
        }
    }
}

TEST_CASE("native agent CABI Agent and legacy default retain real drawing and posted CAM output") {
    Fixture fixture;
    for (const char* method : {"agent_run", "agent_run_v2"}) {
        for (const json& mode : {json::object(), json{{"mode", " AgEnT "}}}) {
            const json drawing = fixture.run(method, requests[0], mode);
            REQUIRE(drawing.value("created_drawing", false));
            const std::string drawing_id = drawing.at("drawing_doc_id");
            const json projected = rpc("drawing_get", {{"doc_id", drawing_id}});
            REQUIRE(projected.contains("sheets"));
            CHECK(drawing.value("source_doc_id", std::string()) == fixture.id);
            const json program = fixture.run(method, requests[1], mode);
            REQUIRE(program.value("status", std::string()) == "done");
            CHECK(program.value("engaged", std::string()) == "cam");
            const fs::path path = program.at("gcode_path").get<std::string>();
            REQUIRE(fs::is_regular_file(path));
            CHECK(path.parent_path() == fixture.output);
        }
    }
}

TEST_CASE("native agent CABI explicit approval resumes design execution rather than drawing CAM shortcuts") {
    const json approved = {{"parts", json::array({{
        {"name", "Approved box edit"}, {"purpose", "Expand the existing part"},
        {"dimensions", json::object()}, {"features", json::array()}}})}};
    for (const char* method : {"agent_run", "agent_run_v2"}) {
        for (json options : {json{{"mode", " PlAn "}}, json{{"plan_only", true}},
                             json{{"mode", "agent"}}}) {
            options["approved_plan"] = approved;
            for (const char* request : requests) {
                Fixture fixture;
                INFO("method=" << method << " options=" << options << " request=" << request);
                const json before = rpc("doc_tree", {{"doc_id", fixture.id}});
                const json ops = json::array({
                    {{"op", "add_feature"}, {"feature", {
                        {"type", "box"}, {"name", "Approved expansion"},
                        {"params", {{"length", {{"value", 30}, {"expr", nullptr}}},
                                    {"width", {{"value", 20}, {"expr", nullptr}}},
                                    {"height", {{"value", 10}, {"expr", nullptr}}}}}
                    }}}
                });
                const json result = fixture.run(method, request, options, ops);
                INFO("result=" << result);
                CHECK(result.value("engaged", std::string()) == "design");
                CHECK(result.value("status", std::string()) == "done");
                CHECK_FALSE(result.contains("drawing_doc_id"));
                CHECK_FALSE(result.contains("cam_doc_id"));
                CHECK_FALSE(result.contains("gcode_path"));
                const json after = rpc("doc_tree", {{"doc_id", fixture.id}});
                CHECK(after.at("revision").get<long>() > before.at("revision").get<long>());
                CHECK(after["tree"]["features"].size() == before["tree"]["features"].size() + 1);
                CHECK(after["tree"]["features"].back().at("name") == "Approved expansion");
            }
        }
    }
}
