// core/hydraulic/motor_selftest.inc — doctest cases for hydraulic_size_motor
// and pump_drive_size (core/hydraulic/motor.*). #included by
// core/tests/test_hydraulic.cpp after its includes (doctest +
// `using namespace chudware::hydraulic;` already in scope). Every expected
// value is a hand calculation written out next to the check.
#include "hydraulic/motor.h"

namespace {
chudware::pyutil::ojson motor_args(double torque, double speed, double dp) {
    chudware::pyutil::ojson p = chudware::pyutil::ojson::object();
    p["torque_nm"] = torque;
    p["speed_rpm"] = speed;
    p["pressure_bar"] = dp;
    return p;
}
}  // namespace

TEST_CASE("size_motor: 100 N·m, 500 rpm, 200 bar hand calc") {
    // D = 2π·100 / (200e5 · 0.9) = 3.4907e-5 m³ = 34.907 cc/rev
    // Q = 34.907 · 500 / 0.95 = 18 372 cc/min = 18.372 L/min
    // P_out = 100 · 2π·500/60 = 5.236 kW;  P_in = 200e5 · 18.372/60000 = 6.124 kW
    const auto r = size_motor(motor_args(100, 500, 200));
    REQUIRE(r["ok"] == true);
    CHECK(r["displacement_cc_rev"].get<double>() == doctest::Approx(34.907).epsilon(1e-4));
    CHECK(r["flow_lpm"].get<double>() == doctest::Approx(18.372).epsilon(1e-4));
    CHECK(r["output_power_kw"].get<double>() == doctest::Approx(5.236).epsilon(1e-3));
    CHECK(r["input_hydraulic_power_kw"].get<double>() == doctest::Approx(6.124).epsilon(1e-3));
    CHECK(r["overall_efficiency"].get<double>() == doctest::Approx(0.855));
    // generic ISO 3662 R10 ladder: next step above 34.9 is 40
    CHECK(r["type"] == "generic");
    CHECK(r["standard_displacement_cc_rev"].get<double>() == doctest::Approx(40.0));
    // 40 cc: Δp = 2π·100/(40e-6·0.9)/1e5 = 174.5 bar; Q = 40·500/0.95 = 21.05 L/min
    CHECK(r["pressure_bar_at_standard"].get<double>() == doctest::Approx(174.53).epsilon(1e-3));
    CHECK(r["flow_lpm_at_standard"].get<double>() == doctest::Approx(21.053).epsilon(1e-3));
    CHECK(r["warnings"].is_array());
    CHECK(r.contains("hint"));
    CHECK(r["next_actions"].size() > 0);

    // gear ladder: next step above 34.9 is 36; 500 rpm and 200 bar are in range
    auto p = motor_args(100, 500, 200);
    p["type"] = "gear";
    const auto g = size_motor(p);
    REQUIRE(g["ok"] == true);
    CHECK(g["standard_displacement_cc_rev"].get<double>() == doctest::Approx(36.0));
    CHECK(g["warnings"].empty());

    // orbital at 2000 rpm, 200 bar: above the 1000 rpm typical ceiling and the
    // 175 bar continuous rating → two warnings
    p["type"] = "orbital";
    p["speed_rpm"] = 2000;
    const auto o = size_motor(p);
    REQUIRE(o["ok"] == true);
    CHECK(o["standard_displacement_cc_rev"].get<double>() == doctest::Approx(40.0));
    CHECK(o["warnings"].size() == 2);
}

TEST_CASE("size_motor: refusals by name") {
    CHECK(size_motor(motor_args(0, 500, 200))["reason"] == "invalid_torque_nm");
    CHECK(size_motor(motor_args(100, -1, 200))["reason"] == "invalid_speed_rpm");
    CHECK(size_motor(motor_args(100, 500, 0))["reason"] == "invalid_pressure_bar");
    auto p = motor_args(100, 500, 200);
    p.erase("pressure_bar");
    CHECK(size_motor(p)["reason"] == "missing_pressure_bar");
    p = motor_args(100, 500, 200);
    p["torque_nm"] = "lots";
    CHECK(size_motor(p)["reason"] == "invalid_torque_nm");
    p = motor_args(100, 500, 200);
    p["mech_efficiency"] = 1.2;
    CHECK(size_motor(p)["reason"] == "invalid_mech_efficiency");
    p = motor_args(100, 500, 200);
    p["type"] = "turbine";
    CHECK(size_motor(p)["reason"] == "invalid_type");
    // 1000 N·m at 100 bar → D = 2π·1000/(1e7·0.9) = 698 cc > gear max 100 cc
    p = motor_args(1000, 500, 100);
    p["type"] = "gear";
    const auto big = size_motor(p);
    CHECK(big["ok"] == false);
    CHECK(big["reason"] == "displacement_exceeds_catalog");
    CHECK(big["displacement_cc_rev"].get<double>() == doctest::Approx(698.13).epsilon(1e-3));
}

TEST_CASE("pump_drive_size: centrifugal 50 m³/h, 30 m, η 0.7 → IEC 7.5 kW 132M") {
    // P_hyd = 998 · 9.80665 · 30 · 50/3600 = 4.0779 kW; shaft = /0.7 = 5.8256 kW
    // required = 5.8256 · 1.15 = 6.6994 kW → IEC 7.5 kW; 1500 rpm @50 Hz → 4-pole
    ojson p = {{"kind", "centrifugal"}, {"flow_m3h", 50}, {"head_m", 30},
               {"pump_efficiency", 0.7}};
    const auto r = pump_drive_size(p);
    REQUIRE(r["ok"] == true);
    CHECK(r["hydraulic_power_kw"].get<double>() == doctest::Approx(4.0779).epsilon(1e-4));
    CHECK(r["shaft_power_kw"].get<double>() == doctest::Approx(5.8256).epsilon(1e-4));
    CHECK(r["required_motor_kw"].get<double>() == doctest::Approx(6.6994).epsilon(1e-4));
    CHECK(r["motor_rating_kw"].get<double>() == doctest::Approx(7.5));
    CHECK(r["poles"] == 4);
    CHECK(r["sync_speed_rpm"].get<double>() == doctest::Approx(1500.0));
    CHECK(r["iec_frame"] == "132M");
    // IEC 60034-30-1 IE3 4-pole 7.5 kW = 90.4 %; typical pf 0.84
    CHECK(r["motor_efficiency"].get<double>() == doctest::Approx(0.904));
    // FLC = (7.5/0.904) kW / (√3 · 400 · 0.84) = 14.256 A
    CHECK(r["full_load_current_a"].get<double>() == doctest::Approx(14.256).epsilon(1e-3));
    // input at duty = 5.8256/0.904 = 6.444 kW
    CHECK(r["input_power_kw"].get<double>() == doctest::Approx(6.4443).epsilon(1e-3));
    CHECK(r["warnings"].empty());
    // head → pressure: 998·9.80665·30 = 2.936 bar
    CHECK(r["pressure_bar"].get<double>() == doctest::Approx(2.9361).epsilon(1e-3));
}

TEST_CASE("pump_drive_size: hydraulic HPU 40 L/min, 180 bar, η 0.85 → IEC 18.5 kW") {
    // P_hyd = 180e5 · 40/60000 = 12.0 kW; shaft = 12/0.85 = 14.118 kW
    // required = 14.118 · 1.15 = 16.235 kW → IEC 18.5 kW, 4-pole frame 180M
    ojson p = {{"kind", "hydraulic"}, {"flow_lpm", 40}, {"pressure_bar", 180},
               {"pump_efficiency", 0.85}};
    const auto r = pump_drive_size(p);
    REQUIRE(r["ok"] == true);
    CHECK(r["hydraulic_power_kw"].get<double>() == doctest::Approx(12.0));
    CHECK(r["shaft_power_kw"].get<double>() == doctest::Approx(14.1176).epsilon(1e-4));
    CHECK(r["required_motor_kw"].get<double>() == doctest::Approx(16.2353).epsilon(1e-4));
    CHECK(r["motor_rating_kw"].get<double>() == doctest::Approx(18.5));
    CHECK(r["iec_frame"] == "180M");
    CHECK(r["motor_efficiency"].get<double>() == doctest::Approx(0.926));
    // IE4 at 18.5 kW = 94.2 %
    p["efficiency_class"] = "IE4";
    CHECK(pump_drive_size(p)["motor_efficiency"].get<double>() == doctest::Approx(0.942));
}

TEST_CASE("pump_drive_size: NEMA hp selection and T-frame") {
    // same HPU on NEMA: 16.235 kW / 0.7457 = 21.77 hp → 25 hp, 4-pole 284T,
    // defaults 60 Hz / 460 V → sync 1800 rpm at the default 4-pole speed
    ojson p = {{"kind", "hydraulic"}, {"flow_lpm", 40}, {"pressure_bar", 180},
               {"pump_efficiency", 0.85}, {"standard", "NEMA"}};
    const auto r = pump_drive_size(p);
    REQUIRE(r["ok"] == true);
    CHECK(r["motor_rating_hp"].get<double>() == doctest::Approx(25.0));
    CHECK(r["motor_rating_kw"].get<double>() == doctest::Approx(18.6425).epsilon(1e-4));
    CHECK(r["nema_frame"] == "284T");
    CHECK(r["frequency_hz"].get<double>() == doctest::Approx(60.0));
    CHECK(r["voltage_v"].get<double>() == doctest::Approx(460.0));
    CHECK(r["poles"] == 4);
    CHECK(r["sync_speed_rpm"].get<double>() == doctest::Approx(1800.0));
    CHECK(!r.contains("iec_frame"));
    // 1500 rpm at 60 Hz is 16.7 % below 4-pole sync → slip warning
    p["speed_rpm"] = 1500;
    const auto s = pump_drive_size(p);
    REQUIRE(s["ok"] == true);
    CHECK(s["warnings"].size() == 1);
}

TEST_CASE("pump_drive_size: pole selection, oversizing warning, refusals") {
    ojson base = {{"kind", "centrifugal"}, {"flow_m3h", 50}, {"head_m", 30}};
    auto p = base;
    p["speed_rpm"] = 2900;  // 50 Hz → 2-pole (3000 sync)
    CHECK(pump_drive_size(p)["poles"] == 2);
    p["speed_rpm"] = 980;  // → 6-pole (1000 sync)
    CHECK(pump_drive_size(p)["poles"] == 6);
    p["speed_rpm"] = 4000;  // above 2-pole sync → 2-pole + warning
    const auto over = pump_drive_size(p);
    CHECK(over["poles"] == 2);
    CHECK(over["warnings"].size() >= 1);

    // tiny duty: 1 L/min at 10 bar → 0.0167 kW hyd → smallest 0.12 kW, oversize warning
    ojson tiny = {{"kind", "hydraulic"}, {"flow_lpm", 1}, {"pressure_bar", 10}};
    const auto t = pump_drive_size(tiny);
    REQUIRE(t["ok"] == true);
    CHECK(t["motor_rating_kw"].get<double>() == doctest::Approx(0.12));
    CHECK(t["warnings"].size() == 1);

    p = base;
    p["flow_lpm"] = 100;
    CHECK(pump_drive_size(p)["reason"] == "ambiguous_flow");
    p = base;
    p.erase("head_m");
    CHECK(pump_drive_size(p)["reason"] == "missing_pressure");
    p = base;
    p.erase("kind");
    CHECK(pump_drive_size(p)["reason"] == "missing_kind");
    p = base;
    p["kind"] = "gas";
    CHECK(pump_drive_size(p)["reason"] == "invalid_kind");
    p = base;
    p["head_m"] = -3;
    CHECK(pump_drive_size(p)["reason"] == "invalid_head_m");
    p = base;
    p["service_factor"] = 0.9;
    CHECK(pump_drive_size(p)["reason"] == "invalid_service_factor");
    p = base;
    p["phases"] = 2;
    CHECK(pump_drive_size(p)["reason"] == "invalid_phases");
    p = base;
    p["efficiency_class"] = "IE5";
    CHECK(pump_drive_size(p)["reason"] == "invalid_efficiency_class");
    // 1000 m³/h at 150 m, η 0.7 → 584 kW shaft > 400 kW ladder
    ojson huge = {{"kind", "centrifugal"}, {"flow_m3h", 1000}, {"head_m", 150}};
    const auto h = pump_drive_size(huge);
    CHECK(h["ok"] == false);
    CHECK(h["reason"] == "drive_exceeds_ladder");
}
