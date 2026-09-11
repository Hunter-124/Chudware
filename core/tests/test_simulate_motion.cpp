// Smoke test + trajectory-shape regression for doc_simulate_motion.
//
// Exercises the full path: doc_new("assembly") -> asm_insert_component ->
// asm_add_mate (distance, drivable) -> doc_simulate_motion.
//
// Uses distance mate (not angle) because angle mates require face-normal
// geometry to resolve, and our boxes share a common axis direction — a
// distance mate drives the gap between the two boxes cleanly.
//
// Verifies:
//   - The RPC returns ok:true, trajectory with requested frame count.
//   - Each frame carries body entries with a 16-element transform list.
//   - Values span [start, stop] as requested.
//   - driver.type is "distance", driver.unit is "mm".
//   - Interference struct present when disabled.
//   - Steps < 2 throws immediately.
//   - Invalid doc_id throws.
//
// Requires CHUDWARE_WITH_OCCT (STEP writer + assembly compile path).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <cmath>
#include <limits>
#include <filesystem>
#include <string>
#include <vector>

#include <BRepAlgoAPI_Cut.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepPrimAPI_MakeSphere.hxx>
#include <gp_Ax2.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

#include <nlohmann/json.hpp>

#include "doc/formats.h"
#include "doc/selectors.h"
#include "doc/session.h"

using json = nlohmann::json;
namespace fs = std::filesystem;
namespace doc = chudware::doc;

namespace {

fs::path tmp_path(const std::string& name) {
    return fs::temp_directory_path() / ("chudware_simmotion_" + name);
}

// Build a 10x10x10 box STEP file at the given X offset.
fs::path make_box_step(const std::string& tag, double ox = 0.0) {
    fs::path p = tmp_path(tag + ".step");
    doc::export_bodies(
        {BRepPrimAPI_MakeBox(gp_Pnt(ox, 0.0, 0.0), 10.0, 10.0, 10.0).Shape()},
        p.string(), "step");
    return p;
}

// Build a two-component assembly with one distance mate between them.
// Returns {doc_id, mate_id}. The distance mate drives the separation
// between the two boxes along the solved axis — it is a drivable DOF.
struct AsmInfo { std::string doc_id, mate_id, cid_a, cid_b; };

AsmInfo make_distance_mate_assembly(const fs::path& step_a, const fs::path& step_b) {
    json nresp = doc::doc_new("assembly", "motion_test_asm");
    const std::string did = nresp.at("doc_id").get<std::string>();

    // Insert both components; the id_map in the response gives us their IDs.
    json insert_ops = json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", step_a.string()}}}},
        {{"op", "asm_insert_component"}, {"source", {{"path", step_b.string()}}}},
    });
    json ir = doc::doc_apply_ops(did, -1, insert_ops, false);

    // Retrieve assigned component IDs via id_map ("$0" -> "k1", "$1" -> "k2").
    std::string cid_a = ir.value("id_map", json::object()).value("$0", std::string());
    std::string cid_b = ir.value("id_map", json::object()).value("$1", std::string());
    if (cid_a.empty()) cid_a = "k1";
    if (cid_b.empty()) cid_b = "k2";

    // Add a resolved plane-distance mate. Face selectors are in each imported
    // component's local geometry coordinates (the second STEP box itself starts
    // at x=40), so the +Z face centroids below resolve exactly.
    //
    // validate_document requires:
    //   - refs: distance takes 2 entries, each with component + selector.
    //   - value: must be a dim object {value, expr}.
    json sel_a = {{"kind", "face"}, {"centroid", json::array({5.0, 5.0, 10.0})}};
    json sel_b = {{"kind", "face"}, {"centroid", json::array({45.0, 5.0, 10.0})}};
    json refs = json::array({
        {{"component", cid_a}, {"selector", sel_a}},
        {{"component", cid_b}, {"selector", sel_b}},
    });
    json add_mate_ops = json::array({
        {{"op", "asm_add_mate"},
         {"mate", {
             {"type",       "distance"},
             {"value",      {{"value", 30.0}, {"expr", nullptr}}},  // dim object
             {"align",      "aligned"},
             {"suppressed", false},
             {"refs",       refs},
         }}}
    });
    json mr = doc::doc_apply_ops(did, -1, add_mate_ops, false);

    // Retrieve the mate id from id_map ("$0" is the first op in this batch).
    std::string mate_id = mr.value("id_map", json::object()).value("$0", std::string());

    return {did, mate_id, cid_a, cid_b};
}

}  // namespace

TEST_CASE("doc_simulate_motion: trajectory shape + frame count") {
    fs::path pa = make_box_step("a", 0.0);
    fs::path pb = make_box_step("b", 40.0);

    AsmInfo ai = make_distance_mate_assembly(pa, pb);

    // Run motion sweep: 10 -> 50 mm in 5 steps, no interference check.
    json result = doc::doc_simulate_motion(
        ai.doc_id,
        ai.mate_id,   // explicit mate id
        10.0,         // start (mm)
        50.0,         // stop  (mm)
        5,            // steps
        false,        // check_interference
        0.0           // min_clearance_mm
    );

    REQUIRE(result.value("ok", false));
    REQUIRE(result.contains("trajectory"));
    REQUIRE(result["trajectory"].is_array());

    // Must have the requested number of frames.
    CHECK((int)result["trajectory"].size() == 5);

    // Each frame: i, value, converged, bodies.
    for (const json& frame : result["trajectory"]) {
        CHECK(frame.contains("i"));
        CHECK(frame.contains("value"));
        CHECK(frame.contains("converged"));
        CHECK(frame.contains("bodies"));
        CHECK(frame["bodies"].is_array());

        for (const json& body : frame["bodies"]) {
            CHECK(body.contains("name"));
            CHECK(body.contains("transform"));
            CHECK(body["transform"].is_array());
            CHECK((int)body["transform"].size() == 16);
            // All transform entries must be numbers.
            for (const json& el : body["transform"])
                CHECK(el.is_number());
        }
    }

    // Values must span the requested range.
    const double v0    = result["trajectory"][0]["value"].get<double>();
    const double vlast = result["trajectory"][4]["value"].get<double>();
    CHECK(std::abs(v0    - 10.0) < 1e-6);
    CHECK(std::abs(vlast - 50.0) < 1e-6);

    // Driver metadata nested under "driver".
    CHECK(result.contains("driver"));
    CHECK(result["driver"].value("type", std::string()) == "distance");
    CHECK(result["driver"].value("unit", std::string()) == "mm");
    CHECK(std::abs(result["driver"].value("start", -1.0) - 10.0) < 1e-6);
    CHECK(std::abs(result["driver"].value("stop",  -1.0) - 50.0) < 1e-6);
    CHECK(result["driver"].value("steps", -1) == 5);

    // Interference struct present (we disabled checking).
    CHECK(result.contains("interference"));
    CHECK_FALSE(result["interference"].value("checked", true));

    // KEY MOTION CHECK: at least one body's transform must differ between frame 0
    // and the last frame. This proves the solver actually moved bodies when the
    // mate value changed, not just that the plumbing returned shaped data.
    {
        const json& b0 = result["trajectory"][0]["bodies"];
        const json& bL = result["trajectory"][4]["bodies"];
        REQUIRE((int)b0.size() == (int)bL.size());
        bool moved = false;
        for (size_t k = 0; k < b0.size(); ++k)
            if (b0[k]["transform"] != bL[k]["transform"]) { moved = true; break; }
        CHECK_MESSAGE(moved, "body transforms did not change across the sweep "
                     "— the mate may be dangling or the solver is not moving bodies");
    }
    // The study is genuinely read-only: no revision bump, tree/value/transform
    // mutation, or restore-shaped residue.
    const json before = doc::doc_tree(ai.doc_id);
    json again = doc::doc_simulate_motion(
        ai.doc_id, ai.mate_id, 15.0, 35.0, 3, false, 0.0);
    REQUIRE(again.value("ok", false));
    const json after = doc::doc_tree(ai.doc_id);
    CHECK(after.at("revision") == before.at("revision"));
    CHECK(after.at("tree") == before.at("tree"));

    fs::remove(pa);
    fs::remove(pb);
}

TEST_CASE("doc_simulate_motion: kinematics-only result carries NO dynamics block") {
    // A kinematics-only study must not claim an unrequested dynamics solve.
    fs::path pa = make_box_step("kin_a", 0.0);
    fs::path pb = make_box_step("kin_b", 40.0);
    AsmInfo ai = make_distance_mate_assembly(pa, pb);

    json result = doc::doc_simulate_motion(ai.doc_id, ai.mate_id,
                                           10.0, 50.0, 4, false, 0.0);
    REQUIRE(result.value("ok", false));
    CHECK_FALSE(result.contains("dynamics"));
    fs::remove(pa);
    fs::remove(pb);
}

TEST_CASE("doc_simulate_motion: underconstrained dynamics reports an incomplete force path") {
    // A plane-distance mate constrains only one translation. It can carry the gravity
    // force component, but cannot balance the offset moment or free rotational DOFs.
    fs::path pa = make_box_step("dyn_a", 0.0);
    fs::path pb = make_box_step("dyn_b", 40.0);
    AsmInfo ai = make_distance_mate_assembly(pa, pb);

    json dyn = {
        {"duration_s", 0.5},
        {"gravity", {0.0, 0.0, -9.81}},
        {"default_density_g_cm3", 2.7},
    };
    json result = doc::doc_simulate_motion(ai.doc_id, ai.mate_id,
                                           10.0, 50.0, 5, false, 0.0, dyn);
    INFO("motion result: ", result.dump(2));
    CHECK_FALSE(result.value("ok", true));
    REQUIRE(result.contains("dynamics"));
    const json& d = result["dynamics"];
    CHECK(d.contains("bodies"));
    CHECK(d.contains("frames"));
    CHECK_FALSE(d.value("ok", true));
    CHECK_FALSE(d.value("force_path_resolved", true));
    // Both boxes got mass from geometry x density (10x10x10 mm at 2.7 g/cm3 = 2.7 g).
    REQUIRE((int)d["bodies"].size() == 2);
    for (const json& b : d["bodies"])
        CHECK(std::abs(b.value("mass_kg", 0.0) - 0.0027) < 1e-6);
    // OCCT reports MatrixOfInertia about COM already. A 10 mm cube has
    // Ixx=Iyy=Izz=m*L^2/6; translating the source geometry must not trigger a
    // second parallel-axis subtraction or produce negative inertia.
    const double expected_i = 0.0027 * 0.01 * 0.01 / 6.0;
    for (const json& b : d["bodies"]) {
        const json& inertia = b.at("inertia_com_kg_m2");
        REQUIRE(inertia.size() == 9);
        CHECK(std::abs(inertia[0].get<double>() - expected_i) < 1e-10);
        CHECK(std::abs(inertia[4].get<double>() - expected_i) < 1e-10);
        CHECK(std::abs(inertia[8].get<double>() - expected_i) < 1e-10);
        CHECK(std::abs(inertia[1].get<double>()) < 1e-12);
    }
    // One dynamics frame per kept trajectory sample, timestamped across duration_s.
    CHECK((int)d["frames"].size() == (int)result["trajectory"].size());
    if (!d["frames"].empty())
        CHECK(std::abs(d["frames"].back().value("t_s", -1.0) - 0.5) < 1e-9);
    fs::remove(pa);
    fs::remove(pb);
}

TEST_CASE("doc_simulate_motion: invalid doc_id throws") {
    bool threw = false;
    try {
        doc::doc_simulate_motion("nonexistent-doc", "", 0.0, 90.0, 5, false, 0.0);
    } catch (...) {
        threw = true;
    }
    CHECK(threw);
}

TEST_CASE("doc_simulate_motion: steps < 2 throws") {
    bool threw = false;
    try {
        doc::doc_simulate_motion("any-doc", "", 0.0, 90.0, 1, false, 0.0);
    } catch (const chudware::doc::OpError& e) {
        threw = true;
        CHECK(std::string(e.what()).find("steps") != std::string::npos);
    }
    CHECK(threw);
}

TEST_CASE("doc_simulate_motion: invalid finite/timing/dynamics setup is rejected") {
    fs::path pa = make_box_step("invalid_a", 0.0);
    fs::path pb = make_box_step("invalid_b", 40.0);
    AsmInfo ai = make_distance_mate_assembly(pa, pb);

    CHECK_THROWS_AS(
        doc::doc_simulate_motion(
            ai.doc_id, ai.mate_id,
            std::numeric_limits<double>::quiet_NaN(), 20.0, 3, false, 0.0),
        chudware::doc::OpError);
    CHECK_THROWS_AS(
        doc::doc_simulate_motion(ai.doc_id, ai.mate_id, 10.0, 20.0, 3,
                                 false, -0.01),
        chudware::doc::OpError);
    CHECK_THROWS_AS(
        doc::doc_simulate_motion(
            ai.doc_id, ai.mate_id, 10.0, 20.0, 2, false, 0.0,
            json::object()),
        chudware::doc::OpError);
    CHECK_THROWS_AS(
        doc::doc_simulate_motion(
            ai.doc_id, ai.mate_id, 10.0, 20.0, 3, false, 0.0,
            json{{"duration_s", 0.0}}),
        chudware::doc::OpError);
    CHECK_THROWS_AS(
        doc::doc_simulate_motion(
            ai.doc_id, ai.mate_id, 10.0, 20.0, 3, false, 0.0,
            json{{"masses", {{ai.cid_b, -1.0}}}}),
        chudware::doc::OpError);

    fs::remove(pa);
    fs::remove(pb);
}

TEST_CASE("doc_simulate_motion: bore-less plate is a one-DOF revolute") {
    const auto path = make_box_step("revolute_plate");
    const auto did = doc::doc_new("assembly", "line_revolute").at("doc_id").get<std::string>();
    doc::doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", true}},
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", false}}
    }), false);
    const json selector = {{"kind", "face"}, {"centroid", {10, 5, 5}}};
    const auto added = doc::doc_apply_ops(did, -1, json::array({
        {{"op", "asm_add_mate"}, {"mate", {
            {"type", "angle"}, {"align", "aligned"},
            {"refs", {{{"component", "k1"}, {"selector", selector}},
                      {{"component", "k2"}, {"selector", selector}}}},
            {"value", {{"value", 0}, {"axis", {0, 1, 0}}, {"axis_point", {910, 0, 545}}}}
        }}}
    }), false);
    const auto mid = added.at("id_map").at("$0").get<std::string>();
    const auto result = doc::doc_simulate_motion(did, mid, 0, -180, 37, false, 0);
    REQUIRE(result.value("ok", false));
    for (const auto& frame : result.at("trajectory")) {
        CHECK(frame.at("converged").get<bool>());
        CHECK(frame.at("dof") == 1);
        CHECK(frame.at("residual_dof") == 0);
        const double theta = frame.at("value").get<double>() * M_PI / 180;
        const double c = std::cos(theta), s = std::sin(theta);
        const std::vector<double> expected = {
            c, 0, -s, 0, 0, 1, 0, 0, s, 0, c, 0,
            910 - 910*c - 545*s, 0, 545 + 910*s - 545*c, 1};
        for (const auto& body : frame.at("bodies")) {
            if (body.at("component") != "k2") continue;
            const auto actual = body.at("transform").get<std::vector<double>>();
            for (size_t k = 0; k < 16; ++k)
                CHECK(std::abs(actual[k] - expected[k]) < 1e-9);
        }
    }
    fs::remove(path);
}

TEST_CASE("doc_simulate_motion: offset revolute pose survives rebuilds and save reopen") {
    const auto path = make_box_step("persistent_revolute");
    const auto saved_path = tmp_path("persistent_revolute.step+");
    const auto did = doc::doc_new("assembly", "persistent_revolute")
                         .at("doc_id").get<std::string>();
    const json authored_pose = {
        1, 0, 0, 10, 0, 1, 0, 20, 0, 0, 1, 30, 0, 0, 0, 1};
    doc::doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}},
         {"fixed", true}, {"transform", authored_pose}},
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}},
         {"fixed", false}, {"transform", authored_pose}}
    }), false);

    // Both measured normals are parallel to the pin. Their real face normals
    // cannot measure its rotation: the angle needs the authored body-frame datum.
    json selector;
    for (const auto& face : doc::describe_faces(
             BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape()))
        if (face.surf == "plane" && std::abs(face.centroid.y() - 10.0) < 1e-9)
            selector = doc::make_selector(face);
    REQUIRE_FALSE(selector.is_null());
    const auto angle_value = [](double degrees) {
        return json{{"value", degrees}, {"axis", {0, 1, 0}},
                    {"axis_point", {910, 0, 545}}};
    };
    const auto added = doc::doc_apply_ops(did, -1, json::array({
        {{"op", "asm_add_mate"}, {"mate", {
            {"type", "angle"}, {"align", "aligned"},
            {"refs", {{{"component", "k1"}, {"selector", selector}},
                      {{"component", "k2"}, {"selector", selector}}}},
            {"value", angle_value(0.0)}
        }}}
    }), false);
    const auto mid = added.at("id_map").at("$0").get<std::string>();
    const auto expected_pose = [](double degrees) {
        const double theta = degrees * M_PI / 180;
        const double c = std::cos(theta), s = std::sin(theta);
        // The authored pin is first-component local; its WORLD line passes
        // through (920, 20, 575). Preserve that line across solved placements.
        return std::array<double, 16>{
            c, 0, s, 920 - 910*c - 545*s,
            0, 1, 0, 20,
            -s, 0, c, 575 + 910*s - 545*c,
            0, 0, 0, 1};
    };
    const auto check_tree_pose = [&](const json& response, double degrees) {
        const auto& tree = response.at("tree");
        REQUIRE(tree.at("components").size() == 2);
        const auto expected = expected_pose(degrees);
        for (const auto& component : tree.at("components")) {
            const auto actual = component.at("transform").get<std::vector<double>>();
            REQUIRE(actual.size() == 16);
            for (size_t k = 0; k < 16; ++k) {
                const double wanted = component.at("id") == "k2" ? expected[k] :
                    authored_pose.at(k).get<double>();
                // A 1e-9-radian residual permits ~1e-6 mm at this offset pin.
                const double tolerance = k == 3 || k == 7 || k == 11 ? 2e-6 : 1e-9;
                CHECK(std::abs(actual[k] - wanted) < tolerance);
            }
        }
        REQUIRE(tree.at("mates").size() == 1);
        CHECK(tree.at("mates").front().at("id") == mid);
        CHECK(tree.at("mates").front().at("value").at("value") == degrees);
    };
    const auto check_current_motion = [&](const std::string& id, double degrees) {
        const json before = doc::doc_tree(id);
        const auto result = doc::doc_simulate_motion(id, mid, degrees, degrees, 2, false, 0);
        INFO(result.dump(2));
        REQUIRE(result.value("ok", false));
        CHECK(result.at("driver").at("start") == degrees);
        CHECK(result.at("driver").at("stop") == degrees);
        REQUIRE(result.at("trajectory").size() == 2);
        const auto expected = expected_pose(degrees);
        for (const auto& frame : result.at("trajectory")) {
            CHECK(frame.at("value") == degrees);
            CHECK(frame.at("converged").get<bool>());
            CHECK(frame.at("residual_dof") == 0);
            REQUIRE(frame.at("bodies").size() == 2);
            for (const auto& body : frame.at("bodies")) {
                const auto actual = body.at("transform").get<std::vector<double>>();
                REQUIRE(actual.size() == 16);
                for (size_t k = 0; k < 16; ++k) {
                    // Motion frames expose column-major transforms.
                    const size_t row_major = (k % 4) * 4 + k / 4;
                    const double wanted = body.at("component") == "k2" ?
                        expected[row_major] : authored_pose.at(row_major).get<double>();
                    const double tolerance = row_major == 3 || row_major == 7 || row_major == 11 ?
                        2e-6 : 1e-9;
                    CHECK(std::abs(actual[k] - wanted) < tolerance);
                }
            }
        }
        const json after = doc::doc_tree(id);
        CHECK(after.at("revision") == before.at("revision"));
        CHECK(after.at("tree") == before.at("tree"));
    };

    check_tree_pose(added, 0.0);
    for (double degrees : {-1.5, -4.0}) {
        // The first ME490 input-yoke edit was -1.5 degrees. Recompiling its
        // solved pose used to silently turn that into -3 despite zero residuals.
        const auto edited = doc::doc_apply_ops(did, -1, json::array({
            {{"op", "asm_edit_mate"}, {"id", mid}, {"value", angle_value(degrees)}}
        }), false);
        check_tree_pose(edited, degrees);
        check_tree_pose(doc::doc_rebuild(did), degrees);
        check_current_motion(did, degrees);
    }
    doc::doc_save(did, saved_path.string());
    const auto reopened = doc::doc_open(saved_path.string());
    const auto reopened_id = reopened.at("doc_id").get<std::string>();
    check_tree_pose(reopened, -4.0);
    check_tree_pose(doc::doc_rebuild(reopened_id), -4.0);
    check_current_motion(reopened_id, -4.0);
    fs::remove(saved_path);
    fs::remove(path);
}

TEST_CASE("doc_simulate_motion: saved oblique joint remains compatible with its two spherical pivots") {
    const auto path = tmp_path("oblique_spherical_pivots.step");
    const auto saved_path = tmp_path("oblique_spherical_pivots.step+");
    const TopoDS_Shape lower = BRepPrimAPI_MakeSphere(gp_Pnt(0, 0, 0), 1).Shape();
    const TopoDS_Shape upper = BRepPrimAPI_MakeSphere(gp_Pnt(10, 20, 120), 1).Shape();
    doc::export_bodies({lower, upper}, path.string(), "step");
    json lower_selector, upper_selector;
    for (const auto& face : doc::describe_faces(lower))
        if (face.surf == "sphere") lower_selector = doc::make_selector(face);
    for (const auto& face : doc::describe_faces(upper))
        if (face.surf == "sphere") upper_selector = doc::make_selector(face);
    REQUIRE_FALSE(lower_selector.is_null());
    REQUIRE_FALSE(upper_selector.is_null());
    const auto did = doc::doc_new("assembly", "oblique_spherical_pivots")
                         .at("doc_id").get<std::string>();
    doc::doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", true}},
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", false}}
    }), false);
    const auto refs = [](const json& selector) {
        return json::array({{{"component", "k1"}, {"selector", selector}},
                            {{"component", "k2"}, {"selector", selector}}});
    };
    // The two real sphere centers and the declared pin define the SAME line.
    // Rounding the unit axis on save changes that line and makes the redundant
    // constraints inconsistent as soon as the driven body rotates.
    const double length = std::sqrt(149.0);
    const auto added = doc::doc_apply_ops(did, -1, json::array({
        {{"op", "asm_add_mate"}, {"mate", {
            {"type", "coincident"}, {"refs", refs(lower_selector)}}}},
        {{"op", "asm_add_mate"}, {"mate", {
            {"type", "coincident"}, {"refs", refs(upper_selector)}}}},
        {{"op", "asm_add_mate"}, {"mate", {
            {"type", "angle"}, {"align", "aligned"}, {"refs", refs(lower_selector)},
            {"value", {{"value", 0.0}, {"axis", {1.0 / length, 2.0 / length, 12.0 / length}},
                       {"axis_point", {0, 0, 0}}}}}}}
    }), false);
    REQUIRE(added.at("rebuild").at("converged").get<bool>());
    const auto mid = added.at("id_map").at("$2").get<std::string>();
    const auto sweep = [&](const std::string& id) {
        const auto motion = doc::doc_simulate_motion(id, mid, 0, 20, 3, false, 0);
        INFO(motion.dump(2));
        REQUIRE(motion.at("ok").get<bool>());
        REQUIRE(motion.at("trajectory").size() == 3);
        for (const auto& frame : motion.at("trajectory")) {
            REQUIRE(frame.at("converged").get<bool>());
            CHECK(frame.at("pose_check").at("ok").get<bool>());
            for (const auto& body : frame.at("bodies")) {
                if (body.at("component") != "k2") continue;
                const auto transform = body.at("transform").get<std::vector<double>>();
                for (const auto& point : std::array<std::array<double, 3>, 2>{
                         std::array<double, 3>{0, 0, 0}, {10, 20, 120}}) {
                    for (size_t row = 0; row < 3; ++row) {
                        double world = transform[12 + row];
                        for (size_t column = 0; column < 3; ++column)
                            world += transform[4 * column + row] * point[column];
                        CHECK(std::abs(world - point[row]) < 1e-7);
                    }
                }
            }
        }
    };
    sweep(did);
    doc::doc_save(did, saved_path.string());
    const auto reopened = doc::doc_open(saved_path.string());
    sweep(reopened.at("doc_id").get<std::string>());
    fs::remove(saved_path);
    fs::remove(path);
}

TEST_CASE("doc_simulate_motion: legacy angle borrows only an unambiguous grounded kingpin") {
    const auto frame_path = make_box_step("legacy_frame");
    const auto pin_path = tmp_path("legacy_pin.step");
    const auto wheel_path = tmp_path("legacy_wheel.step");
    const TopoDS_Shape pin = BRepPrimAPI_MakeCylinder(1.5, 10).Shape();
    const TopoDS_Shape blank =
        BRepPrimAPI_MakeBox(gp_Pnt(10, 20, 0), 30, 20, 10).Shape();
    const TopoDS_Shape bore = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(20, 30, -5), gp_Dir(0, 0, 1)), 2, 20).Shape();
    const TopoDS_Shape cross_bore = BRepPrimAPI_MakeCylinder(
        gp_Ax2(gp_Pnt(0, 35, 5), gp_Dir(1, 0, 0)), 2, 60).Shape();
    const TopoDS_Shape wheel = BRepAlgoAPI_Cut(
        BRepAlgoAPI_Cut(blank, bore).Shape(), cross_bore).Shape();
    doc::export_bodies({pin}, pin_path.string(), "step");
    doc::export_bodies({wheel}, wheel_path.string(), "step");
    json pin_axis, wheel_axis, second_axis, wheel_face;
    for (const auto& face : doc::describe_faces(pin))
        if (face.surf == "cylinder") pin_axis = doc::make_selector(face);
    for (const auto& face : doc::describe_faces(wheel)) {
        if (face.surf == "cylinder" && face.axis && std::abs(face.axis->z()) > 0.9)
            wheel_axis = doc::make_selector(face);
        if (face.surf == "cylinder" && face.axis && std::abs(face.axis->x()) > 0.9)
            second_axis = doc::make_selector(face);
        if (face.surf == "plane" && std::abs(face.centroid.x() - 40) < 1e-6)
            wheel_face = doc::make_selector(face);
    }
    REQUIRE_FALSE(pin_axis.is_null());
    REQUIRE_FALSE(wheel_axis.is_null());
    REQUIRE_FALSE(second_axis.is_null());
    REQUIRE_FALSE(wheel_face.is_null());
    const auto did = doc::doc_new("assembly", "legacy_kingpin").at("doc_id").get<std::string>();
    // Distinct translated/rotated frames: the pin is world +Y, which is +X in
    // the angle mate's fixed reference and +Z in the driven plate.
    const json frame_pose = {0, -1, 0, 100, 1, 0, 0, 200, 0, 0, 1, 300, 0, 0, 0, 1};
    const json pin_pose = {1, 0, 0, 70, 0, 0, 1, 60, 0, -1, 0, 40, 0, 0, 0, 1};
    const json wheel_pose = {1, 0, 0, 50, 0, 0, 1, 60, 0, -1, 0, 70, 0, 0, 0, 1};
    doc::doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", frame_path.string()}}},
         {"fixed", true}, {"transform", frame_pose}},
        {{"op", "asm_insert_component"}, {"source", {{"path", pin_path.string()}}},
         {"fixed", true}, {"transform", pin_pose}},
        {{"op", "asm_insert_component"}, {"source", {{"path", wheel_path.string()}}},
         {"fixed", false}, {"transform", wheel_pose}}
    }), false);
    const auto added = doc::doc_apply_ops(did, -1, json::array({
        {{"op", "asm_add_mate"}, {"mate", {
            {"type", "concentric"}, {"align", "aligned"},
            {"refs", {{{"component", "k2"}, {"selector", pin_axis}},
                      {{"component", "k3"}, {"selector", wheel_axis}}}}
        }}},
        {{"op", "asm_add_mate"}, {"mate", {
            {"type", "angle"}, {"align", "aligned"}, {"value", {{"value", 0}}},
            {"refs", {{{"component", "k1"},
                       {"selector", {{"kind", "face"}, {"centroid", {5, 0, 5}}}}},
                      {{"component", "k3"}, {"selector", wheel_face}}}}
        }}}
    }), false);
    const auto mid = added.at("id_map").at("$1").get<std::string>();
    bool expect_motion = true;
    SUBCASE("fixed pin supplies the measured line") {}
    SUBCASE("a floating pin is not the fixed angle reference") {
        doc::doc_apply_ops(did, -1, json::array({
            {{"op", "asm_edit_component"}, {"id", "k2"}, {"fixed", false}}
        }), false);
        expect_motion = false;
    }
    SUBCASE("two different grounded joint axes are ambiguous") {
        // A second fixed pin lies on the plate's transverse bore. Both concentric
        // mates hold at the initial pose, but neither is a unique rotation axis.
        doc::doc_apply_ops(did, -1, json::array({
            {{"op", "asm_insert_component"}, {"source", {{"path", pin_path.string()}}},
             {"fixed", true},
             {"transform", {0, 0, 1, 70, 1, 0, 0, 65, 0, 1, 0, 35, 0, 0, 0, 1}}},
            {{"op", "asm_add_mate"}, {"mate", {
                {"type", "concentric"}, {"align", "aligned"},
                {"refs", {{{"component", "k4"}, {"selector", pin_axis}},
                          {{"component", "k3"}, {"selector", second_axis}}}}
            }}}
        }), false);
        expect_motion = false;
    }
    const json before = doc::doc_tree(did);
    const auto result = doc::doc_simulate_motion(did, mid, -30, 30, 3, false, 0);
    INFO(result.dump(2));
    CHECK(result.value("ok", false) == expect_motion);
    if (expect_motion) {
        REQUIRE(result.at("trajectory").size() == 3);
        for (const auto& sample : result.at("trajectory")) {
            const double theta = sample.at("value").get<double>() * M_PI / 180;
            const double c = std::cos(theta), s = std::sin(theta);
            // World rotation about x=70,z=40, composed with the original plate pose.
            const std::vector<double> expected = {
                c, 0, -s, 0, -s, 0, -c, 0, 0, 1, 0, 0,
                70 - 20*c + 30*s, 60, 40 + 20*s + 30*c, 1};
            for (const auto& body : sample.at("bodies")) {
                const auto actual = body.at("transform").get<std::vector<double>>();
                const auto& source = body.at("component") == "k1" ? frame_pose : pin_pose;
                for (size_t k = 0; k < 16; ++k) {
                    const double wanted = body.at("component") == "k3" ? expected[k] :
                        source.at((k % 4) * 4 + k / 4).get<double>();
                    CHECK(actual[k] == doctest::Approx(wanted).epsilon(1e-7));
                }
            }
        }
    } else {
        CHECK(result.at("reason") == "driver_axis_line_unresolved");
        CHECK(result.at("trajectory").empty());
    }
    const json after = doc::doc_tree(did);
    CHECK(after.at("revision") == before.at("revision"));
    CHECK(after.at("tree") == before.at("tree"));
    fs::remove(frame_path);
    fs::remove(pin_path);
    fs::remove(wheel_path);
}

TEST_CASE("doc_simulate_motion: classification keeps overlap and exact unsafe clearance") {
    const auto path = make_box_step("classification");
    const auto did = doc::doc_new("assembly", "motion_classification").at("doc_id").get<std::string>();
    doc::doc_apply_ops(did, -1, json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", true}},
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", false},
         {"transform", {1, 0, 0, 12, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1}}},
        {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", true},
         {"transform", {1, 0, 0, 0, 0, 1, 0, 1000, 0, 0, 1, 0, 0, 0, 0, 1}}}
    }), false);
    const json selector = {{"kind", "face"}, {"centroid", {10, 5, 5}}};
    const auto added = doc::doc_apply_ops(did, -1, json::array({
        {{"op", "asm_add_mate"}, {"mate", {
            {"type", "distance"}, {"align", "aligned"}, {"value", {{"value", 12}}},
            {"refs", {{{"component", "k1"}, {"selector", selector}},
                      {{"component", "k2"}, {"selector", selector}}}}
        }}}
    }), false);
    const auto mid = added.at("id_map").at("$0").get<std::string>();
    const json before = doc::doc_tree(did);
    const auto overlap = doc::doc_simulate_motion(did, mid, 0, 15, 4, true, 0);
    INFO(overlap.dump(2));
    REQUIRE(overlap.at("ok").get<bool>());
    for (const auto& frame : overlap.at("trajectory")) {
        const double value = frame.at("value").get<double>();
        CHECK(frame.at("interference_complete").get<bool>());
        CHECK(frame.at("pairs_checked") == 3);
        CHECK(frame.at("interference").get<bool>() == (value < 10));
        if (value < 10) {
            REQUIRE(frame.at("collision_detail").size() == 1);
            const auto& pair = frame.at("collision_detail").front();
            CHECK(pair.at("component_a") == "k1");
            CHECK(pair.at("component_b") == "k2");
            CHECK(pair.at("overlap_volume_mm3").get<double>() ==
                  doctest::Approx((10 - value) * 100));
        }
    }
    const auto clearance = doc::doc_simulate_motion(did, mid, 12, 14, 3, true, 3);
    INFO(clearance.dump(2));
    REQUIRE(clearance.at("ok").get<bool>());
    const auto& unsafe = clearance.at("trajectory").front().at("collision_detail");
    REQUIRE(unsafe.size() == 1);
    CHECK(unsafe.front().at("component_a") == "k1");
    CHECK(unsafe.front().at("component_b") == "k2");
    CHECK(unsafe.front().at("distance").get<double>() == doctest::Approx(2));
    CHECK(clearance.at("trajectory").back().at("collision_detail").empty());
    CHECK(clearance.at("interference").at("complete").get<bool>());
    // Single Evaluate still measures ALL three gaps, including the distant part.
    const auto exact = doc::doc_eval_interference(did, json::object());
    REQUIRE(exact.at("complete").get<bool>());
    REQUIRE(exact.at("clearances").size() == 3);
    for (const auto& pair : exact.at("clearances")) {
        CHECK(pair.at("distance").is_number());
        CHECK_FALSE(pair.contains("distance_lower_bound_mm"));
        CHECK_FALSE(pair.value("approx", false));
    }
    const auto after = doc::doc_tree(did);
    CHECK(after.at("revision") == before.at("revision"));
    CHECK(after.at("tree") == before.at("tree"));
    fs::remove(path);
}
