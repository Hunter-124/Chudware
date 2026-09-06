// Smoke tests for the C++ mate solver (chudware::mate). Mirrors the spirit of
// engine/doc/test_mate_solver.py: rotation log/exp round-trips, the peg-in-hole
// 1-DOF scenario, and align-flip. The full Python-vs-C++ parity lives in the
// differential corpus harness (tools/mate_diff.py); this file just proves the
// solver builds and behaves under MSVC.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "mate/mate.h"

#include <cmath>
#include <optional>
#include <random>
#include <string>

using namespace chudware::mate;
using nlohmann::json;
using Eigen::Vector3d;
using Eigen::Matrix3d;

namespace {

std::vector<double> ident16() {
    return {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
}

std::vector<double> xform(Vector3d t, Vector3d w) {
    Matrix3d R = rodrigues(w);
    std::vector<double> M(16, 0.0);
    M[15] = 1.0;
    for (int r = 0; r < 3; ++r) {
        for (int c = 0; c < 3; ++c) M[r * 4 + c] = R(r, c);
        M[r * 4 + 3] = t[r];
    }
    return M;
}

json geom_entry(const std::string& mid, int ri, const std::string& kind,
                Vector3d p, Vector3d n, Vector3d d, double radius) {
    json g;
    g["mate_id"] = mid;
    g["ref_index"] = ri;
    g["geom"] = {{"kind", kind},
                 {"point", {p[0], p[1], p[2]}},
                 {"normal", {n[0], n[1], n[2]}},
                 {"direction", {d[0], d[1], d[2]}},
                 {"radius", radius}};
    return g;
}

}  // namespace

TEST_CASE("rotation log/exp round-trips") {
    std::mt19937_64 rng(0);
    std::normal_distribution<double> nd(0.0, 0.7);
    for (int i = 0; i < 20; ++i) {
        Vector3d w(nd(rng), nd(rng), nd(rng));
        Vector3d w2 = mat_to_rotvec(rodrigues(w));
        CHECK((w - w2).cwiseAbs().maxCoeff() < 1e-9);
    }
}

TEST_CASE("peg-in-hole leaves exactly one DOF") {
    json components = json::array();
    components.push_back({{"id", "k1"}, {"transform", ident16()}, {"fixed", true}});
    components.push_back({{"id", "k2"},
                          {"transform", xform({30, -10, 25}, {0.3, -0.2, 0.5})},
                          {"fixed", false}});

    json mates = json::array();
    mates.push_back({{"id", "m1"}, {"type", "concentric"}, {"align", "aligned"},
                     {"refs", {{{"component", "k1"}}, {{"component", "k2"}}}}});
    mates.push_back({{"id", "m2"}, {"type", "coincident"}, {"align", "aligned"},
                     {"refs", {{{"component", "k1"}}, {{"component", "k2"}}}}});

    json input;
    input["components"] = components;
    input["mates"] = mates;
    input["resolved_geometry"] = json::array({
        geom_entry("m1", 0, "axis", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 4.0),
        geom_entry("m1", 1, "axis", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 3.9),
        geom_entry("m2", 0, "plane", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 0.0),
        geom_entry("m2", 1, "plane", {0, 0, 0}, {0, 0, -1}, {0, 0, 1}, 0.0),
    });

    json out = solve_mates_json(input);
    CHECK(out["converged"].get<bool>());
    CHECK(out["dof"].get<int>() == 1);
    auto M = out["transforms"]["k2"].get<std::vector<double>>();
    // peg base seats at z=0, on the z-axis (x=y=0).
    CHECK(std::abs(M[3]) < 1e-5);   // tx
    CHECK(std::abs(M[7]) < 1e-5);   // ty
    CHECK(std::abs(M[11]) < 1e-5);  // tz
    for (const auto& s : out["mate_status"])
        CHECK(s["status"].get<std::string>() == "ok");
}

TEST_CASE("redundant concentric is not over-defined") {
    json components = json::array();
    components.push_back({{"id", "k1"}, {"transform", ident16()}, {"fixed", true}});
    components.push_back({{"id", "k2"},
                          {"transform", xform({30, -10, 25}, {0.3, -0.2, 0.5})},
                          {"fixed", false}});
    json mates = json::array();
    mates.push_back({{"id", "m1"}, {"type", "concentric"}, {"align", "aligned"},
                     {"refs", {{{"component", "k1"}}, {{"component", "k2"}}}}});
    mates.push_back({{"id", "m2"}, {"type", "coincident"}, {"align", "aligned"},
                     {"refs", {{{"component", "k1"}}, {{"component", "k2"}}}}});
    mates.push_back({{"id", "m3"}, {"type", "concentric"}, {"align", "aligned"},
                     {"refs", {{{"component", "k1"}}, {{"component", "k2"}}}}});
    json input;
    input["components"] = components;
    input["mates"] = mates;
    input["resolved_geometry"] = json::array({
        geom_entry("m1", 0, "axis", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 4.0),
        geom_entry("m1", 1, "axis", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 3.9),
        geom_entry("m2", 0, "plane", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 0.0),
        geom_entry("m2", 1, "plane", {0, 0, 0}, {0, 0, -1}, {0, 0, 1}, 0.0),
        geom_entry("m3", 0, "axis", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 4.0),
        geom_entry("m3", 1, "axis", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 3.9),
    });
    json out = solve_mates_json(input);
    CHECK(!out["over_defined"].get<bool>());
    bool any_redundant = false;
    for (const auto& s : out["mate_status"])
        if (s["status"].get<std::string>() == "redundant") any_redundant = true;
    CHECK(any_redundant);
}

// A FIXED component's pose is authored, never solved — so a stale warm-start entry
// (the previous solve's params, which include every fixed component) must not override
// the transform the document carries. Before this, re-posing the anchor was silently
// discarded on the next solve. An unfixed component still warm-starts: that seed is the
// solver's, which is why a mated component stays solver-owned.
TEST_CASE("a fixed component's transform beats a stale warm entry") {
    json components = json::array();
    components.push_back({{"id", "k1"}, {"transform", xform({50, 0, 0}, {0, 0, 0})},
                          {"fixed", true}});
    components.push_back({{"id", "k2"}, {"transform", xform({0, 0, 0}, {0, 0, 0})},
                          {"fixed", false}});

    WarmMap warm;
    warm["k1"] = {Vector3d(0, 0, 0), Vector3d(0, 0, 0)};   // stale: the pre-edit anchor pose
    warm["k2"] = {Vector3d(0, 7, 0), Vector3d(0, 0, 0)};

    MateSolveResult out = solve_mates(components, json::array(), ResolvedGeometry{}, warm);
    REQUIRE(out.converged);

    auto k1 = out.transforms.at("k1");
    CHECK(std::abs(k1[3] - 50.0) < 1e-9);  // authored tx wins over warm's 0
    CHECK(std::abs(k1[7]) < 1e-9);
    CHECK(std::abs(k1[11]) < 1e-9);

    // Control: the unfixed component is still seeded from warm (mate-owned pose), so with
    // no mates to pull it, it stays at the warm y=7 rather than its authored origin.
    auto k2 = out.transforms.at("k2");
    CHECK(std::abs(k2[7] - 7.0) < 1e-9);
}

// ME 490 phase 5A: a 20-mate network over 9 components at rover scale (~2 m) "diverged",
// components landing thousands of mm from where they were authored, and the reply said
// only that the solve had not converged. The mechanism was the random-restart escape:
// when ANY mate was unsatisfiable the escape perturbed the rotation of EVERY free
// component in the document and kept whichever restart scored a hair lower, so corners
// that were perfectly seated came back scrambled. Two independent chains hang off the
// fixed frame here; the second one carries a bore conflict that no pose can satisfy.
TEST_CASE("one unsatisfiable mate cannot move a different chain") {
    const Vector3d axle_a(668.0, -625.5, 190.0);   // left corner, satisfiable
    const Vector3d axle_b(-668.0, -625.5, 190.0);  // right corner, over-constrained
    const Vector3d out_dir(0, -1, 0);

    json components = json::array();
    components.push_back({{"id", "frame"}, {"transform", ident16()}, {"fixed", true}});
    // Components imported from one merged STEP share the assembly's coordinate frame, so
    // their local geometry sits ~2 m from their own origin and the authored transform is a
    // small measured correction.
    components.push_back({{"id", "hub_a"}, {"transform", xform({0.4, -0.2, 0.1}, {0, 0, 0})},
                          {"fixed", false}});
    components.push_back({{"id", "hub_b"}, {"transform", xform({-0.3, 0.2, 0.1}, {0, 0, 0})},
                          {"fixed", false}});

    json mates = json::array();
    auto add_mate = [&](const std::string& id, const std::string& type,
                        const std::string& cid) {
        mates.push_back({{"id", id}, {"type", type}, {"align", "aligned"},
                         {"refs", {{{"component", "frame"}}, {{"component", cid}}}}});
    };
    add_mate("a_bore", "concentric", "hub_a");
    add_mate("a_face", "coincident", "hub_a");
    add_mate("b_bore", "concentric", "hub_b");
    add_mate("b_face", "coincident", "hub_b");
    add_mate("b_wrong_bore", "concentric", "hub_b");  // the mistake: a parallel bore 5 mm over

    json input;
    input["components"] = components;
    input["mates"] = mates;
    input["resolved_geometry"] = json::array({
        geom_entry("a_bore", 0, "axis", axle_a, {0, 0, 1}, out_dir, 25.0),
        geom_entry("a_bore", 1, "axis", axle_a, {0, 0, 1}, out_dir, 25.0),
        geom_entry("a_face", 0, "plane", axle_a, out_dir, {0, 0, 1}, 0.0),
        geom_entry("a_face", 1, "plane", axle_a, -out_dir, {0, 0, 1}, 0.0),
        geom_entry("b_bore", 0, "axis", axle_b, {0, 0, 1}, out_dir, 25.0),
        geom_entry("b_bore", 1, "axis", axle_b, {0, 0, 1}, out_dir, 25.0),
        geom_entry("b_face", 0, "plane", axle_b, out_dir, {0, 0, 1}, 0.0),
        geom_entry("b_face", 1, "plane", axle_b, -out_dir, {0, 0, 1}, 0.0),
        geom_entry("b_wrong_bore", 0, "axis", axle_b + Vector3d(5.0, 0, 0), {0, 0, 1},
                   out_dir, 25.0),
        geom_entry("b_wrong_bore", 1, "axis", axle_b, {0, 0, 1}, out_dir, 25.0),
    });

    json out = solve_mates_json(input);
    REQUIRE_FALSE(out["converged"].get<bool>());

    // The report NAMES what is holding the solve back, worst first. The 5 mm bore
    // conflict splits evenly, so BOTH of hub_b's bore mates carry ~2.5 mm and either
    // may head the list; every other mate is seated to numerical zero.
    REQUIRE(out["residuals"].size() == 5);
    const std::string worst = out["worst_mate"].get<std::string>();
    CHECK((worst == "b_bore" || worst == "b_wrong_bore"));
    CHECK(out["residuals"][0]["mate_id"].get<std::string>() == worst);
    CHECK(out["residuals"][0]["max_abs"].get<double>() == doctest::Approx(2.5).epsilon(0.01));
    CHECK(out["residuals"][1]["max_abs"].get<double>() == doctest::Approx(2.5).epsilon(0.01));
    CHECK(out["residuals"][2]["max_abs"].get<double>() < 1e-6);
    for (size_t i = 1; i < out["residuals"].size(); ++i)
        CHECK(out["residuals"][i]["max_abs"].get<double>()
              <= out["residuals"][i - 1]["max_abs"].get<double>());
    CHECK(out["over_defined"].get<bool>());
    for (const auto& s : out["mate_status"])
        if (s["mate_id"].get<std::string>() == "b_wrong_bore")
            CHECK(s["status"].get<std::string>() == "conflicting");

    // The satisfiable chain SEATS: hub_a takes up its own 0.46 mm of authored error and
    // nothing more, and its two mates read zero. It used to come back from the restart
    // escape rotated into a different branch, hundreds of mm away.
    auto ta = out["transforms"]["hub_a"].get<std::vector<double>>();
    CHECK(Vector3d(ta[3] - 0.4, ta[7] + 0.2, ta[11] - 0.1).norm() < 1.0);
    for (const auto& r : out["residuals"])
        if (r["mate_id"].get<std::string>()[0] == 'a')
            CHECK(r["max_abs"].get<double>() < 1e-6);

    // The failing chain settles on the least-squares compromise (half of the 5 mm bore
    // offset) near its authored pose instead of being launched across the assembly.
    auto tb = out["transforms"]["hub_b"].get<std::vector<double>>();
    CHECK(Vector3d(tb[3] + 0.3, tb[7] - 0.2, tb[11] - 0.1).norm() < 10.0);
}

// The convergence threshold is quoted for a ~100 mm part. Held flat, it is a threshold on
// an ABSOLUTE length while the achievable residual scales with the lever arms of the model
// — so a large, perfectly seated assembly is reported as a failed solve and the caller
// falls back to authored transforms for no reason. The threshold now scales with the
// model's own size and is reported alongside the result.
TEST_CASE("the convergence tolerance scales with the assembly") {
    auto seat = [](double reach, Vector3d t_err, Vector3d w_err) {
        const Vector3d axle(reach, 0.0, 0.0);
        json components = json::array();
        components.push_back({{"id", "frame"}, {"transform", ident16()}, {"fixed", true}});
        components.push_back({{"id", "hub"}, {"transform", xform(t_err, w_err)},
                              {"fixed", false}});
        json mates = json::array();
        mates.push_back({{"id", "bore"}, {"type", "concentric"}, {"align", "aligned"},
                         {"refs", {{{"component", "frame"}}, {{"component", "hub"}}}}});
        mates.push_back({{"id", "face"}, {"type", "coincident"}, {"align", "aligned"},
                         {"refs", {{{"component", "frame"}}, {{"component", "hub"}}}}});
        json input;
        input["components"] = components;
        input["mates"] = mates;
        input["resolved_geometry"] = json::array({
            geom_entry("bore", 0, "axis", axle, {0, 0, 1}, {0, 1, 0}, 20.0),
            geom_entry("bore", 1, "axis", axle, {0, 0, 1}, {0, 1, 0}, 20.0),
            geom_entry("face", 0, "plane", axle, {0, 1, 0}, {0, 0, 1}, 0.0),
            geom_entry("face", 1, "plane", axle, {0, -1, 0}, {0, 0, 1}, 0.0),
        });
        return solve_mates_json(input);
    };

    // At or below the 100 mm reference the threshold is the caller's, unchanged.
    json small = seat(60.0, {0.7, -0.4, 0.25}, {0, 0, 0});
    CHECK(small["converged"].get<bool>());
    CHECK(small["tolerance"].get<double>() == doctest::Approx(1e-9));

    json rover = seat(2000.0, {0.7, -0.4, 0.25}, {0, 0, 0});
    CHECK(rover["converged"].get<bool>());
    CHECK(rover["tolerance"].get<double>() == doctest::Approx(2e-8));
    CHECK(rover["max_residual"].get<double>() <= rover["tolerance"].get<double>());

    // A 20 m assembly a tenth of a micro-radian off its seat: the mates are satisfiable
    // and the solve settles at ~5e-8 mm — a thousandth of an atom across twenty metres —
    // but it cannot reach 1e-9 mm, so the flat threshold called this seated assembly
    // divergent and its caller fell back to authored transforms for nothing.
    json big = seat(20000.0, {0, 0, 0}, {0, 0, 1e-7});
    CHECK(big["converged"].get<bool>());
    CHECK(big["tolerance"].get<double>() == doctest::Approx(2e-7));
    CHECK(big["max_residual"].get<double>() > 1e-9);
    CHECK(big["max_residual"].get<double>() <= big["tolerance"].get<double>());
}

// ME 490 phase 5D item 4: the merged rover's GROUNDED frame (fixed:true) came back from the
// mate solve ~0.046 mm off its authored placement, which showed up as a 73.7 mm3 frame<->hub
// overlap per corner that vanished when the same two solids were placed by transform. A
// fixed component is a HARD CONSTRAINT, not six variables with a stiff spring: it must be
// echoed BIT-IDENTICALLY and the mates that touch it must be satisfied against the
// placement the document actually carries.
//
// The mechanism was the pose parameterization. Every component went through
// transform -> (t, exp-map w) -> transform, and a merged import's placement is authored
// from MEASURED axes (fit_cylinder_axis on a faceted bore is good to ~1e-4 rad), so its
// 3x3 block is orthonormal only to within the measurement. The SVD projection needed to
// express it as a rotation vector is a real re-pose of the anchor, and the whole assembly
// is then seated to the projected frame instead of the authored one.
TEST_CASE("a grounded component is pinned, bit-for-bit") {
    // The mission's own corners: the frame's local geometry sits ~0.93 m from its origin
    // because every component was imported from one merged STEP.
    const Vector3d axle_l(668.0, -625.5, 190.0);
    const Vector3d axle_r(-668.0, -625.5, 190.0);
    const Vector3d out_dir(0, -1, 0);

    // Authored from two measured axes that are 1e-4 rad off perpendicular, with a measured
    // translation correction. Rigid to within the measurement, and exactly where the
    // document says the frame is.
    const double skew = 1.0e-4;
    Matrix3d Rm;
    Rm.col(0) = Vector3d(1.0, 0.0, 0.0);
    Rm.col(1) = Vector3d(skew, 1.0, 0.0).normalized();
    Rm.col(2) = Rm.col(0).cross(Rm.col(1)).normalized();
    const Vector3d frame_t(0.35, -0.20, 0.10);
    std::vector<double> authored(16, 0.0);
    authored[15] = 1.0;
    for (int r = 0; r < 3; ++r) {
        for (int c = 0; c < 3; ++c) authored[r * 4 + c] = Rm(r, c);
        authored[r * 4 + 3] = frame_t[r];
    }

    json components = json::array();
    components.push_back({{"id", "frame"}, {"transform", authored}, {"fixed", true}});
    components.push_back({{"id", "hub_l"}, {"transform", xform({0.4, -0.2, 0.1}, {0, 0, 0})},
                          {"fixed", false}});
    components.push_back({{"id", "hub_r"}, {"transform", xform({-0.3, 0.2, 0.1}, {0, 0, 0})},
                          {"fixed", false}});

    json mates = json::array();
    auto add_mate = [&](const std::string& id, const std::string& type,
                        const std::string& cid) {
        mates.push_back({{"id", id}, {"type", type}, {"align", "aligned"},
                         {"refs", {{{"component", "frame"}}, {{"component", cid}}}}});
    };
    // Two consistent chains off the anchor, one per corner: bore concentric + face flush.
    add_mate("l_bore", "concentric", "hub_l");
    add_mate("l_face", "coincident", "hub_l");
    add_mate("r_bore", "concentric", "hub_r");
    add_mate("r_face", "coincident", "hub_r");

    json input;
    input["components"] = components;
    input["mates"] = mates;
    input["resolved_geometry"] = json::array({
        geom_entry("l_bore", 0, "axis", axle_l, {0, 0, 1}, out_dir, 12.505),
        geom_entry("l_bore", 1, "axis", axle_l, {0, 0, 1}, out_dir, 12.500),
        geom_entry("l_face", 0, "plane", axle_l, out_dir, {0, 0, 1}, 0.0),
        geom_entry("l_face", 1, "plane", axle_l, -out_dir, {0, 0, 1}, 0.0),
        geom_entry("r_bore", 0, "axis", axle_r, {0, 0, 1}, out_dir, 12.505),
        geom_entry("r_bore", 1, "axis", axle_r, {0, 0, 1}, out_dir, 12.500),
        geom_entry("r_face", 0, "plane", axle_r, out_dir, {0, 0, 1}, 0.0),
        geom_entry("r_face", 1, "plane", axle_r, -out_dir, {0, 0, 1}, 0.0),
    });

    json out = solve_mates_json(input);
    REQUIRE(out["converged"].get<bool>());

    // 1. The anchor's transform is the document's, to the last bit.
    auto solved = out["transforms"]["frame"].get<std::vector<double>>();
    REQUIRE(solved.size() == authored.size());
    for (size_t i = 0; i < authored.size(); ++i) CHECK(solved[i] == authored[i]);

    // 2. Stated as the measurement the mission made: the grounded component's geometry does
    // not move. Pre-fix the exp-map round trip moved these two mate points 0.045758 mm and
    // 0.045756 mm (the authored 1.0e-4 shear came back as +/-5e-05) — the mission's
    // reported ~0.046 mm micro-pose.
    auto world = [](const std::vector<double>& M, const Vector3d& p) {
        return Vector3d(M[0] * p[0] + M[1] * p[1] + M[2] * p[2] + M[3],
                        M[4] * p[0] + M[5] * p[1] + M[6] * p[2] + M[7],
                        M[8] * p[0] + M[9] * p[1] + M[10] * p[2] + M[11]);
    };
    for (const Vector3d& p : {axle_l, axle_r})
        CHECK((world(solved, p) - world(authored, p)).norm() < 1e-9);

    // 3. And the mates that touch it are satisfied against THAT placement, in mm, not
    // against a projection of it: every residual is numerical zero.
    REQUIRE(out["residuals"].size() == 4);
    for (const auto& r : out["residuals"])
        CHECK(r["max_abs"].get<double>() < 1e-9);
    CHECK(out["max_residual"].get<double>() < 1e-9);
    CHECK(out["tolerance"].get<double>() <= 1e-6);
}

// The scale-widened threshold is a widening of a LENGTH IN MM, so it needs a ceiling: left
// proportional it grows without bound with the model, and a large enough assembly would
// report a visible gap as a seated mate. One nanometre is the ceiling — three orders below
// any real fit tolerance, and 46 000 times smaller than the micro-pose above.
TEST_CASE("the convergence threshold is absolute in mm however big the assembly") {
    auto seat = [](double reach) {
        const Vector3d axle(reach, 0.0, 0.0);
        json components = json::array();
        components.push_back({{"id", "frame"}, {"transform", ident16()}, {"fixed", true}});
        components.push_back({{"id", "hub"}, {"transform", ident16()}, {"fixed", false}});
        json mates = json::array();
        mates.push_back({{"id", "bore"}, {"type", "concentric"}, {"align", "aligned"},
                         {"refs", {{{"component", "frame"}}, {{"component", "hub"}}}}});
        mates.push_back({{"id", "face"}, {"type", "coincident"}, {"align", "aligned"},
                         {"refs", {{{"component", "frame"}}, {{"component", "hub"}}}}});
        json input;
        input["components"] = components;
        input["mates"] = mates;
        input["resolved_geometry"] = json::array({
            geom_entry("bore", 0, "axis", axle, {0, 0, 1}, {0, 1, 0}, 20.0),
            geom_entry("bore", 1, "axis", axle, {0, 0, 1}, {0, 1, 0}, 20.0),
            geom_entry("face", 0, "plane", axle, {0, 1, 0}, {0, 0, 1}, 0.0),
            geom_entry("face", 1, "plane", axle, {0, -1, 0}, {0, 0, 1}, 0.0),
        });
        return solve_mates_json(input);
    };

    // 400 m of reach: the proportional threshold was 4e-6 mm here and keeps growing with
    // the model. (doctest's Approx is useless at this magnitude — its default scale of 1.0
    // makes every value below ~1e-5 "equal" — so these are strict comparisons.)
    json huge = seat(400000.0);
    CHECK(huge["tolerance"].get<double>() <= 1e-6);
    CHECK(huge["converged"].get<bool>());
    CHECK(huge["max_residual"].get<double>() <= huge["tolerance"].get<double>());
    CHECK(huge["max_residual"].get<double>() <= 1e-6);

    // Nothing at or below the 100 mm reference moves: the caller's own threshold stands.
    json small = seat(60.0);
    CHECK(small["tolerance"].get<double>() == 1e-9);
    CHECK(small["converged"].get<bool>());
}

// ME 490 phase 5F item 5: the steer joint's angle mate could not be driven at all. At
// exactly 0 deg it came back `status: redundant` with residual 0 — `da . db - cos(target)`
// has derivative -sin(theta) in the angle it controls, so at 0 deg (and at 180 deg) its
// Jacobian row is EXACTLY ZERO and the mate adds no rank: the solver cannot drive it.
// Authored 10 deg off zero the same first-order-flat residual stalled the LM at 3.589e-03
// across three passes, and simulate_motion refused the sweep ("contains unresolved or
// non-converged frames"). A dot product also cannot tell +30 deg from -30 deg, so a sweep
// folded at zero instead of passing through it. The residual is now the SIGNED angle about
// the mate's reference axis — atan2(ref . (da x db), da.db - (da.ref)(db.ref)) — whose
// derivative is 1 per radian of relative rotation at every angle, zero included.
namespace {

const double kRad2Deg = 180.0 / std::acos(-1.0);
const double kDeg2Rad = std::acos(-1.0) / 180.0;

MateGeom mgeom(const std::string& kind, Vector3d p, Vector3d n, Vector3d d) {
    MateGeom g;
    g.kind = kind;
    g.point = p;
    g.normal = n;
    g.direction = d;
    return g;
}

// A steer joint: a fixed post and a free upright pinned to spin about the post's +z by a
// concentric + coincident pair (1 DOF on their own), with ONE angle mate as the driver.
// Both angle refs are the components' local +x, so the upright's solved spin about z IS
// the angle the mate asks for. `seed_deg` is the authored, pre-solve spin.
struct SteerRig {
    json components;
    json mates;
    ResolvedGeometry geom;
};

// `first_ref_moves` puts the FREE component first on every mate, so the angle mate's
// reference axis (which is carried by its first component) is itself being solved for.
SteerRig steer_rig(double seed_deg, double angle_deg, const std::string& align = "aligned",
                   const json& value_extra = json::object(),
                   bool first_ref_moves = false) {
    SteerRig rig;
    rig.components = json::array();
    rig.components.push_back({{"id", "post"}, {"transform", ident16()}, {"fixed", true}});
    rig.components.push_back({{"id", "upright"},
                              {"transform", xform({0, 0, 0}, {0, 0, seed_deg * kDeg2Rad})},
                              {"fixed", false}});
    json value = {{"value", angle_deg}, {"expr", nullptr}};
    for (auto it = value_extra.begin(); it != value_extra.end(); ++it)
        value[it.key()] = it.value();
    auto refs = first_ref_moves
        ? json{{{"component", "upright"}}, {{"component", "post"}}}
        : json{{{"component", "post"}}, {{"component", "upright"}}};
    rig.mates = json::array();
    rig.mates.push_back({{"id", "bore"}, {"type", "concentric"}, {"align", "aligned"},
                         {"refs", refs}});
    rig.mates.push_back({{"id", "seat"}, {"type", "coincident"}, {"align", "aligned"},
                         {"refs", refs}});
    rig.mates.push_back({{"id", "steer"}, {"type", "angle"}, {"align", align},
                         {"refs", refs}, {"value", value}});
    rig.geom[{"bore", 0}] = mgeom("axis", {0, 0, 0}, {0, 0, 1}, {0, 0, 1});
    rig.geom[{"bore", 1}] = mgeom("axis", {0, 0, 0}, {0, 0, 1}, {0, 0, 1});
    rig.geom[{"seat", 0}] = mgeom("plane", {0, 0, 0}, {0, 0, 1}, {1, 0, 0});
    rig.geom[{"seat", 1}] = mgeom("plane", {0, 0, 0}, {0, 0, -1}, {1, 0, 0});
    rig.geom[{"steer", 0}] = mgeom("axis", {0, 0, 0}, {0, 0, 1}, {1, 0, 0});
    rig.geom[{"steer", 1}] = mgeom("axis", {0, 0, 0}, {0, 0, 1}, {1, 0, 0});
    return rig;
}

// The upright's world spin about z, in degrees: the direction its local +x came out at.
double spin_deg(const MateSolveResult& out) {
    const std::vector<double>& M = out.transforms.at("upright");
    return std::atan2(M[4], M[0]) * kRad2Deg;
}

std::string status_of(const MateSolveResult& out, const std::string& mid) {
    for (const MateStatus& s : out.mate_status)
        if (s.mate_id == mid) return s.status;
    return "missing";
}

double residual_of(const MateSolveResult& out, const std::string& mid) {
    for (const MateResidual& r : out.residuals)
        if (r.mate_id == mid) return r.max_abs;
    return -1.0;
}

}  // namespace

TEST_CASE("an angle mate at 0 deg is a constraint the solver can drive") {
    SteerRig rig = steer_rig(/*seed_deg=*/10.0, /*angle_deg=*/0.0);
    MateSolveResult out = solve_mates(rig.components, rig.mates, rig.geom);

    REQUIRE(out.converged);
    CHECK(out.max_residual < 1e-9);
    CHECK(residual_of(out, "steer") < 1e-9);
    // The mate carries rank AT zero: it takes up the last DOF the hinge pair left, and it
    // is not a mate the classifier can drop. Both were the defect.
    CHECK(out.dof == 0);
    CHECK(status_of(out, "steer") == "ok");
    // A 1e-9 rad residual leaves up to 6e-8 deg of pose error; a millionth of a degree
    // is a bound on the POSE, not a restatement of the residual check above.
    CHECK(std::abs(spin_deg(out)) < 1e-6);
}

TEST_CASE("an angle mate authored 10 deg off a parallel seed converges") {
    // The reported stall: the two references start exactly parallel (the singular
    // configuration) and the mate asks for 10 deg. The old residual was 1 - cos(10 deg)
    // = 1.5e-2 with a zero gradient, so the LM had no direction to move in.
    SteerRig rig = steer_rig(/*seed_deg=*/0.0, /*angle_deg=*/10.0);
    MateSolveResult out = solve_mates(rig.components, rig.mates, rig.geom);

    REQUIRE(out.converged);
    CHECK(out.max_residual < 1e-9);
    CHECK(residual_of(out, "steer") < 1e-9);
    CHECK(status_of(out, "steer") == "ok");
    CHECK(std::abs(spin_deg(out) - 10.0) < 1e-6);
}

TEST_CASE("180 deg is not singular either") {
    SteerRig rig = steer_rig(/*seed_deg=*/175.0, /*angle_deg=*/180.0);
    MateSolveResult out = solve_mates(rig.components, rig.mates, rig.geom);

    REQUIRE(out.converged);
    CHECK(out.max_residual < 1e-9);
    CHECK(out.dof == 0);
    CHECK(status_of(out, "steer") == "ok");
    CHECK(std::abs(std::abs(spin_deg(out)) - 180.0) < 1e-6);
}

// What simulate_motion does to a driven dimensional mate: re-solve the same document at
// each sample, warm-starting from the previous frame. Every frame must SOLVE — a single
// non-converged frame makes the whole study refuse.
TEST_CASE("a driven angle mate sweeps through its whole range") {
    auto sweep = [](double start, double stop, int steps) {
        std::optional<WarmMap> warm;
        for (int i = 0; i < steps; ++i) {
            const double target =
                start + (stop - start) * static_cast<double>(i) / (steps - 1);
            SteerRig rig = steer_rig(/*seed_deg=*/0.0, target);
            MateSolveResult out = solve_mates(rig.components, rig.mates, rig.geom, warm);
            warm = out.params;

            INFO("target = " << target << " deg");
            REQUIRE(out.converged);
            CHECK(out.max_residual < 1e-9);
            CHECK(residual_of(out, "steer") < 1e-9);
            CHECK(status_of(out, "steer") == "ok");
            CHECK(out.dof == 0);
            // The pose TRACKS the driver, with its sign: this is what a motion study
            // renders, and what a dot-product residual could not express.
            CHECK(std::abs(spin_deg(out) - target) < 1e-6);
        }
    };

    SUBCASE("0 -> 60 deg in 5 deg steps") { sweep(0.0, 60.0, 13); }
    // Straight THROUGH zero, from one side to the other. A dot-product residual is even
    // in theta, so the negative half of this sweep came back as its own mirror image and
    // the frames stopped moving at zero.
    SUBCASE("-30 -> +30 deg through zero") { sweep(-30.0, 30.0, 13); }
}

// The reference axis a signed angle is measured about can be NAMED on the mate — which is
// what an assembly whose angle mate sits at 0 deg needs, since a derivation has no relative
// rotation there to read the joint axis off. Reversing it reverses the sign of the mate.
TEST_CASE("an angle mate's reference axis can be authored") {
    SteerRig up = steer_rig(0.0, 30.0, "aligned", {{"axis", {0, 0, 1}}});
    MateSolveResult out_up = solve_mates(up.components, up.mates, up.geom);
    REQUIRE(out_up.converged);
    CHECK(std::abs(spin_deg(out_up) - 30.0) < 1e-6);

    SteerRig down = steer_rig(0.0, 30.0, "aligned", {{"axis", {0, 0, -1}}});
    MateSolveResult out_down = solve_mates(down.components, down.mates, down.geom);
    REQUIRE(out_down.converged);
    CHECK(std::abs(spin_deg(out_down) + 30.0) < 1e-6);

    // An axis along the direction being measured describes no angle at all. Authored
    // input is refused, not silently replaced by a derived axis.
    SteerRig bad = steer_rig(0.0, 30.0, "aligned", {{"axis", {1, 0, 0}}});
    MateSolveResult out_bad = solve_mates(bad.components, bad.mates, bad.geom);
    CHECK(status_of(out_bad, "steer") == "unresolved");
}

// `align: anti` measures to the opposite of the second reference's direction, as it does
// for every other direction mate in the solver: an anti target of A deg is the aligned
// A - 180 deg. Before the reformulation an angle mate ignored `align` entirely.
TEST_CASE("an anti-aligned angle mate measures to the reversed direction") {
    SteerRig rig = steer_rig(/*seed_deg=*/10.0, /*angle_deg=*/180.0, "anti");
    MateSolveResult out = solve_mates(rig.components, rig.mates, rig.geom);

    REQUIRE(out.converged);
    CHECK(out.max_residual < 1e-9);
    CHECK(std::abs(spin_deg(out)) < 1e-6);
}

// Every case above pins the mate's FIRST component (the reference axis rides on it, so
// its Jacobian block and the axis's own derivative are both zero there). With the moving
// component first, all three blocks of the row — d/d(da), d/d(db) and d/d(ref) — are
// live: an angle measured about an axis that is itself being solved for. The angle is
// signed from the first reference to the second, so swapping them negates it.
TEST_CASE("an angle mate is exact when its first reference is the one that moves") {
    std::optional<WarmMap> warm;
    for (double target : {-30.0, -15.0, 0.0, 15.0, 30.0}) {
        SteerRig rig = steer_rig(/*seed_deg=*/0.0, target, "aligned", json::object(),
                                 /*first_ref_moves=*/true);
        MateSolveResult out = solve_mates(rig.components, rig.mates, rig.geom, warm);
        warm = out.params;

        INFO("target = " << target << " deg");
        REQUIRE(out.converged);
        CHECK(out.max_residual < 1e-9);
        CHECK(residual_of(out, "steer") < 1e-9);
        CHECK(out.dof == 0);
        CHECK(status_of(out, "steer") == "ok");
        CHECK(std::abs(spin_deg(out) + target) < 1e-6);
    }
}

// The reference axis is derived from the DOCUMENT's poses, never from the solver's seed.
// A driven sweep warm-starts each frame from the previous one, so a warm-derived axis is
// re-derived per frame — and where the sweep passes through zero the two directions are
// parallel, their cross product is gone, and the derivation silently falls back to a
// different axis for that frame alone. Here the joint turns about +X while the fallback
// guess is the component's local +Z: an axis that swaps mid-sweep does not just change
// the sign, it asks for a rotation the hinge cannot make, and the frame fails to solve.
TEST_CASE("a sweep through zero measures one axis, not one per frame") {
    // Hinge about +X; the measured directions are the components' local +Y. The angle
    // geoms carry the resolver's default companion (+Z), which is NOT the hinge axis, so
    // only the authored 8 deg of relative rotation names +X.
    auto hinge_rig = [](double target) {
        SteerRig rig;
        rig.components = json::array();
        rig.components.push_back({{"id", "post"}, {"transform", ident16()}, {"fixed", true}});
        rig.components.push_back({{"id", "upright"},
                                  {"transform", xform({0, 0, 0}, {8.0 * kDeg2Rad, 0, 0})},
                                  {"fixed", false}});
        auto refs = json{{{"component", "post"}}, {{"component", "upright"}}};
        rig.mates = json::array();
        rig.mates.push_back({{"id", "bore"}, {"type", "concentric"}, {"align", "aligned"},
                             {"refs", refs}});
        rig.mates.push_back({{"id", "seat"}, {"type", "coincident"}, {"align", "aligned"},
                             {"refs", refs}});
        rig.mates.push_back({{"id", "steer"}, {"type", "angle"}, {"align", "aligned"},
                             {"refs", refs},
                             {"value", {{"value", target}, {"expr", nullptr}}}});
        rig.geom[{"bore", 0}] = mgeom("axis", {0, 0, 0}, {0, 0, 1}, {1, 0, 0});
        rig.geom[{"bore", 1}] = mgeom("axis", {0, 0, 0}, {0, 0, 1}, {1, 0, 0});
        rig.geom[{"seat", 0}] = mgeom("plane", {0, 0, 0}, {1, 0, 0}, {0, 0, 1});
        rig.geom[{"seat", 1}] = mgeom("plane", {0, 0, 0}, {-1, 0, 0}, {0, 0, 1});
        rig.geom[{"steer", 0}] = mgeom("axis", {0, 0, 0}, {0, 0, 1}, {0, 1, 0});
        rig.geom[{"steer", 1}] = mgeom("axis", {0, 0, 0}, {0, 0, 1}, {0, 1, 0});
        return rig;
    };

    std::optional<WarmMap> warm;
    for (int i = 0; i < 9; ++i) {
        const double target = -20.0 + 5.0 * i;
        SteerRig rig = hinge_rig(target);
        MateSolveResult out = solve_mates(rig.components, rig.mates, rig.geom, warm);
        warm = out.params;

        INFO("target = " << target << " deg");
        REQUIRE(out.converged);
        CHECK(out.max_residual < 1e-9);
        CHECK(status_of(out, "steer") == "ok");
        CHECK(out.dof == 0);
        // Spin about +X, read off the upright's local +Y.
        const std::vector<double>& M = out.transforms.at("upright");
        CHECK(std::abs(std::atan2(M[9], M[5]) * kRad2Deg - target) < 1e-6);
    }
}
