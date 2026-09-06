// Smoke tests for the C++ mate solver (chudware::mate). Mirrors the spirit of
// engine/doc/test_mate_solver.py: rotation log/exp round-trips, the peg-in-hole
// 1-DOF scenario, and align-flip. The full Python-vs-C++ parity lives in the
// differential corpus harness (tools/mate_diff.py); this file just proves the
// solver builds and behaves under MSVC.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "mate/mate.h"

#include <cmath>
#include <random>

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
