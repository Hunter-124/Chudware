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
