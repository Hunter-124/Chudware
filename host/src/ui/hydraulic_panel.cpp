// HydraulicPanel implementation — see hydraulic_panel.h.
#include "hydraulic_panel.h"
#include "sim/hydraulic_internal.h"

#include <algorithm>
#include <cfloat>
#include <cmath>
#include <cstdlib>
#include <iterator>
#include <string_view>
#include <utility>

#include "imgui.h"

#include "../core/app_state.h"

namespace ui {
using namespace hydint;
namespace {

// ---- ISO 1219 component types ------------------------------------------------------------------
// The parameter keys are the ones hydraulic_simulate / the sizing tools read (contracts/
// hydraulic_schema.md). `optional` keys are omitted (field left empty) so the engine applies its
// default. Labels carry no unit: the unit is the field's suffix.

ParamDef num(const char* key, const char* label, const char* unit, double def, const char* tip = nullptr) {
    return {key, label, unit, def, {}, false, false, false, tip};
}
ParamDef opt(const char* key, const char* label, const char* unit, double def, const char* tip = nullptr) {
    return {key, label, unit, def, {}, false, true, false, tip};
}
ParamDef ratio(const char* key, const char* label, double def, const char* tip = nullptr) {
    return {key, label, nullptr, def, {}, false, false, true, tip};
}
ParamDef choice(const char* key, const char* label, std::vector<const char*> choices, bool intChoice = false,
                const char* tip = nullptr) {
    return {key, label, nullptr, 0.0, std::move(choices), intChoice, false, false, tip};
}

}  // namespace

namespace hydint {

const std::vector<TypeDef>& typeDefs() {
    static const char* const kKv = "m\xc2\xb3/h";
    static const std::vector<TypeDef> defs = {
        {"pump", "Pump",
         {choice("pump_type", "Pump type", {"gear", "vane", "piston_axial", "piston_radial", "screw"}),
          num("displacement_cc_rev", "Displacement", "cc/rev", 16.0, "Geometric displacement per shaft revolution."),
          num("speed_rpm", "Speed", "rpm", 1450.0, "Drive speed (electric motor or engine)."),
          ratio("vol_efficiency", "Volumetric efficiency", 0.95),
          ratio("mech_efficiency", "Mechanical efficiency", 0.9),
          opt("max_pressure_bar", "Rated pressure", "bar", 210.0, "Catalog rating; informational."),
          opt("compensator_bar", "Compensator", "bar", 0.0,
              "Pressure-compensator setting: the pump destrokes above it. Empty = fixed displacement."),
          opt("compensator_band_bar", "Compensator band", "bar", 0.0,
              "Pressure band over which the compensator destrokes fully. Empty = engine default.")}},
        {"motor_hydraulic", "Hydraulic motor",
         {num("displacement_cc_rev", "Displacement", "cc/rev", 100.0),
          ratio("vol_efficiency", "Volumetric efficiency", 0.95),
          ratio("mech_efficiency", "Mechanical efficiency", 0.9),
          num("inertia_kgm2", "Rotor inertia", "kg m2", 0.002,
              "The motor's own rotor; the driven load's inertia goes in Simulate > Loads."),
          opt("friction_nm", "Friction torque", "N m", 0.0),
          opt("max_speed_rpm", "Max speed", "rpm", 0.0, "Catalog rating; informational."),
          opt("max_pressure_bar", "Rated pressure", "bar", 0.0, "Catalog rating; informational.")}},
        {"cylinder_linear", "Cylinder",
         {choice("actuator_type", "Acting", {"double_acting", "single_acting"}),
          num("bore_mm", "Bore", "mm", 63.0),
          num("rod_mm", "Rod", "mm", 36.0, "0 for single-acting / rodless."),
          num("stroke_mm", "Stroke", "mm", 300.0),
          num("piston_mass_kg", "Piston + rod mass", "kg", 5.0,
              "Moving mass of the cylinder itself; the driven load's mass goes in Simulate > Loads."),
          opt("friction_n", "Seal friction", "N", 0.0, "Coulomb friction of the seals."),
          opt("viscous_ns_m", "Viscous friction", "N\xc2\xb7s/m", 0.0)}},
        {"cylinder_rotary", "Rotary actuator",
         {choice("actuator_type", "Acting", {"double_acting", "single_acting"}),
          num("displacement_cc_rev", "Displacement", "cc/rev", 50.0),
          num("max_angle_deg", "Rotation range", "\xc2\xb0", 180.0)}},
        {"valve_directional", "Directional valve",
         {choice("ways", "Ways", {"4", "3", "2"}, true, "4-way: P, T, A, B. 3-way: P, A, T. 2-way: P, A."),
          choice("positions", "Positions", {"3", "2"}, true),
          choice("center_condition", "Center", {"closed", "tandem", "open", "float"}, false,
                 "Closed: all blocked. Tandem: P to T, A and B blocked. Open: all to T. Float: A and B to T."),
          choice("actuator", "Actuation", {"solenoid", "manual", "pilot", "spring"}),
          opt("Kv", "Kv", kKv, 0.0,
              "Flow capacity per spool path at a 1 bar drop. Empty = rated flow at the rated drop below "
              "(engine default: pump flow at 5 bar)."),
          opt("rated_flow_lpm", "Rated flow", "L/min", 0.0),
          opt("rated_dp_bar", "Rated drop", "bar", 0.0),
          num("response_ms", "Spool response", "ms", 30.0)}},
        {"valve_relief", "Relief valve",
         {num("set_pressure_bar", "Cracking pressure", "bar", 160.0),
          opt("override_bar", "Override", "bar", 0.0,
              "Pressure rise from cracking to full flow. Empty = 10 % of the setting."),
          opt("rated_flow_lpm", "Rated flow", "L/min", 0.0, "Empty = total pump flow.")}},
        {"valve_check", "Check valve",
         {num("cracking_pressure_bar", "Cracking pressure", "bar", 0.5),
          opt("Kv", "Kv", kKv, 0.0, "Flow capacity at a 1 bar drop. Empty = engine default capacity.")}},
        {"valve_flow_control", "Flow control valve",
         {opt("set_flow_lpm", "Set flow", "L/min", 10.0,
              "Pressure-compensated setting. Empty = use Kv as a plain throttle."),
          opt("compensator_dp_bar", "Compensator drop", "bar", 0.0),
          opt("Kv", "Kv", kKv, 0.0, "Flow capacity at a 1 bar drop.")}},
        {"accumulator", "Accumulator",
         {choice("style", "Style", {"bladder", "piston", "diaphragm"}),
          num("volume_l", "Gas volume", "L", 4.0),
          num("pre_charge_bar", "Pre-charge", "bar", 90.0, "Nitrogen pre-charge."),
          num("polytropic_n", "Polytropic index", nullptr, 1.4, "1.0 isothermal, 1.4 adiabatic.")}},
        {"reservoir", "Reservoir",
         {opt("volume_l", "Volume", "L", 100.0),
          opt("pressure_bar", "Tank pressure", "bar", 0.0, "Gauge pressure in the tank.")}},
        {"filter", "Filter",
         {opt("rated_flow_lpm", "Rated flow", "L/min", 0.0, "Empty = total pump flow."),
          num("rated_dp_bar", "Drop at rated flow", "bar", 1.0)}},
        {"heat_exchanger", "Heat exchanger",
         {opt("rated_flow_lpm", "Rated flow", "L/min", 0.0, "Empty = total pump flow."),
          num("rated_dp_bar", "Drop at rated flow", "bar", 0.5)}},
    };
    return defs;
}

const TypeDef* typeDef(const std::string& type) {
    for (const auto& d : typeDefs())
        if (type == d.type) return &d;
    return nullptr;
}
std::string typeLabel(const std::string& type) {
    const TypeDef* d = typeDef(type);
    return d ? d->label : type;
}

json defaultParams(const TypeDef& d) {
    json p = json::object();
    for (const auto& pd : d.params) {
        if (!pd.choices.empty()) {
            if (pd.intChoice)
                p[pd.key] = std::atoi(pd.choices[0]);
            else
                p[pd.key] = pd.choices[0];
        } else if (!pd.optional || pd.def != 0.0) {
            p[pd.key] = pd.def;
        }
    }
    return p;
}

bool validComponentParams(const std::string& type, const json& params, std::string& error) {
    if (!params.is_object()) {
        error = "Component parameters must be a JSON object.";
        return false;
    }
    const TypeDef* def = typeDef(type);
    if (!def) return true; // native engine owns capability validation for unmodeled types
    for (const auto& pd : def->params) {
        if (!params.contains(pd.key)) continue;
        const json& v = params[pd.key];
        const bool valid = (pd.optional && v.is_null()) ||
                           (pd.choices.empty() ? v.is_number() && std::isfinite(v.get<double>())
                            : pd.intChoice ? v.is_number_integer() : v.is_string());
        if (!valid) {
            error = std::string(pd.key) + " has an invalid native parameter type.";
            return false;
        }
    }
    return true;
}

// Port ids hydraulic_simulate resolves per type (case-insensitive on the engine side).
std::vector<std::string> portsOf(const json& comp) {
    const std::string type = jstr(comp, "type");
    std::vector<std::string> declared;
    for (const auto& p : jarr(comp, "ports")) {
        const std::string id = jstr(p, "id");
        if (!id.empty() && std::find(declared.begin(), declared.end(), id) == declared.end()) declared.push_back(id);
    }
    if (!declared.empty()) return declared;
    if (type == "pump") return {"P", "T", "L"};
    if (type == "motor_hydraulic" || type == "cylinder_rotary") return {"A", "B", "L"};
    if (type == "cylinder_linear") {
        if (jstr(jobj(comp, "params"), "actuator_type") == "single_acting") return {"A"};
        return {"A", "B"};
    }
    if (type == "valve_directional") {
        const int ways = (int)jnum(jobj(comp, "params"), "ways", 4.0);
        if (ways == 2) return {"P", "A"};
        if (ways == 3) return {"P", "A", "T"};
        return {"P", "T", "A", "B"};
    }
    if (type == "valve_relief") return {"P", "T"};
    if (type == "accumulator") return {"P"};
    if (type == "reservoir") return {"T", "T2", "T3", "S"};
    return {"IN", "OUT"};
}
// Ports the simulator does not require to be connected.
bool portOptional(const std::string& type, const std::string& port) {
    if (port == "L") return true;
    if (type == "reservoir") return true;
    return false;
}

std::vector<std::string> openRequiredPorts(const json& circuit, const json& component) {
    std::vector<std::string> open;
    const std::string id = jstr(component, "id"), type = jstr(component, "type");
    for (const auto& port : portsOf(component)) {
        if (portOptional(type, port)) continue;
        bool connected = false;
        for (const auto& hose : jarr(circuit, "hoses"))
            for (const auto& end : jarr(hose, "connections"))
                connected |= jstr(end, "component_id") == id && jstr(end, "port") == port;
        if (!connected) open.push_back(port);
    }
    return open;
}

std::string portMeaning(const std::string& type, const std::string& port) {
    if (port == "P") return "pressure";
    if (port == "T" || port == "T2" || port == "T3") return "tank";
    if (port == "S") return "suction";
    if (port == "L") return "case drain";
    if (type == "cylinder_linear" && port == "A") return "cap end";
    if (type == "cylinder_linear" && port == "B") return "rod end";
    if (port == "A" || port == "B") return "work port";
    return port == "IN" ? "inlet" : port == "OUT" ? "outlet" : "port";
}

}  // namespace hydint

namespace {

const char* const kMotorTypeKeys[] = {"", "gear", "vane", "piston", "orbital"};
// Keep at most `maxN` evenly spaced samples (first and last exact) of every array of length n.
json pickSamples(const json& arr, const std::vector<std::size_t>& idx) {
    json out = json::array();
    for (std::size_t i : idx) out.push_back(arr[i]);
    return out;
}
void downsampleResult(json& r, int maxN) {
    const json& t = jarr(r, "time_s");
    const std::size_t n = t.size();
    if (n <= (std::size_t)maxN || maxN < 2) return;
    std::vector<std::size_t> idx;
    idx.reserve(maxN);
    for (int k = 0; k < maxN; ++k)
        idx.push_back((std::size_t)std::llround((double)k * (double)(n - 1) / (double)(maxN - 1)));
    auto squeeze = [&](json& holder) {
        if (!holder.is_object()) return;
        for (auto it = holder.begin(); it != holder.end(); ++it)
            if (it->is_array() && it->size() == n) *it = pickSamples(*it, idx);
    };
    r["time_s"] = pickSamples(r["time_s"], idx);
    if (r.contains("nodes") && r["nodes"].is_object())
        for (auto it = r["nodes"].begin(); it != r["nodes"].end(); ++it) squeeze(*it);
    if (r.contains("components") && r["components"].is_object())
        for (auto it = r["components"].begin(); it != r["components"].end(); ++it)
            if (it->is_object() && it->contains("series")) squeeze((*it)["series"]);
    r["samples_stored"] = maxN;
    r["samples_full"] = (int)n;
}

const char* recordIcon() { return "study_hydraulics"; }

std::string recordSummary(const json& rec) {
    const json& circuit = jobj(jobj(rec, "params"), "circuit");
    const json& results = jobj(rec, "results");
    const std::size_t nc = jarr(circuit, "components").size();
    const std::size_t nh = jarr(circuit, "hoses").size();
    std::string s = std::to_string(nc) + (nc == 1 ? " component" : " components") + " \xc2\xb7 " +
                    std::to_string(nh) + (nh == 1 ? " hose" : " hoses");
    const json& sim = jobj(results, "simulate");
    if (!sim.empty()) s += " \xc2\xb7 peak " + qty(jobj(sim, "summary"), "peak_pressure_bar", "bar");
    if (results.contains("bom")) s += " \xc2\xb7 BOM";
    if (results.contains("piping_handoff")) s += " \xc2\xb7 piping";
    return s;
}

}  // namespace

// ============================================================================================
// construction / record plumbing
// ============================================================================================

HydraulicPanel::HydraulicPanel()
    : store_(sim::StudyRecordStore::Config{"[hydraulics]", "Open a part to save this circuit inside a document.",
                                           "Could not save the circuit into the document; retry.",
                                           "Could not delete the circuit from the document.", 60000}) {
    // Hydraulic duty defaults for the shared pump-drive form (ISO VG32 oil, gear pump).
    drive_.flowLpm = 20.0;
    drive_.pressureBar = 160.0;
    drive_.densityKgm3 = 870.0;
    drive_.pumpEff = 0.88;
    handoffParams_ = {{"supply_pressure_bar", hoSupplyBar_}, {"flow_lpm", hoFlowLpm_},
                      {"tolerance_pct", hoTolPct_}};
}

bool HydraulicPanel::isHydraulicStudyType(const std::string& type) { return type == "hydraulic_circuit"; }

void HydraulicPanel::setStudies(AppState& st, const std::string& docId,
                                const std::vector<nlohmann::json>& records) {
    (void)st;
    store_.setRecords(docId, records);
    if (busy()) return; // never retarget an in-flight circuit RPC to another record
    // Adopt the first circuit only while nothing local would be clobbered.
    const bool localEmpty = !circuit_.is_object() || jarr(circuit_, "components").empty();
    store_.sync(slot_, "hydraulic_circuit", localEmpty, [this](const json& r) { adopt(r); });
}

nlohmann::json HydraulicPanel::scheduleJson() const {
    json out = json::array();
    for (const auto& r : schedule_) {
        json row = r.native;
        if (!row.is_object()) row = json::object();
        if (row.empty() || r.edited) {
            row["t_s"] = r.t;
            row["component_id"] = r.valve;
            if (r.pos == 0) row["position"] = "a";
            else if (r.pos == 1) row["position"] = "b";
            else if (r.pos == 2) row["position"] = "center";
            else row["position"] = r.frac;
        }
        out.push_back(std::move(row));
    }
    return out;
}

nlohmann::json HydraulicPanel::record() const {
    json results = json::object();
    if (!sim_.is_null()) results["simulate"] = sim_;
    if (!bom_.is_null()) results["bom"] = bom_;
    if (!handoff_.is_null()) results["piping_handoff"] = handoff_;
    results["sizing"] = sizing_;
    return {{"type", "hydraulic_circuit"},
            {"name", jstr(circuit_, "name", "Hydraulic circuit")},
            {"engine", "cpp-hydraulic"},
            {"params", [&] {
                json p = simulationDefinition();
                p["circuit"] = circuit_;
                json layout = json::object();
                for (const auto& component : jarr(circuit_, "components")) {
                    float xyz[3]; componentPosition3d(component, xyz);
                    layout[jstr(component, "id")] = json::array({xyz[0], xyz[1], xyz[2]});
                }
                p["layout3d"] = std::move(layout);
                p["piping_handoff"] = handoffDefinition();
                return p;
            }()},
            {"results", results}};
}

void HydraulicPanel::adopt(const nlohmann::json& rec) {
    ++circuitGeneration_;
    ++resultGeneration_;
    ++layoutGeneration_;
    placing3d_ = false;
    slot_.bind(rec);
    const json& p = jobj(rec, "params");
    circuit_ = jobj(p, "circuit");
    if (circuit_.empty()) circuit_ = json();
    layout3d_ = jobj(p, "layout3d");
    schedule_.clear();
    for (const auto& r : jarr(p, "valve_schedule")) {
        ScheduleRow row;
        if (!r.is_object()) continue;
        row.native = r;
        row.t = jnum(r, "t_s");
        row.valve = jstr(r, "component_id");
        const json pos = r.value("position", json());
        if (pos.is_number()) {
            row.pos = 3;
            row.frac = pos.get<double>();
        } else {
            const std::string s = pos.is_string() ? pos.get<std::string>() : std::string("center");
            row.pos = s == "a" ? 0 : s == "b" ? 1 : 2;
        }
        schedule_.push_back(row);
    }
    loads_ = jobj(p, "loads");
    initial_ = jobj(p, "initial");
    const json& fl = jobj(p, "fluid");
    fluid_ = p.contains("fluid") ? p["fluid"] : json::object();
    handoffParams_ = jobj(p, "piping_handoff");
    hoSupplyBar_ = jnum(handoffParams_, "supply_pressure_bar", 160.0);
    hoFlowLpm_ = jnum(handoffParams_, "flow_lpm", 20.0);
    hoLoadBar_ = jnum(handoffParams_, "load_pressure_bar", 0.0);
    hoLoadPresent_ = jhas(handoffParams_, "load_pressure_bar");
    hoTolPct_ = jnum(handoffParams_, "tolerance_pct", 30.0);
    simulationExtra_ = p;
    simulationJsonDirty_ = true;
    for (const char* key : {"circuit", "layout3d", "valve_schedule", "loads", "fluid", "initial",
                            "duration_s", "output_dt_s", "piping_handoff"})
        simulationExtra_.erase(key);
    fluidRho_ = jnum(fl, "density_kgm3", 870.0);
    fluidBeta_ = jnum(fl, "bulk_modulus_bar", 15000.0);
    fluidCst_ = jnum(fl, "viscosity_cst", 32.0);
    durationS_ = jnum(p, "duration_s", 5.0);
    outputDtS_ = jnum(p, "output_dt_s", 0.01);
    const json& res = jobj(rec, "results");
    sim_ = res.contains("simulate") && res["simulate"].is_object() ? res["simulate"] : json();
    bom_ = res.contains("bom") && res["bom"].is_object() ? res["bom"] : json();
    handoff_ = res.contains("piping_handoff") && res["piping_handoff"].is_object() ? res["piping_handoff"] : json();
    sizing_ = jobj(res, "sizing");
    auto firstOf = [&](const char* group, std::string& target) -> json {
        const json& g = jobj(sizing_, group);
        if (g.contains(target) && g[target].is_object()) return g[target];
        for (auto it = g.begin(); it != g.end(); ++it)
            if (it->is_object()) {
                if (it.key() != "manual") target = it.key();
                return *it;
            }
        return json();
    };
    cylRes_ = firstOf("cylinders", cylTarget_);
    pumpRes_ = firstOf("pumps", pumpTarget_);
    motorRes_ = firstOf("motors", motorTarget_);
    driveRes_ = firstOf("drives", driveTarget_);
    const int n = (int)jarr(circuit_, "components").size();
    if (selected_ >= n) selected_ = n - 1;
    selectedHose_ = -1;
    dragging_ = -1;
    endpointComponent_.clear();
    endpointPort_.clear();
    ends_.clear();
    jsonDrafts_.clear();
    hoSeeded_ = p.contains("piping_handoff");
    parametersDirty_ = false;
    startVisible_ = jarr(circuit_, "components").empty();
    addOpen_ = hosesOpen_ = connectOpen_ = false;
    confirmDelete_ = false;
    canvas_.connectArmed = false;
    resetHistory();
}

void HydraulicPanel::persist(AppState& st) {
    if (circuit_.is_object()) {
        store_.saveRecord(st, slot_, record());
        if (parametersDirty_) committed_.simulation = simulationDefinition();
        parametersDirty_ = false;
    }
}

void HydraulicPanel::storeSizing(AppState& st, const char* group, const std::string& key,
                                 const nlohmann::json& result) {
    if (!sizing_.is_object()) sizing_ = json::object();
    if (!sizing_.contains(group) || !sizing_[group].is_object()) sizing_[group] = json::object();
    sizing_[group][key.empty() ? std::string("manual") : key] = result;
    persist(st);
}

// ============================================================================================
// RPC plumbing
// ============================================================================================

void HydraulicPanel::call(AppState& st, const char* method, nlohmann::json params, Done done, int timeoutMs) {
    callStart_ = ImGui::GetTime();
    store_.call(st, method, std::move(params), this, done, timeoutMs);
}

// ============================================================================================
// circuit helpers
// ============================================================================================

int HydraulicPanel::componentIndex(const std::string& id) const {
    const json& comps = jarr(circuit_, "components");
    for (int i = 0; i < (int)comps.size(); ++i)
        if (jstr(comps[i], "id") == id) return i;
    return -1;
}

std::string HydraulicPanel::componentIdByName(const std::string& name) const {
    for (const auto& c : jarr(circuit_, "components"))
        if (jstr(c, "name") == name) return jstr(c, "id");
    return std::string();
}

std::string HydraulicPanel::componentName(const std::string& id) const {
    const int i = componentIndex(id);
    if (i < 0) return id;
    const json& c = jarr(circuit_, "components")[i];
    const std::string name = jstr(c, "name");
    return !name.empty() ? name : typeLabel(jstr(c, "type"));
}

std::vector<int> HydraulicPanel::componentsOfType(const char* type) const {
    std::vector<int> out;
    const json& comps = jarr(circuit_, "components");
    for (int i = 0; i < (int)comps.size(); ++i)
        if (jstr(comps[i], "type") == type) out.push_back(i);
    return out;
}

void HydraulicPanel::invalidateResults() {
    ++resultGeneration_;
    // The simulation, BOM and piping handoff describe the circuit as it was: drop them.
    // Sizing results are inputs-only calculations and stay.
    sim_ = json();
    bom_ = json();
    handoff_ = json();
    pipingOpenReq_ = false;
    parametersDirty_ = true;
    simulationJsonDirty_ = true;
}

HydraulicPanel::Snapshot HydraulicPanel::snapshot() const {
    return {circuit_, layout3d_, simulationDefinition()};
}

void HydraulicPanel::resetHistory() {
    undo_.clear();
    redo_.clear();
    committed_ = snapshot();
}

void HydraulicPanel::commitCircuitMutation(AppState& st, bool invalidate) {
    const bool changed = committed_.circuit != circuit_ || committed_.layout3d != layout3d_;
    if (changed && !restoring_) {
        if (undo_.size() == 50) undo_.erase(undo_.begin());
        undo_.push_back(std::move(committed_));
        redo_.clear();
    }
    ++circuitGeneration_;
    ++layoutGeneration_;
    if (invalidate) invalidateResults();
    committed_ = snapshot();
    parametersDirty_ = false; // snapshot already captured setup; persist need not copy it again
    persist(st);
}

void HydraulicPanel::circuitChanged(AppState& st) {
    commitCircuitMutation(st);
}

bool HydraulicPanel::canUndo() const { return !busy() && !undo_.empty(); }
bool HydraulicPanel::canRedo() const { return !busy() && !redo_.empty(); }

void HydraulicPanel::restoreSnapshot(AppState& st, Snapshot state) {
    restoring_ = true;
    json rec = record();
    rec["id"] = slot_.id;
    rec["params"] = std::move(state.simulation);
    rec["params"]["circuit"] = std::move(state.circuit);
    rec["params"]["layout3d"] = std::move(state.layout3d);
    rec["params"]["piping_handoff"] = handoffDefinition();
    // adopt normalises every dependent form; retain the undo/redo stacks across that adoption.
    auto undo = std::move(undo_), redo = std::move(redo_);
    adopt(rec);
    undo_ = std::move(undo);
    redo_ = std::move(redo);
    selected_ = selectedHose_ = -1;
    startVisible_ = jarr(circuit_, "components").empty();
    circuitChanged(st);
    restoring_ = false;
}

bool HydraulicPanel::undo(AppState& st) {
    if (!canUndo()) return false;
    redo_.push_back(snapshot());
    Snapshot previous = std::move(undo_.back());
    undo_.pop_back();
    restoreSnapshot(st, std::move(previous));
    setStatus("Circuit edit undone.", 1);
    return true;
}

bool HydraulicPanel::redo(AppState& st) {
    if (!canRedo()) return false;
    undo_.push_back(snapshot());
    Snapshot next = std::move(redo_.back());
    redo_.pop_back();
    restoreSnapshot(st, std::move(next));
    setStatus("Circuit edit restored.", 1);
    return true;
}

bool HydraulicPanel::deleteSelection(AppState& st) {
    if (busy()) return false;
    if (selectedHose_ >= 0) {
        removeHose(st, selectedHose_);
        return true;
    }
    if (selected_ >= 0) {
        removeComponent(st, selected_);
        return true;
    }
    setStatus("Select a component or hose to remove.", 2);
    return false;
}

double HydraulicPanel::pumpFlowLpm(const nlohmann::json& pump) const {
    const json& p = jobj(pump, "params");
    if (jhas(p, "flow_lpm")) return jnum(p, "flow_lpm");
    return jnum(p, "displacement_cc_rev") * jnum(p, "speed_rpm") * jnum(p, "vol_efficiency", 0.95) / 1000.0;
}

double HydraulicPanel::reliefPressureBar() const {
    double best = 0.0;
    for (int i : componentsOfType("valve_relief"))
        best = std::max(best, jnum(jobj(jarr(circuit_, "components")[i], "params"), "set_pressure_bar"));
    return best;
}

void HydraulicPanel::removeComponent(AppState& st, int index) {
    if (!circuit_.is_object() || !circuit_.contains("components")) return;
    json& comps = circuit_["components"];
    if (index < 0 || index >= (int)comps.size()) return;
    const std::string id = jstr(comps[index], "id");
    const std::string name = componentName(id);
    comps.erase(comps.begin() + index);
    // Drop the component's hose ends; a hose left with fewer than two ends is removed.
    if (circuit_.contains("hoses") && circuit_["hoses"].is_array()) {
        json kept = json::array();
        for (auto h : circuit_["hoses"]) {
            json conns = json::array();
            for (const auto& c : jarr(h, "connections"))
                if (jstr(c, "component_id") != id) conns.push_back(c);
            if (conns.size() < 2) continue;
            h["connections"] = std::move(conns);
            kept.push_back(std::move(h));
        }
        circuit_["hoses"] = std::move(kept);
    }
    schedule_.erase(std::remove_if(schedule_.begin(), schedule_.end(),
                                   [&](const ScheduleRow& r) { return r.valve == id; }),
                    schedule_.end());
    loads_.erase(id);
    initial_.erase(id);
    layout3d_.erase(id);
    selected_ = comps.empty() ? -1 : std::min(index, (int)comps.size() - 1);
    selectedHose_ = -1;
    ends_.clear();
    if (comps.empty()) startVisible_ = true;
    circuitChanged(st);
    setStatus("Removed " + name + " and its hose ends.", 0);
}

void HydraulicPanel::removeHose(AppState& st, int index) {
    if (!circuit_.is_object() || !circuit_.contains("hoses") || !circuit_["hoses"].is_array()) return;
    json& hoses = circuit_["hoses"];
    if (index < 0 || index >= (int)hoses.size()) return;
    const std::string name = jstr(hoses[index], "net_name", "Hose");
    hoses.erase(hoses.begin() + index);
    selectedHose_ = -1;
    circuitChanged(st);
    setStatus("Removed " + name + ".", 0);
}

void HydraulicPanel::seedConnectDefaults() {
    const json& comps = jarr(circuit_, "components");
    const int n = (int)comps.size();
    for (auto& e : ends_)
        if (e.comp >= n) e = End{};
    while (ends_.size() < 2) ends_.push_back(End{});
    if (n > 0 && ends_[0].comp < 0) ends_[0].comp = selected_ >= 0 && selected_ < n ? selected_ : 0;
    if (n > 1 && ends_[1].comp < 0) ends_[1].comp = ends_[0].comp == 0 ? 1 : 0;
    // A component's port list can shrink (ways / acting edited): keep every end on a real port.
    for (auto& e : ends_)
        if (e.comp >= 0) e.port = std::clamp(e.port, 0, std::max(0, (int)portsOf(comps[e.comp]).size() - 1));
}


void HydraulicPanel::seedHandoffDefaults() {
    if (busy() || hoSeeded_ || !circuit_.is_object() || jarr(circuit_, "hoses").empty()) return;
    hoSeeded_ = true;
    if (reliefPressureBar() > 0.0) hoSupplyBar_ = reliefPressureBar();
    double q = 0.0;
    for (int i : componentsOfType("pump")) q += pumpFlowLpm(jarr(circuit_, "components")[i]);
    if (q > 0.0) hoFlowLpm_ = q;
    handoffParams_["supply_pressure_bar"] = hoSupplyBar_;
    handoffParams_["flow_lpm"] = hoFlowLpm_;
    handoffParams_["tolerance_pct"] = hoTolPct_;
}

// ---- readiness (the footer shows these inline; the run* entry points refuse with them) ---------

std::string HydraulicPanel::cylinderBlocker() const {
    if (!(cylBore_ > 0.0) || !(cylPressure_ > 0.0) || !(cylStroke_ > 0.0) || !(cylVelocity_ > 0.0))
        return "Bore, pressure, stroke and speed must be positive.";
    return std::string();
}
std::string HydraulicPanel::pumpBlocker() const {
    if (!(pumpFlow_ > 0.0) || !(pumpPressure_ > 0.0) || !(pumpSpeed_ > 0.0))
        return "Flow, pressure and speed must be positive.";
    return std::string();
}
std::string HydraulicPanel::motorBlocker() const {
    if (!(motTorque_ > 0.0) || !(motSpeed_ > 0.0) || !(motPressure_ > 0.0))
        return "Torque, speed and pressure must be positive.";
    return std::string();
}
std::string HydraulicPanel::driveBlocker() const {
    if (drive_.kind == 0 && (!(drive_.flowLpm > 0.0) || !(drive_.pressureBar > 0.0)))
        return "Flow and pressure must be positive.";
    if (drive_.kind != 0 && (!(drive_.flowM3h > 0.0) || !(drive_.headM > 0.0)))
        return "Flow and head must be positive.";
    return std::string();
}
std::string HydraulicPanel::simulateBlocker() const {
    const auto& components = jarr(circuit_, "components");
    if (components.empty()) return "Add components or choose a starting circuit before running.";
    bool actuator = false, pump = false, reservoir = false;
    for (const auto& c : components) {
        const auto type = jstr(c, "type");
        actuator |= type == "cylinder_linear" || type == "cylinder_rotary" || type == "motor_hydraulic";
        pump |= type == "pump";
        reservoir |= type == "reservoir";
    }
    if (actuator && !pump) return "Add a pump to supply the actuators before running.";
    if (actuator && !reservoir) return "Add a reservoir for the return flow before running.";
    for (const auto& c : components) {
        const auto ports = openRequiredPorts(circuit_, c);
        if (!ports.empty())
            return "Connect " + componentName(jstr(c, "id")) + " \xc2\xb7 " + ports.front() +
                   " (" + portMeaning(jstr(c, "type"), ports.front()) + ") before running.";
    }
    if (jarr(circuit_, "hoses").empty()) return "Connect the components with hoses before running.";
    if (!(durationS_ > 0.0) || !(outputDtS_ > 0.0)) return "Set a positive duration and output step before running.";
    for (const auto& r : schedule_)
        if (componentIndex(r.valve) < 0) return "Choose an existing valve for each schedule step before running.";
    return std::string();
}
std::string HydraulicPanel::bomBlocker() const {
    if (!circuit_.is_object() || jarr(circuit_, "components").empty()) return "The circuit has no components.";
    return std::string();
}
std::string HydraulicPanel::handoffBlocker() const {
    if (jarr(circuit_, "hoses").empty()) return "The circuit has no hoses to hand off.";
    if (!(hoSupplyBar_ > 0.0) || !(hoFlowLpm_ > 0.0)) return "Supply pressure and flow must be positive.";
    return std::string();
}
std::string HydraulicPanel::connectBlocker() const {
    if (!endpointComponent_.empty()) return "Select the second port on the schematic first.";
    const json& comps = jarr(circuit_, "components");
    if (comps.size() < 2) return "Add at least two components to connect them.";
    std::vector<std::pair<int, int>> seen;
    for (const auto& e : ends_) {
        if (e.comp < 0 || e.comp >= (int)comps.size()) return "Pick a component for every end.";
        for (const auto& s : seen)
            if (s.first == e.comp && s.second == e.port) {
                const std::vector<std::string> ports = portsOf(comps[e.comp]);
                const std::string port = e.port >= 0 && e.port < (int)ports.size() ? ports[e.port] : "?";
                return componentName(jstr(comps[e.comp], "id")) + " port " + port + " is picked twice.";
            }
        seen.push_back({e.comp, e.port});
    }
    if (!std::isfinite(hoseLengthMm_) || hoseLengthMm_ < 0.0 ||
        !std::isfinite(hoseIdMm_) || !(hoseIdMm_ > 0.0))
        return "Hose length must be finite and nonnegative; inside diameter must be finite and positive.";
    return std::string();
}
HydraulicPanel::NextStep HydraulicPanel::circuitNextStep() const {
    const json& comps = jarr(circuit_, "components");
    if (comps.empty()) return NextStep::Add;
    const json& hoses = jarr(circuit_, "hoses");
    if (hoses.empty()) return NextStep::Connect;
    for (const auto& c : comps)
        if (!openRequiredPorts(circuit_, c).empty()) return NextStep::Connect;
    return NextStep::Simulate;
}

// ============================================================================================
// actions
// ============================================================================================

void HydraulicPanel::fetchTemplates(AppState& st) {
    if (templatesFetched_ || templatesPending_ || busy()) return;
    templatesFetched_ = templatesPending_ = true;
    templatesError_.clear();
    call(st, "hydraulic_templates", json::object(), &HydraulicPanel::onTemplates, 15000);
}

void HydraulicPanel::onTemplates(AppState& st, bool ok, const json& r, const std::string& why) {
    (void)st;
    templatesPending_ = false;
    if (!ok || !r.contains("templates") || !r["templates"].is_array()) {
        templatesError_ = "Could not load starting circuits: " + (ok ? std::string("no templates in the reply") : why);
        return;
    }
    templates_ = r["templates"];
    std::stable_sort(templates_.begin(), templates_.end(), [](const json& a, const json& b) {
        return jstr(a, "id") == "cylinder_circuit" && jstr(b, "id") != "cylinder_circuit";
    });
}

bool HydraulicPanel::newCircuit(AppState& st, const std::string& name) {
    if (busy()) return false;
    templateApplying_ = false;
    templateChoice_ = false;
    setStatus("Creating the circuit\xe2\x80\xa6", 0);
    call(st, "hydraulic_new", {{"name", name.empty() ? std::string("Hydraulic circuit") : name},
                              {"template", "blank"}}, &HydraulicPanel::onNew, 15000);
    return true;
}

bool HydraulicPanel::applyTemplate(AppState& st, const std::string& templateId) {
    if (busy()) return false;
    templateApplying_ = circuit_.is_object();
    templateChoice_ = true;
    const std::string name = jstr(circuit_, "name", "Hydraulic circuit");
    setStatus("Creating the starting circuit\xe2\x80\xa6", 0);
    call(st, "hydraulic_new", {{"name", name}, {"template", templateId}}, &HydraulicPanel::onNew, 15000);
    return true;
}

void HydraulicPanel::onNew(AppState& st, bool ok, const json& r, const std::string& why) {
    const bool applying = templateApplying_, chosen = templateChoice_;
    templateApplying_ = templateChoice_ = false;
    if (!ok || !r.contains("doc") || !r["doc"].is_object()) {
        setStatus("New circuit refused: " + (ok ? std::string("no circuit in the reply") : why), 3);
        return;
    }
    Snapshot previous = applying ? snapshot() : Snapshot{};
    auto undo = std::move(undo_), redo = std::move(redo_);
    const std::string studyId = applying ? slot_.id : std::string{};
    json params = jobj(r, "simulation");
    params["circuit"] = r["doc"];
    params["layout3d"] = json::object();
    adopt({{"id", studyId}, {"type", "hydraulic_circuit"}, {"name", jstr(r["doc"], "name")},
           {"params", std::move(params)}, {"results", json::object()}});
    cylTarget_.clear();
    pumpTarget_.clear();
    motorTarget_.clear();
    driveTarget_.clear();
    cylRes_ = pumpRes_ = motorRes_ = driveRes_ = json();
    selected_ = selectedHose_ = -1;
    setForm(Form::Circuit);
    setCentralView(CentralView::Schematic);
    startVisible_ = !chosen;
    addOpen_ = !startVisible_ && jarr(circuit_, "components").empty();
    if (applying) {
        undo_ = std::move(undo);
        redo_ = std::move(redo);
        committed_ = std::move(previous);
        circuitChanged(st);
    } else {
        persist(st);
    }
    setStatus(startVisible_ ? "Choose a starting circuit or build a blank circuit."
                            : "Starting circuit ready. Simulate from the ribbon, or edit any component.", 1);
}

bool HydraulicPanel::addComponent(AppState& st, const std::string& type, const std::string& name,
                                  double x, double y, const nlohmann::json& params) {
    if (busy()) return false;
    if (!circuit_.is_object()) {
        setStatus("Create a circuit first.", 3);
        return false;
    }
    std::string error;
    if (!validComponentParams(type, params, error) || !std::isfinite(x) || !std::isfinite(y)) {
        setStatus(error.empty() ? "Placement coordinates must be finite." : error, 3);
        return false;
    }
    json p = {{"doc", circuit_}, {"type", type}, {"x", x}, {"y", y}};
    if (!name.empty()) p["name"] = name;
    if (params.is_object() && !params.empty()) p["params"] = params;
    setStatus("Adding " + typeLabel(type) + "\xe2\x80\xa6", 0);
    call(st, "hydraulic_add_component", std::move(p), &HydraulicPanel::onAdd, 15000);
    return true;
}

void HydraulicPanel::onAdd(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        setStatus("Component refused: " + why, 3);
        return;
    }
    const json& comp = jobj(r, "component");
    const std::string id = jstr(r, "component_id", jstr(comp, "id"));
    // Append the engine-built component to the live circuit (edits made while the call was in
    // flight survive); fall back to the returned document if the id already exists locally.
    if (!comp.empty() && componentIndex(id) < 0 && circuit_.is_object()) {
        if (!circuit_.contains("components") || !circuit_["components"].is_array())
            circuit_["components"] = json::array();
        circuit_["components"].push_back(comp);
    } else if (r.contains("doc") && r["doc"].is_object()) {
        circuit_ = r["doc"];
    }
    selected_ = componentIndex(id);
    // The Add form follows what was just placed (a scripted or ribbon add included), so its type
    // and the footer's "Add …" agree with the status line.
    if (selected_ >= 0) {
        const std::string type = jstr(jarr(circuit_, "components")[selected_], "type");
        const std::vector<TypeDef>& defs = typeDefs();
        for (int k = 0; k < (int)defs.size(); ++k)
            if (type == defs[k].type) addType_ = k;
    }
    ends_.clear();
    selectedHose_ = -1;
    startVisible_ = false;
    focusEditor_ = true;
    circuitChanged(st);
    setStatus("Added " + componentName(id) + ".", 1);
}

bool HydraulicPanel::addHose(AppState& st, const std::string& netName,
                             const std::vector<std::pair<std::string, std::string>>& ends,
                             double lengthMm, double idMm, double ratingBar) {
    if (busy()) return false;
    if (!circuit_.is_object()) {
        setStatus("Create a circuit first.", 3);
        return false;
    }
    if (ends.size() < 2) {
        setStatus("A hose needs at least two port ends.", 3);
        return false;
    }
    if (!std::isfinite(lengthMm) || lengthMm < 0.0 || !std::isfinite(idMm) || !(idMm > 0.0)) {
        setStatus("Hose length must be finite and nonnegative; inside diameter must be finite and positive.", 3);
        return false;
    }
    json conns = json::array();
    for (std::size_t i = 0; i < ends.size(); ++i) {
        const int ci = componentIndex(ends[i].first);
        const auto ports = ci >= 0 ? portsOf(jarr(circuit_, "components")[ci]) : std::vector<std::string>{};
        if (ci < 0 || std::find(ports.begin(), ports.end(), ends[i].second) == ports.end()) {
            setStatus("Choose an existing component and native port for every hose end.", 3);
            return false;
        }
        for (std::size_t j = 0; j < i; ++j)
            if (ends[j] == ends[i]) {
                setStatus(componentName(ends[i].first) + " port " + ends[i].second + " is listed twice.", 3);
                return false;
            }
        conns.push_back({{"component_id", ends[i].first}, {"port", ends[i].second}});
    }
    // Default net name from the ends' display names ("Pump P – DCV 4/3 P"), never raw ids.
    std::string net = netName;
    if (net.empty())
        net = componentName(ends[0].first) + " " + ends[0].second + " \xe2\x80\x93 " + componentName(ends[1].first) +
              " " + ends[1].second;
    pendingHoseGeom_ = {{"length_mm", lengthMm}, {"id_mm", idMm}};
    if (ratingBar > 0.0) pendingHoseGeom_["rating_bar"] = ratingBar;
    setStatus("Connecting\xe2\x80\xa6", 0);
    call(st, "hydraulic_add_hose", {{"doc", circuit_}, {"net_name", net}, {"connections", conns}},
         &HydraulicPanel::onHose, 15000);
    return true;
}

void HydraulicPanel::onHose(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        setStatus("Hose refused: " + why, 3);
        return;
    }
    json hose = jobj(r, "hose");
    const std::string id = jstr(r, "hose_id", jstr(hose, "id"));
    if (hose.empty() && r.contains("doc")) {
        for (const auto& h : jarr(r["doc"], "hoses"))
            if (jstr(h, "id") == id) hose = h;
    }
    if (pendingHoseGeom_.is_object()) hose.update(pendingHoseGeom_);
    pendingHoseGeom_ = json();
    if (!circuit_.contains("hoses") || !circuit_["hoses"].is_array()) circuit_["hoses"] = json::array();
    bool replaced = false;
    for (auto& h : circuit_["hoses"])
        if (jstr(h, "id") == id) {
            h = hose;
            replaced = true;
        }
    if (!replaced) circuit_["hoses"].push_back(hose);
    selectedHose_ = (int)circuit_["hoses"].size() - 1;
    selected_ = -1;
    focusHose_ = true;
    hoseNet_.clear();
    circuitChanged(st);
    // "Connected Pump P to DCV 4/3 P" (ids stay internal; a tee lists every end).
    std::string ends;
    const json& conns = jarr(hose, "connections");
    for (std::size_t k = 0; k < conns.size(); ++k)
        ends += std::string(k == 0 ? "" : conns.size() == 2 ? " to " : ", ") +
                componentName(jstr(conns[k], "component_id")) + " " + jstr(conns[k], "port");
    setStatus("Connected " + ends + ".", 1);
}

bool HydraulicPanel::connectPorts(AppState& st, const std::string& componentA, const std::string& portA,
                                   const std::string& componentB, const std::string& portB) {
    if (busy()) return false;
    if (componentA == componentB && portA == portB) {
        setStatus("Choose a different port to connect.", 2);
        return false;
    }
    const auto valid = [&](const std::string& id, const std::string& port) {
        const int i = componentIndex(id);
        if (i < 0) return false;
        const auto ports = portsOf(circuit_["components"][i]);
        return std::find(ports.begin(), ports.end(), port) != ports.end();
    };
    if (!valid(componentA, portA) || !valid(componentB, portB)) {
        setStatus("Choose existing ports on the circuit.", 2);
        return false;
    }
    int hoseA = -1, hoseB = -1;
    auto& hoses = circuit_["hoses"];
    for (int i = 0; i < (int)hoses.size(); ++i)
        for (const auto& end : jarr(hoses[i], "connections")) {
            if (jstr(end, "component_id") == componentA && jstr(end, "port") == portA) hoseA = i;
            if (jstr(end, "component_id") == componentB && jstr(end, "port") == portB) hoseB = i;
        }
    if (hoseA >= 0 && hoseA == hoseB) {
        setStatus("These ports are already connected.", 2);
        return false;
    }
    bool accepted = false;
    if (hoseA >= 0 || hoseB >= 0) {
        const int target = hoseA >= 0 ? hoseA : hoseB;
        json definition = hoses[target];
        const int other = target == hoseA ? hoseB : hoseA;
        if (other >= 0) {
            for (const auto& end : jarr(hoses[other], "connections"))
                definition["connections"].push_back(end);
        } else {
            definition["connections"].push_back({{"component_id", hoseA >= 0 ? componentB : componentA},
                                                 {"port", hoseA >= 0 ? portB : portA}});
        }
        std::string error;
        const std::string hoseId = jstr(definition, "id");
        if (other >= 0) {
            // Merge complete nets in one edit, retaining the first hose's authored geometry.
            hoses.erase(hoses.begin() + other);
            for (auto& hose : hoses)
                if (jstr(hose, "id") == hoseId) hose = std::move(definition);
            circuitChanged(st);
            accepted = true;
        } else {
            accepted = configureHose(st, hoseId, definition, error);
        }
        if (accepted) {
            selected_ = -1;
            selectedHose_ = other >= 0 && other < target ? target - 1 : target;
            focusHose_ = true;
            setStatus("Connected ports to the existing hose.", 1);
        }
    } else {
        accepted = addHose(st, "", {{componentA, portA}, {componentB, portB}},
                          hoseLengthMm_, hoseIdMm_, hoseRatingBar_);
    }
    if (accepted) {
        endpointComponent_.clear();
        endpointPort_.clear();
        ends_.clear();
    }
    return accepted;
}

bool HydraulicPanel::addPickedComponent(AppState& st) {
    const std::vector<TypeDef>& defs = typeDefs();
    const TypeDef& d = defs[std::clamp(addType_, 0, (int)defs.size() - 1)];
    if (addParamsType_ != addType_) {
        addParams_ = defaultParams(d);
        addParamsType_ = addType_;
    }
    if (!addComponent(st, d.type, addName_, addX_, addY_, addParams_)) return false;
    addName_.clear();
    return true;
}

bool HydraulicPanel::connectEnds(AppState& st) {
    const std::string why = connectBlocker();
    if (!why.empty()) {
        setStatus(why, 3);
        return false;
    }
    const json& comps = jarr(circuit_, "components");
    std::vector<std::pair<std::string, std::string>> ends;
    for (const auto& e : ends_) {
        const std::vector<std::string> ports = portsOf(comps[e.comp]);
        if (e.port < 0 || e.port >= (int)ports.size()) continue;
        ends.push_back({jstr(comps[e.comp], "id"), ports[e.port]});
    }
    return addHose(st, hoseNet_, ends, hoseLengthMm_, hoseIdMm_, hoseRatingBar_);
}

void HydraulicPanel::setCylinderInputs(const std::string& componentId, double boreMm, double rodMm,
                                       double pressureBar, double strokeMm, double velocityMmS) {
    boreNote_.clear();
    cylTarget_ = componentId;
    cylBore_ = boreMm;
    cylRod_ = rodMm;
    cylPressure_ = pressureBar;
    cylStroke_ = strokeMm;
    cylVelocity_ = velocityMmS;
}

bool HydraulicPanel::runSizeCylinder(AppState& st) {
    if (busy()) return false;
    const std::string why = cylinderBlocker();
    if (!why.empty()) {
        setStatus(why, 3);
        return false;
    }
    setStatus("Sizing the cylinder\xe2\x80\xa6", 0);
    call(st, "hydraulic_size_cylinder",
         {{"bore_mm", cylBore_},
          {"rod_mm", cylRod_},
          {"pressure_bar", cylPressure_},
          {"stroke_mm", cylStroke_},
          {"velocity_mm_s", cylVelocity_},
          {"efficiency", cylEff_}},
         &HydraulicPanel::onCylinder, 15000);
    return true;
}

void HydraulicPanel::onCylinder(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        setStatus("Cylinder sizing refused: " + why, 3);
        return;
    }
    cylRes_ = stripAdvice(r);
    storeSizing(st, "cylinders", cylTarget_, cylRes_);
    reveal_ = Reveal::Cylinder;
    setDoneStatus("Cylinder sized", r);
}

bool HydraulicPanel::applyCylinderSizing(AppState& st) {
    if (busy()) return false;
    const int i = componentIndex(cylTarget_);
    if (i < 0 || cylRes_.is_null()) {
        setStatus("Size the cylinder for a circuit component first.", 3);
        return false;
    }
    json& p = circuit_["components"][i]["params"];
    if (!p.is_object()) p = json::object();
    p["bore_mm"] = jnum(cylRes_, "bore_mm", cylBore_);
    p["rod_mm"] = jnum(cylRes_, "rod_mm", cylRod_);
    p["stroke_mm"] = jnum(cylRes_, "stroke_mm", cylStroke_);
    circuitChanged(st);
    setStatus("Bore, rod and stroke applied to " + componentName(cylTarget_) + ".", 1);
    return true;
}

bool HydraulicPanel::runSizePump(AppState& st) {
    if (busy()) return false;
    const std::string why = pumpBlocker();
    if (!why.empty()) {
        setStatus(why, 3);
        return false;
    }
    setStatus("Sizing the pump\xe2\x80\xa6", 0);
    call(st, "hydraulic_size_pump",
         {{"flow_lpm", pumpFlow_}, {"pressure_bar", pumpPressure_}, {"efficiency", pumpEff_}, {"speed_rpm", pumpSpeed_}},
         &HydraulicPanel::onPump, 15000);
    return true;
}

void HydraulicPanel::onPump(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        setStatus("Pump sizing refused: " + why, 3);
        return;
    }
    pumpRes_ = stripAdvice(r);
    storeSizing(st, "pumps", pumpTarget_, pumpRes_);
    reveal_ = Reveal::Pump;
    setDoneStatus("Pump sized", r);
}

bool HydraulicPanel::runSizeMotor(AppState& st) {
    if (busy()) return false;
    const std::string why = motorBlocker();
    if (!why.empty()) {
        setStatus(why, 3);
        return false;
    }
    json p = {{"torque_nm", motTorque_},
              {"speed_rpm", motSpeed_},
              {"pressure_bar", motPressure_},
              {"mech_efficiency", motMech_},
              {"vol_efficiency", motVol_}};
    if (motType_ > 0) p["type"] = kMotorTypeKeys[motType_];
    setStatus("Sizing the motor\xe2\x80\xa6", 0);
    call(st, "hydraulic_size_motor", std::move(p), &HydraulicPanel::onMotor, 15000);
    return true;
}

void HydraulicPanel::onMotor(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        setStatus("Motor sizing refused: " + why, 3);
        return;
    }
    motorRes_ = stripAdvice(r);
    storeSizing(st, "motors", motorTarget_, motorRes_);
    reveal_ = Reveal::Motor;
    setDoneStatus("Motor sized", r);
}

void HydraulicPanel::setDriveFromPump(const std::string& pumpComponentId) {
    const int i = componentIndex(pumpComponentId);
    if (i < 0) return;
    driveTarget_ = pumpComponentId;
    drive_.kind = 0;
    const json& pump = jarr(circuit_, "components")[i];
    const double q = pumpFlowLpm(pump);
    if (q > 0.0) drive_.flowLpm = q;
    const double relief = reliefPressureBar();
    if (relief > 0.0) drive_.pressureBar = relief;
    const double speed = jnum(jobj(pump, "params"), "speed_rpm");
    if (speed > 0.0) drive_.speedRpm = speed;
    drive_.densityKgm3 = fluidRho_;
}

bool HydraulicPanel::runPumpDrive(AppState& st) {
    if (busy()) return false;
    const std::string why = driveBlocker();
    if (!why.empty()) {
        setStatus(why, 3);
        return false;
    }
    setStatus("Sizing the electric drive\xe2\x80\xa6", 0);
    call(st, "pump_drive_size", sim::pumpDriveParams(drive_), &HydraulicPanel::onDrive, 15000);
    return true;
}

void HydraulicPanel::onDrive(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        setStatus("Drive sizing refused: " + why, 3);
        return;
    }
    driveRes_ = stripAdvice(r);
    storeSizing(st, "drives", driveTarget_, driveRes_);
    reveal_ = Reveal::Drive;
    setDoneStatus("Drive sized", r);
}

void HydraulicPanel::setDoneStatus(const char* what, const nlohmann::json& result) {
    const std::size_t n = jarr(result, "warnings").size();
    if (n == 0)
        setStatus(std::string(what) + ".", 1);
    else
        setStatus(std::string(what) + " \xc2\xb7 " + sim::countNoun((long long)n, "warning") + ".", 2);
}

void HydraulicPanel::clearSchedule() {
    if (busy() || schedule_.empty()) return;
    schedule_.clear();
    invalidateResults();
}

void HydraulicPanel::setDuration(double seconds) {
    if (busy() || durationS_ == seconds) return;
    durationS_ = seconds;
    invalidateResults();
}

void HydraulicPanel::addScheduleRow(double tS, const std::string& valveId, const std::string& position) {
    if (busy()) return;
    ScheduleRow row;
    row.t = tS;
    row.valve = valveId;
    row.pos = position == "a" ? 0 : position == "b" ? 1 : 2;
    schedule_.push_back(row);
    invalidateResults();
}

void HydraulicPanel::setLoad(const std::string& componentId, double forceN, double massKg) {
    if (busy()) return;
    json& row = loads_[componentId];
    if (!row.is_object()) row = json::object();
    if (jnum(row, "force_n") == forceN && jnum(row, "mass_kg") == massKg) return;
    row["force_n"] = forceN;
    row["mass_kg"] = massKg;
    invalidateResults();
}

nlohmann::json HydraulicPanel::simulationDefinition() const {
    json p = simulationExtra_;
    p["duration_s"] = durationS_;
    p["output_dt_s"] = outputDtS_;
    p["valve_schedule"] = scheduleJson();
    p["loads"] = loads_;
    p["initial"] = initial_;
    p["fluid"] = fluid_;
    return p;
}

bool HydraulicPanel::configureSimulation(AppState& st, const json& definition, std::string& error) {
    error.clear();
    if (busy()) error = "Wait for the running request to finish.";
    else if (!definition.is_object()) error = "Simulation parameters must be a JSON object.";
    else {
        for (const char* key : {"doc", "circuit", "id", "type", "results"})
            if (definition.contains(key)) error = std::string(key) + " is not a simulation setting.";
        for (const char* key : {"loads", "initial"})
            if (definition.contains(key) && !definition[key].is_object())
                error = std::string(key) + " must be an object.";
        if (definition.contains("fluid") && !definition["fluid"].is_null() &&
            !definition["fluid"].is_object())
            error = "fluid must be an object or null.";
        if (definition.contains("fluid") && definition["fluid"].is_object()) {
            const auto& fluid = definition["fluid"];
            for (const char* key : {"density_kgm3", "bulk_modulus_bar", "viscosity_cst"})
                if (fluid.contains(key) && !fluid[key].is_null() &&
                    (!fluid[key].is_number() || !std::isfinite(fluid[key].get<double>()) ||
                     fluid[key].get<double>() <= 0.0))
                    error = std::string("fluid.") + key + " must be a finite positive number or null.";
            if (fluid.contains("air_fraction") && !fluid["air_fraction"].is_null() &&
                (!fluid["air_fraction"].is_number() || !std::isfinite(fluid["air_fraction"].get<double>()) ||
                 fluid["air_fraction"].get<double>() < 0.0 || fluid["air_fraction"].get<double>() >= 0.2))
                error = "fluid.air_fraction must be a finite number in [0, 0.2) or null.";
        }
        for (const char* key : {"duration_s", "output_dt_s", "max_step_s"})
            if (definition.contains(key) &&
                (!definition[key].is_number() || !std::isfinite(definition[key].get<double>()) ||
                 definition[key].get<double>() <= 0.0))
                error = std::string(key) + " must be a finite positive number.";
        if (definition.contains("valve_schedule")) {
            if (!definition["valve_schedule"].is_array()) error = "valve_schedule must be an array.";
            else for (const auto& row : definition["valve_schedule"])
                if (!row.is_object() || !row.contains("t_s") || !row["t_s"].is_number() ||
                    !std::isfinite(row["t_s"].get<double>()) ||
                    !row.contains("component_id") || !row["component_id"].is_string() ||
                    !row.contains("position") ||
                    !(row["position"].is_string() ||
                      (row["position"].is_number() && std::isfinite(row["position"].get<double>()))))
                    error = "Each schedule row needs finite t_s, component_id and a string/numeric position.";
        }
    }
    if (!error.empty()) {
        setStatus(error, 3);
        return false;
    }
    if (simulationDefinition() == definition) return true;
    json rec = record();
    rec["id"] = slot_.id;
    rec["params"] = definition;
    rec["params"]["circuit"] = circuit_;
    rec["params"]["layout3d"] = layout3d_;
    rec["params"]["piping_handoff"] = handoffDefinition();
    auto undo = std::move(undo_), redo = std::move(redo_);
    adopt(rec);
    undo_ = std::move(undo);
    redo_ = std::move(redo);
    circuitChanged(st);
    return true;
}

bool HydraulicPanel::setComponentParams(AppState& st, const std::string& componentId, const json& params) {
    const int i = componentIndex(componentId);
    if (busy() || i < 0) return false;
    std::string error;
    if (!validComponentParams(jstr(circuit_["components"][i], "type"), params, error)) {
        setStatus(error, 3);
        return false;
    }
    if (circuit_["components"][i]["params"] == params) return true;
    circuit_["components"][i]["params"] = params;
    circuitChanged(st);
    return true;
}

bool HydraulicPanel::configureHose(AppState& st, const std::string& hoseId, const json& definition,
                                    std::string& error) {
    error.clear();
    if (busy()) error = "Wait for the running request to finish.";
    else if (!definition.is_object() || jstr(definition, "id") != hoseId)
        error = "Keep the existing hose id and enter a JSON object.";
    else if (!definition.contains("connections") || !definition["connections"].is_array() ||
             definition["connections"].size() < 2)
        error = "A hose needs at least two native port connections.";
    else {
        std::vector<std::pair<std::string, std::string>> seen;
        for (const auto& end : definition["connections"]) {
            const std::string cid = jstr(end, "component_id"), port = jstr(end, "port");
            const int ci = componentIndex(cid);
            const auto ports = ci >= 0 ? portsOf(jarr(circuit_, "components")[ci]) : std::vector<std::string>{};
            const auto key = std::make_pair(cid, port);
            if (ci < 0 || std::find(ports.begin(), ports.end(), port) == ports.end() ||
                std::find(seen.begin(), seen.end(), key) != seen.end())
                error = "Hose ends must name existing, distinct component ports.";
            seen.push_back(key);
        }
        for (const char* key : {"length_mm", "id_mm", "wall_bulk_modulus_bar"})
            if (definition.contains(key) && !definition[key].is_null()) {
                const auto& value = definition[key];
                const bool length = std::string_view(key) == "length_mm";
                if (!value.is_number() || !std::isfinite(value.get<double>()) ||
                    (length ? value.get<double>() < 0.0 : value.get<double>() <= 0.0))
                    error = std::string(key) + (length
                        ? " must be a finite nonnegative number or null."
                        : " must be a finite positive number or null.");
            }
    }
    if (!error.empty()) {
        setStatus(error, 3);
        return false;
    }
    for (auto& hose : circuit_["hoses"])
        if (jstr(hose, "id") == hoseId) {
            if (hose == definition) return true;
            hose = definition;
            circuitChanged(st);
            return true;
        }
    error = "Select an existing hose.";
    setStatus(error, 3);
    return false;
}

nlohmann::json HydraulicPanel::handoffDefinition() const {
    return handoffParams_;
}

bool HydraulicPanel::configureHandoff(AppState& st, const json& definition, std::string& error) {
    error.clear();
    if (busy()) error = "Wait for the running request to finish.";
    else if (!definition.is_object()) error = "Handoff inputs must be a JSON object.";
    else {
        for (const char* key : {"doc", "circuit", "id", "type", "results"})
            if (definition.contains(key)) error = std::string(key) + " is not a handoff setting.";
        for (const char* key : {"supply_pressure_bar", "flow_lpm", "default_id_mm", "default_length_mm"})
            if (definition.contains(key) && !definition[key].is_null() &&
                (!definition[key].is_number() || !std::isfinite(definition[key].get<double>()) ||
                 definition[key].get<double>() <= 0.0))
                error = std::string(key) + " must be a finite positive number.";
        for (const char* key : {"supply_pressure_bar", "flow_lpm"})
            if (!definition.contains(key) || definition[key].is_null())
                error = std::string(key) + " must be a finite positive number.";
        if (definition.contains("tolerance_pct") && !definition["tolerance_pct"].is_null() &&
            (!definition["tolerance_pct"].is_number() ||
             !std::isfinite(definition["tolerance_pct"].get<double>()) ||
             definition["tolerance_pct"].get<double>() < 0.0))
            error = "tolerance_pct must be a finite nonnegative number or null.";
        if (definition.contains("lines") && !definition["lines"].is_object())
            error = "lines must be an object keyed by hose id.";
        if (definition.contains("load_pressure_bar") && !definition["load_pressure_bar"].is_null() &&
            (!definition["load_pressure_bar"].is_number() ||
             !std::isfinite(definition["load_pressure_bar"].get<double>()) ||
             definition["load_pressure_bar"].get<double>() < 0.0))
            error = "load_pressure_bar must be a finite nonnegative number or null.";
    }
    if (!error.empty()) {
        setStatus(error, 3);
        return false;
    }
    if (handoffDefinition() == definition) return true;
    handoffParams_ = definition;
    hoSupplyBar_ = jnum(definition, "supply_pressure_bar", 160.0);
    hoFlowLpm_ = jnum(definition, "flow_lpm", 20.0);
    hoLoadBar_ = jnum(definition, "load_pressure_bar");
    hoLoadPresent_ = jhas(definition, "load_pressure_bar");
    hoTolPct_ = jnum(definition, "tolerance_pct", 30.0);
    hoSeeded_ = true;
    handoff_ = json();
    pipingOpenReq_ = false;
    persist(st);
    return true;
}

bool HydraulicPanel::selectPortEndpoint(AppState& st, const std::string& componentId,
                                        const std::string& port) {
    const int ci = componentIndex(componentId);
    if (busy() || ci < 0) return false;
    const auto ports = portsOf(jarr(circuit_, "components")[ci]);
    const auto it = std::find(ports.begin(), ports.end(), port);
    if (it == ports.end()) return false;
    const int pi = (int)std::distance(ports.begin(), it);
    setForm(Form::Circuit);
    setCircuitPage(CircuitPage::Connect);
    selected_ = ci;
    selectedHose_ = -1;
    if (endpointComponent_.empty()) {
        endpointComponent_ = componentId;
        endpointPort_ = port;
        ends_ = {{ci, pi}};
    } else if (endpointComponent_ != componentId || endpointPort_ != port) {
        const int source = componentIndex(endpointComponent_);
        if (source < 0) return false;
        const auto sourcePorts = portsOf(jarr(circuit_, "components")[source]);
        const auto sourcePort = std::find(sourcePorts.begin(), sourcePorts.end(), endpointPort_);
        if (sourcePort == sourcePorts.end()) return false;
        ends_ = {{source, (int)std::distance(sourcePorts.begin(), sourcePort)}, {ci, pi}};
        endpointComponent_.clear();
        endpointPort_.clear();
        setStatus("Ports selected. Customize the hose, then Connect.", 1);
    }
    return true;
}

nlohmann::json HydraulicPanel::simulateParams() const {
    json p = simulationDefinition();
    p["doc"] = circuit_;
    auto& sched = p["valve_schedule"];
    std::stable_sort(sched.begin(), sched.end(), [](const json& a, const json& b) {
        return jnum(a, "t_s") < jnum(b, "t_s");
    });
    return p;
}

bool HydraulicPanel::runSimulate(AppState& st) {
    if (busy()) return false;
    const std::string why = simulateBlocker();
    if (!why.empty()) {
        setStatus(why, 3);
        return false;
    }
    canvas_.connectArmed = false;
    endpointComponent_.clear();
    endpointPort_.clear();
    setStatus("Simulating " + kit::fmtQuantity(durationS_, kit::unitText(kit::Unit::s)) + " of circuit time\xe2\x80\xa6", 0);
    call(st, "hydraulic_simulate", simulateParams(), &HydraulicPanel::onSimulate, 600000);
    return true;
}

void HydraulicPanel::onSimulate(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        setStatus("Simulation refused: " + why, 3);
        return;
    }
    json res = stripAdvice(r);
    downsampleResult(res, kMaxStoredSamples);
    sim_ = std::move(res);
    ++resultGeneration_;
    seriesPick_ = 0;
    setForm(Form::Results);
    setResultPage(ResultPage::Outcome);
    persist(st);
    // The Outcome card says how it went (verdict, warnings): no footer echo.
    store_.clearStatus();
    reveal_ = Reveal::Simulate;
}

bool HydraulicPanel::runBom(AppState& st) {
    if (busy()) return false;
    const std::string why = bomBlocker();
    if (!why.empty()) {
        setStatus(why, 3);
        return false;
    }
    setStatus("Building the bill of materials\xe2\x80\xa6", 0);
    call(st, "hydraulic_bom", {{"doc", circuit_}}, &HydraulicPanel::onBom, 15000);
    return true;
}

void HydraulicPanel::onBom(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        setStatus("BOM refused: " + why, 3);
        return;
    }
    bom_ = stripAdvice(r);
    persist(st);
    // The Summary card carries the line and sourced counts: no footer echo.
    store_.clearStatus();
}

bool HydraulicPanel::runPipingHandoff(AppState& st) {
    if (busy()) return false;
    const std::string why = handoffBlocker();
    if (!why.empty()) {
        setStatus(why, 3);
        return false;
    }
    json p = handoffDefinition();
    p["doc"] = circuit_;
    setStatus("Generating and solving the piping network\xe2\x80\xa6", 0);
    call(st, "hydraulic_piping_handoff", std::move(p), &HydraulicPanel::onHandoff, 60000);
    return true;
}

void HydraulicPanel::onHandoff(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    // A failed live solve still returns the generated network: keep it, flagged.
    if (!ok && !r.contains("piping_doc")) {
        setStatus("Piping handoff refused: " + why, 3);
        return;
    }
    handoff_ = stripAdvice(r);
    persist(st);
    reveal_ = Reveal::Handoff;
    // A solved network speaks through its outcome chips; only a failed solve keeps its reason here.
    if (ok)
        store_.clearStatus();
    else
        setStatus("Network generated; the solve failed: " + why, 2);
}

bool HydraulicPanel::takePipingOpen(PipingOpen& out) {
    if (!pipingOpenReq_) return false;
    pipingOpenReq_ = false;
    if (!handoff_.is_object() || !handoff_.contains("piping_doc")) return false;
    out.pipingDoc = handoff_["piping_doc"];
    out.boundary = handoff_.value("boundary", json::array());
    out.fluid = handoff_.value("fluid", json::object());
    out.solve = handoff_.value("solve", json());
    return true;
}

// ============================================================================================
// commands
// ============================================================================================

bool HydraulicPanel::handleCommand(AppState& st, const std::string& cmdId) {
    const bool viewSchematic = cmdId == "sim_hyd_view_schematic";
    const bool viewPlots = cmdId == "sim_hyd_view_plots";
    const bool view3d = cmdId == "sim_hyd_view_3d";
    Form f = Form::Circuit;
    SizeKind k = sizeKind_;
    if (cmdId == "sim_hyd_new" || cmdId == "sim_hyd_add" || cmdId == "sim_hyd_hose") f = Form::Circuit;
    else if (cmdId == "sim_hyd_size_cyl") { f = Form::Design; k = SizeKind::Cylinder; }
    else if (cmdId == "sim_hyd_size_pump") { f = Form::Design; k = SizeKind::Pump; }
    else if (cmdId == "sim_hyd_size_motor") { f = Form::Design; k = SizeKind::Motor; }
    else if (cmdId == "sim_hyd_drive") { f = Form::Design; k = SizeKind::Drive; }
    else if (cmdId == "sim_hyd_simulate") f = Form::Simulate;
    else if (cmdId == "sim_hyd_bom" || cmdId == "sim_hyd_piping") f = Form::Results;
    else if (view3d) f = Form::Circuit;
    else if (viewSchematic || viewPlots) f = form_;
    else return false;

    st.activeWorkspace = AppState::Workspace::Simulate;
    st.leftPaneTab = AppState::LP_Simulation;
    st.showRibbon = true;
    st.ribbonTab = kRibbonTabHydraulics;
    open_ = true;
    dragging_ = -1;
    setForm(f);
    if (viewSchematic || viewPlots || view3d) {
        setCentralView(view3d ? CentralView::View3D : viewPlots ? CentralView::Plots : CentralView::Schematic);
        return true;
    }
    if (f == Form::Design) setSizeKind(k);
    if (cmdId == "sim_hyd_bom") setResultPage(ResultPage::Bom);
    if (cmdId == "sim_hyd_piping") setResultPage(ResultPage::Piping);
    if (f == Form::Circuit && central_ == CentralView::Plots) setCentralView(CentralView::Schematic);
    if (cmdId == "sim_hyd_new") {
        int n = 1;
        for (const auto& r : store_.records())
            if (jstr(r, "type") == "hydraulic_circuit") ++n;
        newCircuit(st, "Hydraulic circuit " + std::to_string(n));
    } else if (cmdId == "sim_hyd_add") {
        startVisible_ = false;
        setCircuitPage(CircuitPage::Add);
    } else if (cmdId == "sim_hyd_hose") {
        setCentralView(CentralView::Schematic);
        canvas_.connectArmed = true;
        endpointComponent_.clear();
        endpointPort_.clear();
        seedConnectDefaults();
        setStatus("Click a port, then another port to connect them. Esc to stop.", 0);
    } else if (cmdId == "sim_hyd_simulate") {
        if (simulateBlocker().empty()) runSimulate(st);
        else {
            focusSimulationBlocker_ = true;
            setStatus(simulateBlocker(), 2);
        }
    } else if (k == SizeKind::Drive && f == Form::Design && driveTarget_.empty()) {
        const auto pumps = componentsOfType("pump");
        if (!pumps.empty()) setDriveFromPump(jstr(jarr(circuit_, "components")[pumps.front()], "id"));
    }
    return true;
}

// ============================================================================================
// drawing
// ============================================================================================

void HydraulicPanel::drawStudyRows(AppState& st) {
    const std::vector<json>& recs = store_.records();
    int picked = -1;
    for (int i = 0; i < (int)recs.size(); ++i) {
        const json& rec = recs[i];
        const std::string title = jstr(rec, "name", "Hydraulic circuit");
        const std::string summary = recordSummary(rec);
        kit::RowStyle rs;
        rs.icon = recordIcon();
        rs.secondary = summary.c_str();
        rs.selected = open_ && jstr(rec, "id") == slot_.id;
        rs.tooltip = "Open this hydraulic circuit in the Hydraulics panel.";
        ImGui::PushID(i);
        if (kit::row("hyd_rec", title.c_str(), rs)) picked = i;
        ImGui::PopID();
    }
    if (picked < 0 || busy()) return;
    const json rec = recs[picked];
    adopt(rec);
    handleCommand(st, "sim_hyd_add");
    setCircuitPage(CircuitPage::Components);
}

void HydraulicPanel::setForm(Form f) {
    if (f == form_) return;
    form_ = f;
    dragging_ = -1;
    store_.clearStatus();  // the status line belongs to the page it was set on
    // Seed the form's defaults before its first frame draws.
    confirmDelete_ = false;
    if (f == Form::Results && resultPage_ == ResultPage::Piping) seedHandoffDefaults();
}

void HydraulicPanel::setCircuitPage(CircuitPage p) {
    if (p == CircuitPage::Add) addOpen_ = true;
    if (p == CircuitPage::Hoses) hosesOpen_ = true;
    if (p == CircuitPage::Connect) {
        connectOpen_ = true;
        seedConnectDefaults();
    }
}

void HydraulicPanel::setResultPage(ResultPage p) {
    if (p == resultPage_) return;
    resultPage_ = p;
    store_.clearStatus();
    if (p == ResultPage::Piping) seedHandoffDefaults();
}

void HydraulicPanel::setSizeKind(SizeKind k) {
    if (k == sizeKind_) return;
    sizeKind_ = k;
    store_.clearStatus();
}

void HydraulicPanel::syncForDraw() {
    if (!circuit_.is_object()) return;
    if (!circuit_.contains("components") || !circuit_["components"].is_array()) circuit_["components"] = json::array();
    if (!circuit_.contains("hoses") || !circuit_["hoses"].is_array()) circuit_["hoses"] = json::array();
    json& comps = circuit_["components"];
    for (auto& c : comps)
        if (c.is_object() && !c.contains("params")) c["params"] = json::object();
    const int n = (int)comps.size();
    if (selected_ >= n) selected_ = n - 1;
    if (selectedHose_ >= (int)circuit_["hoses"].size()) selectedHose_ = -1;
    if (n >= 2) seedConnectDefaults();
    if (form_ == Form::Results && resultPage_ == ResultPage::Piping) seedHandoffDefaults();
    // BOM sourcing cards open only where the part number is still missing (first sight only;
    // afterwards the user's open/closed choice sticks).
    for (const auto& c : comps)
        srcOpen_.emplace(jstr(c, "id"), jstr(jobj(c, "sourcing"), "mpn").empty());
}

namespace {
// A pinned row of sub-page chips (one accent chip = the open page). Index-scoped by the chip flow;
// labels are static. Returns true when *sel changed.
bool subPageChips(const char* id, const char* const* labels, const char* const* tips, int n, int* sel) {
    bool changed = false;
    kit::chipFlowBegin(id);
    for (int i = 0; i < n; ++i) {
        bool on = i == *sel;
        if (kit::flowToggleChip(labels[i], &on, kit::Tone::Accent, tips[i]) && i != *sel) {
            *sel = i;
            changed = true;
        }
    }
    kit::chipFlowEnd();
    return changed;
}
}  // namespace

void HydraulicPanel::drawSubPages() {
    if (form_ == Form::Design) {
        static const char* const kLabels[] = {"Cylinder", "Pump", "Motor", "Drive"};
        static const char* const kTips[] = {"Size a linear cylinder: bore, rod, forces, flows.",
                                           "Size a pump: displacement and shaft power.",
                                           "Size a hydraulic motor: displacement and catalog series.",
                                           "Size the electric motor that drives a pump."};
        int sel = (int)sizeKind_;
        if (subPageChips("size_kinds", kLabels, kTips, 4, &sel)) setSizeKind((SizeKind)sel);
    } else if (form_ == Form::Results) {
        static const char* const kLabels[] = {"Outcome", "BOM", "To Piping"};
        static const char* const kTips[] = {"Simulation outcome, actuator metrics and warnings.",
                                           "Sourcing and the bill of materials.",
                                           "Hand the hoses to Piping as editable pipe runs."};
        int sel = (int)resultPage_;
        if (subPageChips("result_pages", kLabels, kTips, 3, &sel)) setResultPage((ResultPage)sel);
    }
}

void HydraulicPanel::draw(AppState& st) {
    syncForDraw();
    if (parametersDirty_ && !busy()) persist(st);
    if (open_) fetchTemplates(st);
    kit::panelBegin("hyd_panel");
    const bool haveCircuit = circuit_.is_object();
    const std::string title = haveCircuit ? jstr(circuit_, "name", "Hydraulic circuit") : std::string("Hydraulics");
    kit::PanelHeaderSpec h;
    h.icon = "study_hydraulics";
    h.title = title.c_str();
    h.doneTip = "Close the Hydraulics panel and return to the study tree. The circuit is saved as you edit it.";
    h.titleFont = st.fonts.medium;
    if (haveCircuit && slot_.id.empty()) {
        h.statusChip = slot_.addPending ? "Saving" : "Not saved";
        h.statusTone = slot_.addPending ? kit::Tone::Neutral : kit::Tone::Warn;
        h.statusTip = "Open a part: the circuit saves into its document.";
    }
    kit::HeaderAction actions[] = {
        {"undo", "undo", "Undo circuit edit (Ctrl+Z)", canUndo()},
        {"redo", "redo", "Redo circuit edit (Ctrl+Shift+Z)", canRedo()},
        {"delete", "delete", "Delete this circuit from the document", haveCircuit && !slot_.id.empty() && !busy()}};
    int hit = -1;
    h.actions = actions;
    h.actionCount = 3;
    h.actionPressed = &hit;
    const bool done = kit::panelHeader("hdr", h);
    if (hit == 0) undo(st);
    if (hit == 1) redo(st);
    if (hit == 2) confirmDelete_ = true;
    const auto& io = ImGui::GetIO();
    const auto* hovered = GImGui->HoveredWindow;
    const bool overViewport = hovered &&
        (std::string_view(hovered->RootWindow->Name) == "Viewport" ||
         std::string_view(hovered->RootWindow->Name).find("###Viewport") != std::string_view::npos);
    if (ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows) &&
        !overViewport && !io.WantTextInput && !busy()) {
        if (ImGui::IsKeyPressed(ImGuiKey_Z, false) && io.KeyCtrl) {
            if (io.KeyShift) redo(st);
            else undo(st);
        } else if (ImGui::IsKeyPressed(ImGuiKey_Y, false) && io.KeyCtrl) {
            redo(st);
        }
    }
    static const char* const kTabs[] = {"Circuit", "Design", "Simulate", "Results"};
    static const char* const kTips[] = {"Components, hoses and connections.",
                                       "Cylinder, pump, motor and electric pump-drive sizing.",
                                       "Valve schedule, loads, fluid and expert simulation settings.",
                                       "Simulation outcome, bill of materials and piping handoff."};
    int f = (int)form_;
    if (kit::formTabs("forms", kTabs, kTabs, 4, &f, kTips)) setForm((Form)f);
    drawSubPages();
    static const char* const kSizePages[] = {"size_cylinder", "size_pump", "size_motor", "size_drive"};
    static const char* const kResultPages[] = {"outcome", "bom", "piping"};
    const char* page = form_ == Form::Circuit ? "circuit"
                       : form_ == Form::Design ? kSizePages[(int)sizeKind_]
                       : form_ == Form::Results ? kResultPages[(int)resultPage_] : "simulate";
    kit::panelBodyBegin(page);
    ImGui::BeginDisabled(busy());
    if (confirmDelete_) {
        const std::string question = "Delete \"" + title + "\"? Its inputs and results are removed from this document.";
        kit::inlineStatus(question.c_str(), kit::Tone::Warn);
        if (kit::dangerButton("Delete circuit###del_yes", !busy())) {
            store_.deleteRecord(st, slot_);
            adopt({{"params", json::object()}, {"results", json::object()}});
            cylTarget_.clear();
            pumpTarget_.clear();
            motorTarget_.clear();
            driveTarget_.clear();
            selected_ = selectedHose_ = -1;
            setForm(Form::Circuit);
            startVisible_ = true;
            setStatus("Circuit deleted.", 0);
        }
        ImGui::SameLine();
        if (kit::secondaryButton("Cancel###del_no")) confirmDelete_ = false;
        kit::gapRow();
    }
    switch (form_) {
        case Form::Circuit: drawCircuitForm(st); break;
        case Form::Design: drawSizingForm(st); break;
        case Form::Simulate: drawSimulateForm(st); break;
        case Form::Results: drawResultsForm(st); break;
    }
    ImGui::EndDisabled();
    kit::panelBodyEnd();
    if (circuit_.is_object()) {
        kit::panelFooterBegin();
        drawFooter(st);
    }
    kit::panelEnd();
    if (done) close();
}

void HydraulicPanel::drawFooter(AppState& st) {
    const bool running = busy();
    if (!running) kit::footerStatus(store_.status().c_str(), sim::toneOf(store_.statusTone()));
    kit::ActionSpec a;
    a.running = running;
    a.stage = templatesPending_ ? "Loading starting circuits" : store_.status().c_str();
    a.elapsed = running ? ImGui::GetTime() - callStart_ : -1.0;
    a.cancellable = false;
    a.cancelBlocker = "The engine finishes this request; it cannot be interrupted.";
    std::string blocker;
    const NextStep next = circuitNextStep();
    const bool handedOff = handoff_.is_object() && handoff_.contains("piping_doc");
    switch (form_) {
        case Form::Circuit:
            a.primaryLabel = next == NextStep::Add ? "Add component"
                             : next == NextStep::Connect ? "Connect ports" : "Go to Simulate";
            break;
        case Form::Design:
            switch (sizeKind_) {
                case SizeKind::Cylinder: a.primaryLabel = "Size cylinder"; blocker = cylinderBlocker(); break;
                case SizeKind::Pump: a.primaryLabel = "Size pump"; blocker = pumpBlocker(); break;
                case SizeKind::Motor: a.primaryLabel = "Size motor"; blocker = motorBlocker(); break;
                case SizeKind::Drive: a.primaryLabel = "Size drive"; blocker = driveBlocker(); break;
            }
            break;
        case Form::Simulate:
            a.primaryLabel = "Run simulation";
            blocker = simulateBlocker();
            break;
        case Form::Results:
            switch (resultPage_) {
                case ResultPage::Outcome:
                    a.primaryLabel = sim_.is_object() && !sim_.empty() ? "Re-run simulation" : "Run simulation";
                    blocker = simulateBlocker();
                    break;
                case ResultPage::Bom:
                    a.primaryLabel = bom_.is_object() && !bom_.empty() ? "Rebuild bill of materials" : "Build bill of materials";
                    a.primaryAsSecondary = bom_.is_object() && !bom_.empty();
                    blocker = bomBlocker();
                    break;
                case ResultPage::Piping:
                    if (handedOff) {
                        a.primaryLabel = "Open in Piping";
                        a.secondaryLabel = "Regenerate";
                        a.secondaryEnabled = handoffBlocker().empty();
                        a.secondaryReason = a.secondaryEnabled ? nullptr : "Fix the inputs first.";
                    } else {
                        a.primaryLabel = "Generate network";
                        blocker = handoffBlocker();
                    }
                    break;
            }
            break;
    }
    a.primaryEnabled = blocker.empty();
    a.blocker = blocker.empty() ? nullptr : blocker.c_str();
    switch (kit::actionFooter("action", a)) {
        case kit::FooterAction::Primary:
            switch (form_) {
                case Form::Circuit:
                    if (next == NextStep::Add) {
                        startVisible_ = false;
                        setCircuitPage(CircuitPage::Add);
                    } else if (next == NextStep::Connect) handleCommand(st, "sim_hyd_hose");
                    else setForm(Form::Simulate);
                    break;
                case Form::Design:
                    switch (sizeKind_) {
                        case SizeKind::Cylinder: runSizeCylinder(st); break;
                        case SizeKind::Pump: runSizePump(st); break;
                        case SizeKind::Motor: runSizeMotor(st); break;
                        case SizeKind::Drive: runPumpDrive(st); break;
                    }
                    break;
                case Form::Simulate: runSimulate(st); break;
                case Form::Results:
                    if (resultPage_ == ResultPage::Outcome) runSimulate(st);
                    else if (resultPage_ == ResultPage::Bom) runBom(st);
                    else if (handedOff) pipingOpenReq_ = true;
                    else runPipingHandoff(st);
                    break;
            }
            break;
        case kit::FooterAction::Secondary:
            if (form_ == Form::Results && resultPage_ == ResultPage::Piping) runPipingHandoff(st);
            break;
        default: break;
    }
}

std::string HydraulicPanel::selectedComponentId() const {
    const auto& components = jarr(circuit_, "components");
    return selected_ >= 0 && selected_ < (int)components.size() ? jstr(components[selected_], "id") : std::string{};
}

void HydraulicPanel::selectComponent3d(const std::string& id) {
    selectFromCanvas(componentIndex(id), -1);
}

void HydraulicPanel::armPlacement3d(bool armed) {
    placing3d_ = armed && !busy() && !selectedComponentId().empty();
    if (placing3d_) setCentralView(CentralView::View3D);
}

void HydraulicPanel::ensureDefaultLayout3d() const {
    if (defaultCircuitRevision_ == circuitGeneration_ && defaultLayoutRevision_ == layoutGeneration_) return;
    defaultCircuitRevision_ = circuitGeneration_;
    defaultLayoutRevision_ = layoutGeneration_;
    defaultLayout3d_.clear();
    struct Entry {
        std::string id;
        std::array<float, 3> position{}, saved{}, lo{}, hi{};
        bool explicitPosition = false;
    };
    std::vector<Entry> entries;
    const auto& components = jarr(circuit_, "components");
    entries.reserve(components.size());
    for (const auto& component : components) {
        Entry e;
        e.id = jstr(component, "id");
        componentEnvelope3d(component, e.lo.data(), e.hi.data());
        const auto& xy = jarr(component, "position");
        for (int k = 0; k < 2; ++k) {
            const float value = xy.size() > (std::size_t)k && xy[k].is_number() ? xy[k].get<float>() : 0;
            e.position[k] = std::isfinite(value) ? value : 0;
        }
        const auto it = layout3d_.find(e.id);
        if (it != layout3d_.end() && it->is_array() && it->size() == 3 &&
            (*it)[0].is_number() && (*it)[1].is_number() && (*it)[2].is_number()) {
            for (int k = 0; k < 3; ++k) e.saved[k] = (*it)[k].get<float>();
            e.explicitPosition = std::isfinite(e.saved[0]) && std::isfinite(e.saved[1]) && std::isfinite(e.saved[2]);
        }
        entries.push_back(std::move(e));
    }
    // A single schematic-to-world scale preserves the drawing's relative ordering. For each
    // pair it need only separate ONE XY axis, using asymmetric, rotated glyph envelopes.
    constexpr float clearanceMm = 40;
    float scale = 2;
    for (std::size_t i = 0; i < entries.size(); ++i) {
        for (std::size_t j = i + 1; j < entries.size(); ++j) {
            float required = FLT_MAX;
            for (int k = 0; k < 2; ++k) {
                const float delta = entries[j].position[k] - entries[i].position[k];
                if (delta > 0)
                    required = std::min(required, (entries[i].hi[k] - entries[j].lo[k] + clearanceMm) / delta);
                else if (delta < 0)
                    required = std::min(required, (entries[j].hi[k] - entries[i].lo[k] + clearanceMm) / -delta);
            }
            if (required != FLT_MAX) scale = std::max(scale, required);
        }
    }
    for (auto& e : entries) {
        if (e.explicitPosition) e.position = e.saved;
        else for (int k = 0; k < 2; ++k) e.position[k] *= scale;
    }
    // Explicitly authored positions are never moved. Coincident schematic symbols and new
    // defaults beside an authored glyph are packed to the right until their envelopes clear.
    std::vector<const Entry*> placed;
    placed.reserve(entries.size());
    for (const auto& e : entries) if (e.explicitPosition) placed.push_back(&e);
    for (auto& e : entries) {
        if (e.explicitPosition) continue;
        bool moved;
        do {
            moved = false;
            for (const auto* other : placed) {
                bool overlap = true;
                for (int k = 0; k < 3; ++k)
                    if (e.position[k] + e.hi[k] + clearanceMm <= other->position[k] + other->lo[k] ||
                        other->position[k] + other->hi[k] + clearanceMm <= e.position[k] + e.lo[k])
                        overlap = false;
                if (overlap) {
                    e.position[0] = std::nextafter(std::max(e.position[0],
                        other->position[0] + other->hi[0] + clearanceMm - e.lo[0]), FLT_MAX);
                    moved = true;
                }
            }
        } while (moved);
        placed.push_back(&e);
        defaultLayout3d_.emplace(e.id, e.position);
    }
}

void HydraulicPanel::componentPosition3d(const nlohmann::json& component, float position[3]) const {
    const auto it = layout3d_.find(jstr(component, "id"));
    if (it != layout3d_.end() && it->is_array() && it->size() == 3 &&
        (*it)[0].is_number() && (*it)[1].is_number() && (*it)[2].is_number()) {
        for (int k = 0; k < 3; ++k) position[k] = (*it)[k].get<float>();
        if (std::isfinite(position[0]) && std::isfinite(position[1]) && std::isfinite(position[2])) return;
    }
    ensureDefaultLayout3d();
    const auto fallback = defaultLayout3d_.find(jstr(component, "id"));
    for (int k = 0; k < 3; ++k)
        position[k] = fallback != defaultLayout3d_.end() ? fallback->second[k] : 0.0f;
}

bool HydraulicPanel::moveComponent3d(AppState& st, const std::string& id, const float position[3]) {
    if (busy() || componentIndex(id) < 0) return false;
    for (int k = 0; k < 3; ++k) if (!std::isfinite(position[k])) return false;
    const json next = json::array({position[0], position[1], position[2]});
    const auto current = layout3d_.find(id);
    if (current != layout3d_.end() && *current == next) return true;
    layout3d_[id] = next;
    commitCircuitMutation(st, false); // physical placement leaves lumped-parameter results valid
    return true;
}

}  // namespace ui

