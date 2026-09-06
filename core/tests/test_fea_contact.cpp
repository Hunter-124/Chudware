// Unilateral (frictionless, small-deflection) contact FEA gates.
//
// Exercises the two halves of the real contact path together — fea_contact_candidates
// (mesh boundary faces -> surface normals/areas -> candidate pairs across a gap) and
// fea_static_solve_contact (the active-set penalty loop) — on inline structured meshes,
// so the test is OCCT-free and mesher-free.
//
// The three cases are the physics that separates a contact solve from a bonded one:
//   (a) a gap that CLOSES under load transmits the whole load into the support,
//   (b) the same gap loaded the other way OPENS: no tension crosses a contact, so the
//       loaded part is a mechanism and the run must be refused, not solved,
//   (c) a pin in a clearance bore touches on ONE side, and which side is a result.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <cmath>
#include <cstdio>
#include <map>
#include <string>
#include <vector>

#include "sim/fea_contact.h"
#include "sim/fea_static.h"

using chudware::sim::ContactCandidateOptions;
using chudware::sim::fea_contact_candidates;
using chudware::sim::fea_static_solve_contact;
using chudware::sim::FeaContactOptions;
using chudware::sim::FeaContactReport;
using chudware::sim::FeaStaticResult;

namespace {

struct Mesh {
    std::vector<double> points;
    std::vector<int> tets;
};

// Freudenthal 6-tet split of every cell of a structured grid over the box
// [x0,x0+Lx] x [y0,y0+Ly] x [z0,z0+Lz], appended into `m` (so two boxes can share one
// mesh array as two DISCONNECTED components — exactly what an imported assembly meshes
// to).
void add_box(Mesh& m, int nx, int ny, int nz, double x0, double y0, double z0, double Lx,
             double Ly, double Lz) {
    const int NX = nx + 1, NY = ny + 1, NZ = nz + 1;
    const int base = static_cast<int>(m.points.size() / 3);
    auto gid = [&](int i, int j, int k) { return base + (k * NY + j) * NX + i; };
    for (int k = 0; k < NZ; ++k)
        for (int j = 0; j < NY; ++j)
            for (int i = 0; i < NX; ++i) {
                m.points.push_back(x0 + Lx * i / nx);
                m.points.push_back(y0 + Ly * j / ny);
                m.points.push_back(z0 + Lz * k / nz);
            }
    static const int TET[6][4] = {{0, 1, 2, 6}, {0, 2, 3, 6}, {0, 3, 7, 6},
                                  {0, 7, 4, 6}, {0, 4, 5, 6}, {0, 5, 1, 6}};
    for (int k = 0; k < nz; ++k)
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                const int c[8] = {gid(i, j, k),         gid(i + 1, j, k),
                                  gid(i + 1, j + 1, k), gid(i, j + 1, k),
                                  gid(i, j, k + 1),     gid(i + 1, j, k + 1),
                                  gid(i + 1, j + 1, k + 1), gid(i, j + 1, k + 1)};
                for (auto& tt : TET)
                    for (int v = 0; v < 4; ++v) m.tets.push_back(c[tt[v]]);
            }
}

// A hollow cylindrical tube (annulus r_in..r_out over z0..z0+Lz), structured in
// (radius, angle, z) and split into 6 tets per cell. Hollow so no cell degenerates on
// the axis. Angular wrap closes the ring.
void add_tube(Mesh& m, int nr, int nt, int nz, double r_in, double r_out, double z0,
              double Lz, double cx, double cy) {
    const int NR = nr + 1, NZ = nz + 1;
    const int base = static_cast<int>(m.points.size() / 3);
    auto gid = [&](int ir, int it, int k) {
        return base + (k * nt + (it % nt)) * NR + ir;
    };
    for (int k = 0; k < NZ; ++k)
        for (int it = 0; it < nt; ++it) {
            const double th = 2.0 * M_PI * it / nt;
            for (int ir = 0; ir < NR; ++ir) {
                const double r = r_in + (r_out - r_in) * ir / nr;
                m.points.push_back(cx + r * std::cos(th));
                m.points.push_back(cy + r * std::sin(th));
                m.points.push_back(z0 + Lz * k / nz);
            }
        }
    static const int TET[6][4] = {{0, 1, 2, 6}, {0, 2, 3, 6}, {0, 3, 7, 6},
                                  {0, 7, 4, 6}, {0, 4, 5, 6}, {0, 5, 1, 6}};
    for (int k = 0; k < nz; ++k)
        for (int it = 0; it < nt; ++it)
            for (int ir = 0; ir < nr; ++ir) {
                const int c[8] = {gid(ir, it, k),         gid(ir + 1, it, k),
                                  gid(ir + 1, it + 1, k), gid(ir, it + 1, k),
                                  gid(ir, it, k + 1),     gid(ir + 1, it, k + 1),
                                  gid(ir + 1, it + 1, k + 1), gid(ir, it + 1, k + 1)};
                for (auto& tt : TET)
                    for (int v = 0; v < 4; ++v) m.tets.push_back(c[tt[v]]);
            }
}

const double kE = 200000.0;  // MPa (steel)
const double kNu = 0.3;
const double kYield = 250.0;

// Two 20x20 mm boxes stacked along z with `gap` mm of air between them:
// lower box z in [0,10], upper box z in [10+gap, 20+gap].
Mesh stacked_boxes(double gap) {
    Mesh m;
    add_box(m, 3, 3, 2, 0.0, 0.0, 0.0, 20.0, 20.0, 10.0);
    add_box(m, 3, 3, 2, 0.0, 0.0, 10.0 + gap, 20.0, 20.0, 10.0);
    return m;
}

std::vector<int> nodes_at_z(const Mesh& m, double z) {
    std::vector<int> out;
    const int n = static_cast<int>(m.points.size() / 3);
    for (int i = 0; i < n; ++i)
        if (std::abs(m.points[static_cast<std::size_t>(i) * 3 + 2] - z) < 1e-9)
            out.push_back(i);
    return out;
}

}  // namespace

// ── (a) The gap closes and the load reaches the support ─────────────────────────
TEST_CASE("contact: stacked boxes with a 0.05 mm gap close and carry the load") {
    const double gap = 0.05;
    Mesh m = stacked_boxes(gap);
    ContactCandidateOptions copt;
    copt.max_gap_mm = 0.5;
    auto cand = fea_contact_candidates(m.points, m.tets, 4, kE, copt);
    REQUIRE(cand.error.empty());
    CHECK(cand.n_components == 2);
    REQUIRE_FALSE(cand.springs.empty());
    // Every candidate sits on the interface, faces along z, and its initial gap is the
    // real 0.05 mm air gap — the pairing came from the mesh boundary, not from the CAD.
    for (const auto& s : cand.springs) {
        CHECK(std::abs(s.normal[2]) == doctest::Approx(1.0).epsilon(1e-9));
        CHECK(s.gap0_mm == doctest::Approx(gap).epsilon(1e-6));
        CHECK(s.area_mm2 > 0.0);
    }

    const int n_nodes = static_cast<int>(m.points.size() / 3);
    std::vector<double> forces(static_cast<std::size_t>(n_nodes) * 3, 0.0);
    const std::vector<int> base = nodes_at_z(m, 0.0);
    const std::vector<int> top = nodes_at_z(m, 20.0 + gap);
    REQUIRE_FALSE(base.empty());
    REQUIRE_FALSE(top.empty());
    const double F = 4000.0;  // N pressing DOWN on the upper box
    for (int nid : top) forces[static_cast<std::size_t>(nid) * 3 + 2] = -F / top.size();

    FeaContactReport report;
    FeaStaticResult r = fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, base,
                                                 forces, 4, {}, cand.springs,
                                                 FeaContactOptions{}, report);
    REQUIRE(r.ok);
    CHECK(report.converged);
    CHECK(report.contact_iterations >= 1);
    CHECK(report.active_pairs > 0);
    CHECK(report.candidate_pairs == static_cast<int>(cand.springs.size()));
    REQUIRE(report.groups.size() == 1);

    // The whole load crosses the closed contact into the fixed base.
    CHECK(r.reaction_force_n[2] == doctest::Approx(F).epsilon(0.01));
    CHECK(r.equilibrium_error_pct < 1.0);
    CHECK(report.groups[0].contact_force_n == doctest::Approx(F).epsilon(0.05));
    // Group orientation is canonical: component 0 (the lower box) pushes component 1
    // (the upper box) away from itself, i.e. UP in +z.
    CHECK(report.groups[0].force_n[2] > 0.0);
    CHECK(report.groups[0].max_contact_pressure_mpa > 0.0);
    // The stabilizer must not be carrying the load — this is a frictionless answer.
    CHECK(report.tangential_force_fraction < 0.01);

    // Travel of the upper box = the gap it closes + the elastic compression of both
    // boxes (F*L/(A*E) each) + the penalty indentation (k is E*h, so ~1e-3 mm here).
    const double A = 20.0 * 20.0, L = 10.0;
    const double squash = 2.0 * F * L / (A * kE);
    CHECK(r.max_disp_mm == doctest::Approx(gap + squash).epsilon(0.15));
    CHECK(r.max_disp_mm > gap);  // it really closed the gap
    std::printf("(a) iters=%d active=%d/%d Rz=%.2f N contact=%.2f N pmax=%.3f MPa "
                "disp=%.5f mm (gap+squash=%.5f)\n",
                report.contact_iterations, report.active_pairs, report.candidate_pairs,
                r.reaction_force_n[2], report.groups[0].contact_force_n,
                report.groups[0].max_contact_pressure_mpa, r.max_disp_mm, gap + squash);
}

// ── (b) Reverse the load: the contact separates, so there is no static solution ──
TEST_CASE("contact: a load that lifts the part off is refused, not solved") {
    const double gap = 0.05;
    Mesh m = stacked_boxes(gap);
    ContactCandidateOptions copt;
    copt.max_gap_mm = 0.5;
    auto cand = fea_contact_candidates(m.points, m.tets, 4, kE, copt);
    REQUIRE(cand.error.empty());
    REQUIRE_FALSE(cand.springs.empty());

    const int n_nodes = static_cast<int>(m.points.size() / 3);
    std::vector<double> forces(static_cast<std::size_t>(n_nodes) * 3, 0.0);
    const std::vector<int> base = nodes_at_z(m, 0.0);
    const std::vector<int> top = nodes_at_z(m, 20.0 + gap);
    const double F = 4000.0;  // N pulling UP on the upper box
    for (int nid : top) forces[static_cast<std::size_t>(nid) * 3 + 2] = F / top.size();

    FeaContactReport report;
    FeaStaticResult r = fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, base,
                                                 forces, 4, {}, cand.springs,
                                                 FeaContactOptions{}, report);
    CHECK_FALSE(r.ok);
    CHECK(r.error.find("contact separated") != std::string::npos);
    // The refusal still reports the state it refused on: every pair released.
    CHECK(report.candidate_pairs == static_cast<int>(cand.springs.size()));
    CHECK(report.active_pairs == 0);
    CHECK(report.contact_iterations >= 2);  // one solve to discover the tension
    // No fabricated field came back with the refusal.
    CHECK(r.max_disp_mm == 0.0);
    std::printf("(b) refused after %d iteration(s): %s\n", report.contact_iterations,
                r.error.c_str());
}

// ── (c) Pin in a clearance bore, loaded transversely: one-sided contact ─────────
TEST_CASE("contact: a pin in a 0.1 mm clearance bore touches on one side only") {
    // Pin: tube r 3..5 mm. Bore: tube r 5.1..9 mm. Radial clearance 0.1 mm.
    const double clearance = 0.1;
    Mesh m;
    add_tube(m, 2, 16, 2, 3.0, 5.0, 0.0, 10.0, 0.0, 0.0);                    // pin
    add_tube(m, 2, 16, 2, 5.0 + clearance, 9.0, 0.0, 10.0, 0.0, 0.0);        // bore
    ContactCandidateOptions copt;
    copt.max_gap_mm = 0.4;
    auto cand = fea_contact_candidates(m.points, m.tets, 4, kE, copt);
    REQUIRE(cand.error.empty());
    CHECK(cand.n_components == 2);
    REQUIRE_FALSE(cand.springs.empty());

    const int n_nodes = static_cast<int>(m.points.size() / 3);
    std::vector<double> forces(static_cast<std::size_t>(n_nodes) * 3, 0.0);
    // Fixture: the bore's outer cylinder (r == 9).
    std::vector<int> fixed;
    std::vector<int> pin_nodes;
    for (int i = 0; i < n_nodes; ++i) {
        const double x = m.points[static_cast<std::size_t>(i) * 3];
        const double y = m.points[static_cast<std::size_t>(i) * 3 + 1];
        const double r = std::sqrt(x * x + y * y);
        if (std::abs(r - 9.0) < 1e-6) fixed.push_back(i);
        if (r < 5.0 + 0.5 * clearance) pin_nodes.push_back(i);
    }
    REQUIRE_FALSE(fixed.empty());
    REQUIRE_FALSE(pin_nodes.empty());
    const double F = 1500.0;  // N pushing the pin in +x
    for (int nid : pin_nodes)
        forces[static_cast<std::size_t>(nid) * 3] = F / pin_nodes.size();

    FeaContactReport report;
    FeaStaticResult r = fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, fixed,
                                                 forces, 4, {}, cand.springs,
                                                 FeaContactOptions{}, report);
    REQUIRE(r.ok);
    CHECK(report.converged);
    CHECK(report.active_pairs > 0);
    // One-sided: the pin cannot be pressed against the whole bore at once.
    CHECK(report.active_pairs < report.candidate_pairs);
    REQUIRE(report.groups.size() == 1);
    // The bore reacts the applied load.
    CHECK(r.reaction_force_n[0] == doctest::Approx(-F).epsilon(0.01));
    CHECK(r.equilibrium_error_pct < 1.0);
    // Which side carries it: the resultant the pin presses into the bore is +x, so the
    // active arc is the +x side of the bore.
    const double fx = report.groups[0].force_n[0];
    CHECK(fx == doctest::Approx(F).epsilon(0.1));
    CHECK(std::abs(report.groups[0].force_n[1]) < 0.2 * std::abs(fx));

    // Every active pair really is on the loaded side; no pair on the far side carries load.
    int active_plus_x = 0, active_minus_x = 0;
    for (const auto& s : cand.springs) {
        double rel = 0.0;
        for (int d = 0; d < 3; ++d)
            rel += s.normal[d] * (r.displacement[static_cast<std::size_t>(s.b) * 3 + d] -
                                  r.displacement[static_cast<std::size_t>(s.a) * 3 + d]);
        if (s.gap0_mm + rel >= 0.0) continue;  // separated
        (s.normal[0] > 0.0 ? active_plus_x : active_minus_x)++;
    }
    CHECK(active_plus_x > 0);
    CHECK(active_minus_x == 0);
    std::printf("(c) iters=%d active=%d/%d (+x %d, -x %d) Rx=%.2f N contact=(%.1f, %.1f, "
                "%.1f) N pmax=%.3f MPa\n",
                report.contact_iterations, report.active_pairs, report.candidate_pairs,
                active_plus_x, active_minus_x, r.reaction_force_n[0],
                report.groups[0].force_n[0], report.groups[0].force_n[1],
                report.groups[0].force_n[2],
                report.groups[0].max_contact_pressure_mpa);
}
