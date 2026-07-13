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
// MSVC only exposes M_PI from <cmath> when _USE_MATH_DEFINES is set (before the include).
#define _USE_MATH_DEFINES
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

// ═══════════════════════════ Transient cases (T-FIN18) ══════════════════════════════════

// ── Case T1: 1D slab quench vs the analytic Fourier series (tet4, lumped C). ────────────
// A slab 0..L at uniform T0 has both faces suddenly held at 0. Analytic (Incropera §5.5):
//   T(x,t) = Σ_{odd n} (4 T0 / nπ) sin(nπx/L) exp(-α n²π² t / L²),  α = k/(ρ cp).
// Checked at the mid-plane at a mid-decay time (Fo≈0.3): both spatial discretisation and
// backward-Euler O(dt) error present, so an engineering tolerance (1.5%) applies.
TEST_CASE("thermal transient: 1D slab quench matches the Fourier series (tet4, lumped)") {
    const double L = 20.0, k = 0.05, rho_cp = 0.004, T0 = 100.0;  // mm, W/(mm·K), J/(mm^3·K)
    const double alpha = k / rho_cp;                              // 12.5 mm^2/s
    const double t_star = 0.3 * L * L / alpha;                    // Fo = 0.3  ->  9.6 s
    Box m = box_mesh(L, 8.0, 8.0, 20, 2, 2);
    std::vector<int> fixed = m.x0;
    std::vector<double> temps = uniform(m.x0.size(), 0.0);
    for (int nid : m.xL) { fixed.push_back(nid); temps.push_back(0.0); }

    aicad::sim::ThermalTransientOptions opts;
    opts.rho_cp = rho_cp;
    opts.dt = t_star / 400.0;
    opts.t_end = t_star;
    opts.t_init = T0;
    auto r = aicad::sim::fea_thermal_transient(m.pts, m.tets, k, fixed, temps, {}, {}, opts, 4);
    REQUIRE(r.ok);
    REQUIRE(!r.frames.empty());
    CHECK(r.frames.back().time == doctest::Approx(t_star));

    auto exact = [&](double x, double t) {
        double s = 0.0;
        for (int n = 1; n <= 199; n += 2)
            s += 4.0 * T0 / (n * M_PI) * std::sin(n * M_PI * x / L) *
                 std::exp(-alpha * n * n * M_PI * M_PI * t / (L * L));
        return s;
    };
    // Mid-plane FEM temperature: average the nodes nearest x = L/2.
    double sumT = 0.0;
    int cnt = 0;
    for (size_t i = 0; i < m.pts.size() / 3; ++i)
        if (std::abs(m.pts[i * 3] - L / 2.0) < 1e-9) { sumT += r.frames.back().temperature[i]; ++cnt; }
    REQUIRE(cnt > 0);
    const double T_mid_fem = sumT / cnt;
    const double T_mid_exact = exact(L / 2.0, t_star);
    MESSAGE("slab quench: T_mid_exact=", T_mid_exact, " T_mid_fem=", T_mid_fem);
    CHECK(T_mid_fem == doctest::Approx(T_mid_exact).epsilon(0.02));
    // Envelope: the max over the run is the initial condition (monotone decay).
    CHECK(r.max_temp == doctest::Approx(T0).epsilon(1e-9));
    CHECK(r.max_temp_time == doctest::Approx(0.0));
}

// ── Case T2: consistent capacitance reproduces the same quench (tet4). ──────────────────
TEST_CASE("thermal transient: consistent capacitance agrees with lumped on the quench") {
    const double L = 20.0, k = 0.05, rho_cp = 0.004, T0 = 100.0;
    const double alpha = k / rho_cp, t_star = 0.3 * L * L / alpha;
    Box m = box_mesh(L, 8.0, 8.0, 20, 2, 2);
    std::vector<int> fixed = m.x0;
    std::vector<double> temps = uniform(m.x0.size(), 0.0);
    for (int nid : m.xL) { fixed.push_back(nid); temps.push_back(0.0); }
    aicad::sim::ThermalTransientOptions opts;
    opts.rho_cp = rho_cp;
    opts.dt = t_star / 400.0;
    opts.t_end = t_star;
    opts.t_init = T0;
    opts.lumped = false;
    auto r = aicad::sim::fea_thermal_transient(m.pts, m.tets, k, fixed, temps, {}, {}, opts, 4);
    REQUIRE(r.ok);
    double sumT = 0.0;
    int cnt = 0;
    for (size_t i = 0; i < m.pts.size() / 3; ++i)
        if (std::abs(m.pts[i * 3] - L / 2.0) < 1e-9) { sumT += r.frames.back().temperature[i]; ++cnt; }
    double T_exact = 0.0;
    for (int n = 1; n <= 199; n += 2)
        T_exact += 4.0 * T0 / (n * M_PI) * std::sin(n * M_PI * 0.5) *
                   std::exp(-alpha * n * n * M_PI * M_PI * t_star / (L * L));
    CHECK(sumT / cnt == doctest::Approx(T_exact).epsilon(0.02));
}

// ── Case T3: long-time transient converges to the steady solution (tet10). ──────────────
// Hot Dirichlet face + convection cold face (Case 3's setup) run to ~10 diffusion time
// constants must land on the steady solver's answer — ties the transient path to the
// regression-gated steady kernel, exercising the tet10 HRZ capacitance.
TEST_CASE("thermal transient: long-time limit matches the steady solve (tet10)") {
    const double L = 20.0, k = 0.05, T_hot = 150.0, T_amb = 25.0, h = 0.002;
    const double rho_cp = 0.004;
    Box b4 = box_mesh(L, 10.0, 10.0, 8, 2, 2);
    Box10 m = to_tet10(b4);
    std::vector<int> fixed = m.x0;
    std::vector<double> temps = uniform(m.x0.size(), T_hot);
    aicad::sim::ThermalConvection cv;
    cv.nodes = m.xL;
    cv.h = h;
    cv.t_amb = T_amb;

    auto steady = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, fixed, temps, {}, 0.0, {cv}, 10);
    REQUIRE(steady.ok);

    const double tau = rho_cp * L * L / k;  // diffusion time scale
    aicad::sim::ThermalTransientOptions opts;
    opts.rho_cp = rho_cp;
    opts.t_end = 10.0 * tau;
    opts.dt = opts.t_end / 500.0;
    opts.t_init = T_amb;
    auto r = aicad::sim::fea_thermal_transient(m.pts, m.tets, k, fixed, temps, {}, {cv}, opts, 10);
    REQUIRE(r.ok);
    double max_dev = 0.0;
    for (size_t i = 0; i < r.frames.back().temperature.size(); ++i)
        max_dev = std::max(max_dev, std::abs(r.frames.back().temperature[i] - steady.temperature[i]));
    MESSAGE("long-time vs steady max deviation = ", max_dev);
    CHECK(max_dev < 0.001 * (T_hot - T_amb));  // within 0.1% of the temperature span
}

// ── Case T4: duty-cycled flux — envelope peaks above the average-power steady state. ────
// A 50% duty flux on the hot face with a fixed cold face. The cycle-averaged response must
// straddle the steady solve with q_avg = duty·q, and the envelope max must exceed the
// steady-with-average answer (the whole reason transient duty-cycle analysis exists).
TEST_CASE("thermal transient: duty-cycle flux peaks above the mean-power steady state") {
    const double L = 30.0, k = 0.02, T_cold = 40.0, q_in = 0.01, duty = 0.5;
    const double rho_cp = 0.004;
    Box m = box_mesh(L, 8.0, 8.0, 12, 2, 2);
    std::vector<int> fixed = m.xL;
    std::vector<double> temps = uniform(m.xL.size(), T_cold);

    // Steady reference at the AVERAGE power.
    auto steady_avg = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, fixed, temps, m.x0,
                                                    duty * q_in, {}, 4);
    REQUIRE(steady_avg.ok);

    const double tau = rho_cp * L * L / k;      // 180 s diffusion scale
    const double period = tau / 5.0;            // fast-ish cycling relative to diffusion
    aicad::sim::ThermalFluxLoad fl;
    fl.nodes = m.x0;
    fl.q = q_in;
    fl.schedule.times = {0.0, duty * period};
    fl.schedule.scales = {1.0, 0.0};
    fl.schedule.period = period;

    aicad::sim::ThermalTransientOptions opts;
    opts.rho_cp = rho_cp;
    opts.t_end = 12.0 * tau;                    // enough cycles to reach the periodic regime
    opts.dt = period / 40.0;
    opts.t_init = T_cold;
    auto r = aicad::sim::fea_thermal_transient(m.pts, m.tets, k, fixed, temps, {fl}, {}, opts, 4);
    REQUIRE(r.ok);

    MESSAGE("duty-cycle: steady_avg max=", steady_avg.max_temp, " envelope max=", r.max_temp);
    // The periodic peak exceeds the mean-power steady max, but stays below the 100%-on
    // steady max (q_in continuous) — the physically required bracket.
    auto steady_full = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, fixed, temps, m.x0,
                                                     q_in, {}, 4);
    REQUIRE(steady_full.ok);
    CHECK(r.max_temp > steady_avg.max_temp);
    CHECK(r.max_temp < steady_full.max_temp);
    // Envelope field is well-formed: per-node max >= final frame everywhere.
    for (size_t i = 0; i < r.envelope_max.size(); ++i)
        CHECK(r.envelope_max[i] >= r.frames.back().temperature[i] - 1e-12);
}

// ── Case T5: schedule evaluation semantics (sample-and-hold + periodicity). ─────────────
TEST_CASE("thermal transient: schedule scale is sample-and-hold and periodic") {
    aicad::sim::ThermalSchedule s;
    CHECK(aicad::sim::thermal_schedule_scale(s, 3.0) == 1.0);  // empty = always on
    s.times = {0.0, 2.0, 5.0};
    s.scales = {1.0, 0.25, 0.0};
    CHECK(aicad::sim::thermal_schedule_scale(s, 0.0) == 1.0);
    CHECK(aicad::sim::thermal_schedule_scale(s, 1.9) == 1.0);
    CHECK(aicad::sim::thermal_schedule_scale(s, 2.0) == 0.25);
    CHECK(aicad::sim::thermal_schedule_scale(s, 4.9) == 0.25);
    CHECK(aicad::sim::thermal_schedule_scale(s, 7.0) == 0.0);   // last holds
    s.period = 10.0;
    CHECK(aicad::sim::thermal_schedule_scale(s, 11.0) == 1.0);  // wraps
    CHECK(aicad::sim::thermal_schedule_scale(s, 12.5) == 0.25);
}

// ── Case T6: transient validation + frame selection. ────────────────────────────────────
TEST_CASE("thermal transient: invalid inputs rejected; frames land at requested times") {
    Box m = box_mesh(20.0, 10.0, 10.0, 4, 1, 1);
    std::vector<int> fixed = m.x0;
    std::vector<double> temps = uniform(m.x0.size(), 0.0);
    aicad::sim::ThermalTransientOptions opts;
    opts.rho_cp = 0.0;  // invalid
    opts.dt = 0.1;
    opts.t_end = 1.0;
    auto r1 = aicad::sim::fea_thermal_transient(m.pts, m.tets, 0.05, fixed, temps, {}, {}, opts, 4);
    CHECK_FALSE(r1.ok);
    opts.rho_cp = 0.004;
    opts.dt = -1.0;  // invalid
    auto r2 = aicad::sim::fea_thermal_transient(m.pts, m.tets, 0.05, fixed, temps, {}, {}, opts, 4);
    CHECK_FALSE(r2.ok);
    opts.dt = 0.1;
    opts.t_init = 50.0;
    opts.frame_times = {0.0, 0.35, 0.75};
    auto r3 = aicad::sim::fea_thermal_transient(m.pts, m.tets, 0.05, fixed, temps, {}, {}, opts, 4);
    REQUIRE(r3.ok);
    REQUIRE(r3.frames.size() == 4);  // t=0, snap(0.35)->0.4, snap(0.75)->0.8, final 1.0
    CHECK(r3.frames[0].time == doctest::Approx(0.0));
    CHECK(r3.frames[1].time == doctest::Approx(0.4));
    CHECK(r3.frames[2].time == doctest::Approx(0.8));
    CHECK(r3.frames[3].time == doctest::Approx(1.0));
    // A pure-Neumann transient (no datum) is well-posed: uniform heating, no error.
    aicad::sim::ThermalFluxLoad fl;
    fl.nodes = m.x0;
    fl.q = 0.001;
    auto r4 = aicad::sim::fea_thermal_transient(m.pts, m.tets, 0.05, {}, {}, {fl}, {}, opts, 4);
    CHECK(r4.ok);
    CHECK(r4.max_temp > 50.0);  // adiabatic body warms above the initial field
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

// ── Case 8: MULTIPLE independent flux loads (regression for the C3 defect). ─────────────
// The fea_run steady path used to collapse every flux load onto ONE node set with the LAST
// load's magnitude — two loads with different q were silently mis-applied. The per-load
// overload superposes each load's own q·∫N dS.
TEST_CASE("thermal: two flux loads superpose (same face, different magnitudes)") {
    // 1-D wall: T fixed at x=0, fluxes q1 + q2 both into x=L as TWO separate loads.
    // Exact linear field: T(L) = T0 + (q1+q2)·L/k. The old collapse gave q2 only.
    const double L = 50.0, T0 = 20.0, k = 0.05, q1 = 0.002, q2 = 0.0007;
    Box m = box_mesh(L, 10.0, 10.0, 8, 2, 2);
    aicad::sim::ThermalFluxLoad f1, f2;
    f1.nodes = m.xL; f1.q = q1;
    f2.nodes = m.xL; f2.q = q2;
    auto r = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, m.x0, uniform(m.x0.size(), T0),
                                           {f1, f2}, {}, 4);
    REQUIRE(r.ok);
    const double exactL = T0 + (q1 + q2) * L / k;
    double got = 0.0;
    for (int nid : m.xL) got = std::max(got, r.temperature[nid]);
    MESSAGE("double-flux wall T(L) = ", got, " exact ", exactL);
    CHECK(got == doctest::Approx(exactL).epsilon(1e-6));  // linear field -> solver tolerance
}

TEST_CASE("thermal: flux loads on different faces satisfy node-wise superposition") {
    // Linearity: with the SAME Dirichlet datum T0, (q1 on xL) + (q2 on y=0) solved together
    // must equal the sum of the single-load solutions minus the datum, at EVERY node.
    const double L = 30.0, T0 = 25.0, k = 0.08, q1 = 0.0015, q2 = -0.0004;
    Box m = box_mesh(L, 12.0, 9.0, 5, 3, 2);
    std::vector<int> y0;  // nodes on the y≈0 lateral face
    for (size_t n = 0; n < m.pts.size() / 3; ++n)
        if (std::abs(m.pts[n * 3 + 1]) < 1e-9) y0.push_back(static_cast<int>(n));
    REQUIRE(!y0.empty());
    aicad::sim::ThermalFluxLoad f1, f2;
    f1.nodes = m.xL; f1.q = q1;
    f2.nodes = y0;   f2.q = q2;
    const std::vector<double> temps = uniform(m.x0.size(), T0);
    auto rA  = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, m.x0, temps, {f1}, {}, 4);
    auto rB  = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, m.x0, temps, {f2}, {}, 4);
    auto rAB = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, m.x0, temps, {f1, f2}, {}, 4);
    REQUIRE(rA.ok); REQUIRE(rB.ok); REQUIRE(rAB.ok);
    double max_err = 0.0;
    for (size_t i = 0; i < rAB.temperature.size(); ++i) {
        const double expect = rA.temperature[i] + rB.temperature[i] - T0;
        max_err = std::max(max_err, std::abs(rAB.temperature[i] - expect));
    }
    MESSAGE("multi-face superposition max node error = ", max_err);
    CHECK(max_err < 1e-8);
}

// ── Case 9: the legacy single-flux signature matches the per-load overload bit-for-bit. ──
TEST_CASE("thermal: legacy single-flux entry point delegates identically") {
    const double L = 40.0, T0 = 10.0, k = 0.05, q = 0.001;
    Box m = box_mesh(L, 10.0, 10.0, 6, 2, 2);
    auto legacy = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, m.x0,
                                                uniform(m.x0.size(), T0), m.xL, q, {}, 4);
    aicad::sim::ThermalFluxLoad fl;
    fl.nodes = m.xL; fl.q = q;
    auto perload = aicad::sim::fea_thermal_solve(m.pts, m.tets, k, m.x0,
                                                 uniform(m.x0.size(), T0), {fl}, {}, 4);
    REQUIRE(legacy.ok); REQUIRE(perload.ok);
    REQUIRE(legacy.temperature.size() == perload.temperature.size());
    for (size_t i = 0; i < legacy.temperature.size(); ++i)
        CHECK(legacy.temperature[i] == perload.temperature[i]);  // bit-for-bit
}
