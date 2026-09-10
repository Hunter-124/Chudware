// Task 04: per-body appearance (set_appearance op + AICM bake). The C++ core had never ported the
// set_appearance op (apply_one_op threw "unsupported"), and the tessellation paths ignored
// doc["appearances"] — so a body's colour/material couldn't be set by the copilot/MCP and wouldn't
// render. This exercises the now-ported op end to end: the op writes the contracted doc field, and
// both tessellation paths (compile AICM via the apply-ops tess metadata + doc_tessellate_bodies)
// bake the colour + RealView material onto the body.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <chrono>
#include <filesystem>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/session.h"
#include "doc/stepplus.h"
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

struct AppearanceFiles {
    std::filesystem::path dir;
    explicit AppearanceFiles(const std::string& id)
        : dir(std::filesystem::temp_directory_path() / ("chudware_appearance_" + id)) {
        std::filesystem::create_directories(dir);
    }
    ~AppearanceFiles() {
        std::error_code ec;
        std::filesystem::remove_all(dir, ec);
    }
};

aicm::AppearanceRegion face_appearance(const aicm::AicmBody& body, uint32_t face_id) {
    for (const auto& region : body.appearance_regions)
        if (face_id >= region.first_face && face_id - region.first_face < region.face_count)
            return region;
    aicm::AppearanceRegion appearance;
    for (int k = 0; k < 3; ++k) appearance.color[k] = body.color[k];
    appearance.metallic = body.metallic;
    appearance.roughness = body.roughness;
    return appearance;
}

void check_face_appearance(const aicm::AicmBody& body, uint32_t face_id,
                           const aicm::AicmBody& expected) {
    const auto appearance = face_appearance(body, face_id);
    for (int k = 0; k < 3; ++k) CHECK(appearance.color[k] == Approx(expected.color[k]));
    CHECK(appearance.metallic == Approx(expected.metallic));
    CHECK(appearance.roughness == Approx(expected.roughness));
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

TEST_CASE("assembly appearance: unstyled repeated components match the standalone part") {
    using namespace chudware::doc;
    const std::string part = make_box_doc();
    AppearanceFiles files(part);
    const auto path = files.dir / "neutral.step+";
    doc_save(part, path.string());
    const auto standalone = doc_tessellate_bodies(part);
    REQUIRE(standalone.size() == 1);
    const std::string assembly = doc_new("assembly", "NeutralAssembly").at("doc_id");
    doc_apply_ops(assembly, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", true}},
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", true}},
    }), false);
    const auto bodies = doc_tessellate_bodies(assembly);
    REQUIRE(bodies.size() == 2);
    for (size_t i = 0; i < bodies.size(); ++i) {
        CHECK(bodies[i].body_id == i + 1);
        REQUIRE(bodies[i].faces.size() == standalone[0].faces.size());
        for (size_t fi = 0; fi < bodies[i].faces.size(); ++fi) {
            CHECK(bodies[i].faces[fi].face_id == standalone[0].faces[fi].face_id);
            check_face_appearance(bodies[i], bodies[i].faces[fi].face_id, standalone[0]);
        }
    }
}

TEST_CASE("assembly appearance: multibody colors and finishes inherit through nesting and overrides") {
    using namespace chudware::doc;
    const std::string part = make_box_doc();
    AppearanceFiles files(part);
    doc_apply_ops(part, -1, json::array({
        {{"op", "add_feature"}, {"feature", {
            {"type", "box"}, {"name", "Second"}, {"params", {
                {"length", 10}, {"width", 10}, {"height", 10},
                {"at", {60, 0, 0}}, {"mode", "new"}}}}}},
        {{"op", "set_appearance"}, {"id", "b1"}, {"color", {0.85, 0.05, 0.05}},
         {"metallic", 0.9}, {"roughness", 0.2}},
        {{"op", "set_appearance"}, {"id", "b2"}, {"color", {0.05, 0.05, 0.85}},
         {"metallic", 0.1}, {"roughness", 0.8}},
    }), false);
    const auto standalone = doc_tessellate_bodies(part);
    REQUIRE(standalone.size() == 2);
    const auto part_path = files.dir / "two_bodies.step+";
    doc_save(part, part_path.string());
    SUBCASE("source compiled from features instead of solved BREP cache") {
        auto saved = stepplus_open(part_path.string());
        saved.extra.erase("cache.json");
        saved.extra.erase("cache.brep");
        saved.extra.erase("cache.aicm");
        stepplus_save(saved.doc, part_path.string(), saved.extra);
    }
    SUBCASE("source loaded from solved BREP cache") {}
    const std::string child = doc_new("assembly", "ColoredChild").at("doc_id");
    doc_apply_ops(child, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", part_path.string()}}}, {"fixed", true}},
        {{"op", "asm_insert_component"}, {"source", {{"path", part_path.string()}}}, {"fixed", true}},
        // A color-only component override retains the source finishes, not its colors.
        {{"op", "set_appearance"}, {"id", "b2"}, {"color", {0.05, 0.75, 0.05}}},
    }), false);
    const auto child_bodies = doc_tessellate_bodies(child);
    REQUIRE(child_bodies.size() == 2);
    const uint32_t faces_per_part =
        static_cast<uint32_t>(standalone[0].faces.size() + standalone[1].faces.size());
    REQUIRE(child_bodies[0].faces.size() == faces_per_part);
    const auto child_path = files.dir / "child.step+";
    doc_save(child, child_path.string());
    const std::string parent = doc_new("assembly", "ColoredParent").at("doc_id");
    doc_apply_ops(parent, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", child_path.string()}}}, {"fixed", true}},
    }), false);
    const auto nested = doc_tessellate_bodies(parent);
    REQUIRE(nested.size() == 1);  // a component remains one selector/world body
    CHECK(nested[0].body_id == 1);
    REQUIRE(nested[0].faces.size() == 2 * faces_per_part);
    for (uint32_t fi = 1; fi <= 2 * faces_per_part; ++fi) {
        const size_t source = ((fi - 1) % faces_per_part) >= standalone[0].faces.size() ? 1 : 0;
        aicm::AicmBody expected = standalone[source];
        if (fi > faces_per_part) {
            expected.color[0] = 0.05f; expected.color[1] = 0.75f; expected.color[2] = 0.05f;
        }
        CHECK(nested[0].faces[fi - 1].face_id == fi);
        check_face_appearance(nested[0], fi, expected);
    }
    // An outer override wins over both authored source and inner component overrides.
    set_appearance(parent, {{"color", {0.3, 0.3, 0.3}}, {"metallic", 0.4}, {"roughness", 0.6}});
    const auto overridden = doc_tessellate_bodies(parent);
    REQUIRE(overridden.size() == 1);
    CHECK(overridden[0].body_id == nested[0].body_id);
    CHECK(overridden[0].pos.vec() == nested[0].pos.vec());
    CHECK(overridden[0].idx.vec() == nested[0].idx.vec());
    REQUIRE(overridden[0].faces.size() == nested[0].faces.size());
    aicm::AicmBody expected;
    expected.color[0] = expected.color[1] = expected.color[2] = 0.3f;
    expected.metallic = 0.4f; expected.roughness = 0.6f;
    for (size_t fi = 0; fi < overridden[0].faces.size(); ++fi) {
        CHECK(overridden[0].faces[fi].face_id == nested[0].faces[fi].face_id);
        check_face_appearance(overridden[0], overridden[0].faces[fi].face_id, expected);
    }
}

TEST_CASE("assembly appearance: a descendant edit invalidates the outer saved display cache") {
    using namespace chudware::doc;
    const std::string part = make_box_doc();
    AppearanceFiles files(part);
    const auto part_path = files.dir / "leaf.step+";
    set_appearance(part, {{"color", {0.9, 0.05, 0.05}}});
    doc_save(part, part_path.string());
    const auto old_time = std::filesystem::last_write_time(part_path);
    const std::string child = doc_new("assembly", "CachedChildAppearance").at("doc_id");
    doc_apply_ops(child, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", part_path.string()}}}, {"fixed", true}},
    }), false);
    const auto child_path = files.dir / "child.step+";
    doc_save(child, child_path.string());
    const std::string parent = doc_new("assembly", "CachedParentAppearance").at("doc_id");
    doc_apply_ops(parent, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", child_path.string()}}}, {"fixed", true}},
    }), false);
    const auto parent_path = files.dir / "parent.step+";
    doc_save(parent, parent_path.string());
    REQUIRE(doc_close(parent));
    const json before = doc_open(parent_path.string());
    REQUIRE(before.value("cache_hit", false));
    CHECK(before.at("tess").at("bodies").at(0).at("color")[0].get<float>() == Approx(0.9f));
    REQUIRE(doc_close(before.at("doc_id").get<std::string>()));

    // The child assembly and outer document stay byte-for-byte unchanged. Only the
    // leaf source changes; force a distinct timestamp without sleeping on coarse filesystems.
    set_appearance(part, {{"color", {0.05, 0.05, 0.9}}});
    doc_save(part, part_path.string());
    std::filesystem::last_write_time(part_path, old_time + std::chrono::seconds(2));
    const json after = doc_open(parent_path.string());
    // Inspect the immediate display response, not doc_tessellate_bodies: that lazy
    // recompilation would conceal an incorrect cold-cache hit from the host.
    const json& color = after.at("tess").at("bodies").at(0).at("color");
    CHECK(color[0].get<float>() == Approx(0.05f));
    CHECK(color[2].get<float>() == Approx(0.9f));
    REQUIRE(doc_close(after.at("doc_id").get<std::string>()));
}
