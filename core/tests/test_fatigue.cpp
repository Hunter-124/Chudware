// Unit tests for the high-cycle fatigue study (chudware::sim::fea_run_fatigue).
//
// OCCT/oracle-free: an inline-meshed unit cube (6 tets) fixed at z=0 and pulled in
// +z gives a finite stress field; we assert the fatigue solver runs and returns the
// documented summary shape — Sut/Sy/Se echoes, lmin/lmax, crit_sigma_a/m, and a
// theories{} block with goodman/gerber/soderberg/asme/morrow each carrying sa_eq_mpa.
// The host (not the engine) finishes FoS / life / reliability from these scalars.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <limits>
#include <map>
#include <thread>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "sim/fea_fatigue.h"
#include "sim/fea_run.h"
#include "sim/sim_cache.h"
#include "sim/sim_cancel.h"

using nlohmann::json;
using chudware::sim::fea_run_fatigue;

using chudware::sim::fea_run_static;
namespace {


struct Tet10Box {
    std::vector<double> points;
    std::vector<int> tets;
    int n_corner = 0;
};

Tet10Box make_tet10_box(int nx, int ny, int nz,
                        double Lx, double Ly, double Lz) {
    Tet10Box mesh;
    const int NX = nx + 1, NY = ny + 1, NZ = nz + 1;
    auto node_id = [&](int i, int j, int k) {
        return (k * NY + j) * NX + i;
    };
    mesh.n_corner = NX * NY * NZ;
    mesh.points.resize(static_cast<size_t>(mesh.n_corner) * 3);
    for (int k = 0; k < NZ; ++k)
        for (int j = 0; j < NY; ++j)
            for (int i = 0; i < NX; ++i) {
                const int id = node_id(i, j, k);
                mesh.points[id * 3 + 0] = Lx * i / nx;
                mesh.points[id * 3 + 1] = Ly * j / ny;
                mesh.points[id * 3 + 2] = Lz * k / nz;
            }

    static const int kCellTets[6][4] = {
        {0, 1, 2, 6}, {0, 2, 3, 6}, {0, 3, 7, 6},
        {0, 7, 4, 6}, {0, 4, 5, 6}, {0, 5, 1, 6}};
    std::vector<int> tet4;
    for (int k = 0; k < nz; ++k)
        for (int j = 0; j < ny; ++j)
            for (int i = 0; i < nx; ++i) {
                const int corners[8] = {
                    node_id(i, j, k), node_id(i + 1, j, k),
                    node_id(i + 1, j + 1, k), node_id(i, j + 1, k),
                    node_id(i, j, k + 1), node_id(i + 1, j, k + 1),
                    node_id(i + 1, j + 1, k + 1),
                    node_id(i, j + 1, k + 1)};
                for (const auto& tet : kCellTets)
                    for (int local : tet) tet4.push_back(corners[local]);
            }

    std::map<std::pair<int, int>, int> edge_mid;
    auto mid_node = [&](int a, int b) {
        const auto edge = std::minmax(a, b);
        const auto found = edge_mid.find(edge);
        if (found != edge_mid.end()) return found->second;
        const int id = static_cast<int>(mesh.points.size() / 3);
        for (int c = 0; c < 3; ++c)
            mesh.points.push_back(
                0.5 * (mesh.points[a * 3 + c] + mesh.points[b * 3 + c]));
        edge_mid.emplace(edge, id);
        return id;
    };
    static const int kEdges[6][2] = {
        {0, 1}, {1, 2}, {2, 0}, {3, 0}, {3, 2}, {3, 1}};
    for (size_t e = 0; e < tet4.size() / 4; ++e) {
        const int* tet = &tet4[e * 4];
        for (int local = 0; local < 4; ++local)
            mesh.tets.push_back(tet[local]);
        for (const auto& edge : kEdges)
            mesh.tets.push_back(mid_node(tet[edge[0]], tet[edge[1]]));
    }
    return mesh;
}
// 1x1x1 cube as 8 corner nodes + 6 tets (standard decomposition), fixed face z=0,
// a +z force on the top face. Inline mesh => no STEP path => no overlay packaging.
json cube_study(const json& fatigue) {
    json study;
    // Inline mesh: fea_run_static's resolve_mesh expects FLAT points (n*3) and FLAT
    // tets (m*4), not nested per-node/per-elem arrays.
    study["points"] = {0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
                       0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1};
    // Freudenthal 6-tet subdivision about the 0-6 body diagonal (gap/overlap-free).
    study["tets"] = {0, 1, 2, 6, 0, 2, 3, 6, 0, 3, 7, 6,
                     0, 7, 4, 6, 0, 4, 5, 6, 0, 5, 1, 6};
    study["material"] = {
        {"E_mpa", 200000.0}, {"nu", 0.29}, {"yield_mpa", 370.0},
        {"ultimate_mpa", 440.0}, {"endurance_mpa", 220.0}};
    // Fix the four bottom (z=0) nodes; pull the four top (z=1) nodes in +z.
    study["fixtures"] = {{{"points", {{0, 0, 0}, {1, 0, 0}, {1, 1, 0}, {0, 1, 0}}},
                          {"tol_mm", 0.01}}};
    study["loads"] = {{{"type", "force"},
                       {"vector", {0.0, 0.0, 50000.0}},
                       {"faces", {{"points", {{0, 0, 1}, {1, 0, 1}, {1, 1, 1}, {0, 1, 1}}},
                                  {"tol_mm", 0.01}}}}};
    if (!fatigue.is_null()) study["fatigue"] = fatigue;
    return study;
}

const char* kTheories[] = {"goodman", "gerber", "soderberg", "asme", "morrow"};

void check_summary_shape(const json& res) {
    REQUIRE(res.value("ok", false));
    REQUIRE(res.contains("summary"));
    const json& s = res["summary"];
    for (const char* k : {"se_corrected_mpa", "se_base_mpa", "sut_mpa", "sy_mpa",
                          "lmin", "lmax", "crit_sigma_a", "crit_sigma_m", "theories"}) {
        CHECK_MESSAGE(s.contains(k), "missing summary key: ", k);
    }
    REQUIRE(s.contains("theories"));
    const json& t = s["theories"];
    for (const char* name : kTheories) {
        CHECK_MESSAGE(t.contains(name), "missing theory: ", name);
        REQUIRE(t.contains(name));
        CHECK_MESSAGE(t[name].contains("sa_eq_mpa"), "theory missing sa_eq_mpa: ", name);
        CHECK_MESSAGE(t[name].contains("status"),
                      "theory missing capability status: ", name);
    }
    CHECK(res.contains("n_nodes"));
    CHECK(res.contains("n_elems"));
    CHECK(res.contains("n_dof"));
    CHECK(res.contains("solver_notes"));
}

// The largest static von Mises in the field = crit_sigma_a for a fully-reversed cycle.
double crit_sa(const json& res) {
    return res["summary"]["crit_sigma_a"].get<double>();
}

// Regression for the production tet10 load glue. The load faceset intentionally
// samples only corner nodes; fea_run_static must complete the pick with mid-side
// nodes before depositing tri6 work-equivalent weights.
TEST_CASE("fea_run_static applies tet10 end load through work-equivalent mid-side nodes (Z1)") {
    const double Lx = 100.0, Ly = 10.0, Lz = 10.0;
    const double E = 200000.0, nu = 0.3;
    const double force = 10000.0;
    const double sigma_nom = force / (Ly * Lz);
    const double disp_nom = force * Lx / (Ly * Lz * E);
    const double tol = 0.5;

    struct AxialResult {
        double mid_vm;
        double loaded_end_peak_vm;
        double max_disp;
    };
    auto solve = [&](int nx, int ny, int nz) -> AxialResult {
        const Tet10Box mesh = make_tet10_box(nx, ny, nz, Lx, Ly, Lz);
        const int n_nodes = static_cast<int>(mesh.points.size() / 3);
        json fixture_points = json::array();
        json load_corner_points = json::array();
        for (int nid = 0; nid < n_nodes; ++nid) {
            const double x = mesh.points[nid * 3 + 0];
            const json point = {mesh.points[nid * 3 + 0],
                                mesh.points[nid * 3 + 1],
                                mesh.points[nid * 3 + 2]};
            if (std::abs(x) < 1e-9) fixture_points.push_back(point);
            if (nid < mesh.n_corner && std::abs(x - Lx) < 1e-9)
                load_corner_points.push_back(point);
        }
        REQUIRE_FALSE(fixture_points.empty());
        REQUIRE_FALSE(load_corner_points.empty());

        json study;
        study["points"] = mesh.points;
        study["tets"] = mesh.tets;
        study["nodes_per_elem"] = 10;
        study["material"] =
            {{"E_mpa", E}, {"nu", nu}, {"yield_mpa", 250.0}};
        study["fixtures"] =
            {{{"points", fixture_points}, {"tol_mm", tol}}};
        study["loads"] =
            {{{"type", "force"},
              {"vector", {force, 0.0, 0.0}},
              {"faces",
               {{"points", load_corner_points}, {"tol_mm", tol}}}}};

        const json result = fea_run_static(study);
        REQUIRE_MESSAGE(result.value("ok", false), result.dump());
        const std::vector<double> vm =
            result.at("von_mises").get<std::vector<double>>();
        REQUIRE(vm.size() == static_cast<size_t>(n_nodes));

        bool used_production_consistent_path = false;
        for (const auto& note : result.at("solver_notes"))
            if (note.is_string() &&
                note.get<std::string>().find(
                    "Force spread work-consistently") != std::string::npos)
                used_production_consistent_path = true;
        CHECK(used_production_consistent_path);

        double mid_sum = 0.0;
        int mid_count = 0;
        double loaded_end_peak = 0.0;
        for (int nid = 0; nid < n_nodes; ++nid) {
            const double x = mesh.points[nid * 3 + 0];
            if (std::abs(x - Lx / 2.0) < Lx / 8.0) {
                mid_sum += vm[static_cast<size_t>(nid)];
                ++mid_count;
            }
            if (x > Lx * 7.0 / 8.0)
                loaded_end_peak =
                    std::max(loaded_end_peak, vm[static_cast<size_t>(nid)]);
        }
        REQUIRE(mid_count > 0);
        return {mid_sum / mid_count, loaded_end_peak,
                result.at("summary").at("max_disp_mm").get<double>()};
    };

    const AxialResult coarse = solve(10, 2, 2);
    const AxialResult fine = solve(20, 4, 4);
    CHECK(coarse.mid_vm == doctest::Approx(sigma_nom).epsilon(0.12));
    CHECK(fine.mid_vm == doctest::Approx(sigma_nom).epsilon(0.12));
    CHECK(coarse.loaded_end_peak_vm < 2.0 * sigma_nom);
    CHECK(fine.loaded_end_peak_vm < 2.0 * sigma_nom);
    CHECK(std::abs(fine.loaded_end_peak_vm -
                   coarse.loaded_end_peak_vm) /
              sigma_nom <
          0.50);
    CHECK(coarse.max_disp == doctest::Approx(disp_nom).epsilon(0.15));
    CHECK(fine.max_disp == doctest::Approx(disp_nom).epsilon(0.15));
}

// Regression for the pressure SIGN convention (V&V finding, 2026-09-05). `pressure` is
// documented "MPa, inward": a POSITIVE magnitude pushes INTO the loaded face, i.e. along
// -n_outward. The solver used to deposit +p*integral(N_i n_outward)dA, so +p pulled the
// loaded surface OUTWARD: a bore under +p was externally pressurised and the Lame hoop
// stress came back at -29.6 MPa against the closed form's +33.33. Asserted on a flat
// face, where n_outward is known: +p on the +Z face of a cube must impose a resultant of
// p*A in -Z and push the loaded nodes DOWN. (The nodal DISTRIBUTION is deliberately not
// compared against an equivalent force load: pressure deposits tributary weights
// integral(N_i)dA while a tet4 force load splits equally over the picked nodes, so the
// two agree on the resultant, not node for node.)
TEST_CASE("pressure load acts along the INWARD face normal (sign convention)") {
    const double p = 10.0;    // MPa on the +Z face of the unit cube
    const double area = 1.0;  // mm2 -> p*area = 10 N total, inward = -Z
    json study;
    study["points"] = {0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
                       0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1};
    study["tets"] = {0, 1, 2, 6, 0, 2, 3, 6, 0, 3, 7, 6,
                     0, 7, 4, 6, 0, 4, 5, 6, 0, 5, 1, 6};
    study["material"] = {{"E_mpa", 200000.0}, {"nu", 0.29}, {"yield_mpa", 370.0}};
    study["fixtures"] = {{{"points", {{0, 0, 0}, {1, 0, 0}, {1, 1, 0}, {0, 1, 0}}},
                          {"tol_mm", 0.01}}};
    study["loads"] = {{{"type", "pressure"},
                       {"magnitude", p},
                       {"faces", {{"points", {{0, 0, 1}, {1, 0, 1}, {1, 1, 1}, {0, 1, 1}}},
                                  {"tol_mm", 0.01}}}}};

    const json pressed = fea_run_static(study);
    REQUIRE_MESSAGE(pressed.value("ok", false), pressed.dump());

    // (1) The applied resultant is p*A along the INWARD normal of the loaded face.
    const auto applied =
        pressed.at("reactions").at("applied_force_n").get<std::vector<double>>();
    REQUIRE(applied.size() == 3);
    CHECK(applied[0] == doctest::Approx(0.0).epsilon(1e-9).scale(p * area));
    CHECK(applied[1] == doctest::Approx(0.0).epsilon(1e-9).scale(p * area));
    CHECK(applied[2] == doctest::Approx(-p * area).epsilon(1e-6));

    // (2) +p COMPRESSES the part: every loaded (z=1) node moves in -Z.
    const auto u = pressed.at("displacement").get<std::vector<double>>();
    REQUIRE(u.size() == 24);
    for (int nid : {4, 5, 6, 7})
        CHECK(u[static_cast<size_t>(nid) * 3 + 2] < -1e-12);
}

TEST_CASE("public FEA run wrappers return JSON failures for malformed mesh setup") {
    json malformed_mesh = {
        {"points", {0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
                    0.0, 1.0, 0.0, 0.0, 0.0, "not-a-number"}},
        {"tets", {0, 1, 2, 3}}};

    json static_study = malformed_mesh;
    static_study["material"] =
        {{"E_mpa", 200000.0}, {"nu", 0.3}, {"yield_mpa", 250.0}};
    static_study["fixtures"] =
        {{{"points", {{0.0, 0.0, 0.0}}}, {"tol_mm", 0.1}}};
    static_study["loads"] =
        {{{"type", "force"},
          {"vector", {1.0, 0.0, 0.0}},
          {"faces",
           {{"points", {{1.0, 0.0, 0.0}}}, {"tol_mm", 0.1}}}}};
    const json static_result = fea_run_static(static_study);
    CHECK_FALSE(static_result.value("ok", true));
    CHECK(static_result.value("error", std::string()).find(
              "invalid study JSON") != std::string::npos);

    json modal_study = malformed_mesh;
    modal_study["material"] = {{"E_mpa", 200000.0},
                                {"nu", 0.3},
                                {"density_g_cm3", 7.85}};
    const json modal_result = chudware::sim::fea_run_modal(modal_study, 1);
    CHECK_FALSE(modal_result.value("ok", true));
    CHECK(modal_result.value("error", std::string()).find(
              "invalid study JSON") != std::string::npos);

    json thermal_study = malformed_mesh;
    thermal_study["material"] = {{"k_w_mk", 45.0}};
    const json thermal_result =
        chudware::sim::fea_run_thermal(thermal_study);
    CHECK_FALSE(thermal_result.value("ok", true));
    CHECK(thermal_result.value("error", std::string()).find(
              "invalid study JSON") != std::string::npos);

    json wrong_mesh_option = static_study;
    wrong_mesh_option.erase("points");
    wrong_mesh_option.erase("tets");
    wrong_mesh_option["step_path"] = "/does/not/need/to/exist.step";
    wrong_mesh_option["fineness"] = "fine";
    const json option_result = fea_run_static(wrong_mesh_option);
    CHECK_FALSE(option_result.value("ok", true));
    CHECK(option_result.value("error", std::string()).find(
              "invalid study JSON") != std::string::npos);
}

TEST_CASE("fully reversed: sa_eq == sa for every theory (sm == 0)") {
    // lmin=-1, lmax=1: sa = sigma, sm = 0 => no mean-stress penalty anywhere.
    json res = fea_run_fatigue(cube_study(json{{"se_corrected_mpa", 200.0},
                                               {"ultimate_mpa", 440.0},
                                               {"yield_mpa", 370.0},
                                               {"lmin", -1.0},
                                               {"lmax", 1.0}}));
    check_summary_shape(res);
    CHECK(res["summary"]["lmin"].get<double>() == doctest::Approx(-1.0));
    CHECK(res["summary"]["lmax"].get<double>() == doctest::Approx(1.0));
    CHECK(res["summary"]["crit_sigma_m"].get<double>() == doctest::Approx(0.0));
    CHECK(res["summary"]["se_corrected_mpa"].get<double>() == doctest::Approx(200.0));
    CHECK(res["summary"]["sut_mpa"].get<double>() == doctest::Approx(440.0));
    CHECK(res["summary"]["sy_mpa"].get<double>() == doctest::Approx(370.0));

    const double sa = crit_sa(res);
    CHECK(sa > 0.0);
    // With sm == 0, every theory's sa_eq collapses to sa.
    const json& t = res["summary"]["theories"];
    for (const char* name : kTheories) {
        CHECK_MESSAGE(t[name]["sa_eq_mpa"].get<double>() == doctest::Approx(sa).epsilon(1e-4),
                      "sa_eq != sa for theory: ", name);
    }
}

TEST_CASE("zero-based: positive mean -> mean-stress penalty; gerber < goodman") {
    // lmin=0, lmax=1: sa = sigma/2, sm = sigma/2 (positive mean everywhere). The cube
    // load drives a very high peak stress (crit sa~5e4 MPa), so use strengths large
    // enough that sm_eff/Sut stays well below 1 and the mean-stress denominators are
    // valid (otherwise every theory saturates at the 1e9 "fails" sentinel and the
    // gerber<goodman ordering is lost).
    json res = fea_run_fatigue(cube_study(json{{"se_corrected_mpa", 40000.0},
                                               {"ultimate_mpa", 100000.0},
                                               {"yield_mpa", 80000.0},
                                               {"lmin", 0.0},
                                               {"lmax", 1.0}}));
    check_summary_shape(res);
    CHECK(res["summary"]["crit_sigma_m"].get<double>() > 0.0);
    CHECK(res["summary"]["crit_sigma_a"].get<double>() > 0.0);
    // crit_sigma_a == crit_sigma_m for a zero-based cycle.
    CHECK(res["summary"]["crit_sigma_a"].get<double>() ==
          doctest::Approx(res["summary"]["crit_sigma_m"].get<double>()));

    const json& t = res["summary"]["theories"];
    const double sa = res["summary"]["crit_sigma_a"].get<double>();
    const double goodman = t["goodman"]["sa_eq_mpa"].get<double>();
    const double gerber = t["gerber"]["sa_eq_mpa"].get<double>();
    // Positive mean stress raises the equivalent reversed stress above the raw sa.
    for (const char* name : kTheories) {
        CHECK_MESSAGE(t[name]["sa_eq_mpa"].get<double>() >= sa,
                      "sa_eq < sa under positive mean for theory: ", name);
    }
    // Mean-stress penalty: goodman strictly above sa.
    CHECK(goodman > sa);
    // Gerber is less conservative than Goodman (parabola sits above the line).
    CHECK(gerber < goodman);
}

TEST_CASE("legacy load_type fallback: zero_based derives lmin=0,lmax=1") {
    json res = fea_run_fatigue(cube_study(json{{"se_corrected_mpa", 200.0},
                                               {"ultimate_mpa", 440.0},
                                               {"yield_mpa", 370.0},
                                               {"load_type", "zero_based"}}));
    check_summary_shape(res);
    CHECK(res["summary"]["lmin"].get<double>() == doctest::Approx(0.0));
    CHECK(res["summary"]["lmax"].get<double>() == doctest::Approx(1.0));
    CHECK(res["summary"]["crit_sigma_m"].get<double>() > 0.0);
}

TEST_CASE("legacy load_type fallback: reversed (default) -> lmin=-1,lmax=1") {
    json res = fea_run_fatigue(cube_study(json{{"se_corrected_mpa", 200.0},
                                               {"ultimate_mpa", 440.0}}));
    check_summary_shape(res);
    CHECK(res["summary"]["lmin"].get<double>() == doctest::Approx(-1.0));
    CHECK(res["summary"]["lmax"].get<double>() == doctest::Approx(1.0));
    CHECK(res["summary"]["crit_sigma_m"].get<double>() == doctest::Approx(0.0));
}

TEST_CASE("legacy load_type fallback: ratio uses load_ratio for lmin") {
    json res = fea_run_fatigue(cube_study(json{{"se_corrected_mpa", 200.0},
                                               {"ultimate_mpa", 440.0},
                                               {"load_type", "ratio"},
                                               {"load_ratio", 0.1}}));
    check_summary_shape(res);
    CHECK(res["summary"]["lmin"].get<double>() == doctest::Approx(0.1));
    CHECK(res["summary"]["lmax"].get<double>() == doctest::Approx(1.0));
}

TEST_CASE("Se_corrected falls back to material endurance, then 0.5*Sut") {
    // No se_corrected_mpa in fatigue -> pull from material.endurance_mpa (220).
    json res = fea_run_fatigue(cube_study(json{{"ultimate_mpa", 440.0}}));
    check_summary_shape(res);
    CHECK(res["summary"]["se_corrected_mpa"].get<double>() == doctest::Approx(220.0));

    // Strip material endurance too -> 0.5*Sut = 220.
    json study = cube_study(json{{"ultimate_mpa", 440.0}});
    study["material"].erase("endurance_mpa");
    json res2 = fea_run_fatigue(study);
    check_summary_shape(res2);
    CHECK(res2["summary"]["se_corrected_mpa"].get<double>() == doctest::Approx(220.0));
}

TEST_CASE("Sy<=0 -> soderberg/asme fall back to Sut (no crash)") {
    // Strengths sized above the cube's high peak stress so the Sut fallback actually
    // exercises a valid (non-saturated) denominator rather than the 1e9 sentinel.
    json study = cube_study(json{{"se_corrected_mpa", 40000.0},
                                 {"ultimate_mpa", 100000.0},
                                 {"yield_mpa", 0.0},
                                 {"lmin", 0.0},
                                 {"lmax", 1.0}});
    study["material"].erase("yield_mpa");
    json res = fea_run_fatigue(study);
    check_summary_shape(res);
    // soderberg/asme still produce finite, non-saturated sa_eq (denominator used Sut).
    const json& t = res["summary"]["theories"];
    const double soderberg = t["soderberg"]["sa_eq_mpa"].get<double>();
    const double asme = t["asme"]["sa_eq_mpa"].get<double>();
    CHECK(soderberg > 0.0);
    CHECK(asme > 0.0);
    CHECK(soderberg < 1e9);  // not the "fails" sentinel
    CHECK(asme < 1e9);
}

TEST_CASE("missing Sut everywhere is a clean error, not a crash") {
    json study = cube_study(json{{"se_corrected_mpa", 200.0}});
    study["material"].erase("ultimate_mpa");
    study["material"].erase("endurance_mpa");
    json res = fea_run_fatigue(study);
    CHECK_FALSE(res.value("ok", true));
    CHECK(res.value("error", std::string()).find("Sut") != std::string::npos);
}

TEST_CASE("failed static solve propagates ok:false") {
    json study = cube_study(json{{"se_corrected_mpa", 200.0}, {"ultimate_mpa", 440.0}});
    study["points"] = json::array();  // empty mesh -> static fails
    study["tets"] = json::array();
    json res = fea_run_fatigue(study);
    CHECK_FALSE(res.value("ok", true));
}

TEST_CASE("invalid fatigue cycle and endurance data fail before the static solve") {
    json reversed_zero =
        cube_study(json{{"ultimate_mpa", 440.0},
                        {"se_corrected_mpa", 200.0},
                        {"lmin", 1.0},
                        {"lmax", 1.0}});
    json result = fea_run_fatigue(reversed_zero);
    CHECK_FALSE(result.value("ok", true));
    CHECK(result.value("error", std::string()).find("lmax") !=
          std::string::npos);

    json invalid_endurance =
        cube_study(json{{"ultimate_mpa", 440.0},
                        {"se_corrected_mpa", 400.0}});
    result = fea_run_fatigue(invalid_endurance);
    CHECK_FALSE(result.value("ok", true));
    CHECK(result.value("error", std::string()).find("0.9*Sut") !=
          std::string::npos);

    json nonfinite =
        cube_study(json{{"ultimate_mpa", 440.0},
                        {"se_corrected_mpa", 200.0},
                        {"lmin", std::numeric_limits<double>::quiet_NaN()},
                        {"lmax", 1.0}});
    result = fea_run_fatigue(nonfinite);
    CHECK_FALSE(result.value("ok", true));

    json overflowing_range =
        cube_study(json{{"ultimate_mpa", 440.0},
                        {"se_corrected_mpa", 200.0},
                        {"lmin", -std::numeric_limits<double>::max()},
                        {"lmax", std::numeric_limits<double>::max()}});
    result = fea_run_fatigue(overflowing_range);
    CHECK_FALSE(result.value("ok", true));
    CHECK(result.value("error", std::string()).find("representable") !=
          std::string::npos);
}

TEST_CASE("accepted fatigue cancellation cannot publish a cached-static success") {
    using chudware::sim::cache::Kind;
    namespace sim_cache = chudware::sim::cache;

    sim_cache::clear();
    sim_cache::reset_stats();
    json study =
        cube_study(json{{"se_corrected_mpa", 200.0},
                        {"ultimate_mpa", 440.0}});
    study["job_id"] = "fatigue-cancel-after-static-cache-hit";

    json cache_study = study;
    cache_study.erase("job_id");
    const std::string hash =
        sim_cache::hash_fea_study(Kind::FeaStatic, cache_study);
    REQUIRE_FALSE(hash.empty());

    constexpr int kStressNodes = 1000000;
    const json cached_static = {
        {"ok", true},
        {"von_mises", std::vector<double>(kStressNodes, 100.0)},
        {"n_nodes", kStressNodes},
        {"n_elems", 1},
        {"n_dof", kStressNodes * 3}};
    REQUIRE(sim_cache::store(Kind::FeaStatic, hash, cached_static));

    std::atomic<bool> finished{false};
    json result;
    std::thread worker([&] {
        result = fea_run_fatigue(study);
        finished.store(true, std::memory_order_release);
    });

    const auto deadline =
        std::chrono::steady_clock::now() + std::chrono::seconds(5);
    while (!finished.load(std::memory_order_acquire) &&
           sim_cache::stats().hits == 0 &&
           std::chrono::steady_clock::now() < deadline)
        std::this_thread::yield();
    const bool saw_cache_hit = sim_cache::stats().hits > 0;
    json cancelled = {{"cancelled", false}};
    if (saw_cache_hit) {
        // Let the cache-hit wrapper return so cancellation targets fatigue
        // post-processing rather than the nested static call.
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
        cancelled = chudware::sim::sim_cancel(study["job_id"]);
    }
    worker.join();
    REQUIRE(saw_cache_hit);
    REQUIRE(cancelled.value("cancelled", false));

    CHECK_FALSE(result.value("ok", true));
    CHECK(result.value("error", std::string()).find("cancelled") !=
          std::string::npos);
    sim_cache::clear();
}

TEST_CASE("mean-stress theory limit is reported explicitly") {
    json result =
        fea_run_fatigue(cube_study(json{{"ultimate_mpa", 440.0},
                                        {"se_corrected_mpa", 200.0},
                                        {"yield_mpa", 370.0},
                                        {"lmin", 0.0},
                                        {"lmax", 1.0}}));
    check_summary_shape(result);
    const json& theories = result["summary"]["theories"];
    CHECK(theories["goodman"]["status"] ==
          "mean_stress_limit_exceeded");
    CHECK(theories["soderberg"]["status"] ==
          "mean_stress_limit_exceeded");
}

}  // namespace
