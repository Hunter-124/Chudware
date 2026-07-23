// Task 04: per-body appearance (set_appearance op + AICM bake). The C++ core had never ported the
// set_appearance op (apply_one_op threw "unsupported"), and the tessellation paths ignored
// doc["appearances"] — so a body's colour/material couldn't be set by the copilot/MCP and wouldn't
// render. This exercises the now-ported op end to end: the op writes the contracted doc field, and
// both tessellation paths (compile AICM via the apply-ops tess metadata + doc_tessellate_bodies)
// bake the colour + RealView material onto the body.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <filesystem>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/session.h"
#include "geom/aicm.h"

using json = nlohmann::json;
using doctest::Approx;

namespace {

std::string make_box_doc() {
    using chudware::doc::doc_new;
    using chudware::doc::doc_apply_ops;
    json nr = doc_new("part", "AppearanceTest");
    std::string id = nr.at("doc_id").get<std::string>();
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };
    json ops = json::array({
        json{{"op", "add_feature"},
             {"feature", {{"type", "box"}, {"name", "Block"},
                          {"params", {{"length", dimv(40)}, {"width", dimv(24)},
                                      {"height", dimv(12)}}}}}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);
    REQUIRE(resp.value("volume", 0.0) > 0.0);
    return id;
}

// Apply one set_appearance op and return the full ops-response (carries tree + tess).
json set_appearance(const std::string& id, const json& op_extra) {
    json op = json{{"op", "set_appearance"}, {"id", "b1"}};
    op.update(op_extra);
    return chudware::doc::doc_apply_ops(id, -1, json::array({op}), false);
}

}  // namespace

TEST_CASE("set_appearance: full material (color+metallic+roughness) writes doc and bakes onto body") {
    std::string id = make_box_doc();
    json resp = set_appearance(id, json{{"color", json::array({0.80, 0.62, 0.28})},
                                        {"metallic", 0.95}, {"roughness", 0.30}});

    // (a) doc field is the contracted {color, metallic, roughness} object form.
    const json& ap = resp.at("tree").at("appearances");
    REQUIRE(ap.contains("b1"));
    const json& v = ap.at("b1");
    REQUIRE(v.is_object());
    CHECK(v.at("color")[0].get<double>() == Approx(0.80));
    CHECK(v.at("color")[2].get<double>() == Approx(0.28));
    CHECK(v.at("metallic").get<double>() == Approx(0.95));
    CHECK(v.at("roughness").get<double>() == Approx(0.30));

    // (b) the apply-ops tess metadata reports the new appearance (host body list / swatch).
    const json& body0 = resp.at("tess").at("bodies").at(0);
    CHECK(body0.at("color")[0].get<double>() == Approx(0.80));
    CHECK(body0.at("metallic").get<double>() == Approx(0.95));
    CHECK(body0.at("roughness").get<double>() == Approx(0.30));

    // (c) the render tessellation bakes it onto the actual AICM body (colour + material trailer).
    std::vector<aicm::AicmBody> bodies = chudware::doc::doc_tessellate_bodies(id);
    REQUIRE(bodies.size() == 1);
    CHECK(bodies[0].color[0] == Approx(0.80f));
    CHECK(bodies[0].color[1] == Approx(0.62f));
    CHECK(bodies[0].color[2] == Approx(0.28f));
    CHECK(bodies[0].metallic == Approx(0.95f));
    CHECK(bodies[0].roughness == Approx(0.30f));
}

TEST_CASE("set_appearance: colour-only stores the bare [r,g,b] form and resets material to default") {
    std::string id = make_box_doc();
    // First set a metal, then a colour-only change -> material falls back to the dielectric default.
    set_appearance(id, json{{"color", json::array({0.80, 0.62, 0.28})}, {"metallic", 0.95}});
    json resp = set_appearance(id, json{{"color", json::array({0.10, 0.20, 0.30})}});

    const json& v = resp.at("tree").at("appearances").at("b1");
    REQUIRE(v.is_array());  // bare [r,g,b]
    CHECK(v[1].get<double>() == Approx(0.20));

    std::vector<aicm::AicmBody> bodies = chudware::doc::doc_tessellate_bodies(id);
    REQUIRE(bodies.size() == 1);
    CHECK(bodies[0].color[0] == Approx(0.10f));
    CHECK(bodies[0].metallic == Approx(0.0f));   // colour-only -> default dielectric
    CHECK(bodies[0].roughness == Approx(0.5f));
}

TEST_CASE("set_appearance: null colour clears back to the default grey") {
    std::string id = make_box_doc();
    set_appearance(id, json{{"color", json::array({0.10, 0.20, 0.30})}});
    json resp = set_appearance(id, json{{"color", nullptr}});

    // Cleared: the b1 entry is gone (and with no entries the appearances object is dropped).
    const json& tree = resp.at("tree");
    bool cleared = !tree.contains("appearances") ||
                   !tree.at("appearances").is_object() ||
                   !tree.at("appearances").contains("b1");
    CHECK(cleared);

    std::vector<aicm::AicmBody> bodies = chudware::doc::doc_tessellate_bodies(id);
    REQUIRE(bodies.size() == 1);
    CHECK(bodies[0].color[0] == Approx(0.62f));  // back to the render default grey
}

TEST_CASE("set_appearance: appearance round-trips through the native .step+ save/open") {
    namespace fs = std::filesystem;
    std::string id = make_box_doc();
    set_appearance(id, json{{"color", json::array({0.80, 0.62, 0.28})},
                            {"metallic", 0.95}, {"roughness", 0.30}});

    fs::path out = fs::temp_directory_path() / "chudware_appearance_roundtrip.step+";
    json sv = chudware::doc::doc_save(id, out.string());
    REQUIRE(fs::exists(out));

    json op = chudware::doc::doc_open(out.string());
    std::string nid = op.at("doc_id").get<std::string>();

    // The reopened doc still carries the appearance, baked onto its tessellated body.
    std::vector<aicm::AicmBody> bodies = chudware::doc::doc_tessellate_bodies(nid);
    REQUIRE(bodies.size() == 1);
    CHECK(bodies[0].color[0] == Approx(0.80f));
    CHECK(bodies[0].color[2] == Approx(0.28f));
    CHECK(bodies[0].metallic == Approx(0.95f));
    CHECK(bodies[0].roughness == Approx(0.30f));

    std::error_code ec;
    fs::remove(out, ec);
}

TEST_CASE("set_appearance: out-of-range components are clamped, missing id is rejected") {
    std::string id = make_box_doc();
    json resp = set_appearance(id, json{{"color", json::array({1.5, -0.2, 0.5})}});
    const json& v = resp.at("tree").at("appearances").at("b1");
    CHECK(v[0].get<double>() == Approx(1.0));  // clamped high
    CHECK(v[1].get<double>() == Approx(0.0));  // clamped low
    CHECK(v[2].get<double>() == Approx(0.5));

    // A set_appearance without a body id is a contract error.
    bool threw = false;
    try {
        chudware::doc::doc_apply_ops(
            id, -1, json::array({json{{"op", "set_appearance"},
                                      {"color", json::array({0, 0, 0})}}}), false);
    } catch (const chudware::doc::OpError&) {
        threw = true;
    }
    CHECK(threw);
}
