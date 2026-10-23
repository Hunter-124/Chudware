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

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <map>
#include <set>
#include <string>
#include <vector>
#include "environment_test_support.h"
#include "util/resource_governor.h"

#include "sim/fea_contact.h"
#include "sim/fea_run.h"
#include "sim/fea_selection.h"
#include "sim/fea_static.h"

using chudware::sim::ContactCandidateOptions;
using chudware::sim::fea_contact_candidates;
using chudware::sim::fea_static_solve_contact;
using chudware::sim::FeaContactOptions;
using chudware::sim::FeaContactReport;
using chudware::sim::FeaStaticResult;
using chudware::sim::selection_spans_contact;
using chudware::sim::SelectionSpan;

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

// ── A small SEATED PAD: the interface a coarse mesh cannot describe ─────────────
// A `pad` x `pad` mm boss sitting `gap` mm above a 20 x 20 x 10 mm plate. The
// interface is CONFORMAL (both faces flat and parallel, every candidate's initial
// gap exactly `gap`), so nothing about the physics is marginal: under load the
// seat carries a uniform pressure of F/pad^2, and every pair on it closes at
// once.
//
// What varies is the DISCRETIZATION. `plate_n` and `pad_n` subdivide the plate and
// the pad, i.e. they set the element size AT the interface, which is exactly what
// contact.interface_size_mm sets on a real study (there by refining the candidate
// faces locally instead of meshing the whole part that fine). One element across
// the pad puts four nodes on a seat that has to report a pressure; the tributary
// area those four nodes claim is the plate's own 6.7 mm cells, which is an order
// of magnitude larger than the seat.
Mesh seated_pad(int plate_n, int pad_n, double gap, double pad) {
    Mesh m;
    add_box(m, plate_n, plate_n, 2, 0.0, 0.0, 0.0, 20.0, 20.0, 10.0);
    add_box(m, pad_n, pad_n, 2, 10.0 - pad / 2, 10.0 - pad / 2, 10.0 + gap, pad, pad,
            8.0);
    return m;
}

// The active set the converged displacement field implies, and the tributary area
// it carries: a pair is pressed together when its gap has closed. The area is
// summed per NODE, not per pair — the candidate search pairs from both sides of
// the interface, so one node can appear in two pairs — which is the same rule the
// solver's own contact_area_mm2 uses.
struct ActivePatch {
    int pairs = 0;
    double area_mm2 = 0.0;
};
ActivePatch active_patch(const std::vector<chudware::sim::ContactSpring>& springs,
                         const std::vector<double>& displacement) {
    ActivePatch out;
    std::set<int> counted;
    for (const auto& s : springs) {
        double rel = 0.0;
        for (int d = 0; d < 3; ++d)
            rel += s.normal[static_cast<std::size_t>(d)] *
                   (displacement[static_cast<std::size_t>(s.b) * 3 + d] -
                    displacement[static_cast<std::size_t>(s.a) * 3 + d]);
        if (s.gap0_mm + rel >= 0.0) continue;
        ++out.pairs;
        if (counted.insert(s.a).second) out.area_mm2 += s.area_mm2;
    }
    return out;
}

// A deterministic pseudo-random value in [0,1) from one integer — the mesher's node
// placement is not random, but it IS arbitrary at this scale, and what varies
// between two meshes of the same solid is exactly this: where on the true surface
// each node landed.
double hash01(unsigned x) {
    x = x * 1664525u + 1013904223u;
    x ^= x >> 16;
    x *= 2246822519u;
    x ^= x >> 13;
    return static_cast<double>(x >> 8) / 16777216.0;
}

// Push one radial LAYER of the tube whose nodes start at `base` in or out by up to
// `amp` mm, node by node. This is the mesher's own surface residual on a curved face
// (measured 4.7e-3 mm mean / 0.40 mm max on the live keyway at h = 0.25 mm): it is
// what makes the CLOSEST APPROACH of a nominally-touching fit a matter of luck, and
// therefore what a seat rule keyed on the closest approach inherits.
void jitter_layer(Mesh& m, int base, int nr, int nt, int nz, int layer, double amp,
                  unsigned seed, double cx, double cy) {
    const int NR = nr + 1;
    for (int k = 0; k <= nz; ++k)
        for (int it = 0; it < nt; ++it) {
            const int nid = base + (k * nt + it) * NR + layer;
            const std::size_t at = static_cast<std::size_t>(nid) * 3;
            const double dx = m.points[at] - cx, dy = m.points[at + 1] - cy;
            const double r = std::sqrt(dx * dx + dy * dy);
            if (!(r > 0.0)) continue;
            const double d =
                amp * (2.0 * hash01(static_cast<unsigned>(nid) * 2654435761u + seed) -
                       1.0);
            m.points[at] = cx + dx * (r + d) / r;
            m.points[at + 1] = cy + dy * (r + d) / r;
        }
}

struct SolverEnvironment {
    std::string name, value;
    bool present;
    SolverEnvironment(const char* key, const char* setting) : name(key) {
        const char* old = std::getenv(key);
        present = old != nullptr;
        if (old) value = old;
        chudware::test::setOrUnsetEnvironment(key, setting);
    }
    ~SolverEnvironment() {
        chudware::test::setOrUnsetEnvironment(name.c_str(), present ? value.c_str() : nullptr);
    }
};

struct MemoryProbeScope {
    chudware::governor::HostMemoryProbe previous;
    explicit MemoryProbeScope(chudware::governor::HostMemoryProbe probe)
        : previous(chudware::governor::set_host_memory_probe(probe)) {}
    ~MemoryProbeScope() { chudware::governor::set_host_memory_probe(previous); }
};

void compare_linear_fields(const FeaStaticResult& actual, const FeaStaticResult& direct) {
    REQUIRE_MESSAGE(actual.ok, actual.error);
    REQUIRE_MESSAGE(direct.ok, direct.error);
    REQUIRE(actual.displacement.size() == direct.displacement.size());
    double squared_error = 0.0, squared_reference = 0.0;
    for (size_t i = 0; i < direct.displacement.size(); ++i) {
        squared_error += std::pow(actual.displacement[i] - direct.displacement[i], 2);
        squared_reference += direct.displacement[i] * direct.displacement[i];
    }
    CHECK(std::sqrt(squared_error / squared_reference) < 2e-6);
    const auto norm = [](const std::array<double, 3>& v) {
        return std::hypot(v[0], v[1], v[2]);
    };
    const double force_scale = std::max(
        {1.0, norm(direct.applied_force_n), norm(direct.reaction_force_n)});
    const double moment_scale = std::max(
        {1.0, norm(direct.applied_moment_nmm), norm(direct.reaction_moment_nmm)});
    std::array<double, 3> force_error{}, moment_error{}, moment_balance{};
    for (int d = 0; d < 3; ++d) {
        force_error[d] = actual.reaction_force_n[d] - direct.reaction_force_n[d];
        moment_error[d] = actual.reaction_moment_nmm[d] - direct.reaction_moment_nmm[d];
        moment_balance[d] = actual.reaction_moment_nmm[d] + actual.applied_moment_nmm[d];
    }
    // Relative vector accuracy must not become an absolute tolerance on a
    // symmetry-zero component. All components still contribute to each norm.
    CHECK(norm(force_error) / force_scale < 2e-6);
    CHECK(norm(moment_error) / moment_scale < 2e-6);
    CHECK(norm(moment_balance) / moment_scale < 2e-5);
    CHECK(actual.equilibrium_error_pct < 1e-4);
    REQUIRE(actual.element_von_mises.size() == direct.element_von_mises.size());
    for (size_t e = 0; e < direct.element_von_mises.size(); ++e) {
        CHECK(actual.element_von_mises[e] ==
              doctest::Approx(direct.element_von_mises[e]).epsilon(2e-5).scale(1.0));
        CHECK(actual.element_factor_of_safety[e] ==
              doctest::Approx(direct.element_factor_of_safety[e]).epsilon(2e-5).scale(1.0));
    }
    for (const auto& step : actual.iterative.solves) {
        CHECK(step.converged);
        CHECK(step.relative_residual <= 1e-10);
    }
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
    // A CONFORMAL interface is resolved however coarse the elements are next to the
    // gap: h/gap is 133 here and the resolution guard still passes it, because all
    // 16 pairs sit in the closing zone and seat together. That AND is deliberate —
    // refusing on the ratio alone would reject this study, whose reaction, contact
    // resultant and patch pressure are all verified below.
    const chudware::sim::ContactResolution flat =
        chudware::sim::fea_contact_resolution(cand);
    CHECK(flat.ratio > chudware::sim::kContactMaxHOverGap);
    CHECK(flat.seatable_pairs == flat.candidate_pairs);
    CHECK(flat.resolved);

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

TEST_CASE("contact: mixed three-body interfaces keep the press fit bonded and the pin unilateral") {
    // A structured section through the plate / bushing / pin load path. The
    // zero-gap press-fit section is bilateral; the running seat is 0.05 mm away.
    // The live STEP test covers the actual cylindrical geometry and materials.
    Mesh m;
    add_box(m, 3, 3, 2, 0, 0, 0, 20, 20, 10);
    add_box(m, 3, 3, 2, 0, 0, 10, 20, 20, 10);
    add_box(m, 3, 3, 2, 0, 0, 20.05, 20, 20, 10);
    ContactCandidateOptions opts;
    opts.max_gap_mm = 0.1;
    opts.bonded_pairs = {{1, 2}};
    opts.contact_pairs = {{2, 3}};
    opts.default_interface = chudware::sim::InterfaceKind::Free;
    auto candidates = fea_contact_candidates(m.points, m.tets, 4, kE, opts);
    REQUIRE(candidates.error.empty());
    REQUIRE(candidates.bonded.size() == 16);
    REQUIRE(candidates.springs.size() == 16);
    CHECK(candidates.min_gap0_mm == doctest::Approx(0.05));
    CHECK(chudware::sim::fea_contact_resolution(candidates).resolved);
    std::vector<chudware::sim::BondSpring> bonds;
    for (const auto& s : candidates.bonded) bonds.push_back({s.a, s.b, s.k});

    // Load only the sleeve in tension, with the pin independently restrained:
    // none of the unilateral springs can be mistaken for the tensile tie.
    auto fixed = nodes_at_z(m, 0);
    for (size_t i = 0; i < candidates.component.size(); ++i)
        if (candidates.component[i] == 2) fixed.push_back(static_cast<int>(i));
    std::vector<double> force(m.points.size(), 0);
    const auto sleeve_top = nodes_at_z(m, 20);
    for (int i : sleeve_top) force[i * 3 + 2] = 4000.0 / sleeve_top.size();
    auto tension = chudware::sim::fea_static_solve(
        m.points, m.tets, kE, kNu, kYield, fixed, force, 4, bonds);
    REQUIRE(tension.ok);
    CHECK(tension.reaction_force_n[2] == doctest::Approx(-4000).epsilon(0.01));
    double tensile_tie_force = 0;
    for (const auto& s : bonds)
        tensile_tie_force += s.k * (tension.displacement[s.b * 3 + 2] -
                                    tension.displacement[s.a * 3 + 2]);
    CHECK(tensile_tie_force == doctest::Approx(4000).epsilon(0.01));
    CHECK(tension.max_disp_mm < 0.01);

    using nlohmann::json;
    auto faces = [&](double z) {
        json samples = json::array();
        for (int i : nodes_at_z(m, z))
            samples.push_back({m.points[i * 3], m.points[i * 3 + 1], m.points[i * 3 + 2]});
        return json{{"points", samples}, {"tol_mm", 0.001}};
    };
    json study{{"points", m.points}, {"tets", m.tets}, {"nodes_per_elem", 4},
        {"material", {{"E_mpa", kE}, {"nu", kNu}, {"yield_mpa", kYield}}},
        {"fixtures", json::array({faces(0)})},
        {"loads", json::array({json{{"faces", faces(30.05)}, {"type", "force"},
                                    {"vector", {0, 0, -4000}}}})},
        {"contact", {{"max_gap_mm", 0.1}, {"penalty_scale", 0.5}, {"max_iterations", 50}}},
        {"interfaces", {{"bonded", {{1, 2}}}, {"contact", {{2, 3}}}, {"default", "free"}}}};
    auto packet = chudware::sim::fea_run_static(study);
    INFO(packet.dump());
    REQUIRE(packet["ok"] == true);
    CHECK(packet["contact"]["interfaces"]["bonded"][0]["pairs"] == 16);
    CHECK(packet["contact"]["interfaces"]["contact"][0]["pairs"] == 16);
    CHECK(packet["contact"]["interfaces"]["bonded"][0]["a"] == 1);
    CHECK(packet["contact"]["interfaces"]["contact"][0]["b"] == 3);
    CHECK(packet["contact"]["penalty_scale"] == 0.5);
    CHECK(packet["contact"]["load_path"]["fraction"].get<double>() > 0.95);
    study["loads"][0]["vector"] = {0, 0, 4000};
    auto lifted = chudware::sim::fea_run_static(study);
    REQUIRE(lifted["ok"] == false);
    const auto& separation = lifted["contact"]["load_path_refusal"];
    CHECK(separation["criterion"] == "normal_cone_excludes_load");
    CHECK(separation["normal_range"]["min"][2].get<double>() > 0.0);
    CHECK(separation["load_direction"][2] == 1.0);
    CHECK_FALSE(lifted.contains("summary"));

    study["interfaces"]["contact"] = {{2, 1}};
    auto conflict = chudware::sim::fea_run_static(study);
    CHECK(conflict["ok"] == false);
    CHECK(conflict["error"].get<std::string>().find("[1,2]") != std::string::npos);
    study["interfaces"]["contact"] = {{2, 9}};
    auto unknown = chudware::sim::fea_run_static(study);
    CHECK(unknown["ok"] == false);
    CHECK(unknown["error"].get<std::string>().find("ordinal 9") != std::string::npos);
    study["interfaces"]["contact"] = {{1, 3}};
    auto unmatched = chudware::sim::fea_run_static(study);
    CHECK(unmatched["ok"] == false);
    CHECK(unmatched["contact"]["interfaces"]["unmatched"][0]["a"] == 1);
    CHECK(unmatched["contact"]["interfaces"]["unmatched"][0]["b"] == 3);
}

TEST_CASE("contact: concentric plate bushing pin keeps both declared interfaces") {
    Mesh m;
    // Annular plate around a pressed-in sleeve, with the pin's 0.05 mm
    // running clearance. Hollow pin core avoids degenerate axis tetrahedra.
    add_tube(m, 2, 48, 3, 8, 12, 0, 6, 0, 0);
    add_tube(m, 2, 48, 3, 5.05, 8, 0, 6, 0, 0);
    add_tube(m, 2, 48, 3, 1, 5, 0, 6, 0, 0);
    ContactCandidateOptions options;
    options.max_gap_mm = 0.1;
    options.bonded_pairs = {{1, 2}};
    options.contact_pairs = {{2, 3}};
    auto candidates = fea_contact_candidates(m.points, m.tets, 4, kE, options);
    REQUIRE(candidates.error.empty());
    REQUIRE_FALSE(candidates.bonded.empty());
    REQUIRE_FALSE(candidates.springs.empty());
    std::vector<chudware::sim::BondSpring> bonds;
    for (const auto& s : candidates.bonded) {
        CHECK(candidates.component[s.a] == 0);
        CHECK(candidates.component[s.b] == 1);
        bonds.push_back({s.a, s.b, s.k});
    }
    for (const auto& s : candidates.springs) {
        CHECK(candidates.component[s.a] == 1);
        CHECK(candidates.component[s.b] == 2);
        CHECK(s.gap0_mm == doctest::Approx(0.05).epsilon(0.01));
    }
    std::vector<int> fixed;
    std::vector<double> forces(m.points.size(), 0);
    int pin_nodes = 0;
    for (int c : candidates.component) if (c == 2) ++pin_nodes;
    for (size_t i = 0; i < candidates.component.size(); ++i) {
        if (candidates.component[i] == 0 &&
            std::hypot(m.points[i * 3], m.points[i * 3 + 1]) > 11.99)
            fixed.push_back(static_cast<int>(i));
        if (candidates.component[i] == 2) forces[i * 3 + 1] = -4000.0 / pin_nodes;
    }
    FeaContactOptions solver_options;
    solver_options.max_iterations = 100;
    FeaContactReport report;
    auto solved = fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield,
        fixed, forces, 4, bonds, candidates.springs, solver_options, report);
    INFO(solved.error);
    REQUIRE(solved.ok);
    REQUIRE(report.converged);
    CHECK(report.active_pairs > 6);
    CHECK(solved.reaction_force_n[1] == doctest::Approx(4000).epsilon(0.01));
    CHECK(report.groups[0].force_n[1] == doctest::Approx(4000).epsilon(0.02));
    std::printf("mixed concentric: %zu bonds, %d/%d active contacts, %d iterations, Ry=%.3f N\n",
        bonds.size(), report.active_pairs, report.candidate_pairs,
        report.contact_iterations, solved.reaction_force_n[1]);
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

// ── (d) A coarse interface funnels the load, and the study is REFUSED ───────────
// The live defect this guard exists for: the FRAME-PEDAL-BRAKE keyway (0.350 mm
// measured clearance) was auto-coarsened to h = 2.91 mm at the interface — 8x the
// clearance — and its contact study CONVERGED with 1 of 848 candidate pairs
// active, 2902 N through that one node pair, and 13,543 MPa of reported "contact
// pressure". Equilibrium error 0.0%, plausibility ok: every other gate in the
// packet is about the SOLVE, and this defect is in the discretization.
//
// Here the same failure is reproduced with a 4 x 4 mm seat and one element across
// it. Note what the numbers do: four nodes claim 178 mm^2 of tributary area for a
// 16 mm^2 seat, so the "pressure" comes out at 22.5 MPa where the seat really
// carries 250 MPa — an 11x understatement that no other field discloses.
TEST_CASE("contact: a coarse interface that funnels the load is refused") {
    const double gap = 0.35, pad = 4.0;
    Mesh m = seated_pad(/*plate_n=*/3, /*pad_n=*/1, gap, pad);
    ContactCandidateOptions copt;
    copt.max_gap_mm = 0.5;
    auto cand = fea_contact_candidates(m.points, m.tets, 4, kE, copt);
    REQUIRE(cand.error.empty());
    REQUIRE_FALSE(cand.springs.empty());
    // The candidate set measures its OWN interface: element size at the pairs, the
    // closest approach, and how many pairs can seat.
    CHECK(cand.min_gap0_mm == doctest::Approx(gap).epsilon(1e-6));
    CHECK(cand.interface_h_mm > 4.0);

    const chudware::sim::ContactResolution res =
        chudware::sim::fea_contact_resolution(cand);
    CHECK_FALSE(res.resolved);
    CHECK(res.gap_mm == doctest::Approx(gap).epsilon(1e-6));
    CHECK(res.ratio > chudware::sim::kContactMaxHOverGap);
    CHECK(res.seatable_pairs < chudware::sim::kContactMinActivePairs);
    // The refusal names the size that resolves it, and it is a LOCAL size: four
    // elements across the clearance, not four across the part.
    CHECK(res.required_interface_size_mm > 0.0);
    CHECK(res.required_interface_size_mm <= 4.0 * gap + 1e-9);
    CHECK(res.reason.find("interface_size_mm") != std::string::npos);

    // And the solve really does produce the artefact the refusal describes: it
    // converges, it balances, and it prices a 16 mm^2 seat as ten times its area.
    const int n_nodes = static_cast<int>(m.points.size() / 3);
    std::vector<double> forces(static_cast<std::size_t>(n_nodes) * 3, 0.0);
    const std::vector<int> base = nodes_at_z(m, 0.0);
    const std::vector<int> top = nodes_at_z(m, 18.0 + gap);
    REQUIRE_FALSE(base.empty());
    REQUIRE_FALSE(top.empty());
    const double F = 4000.0;
    for (int nid : top) forces[static_cast<std::size_t>(nid) * 3 + 2] = -F / top.size();
    FeaContactReport report;
    FeaStaticResult r = fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, base,
                                                 forces, 4, {}, cand.springs,
                                                 FeaContactOptions{}, report);
    REQUIRE(r.ok);
    CHECK(report.converged);
    CHECK(r.equilibrium_error_pct < 1.0);
    REQUIRE(report.groups.size() == 1);
    const chudware::sim::FeaContactGroup& g = report.groups[0];
    CHECK(g.contact_force_n == doctest::Approx(F).epsilon(0.05));
    CHECK(report.active_pairs < chudware::sim::kContactMinActivePairs);
    CHECK(g.contact_area_mm2 > 5.0 * pad * pad);
    const double mean_pressure = g.contact_force_n / g.contact_area_mm2;
    CHECK(mean_pressure < 0.2 * F / (pad * pad));

    // The a-posteriori clause refuses on the active set alone, so a geometry whose
    // funnel only shows up after the solve is caught too.
    chudware::sim::ContactResolution solved = res;
    solved.resolved = true;
    solved.reason.clear();
    chudware::sim::fea_contact_resolution_after_solve(solved, report.active_pairs,
                                                      g.contact_force_n,
                                                      g.contact_area_mm2);
    CHECK_FALSE(solved.resolved);
    CHECK_FALSE(solved.pressure_reliable);
    CHECK(solved.reason.find("active pair") != std::string::npos);
    std::printf("(d) h_iface=%.3f gap=%.3f ratio=%.1f seatable=%d/%d -> REFUSED "
                "(needs %.3f mm); solve: converged=%d active=%d/%d %.1f N over "
                "%.2f mm^2 = %.2f MPa where the seat carries %.2f MPa, equil=%.4f%%\n",
                res.h_interface_mm, res.gap_mm, res.ratio, res.seatable_pairs,
                res.candidate_pairs, res.required_interface_size_mm,
                report.converged ? 1 : 0, report.active_pairs, report.candidate_pairs,
                g.contact_force_n, g.contact_area_mm2, mean_pressure,
                F / (pad * pad), r.equilibrium_error_pct);
}

// ── (e) The same seat with interface_size_mm set resolves ───────────────────────
// Same geometry, same clearance, same load — only the element size AT the
// interface changed, which is what contact.interface_size_mm buys (local
// refinement of the candidate faces, so the part is not meshed this fine).
TEST_CASE("contact: interface_size_mm resolves the same seat into a real patch") {
    const double gap = 0.35, pad = 4.0;
    Mesh m = seated_pad(/*plate_n=*/20, /*pad_n=*/8, gap, pad);  // 0.5-1.0 mm seat
    ContactCandidateOptions copt;
    copt.max_gap_mm = 0.5;
    auto cand = fea_contact_candidates(m.points, m.tets, 4, kE, copt);
    REQUIRE(cand.error.empty());
    REQUIRE_FALSE(cand.springs.empty());
    CHECK(cand.interface_h_mm < 2.0);

    const chudware::sim::ContactResolution res =
        chudware::sim::fea_contact_resolution(cand);
    CHECK(res.resolved);
    CHECK(res.reason.empty());
    CHECK(res.seatable_pairs >= chudware::sim::kContactMinActivePairs);

    const int n_nodes = static_cast<int>(m.points.size() / 3);
    std::vector<double> forces(static_cast<std::size_t>(n_nodes) * 3, 0.0);
    const std::vector<int> base = nodes_at_z(m, 0.0);
    const std::vector<int> top = nodes_at_z(m, 18.0 + gap);
    REQUIRE_FALSE(base.empty());
    REQUIRE_FALSE(top.empty());
    const double F = 4000.0;
    for (int nid : top) forces[static_cast<std::size_t>(nid) * 3 + 2] = -F / top.size();
    FeaContactReport report;
    FeaStaticResult r = fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, base,
                                                 forces, 4, {}, cand.springs,
                                                 FeaContactOptions{}, report);
    REQUIRE(r.ok);
    CHECK(report.converged);
    CHECK(r.equilibrium_error_pct < 1.0);
    REQUIRE(report.groups.size() == 1);
    const chudware::sim::FeaContactGroup& g = report.groups[0];
    // A patch, not a funnel — and the load still crosses in full: a resolved
    // contact is not a softer one.
    CHECK(report.active_pairs >= chudware::sim::kContactMinActivePairs);
    CHECK(r.reaction_force_n[2] == doctest::Approx(F).epsilon(0.02));
    CHECK(g.contact_force_n == doctest::Approx(F).epsilon(0.05));

    // Pressure is now force over an area the mesh resolved. Node-pair tributary
    // areas cannot be tighter than the seat itself, nor wider than the seat plus
    // the one-element lateral band the pairing searches — and the solver's own
    // number agrees with the active set the displacement field implies.
    CHECK(g.contact_area_mm2 >= pad * pad);
    CHECK(g.contact_area_mm2 <=
          (pad + 2.0 * cand.interface_h_mm) * (pad + 2.0 * cand.interface_h_mm));
    const ActivePatch patch = active_patch(cand.springs, r.displacement);
    CHECK(patch.pairs == report.active_pairs);
    CHECK(patch.area_mm2 == doctest::Approx(g.contact_area_mm2).epsilon(1e-9));
    const double mean_pressure = g.contact_force_n / g.contact_area_mm2;
    const double seat_pressure = F / (pad * pad);
    CHECK(mean_pressure > 0.25 * seat_pressure);
    CHECK(mean_pressure <= g.max_contact_pressure_mpa + 1e-9);

    chudware::sim::ContactResolution solved = res;
    chudware::sim::fea_contact_resolution_after_solve(solved, report.active_pairs,
                                                      g.contact_force_n,
                                                      g.contact_area_mm2);
    CHECK(solved.resolved);
    CHECK(solved.pressure_reliable);
    std::printf("(e) h_iface=%.3f gap=%.3f ratio=%.2f seatable=%d/%d -> resolved; "
                "solve: active=%d/%d %.1f N over %.2f mm^2 = %.2f MPa (seat %.2f MPa, "
                "pmax %.2f MPa) Rz=%.1f N\n",
                res.h_interface_mm, res.gap_mm, res.ratio, res.seatable_pairs,
                res.candidate_pairs, report.active_pairs, report.candidate_pairs,
                g.contact_force_n, g.contact_area_mm2, mean_pressure, seat_pressure,
                g.max_contact_pressure_mpa, r.reaction_force_n[2]);
}

// ── (f) TOUCHING faces: the ratio is undefined, the patch decides ───────────────
// Two coincident faces have no clearance, so h/gap is 0/0. That used to read as
// a resolved interface — "clearance 0 mm", ratio 0, below every limit — however
// coarse the mesh was, which is exactly how the live keyway shipped a 1.299 mm
// interface with one active pair as a contact pressure. The judgement for a
// touching interface is the PATCH: enough pairs in contact, and enough elements
// across the candidate area.
TEST_CASE("contact: touching faces are judged on the patch, not on a 0 mm clearance") {
    Mesh m;
    add_box(m, 6, 6, 2, 0.0, 0.0, 0.0, 20.0, 20.0, 10.0);   // 3.33 mm cells
    add_box(m, 6, 6, 2, 0.0, 0.0, 10.0, 20.0, 20.0, 10.0);  // coincident at z=10
    ContactCandidateOptions copt;
    copt.max_gap_mm = 0.5;
    auto cand = fea_contact_candidates(m.points, m.tets, 4, kE, copt);
    REQUIRE(cand.error.empty());
    REQUIRE_FALSE(cand.springs.empty());
    // The interface really is coincident, so there is no clearance to resolve.
    CHECK(cand.min_gap0_mm == doctest::Approx(0.0).epsilon(1e-12));

    const chudware::sim::ContactResolution res =
        chudware::sim::fea_contact_resolution(cand);
    CHECK(chudware::sim::criterion_is(res.criterion, chudware::sim::kCriterionPatchArea));
    CHECK(res.gap_mm == 0.0);
    CHECK(res.ratio == 0.0);      // undefined, and reported as such
    CHECK(res.max_ratio == 0.0);  // the clearance limit does not apply
    CHECK(res.candidate_area_mm2 > 300.0);  // the 20 x 20 mm face
    // Three elements across the patch is the bar, and a 3.33 mm mesh on a 20 mm
    // square face clears it with room to spare.
    CHECK(res.max_h_interface_mm ==
          doctest::Approx(std::sqrt(res.candidate_area_mm2) /
                          chudware::sim::kContactMinPatchElems));
    CHECK(res.h_interface_mm <= res.max_h_interface_mm);
    CHECK(res.seatable_pairs >= chudware::sim::kContactMinActivePairs);
    CHECK(res.resolved);
    CHECK(res.reason.empty());

    // And it solves as a seated joint: the whole load crosses a real patch.
    const int n_nodes = static_cast<int>(m.points.size() / 3);
    std::vector<double> forces(static_cast<std::size_t>(n_nodes) * 3, 0.0);
    const std::vector<int> base = nodes_at_z(m, 0.0);
    const std::vector<int> top = nodes_at_z(m, 20.0);
    REQUIRE_FALSE(base.empty());
    REQUIRE_FALSE(top.empty());
    const double F = 5000.0;
    for (int nid : top) forces[static_cast<std::size_t>(nid) * 3 + 2] = -F / top.size();
    FeaContactReport report;
    FeaStaticResult r = fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, base,
                                                 forces, 4, {}, cand.springs,
                                                 FeaContactOptions{}, report);
    REQUIRE(r.ok);
    CHECK(report.converged);
    CHECK(report.active_pairs >= chudware::sim::kContactMinActivePairs);
    CHECK(r.reaction_force_n[2] == doctest::Approx(F).epsilon(0.02));
    REQUIRE(report.groups.size() == 1);
    const chudware::sim::FeaContactGroup& g = report.groups[0];
    chudware::sim::ContactResolution solved = res;
    chudware::sim::fea_contact_resolution_after_solve(solved, report.active_pairs,
                                                      g.contact_force_n,
                                                      g.contact_area_mm2);
    CHECK(solved.resolved);
    CHECK(solved.pressure_reliable);
    std::printf("(f) touching: h_iface=%.3f patch=%.1f mm^2 limit=%.3f seatable=%d/%d "
                "-> resolved; solve: active=%d %.1f N over %.2f mm^2\n",
                res.h_interface_mm, res.candidate_area_mm2, res.max_h_interface_mm,
                res.seatable_pairs, res.candidate_pairs, report.active_pairs,
                g.contact_force_n, g.contact_area_mm2);
}

// ── (g) …and a touching interface too coarse for its patch is still refused ─────
// The other half of the patch rule: a 4 x 4 mm seat carried by the plate's own
// 6.7 mm cells has fewer than three elements across it and fewer than six pairs
// on it. The refusal must talk about the PATCH — telling a caller to resolve a
// "0 mm clearance" is advice about a number that does not exist.
TEST_CASE("contact: a touching seat coarser than its own patch is refused") {
    const double pad = 4.0;
    Mesh m = seated_pad(/*plate_n=*/3, /*pad_n=*/1, /*gap=*/0.0, pad);
    ContactCandidateOptions copt;
    copt.max_gap_mm = 0.5;
    auto cand = fea_contact_candidates(m.points, m.tets, 4, kE, copt);
    REQUIRE(cand.error.empty());
    REQUIRE_FALSE(cand.springs.empty());

    const chudware::sim::ContactResolution res =
        chudware::sim::fea_contact_resolution(cand);
    CHECK(chudware::sim::criterion_is(res.criterion, chudware::sim::kCriterionPatchArea));
    CHECK_FALSE(res.resolved);
    CHECK(res.h_interface_mm > res.max_h_interface_mm);
    CHECK(res.seatable_pairs < chudware::sim::kContactMinActivePairs);
    CHECK(res.reason.find("TOUCH") != std::string::npos);
    CHECK(res.reason.find("patch") != std::string::npos);
    CHECK(res.reason.find("clearance 0") == std::string::npos);
    // The size it names refines the patch to the bar it just failed.
    CHECK(res.required_interface_size_mm > 0.0);
    CHECK(res.required_interface_size_mm <= res.max_h_interface_mm + 1e-9);
    std::printf("(g) touching+coarse: h_iface=%.3f patch=%.1f mm^2 limit=%.3f "
                "seatable=%d/%d -> refused, asks for %.3f mm\n",
                res.h_interface_mm, res.candidate_area_mm2, res.max_h_interface_mm,
                res.seatable_pairs, res.candidate_pairs,
                res.required_interface_size_mm);
}

// ── (h) A nominally-touching fit seats the same arc at every mesh size ──────────
// The defect this case exists for. The seat rule used to be "within TWICE the
// closest approach", which on a fit whose bodies were placed TOUCHING collapses to
// zero the instant one node lands inside the partner face — so only the pairs the
// discretization pushed through could seat, and that population is mesh noise. On
// the live FRAME-PEDAL-BRAKE keyway the same request seated 8, then 12, then 13,
// then 2 pairs across otherwise identical runs, and at 2 the study was refused as a
// point load.
//
// Here: a pin in a bore with 0.35 mm of radial clearance, PLACED TOUCHING on one
// side (the pin axis offset by the whole clearance), meshed at three sizes with a
// different node-placement residual on each. The physical answer does not depend on
// the mesh, so the SEATED ARC must not either: active pairs x h, per axial ring of
// nodes, is a length and it has to agree across the three meshes. The old rule
// cannot do this — its seed is #(gap <= 0), printed below, which swings by a factor
// of several between the same three meshes.
TEST_CASE("contact: a touching pin in a bore seats the same arc at every mesh size") {
    // Both bodies are thin rings, so the fit is COMPLIANT — which is the live
    // regime: on the keyway the load closes 0.42 mm, several times the 0.127 mm the
    // mesh can resolve, so the seated arc is decided by the LOAD and the geometry
    // and not by which nodes happened to land inside the partner face. A solid
    // thick-walled pin is the opposite case: 8 kN closes it by 2 um, an order below
    // any mesh's own surface residual, and then no seat rule can produce a
    // mesh-independent arc because the physics itself is below the noise floor.
    const double clearance = 0.35, r_pin = 6.0, Lz = 4.0, F = 20000.0;
    struct Leg {
        int nt;
        int nz;
        unsigned seed;
    };
    // h at the interface 0.45 / 0.35 / 0.28 mm — 8 to 14 elements across the seated
    // arc, so the arc is resolved on all three and what is being compared is the
    // seat rule, not the element count. The fourth leg re-meshes the middle size
    // with a different surface residual: that is the run-to-run case, where the
    // geometry is identical and only the noise moved.
    const Leg legs[4] = {{84, 9, 1u}, {108, 11, 8u}, {135, 14, 15u}, {108, 11, 23u}};
    double arc_mm[4] = {0.0, 0.0, 0.0, 0.0};
    int seatable[4] = {0, 0, 0, 0};
    for (int L = 0; L < 4; ++L) {
        const int nt = legs[L].nt, nz = legs[L].nz;
        const double h_theta = 2.0 * M_PI * r_pin / nt;
        // The wall is subdivided the same way on every leg: what varies between the
        // three meshes is the size AT THE INTERFACE, which is what
        // contact.interface_size_mm buys, not the bending stiffness of the rings.
        const int nr = 3;
        Mesh m;
        // Pin (r 4.5 .. 6), axis offset +x by the whole clearance: it touches the
        // bore at theta = 0 and opens to 2 x clearance at theta = pi.
        add_tube(m, nr, nt, nz, 4.5, r_pin, 0.0, Lz, clearance, 0.0);
        const int n_pin = static_cast<int>(m.points.size() / 3);
        jitter_layer(m, 0, nr, nt, nz, /*layer=*/nr, 0.05 * h_theta, legs[L].seed,
                     clearance, 0.0);
        // Bore (r 6.35 .. 7.85), fixed on its outer wall.
        add_tube(m, nr, nt, nz, r_pin + clearance, 7.85, 0.0, Lz, 0.0, 0.0);
        jitter_layer(m, n_pin, nr, nt, nz, /*layer=*/0, 0.05 * h_theta,
                     legs[L].seed + 977u, 0.0, 0.0);

        const int n_nodes = static_cast<int>(m.points.size() / 3);
        std::vector<double> forces(static_cast<std::size_t>(n_nodes) * 3, 0.0);
        std::vector<int> fixed;
        for (int i = n_pin; i < n_nodes; ++i) {
            const double x = m.points[static_cast<std::size_t>(i) * 3];
            const double y = m.points[static_cast<std::size_t>(i) * 3 + 1];
            if (std::sqrt(x * x + y * y) > 7.84) fixed.push_back(i);
        }
        REQUIRE_FALSE(fixed.empty());
        for (int i = 0; i < n_pin; ++i)
            forces[static_cast<std::size_t>(i) * 3] = F / n_pin;

        ContactCandidateOptions copt;
        copt.max_gap_mm = 0.4;
        auto cand = fea_contact_candidates(m.points, m.tets, 4, kE, copt);
        REQUIRE(cand.error.empty());
        REQUIRE(cand.n_components == 2);
        // The mesh really is placed touching, and the residual really does put nodes
        // through the partner face — which is why the closest approach is useless as
        // a seat scale and is reported as a penetration depth instead.
        CHECK(cand.min_gap0_mm <= 0.0);
        CHECK(cand.initial_penetration_max_mm > 0.0);
        // The seat limit is a length the interface supplies, and it does not depend
        // on that penetration.
        CHECK(cand.seat_limit_mm >= chudware::sim::kSeatFractionGap * copt.max_gap_mm);
        CHECK(cand.seatable_pairs >= chudware::sim::kContactMinActivePairs);
        seatable[L] = cand.seatable_pairs;
        int old_rule_seed = 0;
        for (const auto& s : cand.springs)
            if (s.gap0_mm <= 0.0) ++old_rule_seed;

        FeaContactOptions sopt;
        sopt.seat_tolerance_mm = cand.seat_limit_mm;
        FeaContactReport report;
        FeaStaticResult r =
            fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, fixed, forces, 4,
                                     {}, cand.springs, sopt, report);
        REQUIRE(r.ok);
        // A distributed patch, not a point load, on every mesh — and the load really
        // crosses the interface.
        CHECK(report.active_pairs >= chudware::sim::kContactMinActivePairs);
        CHECK(r.reaction_force_n[0] == doctest::Approx(-F).epsilon(0.02));
        CHECK(r.equilibrium_error_pct < 1.0);
        REQUIRE(report.groups.size() == 1);
        const chudware::sim::FeaContactGroup& g = report.groups[0];
        // Active pairs x h, per ring of axial nodes: the seated ARC in mm.
        arc_mm[L] = report.active_pairs * h_theta / (nz + 1);
        const double mean_p = g.contact_force_n / g.contact_area_mm2;
        std::printf("(h) nt=%d h=%.3f mm: seat_limit=%.4f penetration=%.4f "
                    "seatable=%d/%d active=%d arc=%.3f mm (area %.3f mm^2, %d pair(s) "
                    "at gap<=0 — the old rule's whole seed) disp=%.4f mm iters=%d "
                    "contact=%.1f N mean_p=%.1f MPa peak_p=%.1f MPa\n",
                    nt, h_theta, cand.seat_limit_mm, cand.initial_penetration_max_mm,
                    cand.seatable_pairs, static_cast<int>(cand.springs.size()),
                    report.active_pairs, arc_mm[L], g.contact_area_mm2, old_rule_seed,
                    r.max_disp_mm, report.contact_iterations, g.contact_force_n, mean_p,
                    g.max_contact_pressure_mpa);
    }
    const double lo = std::min({arc_mm[0], arc_mm[1], arc_mm[2]});
    const double hi = std::max({arc_mm[0], arc_mm[1], arc_mm[2]});
    REQUIRE(lo > 0.0);
    CHECK(hi / lo <= 1.25);
    // Legs 1 and 3 are the SAME mesh size with a different surface residual — the
    // run-to-run case the live keyway failed, where the same request seated 8, then
    // 12, then 13, then 2 pairs. Neither the seatable population nor the seated arc
    // may follow the noise.
    CHECK(arc_mm[3] == doctest::Approx(arc_mm[1]).epsilon(0.15));
    CHECK(seatable[3] == doctest::Approx(seatable[1]).epsilon(0.10));
    std::printf("(h) seated arc across three meshes: %.3f / %.3f / %.3f mm — spread "
                "%.1f%%; the middle size re-meshed with another residual: %.3f mm, "
                "seatable %d vs %d\n",
                arc_mm[0], arc_mm[1], arc_mm[2], 100.0 * (hi / lo - 1.0), arc_mm[3],
                seatable[3], seatable[1]);
}

// ── (i) A fixture selection must not SPAN the contact interface ────────────────
// The live defect (keyway isolate, 0.35 mm radial clearance): ONE fixture face-set
// carrying a cylinder filter at the default perp_tol_mm = 1.5 mm was pointed at the
// bore's inner face and resolved the MATING body's outer face as well, because the
// tolerance that finds a face is four times the clearance the study exists to close.
// Both bodies were grounded, the contact carried 9.9 N of a 1500 N load, and the
// run reported CONVERGED with a 0.0% equilibrium error. Nothing downstream can see
// it: the mechanism gate is satisfied (both components reach a fixture), the
// interface is meshed fine, and the fixtures react everything, so equilibrium is
// perfect. It is a defect in the SELECTION and it is refused there.
TEST_CASE("contact: a fixture filter that captures both bores is refused") {
    // Pin r 3..5, bore r 5.35..9 — the live 0.35 mm radial clearance. Node layers
    // sit at r = 3, 4, 5 (pin) and r = 5.35, 7.175, 9 (bore).
    const double clearance = 0.35, r_bore_id = 5.0 + clearance;
    const int nt = 24, nz = 3, nr = 2;
    Mesh m;
    add_tube(m, nr, nt, nz, 3.0, 5.0, 0.0, 10.0, 0.0, 0.0);            // pin  -> body 0
    const int n_pin = static_cast<int>(m.points.size() / 3);
    add_tube(m, nr, nt, nz, r_bore_id, 9.0, 0.0, 10.0, 0.0, 0.0);      // bore -> body 1
    const int n_nodes = static_cast<int>(m.points.size() / 3);

    ContactCandidateOptions copt;
    copt.max_gap_mm = 0.4;
    auto cand = fea_contact_candidates(m.points, m.tets, 4, kE, copt);
    REQUIRE(cand.error.empty());
    REQUIRE(cand.n_components == 2);
    REQUIRE_FALSE(cand.springs.empty());
    REQUIRE(cand.group_components.size() == 1);

    // The fixture: a CYLINDER filter on the bore's inner face. A node belongs when
    // its distance to the r = 5.35 cylinder is within perp_tol_mm — exactly the test
    // nodes_in_faceset_surface applies to a quadric faceset. perp_tol is the only
    // thing that varies between the two legs.
    auto cylinder_filter = [&](double perp_tol) {
        std::vector<int> ids;
        for (int i = 0; i < n_nodes; ++i) {
            const double x = m.points[static_cast<std::size_t>(i) * 3];
            const double y = m.points[static_cast<std::size_t>(i) * 3 + 1];
            if (std::abs(std::sqrt(x * x + y * y) - r_bore_id) <= perp_tol)
                ids.push_back(i);
        }
        return ids;
    };
    // The load: +x on the pin's own inner ring, which neither filter reaches.
    const double F = 1500.0;
    std::vector<int> pin_load;
    for (int i = 0; i < n_pin; ++i) {
        const double x = m.points[static_cast<std::size_t>(i) * 3];
        const double y = m.points[static_cast<std::size_t>(i) * 3 + 1];
        if (std::abs(std::sqrt(x * x + y * y) - 3.0) < 1e-9) pin_load.push_back(i);
    }
    REQUIRE_FALSE(pin_load.empty());
    std::vector<double> forces(static_cast<std::size_t>(n_nodes) * 3, 0.0);
    for (int nid : pin_load)
        forces[static_cast<std::size_t>(nid) * 3] = F / pin_load.size();

    auto carried = [](const FeaContactReport& rep) {
        double sum = 0.0;
        for (const auto& g : rep.groups)
            sum += std::sqrt(g.force_n[0] * g.force_n[0] + g.force_n[1] * g.force_n[1] +
                             g.force_n[2] * g.force_n[2]);
        return sum;
    };

    // ── The loose leg: perp_tol 1.5 mm across a 0.35 mm clearance ──────────────
    const std::vector<int> loose = cylinder_filter(1.5);
    const SelectionSpan span =
        selection_spans_contact("fixture", "F7b4f57 (fixtures[0])", loose, cand);
    REQUIRE(span.spans);
    // Both bodies are NAMED, with the count captured on each against the body's own
    // size: the whole bore inner face (r = 5.35) plus two layers of the pin
    // (r = 5 at 0.35 mm through the clearance, r = 4 at 1.35 mm).
    CHECK(span.body_a == 0);
    CHECK(span.body_b == 1);
    CHECK(span.captured_a == 2 * (nz + 1) * nt);
    CHECK(span.captured_b == (nz + 1) * nt);
    CHECK(span.body_a_nodes == (nz + 1) * nt * (nr + 1));
    CHECK(span.body_b_nodes == (nz + 1) * nt * (nr + 1));
    CHECK(span.message.find("mesh body 0") != std::string::npos);
    CHECK(span.message.find("mesh body 1") != std::string::npos);
    CHECK(span.message.find("F7b4f57 (fixtures[0])") != std::string::npos);
    CHECK(span.message.find("perp_tol_mm") != std::string::npos);

    // And this is what the refusal buys: solved, that same study CONVERGES and the
    // contact carries essentially nothing, because the fixture — not the joint —
    // reacts the load on both sides of the interface.
    FeaContactReport loose_rep;
    FeaStaticResult loose_res =
        fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, loose, forces, 4, {},
                                 cand.springs, FeaContactOptions{}, loose_rep);
    REQUIRE(loose_res.ok);
    CHECK(loose_rep.converged);
    CHECK(loose_res.equilibrium_error_pct < 1.0);
    CHECK(carried(loose_rep) < 0.05 * F);

    // ── The tightened leg: perp_tol below the clearance -> one body, and it solves ─
    const std::vector<int> tight = cylinder_filter(0.15);
    REQUIRE(tight.size() == static_cast<std::size_t>((nz + 1) * nt));
    const SelectionSpan ok_span =
        selection_spans_contact("fixture", "F7b4f57 (fixtures[0])", tight, cand);
    CHECK_FALSE(ok_span.spans);
    CHECK(ok_span.message.empty());

    FeaContactReport tight_rep;
    FeaStaticResult tight_res =
        fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, tight, forces, 4, {},
                                 cand.springs, FeaContactOptions{}, tight_rep);
    REQUIRE(tight_res.ok);
    CHECK(tight_rep.converged);
    CHECK(tight_rep.active_pairs > 0);
    CHECK(tight_rep.active_pairs < tight_rep.candidate_pairs);  // one side only
    CHECK(tight_res.reaction_force_n[0] == doctest::Approx(-F).epsilon(0.01));
    CHECK(tight_res.equilibrium_error_pct < 1.0);
    // Now the joint IS the load path.
    CHECK(carried(tight_rep) == doctest::Approx(F).epsilon(0.1));

    std::printf("(i) perp_tol 1.5 mm over a %.2f mm clearance captured body 0 "
                "(%d/%d nodes) AND body 1 (%d/%d) -> refused; solved anyway it "
                "converges with %.1f N of %.0f N through the contact. perp_tol "
                "0.15 mm captures one body and carries %.1f N.\n",
                clearance, span.captured_a, span.body_a_nodes, span.captured_b,
                span.body_b_nodes, carried(loose_rep), F, carried(tight_rep));
}

// ── (j) A pressure is only an answer when the contact carries the load ──────────
// The second half of the keyway isolate's non-answer. Case (i) refuses the
// FIXTURE SELECTION that grounds both bodies; this one is the a-posteriori guard
// that catches every other way the load can miss the interface — the packet said
// `contact_pressure_reliable: true` while 6 active pairs carried 9.9 N of a
// 1500 N applied load, which is a quotient, not a seat pressure.
//
// Same conformal seated pad as (e), solved twice: once with the pad held ONLY by
// the contact (the interface IS the load path, fraction ~ 1), once with the pad's
// own seat face fixtured too, so both bodies are grounded independently and the
// interface is a redundant parallel spring.
TEST_CASE("contact: the load path decides whether the pressure is reliable") {
    const double gap = 0.35, pad = 4.0;
    Mesh m = seated_pad(/*plate_n=*/20, /*pad_n=*/8, gap, pad);
    ContactCandidateOptions copt;
    copt.max_gap_mm = 0.5;
    auto cand = fea_contact_candidates(m.points, m.tets, 4, kE, copt);
    REQUIRE(cand.error.empty());
    REQUIRE_FALSE(cand.springs.empty());
    const chudware::sim::ContactResolution res =
        chudware::sim::fea_contact_resolution(cand);
    REQUIRE(res.resolved);

    const int n_nodes = static_cast<int>(m.points.size() / 3);
    std::vector<double> forces(static_cast<std::size_t>(n_nodes) * 3, 0.0);
    const std::vector<int> base = nodes_at_z(m, 0.0);
    const std::vector<int> seat = nodes_at_z(m, 10.0 + gap);
    const std::vector<int> top = nodes_at_z(m, 18.0 + gap);
    REQUIRE_FALSE(base.empty());
    REQUIRE_FALSE(seat.empty());
    REQUIRE_FALSE(top.empty());
    const double F = 4000.0;
    for (int nid : top) forces[static_cast<std::size_t>(nid) * 3 + 2] = -F / top.size();

    // ── The pad is held only by the seat: the contact is the load path ──────────
    FeaContactReport held_rep;
    FeaStaticResult held_r =
        fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, base, forces, 4, {},
                                 cand.springs, FeaContactOptions{}, held_rep);
    REQUIRE(held_r.ok);
    REQUIRE(held_rep.converged);
    REQUIRE(held_rep.groups.size() == 1);
    const double held_carried = held_rep.groups[0].contact_force_n;
    chudware::sim::ContactResolution held_res = res;
    chudware::sim::fea_contact_resolution_after_solve(
        held_res, held_rep.active_pairs, held_carried,
        held_rep.groups[0].contact_area_mm2);
    REQUIRE(held_res.pressure_reliable);
    chudware::sim::fea_contact_resolution_load_path(held_res, F, held_carried,
                                                    /*held_by_contact_only=*/true);
    CHECK(held_res.contact_carries_load);
    CHECK(held_res.load_fraction == doctest::Approx(1.0).epsilon(0.05));
    CHECK(held_res.applied_load_n == doctest::Approx(F));
    CHECK(held_res.carried_load_n == doctest::Approx(held_carried));
    CHECK(held_res.load_path_note.empty());
    CHECK(held_res.resolved);
    CHECK(held_res.pressure_reliable);  // a real seat pressure survives the guard

    // ── Both bodies grounded: the same seat carries almost nothing ──────────────
    // The fixture now also pins the pad's seat face, which is precisely what a
    // face-set filter that reaches across the clearance does.
    std::vector<int> both = base;
    both.insert(both.end(), seat.begin(), seat.end());
    FeaContactReport gnd_rep;
    FeaStaticResult gnd_r =
        fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield, both, forces, 4, {},
                                 cand.springs, FeaContactOptions{}, gnd_rep);
    REQUIRE(gnd_r.ok);
    REQUIRE(gnd_rep.converged);
    double gnd_carried = 0.0, gnd_area = 0.0;
    for (const chudware::sim::FeaContactGroup& g : gnd_rep.groups) {
        gnd_carried += g.contact_force_n;
        gnd_area += g.contact_area_mm2;
    }
    // Grounded on its own seat, the pad routes the load straight into the fixture:
    // whatever the interface picked up is a rounding error on the applied load.
    CHECK(gnd_carried < 0.05 * F);
    chudware::sim::ContactResolution gnd_res = res;
    chudware::sim::fea_contact_resolution_after_solve(gnd_res, gnd_rep.active_pairs,
                                                      gnd_carried, gnd_area);
    // held_by_contact_only is EMPTY here — every body reaches a fixture without
    // the interface — so the packet must say the path is redundant rather than
    // quote a pressure, and it must not refuse the study either.
    chudware::sim::fea_contact_resolution_load_path(gnd_res, 0.0, gnd_carried,
                                                    /*held_by_contact_only=*/false);
    CHECK_FALSE(gnd_res.contact_carries_load);
    CHECK_FALSE(gnd_res.pressure_reliable);
    CHECK_FALSE(gnd_res.load_path_note.empty());
    CHECK(gnd_res.load_path_note.find("REDUNDANT") != std::string::npos);
    CHECK(gnd_res.resolved);  // a redundant path is not a broken model

    // ── The live shape: held by contact, but the load went somewhere else ───────
    // 9.9 N of 1500 N. Under the bar, so this one REFUSES: a converged solve that
    // routes the load elsewhere is not an answer about this seat.
    chudware::sim::ContactResolution miss = res;
    chudware::sim::fea_contact_resolution_after_solve(
        miss, held_rep.active_pairs, 9.9, held_rep.groups[0].contact_area_mm2);
    REQUIRE(miss.pressure_reliable);
    chudware::sim::fea_contact_resolution_load_path(miss, 1500.0, 9.9,
                                                    /*held_by_contact_only=*/true);
    CHECK_FALSE(miss.contact_carries_load);
    CHECK_FALSE(miss.pressure_reliable);
    CHECK_FALSE(miss.resolved);
    CHECK(miss.load_fraction == doctest::Approx(9.9 / 1500.0).epsilon(1e-6));
    CHECK(miss.load_fraction < chudware::sim::kMinLoadFraction);
    CHECK(miss.reason == miss.load_path_note);

    std::printf("(j) seated pad: pad held by contact -> %.1f N of %.0f N crosses "
                "(fraction %.3f, %d active pairs) reliable=%d; both bodies grounded "
                "-> %.2f N (%d active) carries_load=%d reliable=%d redundant note "
                "%zu chars; 9.9 N of 1500 N -> fraction %.4f refused=%d\n",
                held_carried, F, held_res.load_fraction, held_rep.active_pairs,
                held_res.pressure_reliable ? 1 : 0, gnd_carried, gnd_rep.active_pairs,
                gnd_res.contact_carries_load ? 1 : 0,
                gnd_res.pressure_reliable ? 1 : 0, gnd_res.load_path_note.size(),
                miss.load_fraction, miss.resolved ? 0 : 1);
}

// ── (k) The clearance is the SURFACES', not the facets' ─────────────────────────
// The rover hinge case. A Ø12.000 pin sits in a Ø12.600 saddle: a 0.300 mm radial
// clearance, confirmed analytically by check_interference to 5e-8 mm. Meshed at
// ~3 mm the interface reported a closest approach of 0.03665 mm and the resolution
// guard — correctly, on that number — demanded an interface element size of
// 0.147 mm, which no fill on the machine could deliver at any background size. The
// clearance was never 0.03665 mm. A candidate pair is two boundary NODES, so what
// it measures is n̂·(p_b − p_a): the two meshes sampled the arc at different
// angular positions (13 divisions against 12 here, a real mesher's drift), and the
// projection of that offset onto the facet normal eats the clearance — most of it,
// or more, since the sign can flip and report a 0.300 mm gap as interpenetration.
//
// So the gap is measured on the analytic faces (ContactGapProbe): the signed
// distance from each node to the OTHER body's own surface, which for nested
// cylinders is r_bore − r_pin at every node however coarsely they are faceted. The
// facet value is kept beside it, and the difference is reported.
TEST_CASE("contact: a pin in a bore reports the CAD clearance, not the facet one") {
    const double r_pin = 6.000, r_bore = 6.300, clearance = r_bore - r_pin;
    // ~3 mm elements: 13 divisions of the pin's 37.70 mm circumference (2.90 mm),
    // 12 of the bore's 39.58 mm (3.30 mm), 3 mm axially, one element radially. The
    // two counts are deliberately DIFFERENT — coprime, so the relative angular
    // phase drifts around the ring and the population contains every misalignment
    // from perfectly aligned to half a facet, exactly as an unstructured mesh does.
    Mesh m;
    add_tube(m, 1, 13, 3, 3.0, r_pin, 0.0, 9.0, 0.0, 0.0);
    const int n_pin = static_cast<int>(m.points.size() / 3);
    add_tube(m, 1, 12, 3, r_bore, 9.3, 0.0, 9.0, 0.0, 0.0);

    // The analytic oracle: two concentric cylinders, so the signed distance from a
    // node to the other body's face is the surface-normal offset — the closed form
    // of what BRepExtrema/GeomAPI_ProjectPointOnSurf computes on a real face
    // (detail::make_cad_gap_probe in sim/fea_run.cpp does that on the study's own
    // STEP; this test is OCCT-free by construction).
    struct NestedCylinders : chudware::sim::ContactGapProbe {
        double r_pin = 0.0, r_bore = 0.0;
        static double radius(const double* p) {
            return std::sqrt(p[0] * p[0] + p[1] * p[1]);
        }
        bool pair_gap_mm(int, const double* pa, int, const double* pb,
                         double& gap_mm) const override {
            const double mid = 0.5 * (r_pin + r_bore);
            const bool a_on_pin = radius(pa) < mid;
            if (a_on_pin == (radius(pb) < mid)) return false;  // same body: no CAD gap
            const double* on_pin = a_on_pin ? pa : pb;
            const double* on_bore = a_on_pin ? pb : pa;
            gap_mm = std::min(r_bore - radius(on_pin), radius(on_bore) - r_pin);
            return true;
        }
    };
    NestedCylinders probe;
    probe.r_pin = r_pin;
    probe.r_bore = r_bore;

    ContactCandidateOptions facet_opt;
    facet_opt.max_gap_mm = 0.5;
    ContactCandidateOptions cad_opt = facet_opt;
    cad_opt.gap_probe = &probe;
    cad_opt.closed_fit = false;  // This control tests the clearance-resolved model.

    const auto facet = fea_contact_candidates(m.points, m.tets, 4, kE, facet_opt);
    const auto cad = fea_contact_candidates(m.points, m.tets, 4, kE, cad_opt);
    REQUIRE(facet.error.empty());
    REQUIRE(cad.error.empty());
    REQUIRE(facet.n_components == 2);
    REQUIRE_FALSE(cad.springs.empty());

    // The CAD measurement recovers the real clearance at EVERY pair, so the closest
    // approach and the median are both the fit's own 0.300 mm (the case asks for
    // 0.300 +/- 0.005 mm, i.e. 1.67 % of it).
    CHECK(cad.min_gap0_mm == doctest::Approx(clearance).epsilon(0.0167));
    CHECK(cad.median_gap0_mm == doctest::Approx(clearance).epsilon(0.0167));
    CHECK(cad.gap_analytic_pairs == static_cast<int>(cad.springs.size()));
    CHECK(cad.gap_fallback_nodes == 0);
    // The facets, on the same mesh, do not: their closest approach is a fraction of
    // the clearance (and this is the number the old guard measured).
    CHECK(cad.min_gap_facet_mm < 0.1 * clearance);
    CHECK(cad.min_gap_facet_mm == doctest::Approx(facet.min_gap0_mm).epsilon(1e-9));
    CHECK(cad.facet_error_mm ==
          doctest::Approx(cad.min_gap0_mm - cad.min_gap_facet_mm).epsilon(1e-9));
    // No probe at all: the set is measured on the facets and says so.
    CHECK(facet.gap_analytic_pairs == 0);
    CHECK(facet.min_gap_facet_mm == doctest::Approx(facet.min_gap0_mm).epsilon(1e-12));

    // ── The guard now judges the clearance that exists ──────────────────────────
    const auto cad_res = chudware::sim::fea_contact_resolution(cad);
    const auto facet_res = chudware::sim::fea_contact_resolution(facet);
    CHECK(chudware::sim::criterion_is(cad_res.criterion, chudware::sim::kCriterionClearance));
    CHECK(cad_res.gap_mm == doctest::Approx(clearance).epsilon(0.017));
    CHECK(cad_res.gap_analytic);
    CHECK(cad_res.ratio ==
          doctest::Approx(cad_res.h_interface_mm / clearance).epsilon(0.02));
    // ...and the size it prescribes is the geometric one — four elements across the
    // clearance, a millimetre-class element — not a sub-tenth-of-a-millimetre one
    // that no fill on the machine can place.
    CHECK(cad_res.ratio > chudware::sim::kContactMaxHOverGap);
    CHECK(cad_res.required_interface_size_mm ==
          doctest::Approx(chudware::sim::kContactMaxHOverGap * clearance)
              .epsilon(0.02));
    CHECK(cad_res.required_interface_size_mm > 1.0);
    // The seat limit and the penetration accounting read the same analytic gap.
    // Two bodies 0.300 mm apart have no initial penetration, and the analytic
    // measurement says so; the facets claim 0.247 mm of it, which is what used to
    // be clamped away and reported as "this interface's own surface error".
    CHECK(cad.initial_penetration_max_mm == 0.0);
    CHECK(facet.initial_penetration_max_mm > 0.2);
    CHECK(cad.seat_limit_mm ==
          doctest::Approx(0.5 * cad.interface_h_mm).epsilon(1e-9));
    // ── The pre-fix control, on the SAME mesh and the SAME geometry ─────────────
    // Measured on the facets the fit does not merely look tight — it looks
    // INTERFERING (closest approach −0.247 mm), so the guard loses the clearance
    // criterion altogether and judges a patch that does not exist. That is the
    // defect: not a conservative number, a wrong one.
    CHECK(chudware::sim::criterion_is(facet_res.criterion, chudware::sim::kCriterionPatchArea));
    CHECK(facet_res.gap_mm == 0.0);
    CHECK_FALSE(facet_res.gap_analytic);
    CHECK(facet_res.ratio == 0.0);

    std::printf("(k) pin r%.3f in bore r%.3f at ~3 mm: CAD gap %.6f mm (median "
                "%.6f), facets %.6f mm, facet_error %.6f mm; h_interface %.4f mm, "
                "ratio %.2f, required interface %.4f mm (facet-measured control: "
                "gap %.6f, criterion %s, required %.4f mm)\n",
                r_pin, r_bore, cad.min_gap0_mm, cad.median_gap0_mm,
                cad.min_gap_facet_mm, cad.facet_error_mm, cad_res.h_interface_mm,
                cad_res.ratio, cad_res.required_interface_size_mm, facet_res.gap_mm,
                facet_res.criterion, facet_res.required_interface_size_mm);
}

TEST_CASE("heterogeneous bonded and contact bars reproduce series compliance") {
    Mesh m;
    add_box(m, 2, 2, 16, 0, 0, 0, 4, 4, 32);
    const size_t steel_elements = m.tets.size() / 4;
    const int steel_nodes = static_cast<int>(m.points.size() / 3);
    add_box(m, 2, 2, 16, 0, 0, 32, 4, 4, 32);
    chudware::sim::FeaMaterials materials;
    // nu=0 gives an exact uniaxial solution with all-DOF end fixtures.
    materials.materials = {{200000, 0, 500}, {70000, 0, 200}};
    materials.elem_material.assign(m.tets.size() / 4, 1);
    std::fill(materials.elem_material.begin(),
              materials.elem_material.begin() + steel_elements, 0);
    auto fixed = nodes_at_z(m, 0);
    auto top = nodes_at_z(m, 64);
    std::vector<double> forces(m.points.size(), 0);
    // Consistent uniform pressure on the triangulated top surface: derive each
    // triangle's area load rather than giving corners the same load as interiors.
    for (int j = 0; j < 2; ++j) for (int i = 0; i < 2; ++i) {
        const int n00 = top[j * 3 + i], n10 = top[j * 3 + i + 1];
        const int n01 = top[(j + 1) * 3 + i], n11 = top[(j + 1) * 3 + i + 1];
        for (int n : {n00, n10, n11, n00, n11, n01}) forces[n * 3 + 2] -= 40.0 / 3.0;
    }
    const double force = 320.0;
    const double analytic = force / 16.0 * (32.0 / 200000.0 + 32.0 / 70000.0);
    ContactCandidateOptions options;
    options.max_gap_mm = 0.01;
    options.penalty_scale = 10000;
    options.pair_modulus = [&](int a, int b) {
        return a < steel_nodes && b < steel_nodes ? 200000.0 : 70000.0;
    };
    const auto candidates = fea_contact_candidates(m.points, m.tets, 4, 200000, options);
    REQUIRE(candidates.error.empty());
    REQUIRE_FALSE(candidates.springs.empty());
    std::vector<chudware::sim::BondSpring> bonds;
    for (const auto& s : candidates.springs) {
        CHECK(s.k == doctest::Approx(70000 * candidates.char_len_mm * options.penalty_scale));
        bonds.push_back({s.a, s.b, s.k});
    }
    for (bool contact : {false, true}) {
        FeaContactReport report;
        FeaStaticResult result = contact
            ? fea_static_solve_contact(m.points, m.tets, 200000, 0, 500, fixed,
                forces, 4, {}, candidates.springs, FeaContactOptions{}, report, {}, materials)
            : chudware::sim::fea_static_solve(m.points, m.tets, 200000, 0, 500,
                fixed, forces, 4, bonds, {}, materials);
        REQUIRE_MESSAGE(result.ok, result.error);
        if (contact) REQUIRE(report.converged);
        double displacement = 0;
        for (int n : top) displacement -= result.displacement[n * 3 + 2] / top.size();
        CHECK(displacement == doctest::Approx(analytic).epsilon(0.02));
        double steel_stress = 0, aluminium_stress = 0;
        int ns = 0, na = 0;
        for (int n : nodes_at_z(m, 32)) {
            if (n < steel_nodes) { steel_stress += result.von_mises[n]; ++ns; }
            else { aluminium_stress += result.von_mises[n]; ++na; }
        }
        steel_stress /= ns;
        aluminium_stress /= na;
        CHECK(steel_stress == doctest::Approx(aluminium_stress).epsilon(0.05));
        CHECK(steel_stress == doctest::Approx(force / 16).epsilon(0.05));
        // Aluminium governs at FoS=10; incorrectly using steel yield caps at 15.
        CHECK(result.min_fos == doctest::Approx(200.0 / (force / 16)).epsilon(0.02));
        std::printf("series %s: displacement %.9g analytic %.9g; interface VM steel %.9g aluminium %.9g\n",
                    contact ? "contact" : "bonded", displacement, analytic, steel_stress, aluminium_stress);
    }
}

TEST_CASE("contact: closed fit idealizes CAD clearance but remains unilateral") {
    Mesh m;
    constexpr double gap = 0.05, radius = 6.0, length = 2.4, force = 1500.0;
    add_tube(m, 2, 64, 4, 3.0, radius, 0, length, 0, 0);
    const int pin_nodes = static_cast<int>(m.points.size() / 3);
    add_tube(m, 2, 64, 4, radius + gap, 9.0, 0, length, 0, 0);
    struct Probe : chudware::sim::ContactGapProbe {
        bool pair_gap_mm(int, const double*, int, const double*, double& value) const override {
            value = gap;
            return true;
        }
    } probe;
    ContactCandidateOptions options;
    options.max_gap_mm = 0.2;
    options.gap_probe = &probe;
    auto candidates = fea_contact_candidates(m.points, m.tets, 4, kE, options);
    REQUIRE(candidates.error.empty());
    REQUIRE(candidates.closed_fit_pairs == static_cast<int>(candidates.springs.size()));
    CHECK(candidates.interface_h_mm / gap > 10);
    CHECK(candidates.interface_h_mm / gap < 16);
    CHECK(candidates.min_gap0_mm == doctest::Approx(gap));
    for (const auto& spring : candidates.springs) CHECK(spring.gap0_mm == 0);
    auto resolution = chudware::sim::fea_contact_resolution(candidates);
    CHECK(resolution.resolved);
    std::vector<int> fixed;
    std::vector<double> loads(m.points.size(), 0);
    for (int i = pin_nodes; i < static_cast<int>(m.points.size() / 3); ++i)
        if (std::hypot(m.points[i * 3], m.points[i * 3 + 1]) > 8.99) fixed.push_back(i);
    FeaContactOptions solver;
    solver.max_iterations = 100;
    const double full_bearing = force / (2 * radius * length);
    const double effective_radius = radius * (radius + gap) / gap;
    const double effective_modulus = kE / (2 * (1 - kNu * kNu));
    const double hertz_peak = std::sqrt(force / length * effective_modulus /
                                      (M_PI * effective_radius));
    for (double direction : {1.0, -1.0}) {
        CHECK_FALSE(chudware::sim::fea_contact_support_cone(
            candidates, 0, {direction * force, 0, 0}).excludes_load);
        for (int i = 0; i < pin_nodes; ++i) loads[i * 3] = direction * force / pin_nodes;
        FeaContactReport report;
        auto solved = fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield,
            fixed, loads, 4, {}, candidates.springs, solver, report);
        REQUIRE(solved.ok);
        CHECK(solved.reaction_force_n[0] == doctest::Approx(-direction * force).epsilon(0.01));
        CHECK(report.active_pairs >= chudware::sim::kContactMinActivePairs);
        CHECK(report.active_pairs < report.candidate_pairs);
        REQUIRE(report.groups.size() == 1);
        CHECK(report.groups[0].force_n[0] * direction > 0);
        int wrong_side = 0;
        for (const auto& spring : candidates.springs) {
            double relative = 0;
            for (int axis = 0; axis < 3; ++axis)
                relative += spring.normal[axis] *
                    (solved.displacement[spring.b * 3 + axis] -
                     solved.displacement[spring.a * 3 + axis]);
            if (relative < -1e-10 && spring.normal[0] * direction < -0.1)
                ++wrong_side;
        }
        CHECK(wrong_side == 0);  // The formerly loaded side lifts off on reversal.
        CHECK(report.groups[0].max_contact_pressure_mpa >= full_bearing);
        CHECK(report.groups[0].max_contact_pressure_mpa <= hertz_peak);
        chudware::sim::fea_contact_resolution_after_solve(resolution, report.active_pairs,
            report.groups[0].contact_force_n, report.groups[0].contact_area_mm2);
        CHECK(resolution.resolved);
        std::printf("closed fit direction %.0f h/gap %.3f: %d/%d active, mean %.6g MPa, peak %.6g MPa"
                    " (full bearing %.6g, Hertz peak %.6g MPa)\n",
            direction, candidates.interface_h_mm / gap, report.active_pairs, report.candidate_pairs,
            report.groups[0].contact_force_n / report.groups[0].contact_area_mm2,
            report.groups[0].max_contact_pressure_mpa, full_bearing, hertz_peak);
    }
    options.closed_fit = false;
    auto resolved = fea_contact_candidates(m.points, m.tets, 4, kE, options);
    CHECK(resolved.closed_fit_pairs == 0);
    for (const auto& spring : resolved.springs) CHECK(spring.gap0_mm == doctest::Approx(gap));
    // A sparse solved patch is still refused with an actionable size.
    auto refusal = chudware::sim::fea_contact_resolution(resolved);
    chudware::sim::fea_contact_resolution_after_solve(refusal, 2, force, 0.1);
    CHECK_FALSE(refusal.resolved);
    CHECK(refusal.required_interface_size_mm == doctest::Approx(4 * gap));
}

TEST_CASE("contact: coarse closed bore settles a marginal off-axis bearing patch") {
    // Nonmatching 32/31-sector surfaces, a cantilevered pin, and a 0.0038-degree
    // off-axis load. The zero-threshold loop alternated two sets at iteration 6
    // (53/128 active pairs), despite balancing the load: the patch rim repeatedly
    // released and re-seated. Keep the geometry and load, not an iteration cap.
    Mesh m;
    add_tube(m, 2, 32, 4, 3.0, 6.0, 0.0, 15.0, 0.0, 0.0);
    const int pin_nodes = static_cast<int>(m.points.size() / 3);
    add_tube(m, 2, 31, 4, 6.05, 9.0, -5.0, 15.0, 0.0, 0.0);
    struct Probe : chudware::sim::ContactGapProbe {
        bool pair_gap_mm(int, const double*, int, const double*, double& gap) const override {
            gap = 0.05;
            return true;
        }
    } probe;
    ContactCandidateOptions options;
    options.max_gap_mm = 0.3;
    options.penalty_scale = 0.5;
    options.gap_probe = &probe;
    const auto candidates = fea_contact_candidates(m.points, m.tets, 4, kE, options);
    REQUIRE(candidates.error.empty());
    REQUIRE(candidates.closed_fit_pairs == static_cast<int>(candidates.springs.size()));
    std::vector<int> fixed;
    std::vector<double> loads(m.points.size(), 0.0);
    for (int i = pin_nodes; i < static_cast<int>(m.points.size() / 3); ++i)
        if (m.points[i * 3 + 2] < -4.99) fixed.push_back(i);
    for (int i = 0; i < pin_nodes; ++i)
        if (m.points[i * 3 + 2] > 14.99) {
            loads[i * 3] = 1500.0 / 96;
            loads[i * 3 + 1] = 0.1 / 96;
        }
    FeaContactOptions solver;
    solver.max_iterations = 100;
    solver.interface_h_mm = candidates.interface_h_mm;
    FeaContactReport report;
    const auto result = fea_static_solve_contact(m.points, m.tets, kE, kNu, kYield,
        fixed, loads, 4, {}, candidates.springs, solver, report);
    REQUIRE_MESSAGE(result.ok, result.error);
    REQUIRE(report.converged);
    CHECK(result.equilibrium_error_pct < 0.01);
    CHECK(result.reaction_force_n[0] == doctest::Approx(-1500.0).epsilon(0.001));
    CHECK(result.reaction_force_n[1] == doctest::Approx(-0.1).epsilon(0.001));
    REQUIRE(report.groups.size() == 1);
    CHECK(report.groups[0].force_n[0] == doctest::Approx(1500.0).epsilon(0.01));
    CHECK(report.tangential_force_fraction < 0.01);
    CHECK(report.active_pairs >= chudware::sim::kContactMinActivePairs);
    CHECK(report.active_pairs < report.candidate_pairs);
    std::printf("coarse off-axis bore: iterations=%d cycles_broken=%d active=%d/%d "
                "tol_g=%.9g mm tol_f=%.9g N Rx=%.9g N\n",
        report.contact_iterations, report.cycles_broken, report.active_pairs,
        report.candidate_pairs, report.hysteresis_tol_g_mm, report.hysteresis_tol_f_n,
        result.reaction_force_n[0]);
}

TEST_CASE("contact: an open saddle cannot carry a load outside its normal cone") {
    chudware::sim::ContactCandidateSet candidates;
    candidates.component = {0, 1};
    candidates.n_components = 2;
    candidates.group_components = {{0, 1}};
    for (int i = 0; i <= 30; ++i) {
        const double angle = (-43.5 + 87.0 * i / 30) * M_PI / 180;
        chudware::sim::ContactSpring spring;
        spring.a = 0;
        spring.b = 1;
        spring.group = 0;
        spring.normal = {std::cos(angle), 0, std::sin(angle)};
        candidates.springs.push_back(spring);
    }
    const auto transverse = chudware::sim::fea_contact_support_cone(
        candidates, 0, {0, 0, -1446});
    CHECK(transverse.excludes_load);
    CHECK(transverse.separating_axis == 0);
    CHECK(transverse.normal_max[0] < -0.7);
    CHECK(transverse.supported_arc_deg == doctest::Approx(87));
    // The saddle can oppose a load into its seat, but never one pulling away.
    CHECK_FALSE(chudware::sim::fea_contact_support_cone(
        candidates, 0, {1446, 0, 0}).excludes_load);
    CHECK(chudware::sim::fea_contact_support_cone(
        candidates, 0, {-1446, 0, 0}).excludes_load);
    // An opposing seat restores the missing reaction; this sufficient refusal
    // must not declare that two-sided bearing geometrically impossible.
    candidates.springs.front().normal = {-1, 0, 0};
    CHECK_FALSE(chudware::sim::fea_contact_support_cone(
        candidates, 0, {0, 0, -1446}).excludes_load);
}

TEST_CASE("ICCG: heterogeneous bonded bars preserve direct displacements stresses and reactions") {
    Mesh m;
    add_box(m, 2, 2, 4, 0, 0, 0, 4, 4, 8);
    const size_t first_elements = m.tets.size() / 4;
    const int first_nodes = static_cast<int>(m.points.size() / 3);
    const auto lower_interface = nodes_at_z(m, 8);
    add_box(m, 2, 2, 4, 0, 0, 8, 4, 4, 8);
    chudware::sim::FeaMaterials materials;
    materials.materials = {{200000, 0, 500}, {70000, 0, 200}};
    materials.elem_material.assign(m.tets.size() / 4, 1);
    std::fill(materials.elem_material.begin(),
              materials.elem_material.begin() + first_elements, 0);
    std::vector<chudware::sim::BondSpring> bonds;
    for (size_t i = 0; i < lower_interface.size(); ++i)
        bonds.push_back({lower_interface[i], first_nodes + static_cast<int>(i), 1e6});
    const auto fixed = nodes_at_z(m, 0), top = nodes_at_z(m, 16);
    std::vector<double> loads(m.points.size(), 0.0);
    // Consistent triangular traction, with eccentric x loading to test moments.
    for (int j = 0; j < 2; ++j) for (int i = 0; i < 2; ++i) {
        const int a = top[j * 3 + i], b = top[j * 3 + i + 1];
        const int c = top[(j + 1) * 3 + i], d = top[(j + 1) * 3 + i + 1];
        for (int n : {a, b, d, a, d, c}) {
            loads[n * 3 + 2] -= 40.0 / 3.0;
            loads[n * 3] += 1.0;
        }
    }
    SolverEnvironment backend("CHUDWARE_FEA_SOLVER", "ldlt");
    const auto direct = chudware::sim::fea_static_solve(
        m.points, m.tets, kE, 0, 500, fixed, loads, 4, bonds, {}, materials);
    chudware::test::setEnvironment("CHUDWARE_FEA_SOLVER", "iccg");
    const auto iterative = chudware::sim::fea_static_solve(
        m.points, m.tets, kE, 0, 500, fixed, loads, 4, bonds, {}, materials);
    compare_linear_fields(iterative, direct);
    // Reassign the soft body: compliance must change, not merely copy table fields.
    materials.materials[1].E_mpa *= 2;
    const auto stiffer = chudware::sim::fea_static_solve(
        m.points, m.tets, kE, 0, 500, fixed, loads, 4, bonds, {}, materials);
    REQUIRE_MESSAGE(stiffer.ok, stiffer.error);
    CHECK(stiffer.max_disp_mm < iterative.max_disp_mm);
}

TEST_CASE("ICCG: changing unilateral bore active sets match direct contact and moments") {
    Mesh m;
    add_tube(m, 2, 16, 2, 3, 5, 0, 10, 0, 0);
    add_tube(m, 2, 16, 2, 5.1, 9, 0, 10, 0, 0);
    ContactCandidateOptions options;
    options.max_gap_mm = 0.4;
    const auto candidates = fea_contact_candidates(m.points, m.tets, 4, kE, options);
    REQUIRE(candidates.error.empty());
    std::vector<int> fixed, pin;
    for (int i = 0; i < static_cast<int>(m.points.size() / 3); ++i) {
        const double radius = std::hypot(m.points[i * 3], m.points[i * 3 + 1]);
        if (std::abs(radius - 9) < 1e-6) fixed.push_back(i);
        if (radius < 5.05) pin.push_back(i);
    }
    std::vector<double> loads(m.points.size(), 0.0);
    for (int n : pin) loads[n * 3] = 1500.0 / pin.size();
    SolverEnvironment backend("CHUDWARE_FEA_SOLVER", "ldlt");
    FeaContactReport direct_report, iterative_report;
    const auto direct = fea_static_solve_contact(
        m.points, m.tets, kE, kNu, kYield, fixed, loads, 4, {},
        candidates.springs, FeaContactOptions{}, direct_report);
    chudware::test::setEnvironment("CHUDWARE_FEA_SOLVER", "iccg");
    const auto iterative = fea_static_solve_contact(
        m.points, m.tets, kE, kNu, kYield, fixed, loads, 4, {},
        candidates.springs, FeaContactOptions{}, iterative_report);
    compare_linear_fields(iterative, direct);
    REQUIRE(iterative_report.converged);
    CHECK(iterative_report.active_pairs == direct_report.active_pairs);
    CHECK(iterative_report.active_pairs < iterative_report.candidate_pairs);
    CHECK(iterative_report.contact_iterations > 1);
    CHECK(iterative_report.tangential_force_fraction ==
          doctest::Approx(direct_report.tangential_force_fraction).epsilon(1e-5).scale(1e-8));
    REQUIRE(iterative_report.groups.size() == direct_report.groups.size());
    for (size_t i = 0; i < direct_report.groups.size(); ++i)
        CHECK(iterative_report.groups[i].contact_force_n ==
              doctest::Approx(direct_report.groups[i].contact_force_n).epsilon(2e-6));
}

TEST_CASE("ICCG: iteration exhaustion and cancellation never return a direct answer") {
    Mesh m;
    add_box(m, 3, 3, 8, 0, 0, 0, 4, 4, 20);
    const auto fixed = nodes_at_z(m, 0), top = nodes_at_z(m, 20);
    std::vector<double> loads(m.points.size(), 0.0);
    for (int n : top) loads[n * 3] = 100.0 / top.size();
    SolverEnvironment backend("CHUDWARE_FEA_SOLVER", "iccg");
    SolverEnvironment limit("CHUDWARE_FEA_ICCG_MAX_ITERATIONS", "1");
    const auto failed = chudware::sim::fea_static_solve(
        m.points, m.tets, kE, kNu, kYield, fixed, loads);
    CHECK_FALSE(failed.ok);
    CHECK(failed.displacement.empty());
    REQUIRE(failed.iterative.solves.size() == 1);
    CHECK(failed.iterative.solves[0].iterations == 1);
    CHECK(failed.iterative.solves[0].relative_residual > 1e-10);
    const auto token = std::make_shared<std::atomic<bool>>(true);
    const auto cancelled = chudware::sim::fea_static_solve(
        m.points, m.tets, kE, kNu, kYield, fixed, loads, 4, {}, token);
    CHECK_FALSE(cancelled.ok);
    CHECK(cancelled.error.find("cancelled") != std::string::npos);
    CHECK(cancelled.displacement.empty());
}

TEST_CASE("ICCG: conservative governor refusal survives explicit iterative selection") {
    SolverEnvironment backend("CHUDWARE_FEA_SOLVER", "iccg");
    SolverEnvironment governor("CHUDWARE_GOVERNOR", "1");
    MemoryProbeScope memory(+[]() -> chudware::governor::HostMem {
        return {1024 * 1024, 1024 * 1024};
    });
    Mesh m;
    add_box(m, 1, 1, 1, 0, 0, 0, 1, 1, 1);
    std::vector<double> loads(m.points.size(), 0.0);
    for (int n : nodes_at_z(m, 1)) loads[n * 3 + 2] = -1;
    const auto result = chudware::sim::fea_static_solve(
        m.points, m.tets, kE, kNu, kYield, nodes_at_z(m, 0), loads);
    CHECK_FALSE(result.ok);
    CHECK(result.displacement.empty());
    CHECK(result.iterative.solves.empty());
    CHECK(result.iterative.projected_peak_bytes > chudware::governor::budget_bytes());
    // Selecting ICCG does not change the calibrated direct-mode policy.
    std::string error;
    CHECK_FALSE(chudware::governor::admit_fea(100000, 10, 3, "direct reference", error));
}

TEST_CASE("ICCG: memory lost after adjacency discovery refuses before matrix allocation") {
    SolverEnvironment backend("CHUDWARE_FEA_SOLVER", "iccg");
    SolverEnvironment governor("CHUDWARE_GOVERNOR", "1");
    static unsigned queries;
    queries = 0;
    MemoryProbeScope memory(+[]() -> chudware::governor::HostMem {
        if (++queries == 1) return {1ull << 40, 1ull << 40};
        return {1024 * 1024, 1024 * 1024};
    });
    Mesh m;
    add_box(m, 1, 1, 1, 0, 0, 0, 1, 1, 1);
    std::vector<double> loads(m.points.size(), 0.0);
    for (int n : nodes_at_z(m, 1)) loads[n * 3 + 2] = -1;
    const auto result = chudware::sim::fea_static_solve(
        m.points, m.tets, kE, kNu, kYield, nodes_at_z(m, 0), loads);
    CHECK_FALSE(result.ok);
    CHECK(result.displacement.empty());
    CHECK(result.iterative.solves.empty());
    CHECK(result.iterative.free_dofs == 12);
    CHECK(result.iterative.stored_nonzeros > 0);
    CHECK(result.iterative.projected_peak_bytes > chudware::governor::budget_bytes());
}

TEST_CASE("ICCG: cancellation arriving after admission discards the in-flight solve") {
    SolverEnvironment backend("CHUDWARE_FEA_SOLVER", "iccg");
    SolverEnvironment governor("CHUDWARE_GOVERNOR", "1");
    static chudware::sim::SimCancelToken token;
    static unsigned queries;
    token = std::make_shared<std::atomic<bool>>(false);
    queries = 0;
    MemoryProbeScope memory(+[]() -> chudware::governor::HostMem {
        // A deterministic external cancellation while the solver is running,
        // rather than a timing-sensitive sleeping thread or a pre-cancelled job.
        if (++queries >= 2) token->store(true, std::memory_order_relaxed);
        return {1ull << 40, 1ull << 40};
    });
    Mesh m;
    add_box(m, 2, 2, 3, 0, 0, 0, 4, 4, 6);
    std::vector<double> loads(m.points.size(), 0.0);
    for (int n : nodes_at_z(m, 6)) loads[n * 3 + 2] = -10;
    const auto result = chudware::sim::fea_static_solve(
        m.points, m.tets, kE, kNu, kYield, nodes_at_z(m, 0), loads, 4, {}, token);
    CHECK_FALSE(result.ok);
    CHECK(result.error.find("cancelled") != std::string::npos);
    CHECK(result.displacement.empty());
    token.reset();
}
