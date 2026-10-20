// Task 04: per-body appearance (set_appearance op + AICM bake). The C++ core had never ported the
// set_appearance op (apply_one_op threw "unsupported"), and the tessellation paths ignored
// doc["appearances"] — so a body's colour/material couldn't be set by the copilot/MCP and wouldn't
// render. This exercises the now-ported op end to end: the op writes the contracted doc field, and
// both tessellation paths (compile AICM via the apply-ops tess metadata + doc_tessellate_bodies)
// bake the colour + RealView material onto the body.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <chrono>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <map>
#include <string>
#include <vector>
#include <future>
#include <thread>

#include <miniz.h>
#include <nlohmann/json.hpp>
#include <Standard_Version.hxx>

#include "doc/session.h"
#include "doc/drawing_session.h"
#include "doc/stepplus.h"
#include "doc/document.h"
#include "aicm_loader.h"  // host loader (host/src/render on the include path)
#include "geom/aicm.h"
#include "environment_test_support.h"

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
    REQUIRE(resp.value("volume", 0.0) == Approx(40.0 * 24.0 * 12.0));
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

TEST_CASE("view metrics preserve every camera and visual measurement within the solver thread budget") {
    using namespace chudware::doc;
    struct ThreadBudgetGuard {
        const char* previous = std::getenv("CHUDWARE_SIM_THREADS");
        const bool had_previous = previous != nullptr;
        const std::string saved = previous ? previous : "";
        ~ThreadBudgetGuard() {
            chudware::test::setOrUnsetEnvironment(
                "CHUDWARE_SIM_THREADS", had_previous ? saved.c_str() : nullptr);
        }
    } thread_budget;
    const auto id = make_box_doc();
    struct DocumentGuard {
        const std::string& id;
        ~DocumentGuard() { doc_close(id); }
    } document{id};
    json cameras = json::array();
    for (int i = 0; i < 24; ++i) {
        const double angle = 2.0 * std::acos(-1.0) * i / 24;
        cameras.push_back({
            {"projection", "perspective"},
            {"position", {120.0 * std::cos(angle), 120.0 * std::sin(angle), 100.0}},
            {"target", {0.0, 0.0, 0.0}}, {"up", {0.0, 0.0, 1.0}},
            {"vertical_fov_deg", 30.0}, {"near", 1.0}, {"far", 1000.0}});
    }
    const json request{{"doc_id", id}, {"cameras", cameras}, {"width", 128}, {"height", 96}};
    REQUIRE(chudware::test::setEnvironment("CHUDWARE_SIM_THREADS", "1") == 0);
    const auto serial = doc_view_metrics(request);
    REQUIRE_MESSAGE(serial.value("ok", false), serial.dump());
    REQUIRE(serial.at("views").size() == cameras.size());
    for (const auto& view : serial.at("views")) {
        CHECK(view.at("fg_px").get<int>() > 0);
        CHECK_FALSE(view.at("face_vis").empty());
        CHECK_FALSE(view.at("edge_vis").empty());
    }
    REQUIRE(chudware::test::setEnvironment("CHUDWARE_SIM_THREADS", "4") == 0);
    const auto parallel = doc_view_metrics(request);
    REQUIRE_MESSAGE(parallel.value("ok", false), parallel.dump());
    CHECK(parallel.at("faces") == serial.at("faces"));
    CHECK(parallel.at("edges") == serial.at("edges"));
    CHECK(parallel.at("views") == serial.at("views"));
    // With a disposable TasksMax=20 service this demand runs out of launch slots
    // after some workers start. On an unconstrained host it may legitimately finish.
    // Either outcome must leave the native document usable, never abort or return
    // partially populated measurements as a successful response.
    REQUIRE(chudware::test::setEnvironment("CHUDWARE_SIM_THREADS", "64") == 0);
    json demand = request;
    demand["width"] = 1024;
    demand["height"] = 1024;
    const auto constrained = doc_view_metrics(demand);
    INFO(constrained.dump());
    if (constrained.value("ok", false)) {
        REQUIRE(constrained.at("views").size() == cameras.size());
        for (const auto& view : constrained.at("views")) {
            CHECK(view.at("fg_px").get<int>() > 0);
            CHECK_FALSE(view.at("face_vis").empty());
            CHECK_FALSE(view.at("edge_vis").empty());
        }
    } else {
        CHECK(constrained.at("code") == "metrics_failed");
        CHECK_FALSE(constrained.at("reason").get<std::string>().empty());
        CHECK_FALSE(constrained.contains("views"));
    }
    std::printf("[view_metrics] constrained launch outcome=%s\n",
                constrained.value("ok", false) ? "complete" : "metrics_failed");
    REQUIRE(chudware::test::setEnvironment("CHUDWARE_SIM_THREADS", "1") == 0);
    const auto recovered = doc_view_metrics(request);
    REQUIRE_MESSAGE(recovered.value("ok", false), recovered.dump());
    CHECK(recovered.at("faces") == serial.at("faces"));
    CHECK(recovered.at("edges") == serial.at("edges"));
    CHECK(recovered.at("views") == serial.at("views"));
}

TEST_CASE("selected face finish: body defaults, physics, caches and native nesting survive set and clear") {
    using namespace chudware::doc;
    const auto id = make_box_doc();
    AppearanceFiles files(id);
    doc_apply_ops(id, -1, json::array({
        {{"op", "set_material"}, {"key", "aluminum_6061"}},
        {{"op", "set_material"}, {"id", "b1"}, {"key", "steel_a36"}},
        {{"op", "set_appearance"}, {"id", "b1"}, {"color", {0.1, 0.2, 0.8}},
         {"metallic", 0.2}, {"roughness", 0.7}},
    }), false);
    const auto before = doc_tree(id).at("tree");
    auto cache = make_compile_cache();
    const auto initial = compile_document(before, json::object(), cache.get());
    REQUIRE(initial.ok);
    const auto inventory = face_sid_inventory(initial.shapes.at(0));
    REQUIRE(inventory.size() == 6);
    const auto selected = inventory.begin()->first;
    const auto face_id = inventory.begin()->second.at(0).first;
    const auto response = set_appearance(id, {
        {"faces", {selected}}, {"color", {0.9, 0.05, 0.1}}, {"metallic", 0.95}, {"roughness", 0.12}});
    const auto tree = response.at("tree");
    CHECK(tree.at("material") == before.at("material"));
    CHECK(tree.at("body_materials") == before.at("body_materials"));
    CHECK(response.at("volume").get<double>() == Approx(initial.volume));
    const auto direct = doc_tessellate_bodies(id);
    REQUIRE(direct.size() == 1);
    for (const auto& face : direct[0].faces.vec()) {
        const auto ap = face_appearance(direct[0], face.face_id);
        CHECK(ap.color[0] == Approx(face.face_id == face_id ? 0.9 : 0.1));
        CHECK(ap.metallic == Approx(face.face_id == face_id ? 0.95 : 0.2));
        CHECK(ap.roughness == Approx(face.face_id == face_id ? 0.12 : 0.7));
    }
    // Same retained compile cache + unchanged TShapes: metadata must change both
    // full-result and per-body cache keys, and the serialized APRG must reach its loader.
    auto check_compiled = [&](const json& document, bool painted) {
        const auto compiled = compile_document(document, json::object(), cache.get());
        REQUIRE(compiled.ok);
        render::DocMesh mesh;
        std::string error;
        REQUIRE(render::parseAicm(compiled.aicm.data(), compiled.aicm.size(), mesh, error));
        REQUIRE(mesh.bodies.size() == 1);
        for (const auto& face : mesh.bodies[0].faces) {
            float red = mesh.bodies[0].color[0], metal = mesh.bodies[0].metallic;
            float rough = mesh.bodies[0].roughness;
            for (const auto& ap : mesh.bodies[0].appearance_regions)
                if (face.face_id >= ap.first_face && face.face_id - ap.first_face < ap.face_count) {
                    red = ap.color[0]; metal = ap.metallic; rough = ap.roughness;
                }
            const bool chosen = painted && face.face_id == face_id;
            CHECK(red == Approx(chosen ? 0.9 : 0.1));
            CHECK(metal == Approx(chosen ? 0.95 : 0.2));
            CHECK(rough == Approx(chosen ? 0.12 : 0.7));
        }
    };
    check_compiled(tree, true);
    check_compiled(tree, true); // whole-result hit
    auto path = files.dir / "face_finish.step+";
    doc_save(id, path.string());
    const std::string reopened = doc_open(path.string()).at("doc_id");
    CHECK(doc_tree(reopened).at("tree").at("face_appearances") == tree.at("face_appearances"));
    auto current = reopened;
    for (int depth = 0; depth < 3; ++depth) {
        const auto bodies = doc_tessellate_bodies(current);
        REQUIRE(bodies.size() == 1);
        for (const auto& face : bodies[0].faces.vec()) {
            const auto ap = face_appearance(bodies[0], face.face_id);
            CHECK(ap.color[0] == Approx(face.face_id == face_id ? 0.9 : 0.1));
            CHECK(ap.metallic == Approx(face.face_id == face_id ? 0.95 : 0.2));
            CHECK(ap.roughness == Approx(face.face_id == face_id ? 0.12 : 0.7));
        }
        if (depth == 2) break;
        current = doc_new("assembly", "FaceFinishAssembly").at("doc_id");
        doc_apply_ops(current, -1, json::array({
            {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", true}},
        }), false);
        path = files.dir / ("nested" + std::to_string(depth) + ".step+");
        doc_save(current, path.string());
    }
    const auto cleared = set_appearance(id, {{"faces", {selected}}, {"color", nullptr}}).at("tree");
    CHECK_FALSE(cleared.contains("face_appearances"));
    check_compiled(cleared, false);
    const auto restored = doc_tessellate_bodies(id);
    for (const auto& face : restored.at(0).faces.vec())
        check_face_appearance(restored[0], face.face_id, direct[0]);
}

TEST_CASE("selected face finish: queried bores and bosses match viewport picks in a multibody part") {
    using namespace chudware::doc;
    const auto id = make_box_doc();
    doc_apply_ops(id, -1, json::array({
        {{"op", "add_feature"}, {"feature", {{"type", "cylinder"},
            {"params", {{"radius", 10}, {"height", 20}, {"mode", "new"}}}}}},
        {{"op", "add_feature"}, {"feature", {{"type", "cylinder"},
            {"params", {{"radius", 5}, {"height", 20}, {"mode", "cut"}}}}}},
        {{"op", "set_appearance"}, {"id", "b2"}, {"color", {0.1, 0.2, 0.8}}},
    }), false);
    const auto queried = doc_query(id, {
        {"of", "face"}, {"body_id", 2}, {"surf", "cylinder"}}).at("matches");
    REQUIRE(queried.size() == 2);
    for (const auto& selected : queried) {
        const bool bore = selected.at("concavity") == "bore";
        set_appearance(id, {{"id", "b2"}, {"faces", {selected}},
            {"color", {bore ? 0.9 : 0.4, 0.05, 0.1}}, {"metallic", 0.95}, {"roughness", 0.12}});
    }
    const auto bodies = doc_tessellate_bodies(id);
    REQUIRE(bodies.size() == 2);
    CHECK(bodies[0].appearance_regions.empty());
    for (const auto& face : bodies[1].faces.vec()) {
        const auto picked = doc_resolve_pick(id, 2, "face", face.face_id).at("selector");
        const auto appearance = face_appearance(bodies[1], face.face_id);
        if (picked.at("surf") == "cylinder") {
            const bool bore = picked.at("concavity") == "bore";
            CHECK(appearance.color[0] == Approx(bore ? 0.9 : 0.4));
            CHECK(appearance.metallic == Approx(0.95));
            CHECK(appearance.roughness == Approx(0.12));
            // Clearing through a viewport pick must remove the finish authored by query.
            set_appearance(id, {{"id", "b2"}, {"faces", {picked}}, {"color", nullptr}});
        } else {
            CHECK(appearance.color[0] == Approx(0.1));
        }
    }
    CHECK_FALSE(doc_tree(id).at("tree").contains("face_appearances"));
    CHECK(doc_tessellate_bodies(id).at(1).appearance_regions.empty());
}

TEST_CASE("selected face finish: unknown and colliding SIDs reject the entire transaction") {
    using namespace chudware::doc;
    const auto id = make_box_doc();
    const auto shape = compile_document(doc_tree(id).at("tree"), json::object()).shapes.at(0);
    const auto sid = face_sid_inventory(shape).begin()->first;
    const auto before = doc_tree(id);
    CHECK_THROWS_AS(set_appearance(id, {{"faces", {sid, "Fffffff"}}, {"color", {1, 0, 0}}}), OpError);
    CHECK(doc_tree(id) == before);
    CHECK_THROWS_AS(set_appearance(id, {{"faces", json::array()}, {"color", {1, 0, 0}}}), OpError);
    CHECK_THROWS_AS(set_appearance(id, {{"faces", {1}}, {"color", {1, 0, 0}}}), OpError);
    CHECK_THROWS_AS(set_appearance(id, {{"faces", {{{"kind", "edge"}, {"sid", sid}}}},
                                        {"color", {1, 0, 0}}}), OpError);
    const auto fid = before.at("tree").at("features").at(0).at("id");
    doc_apply_ops(id, -1, json::array({
        {{"op", "edit_feature"}, {"id", fid}, {"params", {{"height", 0.02}}}},
    }), false);
    const auto thin = doc_tree(id);
    const auto thin_shape = compile_document(thin.at("tree"), json::object()).shapes.at(0);
    const auto inventory = face_sid_inventory(thin_shape);
    std::string collision;
    for (const auto& [key, candidates] : inventory)
        if (candidates.size() > 1) { collision = key; break; }
    REQUIRE_FALSE(collision.empty()); // opposite faces round onto the same SID grid
    CHECK_THROWS_AS(doc_apply_ops(id, -1, json::array({
        {{"op", "set_material"}, {"key", "steel_a36"}},
        {{"op", "set_appearance"}, {"id", "b1"}, {"faces", {collision}}, {"color", {1, 0, 0}}},
    }), false), OpError);
    CHECK(doc_tree(id) == thin);
}

TEST_CASE("selected face finish: exact fingerprints distinguish faces sharing a SID") {
    using namespace chudware::doc;
    const auto id = make_box_doc();
    AppearanceFiles files(id);
    const auto fid = doc_tree(id).at("tree").at("features").at(0).at("id");
    doc_apply_ops(id, -1, json::array({
        {{"op", "edit_feature"}, {"id", fid}, {"params", {{"height", 0.02}}}},
    }), false);
    const auto shape = compile_document(doc_tree(id).at("tree"), json::object()).shapes.at(0);
    const auto inventory = face_sid_inventory(shape);
    std::vector<std::pair<uint32_t, json>> collision;
    for (const auto& [sid, candidates] : inventory)
        if (candidates.size() == 2) { collision = candidates; break; }
    REQUIRE(collision.size() == 2);
    const auto first = collision[0].second, second = collision[1].second;
    set_appearance(id, {{"faces", {first}}, {"color", {1, 0, 0}}});
    set_appearance(id, {{"faces", {second}}, {"color", {0, 1, 0}}});
    auto check_colors = [&](const std::string& current, bool first_painted, bool second_painted) {
        const auto bodies = doc_tessellate_bodies(current);
        REQUIRE(bodies.size() == 1);
        const auto live_shape = compile_document(doc_tree(current).at("tree"), json::object()).shapes.at(0);
        const auto live_inventory = face_sid_inventory(live_shape);
        for (const auto& [face_id, selector] : live_inventory.at(first.at("sid").get<std::string>())) {
            const auto appearance = face_appearance(bodies[0], face_id);
            const bool red = selector == first && first_painted;
            const bool green = selector == second && second_painted;
            CHECK(appearance.color[0] == Approx(red ? 1.0 : green ? 0.0 : bodies[0].color[0]));
            CHECK(appearance.color[1] == Approx(green ? 1.0 : red ? 0.0 : bodies[0].color[1]));
        }
    };
    check_colors(id, true, true);
    const auto path = files.dir / "colliding_face_finishes.step+";
    doc_save(id, path.string());
    const std::string reopened = doc_open(path.string()).at("doc_id");
    check_colors(reopened, true, true);
    set_appearance(reopened, {{"faces", {first}}, {"color", nullptr}});
    check_colors(reopened, false, true);
    set_appearance(reopened, {{"faces", {first}}, {"color", {1, 0, 0}}});
    auto stale = second;
    stale["centroid"][2] = 0.021;
    const auto before = doc_tree(reopened);
    CHECK_THROWS_AS(set_appearance(reopened, {{"faces", {stale}}, {"color", {0, 0, 1}}}), OpError);
    CHECK(doc_tree(reopened) == before);
    set_appearance(reopened, {{"faces", {first.at("sid")}}, {"color", nullptr}});
    check_colors(reopened, false, false);
}

TEST_CASE("selected face finish: stale fingerprints are inert even when a changed face keeps its SID") {
    using namespace chudware::doc;
    const auto id = make_box_doc();
    const auto selected = doc_query(id, {{"of", "face"}, {"side", "top"}}).at("matches").at(0);
    const auto sid = selected.at("sid");
    set_appearance(id, {{"faces", {selected}}, {"color", {1, 0, 0}}});
    const auto tree = doc_tree(id).at("tree");
    const auto fid = tree.at("features").at(0).at("id");
    doc_apply_ops(id, -1, json::array({
        {{"op", "edit_feature"}, {"id", fid}, {"params", {{"height", 12.02}}}},
    }), false);
    const auto now = doc_query(id, {{"of", "face"}, {"side", "top"}}).at("matches").at(0);
    CHECK(now.at("sid") == sid);
    const auto bodies = doc_tessellate_bodies(id);
    REQUIRE(bodies.size() == 1);
    CHECK(bodies[0].appearance_regions.empty());
    CHECK(doc_tree(id).at("tree").at("face_appearances") == tree.at("face_appearances"));
    set_appearance(id, {{"faces", {sid}}, {"color", nullptr}});
    CHECK_FALSE(doc_tree(id).at("tree").contains("face_appearances"));
    // A new assignment must resolve against geometry AFTER all ops, not the old ordinal.
    const auto before = doc_tree(id);
    CHECK_THROWS_AS(doc_apply_ops(id, -1, json::array({
        {{"op", "set_appearance"}, {"id", "b1"}, {"faces", {sid}}, {"color", {1, 0, 0}}},
        {{"op", "edit_feature"}, {"id", fid}, {"params", {{"height", 20}}}},
    }), false), OpError);
    CHECK(doc_tree(id) == before);
}

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

TEST_CASE("selected face finish: an older display-cache epoch cannot hide a saved override") {
    using namespace chudware::doc;
    const auto id = make_box_doc();
    AppearanceFiles files(id);
    const auto path = files.dir / "old_face_cache.step+";
    set_appearance(id, {{"color", {0.1, 0.2, 0.8}}});
    doc_save(id, path.string());
    const auto unpainted_mesh = stepplus_open(path.string()).extra.at("cache.aicm");
    const auto compiled = compile_document(doc_tree(id).at("tree"), json::object());
    REQUIRE(compiled.ok);
    const auto inventory = face_sid_inventory(compiled.shapes.at(0));
    REQUIRE(inventory.size() == 6);
    const auto selected = inventory.begin()->first;
    const auto face_id = inventory.begin()->second.at(0).first;
    set_appearance(id, {{"faces", {selected}}, {"color", {0.9, 0.05, 0.1}}});
    doc_save(id, path.string());
    auto stored = stepplus_open(path.string());
    auto header = json::parse(stored.extra.at("cache.json"));
    // A prior writer preserves unknown document fields but does not render them.
    // Its otherwise matching cache must not supersede the authored face finish.
    header["engine"] = "chudware-geomcache/2;occt=" OCC_VERSION_COMPLETE;
    stored.extra["cache.json"] = header.dump();
    stored.extra["cache.aicm"] = unpainted_mesh;
    stepplus_save(stored.doc, path.string(), stored.extra);
    REQUIRE(doc_close(id));
    const auto reopened = doc_open(path.string());
    CHECK_FALSE(reopened.value("cache_hit", false));
    // Inspect the immediate host display, before any lazy geometry recompilation.
    render::DocMesh mesh;
    std::string error;
    REQUIRE(render::loadAicmFile(reopened.at("tess").at("aicm_path"), mesh, error));
    REQUIRE(mesh.bodies.size() == 1);
    float red = mesh.bodies[0].color[0];
    for (const auto& region : mesh.bodies[0].appearance_regions)
        if (face_id >= region.first_face && face_id - region.first_face < region.face_count)
            red = region.color[0];
    CHECK(red == Approx(0.9));
    REQUIRE(doc_close(reopened.at("doc_id").get<std::string>()));
}

// ---------------------------------------------------------------- persisted build state
// cache.ckpt + warm open (contracts/stepplus.md §4): a reopened part never re-runs its
// feature tree while its caches are valid, its first edit executes only the edited tail,
// and every cache skip is visible in the RPC result.
namespace {

// Box + two independent boxes: three features, three bodies.
std::string make_three_box_part() {
    using namespace chudware::doc;
    const std::string id = make_box_doc();
    doc_apply_ops(id, -1, json::array({
        {{"op", "add_feature"}, {"feature", {
            {"type", "box"}, {"name", "Second"}, {"params", {
                {"length", 10}, {"width", 10}, {"height", 10},
                {"at", {60, 0, 0}}, {"mode", "new"}}}}}},
        {{"op", "add_feature"}, {"feature", {
            {"type", "box"}, {"name", "Third"}, {"params", {
                {"length", 8}, {"width", 8}, {"height", 8},
                {"at", {-60, 0, 0}}, {"mode", "new"}}}}}},
    }), false);
    return id;
}

constexpr double kThreeBoxVolume = 40.0 * 24.0 * 12.0 + 1000.0 + 512.0;

long long executed_since(const json& before) {
    return chudware::doc::compile_cache_stats().at("features_executed").get<long long>() -
           before.at("features_executed").get<long long>();
}

bool has_entry(const json& cache, const std::string& name) {
    for (const json& e : cache.at("entries"))
        if (e == name) return true;
    return false;
}

// Every member of a ZIP container, decompressed, by name.
std::map<std::string, std::string> zip_members(const std::filesystem::path& path) {
    mz_zip_archive zip;
    std::memset(&zip, 0, sizeof(zip));
    REQUIRE(mz_zip_reader_init_file(&zip, path.string().c_str(), 0));
    std::map<std::string, std::string> members;
    for (mz_uint i = 0; i < mz_zip_reader_get_num_files(&zip); ++i) {
        mz_zip_archive_file_stat st;
        REQUIRE(mz_zip_reader_file_stat(&zip, i, &st));
        std::size_t size = 0;
        void* data = mz_zip_reader_extract_to_heap(&zip, i, &size, 0);
        REQUIRE((data || size == 0));
        members[st.m_filename] = std::string(static_cast<const char*>(data), size);
        mz_free(data);
    }
    mz_zip_reader_end(&zip);
    return members;
}

// Rewrite a saved container with `edit` applied to its document/extra entries.
template <typename Edit>
void rewrite_container(const std::filesystem::path& path, Edit edit) {
    auto stored = chudware::doc::stepplus_open(path.string());
    edit(stored);
    chudware::doc::stepplus_save(stored.doc, path.string(), stored.extra);
}

}  // namespace

TEST_CASE("persisted build state: a saved part reopens warm and its first op executes no feature") {
    using namespace chudware::doc;
    const std::string id = make_three_box_part();
    AppearanceFiles files(id);
    const auto path = files.dir / "warm.step+";
    const json saved = doc_save(id, path.string());
    const json& cache = saved.at("cache");
    INFO(saved.dump());
    CHECK(cache.at("written") == true);
    CHECK(has_entry(cache, "cache.ckpt"));
    CHECK(has_entry(cache, "cache.brep"));
    CHECK(cache.at("checkpoints").at("written") == true);
    CHECK(cache.at("checkpoints").at("full_result") == true);
    CHECK(cache.at("checkpoints").at("checkpoints_written") == 3);
    REQUIRE(doc_close(id));

    const json before = compile_cache_stats();
    const json opened = doc_open(path.string());
    INFO(opened.at("cache").dump());
    CHECK(opened.at("cache_hit") == true);
    CHECK(opened.at("warm") == true);
    CHECK_FALSE(opened.contains("cache_miss_reason"));
    CHECK(opened.at("cache").at("hydrated_from") == "cache.ckpt");
    CHECK(opened.at("volume").get<double>() == Approx(kThreeBoxVolume));
    const std::string nid = opened.at("doc_id").get<std::string>();

    // The first activate (tab switch) and even a forced rebuild serve the restored build.
    const json activated = doc_activate(nid);
    CHECK(activated.at("rebuild").at("doc_status").at("features_executed") == 0);
    const json rebuilt = doc_rebuild(nid);
    CHECK(rebuilt.at("rebuild").at("doc_status").at("features_executed") == 0);
    CHECK(rebuilt.at("volume").get<double>() == Approx(kThreeBoxVolume));
    CHECK(executed_since(before) == 0);

    // An unedited re-save writes the same checkpoints back without a compile.
    const json resaved = doc_save(nid, path.string());
    CHECK(resaved.at("cache").at("checkpoints").at("written") == true);
    CHECK(executed_since(before) == 0);
    REQUIRE(doc_close(nid));
}

TEST_CASE("persisted build state: editing the last feature after reopen executes only the tail") {
    using namespace chudware::doc;
    const std::string id = make_three_box_part();
    AppearanceFiles files(id);
    const auto path = files.dir / "tail.step+";
    doc_save(id, path.string());
    const std::string last = doc_tree(id).at("tree").at("features").back().at("id");
    REQUIRE(doc_close(id));

    const json opened = doc_open(path.string());
    REQUIRE(opened.at("warm") == true);
    const std::string nid = opened.at("doc_id").get<std::string>();
    const json before = compile_cache_stats();
    const json edited = doc_apply_ops(nid, -1, json::array({
        {{"op", "edit_feature"}, {"id", last},
         {"params", {{"length", {{"value", 16}, {"expr", nullptr}}}}}},
    }), false);
    INFO(edited.at("rebuild").dump());
    CHECK(edited.at("rebuild").at("doc_status").at("features_executed") == 1);
    CHECK(edited.at("rebuild").at("doc_status").at("features_reused") == 2);
    CHECK(executed_since(before) == 1);
    CHECK(edited.at("volume").get<double>() == Approx(40.0 * 24.0 * 12.0 + 1000.0 + 16.0 * 8.0 * 8.0));
    REQUIRE(doc_close(nid));
}

TEST_CASE("persisted build state: a changed document hash is a reported miss") {
    using namespace chudware::doc;
    const std::string id = make_three_box_part();
    AppearanceFiles files(id);
    const auto path = files.dir / "renamed.step+";
    doc_save(id, path.string());
    REQUIRE(doc_close(id));
    // Edited outside Chudware (here: a rename), so the saved display cache no longer
    // describes document.json byte for byte.
    rewrite_container(path, [](StepPlusDoc& stored) { stored.doc["name"] = "RenamedOutside"; });

    const json before = compile_cache_stats();
    const json opened = doc_open(path.string());
    INFO(opened.at("cache").dump());
    CHECK(opened.at("cache_hit") == false);
    CHECK(opened.at("cache_miss_reason") == "doc_hash");
    // The features are unchanged, so the content-addressed checkpoints still serve the
    // build: the miss is reported, yet no feature re-executes.
    CHECK(opened.at("warm") == true);
    CHECK(opened.at("cache").at("features_executed") == 0);
    CHECK(executed_since(before) == 0);
    CHECK(opened.at("volume").get<double>() == Approx(kThreeBoxVolume));
    REQUIRE(doc_close(opened.at("doc_id").get<std::string>()));
}

TEST_CASE("persisted build state: a file saved before cache.ckpt still opens warm from cache.brep") {
    using namespace chudware::doc;
    const std::string id = make_three_box_part();
    AppearanceFiles files(id);
    const auto path = files.dir / "legacy.step+";
    doc_save(id, path.string());
    REQUIRE(doc_close(id));
    rewrite_container(path, [](StepPlusDoc& stored) { stored.extra.erase("cache.ckpt"); });

    const json before = compile_cache_stats();
    const json opened = doc_open(path.string());
    INFO(opened.at("cache").dump());
    CHECK(opened.at("cache_hit") == true);
    CHECK(opened.at("warm") == true);
    CHECK(opened.at("cache").at("hydrated_from") == "cache.brep");
    CHECK(opened.at("cache").at("checkpoints").at("reason") == "absent");
    const std::string nid = opened.at("doc_id").get<std::string>();
    // Activation, picking and edge expansion read the hydrated compile.
    const json activated = doc_activate(nid);
    CHECK(activated.at("rebuild").at("doc_status").at("features_executed") == 0);
    CHECK(activated.at("volume").get<double>() == Approx(kThreeBoxVolume));
    CHECK(activated.at("tess").at("bodies").size() == 3);
    CHECK_FALSE(doc_face_edges(nid, 1, 1).at("edges").empty());
    CHECK(executed_since(before) == 0);
    REQUIRE(doc_close(nid));
}

TEST_CASE("persisted build state: an inadmissible cache entry is a reported skip, never a failed open") {
    using namespace chudware::doc;
    const std::string id = make_three_box_part();
    AppearanceFiles files(id);
    const auto path = files.dir / "padded.step+";
    doc_save(id, path.string());
    REQUIRE(doc_close(id));
    // A cache.json whose deflate ratio exceeds the reader's bound: before, the archive
    // pre-scan rejected the whole document; now only the cache entry is skipped.
    rewrite_container(path, [](StepPlusDoc& stored) {
        json header = json::parse(stored.extra.at("cache.json"));
        header["padding"] = std::string(8u * 1024u * 1024u, ' ');
        stored.extra["cache.json"] = header.dump();
    });

    const json before = compile_cache_stats();
    json opened;
    REQUIRE_NOTHROW(opened = doc_open(path.string()));
    INFO(opened.at("cache").dump());
    CHECK(opened.at("cache_hit") == false);
    CHECK(opened.at("cache_miss_reason") == "budget");
    CHECK(opened.at("cache").at("entries").at("cache.json") == "skipped:compression_ratio");
    CHECK(opened.at("warm") == true);  // the admitted checkpoints still carry the build
    CHECK(executed_since(before) == 0);
    CHECK(opened.at("volume").get<double>() == Approx(kThreeBoxVolume));
    REQUIRE(doc_close(opened.at("doc_id").get<std::string>()));
}

TEST_CASE("persisted build state: a partially failed rebuild persists the succeeded prefix") {
    using namespace chudware::doc;
    const std::string id = make_box_doc();
    AppearanceFiles files(id);
    json tree = doc_tree(id).at("tree");
    REQUIRE(doc_close(id));
    tree["features"].push_back({{"id", "f90"}, {"type", "import"}, {"name", "Ghost"},
                                {"params", {{"source_path", "/no/such/dir/ghost-part.step"},
                                            {"format", "step"}}},
                                {"refs", json::object()}});
    const auto path = files.dir / "partial.step+";
    stepplus_save(tree, path.string(), {});
    const json first = doc_open(path.string());
    CHECK(first.at("cache_miss_reason") == "absent");
    const std::string fid = first.at("doc_id").get<std::string>();
    const json saved = doc_save(fid, path.string());
    INFO(saved.dump());
    CHECK(saved.at("cache").at("written") == false);
    CHECK(saved.at("cache").at("skipped_reason") == "feature_failed");
    CHECK(has_entry(saved.at("cache"), "cache.ckpt"));
    CHECK(saved.at("cache").at("checkpoints").at("reason") == "feature_failed");
    CHECK(saved.at("cache").at("checkpoints").at("checkpoints_written") == 1);
    REQUIRE(doc_close(fid));

    const json before = compile_cache_stats();
    const json reopened = doc_open(path.string());
    INFO(reopened.at("cache").dump());
    CHECK(reopened.at("cache_miss_reason") == "absent");
    CHECK(reopened.at("cache").at("checkpoints").at("checkpoints") == 1);
    // Only the failing feature is re-attempted; the box restores from its checkpoint.
    CHECK(reopened.at("cache").at("features_executed") == 1);
    CHECK(executed_since(before) == 1);
    CHECK(reopened.at("warnings").size() == 1);
    REQUIRE(doc_close(reopened.at("doc_id").get<std::string>()));
}

TEST_CASE("persisted build state: over-budget checkpoints drop oldest first, then report") {
    using namespace chudware::doc;
    const std::string id = make_three_box_part();
    const json tree = doc_tree(id).at("tree");
    REQUIRE(doc_close(id));
    auto cache = make_compile_cache();
    const auto compiled = compile_document(tree, json::object(), cache.get());
    REQUIRE(compiled.ok);
    const auto quality = ::geom::DisplayQuality::Fine;
    json full_report;
    const std::string full = compile_cache_encode(*cache, tree, json::object(), quality,
                                                  stepplus_cache_engine_key(), "hash",
                                                  std::size_t(1) << 30, full_report);
    REQUIRE(!full.empty());
    CHECK(full_report.at("checkpoints_written") == 3);
    CHECK_FALSE(full_report.contains("reason"));

    json trimmed_report;
    const std::string trimmed = compile_cache_encode(*cache, tree, json::object(), quality,
                                                     stepplus_cache_engine_key(), "hash",
                                                     full.size() - 1, trimmed_report);
    INFO(trimmed_report.dump());
    REQUIRE(!trimmed.empty());
    CHECK(trimmed.size() < full.size());
    CHECK(trimmed_report.at("reason") == "budget");
    CHECK(trimmed_report.at("checkpoints_dropped").get<int>() > 0);
    // The deepest checkpoint survives: a decoded trimmed cache still restores the tail.
    json decoded;
    auto restored = compile_cache_decode(trimmed, tree, json::object(), quality,
                                         stepplus_cache_engine_key(), nullptr, decoded);
    REQUIRE(restored);
    CHECK(decoded.at("matched_features") == 3);

    json none_report;
    CHECK(compile_cache_encode(*cache, tree, json::object(), quality, stepplus_cache_engine_key(),
                               "hash", 16, none_report).empty());
    CHECK(none_report.at("written") == false);
    CHECK(none_report.at("reason") == "budget");
    CHECK(none_report.at("checkpoints_dropped") == 3);
}

TEST_CASE("persisted build state: an open that rebuilds a stale cache saves the build into its file") {
    using namespace chudware::doc;
    const std::string id = make_three_box_part();
    AppearanceFiles files(id);
    const auto path = files.dir / "stale.step+";
    doc_save(id, path.string());
    REQUIRE(doc_close(id));
    // An older engine epoch built this state: neither the display nor checkpoints serve.
    rewrite_container(path, [](StepPlusDoc& stored) {
        json header = json::parse(stored.extra.at("cache.json"));
        header["engine"] = "chudware-geomcache/0;occt=stale";
        stored.extra["cache.json"] = header.dump();
        stored.extra.erase("cache.ckpt");
        stored.extra["notes/readme.txt"] = "kept verbatim";
    });
    const auto authored = zip_members(path);

    const json before = compile_cache_stats();
    const json opened = doc_open(path.string());
    INFO(opened.at("cache").dump());
    CHECK(opened.at("cache_miss_reason") == "engine_key");
    CHECK(executed_since(before) == 3);
    CHECK(opened.at("cache").at("writeback").at("written") == true);
    REQUIRE(doc_close(opened.at("doc_id").get<std::string>()));

    // Only build state changed: the document, its metadata and every other member keep
    // their authored bytes.
    const auto rewritten = zip_members(path);
    for (const char* name : {"document.json", "meta.json", "thumbnail.png", "notes/readme.txt"})
        CHECK(rewritten.at(name) == authored.at(name));
    CHECK(rewritten.count("cache.ckpt") == 1);
    CHECK(json::parse(rewritten.at("cache.json")).at("engine") == stepplus_cache_engine_key());

    const json warm_before = compile_cache_stats();
    const json reopened = doc_open(path.string());
    INFO(reopened.at("cache").dump());
    CHECK(reopened.at("cache_hit") == true);
    CHECK(reopened.at("cache").at("hydrated_from") == "cache.ckpt");
    CHECK_FALSE(reopened.at("cache").contains("writeback"));
    CHECK(executed_since(warm_before) == 0);
    REQUIRE(doc_close(reopened.at("doc_id").get<std::string>()));
}

TEST_CASE("persisted build state: a write-back never replaces a file that changed after its read") {
    using namespace chudware::doc;
    namespace fs = std::filesystem;
    const std::string id = make_box_doc();
    AppearanceFiles files(id);
    const auto path = files.dir / "guarded.step+";
    doc_save(id, path.string());
    REQUIRE(doc_close(id));
    const auto authored = zip_members(path);
    const std::string hash = stepplus_doc_hash(stepplus_open(path.string()).doc);
    const std::map<std::string, std::string> entries = {{"cache.json", "{}"}};
    const NativeFileStamp stamp = stepplus_file_stamp(path.string());
    REQUIRE(stamp.valid);

    // The build was for another tree (an edit that kept size and mtime).
    BuildStateWriteback refused =
        stepplus_write_build_state(path.string(), stamp, "another-document", entries);
    CHECK_FALSE(refused.written);
    CHECK(refused.reason == "document_changed");
    // A save landed after the read.
    fs::last_write_time(path, fs::last_write_time(path) + std::chrono::seconds(2));
    refused = stepplus_write_build_state(path.string(), stamp, hash, entries);
    CHECK_FALSE(refused.written);
    CHECK(refused.reason == "file_changed");

    CHECK(zip_members(path) == authored);
    for (const auto& entry : fs::directory_iterator(files.dir))
        CHECK(entry.path().filename() == path.filename());
}

TEST_CASE("persisted assembly cache: a rebuilt component saves its build and the assembly stays warm") {
    using namespace chudware::doc;
    namespace fs = std::filesystem;
    const std::string part = make_box_doc();
    AppearanceFiles files(part);
    const auto part_path = files.dir / "leaf.step+";
    doc_save(part, part_path.string());
    REQUIRE(doc_close(part));
    const std::string assembly = doc_new("assembly", "WriteBackAssembly").at("doc_id");
    doc_apply_ops(assembly, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", part_path.string()}}},
         {"fixed", true}},
    }), false);
    const auto path = files.dir / "assembly.step+";
    doc_save(assembly, path.string());
    REQUIRE(doc_close(assembly));
    // Both files carry state from an older engine epoch.
    auto stale = [](StepPlusDoc& stored) {
        json header = json::parse(stored.extra.at("cache.json"));
        header["engine"] = "chudware-geomcache/0;occt=stale";
        stored.extra["cache.json"] = header.dump();
        stored.extra.erase("cache.ckpt");
    };
    rewrite_container(part_path, stale);
    rewrite_container(path, stale);

    const json opened = doc_open(path.string());
    INFO(opened.at("cache").dump());
    CHECK(opened.at("cache_miss_reason") == "engine_key");
    const json& wb = opened.at("cache").at("writeback");
    CHECK(wb.at("written") == true);
    REQUIRE(wb.at("components").size() == 1);
    CHECK(wb.at("components")[0].at("build_state").at("written") == true);
    REQUIRE(doc_close(opened.at("doc_id").get<std::string>()));

    // The component now opens warm on its own.
    const json leaf = doc_open(part_path.string());
    CHECK(leaf.at("cache_hit") == true);
    REQUIRE(doc_close(leaf.at("doc_id").get<std::string>()));
    // A rewrite of the component that keeps its document (here: only its mtime moves) does
    // not invalidate the assembly's saved display.
    fs::last_write_time(part_path, fs::last_write_time(part_path) + std::chrono::seconds(2));
    const json reopened = doc_open(path.string());
    INFO(reopened.at("cache").dump());
    CHECK(reopened.at("cache_hit") == true);
    REQUIRE(doc_close(reopened.at("doc_id").get<std::string>()));
}

TEST_CASE("persisted assembly cache: detached imports retain warm display and detect changes") {
    using namespace chudware::doc;
    const std::string part = make_box_doc();
    AppearanceFiles files(part);
    const auto part_path = files.dir / "leaf.step+";
    set_appearance(part, {{"color", {0.9, 0.05, 0.05}}});
    doc_save(part, part_path.string());
    const auto old_time = std::filesystem::last_write_time(part_path);
    const std::string assembly = doc_new("assembly", "DetachedCachedAssembly").at("doc_id");
    doc_apply_ops(assembly, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", part_path.string()}}},
         {"fixed", true}},
    }), false);
    const auto path = files.dir / "assembly.step+";
    doc_save(assembly, path.string());
    const auto stored = stepplus_open(path.string());
    REQUIRE(stored.doc["components"][0]["source"]["path"] == "leaf.step+");

    // Keep the original open: detaching must also give the imported copy a unique
    // display name, neither invalidating the saved display nor changing its source.
    const json opened = doc_open(path.string(), false, true);
    REQUIRE_MESSAGE(opened.value("cache_hit", false), opened.dump());
    CHECK(opened["tree"]["name"] != stored.doc["name"]);
    CHECK(opened["tree"]["components"][0]["source"]["path"] == part_path.string());
    CHECK(opened["tess"]["bodies"][0]["color"][0].get<float>() == Approx(0.9f));
    REQUIRE(doc_close(opened.at("doc_id").get<std::string>()));

    SUBCASE("changed dependency still invalidates the display") {
        set_appearance(part, {{"color", {0.05, 0.05, 0.9}}});
        doc_save(part, part_path.string());
        std::filesystem::last_write_time(part_path, old_time + std::chrono::seconds(2));
        const json changed = doc_open(path.string(), false, true);
        CHECK_FALSE(changed.value("cache_hit", false));
        CHECK(changed["cache_miss_reason"] == "components_changed");
        CHECK(changed["tess"]["bodies"][0]["color"][2].get<float>() == Approx(0.9f));
        REQUIRE(doc_close(changed.at("doc_id").get<std::string>()));
    }
    SUBCASE("changed authored tree still invalidates the display") {
        rewrite_container(path, [](StepPlusDoc& document) {
            document.doc["components"][0]["transform"][3] = 12.0;
        });
        const json changed = doc_open(path.string(), false, true);
        CHECK_FALSE(changed.value("cache_hit", false));
        CHECK(changed["cache_miss_reason"] == "doc_hash");
        REQUIRE(doc_close(changed.at("doc_id").get<std::string>()));
    }
    REQUIRE(doc_close(assembly));
    REQUIRE(doc_close(part));
}

TEST_CASE("cache provenance: local saved parts reopen verified with zero executed features") {
    using namespace chudware::doc;
    const auto id = make_three_box_part();
    AppearanceFiles files(id);
    const auto path = files.dir / "authenticated.step+";
    doc_save(id, path.string());
    const auto saved = stepplus_open(path.string());
    REQUIRE(saved.extra.count("cache.provenance") == 1);
    CHECK(stepplus_cache_verified(saved.doc, saved.extra));
    REQUIRE(doc_close(id));
    const auto before = compile_cache_stats();
    const auto opened = doc_open(path.string());
    CHECK(opened.at("cache_trust") == "verified");
    CHECK(opened.at("warm") == true);
    CHECK(opened.at("cache").at("features_executed") == 0);
    CHECK(executed_since(before) == 0);
    CHECK(doc_activate(opened.at("doc_id")).at("rebuild").at("doc_status").at("features_executed") == 0);
    REQUIRE(doc_close(opened.at("doc_id")));
}

TEST_CASE("cache provenance: a foreign hybrid displays immediately then replaces and authenticates") {
    using namespace chudware::doc;
    const auto source = make_box_doc();
    const auto donor = make_box_doc();
    AppearanceFiles files(source);
    const auto target = files.dir / "hybrid.step+";
    const auto donor_path = files.dir / "donor.step+";
    const std::string feature = doc_tree(donor).at("tree").at("features").at(0).at("id");
    const auto donor_response = doc_apply_ops(donor, -1, json::array({
        {{"op", "edit_feature"}, {"id", feature}, {"params", {{"length", 80.0}}}},
    }), false);
    doc_save(source, target.string());
    doc_save(donor, donor_path.string());
    REQUIRE(doc_close(source));
    REQUIRE(doc_close(donor));
    auto hybrid = zip_members(target);
    const auto foreign = zip_members(donor_path);
    hybrid["cache.aicm"] = foreign.at("cache.aicm");
    hybrid["cache.brep"] = foreign.at("cache.brep");
    hybrid.erase("cache.ckpt");
    json header = json::parse(foreign.at("cache.json"));
    header["doc_hash"] = stepplus_doc_hash(json::parse(hybrid.at("document.json")));
    hybrid["cache.json"] = header.dump();
    // Deliberately retain the original MAC: raw ZIP assembly must not re-sign the hybrid.
    mz_zip_archive archive{};
    REQUIRE(mz_zip_writer_init_file(&archive, target.string().c_str(), 0));
    for (const auto& [name, bytes] : hybrid)
        REQUIRE(mz_zip_writer_add_mem(&archive, name.c_str(), bytes.data(), bytes.size(), MZ_NO_COMPRESSION));
    REQUIRE(mz_zip_writer_finalize_archive(&archive));
    mz_zip_writer_end(&archive);
    const auto imported = stepplus_open(target.string());
    CHECK_FALSE(stepplus_cache_verified(imported.doc, imported.extra));

    struct RefreshSinkGuard {
        std::shared_ptr<std::promise<void>> release = std::make_shared<std::promise<void>>();
        ~RefreshSinkGuard() {
            try { release->set_value(); } catch (...) {}
            set_cache_refresh_sink({});
        }
    } guard;
    auto promise = std::make_shared<std::promise<json>>();
    auto refreshed = promise->get_future();
    const auto release = guard.release->get_future().share();
    set_cache_refresh_sink([promise, release](const std::string&, const json& result) {
        promise->set_value(result);
        release.wait();  // publication itself remains lifecycle work
    });
    const json opened = doc_open(target.string());
    CHECK(opened.at("cache_hit") == true);
    CHECK(opened.at("cache_trust") == "unverified");
    CHECK(opened.at("cache_verification_pending") == true);
    CHECK(opened.at("cache").at("features_executed") == 0);
    CHECK(opened.at("volume") == donor_response.at("volume"));
    REQUIRE(refreshed.wait_for(std::chrono::seconds(60)) == std::future_status::ready);
    const json replacement = refreshed.get();
    INFO(replacement.dump());
    std::uint64_t cache_active = 0, dirty = 0;
    const auto activity_deadline = std::chrono::steady_clock::now() + std::chrono::seconds(60);
    while (!activity_counts(cache_active, dirty)) {
        REQUIRE(std::chrono::steady_clock::now() < activity_deadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    CHECK(cache_active >= 1);
    guard.release->set_value();
    CHECK(replacement.at("cache_trust") == "verified");
    CHECK(replacement.at("mismatch") == true);
    CHECK(replacement.at("volume").get<double>() == Approx(40.0 * 24.0 * 12.0));
    REQUIRE(!replacement.at("warnings").empty());
    CHECK(replacement.at("cache_verification").at("writeback").at("written") == true);
    const auto rebuilt = stepplus_open(target.string());
    CHECK(stepplus_cache_verified(rebuilt.doc, rebuilt.extra));
    const std::string id = opened.at("doc_id");
    CHECK(doc_activate(id).at("volume") == replacement.at("volume"));
    REQUIRE(doc_close(id));
    const auto warm = doc_open(target.string());
    CHECK(warm.at("cache_trust") == "verified");
    CHECK(warm.at("cache").at("features_executed") == 0);
    REQUIRE(doc_close(warm.at("doc_id")));
}

TEST_CASE("cache provenance: foreign assembly display stays warm until its background refresh") {
    using namespace chudware::doc;
    const auto part = make_box_doc();
    AppearanceFiles files(part);
    const auto leaf = files.dir / "leaf.step+";
    doc_save(part, leaf.string());
    REQUIRE(doc_close(part));
    const auto component = stepplus_open_component(leaf.string());
    CHECK(component.extra.count("cache.aicm") == 0);
    CHECK(component.extra.count("cache.ckpt") == 0);
    CHECK(stepplus_cache_verified(component.doc, component.extra, true));
    auto changed_member = component.extra;
    changed_member["cache.brep"].back() ^= 1;
    CHECK_FALSE(stepplus_cache_verified(component.doc, changed_member, true));
    const std::string assembly = doc_new("assembly", "AuthenticatedAssembly").at("doc_id");
    doc_apply_ops(assembly, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", leaf.string()}}}, {"fixed", true}},
    }), false);
    const auto path = files.dir / "assembly.step+";
    doc_save(assembly, path.string());
    REQUIRE(doc_close(assembly));
    auto members = zip_members(path);
    members.erase("cache.provenance");
    mz_zip_archive archive{};
    REQUIRE(mz_zip_writer_init_file(&archive, path.string().c_str(), 0));
    for (const auto& [name, bytes] : members)
        REQUIRE(mz_zip_writer_add_mem(&archive, name.c_str(), bytes.data(), bytes.size(),
                                     MZ_NO_COMPRESSION));
    REQUIRE(mz_zip_writer_finalize_archive(&archive));
    mz_zip_writer_end(&archive);
    struct RefreshSinkGuard { ~RefreshSinkGuard() { set_cache_refresh_sink({}); } } guard;
    auto promise = std::make_shared<std::promise<json>>();
    auto refreshed = promise->get_future();
    set_cache_refresh_sink([promise](const std::string&, const json& result) {
        promise->set_value(result);
    });
    const auto before = compile_cache_stats();
    const auto opened = doc_open(path.string());
    CHECK(opened.at("cache_hit") == true);
    CHECK(opened.at("warm") == true);
    CHECK(opened.at("cache_trust") == "unverified");
    REQUIRE(opened.at("tess").at("bodies").size() == 1);
    REQUIRE(refreshed.wait_for(std::chrono::seconds(60)) == std::future_status::ready);
    const auto replacement = refreshed.get();
    INFO(replacement.dump());
    CHECK(replacement.at("cache_trust") == "verified");
    CHECK(replacement.at("cache_verification").at("completed") == true);
    CHECK(replacement.at("cache_verification").at("writeback").at("written") == true);
    CHECK(executed_since(before) == 0);
    const auto authenticated = stepplus_open(path.string());
    CHECK(stepplus_cache_verified(authenticated.doc, authenticated.extra));
    REQUIRE(doc_close(opened.at("doc_id").get<std::string>()));
    const auto warm = doc_open(path.string());
    CHECK(warm.at("cache_trust") == "verified");
    CHECK(warm.at("cache_hit") == true);
    REQUIRE(doc_close(warm.at("doc_id").get<std::string>()));
}

TEST_CASE("foreign admitted checkpoints reopen warm when an unrelated display header is skipped") {
    using namespace chudware::doc;
    const auto id = make_three_box_part();
    AppearanceFiles files(id);
    const auto path = files.dir / "foreign-checkpoints.step+";
    doc_save(id, path.string());
    REQUIRE(doc_close(id));
    auto members = zip_members(path);
    auto header = json::parse(members.at("cache.json"));
    header["padding"] = std::string(8u * 1024u * 1024u, ' ');
    members["cache.json"] = header.dump();
    members.erase("cache.provenance");
    mz_zip_archive archive{};
    REQUIRE(mz_zip_writer_init_file(&archive, path.string().c_str(), 0));
    for (const auto& [name, bytes] : members)
        REQUIRE(mz_zip_writer_add_mem(&archive, name.c_str(), bytes.data(), bytes.size(),
            name == "cache.json" ? MZ_BEST_COMPRESSION : MZ_NO_COMPRESSION));
    REQUIRE(mz_zip_writer_finalize_archive(&archive));
    mz_zip_writer_end(&archive);
    const auto loaded = stepplus_open(path.string());
    REQUIRE(loaded.skipped.at("cache.json") == "compression_ratio");
    CHECK_FALSE(stepplus_cache_verified(loaded.doc, loaded.extra));
    struct RefreshSinkGuard { ~RefreshSinkGuard() { set_cache_refresh_sink({}); } } guard;
    auto promise = std::make_shared<std::promise<json>>();
    auto refreshed = promise->get_future();
    set_cache_refresh_sink([promise](const std::string&, const json& result) {
        promise->set_value(result);
    });
    const auto opened = doc_open(path.string());
    CHECK(opened.at("cache_hit") == false);
    CHECK(opened.at("warm") == true);
    CHECK(opened.at("cache_trust") == "unverified");
    CHECK(opened.at("cache_verification_pending") == true);
    CHECK(opened.at("cache").at("features_executed") == 0);
    CHECK(opened.at("volume").get<double>() == Approx(kThreeBoxVolume));
    REQUIRE(refreshed.wait_for(std::chrono::seconds(60)) == std::future_status::ready);
    const auto replacement = refreshed.get();
    CHECK(replacement.at("cache_trust") == "verified");
    CHECK(replacement.at("cache_verification").at("writeback").at("written") == true);
    REQUIRE(doc_close(opened.at("doc_id").get<std::string>()));
}

TEST_CASE("native activity counts dirty documents until successful save or close") {
    using namespace chudware::doc;
    std::uint64_t cache = 0, before = 0, dirty = 0;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(60);
    while (!activity_counts(cache, before) || cache != 0) {
        REQUIRE(std::chrono::steady_clock::now() < deadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    const auto id = make_box_doc();
    AppearanceFiles files(id);
    REQUIRE(activity_counts(cache, dirty));
    CHECK(dirty == before + 1);
    doc_save(id, (files.dir / "activity.step+").string());
    REQUIRE(activity_counts(cache, dirty));
    CHECK(dirty == before);
    doc_set_name(id, "changed");
    REQUIRE(activity_counts(cache, dirty));
    CHECK(dirty == before + 1);
    REQUIRE(doc_close(id));
    REQUIRE(activity_counts(cache, dirty));
    CHECK(dirty == before);
}

TEST_CASE("drawing activity clears only for its successfully saved exact revision") {
    using namespace chudware::doc;
    std::uint64_t before = 0, dirty = 0;
    REQUIRE(dirty_drawing_count(before));
    const auto source = make_box_doc();
    AppearanceFiles files(source);
    const auto drawing = drawing_new(json::object(), json::object(), "ActivityDrawing", source)
                             .at("doc_id").get<std::string>();
    REQUIRE(dirty_drawing_count(dirty));
    CHECK(dirty == before + 1);
    const auto path = (files.dir / "drawing-activity.step+").string();
    doc_save(source, path);
    dirty = 0; REQUIRE(dirty_drawing_count(dirty)); CHECK(dirty == before);
    drawing_set_units(drawing, "IPS");
    doc_save(source, (files.dir / "snapshot.step+").string(), true);
    dirty = 0; REQUIRE(dirty_drawing_count(dirty)); CHECK(dirty == before + 1);
    DrawingSaveRevisions captured;
    drawings_for_source(source, captured);
    drawing_set_units(drawing, "MMGS");  // races an earlier save snapshot
    drawings_saved(captured);
    dirty = 0; REQUIRE(dirty_drawing_count(dirty)); CHECK(dirty == before + 1);
    doc_save(source, path);
    dirty = 0; REQUIRE(dirty_drawing_count(dirty)); CHECK(dirty == before);
    REQUIRE(drawing_close(drawing));
    REQUIRE(doc_close(source));
}
