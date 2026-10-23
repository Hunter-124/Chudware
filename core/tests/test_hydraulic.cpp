// core/tests/test_hydraulic.cpp — hydraulic core (OCCT-free, offline): ISO
// 1219 circuit schema, cylinder/pump/accumulator/valve sizing hand-calcs,
// the hydraulic_simulate transient solver against analytic steady states,
// motor / pump-drive sizing, BOM, and the dispatch envelopes (incl. the
// hydraulic→piping and hydraulic→FEA handoff + verify flows end-to-end).
// Mirrors the Python oracle suite engine_oracle/hydraulic/test_hydraulic.py
// plus the services/mcp/test_hydraulic_handoff.py flow.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#define _USE_MATH_DEFINES
#include <algorithm>
#include <cmath>
#include <limits>
#include <map>
#include <set>

#include "hydraulic/bom.h"
#include "hydraulic/handoff.h"
#include "hydraulic/hydraulic.h"
#include "hydraulic/schema.h"
#include "hydraulic/sim.h"
#include "hydraulic/solve.h"
#include "piping/simhandoff.h"

using namespace chudware::hydraulic;
using chudware::pyutil::ojson;

#include "hydraulic/motor_selftest.inc"

// ── schema factories + validation (test_hydraulic.py) ────────────────────────

TEST_CASE("schema: new_hydraulic / new_component / new_hose / next_id") {
    const ojson doc = new_hydraulic("My Circuit");
    CHECK(doc["schema"] == "aicad.hydraulic/1");
    CHECK(doc["name"] == "My Circuit");
    CHECK(doc["units"]["pressure"] == "bar");
    CHECK(doc["units"]["flow"] == "L/min");
    CHECK(doc["units"]["force"] == "kN");
    CHECK(doc["units"]["position"] == "mm");
    CHECK(doc["id"].get<std::string>().size() == 36);  // uuid4
    CHECK(doc["components"].empty());
    CHECK(doc["hoses"].empty());
    CHECK(doc["annotations"].empty());

    const ojson c = new_component("c1", "pump", "Main Pump", 100, 200);
    CHECK(c["id"] == "c1");
    CHECK(c["type"] == "pump");
    CHECK(c["name"] == "Main Pump");
    CHECK(c["position"] == ojson::array({100.0, 200.0}));
    CHECK(c["angle"] == 0.0);
    CHECK(c["sourcing"]["mpn"].is_null());
    CHECK(c["sourcing"]["quantity"] == 1);
    CHECK(c["sourcing"]["unit"] == "ea");

    const ojson c2 = new_component("c1", "pump", "", 0, 0,
                                   ojson::parse(R"({"bore_mm": 80, "stroke_mm": 200})"));
    CHECK(c2["params"]["bore_mm"] == 80);
    CHECK(c2["params"]["stroke_mm"] == 200);
    CHECK(new_component("c1", "cylinder_linear")["name"] == "cylinder_linear");

    const ojson h = new_hose("h1", "PRESSURE_LINE",
                             ojson::parse(R"([{"component_id":"c1","port":"p1"},
                                              {"component_id":"c2","port":"p2"}])"));
    CHECK(h["connections"].size() == 2);

    ojson d2 = new_hydraulic();
    CHECK(next_id(d2, "c") == "c1");
    CHECK(next_id(d2, "h") == "h1");
    d2["components"].push_back(new_component("c1", "pump"));
    d2["components"].push_back(new_component("c3", "reservoir"));
    CHECK(next_id(d2, "c") == "c4");
}

static ojson minimal_doc() {
    ojson doc = new_hydraulic("Test");
    doc["components"].push_back(new_component("c1", "pump", "Pump", 0, 0));
    doc["components"].push_back(new_component("c2", "reservoir", "Tank", 100, 0));
    doc["hoses"].push_back(new_hose("h1", "PRESSURE",
                                    ojson::parse(R"([{"component_id":"c1","port":"p1"},
                                                     {"component_id":"c2","port":"p2"}])")));
    return doc;
}

TEST_CASE("schema: validate_hydraulic") {
    CHECK(validate_hydraulic(minimal_doc()).empty());

    ojson bad_type = minimal_doc();
    bad_type["components"][0]["type"] = "rocket_engine";
    auto errs = validate_hydraulic(bad_type);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("HYD_COMPONENT_TYPES") != std::string::npos;
    }) != errs.end());

    ojson bad_port = minimal_doc();
    bad_port["components"][0]["ports"] =
        ojson::parse(R"([{"id":"p1","port_type":"exhaust"}])");
    errs = validate_hydraulic(bad_port);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("HYD_PORT_TYPES") != std::string::npos;
    }) != errs.end());

    ojson dup = minimal_doc();
    dup["components"].push_back(new_component("c1", "filter"));
    errs = validate_hydraulic(dup);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("duplicate") != std::string::npos;
    }) != errs.end());

    ojson wrong = new_hydraulic();
    wrong["schema"] = "wrong/1";
    errs = validate_hydraulic(wrong);
    REQUIRE(errs.size() == 1);
    CHECK(errs[0].find("schema") != std::string::npos);

    ojson bad_ref = minimal_doc();
    bad_ref["hoses"].push_back(
        new_hose("h2", "DRAIN", ojson::parse(R"([{"component_id":"c999","port":"drain"}])")));
    errs = validate_hydraulic(bad_ref);
    CHECK(std::find_if(errs.begin(), errs.end(), [](const std::string& e) {
        return e.find("c999") != std::string::npos;
    }) != errs.end());

    ojson bad_units = minimal_doc();
    bad_units["units"]["flow"] = "m3/s";
    CHECK_FALSE(validate_hydraulic(bad_units).empty());

    ojson one_ended = minimal_doc();
    one_ended["hoses"][0]["connections"].erase(1);
    CHECK_FALSE(validate_hydraulic(one_ended).empty());

    ojson duplicate_end = minimal_doc();
    duplicate_end["hoses"][0]["connections"][1] =
        duplicate_end["hoses"][0]["connections"][0];
    CHECK_FALSE(validate_hydraulic(duplicate_end).empty());

    ojson nonfinite = minimal_doc();
    nonfinite["components"][0]["position"][0] =
        std::numeric_limits<double>::infinity();
    CHECK_FALSE(validate_hydraulic(nonfinite).empty());
}

// ── sizing hand-calcs (test_hydraulic.py) ────────────────────────────────────

TEST_CASE("size_cylinder: 50 mm bore @200 bar → ≈39.27 kN; envelope guards") {
    const ojson r = size_cylinder(50, 0, 200, 100, 50.0, 1.0);
    REQUIRE(r["ok"] == true);
    CHECK(std::abs(r["extend_force_kn"].get<double>() - 39.270) < 0.01);

    CHECK(size_cylinder(80, 56, 200, 300)["retract_force_kn"].get<double>() <
          size_cylinder(80, 56, 200, 300)["extend_force_kn"].get<double>());
    CHECK(std::abs(size_cylinder(50, 0, 100, 500, 100)["cycle_time_s"].get<double>() - 5.0) <
          0.001);
    CHECK(size_cylinder(63, 0, 150, 200)["flow_extend_lpm"].get<double>() > 0);
    CHECK(std::abs(size_cylinder(100, 0, 200, 500)["area_bore_cm2"].get<double>() - 78.54) <
          0.1);
    // guards
    CHECK(size_cylinder(0, 0, 100, 100)["ok"] == false);
    CHECK(size_cylinder(0, 0, 100, 100)["reason"] == "bore_mm must be > 0");
    CHECK(size_cylinder(50, 50, 100, 100)["ok"] == false);
    CHECK(size_cylinder(50, 0, 100, 100, 50.0, 0.0)["ok"] == false);
    const double area_m2 = r["area_bore_cm2"].get<double>() * 1e-4;
    CHECK(r["extend_force_kn"].get<double>() ==
          doctest::Approx(area_m2 * 200.0e5 / 1000.0).epsilon(2e-5));
    CHECK(r["flow_extend_lpm"].get<double>() ==
          doctest::Approx(area_m2 * 0.05 * 60000.0).epsilon(2e-5));
    CHECK(size_cylinder(std::numeric_limits<double>::infinity(), 0, 100, 100)["ok"] ==
          false);
    CHECK(size_cylinder(50, 0, std::numeric_limits<double>::quiet_NaN(), 100)["ok"] ==
          false);
}

TEST_CASE("size_pump: 10 L/min @200 bar η=0.85 → ≈3.33/3.92 kW; displacement") {
    const ojson r = size_pump(10, 200, 0.85);
    REQUIRE(r["ok"] == true);
    CHECK(std::abs(r["hydraulic_power_kw"].get<double>() - 3.333) < 0.01);
    CHECK(std::abs(r["shaft_power_kw"].get<double>() - 3.922) < 0.01);
    CHECK(std::abs(size_pump(15, 100, 0.85, 1500)["displacement_cc_rev"].get<double>() -
                   10.0) < 0.01);
    CHECK(size_pump(20, 200)["torque_nm"].get<double>() > 0);
    CHECK(size_pump(0, 100)["ok"] == false);
    CHECK(size_pump(10, 100, 0.0)["ok"] == false);
    CHECK(r["hydraulic_power_kw"].get<double>() ==
          doctest::Approx((10.0 / 60000.0) * 200.0e5 / 1000.0).epsilon(2e-4));
    CHECK(r["shaft_power_kw"].get<double>() ==
          doctest::Approx(r["hydraulic_power_kw"].get<double>() / 0.85).epsilon(2e-4));
    CHECK(size_pump(std::numeric_limits<double>::quiet_NaN(), 100)["ok"] == false);
}

TEST_CASE("size_accumulator: Boyle hand-calc + ordering guards") {
    const ojson r = size_accumulator(100, 250, 150, 5);
    REQUIRE(r["ok"] == true);
    CHECK(std::abs(r["total_volume_l"].get<double>() - 18.73) < 0.1);
    CHECK(std::abs(size_accumulator(100, 300, 150, 2)["pressure_ratio"].get<double>() - 3.0) <
          1e-3);
    CHECK(size_accumulator(150, 300, 150, 5)["ok"] == false);  // pre >= min
    CHECK(size_accumulator(100, 150, 200, 5)["ok"] == false);  // min >= max
    CHECK(size_accumulator(100, 300, 150,
                           std::numeric_limits<double>::infinity())["ok"] == false);
}

TEST_CASE("valve_flow: Kv conversion + guards") {
    CHECK(valve_flow(10, 5)["flow_lpm"].get<double>() > 0);
    CHECK(std::abs(valve_flow(10, 1, 1.0)["Kv"].get<double>() - 8.65) < 0.001);
    CHECK(valve_flow(0, 5)["ok"] == false);
    CHECK(valve_flow(10, 0)["ok"] == false);
    CHECK(valve_flow(5, 8)["flow_lpm"].get<double>() >
          valve_flow(5, 2)["flow_lpm"].get<double>());
    CHECK(valve_flow(5, std::numeric_limits<double>::infinity())["ok"] == false);
}

// ── hydraulic_simulate: transient solve vs analytic steady states ────────────

namespace {

struct SimCircuit {
    ojson doc = new_hydraulic("sim");
    void comp(const std::string& id, const std::string& type, const ojson& params) {
        doc["components"].push_back(new_component(id, type, "", 0.0, 0.0, params));
    }
    void hose(const std::string& id,
              std::initializer_list<std::pair<const char*, const char*>> ends) {
        ojson conns = ojson::array();
        for (const auto& [cid, port] : ends)
            conns.push_back(ojson::object({{"component_id", cid}, {"port", port}}));
        doc["hoses"].push_back(new_hose(id, "", conns));
    }
};

double at(const ojson& series, const ojson& time, double t) {
    for (size_t k = 0; k < time.size(); ++k)
        if (time[k].get<double>() >= t - 1e-9) return series[k].get<double>();
    return series.back().get<double>();
}

// Pump (10 cc × 1500 rpm × ηv 0.95 = 14.25 L/min) → 4/3 closed-centre valve
// (30 L/min @ 5 bar per path) → Ø50/28 × 200 mm cylinder; relief 200 bar,
// 10 bar override at 14.25 L/min.
ojson cylinder_circuit() {
    SimCircuit c;
    c.comp("c1", "pump", {{"displacement_cc_rev", 10}, {"speed_rpm", 1500},
                          {"vol_efficiency", 0.95}, {"mech_efficiency", 0.9}});
    c.comp("c2", "valve_relief",
           {{"set_pressure_bar", 200}, {"override_bar", 10}, {"rated_flow_lpm", 14.25}});
    c.comp("c3", "valve_directional", {{"center_condition", "closed"},
                                       {"rated_flow_lpm", 30}, {"rated_dp_bar", 5}});
    c.comp("c4", "cylinder_linear", {{"bore_mm", 50}, {"rod_mm", 28}, {"stroke_mm", 200}});
    c.comp("c5", "reservoir", ojson::object());
    c.hose("h1", {{"c1", "P"}, {"c2", "P"}, {"c3", "P"}});
    c.hose("h2", {{"c3", "A"}, {"c4", "A"}});
    c.hose("h3", {{"c3", "B"}, {"c4", "B"}});
    c.hose("h4", {{"c2", "T"}, {"c3", "T"}, {"c5", "T"}});
    return c.doc;
}

// Pump (30 L/min delivered) → motor 20 cc/rev, ηv 0.9, ηm 0.85, driving a
// 30 N·m constant torque + 0.1 N·m·s/rad viscous load; relief 280 bar.
ojson motor_params() {
    SimCircuit c;
    c.comp("c1", "pump", {{"flow_lpm", 30}, {"vol_efficiency", 0.95}});
    c.comp("c2", "valve_relief",
           {{"set_pressure_bar", 280}, {"override_bar", 10}, {"rated_flow_lpm", 30}});
    c.comp("c3", "motor_hydraulic", {{"displacement_cc_rev", 20}, {"vol_efficiency", 0.9},
                                     {"mech_efficiency", 0.85}, {"inertia_kgm2", 0.005}});
    c.comp("c4", "reservoir", ojson::object());
    c.hose("h1", {{"c1", "P"}, {"c2", "P"}, {"c3", "A"}});
    c.hose("h2", {{"c3", "B"}, {"c2", "T"}, {"c4", "T"}});
    return ojson::object(
        {{"doc", c.doc},
         {"duration_s", 3.0},
         {"loads", {{"c3", {{"torque_nm", 30}, {"inertia_kgm2", 0.02}, {"viscous_nms", 0.1}}}}}});
}

void check_template_simulation(const char* template_id) {
    INFO(template_id);
    ojson created;
    REQUIRE(call("hydraulic_new", {{"name", "Template study"}, {"template", template_id}}, created));
    REQUIRE(created["ok"] == true);
    CHECK(created["template"] == template_id);
    const ojson& doc = created["doc"];
    CHECK(doc["name"] == "Template study");
    CHECK(validate_hydraulic(doc).empty());
    REQUIRE(created["simulation"].is_object());
    CHECK(!created["simulation"].contains("doc"));

    std::set<std::pair<std::string, std::string>> connected;
    for (const auto& h : doc["hoses"]) {
        CHECK(!h["net_name"].get<std::string>().empty());
        CHECK(h["length_mm"].get<double>() > 0.0);
        CHECK(h["id_mm"].get<double>() > 0.0);
        CHECK(h["rating_bar"].get<double>() > 0.0);
        for (const auto& end : h["connections"])
            connected.emplace(end["component_id"].get<std::string>(), end["port"].get<std::string>());
    }
    const std::map<std::string, std::vector<std::string>> required = {
        {"reservoir", {"T", "S"}}, {"pump", {"P", "T"}},
        {"valve_relief", {"P", "T"}}, {"filter", {"IN", "OUT"}},
        {"valve_directional", {"P", "T", "A", "B"}},
        {"cylinder_linear", {"A", "B"}}, {"motor_hydraulic", {"A", "B"}}};
    double relief_bar = 0.0;
    std::string actuator_id, actuator_type, filter_id;
    for (const auto& c : doc["components"]) {
        const std::string id = c["id"].get<std::string>();
        const std::string type = c["type"].get<std::string>();
        CHECK(!c["name"].get<std::string>().empty());
        REQUIRE(required.count(type) == 1);
        for (const auto& port : required.at(type)) {
            INFO(id, ".", port);
            CHECK(connected.count({id, port}) == 1);
        }
        REQUIRE(!c["ports"].empty());
        for (const auto& port : c["ports"])
            CHECK(connected.count({id, port["id"].get<std::string>()}) == 1);
        if (type == "valve_relief") relief_bar = c["params"]["set_pressure_bar"].get<double>();
        if (type == "filter") filter_id = id;
        if (type == "cylinder_linear" || type == "motor_hydraulic") {
            actuator_id = id;
            actuator_type = type;
            CHECK(c["position"][1].get<double>() > 180.0);
        }
    }
    for (size_t i = 0; i < doc["components"].size(); ++i) {
        const auto& a = doc["components"][i]["position"];
        for (size_t j = i + 1; j < doc["components"].size(); ++j) {
            const auto& b = doc["components"][j]["position"];
            CHECK(std::hypot(a[0].get<double>() - b[0].get<double>(),
                             a[1].get<double>() - b[1].get<double>()) >= 100.0);
        }
    }

    ojson params = created["simulation"];
    params["doc"] = doc;
    ojson r;
    REQUIRE(call("hydraulic_simulate", params, r));
    INFO(r.dump());
    REQUIRE(r["ok"] == true);
    CHECK(r["warnings"].empty());
    const double peak_bar = r["summary"]["peak_pressure_bar"].get<double>();
    CHECK(std::isfinite(peak_bar));
    CHECK(peak_bar > 0.0);
    // 10 bar relief override plus 20 bar for return backpressure / switching transients.
    CHECK(peak_bar <= relief_bar + 30.0);
    for (const auto& n : r["nodes"])
        for (const auto& p : n["pressure_bar"]) CHECK(std::isfinite(p.get<double>()));
    for (const auto& c : r["components"])
        for (const auto& series : c["series"])
            for (const auto& value : series) CHECK(std::isfinite(value.get<double>()));
    REQUIRE(!filter_id.empty());
    CHECK(r["components"][filter_id]["series"]["flow_lpm"].back().get<double>() > 0.0);
    if (!actuator_id.empty()) {
        const auto& setup = created["simulation"];
        REQUIRE(setup["valve_schedule"].size() == 3);
        CHECK(setup["valve_schedule"][0]["t_s"] == 0.0);
        CHECK(setup["valve_schedule"][0]["position"] == "a");
        CHECK(setup["valve_schedule"][1]["t_s"].get<double>() ==
              doctest::Approx(setup["duration_s"].get<double>() / 2.0));
        CHECK(setup["valve_schedule"][1]["position"] == "b");
        CHECK(setup["valve_schedule"][2]["t_s"] == 2.5);
        CHECK(r["summary"]["useful_work_kj"].get<double>() > 1.0);
        CHECK(r["summary"]["efficiency"].get<double>() > 0.1);
        const auto& series = r["components"][actuator_id]["series"];
        if (actuator_type == "cylinder_linear") {
            CHECK(setup["loads"][actuator_id]["force_n"] == 10000.0);
            CHECK(setup["loads"][actuator_id]["mass_kg"] == 50.0);
            const auto& position = series["position_mm"];
            const auto& motion = r["summary"]["cylinders"][actuator_id];
            CHECK(motion["end_reached"] == true);
            REQUIRE(motion["stroke_time_s"].is_number());
            CHECK(std::isfinite(motion["stroke_time_s"].get<double>()));
            CHECK(motion["stroke_time_s"].get<double>() > 0.0);
            REQUIRE(motion["end_reached_at_s"].is_number());
            CHECK(motion["end_reached_at_s"].get<double>() <
                  setup["valve_schedule"][1]["t_s"].get<double>());
            CHECK(at(position, r["time_s"], 2.0) == doctest::Approx(300.0).epsilon(0.001));
            CHECK(position.back().get<double>() > 100.0);
            CHECK(position.back().get<double>() < 250.0);
            CHECK(setup["valve_schedule"][2]["position"] == "center");
        } else {
            CHECK(setup["loads"][actuator_id]["torque_nm"] == 50.0);
            CHECK(at(series["speed_rpm"], r["time_s"], 1.2) > 100.0);
            CHECK(at(series["speed_rpm"], r["time_s"], 2.4) < -100.0);
            CHECK(setup["valve_schedule"][2]["position"] == "a");
            const double delivered_lpm = doc["components"][1]["params"]["displacement_cc_rev"].get<double>() *
                                         doc["components"][1]["params"]["speed_rpm"].get<double>() *
                                         doc["components"][1]["params"]["vol_efficiency"].get<double>() / 1000.0;
            const auto& motor = doc["components"][5]["params"];
            const double steady_rpm = delivered_lpm * 1000.0 *
                                      motor["vol_efficiency"].get<double>() /
                                      motor["displacement_cc_rev"].get<double>();
            CHECK(r["summary"]["motors"][actuator_id]["steady_speed_rpm"].get<double>() ==
                  doctest::Approx(steady_rpm).epsilon(0.01));
            CHECK(r["summary"]["motors"][actuator_id]["max_speed_rpm"].get<double>() <=
                  motor["max_speed_rpm"].get<double>());
            for (size_t k = 0; k < r["time_s"].size(); ++k)
                if (r["time_s"][k].get<double>() >= 0.9 * setup["duration_s"].get<double>())
                    CHECK(series["speed_rpm"][k].get<double>() ==
                          doctest::Approx(steady_rpm).epsilon(0.01));
        }
    } else {
        CHECK(r["summary"]["relief_open_time_s"].get<double>() > 0.5);
    }
}

}  // namespace

TEST_CASE("simulate: pump → relief deadhead settles at cracking + override, P = p·Q") {
    SimCircuit c;
    c.comp("c1", "pump", {{"displacement_cc_rev", 10}, {"speed_rpm", 1500},
                          {"vol_efficiency", 0.95}});
    c.comp("c2", "valve_relief",
           {{"set_pressure_bar", 150}, {"override_bar", 10}, {"rated_flow_lpm", 14.25}});
    c.comp("c3", "reservoir", ojson::object());
    c.hose("h1", {{"c1", "P"}, {"c2", "P"}});
    c.hose("h2", {{"c2", "T"}, {"c3", "T"}});
    const ojson r = simulate(ojson::object({{"doc", c.doc}, {"duration_s", 1.0}}));
    REQUIRE(r["ok"] == true);
    const double p_end = r["nodes"]["h1"]["pressure_bar"].back().get<double>();
    CHECK(p_end == doctest::Approx(160.0).epsilon(0.005));  // 150 cracking + 10 override
    const ojson& pump = r["components"]["c1"]["series"];
    CHECK(pump["flow_lpm"].back().get<double>() == doctest::Approx(14.25).epsilon(0.002));
    // hydraulic power = p·Q = 160e5 Pa × 14.25/60000 m³/s = 3.80 kW
    CHECK(pump["power_kw"].back().get<double>() == doctest::Approx(3.80).epsilon(0.005));
    CHECK(r["components"]["c2"]["series"]["flow_lpm"].back().get<double>() ==
          doctest::Approx(14.25).epsilon(0.005));
    CHECK(r["summary"]["relief_open_time_s"].get<double>() > 0.95);
    CHECK(r["summary"]["peak_pressure_bar"].get<double>() < 175.0);
    CHECK(r["nodes"]["h2"]["boundary"] == true);
    // all pump power ends as heat across the relief
    const double pump_kj = r["summary"]["pump_energy_kj"].get<double>();
    CHECK(r["summary"]["heat_kj"].get<double>() == doctest::Approx(pump_kj).epsilon(0.01));
}

TEST_CASE("simulate: 4/3 valve extends a loaded cylinder at Q·ηv/A, then hits the stop") {
    const ojson r = simulate(ojson::object(
        {{"doc", cylinder_circuit()},
         {"duration_s", 3.0},
         {"valve_schedule", ojson::parse(R"([{"t_s":0.1,"component_id":"c3","position":"a"}])")},
         {"loads", {{"c4", {{"force_n", 10000}, {"mass_kg", 100}, {"viscous_ns_m", 2000}}}}}}));
    REQUIRE(r["ok"] == true);
    const ojson& time = r["time_s"];
    const ojson& cyl = r["components"]["c4"]["series"];
    const double a_bore = M_PI / 4.0 * 0.05 * 0.05;
    const double a_rod = M_PI / 4.0 * (0.05 * 0.05 - 0.028 * 0.028);
    const double q = 10e-6 * 1500.0 / 60.0 * 0.95;  // m³/s
    const double v = q / a_bore;                      // 0.12096 m/s
    CHECK(at(cyl["velocity_mms"], time, 1.0) == doctest::Approx(v * 1e3).epsilon(0.03));
    // p_A·A_bore = F + c·v + p_B·A_rod; valve Δp = 5 bar·(Q/30 L/min)² per path
    const double dp_pa = 5e5 * std::pow(q / (30.0 / 60000.0), 2);
    const double dp_bt = 5e5 * std::pow(q * a_rod / a_bore / (30.0 / 60000.0), 2);
    const double p_a = (10000.0 + 2000.0 * v + dp_bt * a_rod) / a_bore;
    CHECK(at(cyl["pressure_a_bar"], time, 1.0) == doctest::Approx(p_a / 1e5).epsilon(0.03));
    CHECK(at(r["components"]["c1"]["series"]["pressure_bar"], time, 1.0) ==
          doctest::Approx((p_a + dp_pa) / 1e5).epsilon(0.03));
    const ojson& cs = r["summary"]["cylinders"]["c4"];
    REQUIRE(cs["end_reached"] == true);
    CHECK(cs["stroke_time_s"].get<double>() == doctest::Approx(0.2 / v).epsilon(0.05));
    CHECK(cyl["position_mm"].back().get<double>() == doctest::Approx(200.0).epsilon(0.002));
    // at the end stop all pump flow crosses the relief: 200 + 10 bar
    CHECK(r["components"]["c1"]["series"]["pressure_bar"].back().get<double>() ==
          doctest::Approx(210.0).epsilon(0.01));
    CHECK(r["summary"]["relief_open_time_s"].get<double>() > 0.5);
}

TEST_CASE("simulate: hydraulic motor speed Q·ηv/D and pressure 2πT/(D·ηm)") {
    const ojson r = simulate(motor_params());
    REQUIRE(r["ok"] == true);
    const double w = 30.0 / 60000.0 * 0.9 / (20e-6 / (2.0 * M_PI));  // rad/s
    const double rpm = w * 60.0 / (2.0 * M_PI);                     // 1350 rpm
    const double torque = 30.0 + 0.1 * w;
    const ojson& ms = r["summary"]["motors"]["c3"];
    CHECK(ms["steady_speed_rpm"].get<double>() == doctest::Approx(rpm).epsilon(0.03));
    CHECK(ms["steady_torque_nm"].get<double>() == doctest::Approx(torque).epsilon(0.03));
    const double dp_bar = 2.0 * M_PI * torque / (20e-6 * 0.85) / 1e5;  // ≈163 bar
    const ojson& mser = r["components"]["c3"]["series"]["pressure_bar"];
    double tail = 0.0;
    const size_t n = mser.size();
    for (size_t k = n - n / 10; k < n; ++k) tail += mser[k].get<double>();
    CHECK(tail / static_cast<double>(n / 10) == doctest::Approx(dp_bar).epsilon(0.03));
}

TEST_CASE("simulate: accumulator discharge follows the polytropic gas law") {
    SimCircuit c;
    c.comp("c1", "accumulator",
           {{"volume_l", 4.0}, {"pre_charge_bar", 50.0}, {"polytropic_n", 1.4}});
    c.comp("c2", "valve_flow_control", {{"Kv", 0.5}});
    c.comp("c3", "reservoir", ojson::object());
    c.hose("h1", {{"c1", "P"}, {"c2", "IN"}});
    c.hose("h2", {{"c2", "OUT"}, {"c3", "T"}});
    const ojson r = simulate(ojson::object({{"doc", c.doc},
                                            {"duration_s", 4.0},
                                            {"output_dt_s", 0.01},
                                            {"initial", {{"c1", {{"pressure_bar", 150.0}}}}}}));
    REQUIRE(r["ok"] == true);
    const ojson& acc = r["components"]["c1"]["series"];
    const double patm = 1.01325, p0 = 50.0 + patm;
    // initial oil volume: V0·(1 − (p0/p1)^(1/n)) = 2.158 L at 150 bar
    CHECK(acc["gas_volume_l"][0].get<double>() ==
          doctest::Approx(4.0 * std::pow(p0 / (150.0 + patm), 1.0 / 1.4)).epsilon(1e-4));
    int checked = 0;
    for (size_t k = 0; k < acc["pressure_bar"].size(); ++k) {
        const double p = acc["pressure_bar"][k].get<double>();
        if (p < 52.0) continue;
        const double vg = 4.0 * std::pow(p0 / (p + patm), 1.0 / 1.4);
        CHECK(acc["gas_volume_l"][k].get<double>() == doctest::Approx(vg).epsilon(0.002));
        ++checked;
    }
    CHECK(checked > 20);
    // mass conservation: ∫Q dt out of the accumulator = gas expansion
    double out_l = 0.0;
    const ojson& time = r["time_s"];
    for (size_t k = 1; k < time.size(); ++k) {
        const double dt = time[k].get<double>() - time[k - 1].get<double>();
        out_l -= 0.5 * (acc["flow_lpm"][k].get<double>() + acc["flow_lpm"][k - 1].get<double>()) *
                 dt / 60.0;
    }
    const double expanded =
        acc["gas_volume_l"].back().get<double>() - acc["gas_volume_l"][0].get<double>();
    CHECK(out_l == doctest::Approx(expanded).epsilon(0.02));
    // fully discharged: gas back at the pre-charge volume, node bled to tank
    CHECK(acc["gas_volume_l"].back().get<double>() == doctest::Approx(4.0).epsilon(1e-3));
    CHECK(r["nodes"]["h1"]["pressure_bar"].back().get<double>() < 50.0);
}

TEST_CASE("simulate: energy balance pump = useful work + heat") {
    const ojson r = simulate(motor_params());
    REQUIRE(r["ok"] == true);
    const ojson& s = r["summary"];
    const double pump = s["pump_energy_kj"].get<double>();
    const double useful = s["useful_work_kj"].get<double>();
    const double heat = s["heat_kj"].get<double>();
    CHECK(pump > 10.0);
    CHECK(useful + heat == doctest::Approx(pump).epsilon(0.02));
    CHECK(s["energy_balance_error_pct"].get<double>() < 0.1);
    // steady useful power = 30 N·m × ω; ≈ 0.52 of the hydraulic input
    CHECK(s["efficiency"].get<double>() == doctest::Approx(useful / pump));
    CHECK(s["efficiency"].get<double>() > 0.4);
    CHECK(s["efficiency"].get<double>() < 0.6);
}

TEST_CASE("simulate: refusals name the unconnected port, unknown ids, missing params") {
    // rod side of a double-acting cylinder left unconnected
    ojson doc = cylinder_circuit();
    doc["hoses"].erase(2);  // h3 (c3.B ↔ c4.B)
    ojson r = simulate(ojson::object({{"doc", doc}}));
    CHECK(r["ok"] == false);
    CHECK(r["reason"] == "unconnected_port");
    CHECK(r["component_id"] == "c3");
    CHECK(r["port"] == "B");

    r = simulate(ojson::object(
        {{"doc", cylinder_circuit()},
         {"valve_schedule",
          ojson::parse(R"([{"t_s":0.1,"component_id":"c99","position":"a"}])")}}));
    CHECK(r["ok"] == false);
    CHECK(r["reason"] == "unknown_component");
    CHECK(r["component_id"] == "c99");

    r = simulate(ojson::object(
        {{"doc", cylinder_circuit()},
         {"valve_schedule", ojson::parse(R"([{"t_s":0.1,"component_id":"c4","position":"a"}])")}}));
    CHECK(r["reason"] == "invalid_schedule");

    doc = cylinder_circuit();
    doc["hoses"][1]["connections"][1]["port"] = "p1";
    r = simulate(ojson::object({{"doc", doc}}));
    CHECK(r["reason"] == "unknown_port");

    doc = cylinder_circuit();
    doc["components"][3]["params"].erase("bore_mm");
    r = simulate(ojson::object({{"doc", doc}}));
    CHECK(r["reason"] == "missing_params");
    CHECK(r["missing"] == ojson::array({"c4.bore_mm"}));

    // fixed pump dead-heading a closed centre with no relief
    doc = cylinder_circuit();
    doc["components"].erase(1);
    for (auto& h : doc["hoses"]) {
        ojson keep = ojson::array();
        for (const auto& cn : h["connections"])
            if (cn["component_id"] != "c2") keep.push_back(cn);
        h["connections"] = keep;
    }
    r = simulate(ojson::object({{"doc", doc}, {"duration_s", 0.5}}));
    CHECK(r["reason"] == "pressure_runaway");

    r = simulate(ojson::object({{"doc", cylinder_circuit()}, {"duration_s", -1}}));
    CHECK(r["reason"] == "invalid_settings");
}

TEST_CASE("dispatch: hydraulic_simulate routes; the retired FMU stub is gone") {
    ojson result;
    REQUIRE(call("hydraulic_simulate",
                 ojson::object({{"doc", cylinder_circuit()}, {"duration_s", 0.2}}), result));
    CHECK(result["ok"] == true);
    CHECK(result["solver"]["steps"].get<long>() > 0);
    CHECK(!call("hydraulic_fmu_simulate", ojson::object(), result));
    REQUIRE(call("hydraulic_size_motor",
                 ojson::parse(R"({"torque_nm":100,"speed_rpm":500,"pressure_bar":200})"),
                 result));
    CHECK(result["ok"] == true);
    REQUIRE(call("pump_drive_size",
                 ojson::parse(R"({"kind":"hydraulic","flow_lpm":30,"pressure_bar":200})"),
                 result));
    CHECK(result["ok"] == true);
}

TEST_CASE("dispatch: hydraulic_templates catalog and blank defaults") {
    ojson catalog;
    REQUIRE(call("hydraulic_templates", ojson::object(), catalog));
    REQUIRE(catalog["ok"] == true);
    REQUIRE(catalog["templates"].size() == 4);
    const std::vector<std::string> ids = {"blank", "power_unit", "cylinder_circuit", "motor_circuit"};
    for (size_t i = 0; i < ids.size(); ++i) {
        const auto& entry = catalog["templates"][i];
        CHECK(entry["id"] == ids[i]);
        CHECK(!entry["name"].get<std::string>().empty());
        CHECK(!entry["description"].get<std::string>().empty());
        ojson created;
        REQUIRE(call("hydraulic_new", {{"template", ids[i]}}, created));
        REQUIRE(created["ok"] == true);
        CHECK(created["doc"]["components"].size() == entry["components"].get<size_t>());
        CHECK(created["doc"]["hoses"].size() == entry["hoses"].get<size_t>());
    }
    ojson blank;
    REQUIRE(call("hydraulic_new", ojson::object(), blank));
    REQUIRE(blank["ok"] == true);
    CHECK(blank["template"] == "blank");
    CHECK(blank["doc"]["components"].empty());
    CHECK(blank["doc"]["hoses"].empty());
    CHECK(blank["simulation"]["valve_schedule"].empty());
    CHECK(blank["simulation"]["loads"].empty());
    CHECK(blank["hint"] == "Use hydraulic_add_component to add ISO 1219 components.");
}

TEST_CASE("templates: power_unit validates and simulates") {
    check_template_simulation("power_unit");
}

TEST_CASE("templates: cylinder_circuit validates and simulates both directions") {
    check_template_simulation("cylinder_circuit");
}

TEST_CASE("templates: motor_circuit validates and simulates both directions") {
    check_template_simulation("motor_circuit");
}

TEST_CASE("dispatch: hydraulic_new refuses an unknown template") {
    ojson result;
    REQUIRE(call("hydraulic_new", {{"template", "not_a_template"}}, result));
    REQUIRE(result["ok"] == false);
    CHECK(result["reason"] == "unknown_template");
    const std::string hint = result["hint"].get<std::string>();
    for (const char* id : {"blank", "power_unit", "cylinder_circuit", "motor_circuit"})
        CHECK(hint.find(id) != std::string::npos);
    CHECK(!result.contains("doc"));
    CHECK(!result.contains("simulation"));
}

// ── BOM helpers (test_hydraulic.py) ──────────────────────────────────────────

static ojson bom_circuit() {
    ojson doc = new_hydraulic("BOM test");
    ojson c1 = new_component("c1", "pump", "Main Pump");
    c1["sourcing"]["mpn"] = "HYD-PUMP-001";
    c1["sourcing"]["manufacturer"] = "Bosch Rexroth";
    c1["sourcing"]["description"] = "Axial piston pump 18 cc/rev";
    c1["sourcing"]["category"] = "Hydraulic pump";
    doc["components"].push_back(c1);
    doc["components"].push_back(new_component("c2", "cylinder_linear", "Lift Cylinder"));
    ojson c3 = new_component("c3", "reservoir", "Tank");
    c3["sourcing"]["mpn"] = "TANK-020L";
    doc["components"].push_back(c3);
    return doc;
}

TEST_CASE("bom: lines, sourcing summary, suppressed exclusion") {
    const ojson doc = bom_circuit();
    ojson lines = hydraulic_bom_lines(doc);
    REQUIRE(lines.size() == 3);
    for (const auto& line : lines) {
        for (const char* k : {"id", "type", "name", "mpn", "manufacturer", "description",
                              "quantity", "unit", "category"})
            CHECK(line.contains(k));
    }
    const ojson pump = lines[0];
    CHECK(pump["mpn"] == "HYD-PUMP-001");
    CHECK(pump["manufacturer"] == "Bosch Rexroth");

    const ojson summary = sourcing_summary(doc);
    CHECK(summary["total"] == 3);
    CHECK(summary["sourced"] == 2);
    CHECK(summary["missing_mpn"] == ojson::array({"c2"}));
    CHECK(summary["by_type"]["pump"] == 1);
    CHECK(summary["by_type"]["cylinder_linear"] == 1);
    CHECK(summary["by_type"]["reservoir"] == 1);

    ojson sup = doc;
    sup["components"][0]["suppressed"] = true;
    lines = hydraulic_bom_lines(sup);
    CHECK(lines.size() == 2);
    for (const auto& l : lines) CHECK(l["id"] != "c1");
}

// ── line-loss estimate + hoses→piping translation ────────────────────────────

TEST_CASE("estimate_line_dp: laminar + turbulent regimes") {
    // 12.7 mm hose, 25 L/min ISO VG32: Re ≈ 1307 → laminar.
    const ojson lam = estimate_line_dp(25, 12.7, 1000);
    REQUIRE(lam["ok"] == true);
    CHECK(lam["regime"] == "laminar");
    CHECK(lam["reynolds"].get<double>() < 2300);
    // Hagen-Poiseuille: ΔP = 128·μ·L·Q/(π·D⁴)
    const double hand =
        128 * 0.0278 * 1.0 * (25 / 60000.0) / (M_PI * std::pow(0.0127, 4)) / 1000.0;
    CHECK(std::abs(lam["dp_kpa"].get<double>() - hand) < 0.01);
    // thin water at high flow → turbulent (Swamee-Jain)
    const ojson turb = estimate_line_dp(200, 25, 5000, 998.2, 1.002e-3);
    CHECK(turb["regime"] == "turbulent (Swamee-Jain)");
    CHECK(estimate_line_dp(0, 12.7, 1000)["ok"] == false);
    CHECK(estimate_line_dp(25, 12.7, 1000,
                           std::numeric_limits<double>::quiet_NaN(), 0.0278)["ok"] == false);
    CHECK(estimate_line_dp(std::numeric_limits<double>::max(), 12.7, 1000)["ok"] ==
          false);
}

// ── dispatch envelopes ───────────────────────────────────────────────────────

TEST_CASE("dispatch: hydraulic_new / add_component / add_hose / bom envelopes") {
    ojson result;
    REQUIRE(call("hydraulic_new", ojson::object({{"name", "Press"}}), result));
    REQUIRE(result["ok"] == true);
    CHECK(result["doc"]["name"] == "Press");
    CHECK(result["hint"] == "Use hydraulic_add_component to add ISO 1219 components.");

    ojson doc = result["doc"];
    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"doc", doc},
                                {"type", "pump"},
                                {"name", "Main Pump"},
                                {"params", ojson::parse(R"({"displacement_cc_rev": 18})")},
                                {"sourcing", ojson::parse(R"({"mpn": "A10VSO18DFR"})")}}),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result["component_id"] == "c1");
    CHECK(result["doc_component_count"] == 1);
    CHECK(result["warnings"].empty());
    CHECK(result["hint"].is_null());
    doc = result["doc"];

    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"doc", doc}, {"type", "cylinder_linear"}}), result));
    doc = result["doc"];
    CHECK(result["component_id"] == "c2");

    // unknown component type -> contract-shaped with the valid-types hint
    ojson bad;
    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"doc", doc}, {"type", "rocket_engine"}}), bad));
    CHECK(bad["ok"] == false);
    CHECK(bad["reason"] == "unknown component type 'rocket_engine'");
    CHECK(bad["hint"].get<std::string>().find("'accumulator'") != std::string::npos);

    // no doc -> session-less core reports it like the MCP layer would
    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"type", "pump"}}), bad));
    CHECK(bad["reason"] == "no active hydraulic circuit");

    REQUIRE(call("hydraulic_add_hose",
                 ojson::object(
                     {{"doc", doc},
                      {"net_name", "PRESSURE_LINE"},
                      {"connections", ojson::parse(R"([{"component_id":"c1","port":"p1"},
                                                       {"component_id":"c2","port":"p1"}])")}}),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result["hose_id"] == "h1");
    CHECK(result["doc_hose_count"] == 1);
    doc = result["doc"];

    REQUIRE(call("hydraulic_add_hose",
                 ojson::object(
                     {{"doc", doc},
                      {"net_name", "BAD"},
                      {"connections", ojson::parse(R"([{"component_id":"c999","port":"p1"},
                                                       {"component_id":"c2","port":"p2"}])")}}),
                 bad));
    CHECK(bad["ok"] == false);

    REQUIRE(call("hydraulic_bom", ojson::object({{"doc", doc}}), result));
    REQUIRE(result["ok"] == true);
    CHECK(result["bom_lines"].size() == 2);
    CHECK(result["summary"]["sourced"] == 1);
    CHECK(result["warnings"] == ojson::array({"component c2 has no MPN"}));
    CHECK(result["hint"] == "1 component(s) missing MPN — add to sourcing fields.");
}

TEST_CASE("dispatch: size_cylinder / size_pump tool wrappers add hint + warnings") {
    ojson result;
    REQUIRE(call("hydraulic_size_cylinder",
                 ojson::parse(R"({"bore_mm":50,"rod_mm":0,"pressure_bar":200,
                                   "stroke_mm":100,"efficiency":1.0})"),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result.contains("warnings"));
    CHECK(result["hint"].get<std::string>().find("Pump must supply >= ") == 0);
    CHECK(result["hint"].get<std::string>().find(" at 200.0 bar.") != std::string::npos);
    REQUIRE(call("hydraulic_size_cylinder",
                 ojson::parse(R"({"bore_mm":0,"pressure_bar":100,"stroke_mm":100})"),
                 result));
    CHECK(result["ok"] == false);  // solver errors pass through unwrapped

    REQUIRE(call("hydraulic_size_pump",
                 ojson::parse(R"({"flow_lpm":10,"pressure_bar":200,"efficiency":0.85})"),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result["hint"].get<std::string>().find("Select a pump with >= ") == 0);

    CHECK(!call("piping_solve", ojson::object(), result));  // not ours
}

TEST_CASE("dispatch: hydraulic→piping handoff runs the live solve + verify") {
    // Pump -> cylinder circuit, one pressure-line hose.
    ojson result;
    REQUIRE(call("hydraulic_new", ojson::object({{"name", "Press"}}), result));
    ojson doc = result["doc"];
    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"doc", doc}, {"type", "pump"}}), result));
    doc = result["doc"];
    REQUIRE(call("hydraulic_add_component",
                 ojson::object({{"doc", doc}, {"type", "cylinder_linear"}}), result));
    doc = result["doc"];
    REQUIRE(call("hydraulic_add_hose",
                 ojson::object(
                     {{"doc", doc},
                      {"net_name", "PRESSURE_LINE"},
                      {"connections", ojson::parse(R"([{"component_id":"c1","port":"p1"},
                                                       {"component_id":"c2","port":"p1"}])")}}),
                 result));
    doc = result["doc"];

    REQUIRE(call("hydraulic_piping_handoff",
                 ojson::object({{"doc", doc},
                                {"supply_pressure_bar", 210.0},
                                {"flow_lpm", 25.0},
                                {"load_pressure_bar", 190.0}}),
                 result));
    REQUIRE(result["ok"] == true);
    CHECK(result["stage"] == "solved");
    REQUIRE(result.contains("simhandoff"));
    const ojson env = result["simhandoff"];
    CHECK(env["coupling"] == "hydraulic_to_piping");
    CHECK(env["kind"] == "handoff");
    CHECK(chudware::piping::simhandoff::validate_envelope(env).empty());
    // one hose line: inline piping doc + boundary + estimate + solve agree
    CHECK(result["lines"].size() == 1);
    CHECK(result["piping_doc"]["runs"].size() == 1);
    CHECK(result["solve"]["converged"] == true);
    CHECK(result["verify"]["pass"] == true);  // laminar: estimate == solve
    REQUIRE(result["margin"]["line_loss_solved_bar"].is_null());
    REQUIRE(result["margin"]["per_line"].contains("r1"));
    CHECK(result["margin"]["per_line"]["r1"]["pump_margin_solved_bar"].get<double>() ==
          doctest::Approx(20.0).epsilon(0.02));
    CHECK(result["margin"]["per_line"]["r1"]["pump_margin_solved_pct"].get<double>() ==
          doctest::Approx(9.52).epsilon(0.01));

    // verify step consumes the handoff + solve and rebuilds the envelope
    ojson v;
    REQUIRE(call("hydraulic_verify_piping",
                 ojson::object({{"handoff", env},
                                {"solve_result", result["solve"]},
                                {"supply_pressure_bar", 210.0},
                                {"load_pressure_bar", 190.0}}),
                 v));
    REQUIRE(v["ok"] == true);
    CHECK(v["pass"] == true);
    CHECK(v["checks"].size() == 1);
    CHECK(v["simhandoff"]["kind"] == "verify");
    CHECK(v["simhandoff"]["coupling"] == "hydraulic_to_piping");
    CHECK(chudware::piping::simhandoff::validate_envelope(v["simhandoff"]).empty());
    CHECK(v["margin"]["per_line"]["r1"]["pump_margin_solved_bar"] ==
          result["margin"]["per_line"]["r1"]["pump_margin_solved_bar"]);

    // The generated hose runs are independent cases.  Choose headroom between
    // one line loss and the sum of two identical line losses: both real
    // per-line margins remain positive while the old fabricated series margin
    // would be negative.
    ojson add_second;
    REQUIRE(call("hydraulic_add_hose",
                 ojson::object(
                     {{"doc", doc},
                      {"net_name", "RETURN_LINE"},
                      {"connections", ojson::parse(R"([{"component_id":"c1","port":"p2"},
                                                       {"component_id":"c2","port":"p2"}])")}}),
                 add_second));
    ojson two_hose;
    REQUIRE(call("hydraulic_piping_handoff",
                 ojson::object({{"doc", add_second["doc"]},
                                {"supply_pressure_bar", 210.0},
                                {"flow_lpm", 25.0},
                                {"load_pressure_bar", 209.7}}),
                 two_hose));
    REQUIRE(two_hose["ok"] == true);
    REQUIRE(two_hose["margin"]["per_line"].size() == 2);
    CHECK(two_hose["margin"]["line_loss_solved_bar"].is_null());
    CHECK(two_hose["margin"]["pump_margin_solved_bar"].is_null());
    double independent_loss_sum_bar = 0.0;
    for (auto it = two_hose["margin"]["per_line"].begin();
         it != two_hose["margin"]["per_line"].end(); ++it) {
        independent_loss_sum_bar += it->at("line_loss_solved_bar").get<double>();
        CHECK(it->at("pump_margin_solved_bar").get<double>() > 0.0);
    }
    CHECK(210.0 - 209.7 - independent_loss_sum_bar < 0.0);

    ojson wrong_link = result["solve"];
    wrong_link["links"][0]["id"] = "unrelated_run";
    const auto [missing_env, missing_checks] =
        verify_piping_envelope(env, wrong_link, 30.0);
    REQUIRE(missing_checks.size() == 1);
    CHECK(missing_checks[0]["pass"] == false);
    CHECK(missing_env["pass"] == false);

    ojson not_converged = result["solve"];
    not_converged["converged"] = false;
    REQUIRE(call("hydraulic_verify_piping",
                 ojson::object({{"handoff", env}, {"solve_result", not_converged}}), v));
    CHECK(v["ok"] == false);

    ojson failed_handoff;
    REQUIRE(call(
        "hydraulic_piping_handoff",
        ojson::object(
            {{"doc", doc},
             {"supply_pressure_bar", 210.0},
             {"flow_lpm", 25.0},
             {"fluid", ojson::parse(
                           R"({"name":"air","T":293.15,"P":101325})")}}),
        failed_handoff));
    CHECK(failed_handoff["ok"] == false);
    CHECK(failed_handoff["stage"] == "solve_failed");
    CHECK(failed_handoff["solve"]["ok"] == false);

    // no hoses -> contract-shaped
    ojson empty_doc;
    REQUIRE(call("hydraulic_new", ojson::object(), empty_doc));
    REQUIRE(call("hydraulic_piping_handoff",
                 ojson::object({{"doc", empty_doc["doc"]},
                                {"supply_pressure_bar", 210.0},
                                {"flow_lpm", 25.0}}),
                 v));
    CHECK(v["ok"] == false);
    CHECK(v["reason"] == "hydraulic circuit has no hoses");
}

TEST_CASE("dispatch: hydraulic→FEA handoff + verify envelopes") {
    ojson result;
    REQUIRE(call("hydraulic_fea_handoff",
                 ojson::parse(R"({"bore_mm":80,"rod_mm":56,"max_pressure_bar":250})"),
                 result));
    REQUIRE(result["ok"] == true);
    // π/4·0.08²·250e5 ≈ 125 663.7 N extend
    CHECK(result["extend_force_n"].get<double>() ==
          doctest::Approx(125663.7).epsilon(1e-4));
    CHECK(result["cases"] ==
          ojson::array({"extend_max_pressure", "retract_max_pressure"}));
    CHECK(result["fea_args"]["extend_max_pressure"]["loads"][0]["force"].size() == 3);
    const ojson env = result["simhandoff"];
    CHECK(env["coupling"] == "hydraulic_to_fea");
    CHECK(chudware::piping::simhandoff::validate_envelope(env).empty());
    CHECK(env["conditions"].size() == 3);  // extend + retract + fixture

    // verify: the envelope's own conditions match the independent recompute
    ojson v;
    REQUIRE(call("hydraulic_verify_fea", ojson::object({{"handoff", env}}), v));
    REQUIRE(v["ok"] == true);
    CHECK(v["pass"] == true);
    CHECK(v["checks"].size() == 2);
    CHECK(v["simhandoff"]["kind"] == "verify");
    CHECK(chudware::piping::simhandoff::validate_envelope(v["simhandoff"]).empty());

    // a tampered condition (off-axis force) fails magnitude + flags colinearity
    ojson tampered = env;
    tampered["conditions"][0]["value"] = ojson::array({1000.0, 0.0, 0.0});
    REQUIRE(call("hydraulic_verify_fea", ojson::object({{"handoff", tampered}}), v));
    CHECK(v["pass"] == false);

    ojson missing_case = env;
    missing_case["conditions"].erase(1);
    CHECK(verify_fea_envelope(missing_case, 0.5)["pass"] == false);

    ojson duplicate_case = env;
    duplicate_case["conditions"].push_back(duplicate_case["conditions"][0]);
    CHECK(verify_fea_envelope(duplicate_case, 0.5)["pass"] == false);

    ojson missing_axis = env;
    missing_axis["meta"].erase("axis");
    CHECK(verify_fea_envelope(missing_axis, 0.5)["pass"] == false);

    // guards
    REQUIRE(call("hydraulic_fea_handoff",
                 ojson::parse(R"({"bore_mm":50,"rod_mm":50,"max_pressure_bar":250})"),
                 v));
    CHECK(v["ok"] == false);
    REQUIRE(call("hydraulic_fea_handoff",
                 ojson::parse(R"({"bore_mm":50,"max_pressure_bar":250,"axis":[0,0,0]})"),
                 v));
    CHECK(v["ok"] == false);
    CHECK(v["hint"] == "axis must be a non-zero [x,y,z] vector");
    REQUIRE(call("hydraulic_fea_handoff",
                 ojson::parse(R"({"bore_mm":50,"max_pressure_bar":250,
                                   "load_face":"same","fixture_face":"same"})"),
                 v));
    CHECK(v["ok"] == false);
    REQUIRE(call("hydraulic_fea_handoff",
                 ojson::parse(R"({"bore_mm":50,"max_pressure_bar":250,
                                   "target_doc_id":42})"),
                 v));
    CHECK(v["ok"] == false);
    REQUIRE(call("hydraulic_verify_fea", ojson::object(), v));
    CHECK(v["reason"] == "no hydraulic→FEA handoff envelope");
}
