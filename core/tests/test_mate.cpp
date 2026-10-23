// C++ mate-solver behavior: rotation log/exp round-trips, peg-in-hole degrees
// of freedom, align-flip, and persisted reference frames.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "mate/mate.h"

#include <algorithm>
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
    for (const Vector3d& axis : {unit(Vector3d(0.3, -0.7, 0.4)), unit(Vector3d(-1e-6, 1, 0))}) {
        for (double angle : {M_PI - 1e-7, M_PI, M_PI + 1e-7}) {
            const Matrix3d rotation = rodrigues(axis * angle);
            CHECK((rodrigues(mat_to_rotvec(rotation)) - rotation).cwiseAbs().maxCoeff() < 1e-12);
        }
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

TEST_CASE("mate diagnostics preserve chronological groups through late merges") {
    json input;
    input["components"] = json::array({
        {{"id", "frame"}, {"transform", ident16()}, {"fixed", true}},
        {{"id", "a"}, {"transform", xform({0, 0, 10}, {0, 0, 0})}},
        {{"id", "c"}, {"transform", xform({0, 0, 3}, {0, 0, 0})}},
        {{"id", "b"}, {"transform", xform({0, 0, -4}, {0, 0, 0})}},
        {{"id", "datum"}, {"transform", ident16()}, {"fixed", true}},
    });
    input["mates"] = json::array();
    input["resolved_geometry"] = json::array();
    auto add_mate = [&](const std::string& id, const std::string& type,
                        const std::string& a, const std::string& b) {
        input["mates"].push_back({{"id", id}, {"type", type}, {"align", "aligned"},
                                 {"refs", {{{"component", a}}, {{"component", b}}}}});
        const bool plane = type == "coincident";
        input["resolved_geometry"].push_back(geom_entry(
            id, 0, plane ? "plane" : "axis", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 0.0));
        input["resolved_geometry"].push_back(geom_entry(
            id, 1, plane ? "plane" : "axis", {0, 0, 0},
            plane ? Vector3d(0, 0, -1) : Vector3d(0, 0, 1), {0, 0, 1}, 0.0));
    };
    add_mate("a_bore", "concentric", "frame", "a");
    add_mate("c_bore", "concentric", "frame", "c");
    add_mate("b_bore", "concentric", "frame", "b");
    add_mate("a_duplicate", "concentric", "frame", "a");
    add_mate("fixed_datums", "coincident", "frame", "datum");
    // A rejected bridge must leave its two accumulated groups untouched.
    add_mate("redundant_bridge", "concentric", "a", "b");
    // This four-row mate adds only one independent constraint. Its reversed
    // component order also exercises scattering non-sorted builder columns.
    add_mate("seat_ba", "coincident", "b", "a");
    add_mate("seat_c", "coincident", "frame", "c");
    add_mate("seat_a", "coincident", "frame", "a");
    // B is now seated indirectly via A, despite never having had its own seat.
    add_mate("seat_b_duplicate", "coincident", "frame", "b");
    add_mate("seated_bridge", "coincident", "a", "c");
    add_mate("b_duplicate", "concentric", "frame", "b");

    const json out = solve_mates_json(input);
    REQUIRE(out["converged"].get<bool>());
    CHECK_FALSE(out["over_defined"].get<bool>());
    CHECK(out["dof"].get<int>() == 3);  // one free spin per component
    CHECK(out["max_residual"].get<double>() < 1e-6);
    const std::vector<std::string> expected = {
        "ok", "ok", "ok", "redundant", "ok", "redundant", "ok", "ok", "ok",
        "redundant", "redundant", "redundant",
    };
    REQUIRE(out["mate_status"].size() == expected.size());
    for (size_t i = 0; i < expected.size(); ++i) {
        CAPTURE(i);
        CHECK(out["mate_status"][i]["mate_id"] == input["mates"][i]["id"]);
        CHECK(out["mate_status"][i]["status"].get<std::string>() == expected[i]);
    }
    for (const auto& residual : out["residuals"])
        CHECK(residual["max_abs"].get<double>() < 1e-6);
    for (const std::string cid : {"a", "b", "c"}) {
        const auto transform = out["transforms"][cid].get<std::vector<double>>();
        const auto identity = ident16();
        REQUIRE(transform.size() == identity.size());
        for (size_t i = 0; i < identity.size(); ++i)
            CHECK(transform[i] == doctest::Approx(identity[i]).epsilon(1e-6));
    }
    CHECK(out["transforms"]["frame"].get<std::vector<double>>() == ident16());
    CHECK(out["transforms"]["datum"].get<std::vector<double>>() == ident16());
}

TEST_CASE("saturated mate groups still distinguish redundancy from conflict") {
    for (double distance : {0.0, 1.0}) {
        CAPTURE(distance);
        json input;
        input["components"] = json::array({
            {{"id", "frame"}, {"transform", ident16()}, {"fixed", true}},
            {{"id", "a"}, {"transform", ident16()}},
            {{"id", "b"}, {"transform", ident16()}},
            {{"id", "c"}, {"transform", ident16()}},
        });
        input["mates"] = json::array();
        input["resolved_geometry"] = json::array();
        // Diagnose the authored configuration without moving it or restarting.
        input["max_iter"] = 0;
        input["time_budget_ms"] = 0.0;
        auto add_mate = [&](const std::string& id, const std::string& type,
                            const std::string& a, const std::string& b) {
            input["mates"].push_back({{"id", id}, {"type", type},
                                     {"refs", {{{"component", a}}, {{"component", b}}}}});
            for (int side = 0; side < 2; ++side)
                input["resolved_geometry"].push_back(geom_entry(
                    id, side, "plane", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 0.0));
        };
        add_mate("lock_a", "lock", "frame", "a");
        add_mate("lock_b", "lock", "frame", "b");
        add_mate("rejected_bridge", "lock", "a", "b");
        // A new component still adds six constraints after the rejected bridge.
        add_mate("lock_c", "lock", "b", "c");
        // Grow one group into a long grounded chain before rejecting another
        // bridge. Its early locks must retain their chronological ownership.
        constexpr int chain_length = 24;
        std::string last = "c";
        for (int i = 0; i < chain_length; ++i) {
            const std::string next = "chain_" + std::to_string(i);
            input["components"].push_back({
                {"id", next}, {"transform", xform({3.0 * (i + 1), 0, 0}, {0, 0, 0})}});
            add_mate("lock_" + next, "lock", last, next);
            last = next;
        }
        add_mate("late_bridge", "lock", last, "a");
        add_mate("offset", "distance", "frame", last);
        input["mates"].back()["value"] = distance;

        const json out = solve_mates_json(input);
        CHECK(out["dof"].get<int>() == 0);
        CHECK(out["over_defined"].get<bool>() == (distance != 0.0));
        std::vector<std::string> expected = {"ok", "ok", "redundant", "ok"};
        expected.insert(expected.end(), chain_length, "ok");
        expected.push_back("redundant");
        expected.push_back(distance == 0.0 ? "redundant" : "conflicting");
        REQUIRE(out["mate_status"].size() == expected.size());
        for (size_t i = 0; i < expected.size(); ++i) {
            CAPTURE(i);
            CHECK(out["mate_status"][i]["status"].get<std::string>() == expected[i]);
        }
    }
}

TEST_CASE("fixed-only mate diagnostics retain authored poses with zero DOF") {
    const auto authored = xform({3, 4, 5}, {0, 0, 0});
    json input;
    input["components"] = json::array({
        {{"id", "a"}, {"transform", authored}, {"fixed", true}},
        {{"id", "b"}, {"transform", authored}, {"fixed", true}},
    });
    input["mates"] = json::array({
        {{"id", "seat"}, {"type", "coincident"}, {"align", "aligned"},
         {"refs", {{{"component", "a"}}, {{"component", "b"}}}}},
    });
    input["resolved_geometry"] = json::array({
        geom_entry("seat", 0, "plane", {0, 0, 0}, {0, 0, 1}, {0, 0, 1}, 0.0),
        geom_entry("seat", 1, "plane", {0, 0, 0}, {0, 0, -1}, {0, 0, 1}, 0.0),
    });
    const json out = solve_mates_json(input);
    CHECK(out["converged"].get<bool>());
    CHECK_FALSE(out["over_defined"].get<bool>());
    CHECK(out["dof"].get<int>() == 0);
    CHECK(out["max_residual"].get<double>() == 0.0);
    REQUIRE(out["mate_status"].size() == 1);
    CHECK(out["mate_status"][0]["status"].get<std::string>() == "ok");
    CHECK(out["transforms"]["a"].get<std::vector<double>>() == authored);
    CHECK(out["transforms"]["b"].get<std::vector<double>>() == authored);
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

// Live defect: a 20-mate network over 9 components at rover scale (~2 m) "diverged",
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
    CHECK(out["dof"].get<int>() == 2);  // the two axle spins remain free
    REQUIRE(out["mate_status"].size() == 5);
    for (const auto& s : out["mate_status"]) {
        const std::string expected = s["mate_id"].get<std::string>() == "b_wrong_bore"
            ? "conflicting" : "ok";
        CHECK(s["status"].get<std::string>() == expected);
    }

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

// Live defect: the merged rover's GROUNDED frame (fixed:true) came back from the
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

// Live defect: the steer joint's angle mate could not be driven at all. At
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

TEST_CASE("line angle binds a bore-less plate without warm-start drift") {
    const Vector3d pin(910, 0, 545);
    json components = json::array({
        {{"id", "front"}, {"fixed", true}, {"transform", ident16()}},
        {{"id", "rear"}, {"fixed", false}, {"transform", ident16()}}});
    json mates = json::array({{{"id", "fold"}, {"type", "angle"},
        {"refs", {{{"component", "front"}}, {{"component", "rear"}}}},
        {"value", {{"value", 0.0}, {"axis", {0, 1, 0}},
                   {"axis_point", {910, 0, 545}}}}}});
    ResolvedGeometry geometry;
    geometry[{"fold", 0}] = mgeom("plane", {0, 0, 0}, {1, 0, 0}, {0, 0, 1});
    geometry[{"fold", 1}] = mgeom("plane", {0, 0, 0}, {1, 0, 0}, {0, 0, 1});
    WarmMap warm;
    for (int degrees = 0; degrees >= -180; degrees -= 5) {
        mates[0]["value"]["value"] = degrees;
        const Vector3d w(0, degrees * M_PI / 180.0, 0);
        const Vector3d t = pin - rodrigues(w) * pin;
        // Deliberately displaced seed proves the pin is constrained, not merely seeded.
        warm["rear"] = {t + Vector3d(0.01, -0.02, 0.03), w};
        const auto solved = solve_mates(components, mates, geometry, warm, std::nullopt, 1e-12, 100);
        REQUIRE(solved.converged);
        CHECK(solved.dof == 0);
        const auto expected = xform(t, w);
        const auto& actual = solved.transforms.at("rear");
        for (size_t k = 0; k < 16; ++k)
            CHECK(std::abs(actual[k] - expected[k]) < 1e-9);
    }
    mates[0]["value"].erase("axis_point");
    mates[0]["value"]["value"] = 0;
    CHECK(solve_mates(components, mates, geometry).dof == 5);
}

TEST_CASE("a body-frame revolute honors reversed alignment") {
    const Vector3d pin(910, 0, 545);
    const json components = json::array({
        {{"id", "a"}, {"fixed", true}, {"transform", ident16()}},
        {{"id", "b"}, {"fixed", false}, {"transform", ident16()}}});
    const json mates = json::array({{{"id", "turn"}, {"type", "angle"}, {"align", "anti"},
        {"refs", {{{"component", "a"}}, {{"component", "b"}}}},
        {"value", {{"value", 30.0}, {"axis", {0, 1, 0}}, {"axis_point", {910, 0, 545}}}}}});
    ResolvedGeometry geometry;
    geometry[{"turn", 0}] = mgeom("plane", {0, 0, 0}, {0, 1, 0}, {0, 0, 1});
    geometry[{"turn", 1}] = geometry[{"turn", 0}];
    // Reversing B's measured direction makes a -150-degree physical turn
    // measure +30 degrees. A parallel face must not silently discard align.
    const Vector3d w(0, -150.0 * M_PI / 180.0, 0);
    const Vector3d t = pin - rodrigues(w) * pin;
    const WarmMap warm{{"b", {t + Vector3d(0.01, -0.02, 0.03), w}}};
    const auto solved = solve_mates(components, mates, geometry, warm, std::nullopt, 1e-12, 100);
    REQUIRE(solved.converged);
    const auto expected = xform(t, w);
    const auto& actual = solved.transforms.at("b");
    for (size_t k = 0; k < 16; ++k)
        CHECK(std::abs(actual[k] - expected[k]) < 1e-9);
}

TEST_CASE("a virtual hinge keeps its authored pin and leaves rotation free") {
    const Vector3d ta(120, -35, 70), tb(-18, 42, 11);
    const Vector3d wa(0.3, -0.5, 0.2), wb(-0.4, 0.2, 0.6);
    const Matrix3d Ra = rodrigues(wa), Rb = rodrigues(wb);
    const Vector3d pa(9, -4, 6), axis_a(0, 1, 0);
    const Vector3d pin = Ra * pa + ta, axis = Ra * axis_a;
    const Vector3d pb = Rb.transpose() * (pin - tb);
    const Vector3d axis_b = Rb.transpose() * axis;
    json components = json::array({
        {{"id", "cup"}, {"fixed", true}, {"transform", xform(ta, wa)}},
        {{"id", "cross"}, {"fixed", false}, {"transform", xform(tb, wb)}}});
    json mates = json::array({{{"id", "pin"}, {"type", "hinge"},
        {"refs", {{{"component", "cup"}}, {{"component", "cross"}}}},
        {"value", {{"value", 17.0}, {"axis", {0, 1, 0}}, {"axis_point", {9, -4, 6}}}}}});
    ResolvedGeometry geometry;
    // Cup and journal selector centroids/directions do not define the virtual pin.
    geometry[{"pin", 0}] = mgeom("plane", {80, 10, -20}, {1, 0, 0}, {0, 0, 1});
    geometry[{"pin", 1}] = mgeom("axis", {-11, 32, 44}, {0, 0, 1}, {1, 0, 0});
    for (double degrees : {-65.0, -20.0, 0.0, 40.0, 95.0}) {
        INFO("free rotation = " << degrees);
        const Matrix3d R = rodrigues(axis * (degrees * kDeg2Rad)) * Rb;
        const Vector3d w = mat_to_rotvec(R), t = pin - R * pb;
        WarmMap warm;
        warm["cross"] = {t, w};
        const auto free = solve_mates(components, mates, geometry, warm);
        REQUIRE(free.converged);
        CHECK(free.dof == 1);
        // A hinge's scalar value must never prescribe a cross angle.
        const auto expected = xform(t, w);
        const auto& actual = free.transforms.at("cross");
        for (size_t k = 0; k < 16; ++k)
            CHECK(std::abs(actual[k] - expected[k]) < 1e-8);

        warm = free.params;
        for (int repeat = 0; repeat < 3; ++repeat) {
            // Axial translation is forbidden even though axial rotation is free.
            warm["cross"].first += axis * 0.25;
            const auto corrected = solve_mates(
                components, mates, geometry, warm, std::nullopt, 1e-10, 100);
            REQUIRE(corrected.converged);
            CHECK(corrected.dof == 1);
            const auto& pose = corrected.params.at("cross");
            const Matrix3d rotation = rodrigues(pose.second);
            CHECK((rotation * pb + pose.first - pin).norm() < 1e-8);
            CHECK((rotation * axis_b - axis).norm() < 1e-8);
            warm = corrected.params;
        }
    }
}

TEST_CASE("hinge alignment corrects an opposite-hemisphere seed without fixing its phase") {
    const Vector3d pin(9, -4, 6), axis(0, 0, 1);
    json components = json::array({
        {{"id", "a"}, {"fixed", true}, {"transform", ident16()}},
        {{"id", "b"}, {"fixed", false}, {"transform", ident16()}}});
    ResolvedGeometry geometry;
    geometry[{"pin", 0}] = mgeom("axis", pin, {1, 0, 0}, axis);
    geometry[{"pin", 1}] = geometry.at({"pin", 0});
    for (bool explicit_pin : {false, true}) {
        for (bool anti : {false, true}) {
            INFO("explicit pin = " << explicit_pin << ", anti = " << anti);
            json mate = {{"id", "pin"}, {"type", "hinge"},
                {"align", anti ? "anti" : "aligned"},
                {"refs", {{{"component", "a"}}, {{"component", "b"}}}}};
            if (explicit_pin)
                mate["value"] = {{"axis", {0, 0, 1}}, {"axis_point", {9, -4, 6}}};
            // Explicit pins bind the same oriented reference line on both bodies,
            // independently of selector alignment; geometric pins honor anti.
            const double sign = anti && !explicit_pin ? -1.0 : 1.0;
            const Matrix3d target = rodrigues({sign < 0 ? M_PI : 0.0, 0, 0});
            const Matrix3d seed = rodrigues({170 * kDeg2Rad, 0, 0}) * target;
            WarmMap warm;
            warm["b"] = {pin - seed * pin, mat_to_rotvec(seed)};
            const json mates = json::array({mate});
            const auto solved = solve_mates(
                components, mates, geometry, warm, std::nullopt, 1e-10, 150);
            REQUIRE(solved.converged);
            CHECK(solved.dof == 1);
            const auto& pose = solved.params.at("b");
            const Matrix3d rotation = rodrigues(pose.second);
            CHECK((rotation * axis - sign * axis).norm() < 1e-8);
            CHECK((rotation * pin + pose.first - pin).norm() < 1e-8);

            // Another phase on the corrected branch remains a valid free pose.
            const Matrix3d spun = rodrigues(axis * (73 * kDeg2Rad)) * rotation;
            warm["b"] = {pin - spun * pin, mat_to_rotvec(spun)};
            const auto free = solve_mates(components, mates, geometry, warm);
            REQUIRE(free.converged);
            CHECK(free.dof == 1);
            CHECK((rodrigues(free.params.at("b").second) - spun).norm() < 1e-8);
            CHECK((free.params.at("b").first - warm.at("b").first).norm() < 1e-8);
        }
    }
}

TEST_CASE("an aligned hinge cannot accept a locked antiparallel axis") {
    json components = json::array({
        {{"id", "a"}, {"fixed", true}, {"transform", ident16()}},
        {{"id", "b"}, {"fixed", false}, {"transform", xform({0, 0, 0}, {M_PI, 0, 0})}}});
    json mates = json::array({
        {{"id", "pin"}, {"type", "hinge"}, {"align", "aligned"},
         {"refs", {{{"component", "a"}}, {{"component", "b"}}}}},
        {{"id", "rest"}, {"type", "lock"},
         {"refs", {{{"component", "a"}}, {{"component", "b"}}}}}});
    ResolvedGeometry geometry;
    for (const char* id : {"pin", "rest"}) {
        geometry[{id, 0}] = mgeom("axis", {0, 0, 0}, {1, 0, 0}, {0, 0, 1});
        geometry[{id, 1}] = geometry.at({id, 0});
    }
    const auto solved = solve_mates(components, mates, geometry);
    CHECK_FALSE(solved.converged);
    CHECK(solved.max_residual > solved.tolerance);
}

TEST_CASE("a passive cross follows a driven rotated reference without a cross driver") {
    const Vector3d ta(120, -35, 70), tb(-18, 42, 11);
    const Vector3d wa(0.3, -0.5, 0.2), wb(-0.4, 0.2, 0.6);
    const Matrix3d Ra = rodrigues(wa), Rb = rodrigues(wb);
    const Vector3d pa(9, -4, 6), axis_a(0, 1, 0);
    const Vector3d pb = Rb.transpose() * (Ra * pa + ta - tb);
    const Vector3d axis_b = Rb.transpose() * Ra * axis_a;
    json components = json::array({
        {{"id", "ground"}, {"fixed", true}, {"transform", xform(ta, wa)}},
        {{"id", "cup"}, {"fixed", false}, {"transform", xform(ta, wa)}},
        {{"id", "cross"}, {"fixed", false}, {"transform", xform(tb, wb)}}});
    json mates = json::array({
        {{"id", "crank"}, {"type", "angle"},
         {"refs", {{{"component", "ground"}}, {{"component", "cup"}}}},
         {"value", {{"value", 0.0}, {"axis", {0, 0, 1}}, {"axis_point", {0, 0, 0}}}}},
        {{"id", "pin"}, {"type", "hinge"},
         {"refs", {{{"component", "cup"}}, {{"component", "cross"}}}},
         {"value", {{"axis", {0, 1, 0}}, {"axis_point", {9, -4, 6}}}}}});
    ResolvedGeometry geometry;
    geometry[{"crank", 0}] = mgeom("plane", {0, 0, 0}, {1, 0, 0}, {0, 0, 1});
    geometry[{"crank", 1}] = geometry.at({"crank", 0});
    geometry[{"pin", 0}] = mgeom("plane", {80, 10, -20}, {1, 0, 0}, {0, 0, 1});
    geometry[{"pin", 1}] = mgeom("axis", {-11, 32, 44}, {0, 0, 1}, {1, 0, 0});
    std::optional<WarmMap> warm;
    for (int degrees = -40; degrees <= 40; degrees += 10) {
        INFO("primary crank = " << degrees);
        mates[0]["value"]["value"] = degrees;
        const auto solved = solve_mates(
            components, mates, geometry, warm, std::nullopt, 1e-10, 150);
        REQUIRE(solved.converged);
        CHECK(solved.dof == 1);
        const Matrix3d expected_cup = Ra * rodrigues({0, 0, degrees * kDeg2Rad});
        const auto& cup = solved.params.at("cup");
        CHECK((rodrigues(cup.second) - expected_cup).norm() < 1e-8);
        CHECK((cup.first - ta).norm() < 1e-8);
        const auto& cross = solved.params.at("cross");
        const Matrix3d rotation = rodrigues(cross.second);
        CHECK((rotation * pb + cross.first - (expected_cup * pa + ta)).norm() < 1e-8);
        CHECK((rotation * axis_b - expected_cup * axis_a).norm() < 1e-8);
        warm = solved.params;
    }
}

TEST_CASE("hinge limits remain active with geometric and explicit pins") {
    json components = json::array({
        {{"id", "a"}, {"fixed", true}, {"transform", ident16()}},
        {{"id", "b"}, {"fixed", false}, {"transform", ident16()}}});
    json mates = json::array({{{"id", "pin"}, {"type", "hinge"},
        {"refs", {{{"component", "a"}}, {{"component", "b"}}}},
        {"value", {{"min", 10.0}, {"max", 30.0}}}}});
    ResolvedGeometry geometry;
    geometry[{"pin", 0}] = mgeom("axis", {0, 0, 0}, {1, 0, 0}, {0, 0, 1});
    geometry[{"pin", 1}] = geometry.at({"pin", 0});
    for (bool explicit_pin : {false, true}) {
        if (explicit_pin) {
            mates[0]["value"]["axis"] = {0, 0, 1};
            mates[0]["value"]["axis_point"] = {0, 0, 0};
        }
        for (double seed : {5.0, 50.0}) {
            WarmMap warm;
            warm["b"] = {Vector3d(0, 0, 0.25), Vector3d(0, 0, seed * kDeg2Rad)};
            const auto solved = solve_mates(
                components, mates, geometry, warm, std::nullopt, 1e-10, 100);
            REQUIRE(solved.converged);
            const auto& pose = solved.params.at("b");
            CHECK(pose.first.norm() < 1e-8);
            const double angle = std::acos(std::max(-1.0, std::min(
                1.0, rodrigues(pose.second)(0, 0)))) * kRad2Deg;
            CHECK(angle >= 10.0 - 1e-6);
            CHECK(angle <= 30.0 + 1e-6);
        }
    }
    for (const json& invalid : json::array({
             json{{"axis", {0, 0, 1}}},
             json{{"axis_point", {0, 0, 0}}},
             json{{"axis", {0, 0, 0}}, {"axis_point", {0, 0, 0}}},
             json{{"axis", {0, 1}}, {"axis_point", {0, 0, 0}}},
             json{{"axis", {0, 0, 1}}, {"axis_point", {0, 0}}},
             json{{"axis", {0, 0, 1}}, {"axis_point", {0, "invalid", 0}}}})) {
        mates[0]["value"] = invalid;
        const auto refused = solve_mates(components, mates, geometry);
        CHECK_FALSE(refused.converged);
        CHECK(status_of(refused, "pin") == "unresolved");
    }
}

TEST_CASE("two passive cross hinges transmit a Cardan drive between inclined shafts") {
    const Vector3d pin(120, -35, 70), frame_w(0.3, -0.5, 0.2);
    const Matrix3d frame = rodrigues(frame_w);
    const double beta = 25.0 * kDeg2Rad;
    const Matrix3d output_frame = frame * rodrigues({0, beta, 0});
    json components = json::array({
        {{"id", "ground"}, {"fixed", true}, {"transform", xform(pin, frame_w)}},
        {{"id", "input"}, {"fixed", false}, {"transform", xform(pin, frame_w)}},
        {{"id", "cross"}, {"fixed", false}, {"transform", xform(pin, frame_w)}},
        {{"id", "output"}, {"fixed", false},
         {"transform", xform(pin, mat_to_rotvec(output_frame))}}});
    json mates = json::array({
        {{"id", "crank"}, {"type", "angle"},
         {"refs", {{{"component", "ground"}}, {{"component", "input"}}}},
         {"value", {{"value", 0.0}, {"axis", {0, 0, 1}}, {"axis_point", {0, 0, 0}}}}},
        {{"id", "output_bearing"}, {"type", "hinge"},
         {"refs", {{{"component", "ground"}}, {{"component", "output"}}}},
         {"value", {{"axis", {std::sin(beta), 0, std::cos(beta)}},
                    {"axis_point", {0, 0, 0}}}}},
        {{"id", "input_journal"}, {"type", "hinge"},
         {"refs", {{{"component", "input"}}, {{"component", "cross"}}}},
         {"value", {{"axis", {1, 0, 0}}, {"axis_point", {0, 0, 0}}}}},
        {{"id", "output_journal"}, {"type", "hinge"},
         {"refs", {{{"component", "output"}}, {{"component", "cross"}}}},
         {"value", {{"axis", {0, 1, 0}}, {"axis_point", {0, 0, 0}}}}}});
    ResolvedGeometry geometry;
    for (const auto& mate : mates) {
        const std::string id = mate.at("id").get<std::string>();
        geometry[{id, 0}] = mgeom("plane", {20, -4, 8}, {1, 0, 0}, {0, 0, 1});
        geometry[{id, 1}] = mgeom("plane", {-30, 2, -7}, {1, 0, 0}, {0, 0, 1});
    }
    std::optional<WarmMap> warm;
    for (double degrees : {0.0, 15.0, 30.0, 45.0, 60.0, 45.0, 30.0,
                           15.0, 0.0, -15.0, -30.0, -45.0, -60.0}) {
        INFO("Cardan input = " << degrees);
        mates[0]["value"]["value"] = degrees;
        const auto solved = solve_mates(
            components, mates, geometry, warm, std::nullopt, 1e-10, 150);
        REQUIRE(solved.converged);
        CHECK(solved.dof == 0);
        const double theta = degrees * kDeg2Rad;
        // Orthogonal journals require tan(output) = tan(input) / cos(shaft bend).
        // No output or cross angle is prescribed anywhere in the mate graph.
        const double phi = std::atan2(std::sin(theta), std::cos(beta) * std::cos(theta));
        const auto& output = solved.params.at("output");
        CHECK((rodrigues(output.second) -
               output_frame * rodrigues({0, 0, phi})).norm() < 1e-8);
        for (const char* component : {"input", "cross", "output"})
            CHECK((solved.params.at(component).first - pin).norm() < 1e-8);
        const Matrix3d cross = rodrigues(solved.params.at("cross").second);
        const Matrix3d input = rodrigues(solved.params.at("input").second);
        CHECK((cross.col(0) - input.col(0)).norm() < 1e-8);
        CHECK((cross.col(1) - rodrigues(output.second).col(1)).norm() < 1e-8);
        warm = solved.params;
    }
}

TEST_CASE("a driven body carries an offset locked child without redefining its attachment") {
    const Vector3d pin(120, -35, 70), frame_w(0.3, -0.5, 0.2);
    const Vector3d child_t(1397, -570, 263), child_w(-0.4, 0.2, 0.6);
    const Matrix3d frame = rodrigues(frame_w), child_frame = rodrigues(child_w);
    const Vector3d offset = frame.transpose() * (child_t - pin);
    json components = json::array({
        {{"id", "ground"}, {"fixed", true}, {"transform", xform(pin, frame_w)}},
        {{"id", "carrier"}, {"fixed", false}, {"transform", xform(pin, frame_w)}},
        {{"id", "child"}, {"fixed", false}, {"transform", xform(child_t, child_w)}}});
    json mates = json::array({
        {{"id", "drive"}, {"type", "angle"},
         {"refs", {{{"component", "ground"}}, {{"component", "carrier"}}}},
         {"value", {{"value", 0.0}, {"axis", {0, 0, 1}}, {"axis_point", {0, 0, 0}}}}},
        {{"id", "attachment"}, {"type", "lock"},
         {"refs", {{{"component", "carrier"}}, {{"component", "child"}}}}}});
    ResolvedGeometry geometry;
    for (const auto& mate : mates) {
        const auto id = mate.at("id").get<std::string>();
        geometry[{id, 0}] = mgeom("plane", {0, 0, 0}, {1, 0, 0}, {0, 0, 1});
        geometry[{id, 1}] = geometry.at({id, 0});
    }
    WarmMap warm;
    warm["carrier"] = {pin, frame_w};
    warm["child"] = {child_t, child_w};
    for (double degrees : {0.0, 20.0, 50.0, -15.0}) {
        INFO("drive=" << degrees);
        mates[0]["value"]["value"] = degrees;
        warm["child"].first += Vector3d(0.5, -0.7, 0.3);
        warm["child"].second += Vector3d(0.002, -0.003, 0.001);
        const auto solved = solve_mates(components, mates, geometry, warm, std::nullopt, 1e-11, 150);
        REQUIRE(solved.converged);
        const Matrix3d carrier = frame * rodrigues({0, 0, degrees * kDeg2Rad});
        const auto& child = solved.params.at("child");
        CHECK((child.first - (pin + carrier * offset)).norm() < 1e-7);
        CHECK((rodrigues(child.second) - carrier * frame.transpose() * child_frame).norm() < 1e-9);
        warm = solved.params;
    }
}

TEST_CASE("coaxial mates reject the reverse branch without restraining slide or spin") {
    const Vector3d pin(9, -4, 6), axis(0, 0, 1);
    const json components = json::array({
        {{"id", "a"}, {"fixed", true}, {"transform", ident16()}},
        {{"id", "b"}, {"fixed", false}, {"transform", ident16()}}});
    ResolvedGeometry geometry;
    geometry[{"bore", 0}] = mgeom("axis", pin, {1, 0, 0}, axis);
    geometry[{"bore", 1}] = geometry.at({"bore", 0});
    for (const char* type : {"concentric", "coincident"}) for (bool anti : {false, true}) {
        INFO("type = " << type << ", anti = " << anti);
        const double sign = anti ? -1.0 : 1.0;
        const json mates = json::array({{{"id", "bore"}, {"type", type},
            {"align", anti ? "anti" : "aligned"},
            {"refs", {{{"component", "a"}}, {{"component", "b"}}}}}});
        const Matrix3d seed = rodrigues({(anti ? -10.0 : 170.0) * kDeg2Rad, 0, 0});
        WarmMap warm{{"b", {pin - seed * pin, mat_to_rotvec(seed)}}};
        const auto solved = solve_mates(components, mates, geometry, warm);
        REQUIRE(solved.converged);
        CHECK(solved.dof == 2);
        CHECK((rodrigues(solved.params.at("b").second) * axis - sign * axis).norm() < 1e-8);
        const Matrix3d spun = rodrigues(axis * (73 * kDeg2Rad))
            * rodrigues(solved.params.at("b").second);
        const Vector3d translated = pin + 45 * axis - spun * pin;
        warm["b"] = {translated, mat_to_rotvec(spun)};
        const auto free = solve_mates(components, mates, geometry, warm);
        REQUIRE(free.converged);
        CHECK(free.dof == 2);
        CHECK((rodrigues(free.params.at("b").second) - spun).norm() < 1e-8);
        CHECK((free.params.at("b").first - translated).norm() < 1e-8);
    }
}

TEST_CASE("a prescribed line angle cannot satisfy its phase with a reversed pin") {
    const Vector3d pin(9, -4, 6), axis(0, 0, 1);
    const json components = json::array({
        {{"id", "a"}, {"fixed", true}, {"transform", ident16()}},
        {{"id", "b"}, {"fixed", false}, {"transform", ident16()}}});
    const json mates = json::array({{{"id", "turn"}, {"type", "angle"},
        {"refs", {{{"component", "a"}}, {{"component", "b"}}}},
        {"value", {{"value", 35.0}, {"axis", {0, 0, 1}}, {"axis_point", {9, -4, 6}}}}}});
    ResolvedGeometry geometry;
    geometry[{"turn", 0}] = mgeom("plane", pin, {1, 0, 0}, axis);
    geometry[{"turn", 1}] = geometry.at({"turn", 0});
    const Matrix3d expected = rodrigues(axis * (35 * kDeg2Rad));
    const Matrix3d seed = expected * rodrigues({170 * kDeg2Rad, 0, 0});
    const WarmMap warm{{"b", {pin - seed * pin, mat_to_rotvec(seed)}}};
    const auto solved = solve_mates(components, mates, geometry, warm, std::nullopt, 1e-10, 150);
    REQUIRE(solved.converged);
    CHECK(solved.dof == 0);
    CHECK((rodrigues(solved.params.at("b").second) - expected).norm() < 1e-8);
    CHECK((solved.params.at("b").first - (pin - expected * pin)).norm() < 1e-8);
}

TEST_CASE("a concentric slider follows the moving reference axis through a driven turn") {
    json components = json::array({
        {{"id", "ground"}, {"fixed", true}, {"transform", ident16()}},
        {{"id", "carrier"}, {"fixed", false}, {"transform", ident16()}},
        {{"id", "slider"}, {"fixed", false}, {"transform", xform({0, 300, 0}, {0, 0, 0})}}});
    json refs = {{{"component", "carrier"}}, {{"component", "slider"}}};
    json mates = json::array({
        {{"id", "drive"}, {"type", "angle"},
         {"refs", {{{"component", "ground"}}, {{"component", "carrier"}}}},
         {"value", {{"value", 0.0}, {"axis", {0, 0, 1}}, {"axis_point", {0, 0, 0}}}}},
        {{"id", "axis"}, {"type", "concentric"}, {"refs", refs}},
        {{"id", "extension"}, {"type", "distance"}, {"refs", refs}, {"value", 300.0}}});
    ResolvedGeometry geometry;
    geometry[{"drive", 0}] = mgeom("plane", {0, 0, 0}, {1, 0, 0}, {0, 0, 1});
    geometry[{"drive", 1}] = geometry.at({"drive", 0});
    for (int side = 0; side < 2; ++side) {
        geometry[{"axis", side}] = mgeom("axis", {0, 0, 0}, {0, 1, 0}, {0, 1, 0});
        geometry[{"extension", side}] = mgeom("plane", {0, 0, 0}, {0, 1, 0}, {0, 1, 0});
    }
    std::optional<WarmMap> warm;
    for (double degrees : {0.0, 15.0, 40.0, -25.0}) {
        INFO("drive=" << degrees);
        mates[0]["value"]["value"] = degrees;
        const auto solved = solve_mates(components, mates, geometry, warm, std::nullopt, 1e-11, 150);
        REQUIRE(solved.converged);
        const Matrix3d rotation = rodrigues({0, 0, degrees * kDeg2Rad});
        const auto& slider = solved.params.at("slider");
        CHECK((slider.first - rotation * Vector3d(0, 300, 0)).norm() < 1e-7);
        CHECK((rodrigues(slider.second).col(1) - rotation.col(1)).norm() < 1e-9);
        warm = solved.params;
    }
}
