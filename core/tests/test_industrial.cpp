// core/tests/test_industrial.cpp — industrial process sizing (OCCT-free,
// offline): ASME VIII-1 vessel_size and equal-friction duct_size. Every
// expected number is an independent hand calculation written out here from
// the code formula, not a replay of the implementation.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <algorithm>
#include <string>

#include "piping/duct.h"
#include "piping/vessel.h"

using namespace chudware::piping;
using chudware::pyutil::ojson;

namespace {

constexpr double kPi = 3.14159265358979323846;
constexpr double kKsi = 6.894757293168361;  // MPa per ksi
constexpr double kG = 9.80665;

ojson base_vessel() {
    return ojson{{"inner_diameter_mm", 1500.0}, {"tan_tan_length_mm", 3000.0},
                 {"design_pressure_kpa", 1000.0}, {"design_temp_c", 100.0},
                 {"material", "SA-516-70"},     {"corrosion_allowance_mm", 3.0},
                 {"joint_efficiency", 0.85},    {"head_type", "ellipsoidal_2_1"},
                 {"fluid_density_kgm3", 0.0}};
}

// Plain fixed-point Colebrook (independent of the Clamond implementation).
double colebrook(double re, double ed) {
    double x = 1.0 / std::sqrt(0.02);
    for (int i = 0; i < 200; ++i) x = -2.0 * std::log10(ed / 3.7 + 2.51 * x / re);
    return 1.0 / (x * x);
}

const ojson& seg(const ojson& r, const std::string& id) {
    for (const ojson& s : r["segments"]) {
        if (s["id"] == id) return s;
    }
    FAIL("segment " << id << " missing");
    return r;
}

}  // namespace

// ── vessel_size ──────────────────────────────────────────────────────────────

TEST_CASE("vessel: UG-27 shell + UG-32 2:1 head hand calc (ID 1500, 1000 kPa, SA-516-70 @100 C)") {
    const ojson r = vessel_size(base_vessel());
    REQUIRE(r["ok"] == true);
    CHECK(r["code"] == "ASME VIII-1");

    // S(SA-516-70, 100 °C = 212 °F) = 20.0 ksi (II-D 2023 Table 1A: flat to 500 °F).
    const double s = 20.0 * kKsi;
    CHECK(r["material"]["allowable_mpa"].get<double>() == doctest::Approx(s).epsilon(1e-3));
    const double se = s * 0.85;
    const double p = 1.0;               // MPa, no static head (gas service)
    const double r_corr = 750.0 + 3.0;  // corroded inside radius
    const double t_shell = p * r_corr / (se - 0.6 * p) + 3.0;  // ≈ 9.455 mm
    CHECK(r["shell"]["t_required_mm"].get<double>() == doctest::Approx(t_shell).epsilon(0.01));
    CHECK(t_shell == doctest::Approx(9.455).epsilon(0.001));
    CHECK(r["shell"]["t_nominal_mm"].get<double>() == 10.0);  // next standard plate up

    const double d_corr = 1506.0;
    const double t_head = p * d_corr / (2.0 * se - 0.2 * p) + 3.0;  // ≈ 9.427 mm
    CHECK(r["heads"]["t_required_mm"].get<double>() == doctest::Approx(t_head).epsilon(0.01));
    CHECK(r["heads"]["t_nominal_mm"].get<double>() == 10.0);
    CHECK(r["heads"]["depth_mm"].get<double>() == doctest::Approx(375.0));

    // MAWP back-calc with t = 10 − 3 = 7 mm: shell circumferential governs.
    const double mawp_shell = se * 7.0 / (r_corr + 0.6 * 7.0) * 1000.0;  // ≈ 1084 kPa
    const double mawp_head = 2.0 * se * 7.0 / (d_corr + 0.2 * 7.0) * 1000.0;
    CHECK(mawp_shell < mawp_head);
    CHECK(r["governing"] == "shell");
    CHECK(r["mawp_kpa"].get<double>() == doctest::Approx(mawp_shell).epsilon(1e-4));
    CHECK(r["heads"]["mawp_kpa"].get<double>() == doctest::Approx(mawp_head).epsilon(1e-4));

    // UG-99(b): LSR = 1 (20 ksi at both 20 °C and 100 °C).
    CHECK(r["lowest_stress_ratio"].get<double>() == doctest::Approx(1.0));
    CHECK(r["hydrotest_kpa"].get<double>() == doctest::Approx(1.3 * mawp_shell).epsilon(1e-4));

    // Volume: π·0.75²·3 + 2·π·1.5³/24 = 5.30144 + 0.88357 = 6.18501 m³.
    CHECK(r["volume_m3"].get<double>() == doctest::Approx(6.18501).epsilon(1e-4));
    CHECK(r["tan_tan_length_mm"].get<double>() == doctest::Approx(3000.0));

    // Masses: shell ring + heads at 7850 kg/m³; full water adds V·1000.
    const double shell_kg = kPi / 4.0 * (1520.0 * 1520.0 - 1500.0 * 1500.0) * 3000.0 * 1e-9 * 7850.0;
    const double heads_kg = 2.0 * kPi / 24.0 * (1520.0 * 1520.0 * 1520.0 - 1500.0 * 1500.0 * 1500.0) * 1e-9 * 7850.0;
    CHECK(r["mass_empty_kg"].get<double>() == doctest::Approx(shell_kg + heads_kg).epsilon(1e-3));
    CHECK(r["mass_full_water_kg"].get<double>() - r["mass_empty_kg"].get<double>() ==
          doctest::Approx(6185.01).epsilon(1e-3));
    CHECK(r["mass_operating_kg"].get<double>() == doctest::Approx(r["mass_empty_kg"].get<double>()).epsilon(1e-6));
    CHECK(r["warnings"].is_array());
    CHECK(r["next_actions"].is_array());
}

TEST_CASE("vessel: volume_m3 solves the tan-tan length") {
    ojson p = base_vessel();
    p.erase("tan_tan_length_mm");
    p["volume_m3"] = 6.18501;
    const ojson r = vessel_size(p);
    REQUIRE(r["ok"] == true);
    CHECK(r["tan_tan_length_mm"].get<double>() == doctest::Approx(3000.0).epsilon(1e-4));
    CHECK(r["volume_m3"].get<double>() == doctest::Approx(6.18501).epsilon(1e-5));

    p["volume_m3"] = 0.5;  // two heads alone hold 0.884 m³
    CHECK(vessel_size(p)["reason"] == "volume_too_small_for_diameter");
    p["tan_tan_length_mm"] = 3000.0;
    CHECK(vessel_size(p)["reason"] == "conflicting_length");
}

TEST_CASE("vessel: UG-22 static head of a full vertical vessel enters the calc pressure") {
    ojson p = base_vessel();
    p["fluid_density_kgm3"] = 1000.0;
    p["fill_fraction"] = 0.5;
    const ojson r = vessel_size(p);
    REQUIRE(r["ok"] == true);
    // Height = 3000 + 2·375 = 3750 mm → 1000·9.80665·3.75 Pa.
    const double static_kpa = 1000.0 * kG * 3.75 / 1000.0;
    CHECK(r["design"]["static_head_kpa"].get<double>() == doctest::Approx(static_kpa).epsilon(1e-4));
    const double se = 20.0 * kKsi * 0.85;
    const double pc = (1000.0 + static_kpa) / 1000.0;
    CHECK(r["shell"]["t_pressure_mm"].get<double>() ==
          doctest::Approx(pc * 753.0 / (se - 0.6 * pc)).epsilon(1e-3));
    // MAWP is reported at the top: component MAWP minus the static head.
    const double mawp_shell = se * 7.0 / (753.0 + 4.2) * 1000.0 - static_kpa;
    CHECK(r["mawp_kpa"].get<double>() == doctest::Approx(mawp_shell).epsilon(1e-4));
    // Operating mass at 50 % fill.
    CHECK(r["mass_operating_kg"].get<double>() - r["mass_empty_kg"].get<double>() ==
          doctest::Approx(0.5 * 6185.01).epsilon(1e-3));
    // Horizontal: liquid height is the ID.
    p["orientation"] = "horizontal";
    CHECK(vessel_size(p)["design"]["static_head_kpa"].get<double>() ==
          doctest::Approx(1000.0 * kG * 1.5 / 1000.0).epsilon(1e-4));
}

TEST_CASE("vessel: stainless interpolation, hydrotest stress ratio, head types") {
    ojson p = base_vessel();
    p["material"] = "SA-240 TP304";
    p["design_temp_c"] = 200.0;  // 392 °F: between 300 °F (15.0) and 400 °F (13.8)
    const ojson r = vessel_size(p);
    REQUIRE(r["ok"] == true);
    CHECK(r["material"]["name"] == "SA-240-304");
    const double s_ksi = 15.0 + (392.0 - 300.0) / 100.0 * (13.8 - 15.0);
    CHECK(r["material"]["allowable_mpa"].get<double>() == doctest::Approx(s_ksi * kKsi).epsilon(1e-3));
    CHECK(r["lowest_stress_ratio"].get<double>() == doctest::Approx(20.0 / s_ksi).epsilon(1e-3));
    CHECK(r["hydrotest_kpa"].get<double>() ==
          doctest::Approx(1.3 * r["mawp_kpa"].get<double>() * 20.0 / s_ksi).epsilon(1e-3));

    // Hemispherical heads need about half the ellipsoidal thickness.
    p = base_vessel();
    p["head_type"] = "hemispherical";
    const ojson h = vessel_size(p);
    REQUIRE(h["ok"] == true);
    const double se = 20.0 * kKsi * 0.85;
    CHECK(h["heads"]["t_pressure_mm"].get<double>() == doctest::Approx(1.0 * 753.0 / (2.0 * se - 0.2)).epsilon(1e-3));
    CHECK(h["heads"]["depth_mm"].get<double>() == doctest::Approx(750.0));

    p["head_type"] = "torispherical";
    const ojson t = vessel_size(p);
    REQUIRE(t["ok"] == true);
    CHECK(t["heads"]["t_pressure_mm"].get<double>() ==
          doctest::Approx(0.885 * 1.0 * 1506.0 / (se - 0.1)).epsilon(1e-3));
    CHECK(t["heads"]["depth_mm"].get<double>() == doctest::Approx(0.16934 * 1500.0).epsilon(1e-3));
}

TEST_CASE("vessel: refusals name the problem") {
    ojson p = base_vessel();
    p["material"] = "unobtainium";
    ojson r = vessel_size(p);
    CHECK(r["ok"] == false);
    CHECK(r["reason"] == "unknown_material");
    CHECK(r["hint"].get<std::string>().find("SA-516-70") != std::string::npos);

    p = base_vessel();
    p["design_temp_c"] = 600.0;  // SA-516-70 table ends at 1000 °F (538 °C)
    CHECK(vessel_size(p)["reason"] == "temperature_above_table");
    p["design_temp_c"] = -50.0;
    CHECK(vessel_size(p)["reason"] == "temperature_below_table");

    p = base_vessel();
    p["design_pressure_kpa"] = 60000.0;  // 60 MPa > 0.385SE ≈ 45 MPa
    CHECK(vessel_size(p)["reason"] == "thick_wall_beyond_ug27");
    p["design_pressure_kpa"] = -50.0;
    CHECK(vessel_size(p)["reason"] == "external_pressure_not_supported");

    p = base_vessel();
    p["head_type"] = "conical";
    CHECK(vessel_size(p)["reason"] == "unknown_head_type");
    p = base_vessel();
    p.erase("tan_tan_length_mm");
    CHECK(vessel_size(p)["reason"] == "missing_length");
}

// ── duct_size ────────────────────────────────────────────────────────────────

TEST_CASE("duct: 1 m3/s at 1 Pa/m matches the equal-friction chart") {
    const ojson r = duct_size(ojson{
        {"segments", ojson::array({ojson{{"id", "main"}, {"airflow_m3s", 1.0}, {"length_m", 10.0}}})}});
    REQUIRE(r["ok"] == true);
    const ojson& s = r["segments"][0];
    // ASHRAE friction chart (galvanized, ε 0.09 mm, 20 °C): 1000 L/s at
    // 1 Pa/m reads ≈ 440 mm and ≈ 6.5 m/s.
    const double d_exact = s["diameter_exact_mm"].get<double>();
    CHECK(d_exact == doctest::Approx(440.0).epsilon(0.05));
    const double v_exact = 1.0 / (kPi * std::pow(d_exact / 1000.0, 2) / 4.0);
    CHECK(v_exact == doctest::Approx(6.5).epsilon(0.05));
    // Snapped up to the R20 series: Ø450, friction below target.
    CHECK(s["diameter_mm"].get<double>() == 450.0);
    CHECK(s["velocity_ms"].get<double>() == doctest::Approx(1.0 / (kPi * 0.45 * 0.45 / 4.0)).epsilon(1e-3));
    CHECK(s["friction_rate_pa_per_m"].get<double>() <= 1.0);
    // Independent Colebrook check of the reported friction factor.
    const double rho = r["air"]["density_kgm3"].get<double>();
    const double mu = r["air"]["viscosity_pas"].get<double>();
    CHECK(rho == doctest::Approx(1.204).epsilon(2e-3));
    const double v = 1.0 / (kPi * 0.45 * 0.45 / 4.0);
    const double re = rho * v * 0.45 / mu;
    const double fd = colebrook(re, 0.09 / 450.0);
    CHECK(s["friction_factor"].get<double>() == doctest::Approx(fd).epsilon(2e-3));
    CHECK(s["dp_friction_pa"].get<double>() == doctest::Approx(fd * 10.0 / 0.45 * 0.5 * rho * v * v).epsilon(2e-3));
    CHECK(r["critical_path"] == ojson::array({"main"}));
    CHECK(r["fan_static_pa"].get<double>() == doctest::Approx(s["dp_total_pa"].get<double>()).epsilon(1e-3));
}

TEST_CASE("duct: velocity cap upsizes along the round series") {
    const ojson r = duct_size(ojson{
        {"max_velocity_ms", 5.0},
        {"segments", ojson::array({ojson{{"id", "main"}, {"airflow_m3s", 1.0}, {"length_m", 10.0}}})}});
    REQUIRE(r["ok"] == true);
    const ojson& s = r["segments"][0];
    CHECK(s["diameter_mm"].get<double>() == 560.0);  // Ø500 runs 5.09 m/s
    CHECK(s["upsized_for_velocity"] == true);
    CHECK(s["velocity_ms"].get<double>() <= 5.0);
    CHECK(r["warnings"].size() >= 1);
}

TEST_CASE("duct: Huebscher equivalent diameter identities") {
    // Square: De = 1.30·a^1.25/(2a)^0.25 = 1.30/2^0.25 · a.
    CHECK(huebscher_de(400.0, 400.0) == doctest::Approx(1.30 / std::pow(2.0, 0.25) * 400.0));
    // ASHRAE table: 600×300 → 457 mm.
    CHECK(huebscher_de(600.0, 300.0) == doctest::Approx(457.0).epsilon(0.002));
    CHECK(huebscher_de(300.0, 600.0) == doctest::Approx(huebscher_de(600.0, 300.0)));

    const ojson r = duct_size(ojson{{"segments", ojson::array({ojson{{"id", "r"},
                                                                     {"airflow_m3s", 1.0},
                                                                     {"length_m", 10.0},
                                                                     {"shape", "rect"},
                                                                     {"aspect_ratio", 2.0}}})}});
    REQUIRE(r["ok"] == true);
    const ojson& s = r["segments"][0];
    const double w = s["width_mm"].get<double>(), h = s["height_mm"].get<double>();
    CHECK(std::fmod(w, 50.0) == 0.0);
    CHECK(std::fmod(h, 50.0) == 0.0);
    CHECK(s["equivalent_diameter_mm"].get<double>() == doctest::Approx(huebscher_de(w, h)).epsilon(1e-3));
    CHECK(s["equivalent_diameter_mm"].get<double>() >= s["diameter_exact_mm"].get<double>());
    CHECK(s["velocity_ms"].get<double>() == doctest::Approx(1.0 / (w * h * 1e-6)).epsilon(1e-3));
    CHECK(s["friction_rate_pa_per_m"].get<double>() <= 1.0 + 1e-9);
}

TEST_CASE("duct: 3-segment tree critical path and fan static pressure") {
    const ojson r = duct_size(ojson{
        {"segments", ojson::array({
                         ojson{{"id", "main"}, {"airflow_m3s", 2.0}, {"length_m", 20.0}, {"k_fittings", 1.0}},
                         ojson{{"id", "a"}, {"parent", "main"}, {"airflow_m3s", 1.2}, {"length_m", 15.0},
                               {"k_fittings", 0.5}},
                         ojson{{"id", "b"}, {"parent", "main"}, {"airflow_m3s", 0.8}, {"length_m", 30.0},
                               {"k_fittings", 2.0}},
                     })}});
    REQUIRE(r["ok"] == true);
    const double rho = r["air"]["density_kgm3"].get<double>();
    double totals[3];
    int i = 0;
    for (const char* id : {"main", "a", "b"}) {
        const ojson& s = seg(r, id);
        const double v = s["velocity_ms"].get<double>();
        const double k = id == std::string("main") ? 1.0 : (id == std::string("a") ? 0.5 : 2.0);
        // Fitting loss k·½ρv², friction = rate · length, total = sum.
        CHECK(s["dp_fittings_pa"].get<double>() == doctest::Approx(k * 0.5 * rho * v * v).epsilon(2e-3));
        CHECK(s["dp_friction_pa"].get<double>() ==
              doctest::Approx(s["friction_rate_pa_per_m"].get<double>() * s["length_m"].get<double>()).epsilon(2e-3));
        totals[i] = s["dp_friction_pa"].get<double>() + s["dp_fittings_pa"].get<double>();
        CHECK(s["dp_total_pa"].get<double>() == doctest::Approx(totals[i]).epsilon(1e-3));
        CHECK(v <= 10.0);
        ++i;
    }
    const bool b_critical = totals[2] > totals[1];
    const double fan = totals[0] + std::max(totals[1], totals[2]);
    CHECK(r["fan_static_pa"].get<double>() == doctest::Approx(fan).epsilon(1e-3));
    CHECK(r["critical_path"] == ojson::array({"main", b_critical ? "b" : "a"}));
    CHECK(seg(r, "a")["path_pa"].get<double>() == doctest::Approx(totals[0] + totals[1]).epsilon(1e-3));
    CHECK(seg(r, "main")["children"].size() == 2);
}

TEST_CASE("duct: refusals for bad trees") {
    auto one = [](const char* id, const char* parent) {
        ojson s{{"id", id}, {"airflow_m3s", 0.5}, {"length_m", 5.0}};
        if (parent != nullptr) s["parent"] = parent;
        return s;
    };
    ojson r = duct_size(ojson{{"segments", ojson::array({one("a", nullptr), one("b", "ghost")})}});
    CHECK(r["ok"] == false);
    CHECK(r["reason"] == "unknown_parent");
    CHECK(r["hint"].get<std::string>().find("ghost") != std::string::npos);

    r = duct_size(ojson{{"segments", ojson::array({one("a", "c"), one("b", "a"), one("c", "b")})}});
    CHECK(r["reason"] == "cycle_detected");

    r = duct_size(ojson{{"segments", ojson::array({one("a", nullptr), one("a", nullptr)})}});
    CHECK(r["reason"] == "duplicate_segment_id");

    r = duct_size(ojson{{"method", "static_regain"}, {"segments", ojson::array({one("a", nullptr)})}});
    CHECK(r["reason"] == "unsupported_method");
}
