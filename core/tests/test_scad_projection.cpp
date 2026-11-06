#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "doc/scad_projection.h"

using nlohmann::json;
using chudware::doc::scad::project_native;
using chudware::doc::scad::restore_native;

static json native_document() {
    return {
        {"schema", "aicad.part/1"}, {"id", "doc"}, {"name", "projection"},
        {"equations", {{"globals", json::array({{{"name", "W"}, {"expr", "25.4*2"}}})}}},
        {"features", json::array({
            {{"id", "f1"}, {"type", "box"}, {"name", "base"}, {"params", {{"length", {{"value", 50.8}, {"expr", "W"}}}, {"width", 25.4}, {"height", 12.7}, {"centered", false}}}, {"refs", json::object()}},
            {{"id", "f2"}, {"type", "cylinder"}, {"params", {{"radius", 3.0}, {"height", 20.0}, {"merge", false}, {"at", json::array({10, 10, 0})}}}},
            {{"id", "f3"}, {"type", "boolean"}, {"params", {{"op", "subtract"}}}, {"refs", {{"targets", json::array({"f1"})}, {"tools", json::array({"f2"})}}}},
            {{"id", "f4"}, {"type", "transform"}, {"params", {{"translate", json::array({1, 2, 3})}, {"pivot", json::array({2, 0, 0})}, {"rotate", {{"axis", json::array({0, 1, 0})}, {"angle", 90}}}}}, {"refs", {{"body", "f3"}}}}
        })}
    };
}

TEST_CASE("managed projection preserves graph, expressions, units and deterministic mapping") {
    const json doc = native_document();
    const json result = project_native(doc, 25.4);
    REQUIRE(result.at("ok").get<bool>());
    CHECK(result == project_native(doc, 25.4));
    const auto& p = result.at("params");
    CHECK(p.at("mm_per_source_unit") == 25.4);
    CHECK(p.at("parameters").at("p0_width") == 25.4);
    CHECK(p.at("parameters").at("p0_length").at("expr") == "W");
    const json restored = restore_native(p);
    REQUIRE(restored.at("ok").get<bool>());
    CHECK(restored.at("document") == doc);
}

TEST_CASE("literal edits restore original IDs and native expression values") {
    json p = project_native(native_document(), 25.4).at("params");
    p["parameters"]["p0_width"] = 50.8;
    p["parameters"]["p3_translate0"] = 12.0;
    json restored = restore_native(p);
    REQUIRE(restored.at("ok").get<bool>());
    const json& doc = restored.at("document");
    CHECK(doc["features"][0]["id"] == "f1");
    CHECK(doc["features"][0]["params"]["width"] == 50.8);
    CHECK(doc["features"][0]["params"]["length"]["expr"] == "W");
    CHECK(doc["features"][3]["params"]["translate"][0] == 12.0);
    p["parameters"]["p0_width"] = {{"value", 50.8}, {"expr", "W/2"}};
    restored = restore_native(p);
    REQUIRE(restored.at("ok").get<bool>());
    CHECK(restored["document"]["features"][0]["params"]["width"]["expr"] == "W/2");
}

TEST_CASE("changed executable code, descriptors, mappings and dependencies refuse stale history") {
    const json original = project_native(native_document(), 1).at("params");
    json p = original;
    p["source"] = p["source"].get<std::string>() + "sphere(100);\n";
    CHECK_FALSE(restore_native(p).at("ok").get<bool>());
    p = original; p["parameter_schema"][0]["kind"] = "number";
    CHECK_FALSE(restore_native(p).at("ok").get<bool>());
    p = original; p["native_projection"]["mapping"]["p0_width"] = "/features/0/params/height";
    CHECK_FALSE(restore_native(p).at("ok").get<bool>());
    p = original; p["files"].push_back({{"path", "other.scad"}, {"data_b64", ""}});
    CHECK_FALSE(restore_native(p).at("ok").get<bool>());
    p = original; p["parameters"]["p0_width"] = -1;
    CHECK_FALSE(restore_native(p).at("ok").get<bool>());
}

TEST_CASE("all unsupported features and options are reported") {
    json doc = native_document();
    doc["features"].push_back({{"id", "f5"}, {"type", "fillet"}});
    doc["features"].push_back({{"id", "f6"}, {"type", "sweep"}});
    doc["features"][0]["params"]["axis"] = json::array({1, 0, 0});
    doc["features"][0]["params"]["unknown_option"] = true;
    json result = project_native(doc, 1);
    CHECK_FALSE(result.at("ok").get<bool>());
    bool axis = false, option = false, fillet = false, sweep = false;
    for (const auto& b : result["report"]["blockers"]) {
        axis |= b.value("id", "") == "f1" && b.value("option", "") == "axis";
        option |= b.value("id", "") == "f1" && b.value("option", "") == "unknown_option";
        fillet |= b.value("id", "") == "f5";
        sweep |= b.value("id", "") == "f6";
    }
    CHECK(axis); CHECK(option); CHECK(fillet); CHECK(sweep);
    CHECK_FALSE(project_native(native_document(), 0).at("ok").get<bool>());
}

TEST_CASE("consumption, automatic contact, transforms and live bodies are explicit") {
    json doc = native_document();
    // A default additive primitive after a live body cannot be safely guessed.
    doc["features"][1]["params"].erase("merge");
    CHECK_FALSE(project_native(doc, 1).at("ok").get<bool>());
    doc = native_document();
    doc["features"][3]["params"]["copy"] = true;
    auto projected = project_native(doc, 1);
    REQUIRE(projected.at("ok").get<bool>());
    CHECK(restore_native(projected.at("params"))["document"] == doc);
    doc["features"].push_back({{"id", "f5"}, {"type", "boolean"}, {"params", {{"op", "fuse"}, {"a", "f1"}, {"b", "f4"}}}});
    CHECK_FALSE(project_native(doc, 1).at("ok").get<bool>());
}

TEST_CASE("centered sphere and cone with cut and composed in-place transforms") {
    json doc = {{ "schema", "aicad.part/1" }, {"features", json::array({
        {{"id", "s"}, {"type", "sphere"}, {"params", {{"radius", {{"value", 20.0}, {"expr", nullptr}}}}}},
        {{"id", "c"}, {"type", "cone"}, {"params", {{"r1", 5}, {"r2", 0}, {"height", 50}, {"mode", "cut"}, {"centered", false}}}},
        {{"id", "t1"}, {"type", "transform"}, {"params", {{"body", "c"}, {"translate", json::array({1, 0, 0})}}}},
        {{"id", "t2"}, {"type", "transform"}, {"params", {{"body", "c"}, {"translate", json::array({0, 2, 0})}}}}
    })}};
    json p = project_native(doc, 1).at("params");
    CHECK(restore_native(p).at("document") == doc);
    p["parameters"]["p0_radius"] = 21.0;
    json restored = restore_native(p);
    REQUIRE(restored.at("ok").get<bool>());
    CHECK(restored["document"]["features"][0]["params"]["radius"]["value"] == 21.0);
    CHECK(restored["document"]["features"][0]["params"]["radius"]["expr"].is_null());
}

TEST_CASE("invalid transform axis and unsupported render options refuse restoration") {
    json doc = native_document();
    doc["features"][3]["params"]["rotate"]["axis"] = json::array({0, 0, 0});
    CHECK_FALSE(project_native(doc, 1).at("ok").get<bool>());
    json p = project_native(native_document(), 1).at("params");
    p["parameters"]["p3_axis1"] = 0;
    CHECK_FALSE(restore_native(p).at("ok").get<bool>());
    p = project_native(native_document(), 1).at("params");
    p["renderer_options"] = {{"fn", 12}};
    CHECK_FALSE(restore_native(p).at("ok").get<bool>());
}

TEST_CASE("non-part geometry and altered parameter sets cannot be silently dropped") {
    json doc = native_document();
    doc["schema"] = "aicad.asm/1";
    doc["components"] = json::array({{{"id", "k1"}}});
    CHECK_FALSE(project_native(doc, 1).at("ok").get<bool>());
    doc = native_document();
    doc["features"][0]["params"]["width"] = 1e-9;
    CHECK_FALSE(project_native(doc, 1).at("ok").get<bool>());
    json p = project_native(native_document(), 1).at("params");
    p["parameters"]["other"] = 7;
    CHECK_FALSE(restore_native(p).at("ok").get<bool>());
    p = project_native(native_document(), 1).at("params");
    p["mm_per_source_unit"] = 25.4;
    CHECK_FALSE(restore_native(p).at("ok").get<bool>());
}

static json shared_graph(int depth) {
    json doc = {{"schema", "aicad.part/1"}, {"features", json::array({
        {{"id", "base"}, {"type", "box"}, {"params", {{"length", 20}, {"width", 20}, {"height", 20}}}}
    })}};
    std::string last = "base";
    for (int i = 0; i < depth; ++i) {
        const std::string copy = "copy" + std::to_string(i);
        const std::string combined = "combined" + std::to_string(i);
        doc["features"].push_back({{"id", copy}, {"type", "transform"},
            {"params", {{"body", last}, {"copy", true}, {"translate", json::array({1, 0, 0})}}}});
        doc["features"].push_back({{"id", combined}, {"type", "boolean"},
            {"params", {{"op", "fuse"}, {"a", last}, {"b", copy}}}});
        last = combined;
    }
    return doc;
}

TEST_CASE("shared graph projection grows linearly and retains managed history") {
    json small = project_native(shared_graph(30), 1);
    json large_doc = shared_graph(60);
    json large = project_native(large_doc, 1);
    REQUIRE(small.at("ok").get<bool>());
    REQUIRE(large.at("ok").get<bool>());
    const auto small_size = small["params"]["source"].get_ref<const std::string&>().size();
    const auto large_size = large["params"]["source"].get_ref<const std::string&>().size();
    CHECK(large_size < 3 * small_size);
    json restored = restore_native(large["params"]);
    REQUIRE(restored.at("ok").get<bool>());
    CHECK(restored.at("document") == large_doc);
}

TEST_CASE("projection and metadata restoration refuse graphs beyond resource limits") {
    json large_doc = shared_graph(1030);
    CHECK_FALSE(project_native(large_doc, 1).at("ok").get<bool>());
    json params = project_native(native_document(), 1).at("params");
    params["native_projection"]["document"] = large_doc;
    CHECK_FALSE(restore_native(params).at("ok").get<bool>());
    json oversized = native_document();
    oversized["name"] = std::string(20000, 'x');
    CHECK_FALSE(project_native(oversized, 1).at("ok").get<bool>());
    params["native_projection"]["document"] = oversized;
    CHECK_FALSE(restore_native(params).at("ok").get<bool>());
}
