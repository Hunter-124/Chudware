// Document-native mesh references are display-only, self-contained attachments.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <set>
#include <string>

#include <nlohmann/json.hpp>

#include "doc/formats.h"
#include "doc/session.h"
#include "doc/stepplus.h"

using json = nlohmann::json;
namespace doc = chudware::doc;
namespace fs = std::filesystem;

namespace {

constexpr double kDensity = 2.7;
constexpr double kBoxVolume = 8000.0;

struct Fixture {
    fs::path dir;
    fs::path source;
    std::set<std::string> docs;

    Fixture() {
        static std::atomic<unsigned> sequence{0};
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        dir = fs::temp_directory_path() / ("chudware_mesh_reference_" +
            std::to_string(stamp) + "_" + std::to_string(sequence++));
        REQUIRE(fs::create_directory(dir));
        source = dir / "tetra.ply";
        std::ofstream out(source, std::ios::binary);
        out << "ply\n"
               "format ascii 1.0\n"
               "element vertex 4\n"
               "property float x\n"
               "property float y\n"
               "property float z\n"
               "element face 4\n"
               "property list uchar int vertex_indices\n"
               "end_header\n"
               "0 0 0\n1 0 0\n0 2 0\n0 0 3\n"
               "3 0 2 1\n3 0 1 3\n3 0 3 2\n3 1 2 3\n";
        out.close();
        REQUIRE(out.good());
    }

    ~Fixture() {
        for (const auto& id : docs) doc::doc_close(id);
        std::error_code ignored;
        fs::remove_all(dir, ignored);
    }

    std::string remember(const json& response) {
        const std::string id = response.at("doc_id");
        docs.insert(id);
        return id;
    }

    bool close(const std::string& id) {
        if (!doc::doc_close(id)) return false;
        docs.erase(id);
        return true;
    }

    std::string box() {
        const std::string id = remember(doc::doc_new("part", "MeshReferenceTest"));
        const json built = doc::doc_apply_ops(id, -1, json::array({
            {{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Block"},
                {"params", {{"length", 20.0}, {"width", 20.0}, {"height", 20.0}}}}}}
        }), false);
        REQUIRE(built.at("rebuild").at("ok") == true);
        REQUIRE(built.at("volume").get<double>() == doctest::Approx(kBoxVolume));
        return id;
    }
};

json initial_transform() {
    return {{"mm_per_unit", 2.0},
            {"rotation", json::array({json::array({0, -1, 0}),
                                       json::array({1, 0, 0}),
                                       json::array({0, 0, 1})})},
            {"translation_mm", json::array({100, 200, 300})}};
}

json moved_transform() {
    return {{"mm_per_unit", 0.5},
            {"rotation", json::array({json::array({1, 0, 0}),
                                       json::array({0, 1, 0}),
                                       json::array({0, 0, 1})})},
            {"translation_mm", json::array({-4, 5, 6})}};
}

void check_vector(const json& actual, const std::array<double, 3>& expected) {
    REQUIRE(actual.is_array());
    REQUIRE(actual.size() == 3);
    for (std::size_t i = 0; i < 3; ++i)
        CHECK(std::abs(actual.at(i).get<double>() - expected[i]) < 1e-6);
}

void check_bbox(const json& bbox, const std::array<double, 3>& lo,
                const std::array<double, 3>& hi) {
    check_vector(bbox.at("min"), lo);
    check_vector(bbox.at("max"), hi);
    check_vector(bbox.at("size"), {hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]});
}

void check_mesh(const json& mesh, const std::array<double, 12>& expected) {
    CHECK(mesh.at("ok") == true);
    const json& vertices = mesh.at("vertices_mm");
    REQUIRE(vertices.is_array());
    REQUIRE(vertices.size() == expected.size());
    for (std::size_t i = 0; i < expected.size(); ++i)
        CHECK(std::abs(vertices.at(i).get<double>() - expected[i]) < 1e-6);
    CHECK(mesh.at("indices") == json::array({0, 2, 1, 0, 1, 3, 0, 3, 2, 1, 2, 3}));
}

void check_page(const json& page, const json& full, std::size_t offset, std::size_t count) {
    CHECK(page.at("ok") == true);
    CHECK(page.at("total_tris") == 4);
    CHECK(page.at("offset_tris") == offset);
    const json& vertices = page.at("vertices_mm");
    const json& indices = page.at("indices");
    REQUIRE(vertices.is_array());
    REQUIRE(vertices.size() % 3 == 0);
    REQUIRE(indices.is_array());
    REQUIRE(indices.size() == count * 3);
    for (std::size_t i = 0; i < indices.size(); ++i) {
        const auto local = indices.at(i).get<std::size_t>();
        const auto original = full.at("indices").at(offset * 3 + i).get<std::size_t>();
        REQUIRE(local < vertices.size() / 3);
        for (std::size_t axis = 0; axis < 3; ++axis)
            CHECK(std::abs(vertices.at(local * 3 + axis).get<double>() -
                           full.at("vertices_mm").at(original * 3 + axis).get<double>()) < 1e-6);
    }
}

void check_solid(const std::string& id, double expected_volume) {
    const json mass = doc::doc_eval_mass(id, 0, kDensity);
    CHECK(mass.at("body_count") == 1);
    CHECK(mass.at("volume_mm3").get<double>() == doctest::Approx(expected_volume).epsilon(1e-8));
    CHECK(mass.at("mass_g").get<double>() == doctest::Approx(expected_volume * kDensity * 0.001).epsilon(1e-8));
    CHECK(doc::doc_tessellate_bodies(id).size() == 1);
    check_bbox(doc::doc_bbox(id), {-10, -10, -10}, {10, 10, 10});
}

json single_ref(const std::string& id, const std::string& ref_id) {
    const json listed = doc::doc_mesh_ref_list(id);
    CHECK(listed.at("ok") == true);
    REQUIRE(listed.at("refs").is_array());
    REQUIRE(listed.at("refs").size() == 1);
    const json ref = listed.at("refs").at(0);
    CHECK(ref.at("id") == ref_id);
    CHECK(ref.at("triangles") == 4);
    return ref;
}

void check_empty(const std::string& id) {
    const json listed = doc::doc_mesh_ref_list(id);
    CHECK(listed.at("ok") == true);
    REQUIRE(listed.at("refs").is_array());
    CHECK(listed.at("refs").empty());
}

json doc_summary(const std::string& id) {
    const json listed = doc::doc_list();
    for (const json& row : listed.at("docs"))
        if (row.at("doc_id") == id) return row;
    FAIL("document missing from doc_list");
    return json::object();
}

void check_exported_stl(const fs::path& path) {
    const json objects = doc::mesh_read_raw(path.string(), "stl").at("objects");
    REQUIRE_FALSE(objects.empty());
    std::array<double, 3> lo = {1e100, 1e100, 1e100};
    std::array<double, 3> hi = {-1e100, -1e100, -1e100};
    std::size_t triangles = 0;
    for (const json& object : objects) {
        const json& vertices = object.at("verts");
        const json& indices = object.at("tris");
        REQUIRE(vertices.size() % 3 == 0);
        REQUIRE(indices.size() % 3 == 0);
        triangles += indices.size() / 3;
        for (std::size_t i = 0; i < vertices.size(); ++i) {
            const double value = vertices.at(i).get<double>();
            const std::size_t axis = i % 3;
            lo[axis] = std::min(lo[axis], value);
            hi[axis] = std::max(hi[axis], value);
        }
    }
    CHECK(triangles > 0);
    for (std::size_t axis = 0; axis < 3; ++axis) {
        CHECK(std::abs(lo[axis] + 10.0) < 1e-4);
        CHECK(std::abs(hi[axis] - 10.0) < 1e-4);
    }
}

}  // namespace

TEST_CASE("transformed PLY reference survives source deletion and cached reopen") {
    Fixture f;
    std::string id = f.box();
    check_solid(id, kBoxVolume);
    check_empty(id);
    const json before_estimate = doc::doc_export_estimate(id);
    const long before_revision = doc::doc_tree(id).at("revision");
    const json solid_features = doc::doc_tree(id).at("tree").at("features");

    const json added = doc::doc_mesh_ref_add(id, {
        {"path", f.source.string()}, {"name", "Scanned tetra"}, {"transform", initial_transform()}});
    REQUIRE(added.at("ok") == true);
    const std::string ref_id = added.at("id");
    REQUIRE_FALSE(ref_id.empty());
    CHECK(added.at("revision").get<long>() == before_revision + 1);
    CHECK(added.at("triangles") == 4);
    check_bbox(added.at("bbox_mm"), {96, 200, 300}, {100, 202, 306});
    const json listed = doc::doc_mesh_ref_list(id);
    CHECK(listed.at("revision") == added.at("revision"));
    const json ref = single_ref(id, ref_id);
    CHECK(ref.at("name") == "Scanned tetra");
    CHECK(ref.at("transform") == initial_transform());
    check_bbox(ref.at("bbox_mm"), {96, 200, 300}, {100, 202, 306});
    const json original_mesh = doc::doc_mesh_ref_mesh(id, {{"id", ref_id}});
    check_mesh(original_mesh, {100, 200, 300, 100, 202, 300,
                               96, 200, 300, 100, 200, 306});
    CHECK(original_mesh.at("revision") == added.at("revision"));
    CHECK(original_mesh.at("total_tris") == 4);
    CHECK(original_mesh.at("offset_tris") == 0);
    CHECK(original_mesh.at("next_offset_tris").is_null());
    // Pages contain local indices, but reconstruct exactly the same transformed triangles.
    const json first_page = doc::doc_mesh_ref_mesh(id, {
        {"id", ref_id}, {"offset_tris", 0}, {"max_tris", 2},
        {"expected_revision", added.at("revision")}});
    check_page(first_page, original_mesh, 0, 2);
    CHECK(first_page.at("vertices_mm").size() == 12);  // Shared page vertices stay shared.
    CHECK(first_page.at("revision") == added.at("revision"));
    REQUIRE(first_page.at("next_offset_tris") == 2);
    const json second_page = doc::doc_mesh_ref_mesh(id, {
        {"id", ref_id}, {"offset_tris", first_page.at("next_offset_tris")}, {"max_tris", 2},
        {"expected_revision", first_page.at("revision")}});
    check_page(second_page, original_mesh, 2, 2);
    CHECK(second_page.at("vertices_mm").size() == 12);
    CHECK(second_page.at("next_offset_tris").is_null());
    const json end_page = doc::doc_mesh_ref_mesh(id, {
        {"id", ref_id}, {"offset_tris", 4}, {"max_tris", 2}});
    check_page(end_page, original_mesh, 4, 0);
    CHECK(end_page.at("next_offset_tris").is_null());
    CHECK_THROWS_AS(doc::doc_mesh_ref_mesh(id, {
        {"id", ref_id}, {"offset_tris", 5}, {"max_tris", 2}}), doc::OpError);
    CHECK_THROWS_AS(doc::doc_mesh_ref_mesh(id, {
        {"id", ref_id}, {"offset_tris", 0}, {"max_tris", 0}}), doc::OpError);
    try {
        doc::doc_mesh_ref_mesh(id, {{"id", ref_id}, {"expected_revision", before_revision}});
        FAIL("stale mesh revision was accepted");
    } catch (const doc::OpError& error) {
        CHECK(error.code == "stale_revision");
    }
    CHECK(doc::doc_tree(id).at("revision") == added.at("revision"));
    CHECK(doc::doc_tree(id).at("tree").at("features") == solid_features);
    check_solid(id, kBoxVolume);
    const json after_estimate = doc::doc_export_estimate(id);
    CHECK(after_estimate.at("bodies") == before_estimate.at("bodies"));
    CHECK(after_estimate.at("vertices") == before_estimate.at("vertices"));
    CHECK(after_estimate.at("triangles") == before_estimate.at("triangles"));

    const fs::path saved = f.dir / "part.step+";
    CHECK(doc::doc_save(id, saved.string()).at("cache").at("written") == true);
    REQUIRE(f.close(id));
    REQUIRE(fs::remove(f.source));
    const json opened = doc::doc_open(saved.string());
    id = f.remember(opened);
    CHECK(opened.at("cache_hit") == true);
    CHECK(opened.at("warm") == true);
    CHECK(opened.at("cache").at("features_executed") == 0);
    const json restored = single_ref(id, ref_id);
    CHECK(restored.at("name") == "Scanned tetra");
    CHECK(restored.at("transform") == initial_transform());
    check_bbox(restored.at("bbox_mm"), {96, 200, 300}, {100, 202, 306});
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}),
               {100, 200, 300, 100, 202, 300, 96, 200, 300, 100, 200, 306});
    check_solid(id, kBoxVolume);

    // All presentation edits are durable and undoable without the source file.
    const long rev = doc::doc_tree(id).at("revision");
    const json edited = doc::doc_mesh_ref_update(id, {
        {"id", ref_id}, {"name", "Aligned scan"}, {"visible", false},
        {"opacity", 0.65}, {"color", json::array({0.2, 0.4, 0.6})},
        {"transform", moved_transform()}});
    CHECK(edited.at("ok") == true);
    CHECK(edited.at("revision").get<long>() == rev + 1);
    const json moved = single_ref(id, ref_id);
    CHECK(moved.at("name") == "Aligned scan");
    CHECK(moved.at("visible") == false);
    CHECK(moved.at("opacity").get<double>() == doctest::Approx(0.65));
    CHECK(moved.at("color") == json::array({0.2, 0.4, 0.6}));
    CHECK(moved.at("transform") == moved_transform());
    check_bbox(moved.at("bbox_mm"), {-4, 5, 6}, {-3.5, 6, 7.5});
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}),
               {-4, 5, 6, -3.5, 5, 6, -4, 6, 6, -4, 5, 7.5});
    CHECK(doc::doc_undo(id).at("revision").get<long>() == rev + 2);
    const json undone = single_ref(id, ref_id);
    CHECK(undone.at("name") == "Scanned tetra");
    CHECK(undone.at("visible") == true);
    CHECK(undone.at("opacity").get<double>() == doctest::Approx(0.4));
    CHECK(undone.at("color") == json::array({0.45, 0.65, 0.85}));
    CHECK(undone.at("transform") == initial_transform());
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}),
               {100, 200, 300, 100, 202, 300, 96, 200, 300, 100, 200, 306});
    CHECK(doc::doc_redo(id).at("revision").get<long>() == rev + 3);
    const json redone = single_ref(id, ref_id);
    CHECK(redone.at("name") == "Aligned scan");
    CHECK(redone.at("visible") == false);
    CHECK(redone.at("opacity").get<double>() == doctest::Approx(0.65));
    CHECK(redone.at("color") == json::array({0.2, 0.4, 0.6}));
    CHECK(redone.at("transform") == moved_transform());
    check_solid(id, kBoxVolume);

    CHECK(doc::doc_save(id, saved.string()).at("cache").at("written") == true);
    REQUIRE(f.close(id));
    const json reopened = doc::doc_open(saved.string());
    id = f.remember(reopened);
    CHECK(reopened.at("cache_hit") == true);
    CHECK(reopened.at("cache").at("features_executed") == 0);
    const json reread = single_ref(id, ref_id);
    CHECK(reread.at("name") == "Aligned scan");
    CHECK(reread.at("visible") == false);
    CHECK(reread.at("color") == json::array({0.2, 0.4, 0.6}));
    CHECK(reread.at("opacity").get<double>() == doctest::Approx(0.65));
    check_bbox(reread.at("bbox_mm"), {-4, 5, 6}, {-3.5, 6, 7.5});
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}),
               {-4, 5, 6, -3.5, 5, 6, -4, 6, 6, -4, 5, 7.5});
    check_solid(id, kBoxVolume);
}

TEST_CASE("add and remove each undo and redo with their embedded mesh intact") {
    Fixture f;
    const std::string id = f.box();
    const long rev = doc::doc_tree(id).at("revision");
    const json added = doc::doc_mesh_ref_add(id, {{"path", f.source.string()}});
    REQUIRE(added.at("ok") == true);
    const std::string ref_id = added.at("id");
    CHECK(added.at("revision").get<long>() == rev + 1);
    const json defaults = single_ref(id, ref_id);
    CHECK(defaults.at("name") == "tetra");
    CHECK(defaults.at("visible") == true);
    CHECK(defaults.at("opacity").get<double>() == doctest::Approx(0.4));
    CHECK(defaults.at("color") == json::array({0.45, 0.65, 0.85}));
    CHECK(defaults.at("approximate") == false);
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}),
               {0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 3});
    REQUIRE(fs::remove(f.source));

    CHECK(doc::doc_undo(id).at("revision").get<long>() == rev + 2);
    check_empty(id);
    CHECK_THROWS_AS(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}), doc::OpError);
    CHECK(doc::doc_redo(id).at("revision").get<long>() == rev + 3);
    check_bbox(single_ref(id, ref_id).at("bbox_mm"), {0, 0, 0}, {1, 2, 3});
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}),
               {0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 3});
    const json removed = doc::doc_mesh_ref_remove(id, {{"id", ref_id}});
    CHECK(removed.at("ok") == true);
    CHECK(removed.at("revision").get<long>() == rev + 4);
    check_empty(id);
    CHECK(doc::doc_undo(id).at("revision").get<long>() == rev + 5);
    single_ref(id, ref_id);
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}),
               {0, 0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 3});
    CHECK(doc::doc_redo(id).at("revision").get<long>() == rev + 6);
    check_empty(id);
    const json before = doc::doc_tree(id);
    CHECK_THROWS_AS(doc::doc_mesh_ref_remove(id, {{"id", ref_id}}), doc::OpError);
    CHECK_THROWS_AS(doc::doc_mesh_ref_update(id, {{"id", ref_id}, {"name", "missing"}}), doc::OpError);
    CHECK(doc::doc_tree(id) == before);
    check_solid(id, kBoxVolume);
}

TEST_CASE("booleans and both B-rep and mesh exports exclude references") {
    Fixture f;
    const std::string id = f.box();
    const std::string base = doc::doc_tree(id).at("tree").at("features").at(0).at("id");
    const json added = doc::doc_mesh_ref_add(id, {
        {"path", f.source.string()}, {"transform", initial_transform()}});
    const std::string ref_id = added.at("id");
    REQUIRE(fs::remove(f.source));

    // A separate solid tool still participates in a real boolean with the base.
    const json tool = doc::doc_apply_ops(id, -1, json::array({
        {{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Tool"},
            {"params", {{"length", 8.0}, {"width", 8.0}, {"height", 20.0}, {"merge", false}}}}}}
    }), false);
    REQUIRE(tool.at("rebuild").at("ok") == true);
    const std::string tool_id = tool.at("id_map").at("$0");
    const json cut = doc::doc_apply_ops(id, -1, json::array({
        {{"op", "add_feature"}, {"feature", {{"type", "boolean"},
            {"params", {{"op", "subtract"}}},
            {"refs", {{"targets", json::array({base})}, {"tools", json::array({tool_id})}}}}}}
    }), false);
    REQUIRE(cut.at("rebuild").at("ok") == true);
    const double cut_volume = kBoxVolume - 8.0 * 8.0 * 20.0;
    check_solid(id, cut_volume);
    single_ref(id, ref_id);
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}),
               {100, 200, 300, 100, 202, 300, 96, 200, 300, 100, 200, 306});

    // A reference ID must never become a usable solid boolean operand.
    const std::string result_id = cut.at("id_map").at("$0");
    const json before = doc::doc_tree(id);
    try {
        const json rejected = doc::doc_apply_ops(id, -1, json::array({
            {{"op", "add_feature"}, {"feature", {{"type", "boolean"},
                {"params", {{"op", "subtract"}}},
                {"refs", {{"targets", json::array({result_id})}, {"tools", json::array({ref_id})}}}}}}
        }), false);
        CHECK(rejected.at("rebuild").at("ok") == false);
    } catch (const doc::OpError&) {
        // Structural rejection is also a valid way to refuse this operand.
    }
    CHECK(doc::doc_tree(id) == before);
    check_solid(id, cut_volume);
    single_ref(id, ref_id);

    const fs::path stl = f.dir / "part.stl";
    doc::doc_export_file(id, stl.string(), "stl");
    REQUIRE(fs::exists(stl));
    check_exported_stl(stl);

    const fs::path step = f.dir / "part.step";
    doc::doc_export_step(id, step.string());
    REQUIRE(fs::exists(step));
    const std::string imported = f.remember(doc::doc_import_file(step.string(), "", false, "step"));
    check_solid(imported, cut_volume);
    check_empty(imported);
}

TEST_CASE("invalid reference requests leave the part unchanged") {
    Fixture f;
    const std::string id = f.box();
    const json before = doc::doc_tree(id);
    json bad_scale = initial_transform();
    bad_scale["mm_per_unit"] = 0;
    json bad_rotation = initial_transform();
    bad_rotation["rotation"][0][0] = 2;
    for (const json& transform : {bad_scale, bad_rotation}) {
        CHECK_THROWS_AS(doc::doc_mesh_ref_add(id, {
            {"path", f.source.string()}, {"transform", transform}}), doc::OpError);
        CHECK(doc::doc_tree(id) == before);
        check_empty(id);
    }
    CHECK_THROWS_AS(doc::doc_mesh_ref_add(id, {
        {"path", (f.dir / "missing.ply").string()}}), doc::OpError);
    CHECK(doc::doc_tree(id) == before);

    const json added = doc::doc_mesh_ref_add(id, {{"path", f.source.string()}});
    const std::string ref_id = added.at("id");
    const json before_update = doc::doc_tree(id);
    const json before_list = doc::doc_mesh_ref_list(id);
    const json before_mesh = doc::doc_mesh_ref_mesh(id, {{"id", ref_id}});
    for (const json& changes : {
             json{{"id", ref_id}, {"transform", bad_scale}},
             json{{"id", ref_id}, {"transform", bad_rotation}},
             json{{"id", ref_id}, {"opacity", 1.1}},
             json{{"id", ref_id}, {"color", json::array({0.1, -0.1, 0.3})}},
             json{{"id", ref_id}, {"visible", "false"}}}) {
        CHECK_THROWS_AS(doc::doc_mesh_ref_update(id, changes), doc::OpError);
        CHECK(doc::doc_tree(id) == before_update);
        CHECK(doc::doc_mesh_ref_list(id) == before_list);
        CHECK(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}) == before_mesh);
    }
    check_solid(id, kBoxVolume);

    const std::string assembly = f.remember(doc::doc_new("assembly", "NotAPart"));
    const json before_assembly = doc::doc_tree(assembly);
    CHECK_THROWS_AS(doc::doc_mesh_ref_add(assembly, {{"path", f.source.string()}}), doc::OpError);
    CHECK_THROWS_AS(doc::doc_mesh_ref_list(assembly), doc::OpError);
    CHECK(doc::doc_tree(assembly) == before_assembly);
}

TEST_CASE("identical references share embedded geometry without sharing placement or removal") {
    Fixture f;
    std::string id = f.box();
    const std::string first = doc::doc_mesh_ref_add(id, {
        {"path", f.source.string()}, {"name", "First"}}).at("id");
    const std::string second = doc::doc_mesh_ref_add(id, {
        {"path", f.source.string()}, {"name", "Second"}, {"transform", initial_transform()}}).at("id");
    REQUIRE(first != second);
    const json refs = doc::doc_mesh_ref_list(id).at("refs");
    REQUIRE(refs.size() == 2);
    json first_ref, second_ref;
    for (const json& ref : refs) {
        if (ref.at("id") == first) first_ref = ref;
        if (ref.at("id") == second) second_ref = ref;
    }
    REQUIRE(first_ref.is_object());
    REQUIRE(second_ref.is_object());
    const std::string hash = first_ref.at("sha256");
    CHECK(hash.size() == 64);
    CHECK(second_ref.at("sha256") == hash);
    check_bbox(first_ref.at("bbox_mm"), {0, 0, 0}, {1, 2, 3});
    check_bbox(second_ref.at("bbox_mm"), {96, 200, 300}, {100, 202, 306});

    const fs::path saved = f.dir / "shared.step+";
    doc::doc_save(id, saved.string());
    const auto container = doc::stepplus_open(saved.string());
    const std::string member = "mesh_references/" + hash + ".bin";
    REQUIRE(container.extra.count(member) == 1);
    CHECK(container.extra.at(member).substr(0, 8) == "CHMZ0001");
    std::size_t payloads = 0;
    for (const auto& entry : container.extra)
        if (entry.first.rfind("mesh_references/", 0) == 0) ++payloads;
    CHECK(payloads == 1);
    REQUIRE(f.close(id));
    REQUIRE(fs::remove(f.source));

    const json opened = doc::doc_open(saved.string());
    id = f.remember(opened);
    CHECK(opened.at("cache_hit") == true);
    CHECK(opened.at("cache").at("features_executed") == 0);
    CHECK(doc::doc_mesh_ref_list(id).at("refs").size() == 2);
    doc::doc_mesh_ref_remove(id, {{"id", first}});
    const json remaining = single_ref(id, second);
    CHECK(remaining.at("sha256") == hash);
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", second}}),
               {100, 200, 300, 100, 202, 300, 96, 200, 300, 100, 200, 306});
    doc::doc_save(id, saved.string());
    REQUIRE(doc::stepplus_open(saved.string()).extra.count(member) == 1);
    REQUIRE(f.close(id));
    const json reopened = doc::doc_open(saved.string());
    id = f.remember(reopened);
    CHECK(reopened.at("cache_hit") == true);
    single_ref(id, second);
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", second}}),
               {100, 200, 300, 100, 202, 300, 96, 200, 300, 100, 200, 306});
    check_solid(id, kBoxVolume);
}

TEST_CASE("reference-only part is nonempty and survives save and reopen") {
    Fixture f;
    std::string id = f.remember(doc::doc_new("part", "ReferenceOnly"));
    json summary = doc_summary(id);
    CHECK(summary.at("empty") == true);
    CHECK(summary.at("feature_count") == 0);
    CHECK(summary.at("mesh_reference_count") == 0);

    const json added = doc::doc_mesh_ref_add(id, {
        {"path", f.source.string()}, {"transform", initial_transform()}});
    const std::string ref_id = added.at("id");
    summary = doc_summary(id);
    CHECK(summary.at("empty") == false);
    CHECK(summary.at("feature_count") == 0);
    CHECK(summary.at("mesh_reference_count") == 1);
    CHECK(summary.at("dirty") == true);
    CHECK(summary.at("revision") == added.at("revision"));
    CHECK(doc::doc_tessellate_bodies(id).empty());
    CHECK_THROWS_AS(doc::doc_eval_mass(id, 0), doc::OpError);
    CHECK_THROWS_AS(doc::doc_export_step(id, (f.dir / "empty.step").string()), doc::OpError);

    const fs::path saved = f.dir / "reference-only.step+";
    doc::doc_save(id, saved.string());
    REQUIRE(fs::exists(saved));
    REQUIRE(f.close(id));
    REQUIRE(fs::remove(f.source));
    id = f.remember(doc::doc_open(saved.string()));
    summary = doc_summary(id);
    CHECK(summary.at("empty") == false);
    CHECK(summary.at("feature_count") == 0);
    CHECK(summary.at("mesh_reference_count") == 1);
    single_ref(id, ref_id);
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}),
               {100, 200, 300, 100, 202, 300, 96, 200, 300, 100, 200, 306});
    CHECK(doc::doc_tessellate_bodies(id).empty());

    doc::doc_mesh_ref_remove(id, {{"id", ref_id}});
    summary = doc_summary(id);
    CHECK(summary.at("empty") == true);
    CHECK(summary.at("mesh_reference_count") == 0);
    doc::doc_undo(id);
    summary = doc_summary(id);
    CHECK(summary.at("empty") == false);
    CHECK(summary.at("mesh_reference_count") == 1);
    check_mesh(doc::doc_mesh_ref_mesh(id, {{"id", ref_id}}),
               {100, 200, 300, 100, 202, 300, 96, 200, 300, 100, 200, 306});
}

// A scan-sized open mesh used to be sewn face by face on open, which ran for hours inside the
// shared engine. Over the sewing limit it opens as a reference instead: as a new part, and
// imported into an existing part without touching its body.
TEST_CASE("an open mesh over the sewing limit opens as a mesh reference") {
    Fixture f;
    constexpr int kCellsX = 159, kCellsY = 158;  // 50,244 triangles in an open grid
    constexpr std::uint32_t kTriangles = 2u * kCellsX * kCellsY;
    static_assert(kTriangles > doc::kMaxSewnMeshTriangles, "grid must exceed the sewing limit");
    const fs::path stl = f.dir / "scan.stl";
    {
        std::ofstream out(stl, std::ios::binary);
        const std::string header(80, ' ');
        out.write(header.data(), 80);
        out.write(reinterpret_cast<const char*>(&kTriangles), 4);
        auto corner = [&](int x, int y) {
            const float p[3] = {static_cast<float>(x), static_cast<float>(y), 0.0f};
            out.write(reinterpret_cast<const char*>(p), sizeof(p));
        };
        const float normal[3] = {0.0f, 0.0f, 1.0f};
        const std::uint16_t attribute = 0;
        for (int x = 0; x < kCellsX; ++x)
            for (int y = 0; y < kCellsY; ++y)
                for (const auto& tri : {std::array<int, 6>{x, y, x + 1, y, x + 1, y + 1},
                                        std::array<int, 6>{x, y, x + 1, y + 1, x, y + 1}}) {
                    out.write(reinterpret_cast<const char*>(normal), sizeof(normal));
                    for (int k = 0; k < 6; k += 2) corner(tri[k], tri[k + 1]);
                    out.write(reinterpret_cast<const char*>(&attribute), 2);
                }
        REQUIRE(out.good());
    }

    const json opened = doc::doc_open(stl.string());
    const std::string id = f.remember(opened);
    CHECK(opened.at("ok") == true);
    CHECK(opened.at("import_fidelity") == "mesh_reference");
    CHECK(opened.at("triangles") == kTriangles);
    REQUIRE(opened.at("warnings").size() == 1);
    json summary = doc_summary(id);
    CHECK(summary.at("feature_count") == 0);
    CHECK(summary.at("mesh_reference_count") == 1);
    json refs = doc::doc_mesh_ref_list(id).at("refs");
    REQUIRE(refs.size() == 1);
    CHECK(refs[0].at("name") == "scan");
    CHECK(refs[0].at("triangles") == kTriangles);
    check_bbox(refs[0].at("bbox_mm"), {0, 0, 0}, {kCellsX, kCellsY, 0});
    CHECK(doc::doc_tessellate_bodies(id).empty());

    const std::string part = f.box();
    const json imported = doc::doc_import_file(stl.string(), part, false, "");
    CHECK(imported.at("doc_id") == part);
    CHECK(imported.at("import_fidelity") == "mesh_reference");
    summary = doc_summary(part);
    CHECK(summary.at("feature_count") == 1);
    CHECK(summary.at("mesh_reference_count") == 1);
    check_solid(part, kBoxVolume);
}
