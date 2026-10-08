// core/hydraulic/templates.cpp — one template source for desktop and agent callers.
#include "hydraulic/templates.h"

#include <initializer_list>
#include <utility>

namespace chudware::hydraulic {
namespace {

enum class Template { Blank, PowerUnit, Cylinder, Motor };
struct TemplateInfo {
    Template kind;
    const char* id;
    const char* name;
    const char* description;
    int components;
    int hoses;
};
constexpr TemplateInfo kTemplates[] = {
    {Template::Blank, "blank", "Blank circuit",
     "An empty ISO 1219 circuit for a custom design.", 0, 0},
    {Template::PowerUnit, "power_unit", "Hydraulic power unit",
     "A 16 cc/rev gear pump, 60 L tank, 160 bar relief and return filter, fully connected.", 4, 4},
    {Template::Cylinder, "cylinder_circuit", "Cylinder circuit",
     "A filtered power unit and 4/3 tandem-centre valve driving a 50/28 mm x 300 mm cylinder.", 6, 6},
    {Template::Motor, "motor_circuit", "Motor circuit",
     "A filtered power unit and 4/3 tandem-centre valve driving an 80 cc/rev hydraulic motor.", 6, 6},
};

using Ends = std::initializer_list<std::pair<const char*, const char*>>;

void component(ojson& doc, const char* id, const char* type, const char* name,
               double x, double y, const ojson& params, Ends ports) {
    ojson comp = new_component(id, type, name, x, y, params);
    for (const auto& [port, port_type] : ports)
        comp["ports"].push_back({{"id", port}, {"port_type", port_type}});
    doc["components"].push_back(std::move(comp));
}

void hose(ojson& doc, const char* id, const char* net_name, Ends ends,
          double length_mm, double id_mm, double rating_bar) {
    ojson connections = ojson::array();
    for (const auto& [cid, port] : ends)
        connections.push_back({{"component_id", cid}, {"port", port}});
    ojson h = new_hose(id, net_name, connections);
    h["length_mm"] = length_mm;
    h["id_mm"] = id_mm;
    h["rating_bar"] = rating_bar;
    doc["hoses"].push_back(std::move(h));
}

void power_unit(ojson& doc, bool actuator) {
    component(doc, "c1", "reservoir", "Tank", 0.0, -220.0,
              {{"volume_l", 60.0}, {"pressure_bar", 0.0}}, {{"T", "tank"}, {"S", "tank"}});
    component(doc, "c2", "pump", "Gear pump", 0.0, 0.0,
              {{"pump_type", "gear"}, {"displacement_cc_rev", 16.0}, {"speed_rpm", 1450.0},
               {"vol_efficiency", 0.95}, {"mech_efficiency", 0.9}, {"max_pressure_bar", 250.0}},
              {{"P", "pressure"}, {"T", "tank"}});
    component(doc, "c3", "valve_relief", "160 bar relief", 220.0, 0.0,
              {{"set_pressure_bar", 160.0}, {"override_bar", 10.0}, {"rated_flow_lpm", 30.0},
               {"max_pressure_bar", 250.0}}, {{"P", "pressure"}, {"T", "tank"}});
    component(doc, "c4", "filter", "Return filter", 220.0, -140.0,
              {{"rated_flow_lpm", 40.0}, {"rated_dp_bar", 1.0}, {"max_pressure_bar", 40.0}},
              {{"IN", "generic"}, {"OUT", "generic"}});
    if (actuator) {
        hose(doc, "h1", "Pressure", {{"c2", "P"}, {"c3", "P"}, {"c5", "P"}}, 800.0, 12.7, 280.0);
        hose(doc, "h2", "Return", {{"c3", "T"}, {"c5", "T"}, {"c4", "IN"}}, 1200.0, 19.0, 40.0);
    } else {
        hose(doc, "h1", "Pressure", {{"c2", "P"}, {"c3", "P"}}, 800.0, 12.7, 280.0);
        hose(doc, "h2", "Return", {{"c3", "T"}, {"c4", "IN"}}, 1200.0, 19.0, 40.0);
    }
    hose(doc, "h3", "Filtered return", {{"c4", "OUT"}, {"c1", "T"}}, 600.0, 19.0, 40.0);
    hose(doc, "h4", "Suction", {{"c1", "S"}, {"c2", "T"}}, 400.0, 25.4, 10.0);
}

}  // namespace

ojson hydraulic_templates() {
    ojson entries = ojson::array();
    for (const auto& t : kTemplates)
        entries.push_back({{"id", t.id}, {"name", t.name}, {"description", t.description},
                           {"components", t.components}, {"hoses", t.hoses}});
    return {{"ok", true}, {"templates", std::move(entries)}};
}

ojson new_from_template(const std::string& name, const std::string& template_id) {
    const TemplateInfo* info = nullptr;
    for (const auto& t : kTemplates) {
        if (template_id == t.id) {
            info = &t;
            break;
        }
    }
    if (!info) {
        std::string hint = "Valid templates: ";
        for (const auto& t : kTemplates) {
            if (t.kind != Template::Blank) hint += ", ";
            hint += t.id;
        }
        hint += ".";
        return pyutil::jerr("unknown_template", hint);
    }

    ojson doc = new_hydraulic(name);
    ojson simulation = {{"duration_s", 5.0}, {"output_dt_s", 0.01}, {"max_step_s", 0.01},
                        {"valve_schedule", ojson::array()}, {"loads", ojson::object()},
                        {"fluid", {{"density_kgm3", 870.0}, {"bulk_modulus_bar", 15000.0},
                                   {"viscosity_cst", 32.0}, {"air_fraction", 0.002}}},
                        {"initial", ojson::object()}};
    const bool actuator = info->kind == Template::Cylinder || info->kind == Template::Motor;
    if (info->kind != Template::Blank) {
        power_unit(doc, actuator);
        simulation["duration_s"] = actuator ? 4.0 : 1.0;
    }
    if (actuator) {
        component(doc, "c5", "valve_directional", "DCV 4/3", 0.0, 180.0,
                  {{"ways", 4}, {"positions", 3}, {"center_condition", "tandem"},
                   {"actuator", "solenoid"}, {"response_ms", 100.0},
                   {"rated_flow_lpm", info->kind == Template::Cylinder ? 10.0 : 8.0},
                   {"rated_dp_bar", 5.0}, {"max_pressure_bar", 250.0}, {"initial_position", "a"}},
                  {{"P", "pressure"}, {"T", "tank"}, {"A", "work_a"}, {"B", "work_b"}});
        simulation["valve_schedule"] = ojson::array(
            {{{"t_s", 0.0}, {"component_id", "c5"}, {"position", "a"}},
             {{"t_s", 2.0}, {"component_id", "c5"}, {"position", "b"}},
             {{"t_s", 2.5}, {"component_id", "c5"},
              {"position", info->kind == Template::Cylinder ? "center" : "a"}}});
        if (info->kind == Template::Cylinder) {
            // Meter-out capacity controls the gravity-assisted return. A partial
            // retract then hold retains useful lifted work under the constant load.
            component(doc, "c6", "cylinder_linear", "Lift cylinder", 0.0, 360.0,
                      {{"actuator_type", "double_acting"}, {"bore_mm", 50.0}, {"rod_mm", 28.0},
                       {"stroke_mm", 300.0}, {"piston_mass_kg", 5.0}, {"max_pressure_bar", 250.0}},
                      {{"A", "work_a"}, {"B", "work_b"}});
            hose(doc, "h5", "Cylinder A", {{"c5", "A"}, {"c6", "A"}}, 1500.0, 12.7, 280.0);
            hose(doc, "h6", "Cylinder B", {{"c5", "B"}, {"c6", "B"}}, 1500.0, 12.7, 280.0);
            simulation["loads"]["c6"] = {{"force_n", 10000.0}, {"mass_kg", 50.0},
                                            {"viscous_ns_m", 2000.0}};
            simulation["initial"]["c6"] = {{"position_mm", 0.0}};
            constexpr double initial_pressure_bar =
                10000.0 / (0.25 * 3.14159265358979323846 * 0.05 * 0.05) / 1e5;
            simulation["initial"]["h1"] = {{"pressure_bar", initial_pressure_bar}};
            simulation["initial"]["h5"] = {{"pressure_bar", initial_pressure_bar}};
        } else {
            component(doc, "c6", "motor_hydraulic", "Bidirectional motor", 0.0, 360.0,
                      {{"motor_type", "gear"}, {"displacement_cc_rev", 80.0},
                       {"vol_efficiency", 0.95}, {"mech_efficiency", 0.9}, {"inertia_kgm2", 0.005},
                       {"max_speed_rpm", 2000.0}, {"max_pressure_bar", 250.0}},
                      {{"A", "work_a"}, {"B", "work_b"}});
            hose(doc, "h5", "Motor A", {{"c5", "A"}, {"c6", "A"}}, 1500.0, 12.7, 280.0);
            hose(doc, "h6", "Motor B", {{"c5", "B"}, {"c6", "B"}}, 1500.0, 12.7, 280.0);
            simulation["loads"]["c6"] = {{"torque_nm", 50.0}, {"inertia_kgm2", 0.01},
                                            {"viscous_nms", 0.1}};
            simulation["initial"]["c6"] = {{"speed_rpm", 0.0}};
            // Load-balanced pressures avoid an artificial backwards start; the
            // metered return keeps the constant torque from overrunning in reverse.
            constexpr double initial_pressure_bar =
                50.0 * 2.0 * 3.14159265358979323846 / (80.0 * 1e-6) / 1e5;
            simulation["initial"]["h1"] = {{"pressure_bar", initial_pressure_bar}};
            simulation["initial"]["h5"] = {{"pressure_bar", initial_pressure_bar}};
        }
    }
    ojson out = {{"ok", true}, {"doc", std::move(doc)}, {"template", template_id},
                 {"simulation", std::move(simulation)}, {"warnings", ojson::array()}};
    if (info->kind == Template::Blank) {
        out["hint"] = "Use hydraulic_add_component to add ISO 1219 components.";
        out["next_actions"] = ojson::array(
            {"call hydraulic_add_component to add pumps, cylinders, valves, etc.",
             "call hydraulic_size_cylinder / hydraulic_size_pump to size components"});
    } else {
        out["hint"] = "Pass doc and the suggested simulation setup to hydraulic_simulate.";
        out["next_actions"] = ojson::array(
            {"adapt component parameters and suggested loads to the real duty",
             "call hydraulic_simulate with doc and simulation to inspect pressures and motion",
             "call hydraulic_bom to source the circuit components"});
    }
    return out;
}

}  // namespace chudware::hydraulic
