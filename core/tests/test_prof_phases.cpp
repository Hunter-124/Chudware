// Unit test for the per-phase simulation timers (core/prof/phases.{h,cpp}) and their
// end-to-end wiring into the FEA static solver.
//
// Part 1 exercises the registry directly: StudyScope / PhaseScope / add() / report() /
// reset(), including nesting (inner study gets its own record), the unscoped bucket
// (PhaseScope with no open study), and the ring + cumulative totals.
//
// Part 2 proves the sim instrumentation: a small OCCT-free structured tet4 cantilever
// through fea_static_solve must land its "solve.*" phases (recorded by FeaProf::mark at
// the setup/assemble/factorize/back-substitute/stress-recovery boundaries) in an
// enclosing study, without AI_CAD_FEA_PROFILE being set — the registry is always-on.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <chrono>
#include <string>
#include <thread>
#include <vector>

#include "prof/phases.h"
#include "sim/fea_static.h"

namespace phases = aicad::prof::phases;
using nlohmann::json;

namespace {

// Find a study by domain in report()["studies"]; returns json() when absent.
json find_study(const json& r, const std::string& domain) {
    for (const auto& s : r["studies"])
        if (s.value("domain", std::string()) == domain) return s;
    return json();
}

// Sum of "ms" over a study's phases whose name starts with `prefix`.
double phase_ms(const json& study, const std::string& prefix, int* hits = nullptr) {
    double ms = 0.0;
    if (hits) *hits = 0;
    if (!study.is_object()) return ms;
    for (const auto& p : study["phases"]) {
        const std::string name = p.value("name", std::string());
        if (name.rfind(prefix, 0) == 0) {
            ms += p.value("ms", 0.0);
            if (hits) (*hits)++;
        }
    }
    return ms;
}

// Structured 1x1x4 box beam split into Freudenthal tets (same construction as
// test_sim.cpp's make_box_tet4, trimmed to what this smoke needs).
void make_beam(std::vector<double>& points, std::vector<int>& tets) {
    const int nx = 4, ny = 1, nz = 1;
    const double Lx = 40.0, Ly = 10.0, Lz = 10.0;  // mm
    const int NX = nx + 1, NY = ny + 1, NZ = nz + 1;
    auto gid = [&](int i, int j, int k) { return (k * NY + j) * NX + i; };
    points.resize(static_cast<size_t>(NX) * NY * NZ * 3);
    for (int k = 0; k < NZ; ++k)
        for (int j = 0; j < NY; ++j)
            for (int i = 0; i < NX; ++i) {
                const int id = gid(i, j, k);
                points[id * 3 + 0] = Lx * i / nx;
                points[id * 3 + 1] = Ly * j / ny;
                points[id * 3 + 2] = Lz * k / nz;
            }
    static const int TET[6][4] = {{0, 1, 2, 6}, {0, 2, 3, 6}, {0, 3, 7, 6},
                                  {0, 7, 4, 6}, {0, 4, 5, 6}, {0, 5, 1, 6}};
    for (int k = 0; k < nz; ++k)
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                const int c[8] = {gid(i, j, k),             gid(i + 1, j, k),
                                  gid(i + 1, j + 1, k),     gid(i, j + 1, k),
                                  gid(i, j, k + 1),         gid(i + 1, j, k + 1),
                                  gid(i + 1, j + 1, k + 1), gid(i, j + 1, k + 1)};
                for (const auto& tt : TET)
                    for (int v = 0; v < 4; ++v) tets.push_back(c[tt[v]]);
            }
}

}  // namespace

TEST_CASE("phase registry: scopes, nesting, unscoped bucket, totals, reset") {
    phases::reset();

    {
        phases::StudyScope outer("unit_outer");
        {
            phases::PhaseScope p("work");
            std::this_thread::sleep_for(std::chrono::milliseconds(15));
        }
        phases::add("manual", 5.0, 2);  // manual accumulation joins the same study
        {
            phases::StudyScope inner("unit_inner");  // nested study gets its OWN record
            phases::PhaseScope p("inner_work");
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    }
    {
        phases::PhaseScope p("orphan");  // no open study -> unscoped bucket
    }

    json r = phases::report(false);
    INFO("report = " << r.dump(2));
    REQUIRE(r.value("ok", false));
    CHECK(r.value("completed", 0ULL) == 2);  // inner + outer

    json outer = find_study(r, "unit_outer");
    REQUIRE(outer.is_object());
    // sleep_for(15ms) guarantees AT LEAST 15ms elapsed (may be longer under load — no
    // upper bound asserted; many agents share this box).
    CHECK(phase_ms(outer, "work") >= 14.0);
    int manual_hits = 0;
    CHECK(phase_ms(outer, "manual", &manual_hits) == doctest::Approx(5.0));
    CHECK(manual_hits == 1);
    CHECK(outer.value("total_ms", 0.0) >= 19.0);  // 15ms work + 5ms inner study
    // The inner study's phase must NOT leak into the outer record.
    CHECK(phase_ms(outer, "inner_work") == 0.0);

    json inner = find_study(r, "unit_inner");
    REQUIRE(inner.is_object());
    CHECK(phase_ms(inner, "inner_work") >= 4.0);

    // Unscoped bucket caught the orphan phase.
    REQUIRE(r["unscoped"].contains("orphan"));
    CHECK(r["unscoped"]["orphan"].value("count", 0ULL) == 1);

    // Cumulative totals: one run per domain.
    REQUIRE(r["totals"].contains("unit_outer"));
    CHECK(r["totals"]["unit_outer"].value("runs", 0ULL) == 1);

    // reset() empties everything.
    phases::reset();
    json r2 = phases::report(false);
    CHECK(r2.value("completed", 1ULL) == 0);
    CHECK(r2["studies"].empty());
}

TEST_CASE("fea_static_solve records solve.* phases into an enclosing study") {
    phases::reset();

    std::vector<double> points;
    std::vector<int> tets;
    make_beam(points, tets);
    const int n_nodes = static_cast<int>(points.size() / 3);

    // Clamp the x=0 face, pull the x=Lx face in +z. Values are arbitrary — this test is
    // about the PHASE RECORDS, not the physics (test_sim.cpp owns the numerics).
    std::vector<int> fixed;
    std::vector<double> forces(static_cast<size_t>(n_nodes) * 3, 0.0);
    for (int n = 0; n < n_nodes; ++n) {
        if (points[n * 3 + 0] < 1e-9) fixed.push_back(n);
        if (points[n * 3 + 0] > 40.0 - 1e-9) forces[n * 3 + 2] = 10.0;  // N
    }

    aicad::sim::FeaStaticResult res;
    {
        phases::StudyScope study("unit_fea");
        res = aicad::sim::fea_static_solve(points, tets, 200000.0 /*E MPa*/, 0.3 /*nu*/,
                                           250.0 /*yield MPa*/, fixed, forces,
                                           4 /*tet4*/);
    }
    REQUIRE(res.ok);

    json r = phases::report(false);
    INFO("report = " << r.dump(2));
    json study = find_study(r, "unit_fea");
    REQUIRE(study.is_object());

    // The solver's FeaProf marks must have recorded "solve.<label>" phases — at minimum
    // the assembly and the stress recovery (factorization labels vary by backend:
    // CHOLMOD / LDLT / LU — so only count the prefix family for those).
    int solve_hits = 0;
    (void)phase_ms(study, "solve.", &solve_hits);
    CHECK(solve_hits >= 3);
    int hits = 0;
    (void)phase_ms(study, "solve.assemble-elements", &hits);
    CHECK(hits == 1);
    hits = 0;
    (void)phase_ms(study, "solve.stress-recovery", &hits);
    CHECK(hits == 1);
}
