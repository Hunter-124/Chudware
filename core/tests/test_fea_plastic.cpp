// Small-strain J2 (von Mises) plasticity gates — the material law that lets a
// bearing edge REDISTRIBUTE.
//
// The defect these gates exist for is a hinge-saddle contact study: a frictionless linear-elastic
// contact funnelled 1446 N through 12 node pairs on a sharp saddle edge and reported
// a mean bearing stress of 450 MPa against a 276 MPa yield. That is not a stress
// state — a linear-elastic material cannot yield, so the load has nowhere to go and
// the answer is a discretization reading dressed as a design number. With an
// elastic-plastic law the edge yields, the patch GROWS until the load fits inside the
// flow stress, and the reported pressure lands at the material's own limit.
//
// Four things are pinned here, each of them a statement the closed form can check:
//   (a) a bar loaded past yield with linear hardening reproduces the BILINEAR law:
//       the stress is F/A (equilibrium, which plasticity may not break) and the
//       equivalent plastic strain is (F/A - Y)/H,
//   (b) the same bar PERFECTLY plastic at 1.2x its limit load is REFUSED as a
//       collapse mechanism, at a load fraction that is the limit load itself — not
//       solved into a garbage last iterate,
//   (c) pin-on-edge contact, elastic versus plastic on the SAME mesh and load: the
//       plastic contact patch grows and the mean pressure falls to about yield,
//   (d) plasticity is path-dependent, so a converged run must be increment-count
//       stable: 4 increments and 16 increments have to agree.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <cmath>
#include <cstdio>
#include <set>
#include <vector>

#include "sim/fea_contact.h"
#include "sim/fea_static.h"

using chudware::sim::ContactCandidateOptions;
using chudware::sim::fea_contact_candidates;
using chudware::sim::fea_static_solve_contact;
using chudware::sim::fea_static_solve_plastic;
using chudware::sim::FeaContactOptions;
using chudware::sim::FeaContactReport;
using chudware::sim::FeaPlasticity;
using chudware::sim::FeaPlasticReport;
using chudware::sim::FeaStaticResult;

namespace {

struct Mesh {
    std::vector<double> points;
    std::vector<int> tets;
};

// Freudenthal 6-tet split of a structured grid over the box
// [x0,x0+Lx] x [y0,y0+Ly] x [z0,z0+Lz], appended into `m`.
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

// A hollow cylindrical tube (annulus r_in..r_out over z0..z0+Lz) — the same helper
// test_fea_contact builds its pin-in-a-clearance-bore interface from, so the plastic
// gate below runs on the SAME edge-bearing geometry the elastic contact gates use.
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

// ── The axial bar ────────────────────────────────────────────────────────────────
// 24 x 2 x 2 mm, clamped on x = 0 (all three DOFs, the only fixture this kernel
// takes) and pulled in +x on x = 24. Two things about it are deliberate:
//
//   * the end traction is WORK-EQUIVALENT (an edge node of a structured face owns
//     half a cell, a corner a quarter), not F/n per node. F/n is a point load on
//     every corner — four times the traction its tributary area implies — and a
//     point load on an elastic-plastic body yields LOCALLY without bound: measured,
//     it moved the collapse load of case (b) from 0.83 to 0.68 of the applied load
//     and put 22 % of extra plastic strain into case (a)'s peak;
//   * the closed form is read in the MIDDLE THIRD. The clamp constrains the lateral
//     contraction, so a boundary layer about one width deep is triaxial and is not
//     uniaxial tension at all — its stress and its plastic strain are both higher,
//     legitimately, and they are not what a bilinear hand calculation describes.
struct Bar {
    Mesh mesh;
    std::vector<int> fixed;
    std::vector<double> forces;
    double area_mm2 = 0.0;
};
Bar make_bar(double force_n, double length_mm = 24.0, double side_mm = 2.0) {
    Bar b;
    add_box(b.mesh, 24, 2, 2, 0.0, 0.0, 0.0, length_mm, side_mm, side_mm);
    b.area_mm2 = side_mm * side_mm;
    const int n = static_cast<int>(b.mesh.points.size() / 3);
    b.forces.assign(static_cast<std::size_t>(n) * 3, 0.0);
    std::vector<double> weight(static_cast<std::size_t>(n), 0.0);
    double total = 0.0;
    for (int i = 0; i < n; ++i) {
        const double x = b.mesh.points[static_cast<std::size_t>(i) * 3];
        const double y = b.mesh.points[static_cast<std::size_t>(i) * 3 + 1];
        const double z = b.mesh.points[static_cast<std::size_t>(i) * 3 + 2];
        if (std::abs(x) < 1e-9) b.fixed.push_back(i);
        if (std::abs(x - length_mm) > 1e-9) continue;
        const double wy =
            (std::abs(y) < 1e-9 || std::abs(y - side_mm) < 1e-9) ? 0.5 : 1.0;
        const double wz =
            (std::abs(z) < 1e-9 || std::abs(z - side_mm) < 1e-9) ? 0.5 : 1.0;
        weight[static_cast<std::size_t>(i)] = wy * wz;
        total += wy * wz;
    }
    for (int i = 0; i < n; ++i)
        if (weight[static_cast<std::size_t>(i)] > 0.0)
            b.forces[static_cast<std::size_t>(i) * 3] =
                force_n * weight[static_cast<std::size_t>(i)] / total;
    return b;
}
// Mean of a nodal field over the bar's middle third — the region that is genuinely
// in uniaxial tension (see Bar).
double bar_mid_mean(const Mesh& m, const std::vector<double>& field, double length_mm) {
    double sum = 0.0;
    int count = 0;
    const int n = static_cast<int>(m.points.size() / 3);
    for (int i = 0; i < n; ++i) {
        const double x = m.points[static_cast<std::size_t>(i) * 3];
        if (x < length_mm / 3.0 || x > 2.0 * length_mm / 3.0) continue;
        sum += field[static_cast<std::size_t>(i)];
        ++count;
    }
    return count > 0 ? sum / count : 0.0;
}

// ── The pin-on-edge bearing ──────────────────────────────────────────────────────
// A pin (tube r 3..5) resting in a clearance bore (tube r 5+c..9) and loaded
// transversely INTO the side it rests on. The pin is placed eccentric by exactly the
// clearance, so it TOUCHES at one line and the gap opens as c*(1 - cos(theta)) away
// from it — quadratically, which is the geometry of a sharp bearing edge and the
// shape of the rover hinge saddle. The arc that carries load is therefore whatever
// the pin's travel closes, and the whole question the plastic law answers is what
// that arc does once the pressure on it exceeds the flow stress.
struct PinBore {
    Mesh mesh;
    std::vector<int> fixed;
    std::vector<double> forces;
};
PinBore make_pin_bore(double force_n, double clearance, int nt, int nz) {
    PinBore p;
    // Pin centre offset by the clearance: seated on the +x side, load pressing +x.
    add_tube(p.mesh, 2, nt, nz, 3.0, 5.0, 0.0, 10.0, clearance, 0.0);        // pin
    add_tube(p.mesh, 2, nt, nz, 5.0 + clearance, 9.0, 0.0, 10.0, 0.0, 0.0);  // bore
    const int n = static_cast<int>(p.mesh.points.size() / 3);
    p.forces.assign(static_cast<std::size_t>(n) * 3, 0.0);
    std::vector<int> pin_nodes;
    for (int i = 0; i < n; ++i) {
        const double x = p.mesh.points[static_cast<std::size_t>(i) * 3];
        const double y = p.mesh.points[static_cast<std::size_t>(i) * 3 + 1];
        if (std::abs(std::sqrt(x * x + y * y) - 9.0) < 1e-6) p.fixed.push_back(i);
        const double rp = std::sqrt((x - clearance) * (x - clearance) + y * y);
        if (rp < 5.0 + 1e-6) pin_nodes.push_back(i);
    }
    for (int nid : pin_nodes)
        p.forces[static_cast<std::size_t>(nid) * 3] =
            force_n / static_cast<double>(pin_nodes.size());
    return p;
}

struct ContactPatch {
    double force_n = 0.0;
    double area_mm2 = 0.0;
    double mean_pressure_mpa = 0.0;
    int active_pairs = 0;
};
ContactPatch patch_of(const FeaContactReport& report) {
    ContactPatch out;
    for (const auto& g : report.groups) {
        out.force_n += g.contact_force_n;
        out.area_mm2 += g.contact_area_mm2;
        out.active_pairs += g.active_pairs;
    }
    out.mean_pressure_mpa = out.area_mm2 > 0.0 ? out.force_n / out.area_mm2 : 0.0;
    return out;
}

}  // namespace

// ── (a) The bilinear law, read off a bar loaded past yield ──────────────────────
TEST_CASE("plastic: an axial bar past yield reproduces the bilinear closed form") {
    const double L = 24.0, side = 2.0;
    const double H = 2000.0;         // hardening tangent modulus, MPa
    const double target_mpa = 300.0; // 1.2x yield, 50 MPa into the hardening branch
    const double area = side * side;
    const double F = target_mpa * area;
    Bar b = make_bar(F, L, side);

    FeaPlasticity pl;
    pl.enabled = true;
    pl.yield_mpa = kYield;
    pl.hardening_mpa = H;
    pl.max_increments = 8;
    pl.tol = 1e-6;
    FeaContactReport creport;
    FeaPlasticReport preport;
    FeaStaticResult r = fea_static_solve_plastic(
        b.mesh.points, b.mesh.tets, kE, kNu, kYield, b.fixed, b.forces, 4, {}, {},
        FeaContactOptions{}, pl, creport, preport);
    REQUIRE(r.error.empty());
    REQUIRE(r.ok);
    CHECK(preport.enabled);
    CHECK(preport.converged);
    CHECK(preport.load_fraction == doctest::Approx(1.0));
    // The ladder is the requested increments plus the one that reaches first yield.
    CHECK(preport.increments == 9);
    CHECK(preport.newton_iterations_total > 0);

    // Equilibrium fixes the stress: sigma = F/A whatever the material law is, so this
    // is the check that the return mapping did not lose the load.
    const double vm_mid = bar_mid_mean(b.mesh, r.von_mises, L);
    CHECK(vm_mid == doctest::Approx(target_mpa).epsilon(0.005));  // 0.5 %

    // The material law fixes the plastic strain: sigma = Y + H*eq_p.
    const double eqp_closed = (target_mpa - kYield) / H;  // 0.025
    const double eqp_mid = bar_mid_mean(b.mesh, r.equivalent_plastic_strain, L);
    CHECK(eqp_mid == doctest::Approx(eqp_closed).epsilon(0.01));  // 1 %
    // The Gauss-point peak sits in the clamp's triaxial boundary layer, so it is
    // higher than the uniaxial value — but it is the same order, not a runaway.
    CHECK(preport.max_equivalent_plastic_strain >= eqp_closed);
    CHECK(preport.max_equivalent_plastic_strain < 4.0 * eqp_closed);

    // A bar loaded uniformly past yield is plastic essentially everywhere.
    CHECK(preport.plastic_fraction > 0.9);
    CHECK(preport.plastic_volume_mm3 ==
          doctest::Approx(preport.plastic_fraction * L * side * side).epsilon(0.02));
    CHECK(preport.yielded_nodes > 0);
    // And the reported stress is CAPPED by the flow stress: no node may exceed
    // Y + H*eq_p, which is the whole point of the law.
    CHECK(r.max_von_mises_raw_mpa <=
          kYield + H * preport.max_equivalent_plastic_strain + 1.0);

    std::printf("(a) sigma_mid=%.3f MPa (target %.1f) eqp_mid=%.6f (closed %.6f) "
                "peak_eqp=%.6f incs=%d newton=%d u_max=%.5f vm_peak=%.2f pf=%.3f\n",
                vm_mid, target_mpa, eqp_mid, eqp_closed,
                preport.max_equivalent_plastic_strain, preport.increments,
                preport.newton_iterations_total, r.max_disp_mm,
                r.max_von_mises_raw_mpa, preport.plastic_fraction);
    // NOT checked against L*(sigma/E + eq_p): the peak displacement is the free end's,
    // and it accumulates the clamp boundary layer's extra plastic strain on top of the
    // uniaxial elongation — measured 18 % above the hand calculation at this mesh and
    // 37 % at twice the section refinement, i.e. it is a mesh-dependent boundary-layer
    // number and pinning it would pin the boundary layer, not the material law.
}

// ── (b) Perfectly plastic past the limit load is a MECHANISM, not a result ──────
TEST_CASE("plastic: a perfectly plastic bar at 1.2x its limit load is refused") {
    const double L = 24.0, side = 2.0, area = side * side;
    const double limit_n = kYield * area;  // the fully plastic section, 1000 N
    Bar b = make_bar(1.2 * limit_n, L, side);

    FeaPlasticity pl;
    pl.enabled = true;
    pl.yield_mpa = kYield;
    pl.hardening_mpa = 0.0;  // perfectly plastic: no reserve past the plastic hinge
    pl.max_increments = 10;
    pl.tol = 1e-6;
    FeaContactReport creport;
    FeaPlasticReport preport;
    FeaStaticResult r = fea_static_solve_plastic(
        b.mesh.points, b.mesh.tets, kE, kNu, kYield, b.fixed, b.forces, 4, {}, {},
        FeaContactOptions{}, pl, creport, preport);
    // Refused, not solved: there IS no equilibrium at this load.
    CHECK_FALSE(r.ok);
    CHECK(r.error.find("collapse") != std::string::npos);
    CHECK_FALSE(preport.converged);
    // And the refusal is quantitative: the load fraction it did carry is the limit
    // load of the section, 1/1.2 = 0.833 of what was applied.
    CHECK(preport.load_fraction > 0.6);
    CHECK(preport.load_fraction < 0.98);
    CHECK(preport.load_fraction == doctest::Approx(1.0 / 1.2).epsilon(0.2));
    CHECK(preport.increments >= 1);
    std::printf("(b) refused at load_fraction=%.4f (limit/applied = %.4f) after %d "
                "increment(s), %d Newton iteration(s)\n  %s\n",
                preport.load_fraction, 1.0 / 1.2, preport.increments,
                preport.newton_iterations_total, r.error.c_str());
    // No stress field is offered for a load nobody can carry.
    CHECK(r.von_mises.empty());

    // The SAME bar below its limit load converges — the refusal is about the load,
    // not about perfect plasticity being unsupported.
    Bar ok_bar = make_bar(0.9 * limit_n, L, side);
    FeaPlasticReport ok_report;
    FeaStaticResult ok = fea_static_solve_plastic(
        ok_bar.mesh.points, ok_bar.mesh.tets, kE, kNu, kYield, ok_bar.fixed,
        ok_bar.forces, 4, {}, {}, FeaContactOptions{}, pl, creport, ok_report);
    REQUIRE(ok.ok);
    CHECK(ok_report.converged);
    CHECK(bar_mid_mean(ok_bar.mesh, ok.von_mises, L) ==
          doctest::Approx(0.9 * kYield).epsilon(0.01));
}

// ── (c) Pin-on-edge bearing: the plastic patch GROWS and the pressure falls ─────
TEST_CASE("plastic: a yielding bearing edge grows its contact patch") {
    // 0.6 mm of radial clearance on a r 5 pin, so the seat opens quadratically away
    // from the touching line and the elastic patch is a narrow arc. 16 kN through it
    // is 3.2x yield as an elastic mean pressure — the rover hinge's own failure
    // mode, at the same order of overstress (450 MPa on a 276 MPa yield).
    const double clearance = 0.6;
    const double F = 16000.0;
    PinBore pb = make_pin_bore(F, clearance, /*nt=*/48, /*nz=*/4);
    ContactCandidateOptions copt;
    copt.max_gap_mm = 0.9;
    auto cand = fea_contact_candidates(pb.mesh.points, pb.mesh.tets, 4, kE, copt);
    REQUIRE(cand.error.empty());
    REQUIRE(cand.n_components == 2);
    REQUIRE_FALSE(cand.springs.empty());
    // Default seat rule: only the pairs the closure test itself calls closed start
    // active, which on a seated pin is the touching line.
    const FeaContactOptions copts;

    // Elastic: the arc is whatever the pin's travel closes, and the load funnels
    // through it at whatever pressure that implies — there is no mechanism by which
    // a linear-elastic material could spread it.
    FeaContactReport elastic_report;
    FeaStaticResult elastic = fea_static_solve_contact(
        pb.mesh.points, pb.mesh.tets, kE, kNu, kYield, pb.fixed, pb.forces, 4, {},
        cand.springs, copts, elastic_report);
    REQUIRE(elastic.ok);
    REQUIRE(elastic_report.converged);
    const ContactPatch ep = patch_of(elastic_report);
    REQUIRE(ep.area_mm2 > 0.0);

    // Plastic: same mesh, same load, same candidate set.
    FeaPlasticity pl;
    pl.enabled = true;
    pl.yield_mpa = kYield;
    pl.hardening_mpa = 1000.0;
    pl.max_increments = 10;
    pl.tol = 1e-4;
    FeaContactReport plastic_report;
    FeaPlasticReport preport;
    FeaStaticResult plastic = fea_static_solve_plastic(
        pb.mesh.points, pb.mesh.tets, kE, kNu, kYield, pb.fixed, pb.forces, 4, {},
        cand.springs, copts, pl, plastic_report, preport);
    REQUIRE(plastic.error.empty());
    REQUIRE(plastic.ok);
    CHECK(preport.converged);
    CHECK(plastic_report.converged);
    const ContactPatch pp = patch_of(plastic_report);
    REQUIRE(pp.area_mm2 > 0.0);

    std::printf("(c) F=%.0f N  ELASTIC: %d pairs, %.3f mm^2, %.1f N, mean %.1f MPa, "
                "vM %.1f MPa\n"
                "        PLASTIC: %d pairs, %.3f mm^2, %.1f N, mean %.1f MPa, "
                "vM %.1f MPa, eqp %.5f, plastic %.2f%% of volume, %d incs / %d newton\n"
                "        area growth %.2fx, pressure ratio %.3f of yield\n",
                F, ep.active_pairs, ep.area_mm2, ep.force_n, ep.mean_pressure_mpa,
                elastic.max_von_mises_raw_mpa, pp.active_pairs, pp.area_mm2, pp.force_n,
                pp.mean_pressure_mpa, plastic.max_von_mises_raw_mpa,
                preport.max_equivalent_plastic_strain, 100.0 * preport.plastic_fraction,
                preport.increments, preport.newton_iterations_total,
                pp.area_mm2 / ep.area_mm2, pp.mean_pressure_mpa / kYield);

    // The edge yielded, so the patch spread.
    CHECK(pp.area_mm2 >= 3.0 * ep.area_mm2);
    CHECK(pp.active_pairs > ep.active_pairs);
    // And the pressure it spread to is the material's, not the mesh's.
    CHECK(pp.mean_pressure_mpa <= 1.3 * kYield);
    CHECK(ep.mean_pressure_mpa > 1.3 * kYield);
    // The load still crosses the interface: redistribution is not leakage.
    CHECK(pp.force_n == doctest::Approx(F).epsilon(0.1));
    CHECK(plastic.equilibrium_error_pct < 1.0);
    CHECK(preport.max_equivalent_plastic_strain > 0.0);
    CHECK(preport.yielded_nodes > 0);
    CHECK(static_cast<int>(plastic.equivalent_plastic_strain.size()) == plastic.n_nodes);
}

// ── (d) Plasticity is path-dependent, so the increment count may not change it ──
TEST_CASE("plastic: a converged run is stable in the load-increment count") {
    const double L = 24.0, side = 2.0;
    Bar b = make_bar(300.0 * side * side, L, side);
    auto solve = [&](int increments) {
        FeaPlasticity pl;
        pl.enabled = true;
        pl.yield_mpa = kYield;
        pl.hardening_mpa = 2000.0;
        pl.max_increments = increments;
        pl.tol = 1e-7;
        FeaContactReport creport;
        FeaPlasticReport preport;
        FeaStaticResult r = fea_static_solve_plastic(
            b.mesh.points, b.mesh.tets, kE, kNu, kYield, b.fixed, b.forces, 4, {}, {},
            FeaContactOptions{}, pl, creport, preport);
        REQUIRE(r.ok);
        REQUIRE(preport.converged);
        return std::make_pair(r.max_disp_mm, preport.max_equivalent_plastic_strain);
    };
    const auto coarse = solve(4);
    const auto fine = solve(16);
    std::printf("(d) 4 increments: u=%.6f eqp=%.6f | 16 increments: u=%.6f eqp=%.6f\n",
                coarse.first, coarse.second, fine.first, fine.second);
    // Under linear hardening the return map is exact for a PROPORTIONAL stress path,
    // and the bar's middle third has one — but the clamp's triaxial boundary layer
    // does not: its stress ratio rotates as the plastic zone spreads through it, so
    // the two ladders integrate genuinely different paths there and agree to 0.1 %
    // rather than to solver tolerance. That is the real bound, and it is still tight
    // enough to catch the failure this gate exists for — a state committed from a
    // mid-Newton guess, or a return measured from the wrong increment's state, moves
    // these numbers by tens of percent.
    CHECK(coarse.first == doctest::Approx(fine.first).epsilon(1e-3));
    CHECK(coarse.second == doctest::Approx(fine.second).epsilon(1e-3));
}
