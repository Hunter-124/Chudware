// Unit tests for the high-cycle fatigue study (aicad::sim::fea_run_fatigue).
//
// OCCT/oracle-free: an inline-meshed unit cube (6 tets) fixed at z=0 and pulled in
// +z gives a finite stress field; we assert the fatigue solver runs and returns the
// documented summary shape — Sut/Sy/Se echoes, lmin/lmax, crit_sigma_a/m, and a
// theories{} block with goodman/gerber/soderberg/asme/morrow each carrying sa_eq_mpa.
// The host (not the engine) finishes FoS / life / reliability from these scalars.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <nlohmann/json.hpp>

#include "sim/fea_fatigue.h"

using nlohmann::json;
using aicad::sim::fea_run_fatigue;

namespace {

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

}  // namespace
