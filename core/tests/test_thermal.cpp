// Steady-state thermal (heat-conduction) FEA suite — validates the fea_thermal kernel
// against closed-form 1D solutions on both tet4 and tet10 elements.
//
// The conduction operator K_ij = ∫ k ∇N_i·∇N_j dV is exact for a linear temperature field
// on a straight tet mesh, so the 1D plane-wall Dirichlet solution T(x) = T1 + (T2-T1) x/L is
// reproduced to solver tolerance (not just mesh-convergence). A convection (Robin) case then
// exercises the surface terms h∫N N dS (matrix) and h·T_amb·∫N dS (load) against the series-
// resistance wall solution. Units are internally consistent (mesh length unit L, k in
// W/(L·K), h in W/(L^2·K), flux in W/L^2); the temperatures are unit-agnostic.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <array>
#include <cmath>
#include <map>
#include <utility>
#include <vector>

#include "sim/fea_thermal.h"

namespace {

// A box [0,Lx] x [0,Ly] x [0,Lz] meshed nx x ny x nz cells, each cube split into 6 Kuhn tets
// (same y<->z-symmetric decomposition test_modal uses). Records node ids on the x≈0 and x≈Lx
// faces so the wall can be driven with Dirichlet/flux/convection BCs on opposite faces.
struct Box {
    std::vector<double> pts;   // 3*N
    std::vector<int> tets;     // 4*M
    std::vector<int> x0;       // nodes at x≈0
    std::vector<int> xL;       // nodes at x≈Lx
    double Lx = 0.0;
};

Box box_mesh(double Lx, double Ly, double Lz, int nx, int ny, int nz) {
    Box m;
    m.Lx = Lx;
    auto nid = [&](int i, int j, int k) { return (i * (ny + 1) + j) * (nz + 1) + k; };
    for (int i = 0; i <= nx; ++i)
        for (int j = 0; j <= ny; ++j)
            for (int k = 0; k <= nz; ++k) {
                m.pts.push_back(Lx * i / nx);
                m.pts.push_back(Ly * j / ny);
                m.pts.push_back(Lz * k / nz);
                if (i == 0) m.x0.push_back(nid(i, j, k));
                if (i == nx) m.xL.push_back(nid(i, j, k));
            }
    const int paths[6][4][3] = {
        {{0,0,0},{1,0,0},{1,1,0},{1,1,1}}, {{0,0,0},{1,0,0},{1,0,1},{1,1,1}},
        {{0,0,0},{0,1,0},{1,1,0},{1,1,1}}, {{0,0,0},{0,1,0},{0,1,1},{1,1,1}},
        {{0,0,0},{0,0,1},{1,0,1},{1,1,1}}, {{0,0,0},{0,0,1},{0,1,1},{1,1,1}},
    };
    for (int ci = 0; ci < nx; ++ci)
        for (int cj = 0; cj < ny; ++cj)
            for (int ck = 0; ck < nz; ++ck)
                for (int t = 0; t < 6; ++t)
                    for (int v = 0; v < 4; ++v) {
                        const int* d = paths[t][v];
                        m.tets.push_back(nid(ci + d[0], cj + d[1], ck + d[2]));
                    }
    return m;
}

// tet10 promotion (mid-edge node per unique edge, mesher edge order), reused from the modal
// test. Rebuilds the x≈0 / x≈Lx node sets over the enlarged node list so the mid-side nodes
// on those faces are driven too (otherwise a tet10 face BC would miss its edge midpoints).
struct Box10 {
    std::vector<double> pts;
    std::vector<int> tets;   // 10*M
    std::vector<int> x0;
    std::vector<int> xL;
};

Box10 to_tet10(const Box& m4) {
    Box10 m;
    m.pts = m4.pts;
    std::map<std::pair<int, int>, int> mid;
    auto midnode = [&](int a, int b) {
        auto key = std::minmax(a, b);
        auto it = mid.find({key.first, key.second});
        if (it != mid.end()) return it->second;
        int id = static_cast<int>(m.pts.size() / 3);
        for (int c = 0; c < 3; ++c) m.pts.push_back(0.5 * (m.pts[a * 3 + c] + m.pts[b * 3 + c]));
        mid.emplace(std::make_pair(key.first, key.second), id);
        return id;
    };
    const int LE[6][2] = {{0, 1}, {1, 2}, {2, 0}, {3, 0}, {3, 2}, {3, 1}};
    const int nt = static_cast<int>(m4.tets.size() / 4);
    for (int e = 0; e < nt; ++e) {
        int c[4];
        for (int j = 0; j < 4; ++j) c[j] = m4.tets[e * 4 + j];
        for (int j = 0; j < 4; ++j) m.tets.push_back(c[j]);
        for (int ed = 0; ed < 6; ++ed) m.tets.push_back(midnode(c[LE[ed][0]], c[LE[ed][1]]));
    }
    const int nn = static_cast<int>(m.pts.size() / 3);
    for (int n = 0; n < nn; ++n) {
        if (std::abs(m.pts[n * 3]) < 1e-9) m.x0.push_back(n);
        if (std::abs(m.pts[n * 3] - m4.Lx) < 1e-9) m.xL.push_back(n);
    }
    return m;
}

// Uniform temperature vector for a Dirichlet node set.
std::vector<double> uniform(size_t n, double v) { return std::vector<double>(n, v); }

}  // namespace

// ── Case 1: 1D plane wall, Dirichlet on both faces (tet4). ──────────────────────────────
// A slab of thickness L held at T1 on x=0 and T2 on x=L, no generation, insulated sides. The
// exact steady field is LINEAR: T(x) = T1 + (T2-T1) x/L. The conduction element is exact for a
// linear field, so every interior node must match to solver tolerance (1e-9 relative here).
TEST_CASE("thermal: 1D plane-wall Dirichlet reproduces the linear profile (tet4)") {
    const double L = 50.0, T1 = 100.0, T2 = 20.0, k = 0.05;  // k in W/(mm·K)
    Box m = box_mesh(L, 10.0, 10.0, 8, 2, 2);
    std::vector<int> fixed = m.x0;
    std::vector<double> temps = uniform(m.x0.size(), T1);
    for (int nid : m.xL) { fixed.push_back(nid); temps.push_back(T2); }

    auto r = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, fixed, temps, /*flux_nodes*/ {},
                                           /*flux_q*/ 0.0, /*convections*/ {}, /*npe*/ 4);
    REQUIRE(r.ok);
    REQUIRE(r.temperature.size() == m.pts.size() / 3);

    double max_abs_err = 0.0;
    for (size_t i = 0; i < r.temperature.size(); ++i) {
        const double x = m.pts[i * 3];
        const double exact = T1 + (T2 - T1) * x / L;
        max_abs_err = std::max(max_abs_err, std::abs(r.temperature[i] - exact));
    }
    MESSAGE("plane-wall tet4 max |T_fem - T_exact| = ", max_abs_err);
    CHECK(max_abs_err < 1e-6);            // linear field is reproduced to solver tolerance
    CHECK(r.max_temp == doctest::Approx(T1).epsilon(1e-9));
    CHECK(r.min_temp == doctest::Approx(T2).epsilon(1e-9));
}

// ── Case 2: same plane wall on tet10 (quadratic). ───────────────────────────────────────
// The quadratic element also reproduces a linear field exactly; this locks the tet10 volume-
// gradient path + the mid-side node handling on the Dirichlet faces.
TEST_CASE("thermal: 1D plane-wall Dirichlet reproduces the linear profile (tet10)") {
    const double L = 40.0, T1 = 0.0, T2 = 200.0, k = 0.4;
    Box b4 = box_mesh(L, 12.0, 12.0, 6, 2, 2);
    Box10 m = to_tet10(b4);
    std::vector<int> fixed = m.x0;
    std::vector<double> temps = uniform(m.x0.size(), T1);
    for (int nid : m.xL) { fixed.push_back(nid); temps.push_back(T2); }

    auto r = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, fixed, temps, {}, 0.0, {}, /*npe*/ 10);
    REQUIRE(r.ok);
    double max_abs_err = 0.0;
    for (size_t i = 0; i < r.temperature.size(); ++i) {
        const double x = m.pts[i * 3];
        const double exact = T1 + (T2 - T1) * x / L;
        max_abs_err = std::max(max_abs_err, std::abs(r.temperature[i] - exact));
    }
    MESSAGE("plane-wall tet10 max |T_fem - T_exact| = ", max_abs_err);
    CHECK(max_abs_err < 1e-5);
}

// ── Case 3: wall with a fixed hot face + a convection (Robin) cold face. ─────────────────
// x=0 held at T_hot; x=L loses heat to ambient T_amb through a film h. 1D series-resistance
// gives the steady heat flux  q = (T_hot - T_amb) / (L/k + 1/h)  and the cold-face temperature
//   T_L = T_amb + q/h  =  T_amb + (T_hot - T_amb) * (1/h) / (L/k + 1/h).
// The field is still linear inside (no generation), so T_L is the analytic anchor. Tolerance
// 0.5% (surface-term discretisation on a coarse mesh). This is the term that a static-only
// port would miss entirely, so it is the load-bearing convection check.
TEST_CASE("thermal: hot face + convection cold face matches series-resistance (tet4)") {
    const double L = 20.0, k = 0.05;        // W/(mm·K)
    const double T_hot = 150.0, T_amb = 25.0;
    const double h = 0.002;                  // W/(mm^2·K)
    Box m = box_mesh(L, 10.0, 10.0, 10, 2, 2);

    std::vector<int> fixed = m.x0;
    std::vector<double> temps = uniform(m.x0.size(), T_hot);

    aicad::sim::ThermalConvection cv;
    cv.nodes = m.xL;
    cv.h = h;
    cv.t_amb = T_amb;

    auto r = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, fixed, temps, {}, 0.0, {cv}, /*npe*/ 4);
    REQUIRE(r.ok);

    const double R_cond = L / k;             // conduction resistance (per unit area)
    const double R_conv = 1.0 / h;           // convection resistance (per unit area)
    const double q = (T_hot - T_amb) / (R_cond + R_conv);
    const double T_L_exact = T_amb + q / h;

    // Read the FEM cold-face temperature (average over the x=L nodes; all equal in 1D).
    double sumT = 0.0;
    for (int nid : m.xL) sumT += r.temperature[nid];
    const double T_L_fem = sumT / static_cast<double>(m.xL.size());
    MESSAGE("Robin wall: T_L_exact=", T_L_exact, " T_L_fem=", T_L_fem, " q=", q);
    CHECK(T_L_fem == doctest::Approx(T_L_exact).epsilon(5e-3));   // within 0.5%
    // The cold face sits between ambient and hot, and below the hot face — sanity band.
    CHECK(T_L_fem > T_amb);
    CHECK(T_L_fem < T_hot);
}

// ── Case 4: convection on tet10 (tri6 surface consistent mass path). ────────────────────
// Same Robin wall on quadratic elements exercises the tri6 surface mass matrix + the
// corner-0 / mid-side-A/3 surface load weights. Confirms the tet10 convection assembly.
TEST_CASE("thermal: hot face + convection cold face matches series-resistance (tet10)") {
    const double L = 20.0, k = 0.05, T_hot = 150.0, T_amb = 25.0, h = 0.002;
    Box b4 = box_mesh(L, 10.0, 10.0, 8, 2, 2);
    Box10 m = to_tet10(b4);
    std::vector<int> fixed = m.x0;
    std::vector<double> temps = uniform(m.x0.size(), T_hot);
    aicad::sim::ThermalConvection cv;
    cv.nodes = m.xL;
    cv.h = h;
    cv.t_amb = T_amb;
    auto r = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, fixed, temps, {}, 0.0, {cv}, /*npe*/ 10);
    REQUIRE(r.ok);
    const double q = (T_hot - T_amb) / (L / k + 1.0 / h);
    const double T_L_exact = T_amb + q / h;
    double sumT = 0.0;
    for (int nid : m.xL) sumT += r.temperature[nid];
    const double T_L_fem = sumT / static_cast<double>(m.xL.size());
    MESSAGE("Robin wall tet10: T_L_exact=", T_L_exact, " T_L_fem=", T_L_fem);
    CHECK(T_L_fem == doctest::Approx(T_L_exact).epsilon(5e-3));
}

// ── Case 5: heat-flux (Neumann) load + a fixed cold face. ───────────────────────────────
// A uniform flux q_in enters the x=0 face; x=L is held at T_cold. In steady state ALL of q_in
// must pass through to the cold face, so the hot-face temperature rises by ΔT = q_in·L/k above
// T_cold:  T_0 = T_cold + q_in·L/k. This validates the surface flux load ∫N_i q dS (resultant
// = q_in·A) and that the internal gradient carries it.
TEST_CASE("thermal: applied flux + fixed cold face gives the conduction ΔT (tet4)") {
    const double L = 30.0, k = 0.02, T_cold = 40.0;
    const double q_in = 0.01;  // W/mm^2 into the x=0 face
    Box m = box_mesh(L, 8.0, 8.0, 12, 2, 2);
    std::vector<int> fixed = m.xL;
    std::vector<double> temps = uniform(m.xL.size(), T_cold);

    auto r = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, fixed, temps, /*flux_nodes*/ m.x0,
                                           q_in, /*convections*/ {}, /*npe*/ 4);
    REQUIRE(r.ok);
    const double dT_exact = q_in * L / k;
    const double T0_exact = T_cold + dT_exact;
    double sumT = 0.0;
    for (int nid : m.x0) sumT += r.temperature[nid];
    const double T0_fem = sumT / static_cast<double>(m.x0.size());
    MESSAGE("flux wall: T0_exact=", T0_exact, " T0_fem=", T0_fem);
    CHECK(T0_fem == doctest::Approx(T0_exact).epsilon(1e-6));  // linear field: exact
}

// ── Case 6: singular guard — no Dirichlet and no convection is rejected, not solved. ────
TEST_CASE("thermal: pure-Neumann (no fixed-T, no convection) is reported singular") {
    Box m = box_mesh(20.0, 10.0, 10.0, 4, 1, 1);
    // Only a flux load — no temperature datum anywhere.
    auto r = aicad::sim::fea_thermal_solve(m.pts, m.tets, 0.05, /*fixed*/ {}, /*temps*/ {},
                                           /*flux_nodes*/ m.x0, /*flux_q*/ 0.01,
                                           /*convections*/ {}, /*npe*/ 4);
    CHECK_FALSE(r.ok);
    CHECK(!r.error.empty());
}

// ── Case 7: input validation — bad conductivity and length mismatches are rejected. ─────
TEST_CASE("thermal: invalid inputs are rejected with ok=false") {
    Box m = box_mesh(20.0, 10.0, 10.0, 2, 1, 1);
    // k <= 0
    auto r1 = aicad::sim::fea_thermal_solve(m.pts, m.tets, 0.0, m.x0, uniform(m.x0.size(), 10.0),
                                            {}, 0.0, {}, 4);
    CHECK_FALSE(r1.ok);
    // fixed_nodes / fixed_temps length mismatch
    auto r2 = aicad::sim::fea_thermal_solve(m.pts, m.tets, 0.05, m.x0, uniform(m.x0.size() + 1, 10.0),
                                            {}, 0.0, {}, 4);
    CHECK_FALSE(r2.ok);
}
