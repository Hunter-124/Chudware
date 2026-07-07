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
// Requires AICAD_WITH_OCCT (STEP writer + assembly compile path).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <cmath>
#include <filesystem>
#include <string>
#include <vector>

#include <BRepPrimAPI_MakeBox.hxx>
#include <gp_Pnt.hxx>

#include <nlohmann/json.hpp>

#include "doc/formats.h"
#include "doc/session.h"

using json = nlohmann::json;
namespace fs = std::filesystem;
namespace doc = aicad::doc;

namespace {

fs::path tmp_path(const std::string& name) {
    return fs::temp_directory_path() / ("aicad_simmotion_" + name);
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

    // Add a distance mate. The refs point to the two components (no selectors);
    // the selector fields are null so they will dangle at solve time — that is fine
    // for our purposes (we just need the mate classified as "distance" so it is
    // drivable and validate_document accepts it).
    //
    // validate_document requires:
    //   - refs: distance takes 2 entries, each with component + selector.
    //   - value: must be a dim object {value, expr}.
    // Face selectors require centroid:[x,y,z]. Pick a point on the +Z face of
    // each box (z=10). The selector will dangle at solve time (no matching topo)
    // but validate_document will accept it.
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
    } catch (const aicad::doc::OpError& e) {
        threw = true;
        CHECK(std::string(e.what()).find("steps") != std::string::npos);
    }
    CHECK(threw);
}
