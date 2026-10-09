// PipingPanel implementation — see piping_panel.h.
#include "piping_panel.h"

#include <algorithm>
#include <cctype>
#include <cfloat>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <random>
#include <utility>
#include <climits>
#include <limits>
#include <unordered_map>

#include "imgui.h"

#include "../core/app_state.h"
#include "../core/sim_client.h"
#include "../ui_theme.h"
#include "engine_backend.h"  // engine::daemonMode — server-owned export paths
#include "file_dialog.h"
#include "legend.h"
#include "sim_manager.h"   // SimManager::unitSys — the Simulation pane's display unit system
#include "viewport_panel.h"
#include "widgets/kit.h"
#include "sim/sim_interaction.h"

namespace ui {

using namespace sim;

namespace {

using json = nlohmann::json;

// Display units (one unit per quantity, everywhere in this panel). The engine and every stored
// member stay SI: pipe lengths, coordinates and walls in mm (the network's MMGS units), support
// spans in m, pressures in kPa gauge, flows in m³/h; duct systems in HVAC units (m, m³/s, Pa).
// The DISPLAY and INPUT unit of each quantity follow the Simulation pane's unit system
// (PipingPanel::syncUnits): SI shows those native units, US customary converts exactly (Q below).

// aicad.piping/1 SCHEDULES / MATERIALS members offered by the router form (contracts/piping_schema.md).
const char* const kSchedules[] = {"40", "80", "10", "160", "STD", "XS", "XXS", "5S", "10S", "40S", "80S",
                                 "5", "20", "30", "60", "100", "120", "140"};
const char* const kPipeMaterials[] = {"A106-B", "A53-B", "A312-304", "A312-316", "A333-6", "PVC", "CPVC", "copper"};
// piping fluid_properties built-ins (water is the liquid; the rest solve as gas networks).
const char* const kFluidKeys[] = {"water", "air", "methane", "natural_gas", "nitrogen", "carbon_dioxide"};
const char* const kFluidLabels[] = {"Water", "Air", "Methane", "Natural gas", "Nitrogen", "Carbon dioxide"};
// Boundary kinds per network type (index = Boundary::kind).
const char* const kLiquidKinds[] = {"Pressure", "Head", "Demand"};
const char* const kLiquidKeys[] = {"pressure", "head", "demand"};
const char* const kGasKinds[] = {"Pressure", "Mass demand"};
const char* const kGasKeys[] = {"pressure", "mass_demand"};
const char* const kEnds[] = {"Start", "End"};
const char* const kVesselMaterials[] = {"SA-516-70", "SA-240-304", "SA-240-316", "SA-285-C"};
const char* const kHeadKeys[] = {"ellipsoidal_2_1", "hemispherical", "torispherical"};
const char* const kHeadLabels[] = {"2:1 ellipsoidal", "Hemispherical", "Torispherical"};
const char* const kDuctShapes[] = {"Round", "Rectangular"};

using Form = PipingPanel::Form;

// Network form tabs in the Piping ribbon's order; the full labels are the ribbon's command words,
// the short ones only show when the pane is too narrow for them (tooltip = the full purpose).
// Vessel and Duct are separate records: they have no network tabs.
const Form kTabForms[] = {Form::Network, Form::Route, Form::Fittings, Form::Solve, Form::Spec, Form::Flow, Form::Bom};
const char* const kTabFull[] = {"Network", "Draw Pipe", "Fittings", "Solve", "Spec Check", "Internal CFD", "BOM"};
const char* const kTabShort[] = {"Network", "Draw", "Fittings", "Solve", "Spec", "CFD", "BOM"};
const char* const kTabTips[] = {
    "The network: name, design conditions and its pipe runs.",
    "Draw on a working plane or auto-route around this model's bodies.",
    "Place fittings and edit deterministic or CFD-derived loss coefficients.",
    "Solve the network's 1-D hydraulics; size the pump drive.",
    "Check every run and flange against ASME B31.3.",
    "Internal-flow CFD through a run's bore or a fluid-volume STL.",
    "Line list, material take-off and shop spools."};
// Scroll-position key per form (index = (int)Form); the page index is appended.
const char* const kFormPage[] = {"network", "route", "solve", "spec", "bom", "flow", "vessel", "duct", "fittings"};

// Long forms page their inputs and their results (pinned switch under the tabs).
struct Pages {
    const char* const* labels = nullptr;
    const char* const* tips = nullptr;
    int count = 1;       // 1 = the form is one page
    int results = -1;    // index of the Results page (-1 = none)
};
const char* const kSolvePages[] = {"Conditions", "Results", "Pump drive"};
const char* const kSolveTips[] = {"Fluid and boundary conditions.", "Flows and pressures of the last solve.",
                                  "Size the electric motor for the pump's duty point."};
const char* const kSpecPages[] = {"Design basis", "Results"};
const char* const kSpecTips[] = {"Design conditions and allowances.", "Wall, MAWP, flange and span checks."};
const char* const kSetupPages[] = {"Setup", "Results"};
const char* const kFlowTips[] = {"Fluid domain, ports and flow.", "Pressure drop and its 1-D comparison."};
const char* const kVesselTips[] = {"Geometry, design conditions and contents.", "Shell, heads and masses."};
const char* const kDuctPages[] = {"Segments", "Design", "Results"};
const char* const kDuctTips[] = {"The duct tree: one card per segment.", "Equal-friction design inputs.",
                                 "Sizes and the fan static pressure."};
Pages pagesOf(Form f) {
    switch (f) {
        case Form::Solve: return {kSolvePages, kSolveTips, 3, 1};
        case Form::Spec: return {kSpecPages, kSpecTips, 2, 1};
        case Form::Flow: return {kSetupPages, kFlowTips, 2, 1};
        case Form::Vessel: return {kSetupPages, kVesselTips, 2, 1};
        case Form::Duct: return {kDuctPages, kDuctTips, 3, 2};
        default: return {};
    }
}

// True when `text` contains `word`, ignoring ASCII case.
bool containsNoCase(const std::string& text, const char* word) {
    std::string a = text, b = word;
    for (auto& c : a) c = (char)std::tolower((unsigned char)c);
    for (auto& c : b) c = (char)std::tolower((unsigned char)c);
    return a.find(b) != std::string::npos;
}

const char* const kEmDash = "\xe2\x80\x94";

json vec3(const double v[3]) { return json::array({v[0], v[1], v[2]}); }

double norm3(const double v[3]) { return std::sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]); }
bool normalize3(double v[3]) {
    const double n = norm3(v);
    if (n < 1e-12) return false;
    for (int k = 0; k < 3; ++k) v[k] /= n;
    return true;
}

// Readout of numeric member `k` with significant digits (em dash when absent).
std::string val(const json& o, const char* k, int sig = 4) {
    return jhas(o, k) ? kit::fmtValue(jnum(o, k), sig) : std::string(kEmDash);
}
// Readout of numeric member `k` with its unit (em dash when absent).
std::string qty(const json& o, const char* k, const char* unit, int sig = 4) {
    return jhas(o, k) ? kit::fmtQuantity(jnum(o, k), unit, sig) : std::string(kEmDash);
}
const char* U(kit::Unit u) { return kit::unitText(u); }

// One display quantity: its SI (stored) unit and its US customary unit with the exact factor
// (display = stored * scale + offset). Lengths/coordinates mm -> in; spans and heads m -> ft;
// pressures kPa -> psi; small pipe pressure drops Pa -> psi; duct pressures Pa -> in. w.g.
// (conventional inch of water, 249.08891 Pa); flows m³/h -> US gpm; airflow m³/s -> cfm.
enum class Q {
    Len, Span, Head, Press, PressPa, DuctPa, Friction, Temp, Flow, Airflow, Vel, DuctVel,
    Dens, Visc, Mass, MassFlow, Vol, Area, LineMass
};
struct Disp { const char* unit; double scale; double offset; };
Disp disp(Q q, bool us) {
    using kit::Unit;
    constexpr double kPsiKpa = 6.894757293168;          // kPa per psi
    constexpr double kFt = 0.3048;                      // m per ft
    constexpr double kLb = 0.45359237;                  // kg per lb
    constexpr double kGal = 3.785411784e-3;             // m³ per US gal
    constexpr double kInWg = 249.08891;                 // Pa per inch of water
    switch (q) {
        case Q::Len: return us ? Disp{U(Unit::in), 1.0 / 25.4, 0} : Disp{U(Unit::mm), 1, 0};
        case Q::Span:
        case Q::Head: return us ? Disp{U(Unit::ft), 1.0 / kFt, 0} : Disp{U(Unit::m), 1, 0};
        case Q::Press: return us ? Disp{U(Unit::psi), 1.0 / kPsiKpa, 0} : Disp{U(Unit::kPa), 1, 0};
        case Q::PressPa: return us ? Disp{U(Unit::psi), 1.0 / (kPsiKpa * 1000.0), 0} : Disp{U(Unit::Pa), 1, 0};
        case Q::DuctPa: return us ? Disp{"in. w.g.", 1.0 / kInWg, 0} : Disp{U(Unit::Pa), 1, 0};
        case Q::Friction: return us ? Disp{"in. w.g./100 ft", 100.0 * kFt / kInWg, 0} : Disp{"Pa/m", 1, 0};
        case Q::Temp: return us ? Disp{U(Unit::DegF), 1.8, 32.0} : Disp{U(Unit::DegC), 1, 0};
        case Q::Flow: return us ? Disp{U(Unit::gpm), 1.0 / (3600.0 * kGal / 60.0), 0} : Disp{U(Unit::m3_h), 1, 0};
        case Q::Airflow: return us ? Disp{"cfm", 60.0 / (kFt * kFt * kFt), 0} : Disp{U(Unit::m3_s), 1, 0};
        case Q::Vel: return us ? Disp{"ft/s", 1.0 / kFt, 0} : Disp{U(Unit::m_s), 1, 0};
        case Q::DuctVel: return us ? Disp{"fpm", 60.0 / kFt, 0} : Disp{U(Unit::m_s), 1, 0};
        case Q::Dens: return us ? Disp{"lb/ft\xc2\xb3", kFt * kFt * kFt / kLb, 0} : Disp{U(Unit::kg_m3), 1, 0};
        // US practice quotes dynamic viscosity in centipoise (1 cP = 1 mPa·s).
        case Q::Visc: return us ? Disp{"cP", 1000.0, 0} : Disp{U(Unit::Pa_s), 1, 0};
        case Q::Mass: return us ? Disp{U(Unit::lb), 1.0 / kLb, 0} : Disp{U(Unit::kg), 1, 0};
        case Q::MassFlow: return us ? Disp{"lb/h", 1.0 / kLb, 0} : Disp{"kg/h", 1, 0};
        case Q::Vol: return us ? Disp{"gal", 1.0 / kGal, 0} : Disp{U(Unit::m3), 1, 0};
        case Q::Area: return us ? Disp{U(Unit::in2), 1.0 / 645.16, 0} : Disp{U(Unit::mm2), 1, 0};
        case Q::LineMass: return us ? Disp{"lb/ft", kFt / kLb, 0} : Disp{"kg/m", 1, 0};
    }
    return {"", 1, 0};
}
double toDisp(double si, Q q, bool us) {
    const Disp d = disp(q, us);
    return si * d.scale + d.offset;
}
const char* unitOf(Q q, bool us) { return disp(q, us).unit; }
// A stored SI value as display text with its unit.
std::string fmtQ(double si, Q q, bool us, int sig = 4) { return kit::fmtQuantity(toDisp(si, q, us), unitOf(q, us), sig); }
// Numeric member `k` (stored SI) as display text with its unit (em dash when absent).
std::string qtyQ(const json& o, const char* k, Q q, bool us, int sig = 4) {
    return jhas(o, k) ? fmtQ(jnum(o, k), q, us, sig) : std::string(kEmDash);
}
// The number alone (table cells under a "Title (unit)" header).
std::string valQ(const json& o, const char* k, Q q, bool us, int sig = 4) {
    return jhas(o, k) ? kit::fmtValue(toDisp(jnum(o, k), q, us), sig) : std::string(kEmDash);
}
// "Title (unit)" table header.
std::string colQ(const char* title, Q q, bool us) { return std::string(title) + " (" + unitOf(q, us) + ")"; }
// Field options that edit a stored SI value in the display unit.
kit::FieldOpts fieldQ(Q q, bool us) {
    const Disp d = disp(q, us);
    return kit::FieldOpts().withUnit(d.unit).withScale(d.scale, d.offset);
}
// Placeholder text of an SI default, in the display unit.
std::string hintQ(double si, Q q, bool us) { return kit::fmtValue(toDisp(si, q, us), 3); }

// "NPS 2", "NPS 1-1/2", "NPS 3/4": the nominal pipe size as its standard designator, the same
// spelling on every form (NPS is a size designator, not a measured inch dimension).
std::string npsText(double nps) {
    if (!(nps > 0.0)) return "NPS " + std::string(kEmDash);
    static const struct { double v; const char* t; } kFrac[] = {
        {0.125, "1/8"}, {0.25, "1/4"}, {0.375, "3/8"}, {0.5, "1/2"}, {0.75, "3/4"}};
    const double whole = std::floor(nps + 1e-9);
    const double frac = nps - whole;
    if (frac < 1e-6) return "NPS " + std::to_string((long long)whole);
    for (const auto& f : kFrac)
        if (std::fabs(frac - f.v) < 1e-6)
            return "NPS " + (whole > 0 ? std::to_string((long long)whole) + "-" : std::string()) + f.t;
    return "NPS " + kit::fmtValue(nps, 4);
}
std::string npsOf(const json& o, const char* k = "nps") {
    return jhas(o, k) ? npsText(jnum(o, k)) : "NPS " + fmtText(o, k);
}

double pathLengthMm(const json& path) {
    double len = 0.0, a[3], b[3];
    for (std::size_t i = 1; i < path.size(); ++i)
        if (jvec3(path[i - 1], a) && jvec3(path[i], b)) {
            const double d[3] = {b[0] - a[0], b[1] - a[1], b[2] - a[2]};
            len += norm3(d);
        }
    return len;
}

// "Run 3" for the engine's run id "r3" (any other id verbatim): users never read the raw token.
std::string runName(const std::string& id) {
    if (id.size() >= 2 && id[0] == 'r' &&
        std::all_of(id.begin() + 1, id.end(), [](char c) { return std::isdigit((unsigned char)c) != 0; }))
        return "Run " + id.substr(1);
    return id;
}

// "Run 1 · Supply header" — a run's display name plus its optional user name.
std::string runLabel(const json& r) {
    const std::string name = jstr(r, "name");
    const std::string base = runName(jstr(r, "id"));
    return name.empty() ? base : base + " \xc2\xb7 " + name;
}

// Engine tokens ("elbow_90_lr", "laminar") as sentence-case words ("Elbow 90 lr", "Laminar").
std::string humanToken(std::string s) {
    for (auto& c : s)
        if (c == '_') c = ' ';
    if (!s.empty()) s[0] = (char)std::toupper((unsigned char)s[0]);
    return s;
}

const char* const kValveKeys[] = {"gate", "globe", "angle", "ball", "plug", "butterfly", "check_swing", "check_lift"};
const char* const kValveLabels[] = {"Gate", "Globe", "Angle", "Ball", "Plug", "Butterfly", "Swing check", "Lift check"};
const char* const kFittingLabels[] = {"Elbow 90", "Elbow 45", "Tee / splitter", "Valve", "Reducer", "Orifice", "Flange", "Custom"};
const char* const kFittingTypes[] = {"elbow", "elbow", "tee", "valve", "reducer", "orifice", "flange", "custom"};
const char* const kLossSources[] = {"Correlation", "Constant K", "2-K (Hooper)", "3-K (Darby)", "Cv", "From CFD"};
// A solver fluid's display name: the built-in's label ("water" -> "Water"), else the name as
// given (a handed-off oil keeps its own name).
std::string fluidName(const std::string& key) {
    for (int i = 0; i < countOf(kFluidKeys); ++i)
        if (key == kFluidKeys[i]) return kFluidLabels[i];
    return key.empty() ? std::string(kEmDash) : humanToken(key);
}

// Display name of a pump-drive key: a run id ("r1" -> "Run 1"), the fixed supply node, or the
// manual duty point.
std::string driveRunName(const std::string& key) {
    if (key == "supply") return "Supply";
    if (key == "manual") return "Manual duty point";
    return runName(key);
}

// The record a header Delete removes (PipingPanel::deleteKind: 1 network, 2 vessel, 3 duct).
const char* deleteNoun(int kind) {
    return kind == 1 ? "network" : kind == 2 ? "vessel" : "duct system";
}


std::string newUuid() {
    std::random_device rd;
    std::mt19937_64 g((static_cast<std::uint64_t>(rd()) << 32) ^ rd());
    const std::uint64_t a = g(), b = g();
    char buf[48];
    std::snprintf(buf, sizeof buf, "%08llx-%04llx-4%03llx-%04llx-%012llx",
                  (unsigned long long)(a >> 32), (unsigned long long)((a >> 16) & 0xffff),
                  (unsigned long long)(a & 0x0fff), (unsigned long long)(((b >> 48) & 0x3fff) | 0x8000),
                  (unsigned long long)(b & 0xffffffffffffull));
    return buf;
}

json newNetworkDoc(const std::string& name, const char* material) {
    return {{"schema", "aicad.piping/1"},
            {"id", newUuid()},
            {"name", name},
            {"units", {{"system", "MMGS"}}},
            {"material", material},
            {"runs", json::array()},
            {"fittings", json::array()}};
}

const char* recordIcon(const std::string& type) {
    if (type == "piping_network") return "study_piping";
    if (type == "pipe_flow") return "study_flow";
    if (type == "vessel") return "vessel";
    return "duct";
}

std::string recordSummary(const json& rec, bool us) {
    const std::string type = jstr(rec, "type");
    const json& params = jobj(rec, "params");
    const json& results = jobj(rec, "results");
    if (type == "piping_network") {
        const json& runs = jarr(jobj(params, "piping"), "runs");
        double lenMm = 0.0;
        for (const auto& r : runs) lenMm += pathLengthMm(jarr(r, "path"));
        std::string s = countNoun((long long)runs.size(), "run") + " \xc2\xb7 " + fmtQ(lenMm, Q::Len, us, 3);
        if (results.contains("solve")) s += " \xc2\xb7 solved";
        if (results.contains("spec_check"))
            s += jbool(jobj(results, "spec_check"), "all_pass", false) ? " \xc2\xb7 B31.3 pass"
                                                                         : " \xc2\xb7 B31.3 fail";
        if (results.contains("bom")) s += " \xc2\xb7 BOM";
        if (!jobj(results, "pump_drive").empty()) s += " \xc2\xb7 pump drive";
        return s;
    }
    if (type == "pipe_flow") {
        if (results.empty()) return "not run";
        return "Pressure drop " + qtyQ(results, "dp_pa", Q::PressPa, us) + " \xc2\xb7 Reynolds " +
               val(results, "reynolds", 3) + " \xc2\xb7 " + jstr(results, "regime", kEmDash);
    }
    if (type == "vessel") {
        if (results.empty()) return "not sized";
        return "shell " + qtyQ(jobj(results, "shell"), "t_nominal_mm", Q::Len, us, 3) + " \xc2\xb7 MAWP " +
               qtyQ(jobj(results, "shell"), "mawp_kpa", Q::Press, us);
    }
    if (results.empty()) return "not sized";
    return "fan static " + qtyQ(results, "fan_static_pa", Q::DuctPa, us) + " \xc2\xb7 " +
           countNoun((long long)jarr(results, "segments").size(), "segment");
}

// Default value (stored SI) and display quantity of a new boundary row of `kind`.
double boundaryDefault(bool gas, int kind) {
    if (gas) return kind == 0 ? 500.0 : 50.0;
    return kind == 0 ? 300.0 : kind == 1 ? 30.0 : 10.0;
}
Q boundaryQ(bool gas, int kind) {
    if (kind == 0) return Q::Press;
    if (gas) return Q::MassFlow;
    return kind == 1 ? Q::Head : Q::Flow;
}
const char* boundaryIcon(bool gas, int kind) {
    return kind == 0 || (!gas && kind == 1) ? "load_pressure" : "flow_arrow";
}

}  // namespace

// ============================================================================================
// construction / record plumbing
// ============================================================================================

PipingPanel::PipingPanel()
    : store_({"[piping]", "Open a part to save this result inside a document.",
              "Could not save the result into the document; retry.",
              "Could not delete the study from the document.", 120000}),
      alive_(std::make_shared<int>(0)) {
    driveIn_.kind = 1;   // network pumps are centrifugal: duty is flow + head
}

bool PipingPanel::isPipingStudyType(const std::string& type) {
    return type == "piping_network" || type == "pipe_flow" || type == "vessel" ||
           type == "duct_system";
}

void PipingPanel::syncUnits(const AppState& st) {
    const bool us = SimManager::unitSys(st).us;
    if (us == us_) return;
    us_ = us;
    // Cached display text follows: study-row summaries now, run rows on their next refresh.
    for (auto& row : studyRows_)
        for (const json& rec : store_.records())
            if (jstr(rec, "id") == row.id) { row.summary = recordSummary(rec, us_); break; }
    // The drawing grid's default pitch reads as a round number in either system (50 mm / 2 in);
    // a pitch the user typed is kept.
    if (us_ && draw_.gridMm == 50.0) draw_.gridMm = 50.8;
    else if (!us_ && draw_.gridMm == 50.8) draw_.gridMm = 50.0;
}

const char* PipingPanel::lengthUnit() const { return unitOf(Q::Len, us_); }

void PipingPanel::setStudies(AppState& st, const std::string& docId,
                             const std::vector<nlohmann::json>& records) {
    if (mirroredDocId_ != docId) {
        ++networkGeneration_; ++flowGeneration_; ++vesselGeneration_; ++ductGeneration_;
        ++fluidDisplayGeneration_[0]; ++fluidDisplayGeneration_[1];
        mirroredDocId_ = docId;
    }
    store_.setRecords(docId, records);
    syncUnits(st);
    studyRows_.clear();
    studyRows_.reserve(records.size());
    for (const auto& record : records)
        studyRows_.push_back({jstr(record, "id"), jstr(record, "name", jstr(record, "type")), recordSummary(record, us_)});
    store_.sync(netSlot_, "piping_network", jarr(network_, "runs").empty(),
                [this, &st](const json& r) { adoptNetwork(st, r); });
    store_.sync(vesselSlot_, "vessel", vessel_.is_null(), [this](const json& r) { adoptVessel(r); });
    store_.sync(ductSlot_, "duct_system", duct_.is_null(), [this](const json& r) { adoptDuct(r); });
    // The flow slot follows the run it was launched for; it never adopts an arbitrary record.
    if (store_.sync(flowSlot_, nullptr, false, [this, &st](const json& r) { adoptFlow(st, r); }) ==
        StudyRecordStore::Sync::Forgotten)
        flowSlotKey_.clear();
}

void PipingPanel::adoptNetwork(AppState& st, const nlohmann::json& rec) {
    const auto oldRun = selectedRunId();
    const auto oldFitting = selectedFitting_;
    ++networkGeneration_; ++networkRevision_;
    netSlot_.bind(rec);
    ++endpointGeneration_[0]; ++endpointGeneration_[1];
    endpointResolving_[0] = endpointResolving_[1] = false;
    endpointFace_[0] = endpointFace_[1] = json();
    const json& p = jobj(rec, "params");
    networkParams_ = p;
    const json& piping = jobj(p, "piping");
    network_ = piping.empty() ? newNetworkDoc(jstr(rec, "name", "Pipe network"), kPipeMaterials[0]) : piping;
    const json& design = jobj(p, "design");
    designPressureKpa_ = jnum(design, "pressure_kpa", designPressureKpa_);
    designTempC_ = jnum(design, "temp_c", designTempC_);
    const json& fluid = jobj(p, "fluid");
    fluidIdx_ = indexOf(kFluidKeys, countOf(kFluidKeys), jstr(fluid, "name", "water"));
    fluidTempC_ = jhas(fluid, "T") ? jnum(fluid, "T") - 273.15 : jnum(fluid, "T_C", 20.0);
    fluidOverride_ = jhas(fluid, "rho") && jhas(fluid, "mu") ? json(fluid) : json();
    resolveFluidDisplay(st, false);
    const json& runs = jarr(network_, "runs");
    boundary_.clear();
    for (const auto& b : jarr(p, "boundary")) {
        Boundary row;
        row.native = b;
        const std::string runId = jstr(b, "run");
        row.run = -1;
        for (int i = 0; i < (int)runs.size(); ++i)
            if (jstr(runs[i], "id") == runId) row.run = i;
        row.end = jstr(b, "end") == "end" ? 1 : 0;
        if (jhas(b, "pressure")) { row.kind = 0; row.value = jnum(b, "pressure"); }
        else if (jhas(b, "head")) { row.kind = 1; row.value = jnum(b, "head"); }
        else if (jhas(b, "demand")) { row.kind = 2; row.value = jnum(b, "demand"); }
        else if (jhas(b, "demand_lps")) { row.kind = 2; row.value = jnum(b, "demand_lps") * 3.6; }
        else if (jhas(b, "mass_demand")) { row.kind = 1; row.value = jnum(b, "mass_demand"); }
        else row.run = -1;
        boundary_.push_back(row);
    }
    const json& specIn = jobj(p, "spec_inputs");
    corrosionMm_ = jnum(specIn, "corrosion_allowance_mm", corrosionMm_);
    millTol_ = jnum(specIn, "mill_tolerance_frac", millTol_);
    jointFactor_ = jnum(specIn, "joint_factor", jointFactor_);
    fluidDensity_ = jnum(specIn, "fluid_density_kgm3", fluidDensity_);
    insulationKgm_ = jnum(specIn, "insulation_kgm", insulationKgm_);
    maxSpoolMm_ = jnum(p, "max_spool_length_mm", maxSpoolMm_);
    const json& res = jobj(rec, "results");
    route_ = res.contains("route") ? res["route"] : json();
    solve_ = res.contains("solve") ? res["solve"] : json();
    spec_ = res.contains("spec_check") ? res["spec_check"] : json();
    bom_ = res.contains("bom") ? res["bom"] : json();
    pumpDrive_ = jobj(res, "pump_drive");
    selectRun(st, oldRun);
    if (!oldFitting.empty()) selectFitting(st, oldFitting);
    if (selectedRun_ < 0 && !runs.empty()) selectedRun_ = 0;
    routeSeeded_ = false;
    const auto& inputs = jobj(p, "route_inputs");
    if (jvec3(jobj(inputs, "start").value("point", json()), start_) &&
        jvec3(jobj(inputs, "end").value("point", json()), end_)) {
        jvec3(jobj(inputs, "start").value("dir", json()), startDir_);
        jvec3(jobj(inputs, "end").value("dir", json()), endDir_);
        endpointFace_[0] = jobj(inputs, "start").value("face", json());
        endpointFace_[1] = jobj(inputs, "end").value("face", json());
        nps_ = jnum(inputs, "nps", nps_);
        clearanceMm_ = jnum(inputs, "clearance_mm", clearanceMm_);
        gridMm_ = jnum(inputs, "grid_mm", 0);
        runName_ = jstr(inputs, "name");
        const int schedule = indexOf(kSchedules, countOf(kSchedules), jstr(inputs, "schedule"));
        const int material = indexOf(kPipeMaterials, countOf(kPipeMaterials), jstr(inputs, "material"));
        if (schedule >= 0) scheduleIdx_ = schedule;
        if (material >= 0) materialIdx_ = material;
        routeSeeded_ = true;
    }
}

void PipingPanel::adoptVessel(const nlohmann::json& rec) {
    ++vesselGeneration_;
    vesselSlot_.bind(rec);
    const json& p = jobj(rec, "params");
    vesselIdMm_ = jnum(p, "inner_diameter_mm", vesselIdMm_);
    if (jhas(p, "volume_m3")) {
        vesselSizeBy_ = 1;
        vesselVolM3_ = jnum(p, "volume_m3");
    } else {
        vesselSizeBy_ = 0;
        vesselTtMm_ = jnum(p, "tan_tan_length_mm", vesselTtMm_);
    }
    vesselPKpa_ = jnum(p, "design_pressure_kpa", vesselPKpa_);
    vesselTC_ = jnum(p, "design_temp_c", vesselTC_);
    const int mi = indexOf(kVesselMaterials, countOf(kVesselMaterials), jstr(p, "material"));
    if (mi >= 0) vesselMaterial_ = mi;
    vesselCaMm_ = jnum(p, "corrosion_allowance_mm", vesselCaMm_);
    vesselE_ = jnum(p, "joint_efficiency", vesselE_);
    const int hi = indexOf(kHeadKeys, countOf(kHeadKeys), jstr(p, "head_type"));
    if (hi >= 0) vesselHead_ = hi;
    vesselRho_ = jnum(p, "fluid_density_kgm3", vesselRho_);
    vesselFill_ = jnum(p, "fill_fraction", vesselFill_);
    vesselOrient_ = jstr(p, "orientation", "vertical") == "horizontal" ? 1 : 0;
    vesselParams_ = p;
    const json& res = jobj(rec, "results");
    vessel_ = res.empty() ? json() : res;
}

void PipingPanel::adoptDuct(const nlohmann::json& rec) {
    ++ductGeneration_;
    ductSlot_.bind(rec);
    const json& p = jobj(rec, "params");
    const json& segs = jarr(p, "segments");
    ductSegs_.clear();
    if (!segs.empty()) {
        for (const auto& s : segs) {
            DuctSeg d;
            const json id = s.is_object() ? s.value("id", json()) : json();
            d.id = id.is_string() ? id.get<std::string>() : id.is_number_integer() ? id.dump() : std::string();
            d.native = s;
            d.airflow = jnum(s, "airflow_m3s", d.airflow);
            d.length = jnum(s, "length_m", d.length);
            d.k = jnum(s, "k_fittings", d.k);
            d.shape = jstr(s, "shape", "round") == "rect" ? 1 : 0;
            d.aspect = jnum(s, "aspect_ratio", d.aspect);
            ductSegs_.push_back(d);
        }
        for (std::size_t i = 0; i < segs.size(); ++i) {
            const json parentValue = segs[i].value("parent", json());
            const std::string parent = parentValue.is_string() ? parentValue.get<std::string>() :
                                       parentValue.is_number_integer() ? parentValue.dump() : std::string();
            for (int j = 0; j < (int)ductSegs_.size(); ++j)
                if (!parent.empty() && ductSegs_[j].id == parent) ductSegs_[i].parent = j;
        }
    }
    ductFriction_ = jnum(p, "friction_pa_per_m", ductFriction_);
    ductVmax_ = jnum(p, "max_velocity_ms", ductVmax_);
    ductTempC_ = jnum(p, "air_temp_c", ductTempC_);
    ductRoughMm_ = jnum(p, "roughness_mm", ductRoughMm_);
    ductParams_ = p;
    const json& res = jobj(rec, "results");
    duct_ = res.empty() ? json() : res;
}

void PipingPanel::adoptFlow(AppState& st, const nlohmann::json& rec) {
    ++flowGeneration_;
    flowSlot_.bind(rec);
    const json& p = jobj(rec, "params");
    flowSlotKey_ = jstr(p, "network_id") + "|" + jstr(p, "run_id");
    const json& runs = jarr(network_, "runs");
    if (jstr(p, "network_id") == netSlot_.id)
        for (int i = 0; i < (int)runs.size(); ++i)
            if (jstr(runs[i], "id") == jstr(p, "run_id")) flowRun_ = i;
    jvec3(jobj(p, "inlet").value("point", json()), inlet_);
    jvec3(jobj(p, "inlet").value("normal", json()), inletN_);
    jvec3(jobj(p, "outlet").value("point", json()), outlet_);
    jvec3(jobj(p, "outlet").value("normal", json()), outletN_);
    if (jhas(p, "mean_velocity_ms")) {
        flowRateMode_ = 0;
        flowVelocity_ = jnum(p, "mean_velocity_ms");
    } else if (jhas(p, "flow_m3s")) {
        flowRateMode_ = 1;
        flowRateM3h_ = jnum(p, "flow_m3s") * 3600.0;
    }
    if (jhas(p, "density_kgm3")) {
        flowFluid_ = countOf(kFluidKeys);
        flowDensity_ = jnum(p, "density_kgm3");
        flowViscosity_ = jnum(p, "viscosity_pas", flowViscosity_);
    } else {
        flowFluid_ = indexOf(kFluidKeys, countOf(kFluidKeys), jstr(p, "fluid", "water"));
        // Unknown aliases are shown by their authored name until the native RPC resolves them.
    }
    flowStl_ = jstr(p, "source_stl");
    flowSource_ = flowStl_.empty() ? 0 : 1;
    flowVoxelMm_ = jnum(p, "voxel_mm", 0.0);
    flowMaxSteps_ = (int)jnum(p, "max_steps", 0.0);
    flowLaunchParams_ = p;
    resolveFluidDisplay(st, true);
    const json& res = jobj(rec, "results");
    flow_ = res.empty() ? json() : res;
}

nlohmann::json PipingPanel::boundaryJson() const {
    json out = json::array();
    const json& runs = jarr(network_, "runs");
    const bool gas = gasNetwork();
    for (const auto& b : boundary_) {
        json row = b.native.is_object() ? b.native : json::object();
        if (b.run < 0 || b.run >= (int)runs.size()) {
            if (!row.empty()) out.push_back(std::move(row));
            continue;
        }
        row["run"] = jstr(runs[b.run], "id");
        row["end"] = b.end == 1 ? "end" : "start";
        for (const char* key : {"pressure", "head", "demand", "demand_lps", "mass_demand"})
            row.erase(key);
        const int kind = std::clamp(b.kind, 0, gas ? 1 : 2);
        const char* key = gas ? kGasKeys[kind] : kLiquidKeys[kind];
        if (b.kind == 0 && jhas(b.native, "pressure")) key = "pressure";
        else if (b.kind == 1 && jhas(b.native, "head")) key = "head";
        else if (b.kind == 1 && jhas(b.native, "mass_demand")) key = "mass_demand";
        else if (b.kind == 2 && jhas(b.native, "demand")) key = "demand";
        else if (b.kind == 2 && jhas(b.native, "demand_lps")) key = "demand_lps";
        if (std::strcmp(key, "demand_lps") == 0)
            row[key] = b.value == jnum(b.native, "demand_lps") * 3.6
                ? b.native["demand_lps"] : json(b.value / 3.6);
        else row[key] = b.value;
        out.push_back(std::move(row));
    }
    return out;
}

nlohmann::json PipingPanel::networkRecord() const {
    json results = json::object();
    if (!route_.is_null()) results["route"] = route_;
    if (!solve_.is_null()) results["solve"] = solve_;
    if (!spec_.is_null()) results["spec_check"] = spec_;
    if (!bom_.is_null()) results["bom"] = bom_;
    if (!pumpDrive_.empty()) results["pump_drive"] = pumpDrive_;
    json params = networkParams_.is_object() ? networkParams_ : json::object();
    params["piping"] = network_;
    params["design"].update({{"pressure_kpa", designPressureKpa_}, {"temp_c", designTempC_}});
    params["boundary"] = boundaryJson();
    params["fluid"] = fluidJson();
    params["network_type"] = jstr(networkParams_, "network_type", "liquid");
    params["spec_inputs"].update({{"corrosion_allowance_mm", corrosionMm_},
                                {"mill_tolerance_frac", millTol_}, {"joint_factor", jointFactor_},
                                {"fluid_density_kgm3", fluidDensity_}, {"insulation_kgm", insulationKgm_}});
    params["max_spool_length_mm"] = maxSpoolMm_;
    params["route_inputs"] = routeEndpoints();
    return {{"type", "piping_network"},
            {"name", jstr(network_, "name", "Pipe network")},
            {"engine", "cpp-piping"},
            {"params", std::move(params)},
            {"results", results}};
}

void PipingPanel::persistNetwork(AppState& st) {
    if (!network_.is_object()) return;
    store_.saveRecord(st, netSlot_, networkRecord());
}

void PipingPanel::persistVessel(AppState& st) {
    const std::string name = "Vessel " + fmtQ(vesselIdMm_, Q::Len, us_) + " ID" +
                             (vesselSizeBy_ == 0
                                  ? " \xc3\x97 " + fmtQ(vesselTtMm_, Q::Len, us_) + " T/T"
                                  : " \xc2\xb7 " + fmtQ(vesselVolM3_, Q::Vol, us_, 3));
    store_.saveRecord(st, vesselSlot_,
                      {{"type", "vessel"}, {"name", name}, {"engine", "cpp-industrial"},
                       {"params", vesselParams_}, {"results", vessel_}});
}

void PipingPanel::persistDuct(AppState& st) {
    store_.saveRecord(st, ductSlot_,
                      {{"type", "duct_system"},
                       {"name", "Duct system (" + countNoun((long long)ductSegs_.size(), "segment") + ")"},
                       {"engine", "cpp-industrial"},
                       {"params", ductParams_},
                       {"results", duct_}});
}

void PipingPanel::persistFlow(AppState& st) {
    json params = flowLaunchParams_;
    params.erase("job_id");
    params.erase("stl_path");  // a per-run export cache file; the source is recorded below
    const std::string netId = jstr(params, "network_id");
    const std::string runId = jstr(params, "run_id");
    const std::string key = netId + "|" + runId;
    if (key != flowSlotKey_) {
        ++flowGeneration_;
        flowSlot_ = Slot{};
        flowSlotKey_ = key;
        for (const auto& r : store_.records()) {
            if (jstr(r, "type") != "pipe_flow") continue;
            const json& p = jobj(r, "params");
            if (jstr(p, "network_id") == netId && jstr(p, "run_id") == runId) {
                flowSlot_.id = jstr(r, "id");
                break;
            }
        }
    }
    store_.saveRecord(st, flowSlot_,
                      {{"type", "pipe_flow"},
                       {"name", "Internal flow " + (runId.empty() ? std::string("(model)") : runId)},
                       {"engine", "cpp-lbm-internal"},
                       {"params", params},
                       {"results", flow_}});
}

void PipingPanel::request(AppState& st, Form f, const char* method, nlohmann::json params, const char* stage,
                          void (PipingPanel::*done)(AppState&, bool, const nlohmann::json&,
                                                    const std::string&),
                          int timeoutMs) {
    busyForm_ = f;
    busyStage_ = stage;
    busySince_ = ImGui::GetTime();
    say(f, stage, 0);
    store_.call(st, method, std::move(params), this, done, timeoutMs);
}

void PipingPanel::say(Form f, const std::string& text, int tone) {
    statusForm_ = f;
    store_.setStatus(text, tone);
    warnList_.clear();
    warnStatus_.clear();
}

void PipingPanel::sayWarn(Form f, const std::string& text, const nlohmann::json& result) {
    std::vector<std::string> warnings;
    for (const auto& w : jarr(result, "warnings"))
        if (w.is_string()) warnings.push_back(w.get<std::string>());
    say(f, text, warnings.empty() ? 1 : 2);
    if (warnings.empty()) return;
    warnList_ = std::move(warnings);
    warnStatus_ = text;
    warnOpen_ = true;   // open on arrival; the chip collapses it
}

// ============================================================================================
// blockers ("" = the action can run)
// ============================================================================================

std::string PipingPanel::routeBlocker() const {
    if (endpointResolving_[0] || endpointResolving_[1]) return "Wait for the endpoint face to resolve.";
    const double d[3] = {end_[0] - start_[0], end_[1] - start_[1], end_[2] - start_[2]};
    if (norm3(d) < 1e-6) return "Move the end point off the start point.";
    if (!(nps_ > 0.0)) return "Enter a nominal pipe size (NPS, inches).";
    return {};
}

std::string PipingPanel::solveBlocker() const {
    if (jarr(network_, "runs").empty()) return "Route a pipe first.";
    // An empty list is seeded with a supply pressure + demand when the solve starts.
    if (boundary_.empty()) return {};
    bool fixed = false;
    for (const auto& b : boundary_) {
        if (b.run < 0) fixed |= jhas(b.native, "pressure") || (!gasNetwork() && jhas(b.native, "head"));
        else fixed |= b.kind == 0 || (!gasNetwork() && b.kind == 1);
    }
    if (!fixed) return "Add a pressure (or head) condition.";
    return {};
}

std::string PipingPanel::specBlocker() const {
    if (jarr(network_, "runs").empty()) return "Route a pipe first.";
    if (!(designPressureKpa_ > 0.0)) return "Enter a positive design pressure.";
    return {};
}

std::string PipingPanel::bomBlocker() const {
    if (jarr(network_, "runs").empty()) return "Route a pipe first.";
    return {};
}

std::string PipingPanel::flowBlocker(const AppState& st) const {
    if (!st.simClient) return "Internal CFD needs the simulation backend, which this session lacks.";
    if (st.simClient->busy() && !flowRunning_ && !flowPreparing_)
        return "Wait for the running simulation, or cancel it.";
    double in[3] = {inletN_[0], inletN_[1], inletN_[2]};
    double out[3] = {outletN_[0], outletN_[1], outletN_[2]};
    if (!normalize3(in) || !normalize3(out)) return "Give the inlet and outlet a non-zero normal.";
    const double gap[3] = {outlet_[0] - inlet_[0], outlet_[1] - inlet_[1], outlet_[2] - inlet_[2]};
    if (norm3(gap) < 1e-6) return "Pick a run on Setup, or enter the inlet and outlet points.";
    const double rate = flowRateMode_ == 0 ? flowVelocity_ : flowRateM3h_;
    if (!(rate > 0.0)) return "Enter a positive velocity or flow rate.";
    if (flowFluid_ == countOf(kFluidKeys) && (!(flowDensity_ > 0.0) || !(flowViscosity_ > 0.0)))
        return "Enter a positive density and viscosity.";
    if (flowSource_ == 1) {
        if (flowStl_.empty()) return "Choose the STL of the fluid volume.";
    } else if (store_.activeDocId(st).empty() || !st.docMirror.hasDoc()) {
        return "Open the part whose solid is the fluid volume, or choose an STL.";
    }
    return {};
}

std::string PipingPanel::vesselBlocker() const {
    if (!(vesselIdMm_ > 0.0) || !(vesselPKpa_ > 0.0) ||
        (vesselSizeBy_ == 0 ? !(vesselTtMm_ >= 0.0) : !(vesselVolM3_ > 0.0)))
        return "Enter positive diameter/pressure/volume and a nonnegative shell length.";
    return {};
}

std::string PipingPanel::ductBlocker() const {
    if (ductSegs_.empty()) return "Add at least one duct segment.";
    for (std::size_t i = 0; i < ductSegs_.size(); ++i) {
        const DuctSeg& d = ductSegs_[i];
        if (d.id.empty()) return "Name every duct segment.";
        for (std::size_t j = 0; j < i; ++j)
            if (ductSegs_[j].id == d.id) return "Rename one of the two segments named '" + d.id + "'.";
        if (!(d.airflow > 0.0) || !(d.length >= 0.0))
            return "Enter positive airflow and nonnegative length for '" + d.id + "'.";
    }
    return {};
}

std::string PipingPanel::pumpDriveBlocker() const {
    if (gasNetwork())
        return "Switch the fluid to water: pump drives need a liquid network.";
    const json op = driveDutyPoint();
    if (!op.is_object())
        return "Solve the network first, or set Source to Manual.";
    if (!(jnum(op, "flow_m3h") > 0.0) || !(jnum(op, "head_m") > 0.0))
        return "Enter a positive duty flow and head.";
    return {};
}

// ============================================================================================
// actions
// ============================================================================================

void PipingPanel::newNetwork(AppState& st) {
    ++networkGeneration_; ++flowGeneration_; ++networkRevision_;
    ++fluidDisplayGeneration_[0];
    fluidIdx_ = 0; fluidTempC_ = 20.0;
    int n = 1;
    for (const auto& r : store_.records())
        if (jstr(r, "type") == "piping_network") ++n;
    network_ = newNetworkDoc("Pipe network " + std::to_string(n), kPipeMaterials[materialIdx_]);
    netSlot_ = Slot{};
    route_ = solve_ = spec_ = bom_ = json();
    pumpDrive_ = json::object();
    fluidOverride_ = json();
    networkParams_ = json::object();
    endpointFace_[0] = endpointFace_[1] = json();
    ++endpointGeneration_[0];
    ++endpointGeneration_[1];
    endpointResolving_[0] = endpointResolving_[1] = false;
    boundary_.clear();
    selectedRun_ = -1;
    selectedFitting_.clear();
    tool_ = Tool::None;
    routeMode_ = 0;
    flowRun_ = -1;
    routeSeeded_ = false;
    for (Form f : {Form::Solve, Form::Spec, Form::Flow}) page_[(int)f] = 0;
    seedRouteDefaults(st);
    persistNetwork(st);
    // The Network form says what to do next ("No runs yet"); the old network's status is moot.
    store_.clearStatus();
}

void PipingPanel::deleteNetwork(AppState& st) {
    ++networkGeneration_; ++flowGeneration_; ++networkRevision_;
    ++fluidDisplayGeneration_[0];
    store_.deleteRecord(st, netSlot_);
    network_ = json();
    route_ = solve_ = spec_ = bom_ = json();
    pumpDrive_ = json::object();
    fluidOverride_ = json();
    boundary_.clear();
    selectedRun_ = flowRun_ = -1;
    selectedFitting_.clear();
    tool_ = Tool::None;
    say(Form::Network, "Network deleted from the document.", 0);
}

int PipingPanel::deleteKind() const {
    switch (form_) {
        case Form::Network: return netSlot_.id.empty() ? 0 : 1;
        case Form::Vessel: return vesselSlot_.id.empty() ? 0 : 2;
        case Form::Duct: return ductSlot_.id.empty() ? 0 : 3;
        default: return 0;
    }
}

void PipingPanel::deleteCurrent(AppState& st) {
    const int kind = confirmDelete_;
    confirmDelete_ = 0;
    if (store_.inflight() > 0 || kind != deleteKind()) return;
    if (kind == 1) {
        deleteNetwork(st);
    } else if (kind == 2) {
        ++vesselGeneration_;
        store_.deleteRecord(st, vesselSlot_);
        vessel_ = json();
        page_[(int)Form::Vessel] = 0;
        say(Form::Vessel, "Vessel deleted from the document.", 0);
    } else if (kind == 3) {
        ++ductGeneration_;
        store_.deleteRecord(st, ductSlot_);
        duct_ = json();
        page_[(int)Form::Duct] = 0;
        say(Form::Duct, "Duct system deleted from the document.", 0);
    }
}

void PipingPanel::seedRouteDefaults(AppState& st) {
    if (routeSeeded_) return;
    routeSeeded_ = true;
    const json& runs = jarr(network_, "runs");
    // Chain from the newest run's end: the next run starts where the last one stopped.
    if (!runs.empty()) {
        const json& path = jarr(runs.back(), "path");
        double a[3], b[3];
        if (path.size() >= 2 && jvec3(path[path.size() - 2], a) && jvec3(path.back(), b)) {
            double d[3] = {b[0] - a[0], b[1] - a[1], b[2] - a[2]};
            if (normalize3(d)) {
                for (int k = 0; k < 3; ++k) {
                    start_[k] = b[k];
                    startDir_[k] = d[k];
                    end_[k] = b[k] + d[k] * 1000.0;
                    endDir_[k] = d[k];
                }
                return;
            }
        }
    }
    // Otherwise span the model along X at its centre: the router has to find its way around.
    float lo[3] = {FLT_MAX, FLT_MAX, FLT_MAX}, hi[3] = {-FLT_MAX, -FLT_MAX, -FLT_MAX};
    bool any = false;
    for (const auto& kv : st.faceNormalCache) {
        for (int k = 0; k < 3; ++k) {
            lo[k] = std::min(lo[k], kv.second[3 + k]);
            hi[k] = std::max(hi[k], kv.second[3 + k]);
        }
        any = true;
    }
    if (!any) {
        start_[0] = 0; start_[1] = 0; start_[2] = 0;
        end_[0] = 1000; end_[1] = 0; end_[2] = 0;
    } else {
        const double span = std::max(100.0, (double)(hi[0] - lo[0]));
        const double cy = 0.5 * (lo[1] + hi[1]), cz = 0.5 * (lo[2] + hi[2]);
        start_[0] = lo[0] - 0.5 * span - clearanceMm_;
        start_[1] = cy;
        start_[2] = cz;
        end_[0] = hi[0] + 0.5 * span + clearanceMm_;
        end_[1] = cy;
        end_[2] = cz;
    }
    startDir_[0] = 1; startDir_[1] = 0; startDir_[2] = 0;
    endDir_[0] = 1; endDir_[1] = 0; endDir_[2] = 0;
}

void PipingPanel::setRouteEndpoints(const double start[3], const double startDir[3],
                                    const double end[3], const double endDir[3]) {
    ++networkGeneration_; ++flowGeneration_;
    for (int k = 0; k < 3; ++k) {
        start_[k] = start[k];
        startDir_[k] = startDir[k];
        end_[k] = end[k];
        endDir_[k] = endDir[k];
    }
    ++endpointGeneration_[0];
    ++endpointGeneration_[1];
    endpointFace_[0] = endpointFace_[1] = json();
    endpointResolving_[0] = endpointResolving_[1] = false;
    routeSeeded_ = true;
}

void PipingPanel::setRouteSpec(double nps, const std::string& schedule, const std::string& material,
                               double clearanceMm) {
    ++networkGeneration_; ++flowGeneration_;
    nps_ = nps;
    const int si = indexOf(kSchedules, countOf(kSchedules), schedule);
    if (si >= 0) scheduleIdx_ = si;
    const int mi = indexOf(kPipeMaterials, countOf(kPipeMaterials), material);
    if (mi >= 0) materialIdx_ = mi;
    clearanceMm_ = clearanceMm;
}

bool PipingPanel::runRoute(AppState& st) {
    if (busy()) return false;
    const std::string why = routeBlocker();
    if (!why.empty()) {
        say(Form::Route, why, 3);
        return false;
    }
    json params = routeEndpoints();
    params["start"].erase("face");
    params["end"].erase("face");
    double sd[3] = {startDir_[0], startDir_[1], startDir_[2]};
    double ed[3] = {endDir_[0], endDir_[1], endDir_[2]};
    if (normalize3(sd)) params["start"]["dir"] = vec3(sd);
    else params["start"].erase("dir");
    if (normalize3(ed)) params["end"]["dir"] = vec3(ed);
    else params["end"].erase("dir");
    if (!params.contains("obstacles")) params["obstacles"] = json::array();
    if (gridMm_ > 0.0) params["grid_mm"] = gridMm_;
    if (!runName_.empty()) params["name"] = runName_;
    if (network_.is_object()) params["piping"] = network_;
    const std::string docId = st.docMirror.hasDoc() ? st.docMirror.docId() : std::string();
    if (!docId.empty()) params["doc_id"] = docId;
    request(st, Form::Route, "piping_route", std::move(params),
            docId.empty() ? "Routing (no model open: no obstacles)\xe2\x80\xa6"
                          : "Routing around the model's bodies\xe2\x80\xa6",
            &PipingPanel::onRoute, 120000);
    return true;
}

void PipingPanel::onRoute(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        say(Form::Route, "Route refused: " + why, 3);
        return;
    }
    if (r.contains("piping") && r["piping"].is_object()) network_ = r["piping"];
    ++networkGeneration_; ++flowGeneration_; ++networkRevision_;
    selectedFitting_.clear();
    route_ = r;
    route_.erase("piping");
    route_.erase("next_actions");
    const std::string runId = jstr(r, "run_id");
    const json& runs = jarr(network_, "runs");
    for (int i = 0; i < (int)runs.size(); ++i)
        if (jstr(runs[i], "id") == runId) selectedRun_ = i;
    // The geometry changed: earlier solve / spec / BOM results describe another network.
    solve_ = spec_ = bom_ = json();
    pumpDrive_ = json::object();
    routeSeeded_ = false;
    runName_.clear();
    persistNetwork(st);
    // Frame the new run (path + both endpoints, padded by the pipe size) so the route is in view.
    {
        float lo[3] = {FLT_MAX, FLT_MAX, FLT_MAX}, hi[3] = {-FLT_MAX, -FLT_MAX, -FLT_MAX};
        auto grow = [&](const double p[3]) {
            for (int k = 0; k < 3; ++k) {
                lo[k] = std::min(lo[k], (float)p[k]);
                hi[k] = std::max(hi[k], (float)p[k]);
            }
        };
        double v[3];
        if (selectedRun_ >= 0 && selectedRun_ < (int)runs.size())
            for (const auto& p : jarr(runs[selectedRun_], "path"))
                if (jvec3(p, v)) grow(v);
        grow(start_);
        grow(end_);
        const float pad = (float)(nps_ * 25.4 + clearanceMm_);
        for (int k = 0; k < 3; ++k) {
            st.zoomToBBoxMin[k] = lo[k] - pad;
            st.zoomToBBoxMax[k] = hi[k] + pad;
        }
        st.zoomToBBoxRequested = true;
    }
    // ONE footer line: run · length · bends; its warnings are listed right under it (sayWarn) as
    // well as in the "Last route" card.
    sayWarn(Form::Route, runName(runId) + " routed \xc2\xb7 " + qtyQ(r, "length_mm", Q::Len, us_, 3) +
                             " \xc2\xb7 " + countNoun((long long)jnum(r, "bends"), "bend"),
            r);
}

void PipingPanel::removeRun(AppState& st, int runIndex) {
    refreshRows();
    if (runIndex < 0 || runIndex >= (int)runRows_.size()) return;
    const auto& row = runRows_[runIndex];
    submitEdit(st, json::array({{{"op", "remove_run"}, {"id", row.id}}}), row.label + " removed.");
}

void PipingPanel::seedBoundaryDefaults() {
    const json& runs = jarr(network_, "runs");
    if (!boundary_.empty() || runs.empty()) return;
    // Supply pressure at the first run's start, the demand drawn at the last run's end.
    const bool gas = gasNetwork();
    boundary_.push_back({0, 0, 0, boundaryDefault(gas, 0)});
    const int demand = gas ? 1 : 2;
    boundary_.push_back({(int)runs.size() - 1, 1, demand, boundaryDefault(gas, demand)});
}

bool PipingPanel::gasNetwork() const {
    const std::string mode = jstr(networkParams_, "network_type");
    return mode == "gas" || mode == "compressible";
}

nlohmann::json PipingPanel::fluidJson() const {
    if (networkParams_.contains("fluid")) return networkParams_["fluid"];
    return {{"name", kFluidKeys[fluidIdx_ >= 0 ? fluidIdx_ : 0]}, {"T", fluidTempC_ + 273.15}};
}

void PipingPanel::resolveFluidDisplay(AppState& st, bool flow) {
    const int lane = flow ? 1 : 0;
    const auto generation = ++fluidDisplayGeneration_[lane];
    if (flow ? flowFluid_ == countOf(kFluidKeys) : fluidOverride_.is_object()) return;
    const auto& fluid = jobj(networkParams_, "fluid");
    const std::string name = flow ? jstr(flowLaunchParams_, "fluid", "water") : jstr(fluid, "name", "water");
    const double temp = flow ? jnum(flowLaunchParams_, "temp_c", 20.0) + 273.15 : fluidTempC_ + 273.15;
    const double pressure = flow ? jnum(flowLaunchParams_, "pressure_kpa", 0.0) * 1000.0 + 101325.0
                                 : jnum(fluid, "P", 101325.0);
    const std::string docId = store_.activeDocId(st);
    const std::weak_ptr<int> alive = alive_;
    st.rpc.call("piping_fluid", {{"name", name}, {"T", temp}, {"P", pressure}},
        [this, &st, alive, docId, flow, lane, generation](const core::RpcResult& r) {
            if (alive.expired() || fluidDisplayGeneration_[lane] != generation ||
                store_.activeDocId(st) != docId || !r.ok || protocolError(r.result) ||
                !jbool(r.result, "ok", false)) return;
            const int index = indexOf(kFluidKeys, countOf(kFluidKeys), jstr(r.result, "name"));
            if (flow) flowFluid_ = index;
            else fluidIdx_ = index;
        }, 30000);
}

std::uint64_t& PipingPanel::setupGeneration(Form f) {
    if (f == Form::Vessel) return vesselGeneration_;
    if (f == Form::Duct) return ductGeneration_;
    if (f == Form::Flow) return flowGeneration_;
    return networkGeneration_;
}

const std::uint64_t& PipingPanel::setupGeneration(Form f) const {
    if (f == Form::Vessel) return vesselGeneration_;
    if (f == Form::Duct) return ductGeneration_;
    if (f == Form::Flow) return flowGeneration_;
    return networkGeneration_;
}

nlohmann::json PipingPanel::setupDragOrigin(const AppState& st, Form f) const {
    return {{"doc_id", store_.activeDocId(st)}, {"form", (int)f},
            {"study", f == Form::Vessel ? vesselSlot_.id : f == Form::Duct ? ductSlot_.id :
                      f == Form::Flow ? flowSlot_.id : netSlot_.id},
            {"network_study", netSlot_.id}, {"network", jstr(network_, "id")},
            {"generation", setupGeneration(f)}, {"network_generation", networkGeneration_}};
}

bool PipingPanel::acceptsSetupDrag(const AppState& st, Form f, const json& p) const {
    if (!p.is_object() || busy() || setupEdited_) return false;
    const auto matches = [&](const char* key, std::uint64_t value) {
        const auto it = p.find(key);
        return it != p.end() && it->is_number_unsigned() && it->get<std::uint64_t>() == value;
    };
    return jstr(p, "doc_id") == store_.activeDocId(st) && jnum(p, "form", -1) == (int)f &&
        jstr(p, "study") == (f == Form::Vessel ? vesselSlot_.id : f == Form::Duct ? ductSlot_.id :
                            f == Form::Flow ? flowSlot_.id : netSlot_.id) &&
        jstr(p, "network_study") == netSlot_.id && jstr(p, "network") == jstr(network_, "id") &&
        matches("generation", setupGeneration(f)) && matches("network_generation", networkGeneration_);
}

nlohmann::json PipingPanel::solveParams() const {
    return {{"doc", network_},
            {"boundary", boundaryJson()},
            {"fluid", fluidJson()},
            {"pumps", networkParams_.value("pumps", json(nullptr))},
            {"network_type", jstr(networkParams_, "network_type", "liquid")},
            {"source_handoff", networkParams_.value("source_handoff", json(nullptr))}};
}

bool PipingPanel::runSolve(AppState& st) {
    if (store_.inflight() > 0) return false;
    seedBoundaryDefaults();
    const std::string why = solveBlocker();
    if (!why.empty()) {
        say(Form::Solve, why, 3);
        return false;
    }
    request(st, Form::Solve, "piping_solve", solveParams(), "Solving the network\xe2\x80\xa6", &PipingPanel::onSolve,
            60000);
    return true;
}

void PipingPanel::onSolve(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    // A non-converged solve still answers with its nodes/links: keep it, flagged.
    if (!ok && !r.contains("links")) {
        say(Form::Solve, "Solve failed: " + why, 3);
        return;
    }
    solve_ = r;
    ++networkRevision_;  // refresh solved viewport colours as well as the geometry cache
    solve_.erase("next_actions");
    persistNetwork(st);
    page_[(int)Form::Solve] = pagesOf(Form::Solve).results;   // show what came back
    if (ok)
        sayWarn(Form::Solve,
                "Solved " + countNoun((long long)jarr(r, "links").size(), "link") + " in " +
                    countNoun((long long)jnum(r, "iterations"), "iteration") + ".",
                r);
    else
        say(Form::Solve, "Not converged: " + why, 2);
}

bool PipingPanel::runSpecCheck(AppState& st) {
    if (store_.inflight() > 0) return false;
    const std::string why = specBlocker();
    if (!why.empty()) {
        say(Form::Spec, why, 3);
        return false;
    }
    request(st, Form::Spec, "piping_spec_check",
            {{"piping", network_},
             {"design_pressure_kpa", designPressureKpa_},
             {"design_temp_c", designTempC_},
             {"corrosion_allowance_mm", corrosionMm_},
             {"mill_tolerance_frac", millTol_},
             {"joint_factor", jointFactor_},
             {"fluid_density_kgm3", fluidDensity_},
             {"insulation_kgm", insulationKgm_}},
            "Checking against ASME B31.3\xe2\x80\xa6", &PipingPanel::onSpec, 60000);
    return true;
}

void PipingPanel::onSpec(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        say(Form::Spec, "Spec check refused: " + why, 3);
        return;
    }
    spec_ = r;
    spec_.erase("next_actions");
    persistNetwork(st);
    page_[(int)Form::Spec] = pagesOf(Form::Spec).results;
    if (jbool(r, "all_pass", false)) {
        say(Form::Spec, "All runs and flanges pass ASME B31.3.", 1);
        return;
    }
    long long runFails = 0, flangeFails = 0;
    for (const auto& x : jarr(r, "runs")) runFails += jbool(x, "pass", false) ? 0 : 1;
    for (const auto& x : jarr(r, "flanges")) flangeFails += jbool(x, "pass", false) ? 0 : 1;
    std::string what;
    if (runFails > 0) what = countNoun(runFails, "run");
    if (flangeFails > 0) what += (what.empty() ? "" : " and ") + countNoun(flangeFails, "flange");
    say(Form::Spec, what.empty() ? std::string("The check failed at the design conditions.")
                                 : "Fails ASME B31.3: " + what + ".",
        2);
}

bool PipingPanel::runBom(AppState& st) {
    if (store_.inflight() > 0) return false;
    const std::string why = bomBlocker();
    if (!why.empty()) {
        say(Form::Bom, why, 3);
        return false;
    }
    request(st, Form::Bom, "piping_bom", {{"piping", network_}, {"max_spool_length_mm", maxSpoolMm_}},
            "Building the bill of materials\xe2\x80\xa6", &PipingPanel::onBom, 60000);
    return true;
}

void PipingPanel::onBom(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        say(Form::Bom, "BOM refused: " + why, 3);
        return;
    }
    bom_ = r;
    bom_.erase("next_actions");
    persistNetwork(st);
    // The "Bill of materials" card holds the totals (the footer hides an Ok line beside it).
    sayWarn(Form::Bom, "Bill of materials built.", r);
}

// ---- pump drive (results.pump_drive) -----------------------------------------------------------

nlohmann::json PipingPanel::solvedDutyPoint() const {
    if (!solve_.is_object() || solve_.empty()) return json();
    const double rho = jnum(jobj(solve_, "fluid"), "rho", 998.0);
    // A modelled pump (agent-supplied `pumps`) reports its head on its link.
    for (const auto& l : jarr(solve_, "links")) {
        const double ph = std::fabs(jnum(l, "pump_head_m", 0.0));
        if (ph > 0.0)
            return {{"run", jstr(l, "id")}, {"flow_m3h", std::fabs(jnum(l, "flow_m3h"))}, {"head_m", ph},
                    {"fluid_density_kgm3", rho}, {"source", "piping_solve"}, {"basis", "pump link"}};
    }
    // Otherwise the pump IS the fixed supply: it delivers the total demand at the supply node's
    // gauge pressure head.
    double demand = 0.0, supplyKpa = -1.0;
    for (const auto& n : jarr(solve_, "nodes")) {
        if (jbool(n, "fixed", false))
            supplyKpa = std::max(supplyKpa, jnum(n, "pressure_kpa"));
        else if (jnum(n, "demand_m3h") > 0.0)
            demand += jnum(n, "demand_m3h");
    }
    if (supplyKpa <= 0.0 || demand <= 0.0) return json();
    std::string run;
    const json& runs = jarr(network_, "runs");
    for (const auto& b : boundary_)
        if ((b.kind == 0 || b.kind == 1) && b.run >= 0 && b.run < (int)runs.size()) {
            run = jstr(runs[b.run], "id");
            break;
        }
    return {{"run", run.empty() ? std::string("supply") : run},
            {"flow_m3h", demand},
            {"head_m", supplyKpa * 1000.0 / (rho * 9.80665)},
            {"fluid_density_kgm3", rho},
            {"source", "piping_solve"},
            {"basis", "supply node"}};
}

nlohmann::json PipingPanel::driveDutyPoint() const {
    if (driveSource_ == 0) return solvedDutyPoint();
    const json& runs = jarr(network_, "runs");
    const std::string run = driveRun_ >= 0 && driveRun_ < (int)runs.size() ? jstr(runs[driveRun_], "id")
                                                                           : std::string("manual");
    double rho = jnum(jobj(solve_, "fluid"), "rho", 0.0);
    if (!(rho > 0.0)) rho = fluidOverride_.is_object() ? jnum(fluidOverride_, "rho", 998.0) : 998.0;
    return {{"run", run}, {"flow_m3h", driveIn_.flowM3h}, {"head_m", driveIn_.headM},
            {"fluid_density_kgm3", rho}, {"source", "explicit"}};
}

bool PipingPanel::runPumpDrive(AppState& st) {
    if (store_.inflight() > 0) return false;
    syncUnits(st);
    sim::defaultSupplyForUnits(driveIn_, us_);   // same regional default the form applies
    const std::string why = pumpDriveBlocker();
    if (!why.empty()) {
        say(Form::Solve, why, 3);
        return false;
    }
    const json op = driveDutyPoint();
    sim::PumpDriveInputs in = driveIn_;
    in.kind = 1;
    in.flowM3h = jnum(op, "flow_m3h");
    in.headM = jnum(op, "head_m");
    in.densityKgm3 = jnum(op, "fluid_density_kgm3", 998.0);
    drivePendingRun_ = jstr(op, "run");
    drivePendingOp_ = op;
    drivePendingOp_.erase("run");
    request(st, Form::Solve, "pump_drive_size", sim::pumpDriveParams(in), "Sizing the pump drive\xe2\x80\xa6",
            &PipingPanel::onPumpDrive, 15000);
    return true;
}

void PipingPanel::onPumpDrive(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        say(Form::Solve, "Pump drive refused: " + why, 3);
        return;
    }
    json entry = stripAdvice(r);
    entry["run_id"] = drivePendingRun_;
    entry["operating_point"] = drivePendingOp_;
    if (!pumpDrive_.is_object()) pumpDrive_ = json::object();
    pumpDrive_[drivePendingRun_] = entry;
    persistNetwork(st);
    // The card on this page holds the numbers; the line says what happened (and any warnings).
    sayWarn(Form::Solve, "Pump drive sized \xc2\xb7 " + driveRunName(drivePendingRun_), r);
}

void PipingPanel::openHandoff(AppState& st, const nlohmann::json& pipingDoc, const nlohmann::json& boundary,
                              const nlohmann::json& fluid, const nlohmann::json& solve) {
    // A new network study seeded from the handoff (never overwrites the open network's record).
    json rec = {{"type", "piping_network"},
                {"name", jstr(pipingDoc, "name", "Hydraulic hose runs")},
                {"params", {{"piping", pipingDoc}, {"boundary", boundary}, {"fluid", fluid}}},
                {"results", solve.is_object() ? json{{"solve", solve}} : json::object()}};
    adoptNetwork(st, rec);
    netSlot_ = Slot{};
    if (fluid.is_object() && jhas(fluid, "rho") && jhas(fluid, "mu")) {
        fluidOverride_ = fluid;
        fluidIdx_ = 0;  // liquid network
    }
    if (solve_.is_object()) solve_.erase("next_actions");
    flowRun_ = -1;
    showPanel(st);
    enterForm(st, Form::Solve);
    page_[(int)Form::Solve] = solve_.is_object() && !solve_.empty() ? pagesOf(Form::Solve).results : 0;
    persistNetwork(st);
    say(Form::Solve,
        "Opened the hose runs as a pipe network \xc2\xb7 " +
            countNoun((long long)jarr(network_, "runs").size(), "run") + " \xc2\xb7 " +
            (jstr(fluid, "name").empty() ? std::string("handoff fluid") : fluidName(jstr(fluid, "name"))),
        1);
}

void PipingPanel::seedFlowPorts() {
    const json& runs = jarr(network_, "runs");
    if (flowRun_ < 0 || flowRun_ >= (int)runs.size()) return;
    const json& path = jarr(runs[flowRun_], "path");
    double p0[3], p1[3], q0[3], q1[3];
    if (path.size() < 2 || !jvec3(path[0], p0) || !jvec3(path[1], p1) ||
        !jvec3(path[path.size() - 2], q0) || !jvec3(path.back(), q1))
        return;
    // Both normals point INTO the fluid domain: along the first segment at the inlet, back along
    // the last segment at the outlet.
    for (int k = 0; k < 3; ++k) {
        inlet_[k] = p0[k];
        inletN_[k] = p1[k] - p0[k];
        outlet_[k] = q1[k];
        outletN_[k] = q0[k] - q1[k];
    }
    normalize3(inletN_);
    normalize3(outletN_);
}

nlohmann::json PipingPanel::flowCallParams() const {
    double in[3] = {inletN_[0], inletN_[1], inletN_[2]};
    double out[3] = {outletN_[0], outletN_[1], outletN_[2]};
    normalize3(in);
    normalize3(out);
    json p = flowLaunchParams_.is_object() ? flowLaunchParams_ : json::object();
    for (const char* key : {"job_id", "stl_path", "source_stl", "network_id", "run_id",
                            "voxel_mm", "max_steps"}) p.erase(key);
    p["inlet"].update({{"point", vec3(inlet_)}, {"normal", vec3(in)}});
    p["outlet"].update({{"point", vec3(outlet_)}, {"normal", vec3(out)}});
    if (flowFluid_ == countOf(kFluidKeys)) {
        p["density_kgm3"] = flowDensity_;
        p["viscosity_pas"] = flowViscosity_;
        if (flowLaunchParams_.contains("fluid")) p["fluid"] = flowLaunchParams_["fluid"];
    } else if (!p.contains("fluid")) {
        p["fluid"] = kFluidKeys[flowFluid_ >= 0 ? flowFluid_ : 0];
    }
    if (flowRateMode_ == 0) {
        p["mean_velocity_ms"] = flowVelocity_;
        if (p.contains("flow_m3s") && !p["flow_m3s"].is_null()) p.erase("flow_m3s");
    } else {
        if (!jhas(p, "flow_m3s") || jnum(p, "flow_m3s") * 3600.0 != flowRateM3h_)
            p["flow_m3s"] = flowRateM3h_ / 3600.0;
        if (p.contains("mean_velocity_ms") && !p["mean_velocity_ms"].is_null()) p.erase("mean_velocity_ms");
    }
    if (flowVoxelMm_ > 0.0) p["voxel_mm"] = flowVoxelMm_;
    if (flowMaxSteps_ > 0) p["max_steps"] = flowMaxSteps_;
    return p;
}

bool PipingPanel::runFlow(AppState& st) {
    if (flowRunning_ || flowPreparing_) return false;
    const std::string why = flowBlocker(st);
    if (!why.empty()) {
        say(Form::Flow, why, 3);
        return false;
    }
    json params = flowCallParams();
    json meta = json::object();
    const json& runs = jarr(network_, "runs");
    if (flowRun_ >= 0 && flowRun_ < (int)runs.size()) {
        meta["network_id"] = netSlot_.id;
        const std::string runId = jstr(runs[flowRun_], "id");
        meta["run_id"] = runId;
        // The engine compares against the run's 1-D loss (friction + fitting K) and picks
        // its bend resolution from the run's elbows — only when the ports still sit on the
        // run's two ends, i.e. the fluid volume is that run's bore.
        const json& path = jarr(runs[flowRun_], "path");
        double a[3], b[3];
        auto onEnd = [](const double* p, const double* q) {
            return std::hypot(p[0] - q[0], p[1] - q[1], p[2] - q[2]) <= 1.0;
        };
        if (!runId.empty() && path.size() >= 2 && jvec3(path[0], a) && jvec3(path.back(), b) &&
            onEnd(a, inlet_) && onEnd(b, outlet_)) {
            if (!params.contains("piping")) params["piping"] = network_;
            params["run_id"] = runId;
        }
    }
    if (flowSource_ == 1) meta["source_stl"] = flowStl_;
    core::SimClient* sim = st.simClient;
    const std::string docId = store_.activeDocId(st);
    std::weak_ptr<int> alive = alive_;

    auto launch = [this, &st, sim, docId, meta](json p, std::uint64_t token) {
        std::string note;
        json record = p;
        record.update(meta);
        const bool launched = sim->launch("sim_run_pipe_flow", std::move(p), note, docId, token);
        if (!launched) {
            say(Form::Flow, note.empty() ? std::string("The internal-flow solve did not start.") : note, 3);
            return;
        }
        flowLaunchParams_ = std::move(record);
        flowRunning_ = true;
        flow_ = json();
        say(Form::Flow, "Internal flow running\xe2\x80\xa6", 0);
        st.addLog(note.empty() ? "[piping] internal flow started" : note);
    };
    if (flowSource_ == 0 && !jstr(params, "body").empty()) {
        params["doc_id"] = docId;
        launch(std::move(params), 0);
        return flowRunning_;
    }

    if (flowSource_ == 1) {
        params["stl_path"] = flowStl_;
        launch(std::move(params), 0);
        return flowRunning_;
    }
    std::uint64_t token = 0;
    if (!sim->tryReserve(docId, token)) {
        say(Form::Flow, "Another simulation is being prepared; try again when it starts.", 3);
        return false;
    }
    std::string leaf = "pipeflow_";
    for (char c : docId) leaf += std::isalnum(static_cast<unsigned char>(c)) ? c : '_';
    leaf += "_" + std::to_string(token) + ".stl";
    const std::string path = engine::daemonMode() ? leaf : appdataAppDir() + "/cache/" + leaf;
    flowPreparing_ = true;
    say(Form::Flow, "Exporting the fluid volume\xe2\x80\xa6", 0);
    st.rpc.call(
        "doc_export_step", {{"doc_id", docId}, {"path", path}},
        [this, alive, sim, token, path, params, launch](const core::RpcResult& r) mutable {
            if (alive.expired()) {
                sim->releaseReservation(token);
                return;
            }
            flowPreparing_ = false;
            if (!studyRpcOk(r)) {
                sim->releaseReservation(token);
                say(Form::Flow, "Could not export the fluid volume: " + envelopeWhy(r.result, r.err), 3);
                return;
            }
            params["stl_path"] = jstr(r.result, "path", path);
            launch(std::move(params), token);
        },
        60000);
    return true;
}

void PipingPanel::finishFlowJob(AppState& st, bool ok, bool cancelled, const nlohmann::json& result,
                                const std::string& error, const std::string& ownerDocId) {
    (void)ownerDocId;  // the store saves into the panel's document (the one that launched it)
    flowRunning_ = false;
    if (cancelled) {
        say(Form::Flow, "Internal flow canceled.", 2);
        return;
    }
    const bool answered = result.is_object() && !result.empty();
    if (!ok || (answered && !jbool(result, "ok", false))) {
        say(Form::Flow, "Internal flow refused: " + (answered ? envelopeWhy(result, error) : error), 3);
        return;
    }
    flow_ = result;
    flow_.erase("next_actions");
    persistFlow(st);
    page_[(int)Form::Flow] = pagesOf(Form::Flow).results;
    // One line; the Results page carries the 1-D comparison and the breakdown.
    const bool conv = jbool(result, "converged", false);
    say(Form::Flow,
        "Pressure drop " + qtyQ(result, "dp_pa", Q::PressPa, us_, 3) + " \xc2\xb7 Re " + val(result, "reynolds", 3) +
            " \xc2\xb7 " + jstr(result, "regime", "regime unknown") + (conv ? "" : " \xc2\xb7 not converged"),
        conv ? 1 : 2);
}

nlohmann::json PipingPanel::vesselDefinition() const {
    json p = vesselParams_.is_object() ? vesselParams_ : json::object();
    p.update({{"inner_diameter_mm", vesselIdMm_}, {"design_pressure_kpa", vesselPKpa_},
              {"design_temp_c", vesselTC_}, {"material", kVesselMaterials[vesselMaterial_]},
              {"corrosion_allowance_mm", vesselCaMm_}, {"joint_efficiency", vesselE_},
              {"head_type", kHeadKeys[vesselHead_]}, {"fluid_density_kgm3", vesselRho_},
              {"fill_fraction", vesselFill_},
              {"orientation", vesselOrient_ == 1 ? "horizontal" : "vertical"}});
    p.erase(vesselSizeBy_ == 0 ? "volume_m3" : "tan_tan_length_mm");
    p[vesselSizeBy_ == 0 ? "tan_tan_length_mm" : "volume_m3"] =
        vesselSizeBy_ == 0 ? vesselTtMm_ : vesselVolM3_;
    return p;
}

nlohmann::json PipingPanel::ductDefinition() const {
    json segs = json::array();
    for (const auto& d : ductSegs_) {
        json s = d.native.is_object() ? d.native : json::object();
        s.update({{"id", d.id}, {"airflow_m3s", d.airflow}, {"length_m", d.length},
                  {"k_fittings", d.k}, {"shape", d.shape == 1 ? "rect" : "round"},
                  {"aspect_ratio", d.aspect}});
        const json nativeId = d.native.is_object() ? d.native.value("id", json()) : json();
        if (nativeId.is_number_integer() && nativeId.dump() == d.id) s["id"] = nativeId;
        s.erase("parent");
        if (d.parent >= 0 && d.parent < (int)ductSegs_.size()) {
            const auto& parent = ductSegs_[d.parent];
            const json nativeParentId = parent.native.is_object() ? parent.native.value("id", json()) : json();
            s["parent"] = nativeParentId.is_number_integer() && nativeParentId.dump() == parent.id
                ? nativeParentId : json(parent.id);
        }
        segs.push_back(std::move(s));
    }
    json p = ductParams_.is_object() ? ductParams_ : json::object();
    p.update({{"segments", std::move(segs)}, {"method", "equal_friction"},
              {"friction_pa_per_m", ductFriction_}, {"max_velocity_ms", ductVmax_},
              {"air_temp_c", ductTempC_}, {"roughness_mm", ductRoughMm_}});
    return p;
}

bool PipingPanel::runVessel(AppState& st) {
    if (store_.inflight() > 0) return false;
    const std::string why = vesselBlocker();
    if (!why.empty()) {
        say(Form::Vessel, why, 3);
        return false;
    }
    json p = vesselDefinition();
    vesselParams_ = p;
    request(st, Form::Vessel, "vessel_size", std::move(p), "Sizing the vessel (ASME VIII-1)\xe2\x80\xa6",
            &PipingPanel::onVessel, 30000);
    return true;
}

void PipingPanel::onVessel(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        say(Form::Vessel, "Vessel sizing refused: " + why, 3);
        return;
    }
    vessel_ = r;
    vessel_.erase("next_actions");
    persistVessel(st);
    page_[(int)Form::Vessel] = pagesOf(Form::Vessel).results;
    sayWarn(Form::Vessel,
            "Sized \xc2\xb7 shell " + qtyQ(jobj(r, "shell"), "t_nominal_mm", Q::Len, us_, 3) + " \xc2\xb7 heads " +
                qtyQ(jobj(r, "heads"), "t_nominal_mm", Q::Len, us_, 3) + " \xc2\xb7 hydrotest " +
                qtyQ(r, "hydrotest_kpa", Q::Press, us_, 3),
            r);
}

bool PipingPanel::runDuct(AppState& st) {
    if (store_.inflight() > 0) return false;
    const std::string why = ductBlocker();
    if (!why.empty()) {
        say(Form::Duct, why, 3);
        return false;
    }
    json p = ductDefinition();
    ductParams_ = p;
    request(st, Form::Duct, "duct_size", std::move(p), "Sizing the duct system\xe2\x80\xa6", &PipingPanel::onDuct,
            30000);
    return true;
}

void PipingPanel::onDuct(AppState& st, bool ok, const nlohmann::json& r, const std::string& why) {
    if (!ok) {
        say(Form::Duct, "Duct sizing refused: " + why, 3);
        return;
    }
    duct_ = r;
    duct_.erase("next_actions");
    persistDuct(st);
    page_[(int)Form::Duct] = pagesOf(Form::Duct).results;
    sayWarn(Form::Duct,
            "Sized " + countNoun((long long)jarr(r, "segments").size(), "segment") + " \xc2\xb7 fan static " +
                qtyQ(r, "fan_static_pa", Q::DuctPa, us_, 3),
            r);
}

// ============================================================================================
// commands
// ============================================================================================

void PipingPanel::enterForm(AppState& st, Form f) {
    // Statuses are per form: one form's result line never shows on another.
    if (f != form_) {
        store_.clearStatus();
        confirmDelete_ = 0;   // a pending Delete belongs to the record on screen
    }
    form_ = f;
    if ((f != Form::Route && tool_ == Tool::DrawRun) ||
        (f != Form::Fittings && tool_ == Tool::InsertFitting)) tool_ = Tool::None;
    armed_ = 0;
    const json& runs = jarr(network_, "runs");
    switch (f) {
        case Form::Fittings:
        case Form::Route:
            if (!network_.is_object()) {
                ++networkGeneration_; ++flowGeneration_; ++networkRevision_;
                network_ = newNetworkDoc("Pipe network 1", kPipeMaterials[materialIdx_]);
            }
            if (runs.empty()) routeMode_ = 0;
            seedRouteDefaults(st);
            break;
        case Form::Solve:
            seedBoundaryDefaults();
            break;
        case Form::Flow:
            if ((flowRun_ < 0 || flowRun_ >= (int)runs.size()) && !runs.empty()) {
                ++flowGeneration_;
                flowRun_ = selectedRun_ >= 0 && selectedRun_ < (int)runs.size() ? selectedRun_ : 0;
                seedFlowPorts();
            }
            break;
        case Form::Duct:
            if (ductSegs_.empty()) {
                ++ductGeneration_;
                ductSegs_.push_back({"Main", -1, 3.0, 20.0, 1.5, 1, 2.0});
                ductSegs_.push_back({"Branch A", 0, 1.5, 15.0, 0.8, 0, 2.0});
                ductSegs_.push_back({"Branch B", 0, 1.5, 12.0, 0.8, 0, 2.0});
            }
            break;
        default:
            break;
    }
}

bool PipingPanel::handleCommand(AppState& st, const std::string& cmdId) {
    if (cmdId == "sim_piping_draw") { setTool(st, Tool::DrawRun); return true; }
    if (cmdId == "sim_piping_fitting") {
        showPanel(st); enterForm(st, Form::Fittings); return true;
    }
    Form f;
    if (cmdId == "sim_piping_new") f = Form::Network;
    else if (cmdId == "sim_piping_route") f = Form::Route;
    else if (cmdId == "sim_piping_solve") f = Form::Solve;
    else if (cmdId == "sim_piping_spec") f = Form::Spec;
    else if (cmdId == "sim_piping_bom") f = Form::Bom;
    else if (cmdId == "sim_piping_flow") f = Form::Flow;
    else if (cmdId == "sim_piping_vessel") f = Form::Vessel;
    else if (cmdId == "sim_piping_duct") f = Form::Duct;
    else return false;

    showPanel(st);
    enterForm(st, f);
    if (cmdId == "sim_piping_route") routeMode_ = 1;
    if (cmdId == "sim_piping_new") newNetwork(st);
    return true;
}

void PipingPanel::showPanel(AppState& st) {
    st.activeWorkspace = AppState::Workspace::Simulate;
    st.leftPaneTab = AppState::LP_Simulation;
    st.showRibbon = true;
    st.ribbonTab = kRibbonTabPiping;
    open_ = true;
}

void PipingPanel::refreshRows() const {
    if (rowsRevision_ == networkRevision_ && rowsUs_ == us_) return;
    rowsRevision_ = networkRevision_;
    rowsUs_ = us_;
    runRows_.clear(); fittingRows_.clear(); runLabels_.clear();
    flowRunLabels_ = {"None (enter the ports)"};
    for (const auto& r : jarr(network_, "runs")) {
        const auto& path = jarr(r, "path");
        RunRow row;
        row.id = jstr(r, "id"); row.label = runLabel(r); row.native = r;
        row.detail = npsOf(r) + " sch " + jstr(r, "schedule", "40") + " \xc2\xb7 " +
                     fmtQ(pathLengthMm(path), Q::Len, us_);
        for (int end = 0; end < 2; ++end) {
            row.portLabel[end] = row.label + (end == 0 ? " start" : " end");
            row.portButton[end] = row.portLabel[end] + "###run_port";
        }
        runLabels_.push_back(row.label);
        flowRunLabels_.push_back(row.label + " \xc2\xb7 " + npsOf(r));
        runRows_.push_back(std::move(row));
    }
    int typeCounts[countOf(kFittingTypes)] = {};
    for (const auto& f : jarr(network_, "fittings")) {
        FittingRow row;
        row.id = jstr(f, "id");
        const auto type = jstr(f, "type");
        const int typeIndex = indexOf(kFittingTypes, countOf(kFittingTypes), type);
        const int ordinal = typeIndex >= 0 ? ++typeCounts[typeIndex] : (int)fittingRows_.size() + 1;
        row.label = jstr(f, "name");
        if (row.label.empty() || row.label == type) {
            std::string noun = type == "custom" ? "Custom fitting" : humanToken(type);
            if (type == "valve") {
                const auto valve = jstr(jobj(f, "params"), "valve_type", "gate");
                const int valveIndex = indexOf(kValveKeys, countOf(kValveKeys), valve);
                noun = (valveIndex >= 0 ? std::string(kValveLabels[valveIndex]) : humanToken(valve)) + " valve";
            }
            row.label = noun + " " + std::to_string(ordinal);
        }
        const auto run = jstr(f, "run_id");
        for (const auto& r : runRows_) if (r.id == run) { row.runLabel = r.label; break; }
        row.native = f;
        fittingRows_.push_back(std::move(row));
    }
    for (auto& run : runRows_)
        for (const auto& fitting : fittingRows_)
            if (fitting.runLabel == run.label) run.detail += " · " + fitting.label;
    fittingsCount_ = countNoun((long long)fittingRows_.size(), "fitting");
}

std::string PipingPanel::selectedRunId() const {
    refreshRows();
    return selectedRun_ >= 0 && selectedRun_ < (int)runRows_.size() ? runRows_[selectedRun_].id : std::string();
}

void PipingPanel::selectRun(AppState&, const std::string& runId) {
    refreshRows();
    selectedRun_ = -1;
    selectedFitting_.clear();
    for (int i = 0; i < (int)runRows_.size(); ++i)
        if (runRows_[i].id == runId) { selectedRun_ = i; break; }
}

void PipingPanel::selectFitting(AppState&, const std::string& fittingId) {
    refreshRows();
    selectedFitting_.clear();
    for (const auto& f : fittingRows_) if (f.id == fittingId) {
        selectedFitting_ = fittingId;
        const auto run = jstr(f.native, "run_id");
        selectedRun_ = -1;
        for (int i = 0; i < (int)runRows_.size(); ++i) if (runRows_[i].id == run) selectedRun_ = i;
        break;
    }
}

void PipingPanel::setTool(AppState& st, Tool t) {
    if (t != Tool::None) {
        showPanel(st);
        enterForm(st, t == Tool::DrawRun ? Form::Route : Form::Fittings);
        if (t == Tool::DrawRun) routeMode_ = 0;
    }
    armed_ = 0;
    tool_ = t;
}

nlohmann::json PipingPanel::newRunSpec() const {
    json p = {{"nps", nps_}, {"schedule", kSchedules[scheduleIdx_]},
              {"material", kPipeMaterials[materialIdx_]}, {"bend_radius", bendRadius_ == 0 ? "LR" : "SR"}};
    if (!runName_.empty()) p["name"] = runName_;
    return p;
}

bool PipingPanel::submitEdit(AppState& st, json ops, const std::string& note) {
    if (editInflight_ || busy()) return false;
    if (!ops.is_array() || ops.empty()) return false;
    if (!network_.is_object()) {
        network_ = newNetworkDoc("Pipe network 1", kPipeMaterials[materialIdx_]);
        ++networkRevision_; ++networkGeneration_;
    }
    const auto revision = networkRevision_;
    const auto doc = store_.activeDocId(st);
    const auto originForm = form_;
    const auto selected = selectedRunId();
    const auto selectedFit = selectedFitting_;
    json boundaries = boundaryJson();
    for (auto& b : boundaries) {
        for (const auto& row : runRows_) if (row.id == jstr(b, "run")) {
            const auto& path = jarr(row.native, "path");
            if (!path.empty()) b["_edit_point"] = jstr(b, "end") == "end" ? path.back() : path.front();
            break;
        }
    }
    editInflight_ = true;
    busyForm_ = originForm; busyStage_ = "Editing piping"; busySince_ = ImGui::GetTime();
    store_.call(st, "piping_edit", {{"piping", network_}, {"ops", std::move(ops)}},
        [this, revision, doc, originForm, note, selected, selectedFit, boundaries]
        (AppState& s, bool ok, const json& r, const std::string& why) {
            editInflight_ = false;
            if (networkRevision_ != revision || store_.activeDocId(s) != doc) {
                say(originForm, "The network changed before the edit finished; repeat the edit on the current network.", 2);
                return;
            }
            if (!ok || !r.contains("piping") || !r["piping"].is_object()) {
                say(originForm, "Piping edit refused: " + (why.empty() ? envelopeWhy(r, "") : why), 3);
                return;
            }
            network_ = r["piping"];
            ++networkRevision_; ++networkGeneration_; ++flowGeneration_;
            solve_ = spec_ = bom_ = route_ = flow_ = json();
            pumpDrive_ = json::object();
            routeSeeded_ = false; flowRun_ = -1;
            refreshRows();
            boundary_.clear();
            for (const auto& b : boundaries) {
                Boundary row;
                row.native = b; row.native.erase("_edit_point");
                row.run = -1; row.end = jstr(b, "end") == "end" ? 1 : 0;
                double before[3];
                const bool hasPoint = jvec3(b.value("_edit_point", json()), before);
                for (int i = 0; i < (int)runRows_.size(); ++i) {
                    if (!hasPoint) {
                        if (runRows_[i].id == jstr(b, "run")) row.run = i;
                        continue;
                    }
                    const auto& path = jarr(runRows_[i].native, "path");
                    if (path.empty()) continue;
                    for (int end = 0; end < 2; ++end) {
                        double after[3];
                        if (!jvec3(end == 0 ? path.front() : path.back(), after)) continue;
                        const double d[3] = {after[0] - before[0], after[1] - before[1], after[2] - before[2]};
                        if (norm3(d) < 1e-6 && (row.run < 0 || runRows_[i].id == jstr(b, "run"))) {
                            row.run = i; row.end = end;
                        }
                    }
                }
                if (row.run >= 0) {
                    row.native["run"] = runRows_[row.run].id;
                    row.native["end"] = row.end == 0 ? "start" : "end";
                } else if (std::find(jarr(r, "removed").begin(), jarr(r, "removed").end(),
                                     b.value("run", json())) != jarr(r, "removed").end()) continue;
                if (jhas(b, "pressure")) { row.kind = 0; row.value = jnum(b, "pressure"); }
                else if (jhas(b, "head")) { row.kind = 1; row.value = jnum(b, "head"); }
                else if (jhas(b, "demand")) { row.kind = 2; row.value = jnum(b, "demand"); }
                else if (jhas(b, "demand_lps")) { row.kind = 2; row.value = jnum(b, "demand_lps") * 3.6; }
                else if (jhas(b, "mass_demand")) { row.kind = 1; row.value = jnum(b, "mass_demand"); }
                boundary_.push_back(std::move(row));
            }
            selectRun(s, selected);
            if (!selectedFit.empty()) selectFitting(s, selectedFit);
            for (const auto& id : jarr(r, "created")) {
                if (!id.is_string()) continue;
                const auto key = id.get<std::string>();
                const auto run = std::find_if(runRows_.begin(), runRows_.end(), [&](const RunRow& v) { return v.id == key; });
                if (run != runRows_.end()) { selectRun(s, key); break; }
                const auto fit = std::find_if(fittingRows_.begin(), fittingRows_.end(), [&](const FittingRow& v) { return v.id == key; });
                if (fit != fittingRows_.end()) { selectFitting(s, key); break; }
            }
            fittingDirty_ = false;
            persistNetwork(s);
            sayWarn(originForm, note, r);
        }, 120000);
    return true;
}

void PipingPanel::loadFittingDraft() {
    if (fittingDraftRevision_ == networkRevision_ && fittingDraftId_ == selectedFitting_) return;
    fittingDraftRevision_ = networkRevision_; fittingDraftId_ = selectedFitting_;
    fittingDraft_ = json::object(); fittingName_.clear();
    for (const auto& f : fittingRows_) if (f.id == selectedFitting_) {
        fittingDraft_ = f.native; fittingName_ = jstr(f.native, "name");
        if (fittingName_ == jstr(f.native, "type")) fittingName_.clear();
        break;
    }
    const auto& p = jobj(fittingDraft_, "params");
    fittingRadius_ = jstr(p, "radius", "LR") == "SR" ? 1 : 0;
    fittingBranchNps_ = jnum(p, "branch_nps", jnum(fittingDraft_, "nps", nps_));
    static const char* const classes[] = {"150", "300", "400", "600", "900", "1500", "2500"};
    fittingFlangeClass_ = std::max(0, indexOf(classes, countOf(classes), fmtText(p, "flange_class")));
    fittingFlangeFace_ = jstr(p, "flange_face", "RF") == "RTJ" ? 1 :
                         jstr(p, "flange_face", "RF") == "FF" ? 2 : 0;
    fittingAngle_ = jnum(p, "angle", 90); fittingBeta_ = jnum(p, "beta", 0.5);
    fittingNps2_ = jnum(fittingDraft_, "nps2", jnum(p, "nps2", 1));
    fittingValve_ = std::max(0, indexOf(kValveKeys, countOf(kValveKeys), jstr(p, "valve_type", "gate")));
    fittingLabel_ = jstr(p, "label");
    const auto& k = jobj(p, "k_model");
    const auto kind = jstr(k, "kind");
    if (fittingCfdState_ == "done" && fittingCfdTarget_ == selectedFitting_ &&
        fittingCfdNetwork_ == jstr(network_, "id") && !k.empty() &&
        k == jobj(fittingCfdResult_, "k_model"))
        fittingCfdResult_["_ui_applied"] = true;
    fittingSource_ = kind == "constant" ? 1 : kind == "two_k" ? 2 : kind == "three_k" ? 3 :
                     kind == "cv" ? 4 : kind == "table" ? 5 : 0;
    fittingK_ = jnum(k, "K", 1); fittingK1_ = jnum(k, "K1", 800);
    fittingKinf_ = jnum(k, "Kinf", 0.2); fittingKi_ = jnum(k, "Ki", 0.2);
    fittingKd_ = jnum(k, "Kd", 1); fittingCv_ = jnum(k, "Cv", 10);
    const auto& geometry = jobj(p, "geometry");
    fittingStl_ = jstr(geometry, "stl_path"); fittingBody_ = jstr(geometry, "body");
    fittingGeometrySource_ = !fittingStl_.empty() ? 2 : !geometry.empty() ? 1 : 0;
    jvec3(jobj(geometry, "inlet").value("point", json()), fittingInlet_);
    jvec3(jobj(geometry, "inlet").value("normal", json()), fittingInletN_);
    jvec3(jobj(geometry, "outlet").value("point", json()), fittingOutlet_);
    jvec3(jobj(geometry, "outlet").value("normal", json()), fittingOutletN_);
    fittingBoreMm_ = jnum(geometry, "inner_dia_mm", 52.5);
    fittingDirty_ = false; fittingError_.clear(); fittingPreview_ = json();
    previewRe_.clear(); previewK_.clear(); previewDirty_ = true; previewDue_ = ImGui::GetTime();
    ++previewGeneration_;
}

nlohmann::json PipingPanel::fittingDefinition() const {
    json f = fittingDraft_, p = jobj(fittingDraft_, "params");
    const auto type = jstr(f, "type");
    if (type == "valve") p["valve_type"] = kValveKeys[fittingValve_];
    if (type == "elbow") {
        p["angle"] = fittingAngle_; p["radius"] = fittingRadius_ == 0 ? "LR" : "SR";
    }
    if (type == "tee") p["branch_nps"] = fittingBranchNps_;
    if (type == "flange") {
        static const int classes[] = {150, 300, 400, 600, 900, 1500, 2500};
        p["flange_class"] = classes[fittingFlangeClass_];
        static const char* const faces[] = {"RF", "RTJ", "FF"};
        p["flange_face"] = faces[fittingFlangeFace_];
    }
    if (type == "orifice") p["beta"] = fittingBeta_;
    if (type == "reducer") { p["nps2"] = fittingNps2_; f["nps2"] = fittingNps2_; }
    if (type == "custom") p["label"] = fittingLabel_;
    if (type == "custom" || fittingSource_ == 5) {
        if (fittingGeometrySource_ != 0) {
            json g = {{"inlet", {{"point", vec3(fittingInlet_)}, {"normal", vec3(fittingInletN_)}}},
                      {"outlet", {{"point", vec3(fittingOutlet_)}, {"normal", vec3(fittingOutletN_)}}},
                      {"inner_dia_mm", fittingBoreMm_}};
            if (fittingGeometrySource_ == 2) g["stl_path"] = fittingStl_;
            else if (jobj(p, "geometry").contains("doc_id")) g["doc_id"] = jobj(p, "geometry")["doc_id"];
            if (fittingGeometrySource_ == 1 && !fittingBody_.empty()) g["body"] = fittingBody_;
            p["geometry"] = std::move(g);
        } else p["geometry"] = nullptr;
    }
    switch (fittingSource_) {
        case 0: p["k_model"] = nullptr; break;
        case 1: p["k_model"] = {{"kind", "constant"}, {"K", fittingK_}}; break;
        case 2: p["k_model"] = {{"kind", "two_k"}, {"K1", fittingK1_}, {"Kinf", fittingKinf_}}; break;
        case 3: p["k_model"] = {{"kind", "three_k"}, {"K1", fittingK1_}, {"Ki", fittingKi_}, {"Kd", fittingKd_}}; break;
        case 4: p["k_model"] = {{"kind", "cv"}, {"Cv", fittingCv_}}; break;
        default: break;
    }
    f["params"] = std::move(p);
    if (!f.contains("nps")) f["nps"] = nps_;
    return f;
}

void PipingPanel::tickFittingRequests(AppState& st) {
    const double now = ImGui::GetTime();
    if (form_ == Form::Fittings && !selectedFitting_.empty() && previewDirty_ &&
        !previewInflight_ && now >= previewDue_) {
        previewDirty_ = false; previewInflight_ = true;
        const auto generation = previewGeneration_;
        const auto doc = store_.activeDocId(st);
        json f = fittingDefinition();
        if (f["params"].contains("k_model") && f["params"]["k_model"].is_null()) f["params"].erase("k_model");
        std::string schedule = kSchedules[scheduleIdx_];
        for (const auto& row : runRows_) if (row.id == jstr(f, "run_id")) {
            schedule = jstr(row.native, "schedule", schedule);
            f["nps"] = jnum(row.native, "nps", nps_); break;
        }
        const std::weak_ptr<int> alive = alive_;
        st.rpc.call("piping_fitting_k", {{"fitting", f}, {"schedule", schedule},
                    {"reynolds", fittingRe_}, {"fluid", fluidJson()}},
            [this, alive, generation, doc, &st](const core::RpcResult& r) {
                if (alive.expired()) return;
                previewInflight_ = false;
                if (generation != previewGeneration_ || doc != store_.activeDocId(st)) return;
                fittingError_.clear(); previewRe_.clear(); previewK_.clear();
                if (!envelopeOk(r)) { fittingError_ = envelopeWhy(r.result, r.err); return; }
                fittingPreview_ = r.result; fittingMethod_ = jstr(r.result, "method");
                for (const auto& pt : jarr(r.result, "curve")) {
                    previewRe_.push_back(jnum(pt, "re")); previewK_.push_back(jnum(pt, "K"));
                }
                previewPlot_.x = previewRe_; previewPlot_.series = {{"K", previewK_}};
                previewPlot_.xAxis.title = "Reynolds number";
                previewPlot_.xAxis.log = true; previewPlot_.xAxis.zero = false;
                previewPlot_.yAxis.title = "Loss coefficient K";
            }, 30000);
    }
    if (!fittingCfdRunning_ || fittingCfdCall_ || fittingCfdJob_.empty() || now < cfdPollDue_) return;
    fittingCfdCall_ = true;
    const auto job = fittingCfdJob_;
    const std::weak_ptr<int> alive = alive_;
    st.rpc.call("piping_fitting_k_cfd_status", {{"job_id", job}},
        [this, alive, job](const core::RpcResult& r) {
            if (alive.expired() || job != fittingCfdJob_) return;
            fittingCfdCall_ = false; cfdPollDue_ = ImGui::GetTime() + 0.5;
            if (!envelopeOk(r)) {
                fittingCfdRunning_ = false; fittingCfdState_ = "failed";
                fittingCfdStage_ = "CFD status unavailable";
                fittingCfdError_ = envelopeWhy(r.result, r.err);
                say(Form::Fittings, fittingCfdError_, 3);
                return;
            }
            fittingCfdPct_ = jnum(r.result, "pct");
            fittingCfdStage_ = humanToken(jstr(r.result, "stage"));
            fittingCfdState_ = jstr(r.result, "state", "failed");
            if (fittingCfdState_ == "running") return;
            fittingCfdRunning_ = false;
            if (fittingCfdState_ != "done") {
                fittingCfdError_ = fittingCfdState_ == "cancelled" ?
                    "CFD calculation canceled." : envelopeWhy(r.result, r.err);
                say(Form::Fittings, fittingCfdError_, fittingCfdState_ == "cancelled" ? 2 : 3);
                return;
            }
            fittingCfdResult_ = jobj(r.result, "result");
            if (jobj(fittingCfdResult_, "k_model").empty()) {
                fittingCfdState_ = "failed"; fittingCfdStage_ = "CFD result unavailable";
                fittingCfdError_ = "CFD finished without a loss model. Recompute the fitting calibration.";
                say(Form::Fittings, fittingCfdError_, 3);
                return;
            }
            cfdRe_.clear(); cfdK_.clear();
            for (const auto& pt : jarr(fittingCfdResult_, "points")) {
                cfdRe_.push_back(jnum(pt, "re")); cfdK_.push_back(jnum(pt, "K"));
            }
            cfdPlot_.x = cfdRe_; cfdPlot_.series = {{"CFD K", cfdK_}};
            cfdPlot_.series.front().points = true;
            cfdPlot_.xAxis.title = "Reynolds number";
            cfdPlot_.xAxis.log = true; cfdPlot_.xAxis.zero = false;
            cfdPlot_.yAxis.title = "Loss coefficient K";
            // Local presentation flags are never copied into the persisted k_model.
            fittingCfdResult_["_ui_reveal_pending"] = true;
            fittingCfdResult_["_ui_applied"] = false;
            say(Form::Fittings, "CFD loss curve ready. Apply CFD K to fitting to save it.", 1);
        }, 30000);
}

nlohmann::json PipingPanel::fittingCfdState() const {
    return {{"state", fittingCfdState_}, {"pct", fittingCfdPct_},
            {"stage", fittingCfdStage_}, {"job_id", fittingCfdJob_},
            {"error", fittingCfdError_},
            {"has_result", fittingCfdState_ == "done" &&
                !jobj(fittingCfdResult_, "k_model").empty()}};
}

void PipingPanel::startFittingCfd(AppState& st) {
    if (fittingCfdRunning_) return;
    if (selectedFitting_.empty() || fittingDraft_.empty()) {
        fittingCfdState_ = "blocked"; fittingCfdStage_ = "No fitting selected";
        fittingCfdError_ = "Select a fitting in the list or viewport before computing its CFD loss coefficient.";
        say(Form::Fittings, fittingCfdError_, 3);
        return;
    }
    fittingCfdRunning_ = fittingCfdCall_ = true; fittingCfdCancel_ = false;
    fittingCfdState_ = "starting";
    fittingCfdTarget_ = selectedFitting_; fittingCfdNetwork_ = jstr(network_, "id");
    fittingCfdDoc_ = store_.activeDocId(st);
    fittingCfdJob_.clear(); fittingCfdResult_ = json(); fittingCfdError_.clear();
    fittingCfdPct_ = 0; fittingCfdStage_ = "Preparing fitting CFD";
    json f = fittingDefinition();
    f["params"].erase("k_model");
    std::string schedule = kSchedules[scheduleIdx_];
    for (const auto& row : runRows_) if (row.id == jstr(f, "run_id")) {
        schedule = jstr(row.native, "schedule", schedule); f["nps"] = jnum(row.native, "nps", nps_); break;
    }
    json p = {{"fitting", f}, {"schedule", schedule}, {"fluid", fluidJson()},
              {"quality", fittingQuality_ == 0 ? "quick" : "standard"}};
    if (fittingGeometrySource_ != 0) {
        p["geometry"] = jobj(f["params"], "geometry");
        if (fittingGeometrySource_ == 1) p["geometry"]["doc_id"] = fittingCfdDoc_;
    }
    const std::weak_ptr<int> alive = alive_;
    st.rpc.call("piping_fitting_k_cfd_start", std::move(p),
        [this, alive, &st](const core::RpcResult& r) {
            if (alive.expired()) return;
            fittingCfdCall_ = false;
            if (!envelopeOk(r)) {
                fittingCfdRunning_ = false; fittingCfdState_ = "blocked";
                fittingCfdStage_ = "CFD start refused";
                fittingCfdError_ = envelopeWhy(r.result, r.err);
                say(Form::Fittings, fittingCfdError_, 3);
                return;
            }
            fittingCfdJob_ = jstr(r.result, "job_id"); cfdPollDue_ = 0;
            if (fittingCfdJob_.empty()) {
                fittingCfdRunning_ = false; fittingCfdState_ = "failed";
                fittingCfdStage_ = "CFD job unavailable";
                fittingCfdError_ = "The fitting CFD job did not return an identifier. Recompute the calibration.";
                say(Form::Fittings, fittingCfdError_, 3);
                return;
            }
            fittingCfdState_ = "running";
            if (fittingCfdCancel_) cancelFittingCfd(st);
        }, 30000);
}

void PipingPanel::cancelFittingCfd(AppState& st) {
    fittingCfdCancel_ = true;
    if (fittingCfdJob_.empty()) return;
    const std::weak_ptr<int> alive = alive_;
    st.rpc.call("piping_fitting_k_cfd_cancel", {{"job_id", fittingCfdJob_}},
        [this, alive](const core::RpcResult& r) {
            if (!alive.expired() && !envelopeOk(r)) {
                fittingCfdError_ = envelopeWhy(r.result, r.err);
                say(Form::Fittings, fittingCfdError_, 3);
            }
        }, 30000);
}

void PipingPanel::drawFittingsForm(AppState& st) {
    if (kit::cardBegin("fitting_palette", "Fitting palette", "route_pipe")) {
        kit::FieldGroup fields("palette_fields", "Fitting");
        bool paletteChanged = kit::fieldEnum("type", "Fitting", &fittingPalette_, kFittingLabels, countOf(kFittingLabels));
        if (fittingPalette_ == 3)
            paletteChanged |= kit::fieldEnum("valve_type", "Valve type", &paletteValve_, kValveLabels, countOf(kValveLabels));
        if (fittingPalette_ == 4)
            paletteChanged |= kit::fieldNumber("nps2", "Outlet NPS", &paletteNps2_, kit::FieldOpts().withMin(0)
                                                   .withTip("Outlet nominal pipe size (NPS designator)."));
        if (fittingPalette_ == 5)
            paletteChanged |= kit::fieldNumber("beta", "Bore / pipe ratio", &paletteBeta_, kit::FieldOpts().withMin(0.01).withMax(0.99));
        if (fittingPalette_ == 7)
            paletteChanged |= kit::fieldNumber("initial_k", "Initial K", &paletteK_, kit::FieldOpts().withMin(0));
        // Keep a typed placement payload, independent of the selected fitting's draft.
        if (paletteChanged || draw_.insertParams.empty()) {
            draw_.insertType = kFittingTypes[fittingPalette_];
            draw_.insertParams = json::object();
            if (fittingPalette_ == 3) draw_.insertParams["valve_type"] = kValveKeys[paletteValve_];
            if (fittingPalette_ == 4) draw_.insertParams["nps2"] = paletteNps2_;
            if (fittingPalette_ == 5) draw_.insertParams["beta"] = paletteBeta_;
            if (fittingPalette_ == 7) draw_.insertParams["k_model"] = {{"kind", "constant"}, {"K", paletteK_}};
            if (paletteChanged) {
                setTool(st, fittingPalette_ <= 2 ? Tool::DrawRun : Tool::InsertFitting);
                if (fittingPalette_ <= 2) {
                    bendRadius_ = 0;
                    say(Form::Route, fittingPalette_ == 2 ?
                        "Start or end the new run on an existing pipe to add a tee." :
                        "Corners become elbows automatically.", 0);
                }
            }
        }
        kit::inlineStatus(tool_ == Tool::InsertFitting ? "Placing fittings: click a pipe in the viewport." :
                          "Choose a fitting, then Place fitting to arm the viewport.", kit::Tone::Info);
        if (fittingPalette_ == 2)
            kit::dimText("A tee joins a branch. Draw a branch endpoint onto an existing pipe to connect and split it automatically.");
    }
    kit::cardEnd();
    kit::sectionHeader("Fittings", "route_pipe", nullptr, (int)fittingRows_.size());
    std::string remove;
    for (int i = 0; i < (int)fittingRows_.size(); ++i) {
        const auto& row = fittingRows_[i];
        ImGui::PushID(i);
        bool removed = false;
        bool expanded = row.id == selectedFitting_;
        const auto headerStart = ImGui::GetCursorScreenPos();
        const float cardWidth = ImGui::GetContentRegionAvail().x;
        const bool cardOpen = kit::removableCard("fitting_row", row.label.c_str(), "route_pipe", nullptr,
                                               "Remove this fitting", &removed, &expanded);
        const auto headerEnd = ImGui::GetCursorScreenPos();
        if (!ImGui::IsItemHovered() &&
            ImGui::IsMouseHoveringRect(headerStart, ImVec2(headerStart.x + cardWidth, headerEnd.y))) {
            const auto tip = "Engine fitting id: " + row.id + "\nType: " + jstr(row.native, "type");
            kit::tooltip(tip.c_str());
        }
        if (cardOpen) {
            if (expanded && row.id != selectedFitting_) selectFitting(st, row.id);
            if (!row.runLabel.empty()) kit::keyValueRowId("run", "Run", row.runLabel.c_str());
            if (ImGui::IsItemHovered()) {
                const auto tip = "Engine fitting id: " + row.id + "\nType: " + jstr(row.native, "type");
                kit::itemTooltip(tip.c_str());
            }
        }
        kit::cardEnd();
        if (removed) remove = row.id;
        ImGui::PopID();
    }
    if (!remove.empty()) {
        submitEdit(st, json::array({{{"op", "remove_fitting"}, {"id", remove}}}), "Fitting removed.");
        return;
    }
    loadFittingDraft();
    if (selectedFitting_.empty() || fittingDraft_.empty()) {
        kit::dimText("Select a fitting in this list or in the viewport to edit its loss coefficient.");
        return;
    }
    const auto type = jstr(fittingDraft_, "type");
    const char* fittingTitle = "Selected fitting";
    for (const auto& row : fittingRows_) if (row.id == selectedFitting_) { fittingTitle = row.label.c_str(); break; }
    bool changed = false;
    if (kit::cardBegin("fitting_editor", fittingTitle, "settings")) {
        kit::FieldGroup fields("fitting_fields", "Outlet NPS");
        changed |= kit::fieldText("name", "Name", &fittingName_, kit::FieldOpts().withHint(fittingTitle));
        kit::fieldReadout("type", "Type", humanToken(type).c_str());
        if (type == "elbow") {
            kit::fieldReadout("angle", "Angle", kit::fmtQuantity(fittingAngle_, "°").c_str());
            static const char* const radii[] = {"Long radius", "Short radius"};
            changed |= kit::fieldEnum("radius", "Bend radius", &fittingRadius_, radii, 2);
        }
        if (type == "tee") changed |= kit::fieldNumber("branch_size", "Branch NPS", &fittingBranchNps_,
            kit::FieldOpts().withMin(0).withTip("Branch nominal pipe size (NPS designator)."));
        if (type == "flange") {
            static const char* const classes[] = {"150", "300", "400", "600", "900", "1500", "2500"};
            static const char* const faces[] = {"Raised face", "Ring joint", "Flat face"};
            changed |= kit::fieldEnum("class", "Pressure class", &fittingFlangeClass_, classes, countOf(classes));
            changed |= kit::fieldEnum("face", "Flange face", &fittingFlangeFace_, faces, 3);
        }
        if (type == "valve") changed |= kit::fieldEnum("valve", "Valve type", &fittingValve_, kValveLabels, countOf(kValveLabels));
        if (type == "orifice") changed |= kit::fieldNumber("beta", "Bore / pipe ratio", &fittingBeta_,
            kit::FieldOpts().withMin(0.01).withMax(0.99));
        if (type == "reducer") changed |= kit::fieldNumber("outlet_nps", "Outlet NPS", &fittingNps2_,
            kit::FieldOpts().withMin(0).withTip("Outlet nominal pipe size (NPS designator)."));
        if (type == "custom") changed |= kit::fieldText("label", "Description", &fittingLabel_);
    }
    kit::cardEnd();
    if (type == "custom" || fittingSource_ == 5) {
        if (kit::cardBegin("custom_geometry", "Fluid geometry for CFD", "study_flow")) {
            kit::FieldGroup fields("custom_fields", "Outlet normal");
            static const char* const sources[] = {"None", "Document body", "STL file"};
            changed |= kit::fieldEnum("geometry_source", "Source", &fittingGeometrySource_, sources, 3);
            if (fittingGeometrySource_ == 1) {
                const std::string label = fittingBody_.empty() ? "No body selected" :
                    fittingBody_[0] == 'b' ? "Body " + fittingBody_.substr(1) : "Selected body";
                kit::fieldReadout("body", "Body", label.c_str(), kit::FieldOpts().withTip(fittingBody_.c_str()));
                if (kit::secondaryButton("Use selected body###pick_body", !st.selection.empty(), "Select a body face in the viewport first.")) {
                    fittingBody_ = "b" + std::to_string(st.selection.items().front().hit.body_id);
                    changed = true;
                }
                sim::FaceDrop face;
                if (sim::acceptFaceDrop(store_.activeDocId(st), face)) {
                    fittingBody_ = "b" + std::to_string(face.body);
                    changed = true;
                }
                kit::dimText("Pick a face in the viewport, then Use selected body; or drag its face onto that button.");
            } else if (fittingGeometrySource_ == 2) {
                bool browse = false;
                changed |= kit::fieldPath("stl", "Fluid STL", &fittingStl_, &browse);
                if (browse) {
                    auto path = ui::openFileDialog("STL mesh (*.stl)\0*.stl\0All files (*.*)\0*.*\0");
                    if (!path.empty()) { fittingStl_ = std::move(path); changed = true; }
                }
            }
            if (fittingGeometrySource_ != 0) {
                auto port = [&](const char* id, const char* label, double point[3], double normal[3]) {
                    ImGui::PushID(id);
                    if (kit::secondaryButton((std::string("Use selected ") + label + " face###face_port").c_str(),
                                              !st.selection.empty(), "Select a port face in the viewport first.")) {
                        const auto& hit = st.selection.items().front().hit;
                        const auto found = st.faceNormalCache.find((static_cast<std::uint64_t>(hit.body_id) << 32) | hit.topo_id);
                        if (found != st.faceNormalCache.end()) {
                            for (int k = 0; k < 3; ++k) { point[k] = found->second[k + 3]; normal[k] = -found->second[k]; }
                            changed = true;
                        }
                    }
                    sim::FaceDrop face;
                    if (sim::acceptFaceDrop(store_.activeDocId(st), face)) {
                        const auto found = st.faceNormalCache.find((static_cast<std::uint64_t>(face.body) << 32) | face.topo);
                        if (found != st.faceNormalCache.end()) {
                            for (int k = 0; k < 3; ++k) {
                                point[k] = found->second[k + 3];
                                normal[k] = -found->second[k];
                            }
                            changed = true;
                        }
                    }
                    std::string run; int end = 0;
                    if (acceptRunPort(st, run, end)) {
                        for (const auto& row : runRows_) if (row.id == run) {
                            const auto& path = jarr(row.native, "path");
                            if (path.size() >= 2) {
                                const int index = end == 0 ? 0 : (int)path.size() - 1;
                                double next[3];
                                if (jvec3(path[index], point) && jvec3(path[end == 0 ? 1 : index - 1], next)) {
                                    for (int k = 0; k < 3; ++k) normal[k] = next[k] - point[k];
                                    normalize3(normal); changed = true;
                                }
                            }
                        }
                    }
                    changed |= kit::fieldVec3("point", label, point, fieldQ(Q::Len, us_));
                    changed |= kit::fieldVec3("normal", "Normal into fluid", normal);
                    ImGui::PopID();
                };
                port("inlet", "Inlet point", fittingInlet_, fittingInletN_);
                port("outlet", "Outlet point", fittingOutlet_, fittingOutletN_);
                changed |= kit::fieldNumber("bore", "Inner diameter", &fittingBoreMm_,
                    fieldQ(Q::Len, us_).withMin(0));
                kit::dimText("Geometry is the fluid volume, not the pipe wall. Port normals point into it.");
            }
        }
        kit::cardEnd();
    }
    if (kit::cardBegin("loss_coefficient", "Loss coefficient", "study_flow")) {
        kit::FieldGroup fields("loss_fields", "Reynolds number");
        changed |= kit::fieldEnum("source", "Source", &fittingSource_, kLossSources, countOf(kLossSources));
        if (fittingSource_ == 1)
            changed |= kit::fieldNumber("constant_k", "K", &fittingK_, kit::FieldOpts().withMin(0));
        if (fittingSource_ == 2 || fittingSource_ == 3) {
            changed |= kit::fieldNumber("k1", "K1", &fittingK1_, kit::FieldOpts().withMin(0));
            if (fittingSource_ == 2)
                changed |= kit::fieldNumber("kinf", "K infinite", &fittingKinf_, kit::FieldOpts().withMin(0));
            else {
                changed |= kit::fieldNumber("ki", "Ki", &fittingKi_, kit::FieldOpts().withMin(0));
                changed |= kit::fieldNumber("kd", "Kd", &fittingKd_, kit::FieldOpts().withMin(0));
            }
        }
        if (fittingSource_ == 4)
            changed |= kit::fieldNumber("cv", "Cv", &fittingCv_, kit::FieldOpts().withMin(0).withTip("US gpm / square root psi."));
        if (kit::fieldNumber("reynolds", "Reynolds number", &fittingRe_, kit::FieldOpts().withMin(1))) {
            previewDirty_ = true; previewDue_ = ImGui::GetTime() + 0.25; ++previewGeneration_;
        }
        if (!fittingError_.empty()) kit::inlineStatus(fittingError_.c_str(), kit::Tone::Err);
        if (previewInflight_) kit::inlineStatus("Calculating loss coefficient", kit::Tone::Info);
        if (fittingPreview_.is_object() && fittingError_.empty()) {
            kit::keyValueRowId("preview_k", "K at selected Re", val(fittingPreview_, "K", 5).c_str());
            kit::keyValueRowId("method", "Method", fittingMethod_.c_str());
            kit::linePlot("k_curve", previewPlot_);
        }
        if (type == "custom" && fittingSource_ == 0)
            kit::inlineStatus("Custom fittings need an explicit loss model; choose Constant K, 2-K, 3-K, Cv or compute From CFD.", kit::Tone::Warn);
    }
    kit::cardEnd();
    if (changed) {
        tool_ = Tool::None;  // return ordinary body/face picking while editing a fitting
        fittingDirty_ = true; previewDirty_ = true;
        previewDue_ = ImGui::GetTime() + 0.25; ++previewGeneration_;
    }
    const bool matching = fittingCfdTarget_ == selectedFitting_ &&
        fittingCfdNetwork_ == jstr(network_, "id") && fittingCfdDoc_ == store_.activeDocId(st);
    if (fittingSource_ != 5 && !fittingCfdRunning_ &&
        !(matching && fittingCfdResult_.is_object())) return;
    if (matching && jbool(fittingCfdResult_, "_ui_reveal_pending", false)) {
        // Target the parent scroll body, not the card's own non-scrolling child.
        ImGui::SetScrollFromPosY(ImGui::GetCursorScreenPos().y - ImGui::GetWindowPos().y, 0.0f);
        fittingCfdResult_.erase("_ui_reveal_pending");
    }
    if (kit::cardBegin("fitting_cfd", "CFD-derived loss coefficient", "study_flow")) {
        {
            kit::FieldGroup fields("fitting_cfd_fields", "Quality");
            static const char* const qualities[] = {"Quick", "Standard"};
            kit::fieldEnum("quality", "Quality", &fittingQuality_, qualities, 2);
            kit::fieldReadout("fluid", "Fluid", fluidName(jstr(fluidJson(), "name")).c_str());
        }
        if (!fittingCfdRunning_ && kit::secondaryButton("Compute K from CFD###compute_k")) startFittingCfd(st);
        const auto stateLabel = humanToken(fittingCfdState_);
        kit::keyValueRowId("cfd_state", "CFD state", stateLabel.c_str());
        if (!fittingCfdStage_.empty())
            kit::keyValueRowId("cfd_stage", "Stage", fittingCfdStage_.c_str());
        if (!fittingCfdJob_.empty())
            kit::keyValueRowId("cfd_job", "Job", fittingCfdJob_.c_str());
        if (fittingCfdRunning_) {
            const auto percent = kit::fmtQuantity(fittingCfdPct_, "%");
            kit::progressBar((float)(fittingCfdPct_ / 100), percent.c_str());
            if (kit::secondaryButton("Cancel CFD###cancel_fitting_cfd",
                                     !fittingCfdCancel_)) cancelFittingCfd(st);
        } else if (fittingCfdState_ == "idle") {
            kit::dimText("Compute the current draft directly; Apply fitting changes is not required.");
        }
        if (!fittingCfdError_.empty()) kit::inlineStatus(fittingCfdError_.c_str(), kit::Tone::Err);
        if (matching && fittingCfdResult_.is_object()) {
            kit::linePlot("cfd_curve", cfdPlot_);
            const auto& fit = jobj(jobj(fittingCfdResult_, "k_model"), "fit");
            kit::keyValueRowId("fit_k1", "2-K fit K1", val(fit, "K1").c_str());
            kit::keyValueRowId("fit_kinf", "2-K fit K infinite", val(fit, "Kinf").c_str());
            kit::keyValueRowId("correlation", "Correlation K", val(fittingCfdResult_, "correlation_K").c_str());
            kit::keyValueRowId("deviation", "Deviation", qty(fittingCfdResult_, "deviation_pct", "%").c_str());
            warningsList(fittingCfdResult_);
            if (kit::secondaryButton("Apply to fitting###apply_cfd",
                                     fittingCfdState_ == "done" &&
                                     !jbool(fittingCfdResult_, "_ui_applied", false) &&
                                     !jobj(fittingCfdResult_, "k_model").empty(),
                                     "This CFD loss model is already applied or unavailable.")) {
                json p = fittingDefinition()["params"];
                p["k_model"] = fittingCfdResult_["k_model"];
                submitEdit(st, json::array({{{"op", "update_fitting"}, {"id", selectedFitting_},
                    {"params", p}, {"name", fittingName_}}}), "CFD loss coefficient applied.");
            }
        } else if (!fittingCfdResult_.is_null()) {
            kit::dimText("The CFD result belongs to another fitting. Select its original fitting to apply it.");
        }
    }
    kit::cardEnd();
}

// ============================================================================================
// viewport
// ============================================================================================


nlohmann::json PipingPanel::routeEndpoints() const {
    json p = jobj(networkParams_, "route_inputs");
    p["start"].update({{"point", vec3(start_)}, {"dir", vec3(startDir_)}});
    p["end"].update({{"point", vec3(end_)}, {"dir", vec3(endDir_)}});
    p.update({{"nps", nps_}, {"schedule", kSchedules[scheduleIdx_]},
              {"material", kPipeMaterials[materialIdx_]}, {"clearance_mm", clearanceMm_}});
    p["start"].erase("face");
    p["end"].erase("face");
    if (!endpointFace_[0].is_null()) p["start"]["face"] = endpointFace_[0];
    if (!endpointFace_[1].is_null()) p["end"]["face"] = endpointFace_[1];
    if (gridMm_ > 0) p["grid_mm"] = gridMm_;
    else p.erase("grid_mm");
    if (!runName_.empty()) p["name"] = runName_;
    else p.erase("name");
    return p;
}

bool PipingPanel::routeFacePick(AppState& st, const json& selector, uint32_t body, uint32_t topo) {
    return wantsFacePick() && setRouteFace(st, armed_, selector, body, topo);
}

bool PipingPanel::setRouteFace(AppState& st, int endpoint, const json& selector,
                              uint32_t body, uint32_t topo) {
    if (endpoint < 1 || endpoint > 2 || busy() || !selector.is_object() ||
        jstr(selector, "kind") != "face" || body == 0 || topo == 0) return false;
    const std::string docId = store_.activeDocId(st);
    if (docId.empty() || st.docMirror.docId() != docId) return false;
    const auto it = st.faceNormalCache.find((static_cast<uint64_t>(body) << 32) | topo);
    double centroid[3], normal[3];
    if (it == st.faceNormalCache.end()) {
        say(Form::Route, "Face geometry is unavailable; select a resolved face or enter XYZ.", 2);
        return false;
    }
    for (int k = 0; k < 3; ++k) {
        normal[k] = it->second[k];
        centroid[k] = it->second[k + 3];
    }
    if (!normalize3(normal)) return false;
    json face = selector;
    face["_pick"] = {{"body", body}, {"topo", topo}};
    json clean = face;
    clean.erase("_pick");
    const auto generation = ++endpointGeneration_[endpoint - 1];
    const int revision = st.docMirror.revision();
    const double offset = clearanceMm_ + nps_ * 25.4 * 0.6 + 1.0;
    const json center = vec3(centroid), direction = vec3(normal);
    const std::weak_ptr<int> alive = alive_;
    armed_ = 0;
    endpointResolving_[endpoint - 1] = true;
    say(Form::Route, "Resolving the route endpoint face...", 0);
    st.rpc.call("doc_face_points", {{"doc_id", docId}, {"selector", clean}},
        [this, &st, alive, docId, revision, endpoint, generation, face, center, direction, offset]
        (const core::RpcResult& r) {
            if (alive.expired() || endpointGeneration_[endpoint - 1] != generation) return;
            endpointResolving_[endpoint - 1] = false;
            if (st.docMirror.docId() != docId || st.docMirror.revision() != revision) return;
            if (!r.ok || protocolError(r.result) || jarr(r.result, "points").empty()) {
                say(Form::Route, "Face resolution refused: " + envelopeWhy(r.result, r.err), 3);
                return;
            }
            double n[3], c[3];
            jvec3(center, c);
            jvec3(direction, n);
            // Native planar normals override the mesh cache; curved-face midpoint normals
            // retain the same cached geometry convention as viewport picking.
            if (r.result.contains("face_normal")) jvec3(r.result["face_normal"], n);
            if (!normalize3(n)) return;
            double* p = endpoint == 1 ? start_ : end_;
            double* d = endpoint == 1 ? startDir_ : endDir_;
            for (int k = 0; k < 3; ++k) {
                p[k] = c[k] + n[k] * offset;
                d[k] = endpoint == 1 ? n[k] : -n[k];
            }
            endpointFace_[endpoint - 1] = face;
            routeSeeded_ = true;
            persistNetwork(st);
            say(Form::Route, endpoint == 1 ? "Start set from face; exact coordinates remain editable."
                                          : "End set from face; exact coordinates remain editable.", 0);
        }, 30000);
    return true;
}

// ============================================================================================
// drawing — scaffold
// ============================================================================================

void PipingPanel::drawStudyRows(AppState& st) {
    syncUnits(st);
    const std::vector<json>& recs = store_.records();
    if (recs.empty()) return;
    // Rows act after the loop: opening a record re-adopts state the loop is iterating.
    int picked = -1;
    for (int i = 0; i < (int)recs.size(); ++i) {
        const json& rec = recs[i];
        const std::string id = jstr(rec, "id");
        const std::string type = jstr(rec, "type");
        const std::string title = jstr(rec, "name", type);
        const StudyRow* cached = nullptr;
        if (i < (int)studyRows_.size() && studyRows_[i].id == id) cached = &studyRows_[i];
        else for (const auto& row : studyRows_) if (row.id == id) { cached = &row; break; }
        kit::RowStyle rs;
        rs.icon = recordIcon(type);
        rs.secondary = cached ? cached->summary.c_str() : nullptr;
        rs.selected = open_ && (id == netSlot_.id || id == vesselSlot_.id || id == ductSlot_.id ||
                                id == flowSlot_.id);
        rs.tooltip = "Open this saved result in the Piping panel.";
        ImGui::PushID(i);
        if (kit::row("piping_rec", title.c_str(), rs)) picked = i;
        ImGui::PopID();
    }
    if (picked < 0) return;
    openRecord(st, recs[picked]);
}

bool PipingPanel::openStudyNamed(AppState& st, const std::string& name) {
    for (const json& rec : store_.records())
        if (jstr(rec, "name") == name) {
            openRecord(st, rec);
            return true;
        }
    return false;
}

void PipingPanel::setPage(Form f, int page) {
    const int count = pagesOf(f).count;
    page_[(int)f] = page < 0 ? 0 : page >= count ? count - 1 : page;
}

void PipingPanel::openRecord(AppState& st, json rec) {
    const std::string type = jstr(rec, "type");
    // A saved record opens on its own form; a result opens on its Results page.
    if (type == "piping_network") {
        adoptNetwork(st, rec);
        showPanel(st);
        enterForm(st, Form::Network);
    } else if (type == "pipe_flow") {
        adoptFlow(st, rec);
        handleCommand(st, "sim_piping_flow");
        page_[(int)Form::Flow] = hasResult(Form::Flow) ? pagesOf(Form::Flow).results : 0;
    } else if (type == "vessel") {
        adoptVessel(rec);
        handleCommand(st, "sim_piping_vessel");
        page_[(int)Form::Vessel] = hasResult(Form::Vessel) ? pagesOf(Form::Vessel).results : 0;
    } else {
        adoptDuct(rec);
        handleCommand(st, "sim_piping_duct");
        page_[(int)Form::Duct] = hasResult(Form::Duct) ? pagesOf(Form::Duct).results : 0;
    }
    // Another record is on screen: the previous one's status line (and pending Delete) no longer apply.
    store_.clearStatus();
    confirmDelete_ = 0;
}

std::string PipingPanel::headerTitle() const {
    auto recordName = [&](const Slot& slot, const char* unsaved) {
        const json* rec = store_.find(slot.id);
        return rec ? jstr(*rec, "name", unsaved) : std::string(unsaved);
    };
    if (form_ == Form::Vessel) return recordName(vesselSlot_, "New vessel");
    if (form_ == Form::Duct) return recordName(ductSlot_, "New duct system");
    // The saved record's name is what the study list shows; the network JSON's own name can
    // still be a generic default ("Piping") when an agent or import created it.
    if (network_.is_object()) {
        const std::string own = jstr(network_, "name", "Pipe network");
        const json* rec = store_.find(netSlot_.id);
        return rec ? jstr(*rec, "name", own) : own;
    }
    return std::string("Piping");
}

bool PipingPanel::onResultsPage() const {
    const int r = pagesOf(form_).results;
    return r >= 0 && page_[(int)form_] == r;
}

bool PipingPanel::hasResult(Form f) const {
    auto some = [](const json& j) { return j.is_object() && !j.empty(); };
    switch (f) {
        case Form::Route: return some(route_);
        case Form::Solve: return some(solve_);
        case Form::Spec: return some(spec_);
        case Form::Bom: return some(bom_);
        case Form::Flow: return some(flow_);
        case Form::Vessel: return some(vessel_);
        case Form::Duct: return some(duct_);
        default: return false;
    }
}

void PipingPanel::drawNoResult(const char* icon, const char* title, const char* body) {
    // No action button: the footer's primary is the one way to run it.
    kit::emptyState(icon, title, body);
}

void PipingPanel::draw(AppState& st) {
    syncUnits(st);
    refreshRows();
    tickFittingRequests(st);
    kit::panelBegin("piping_panel");

    // Header: the record's own icon; the type chip only when the title does not already say it;
    // where the record is saved as a status chip (the raw study id lives in its tooltip).
    const std::string title = headerTitle();
    kit::PanelHeaderSpec h;
    const Slot* slot = &netSlot_;
    bool haveWork = network_.is_object();
    switch (form_) {
        case Form::Vessel:
            h.icon = "vessel";
            h.typeChip = containsNoCase(title, "vessel") ? nullptr : "Vessel";
            slot = &vesselSlot_;
            haveWork = hasResult(Form::Vessel) || !vesselSlot_.id.empty();
            break;
        case Form::Duct:
            h.icon = "duct";
            h.typeChip = containsNoCase(title, "duct") ? nullptr : "Duct";
            slot = &ductSlot_;
            haveWork = hasResult(Form::Duct) || !ductSlot_.id.empty();
            break;
        case Form::Flow:
            h.icon = "study_piping";
            h.typeChip = containsNoCase(title, "pipe") || containsNoCase(title, "piping") ? nullptr : "Piping";
            // The title is the network's: until the flow run has its own record, the chip says
            // where the network is saved (the same chip as the other network forms).
            if (!flowSlot_.id.empty() || flowSlot_.addPending || hasResult(Form::Flow)) {
                slot = &flowSlot_;
                haveWork = hasResult(Form::Flow) || !flowSlot_.id.empty();
            }
            break;
        default:
            h.icon = "study_piping";
            h.typeChip = containsNoCase(title, "pipe") || containsNoCase(title, "piping") ? nullptr : "Piping";
            break;
    }
    std::string savedTip;
    if (!slot->id.empty()) {
        h.statusChip = "Saved";
        h.statusTone = kit::Tone::Ok;
        savedTip = "Saved in this document (study " + slot->id + "); it reloads with the file.";
        h.statusTip = savedTip.c_str();
    } else if (slot->addPending) {
        h.statusChip = "Saving\xe2\x80\xa6";
        h.statusTip = "Saving into the document.";
    } else if (haveWork) {
        h.statusChip = "Not saved";
        h.statusTone = kit::Tone::Warn;
        h.statusTip = "Not saved in a document: open a part to keep this with the file.";
    }
    h.title = title.c_str();
    h.doneTip = "Close the Piping panel and return to the study tree. Results are already saved.";
    h.titleFont = st.fonts.medium;
    // The saved record's Delete lives in the header (icon + tooltip), confirmed inline below.
    const int delKind = deleteKind();
    const bool busy = this->busy();
    kit::HeaderAction del;
    std::string delTip;
    int hit = -1;
    if (delKind != 0) {
        delTip = busy ? std::string("A piping request is still running.")
                      : std::string("Delete this ") + deleteNoun(delKind) + " from the document\xe2\x80\xa6";
        del.id = "delete";
        del.icon = "delete";
        del.tooltip = delTip.c_str();
        del.enabled = !busy;
        h.actions = &del;
        h.actionCount = 1;
        h.actionPressed = &hit;
    }
    const bool done = kit::panelHeader("hdr", h);
    if (hit == 0) confirmDelete_ = delKind;

    // Network form tabs (Vessel and Duct are their own records: no tabs there).
    if (form_ != Form::Vessel && form_ != Form::Duct) {
        int tab = 0;
        for (int i = 0; i < countOf(kTabFull); ++i)
            if (kTabForms[i] == form_) tab = i;
        if (kit::formTabs("forms", kTabFull, kTabShort, countOf(kTabFull), &tab, kTabTips))
            enterForm(st, kTabForms[tab]);
    }
    // The form's page switch, pinned with the tabs so it never scrolls away.
    const Pages pg = pagesOf(form_);
    if (pg.count > 1)
        kit::segmented("pages", pg.labels, pg.count, &page_[(int)form_], ImGui::GetContentRegionAvail().x,
                       pg.tips);

    const std::string scrollKey = std::string(kFormPage[(int)form_]) + "_" + std::to_string(page_[(int)form_]);
    kit::panelBodyBegin(scrollKey.c_str());
    const bool setupPage = !onResultsPage() && !(form_ == Form::Solve && page_[(int)form_] == 2);
    setupEdited_ = false;
    ImGui::BeginDisabled(busy);
    if (setupPage && (form_ == Form::Vessel || form_ == Form::Duct)) {
        kit::sectionHeader("Starting designs", form_ == Form::Vessel ? "vessel" : "duct");
        kit::dimText("Apply or drag a starting design. Every assigned dimension and design condition remains editable below.");
        const char* labels[2] = {form_ == Form::Vessel ? "Horizontal liquid vessel" : "Round distribution",
                                form_ == Form::Vessel ? "Vertical receiver" : "Quiet rectangular distribution"};
        int selectedPreset = form_ == Form::Vessel ? (vesselOrient_ == 1 ? 0 : 1) :
                             (!ductSegs_.empty() && ductSegs_.front().shape == 1 ? 1 : 0);
        for (int i = 0; i < 2; ++i) {
            ImGui::PushID(i);
            if (kit::secondaryButton((std::string(labels[i]) + "###setup_preset").c_str())) {
                selectedPreset = i;
                applySetupPreset(st, form_, i);
            }
            if (ImGui::BeginDragDropSource()) {
                selectedPreset = i;
                if (!ImGui::GetDragDropPayload()) {
                    json p = setupDragOrigin(st, form_);
                    p["preset"] = i;
                    const std::string encoded = p.dump();
                    ImGui::SetDragDropPayload("CHUDWARE_PIPE_PRESET", encoded.c_str(), encoded.size() + 1, ImGuiCond_Once);
                }
                ImGui::TextUnformatted(labels[i]);
                ImGui::EndDragDropSource();
            }
            ImGui::PopID();
        }
        kit::fieldReadout("selected_preset", "Selected preset", labels[selectedPreset],
                         kit::FieldOpts().withTip("Selection follows vessel orientation or the main duct shape. Apply restores this starting design's editable defaults."));
        if (kit::secondaryButton("Apply preset here###preset_target"))
            applySetupPreset(st, form_, selectedPreset);
        if (ImGui::BeginDragDropTarget()) {
            if (const auto* payload = ImGui::AcceptDragDropPayload("CHUDWARE_PIPE_PRESET")) {
                if (payload->IsDelivery() && payload->DataSize > 1 &&
                    static_cast<const char*>(payload->Data)[payload->DataSize - 1] == '\0') {
                    const json p = json::parse(static_cast<const char*>(payload->Data),
                        static_cast<const char*>(payload->Data) + payload->DataSize - 1, nullptr, false);
                    if (acceptsSetupDrag(st, form_, p) &&
                        p.contains("preset") && p["preset"].is_number_integer())
                        applySetupPreset(st, form_, p["preset"] == 0 ? 0 : p["preset"] == 1 ? 1 : -1);
                }
            }
            ImGui::EndDragDropTarget();
        }
        kit::gapSection();
    }
    if (delKind != 0 && confirmDelete_ == delKind) {
        // Inline confirmation (no modal), the study tree's idiom.
        const std::string q = "Delete \"" + title + "\"? Its inputs and results are removed from this document.";
        kit::inlineStatus(q.c_str(), kit::Tone::Warn);
        if (kit::dangerButton("Delete###del_yes", !busy, "A piping request is still running.")) deleteCurrent(st);
        ImGui::SameLine();
        if (kit::secondaryButton("Cancel###del_no")) confirmDelete_ = 0;
        kit::gapRow();
    }
    switch (form_) {
        case Form::Network: drawNetworkForm(st); break;
        case Form::Route: drawRouteForm(st); break;
        case Form::Solve: drawSolveForm(st); break;
        case Form::Spec: drawSpecForm(st); break;
        case Form::Bom: drawBomForm(st); break;
        case Form::Flow: drawFlowForm(st); break;
        case Form::Vessel: drawVesselForm(st); break;
        case Form::Duct: drawDuctForm(st); break;
        case Form::Fittings: drawFittingsForm(st); break;
        case Form::Count: break;
    }
    if (setupPage && form_ != Form::Route && form_ != Form::Fittings) drawNativeEditor(st);
    if (setupPage && setupEdited_ && !busy) invalidateSetup(st, form_);
    ImGui::EndDisabled();
    kit::panelBodyEnd();

    kit::panelFooterBegin();
    drawFooter(st);
    kit::panelEnd();

    if (done) close();
    drawNetworkLegend(st);
}

void PipingPanel::drawNetworkLegend(AppState& st) {
    if (!open_ || colorBy_ == 0 || form_ == Form::Vessel || form_ == Form::Duct ||
        st.activeWorkspace != AppState::Workspace::Simulate || !st.showViewport ||
        st.leftPaneTab != AppState::LP_Simulation) return;
    ImVec2 clearMin, clearMax;
    if (!ui::viewportClearRect(clearMin, clearMax)) return;
    const auto& links = jarr(solve_, "links");
    const auto& nodes = jarr(solve_, "nodes");
    const void* linkIdentity = links.get_ptr<const json::array_t*>();
    const void* nodeIdentity = nodes.get_ptr<const json::array_t*>();
    if (legendRevision_ != networkRevision_ || legendColorBy_ != colorBy_ || legendUs_ != us_ ||
        legendLinks_ != linkIdentity || legendNodes_ != nodeIdentity) {
        legendRevision_ = networkRevision_; legendColorBy_ = colorBy_; legendUs_ = us_;
        legendLinks_ = linkIdentity; legendNodes_ = nodeIdentity;
        float low = std::numeric_limits<float>::max(), high = -low;
        bool solved = false;
        const auto number = [](const json& value, const char* key) {
            return jhas(value, key) ? value[key].get<float>() : 0.0f;
        };
        // Match MeshCache::update in sim/pipe_mesh.cpp: only rendered, solved runs contribute;
        // pressure uses both endpoint nodes (missing nodes are zero), velocity uses magnitude.
        std::unordered_map<std::string, float> pressure;
        for (const auto& node : nodes)
            pressure.emplace(jstr(node, "id"), number(node, "pressure_kpa"));
        for (const auto& row : runRows_) {
            if (jbool(row.native, "suppressed", false)) continue;
            int pointCount = 0;
            float previous[3] = {};
            for (const auto& point : jarr(row.native, "path")) {
                if (!point.is_array() || point.size() != 3 ||
                    !point[0].is_number() || !point[1].is_number() || !point[2].is_number()) continue;
                const float p[3] = {point[0].get<float>(), point[1].get<float>(), point[2].get<float>()};
                if (!std::isfinite(p[0]) || !std::isfinite(p[1]) || !std::isfinite(p[2])) continue;
                const float dx = p[0] - previous[0], dy = p[1] - previous[1], dz = p[2] - previous[2];
                if (pointCount == 0 || std::sqrt(dx * dx + dy * dy + dz * dz) > 1e-5f) {
                    std::copy_n(p, 3, previous);
                    ++pointCount;
                }
            }
            if (pointCount < 2) continue;
            for (const auto& link : links) if (jstr(link, "id") == row.id) {
                solved = true;
                if (colorBy_ == 1) {
                    const auto from = pressure.find(jstr(link, "from"));
                    const auto to = pressure.find(jstr(link, "to"));
                    const float a = from != pressure.end() ? from->second : 0;
                    const float b = to != pressure.end() ? to->second : 0;
                    low = std::min({low, a, b}); high = std::max({high, a, b});
                } else {
                    const float velocity = std::fabs(number(link, "velocity_ms"));
                    low = std::min(low, velocity); high = std::max(high, velocity);
                }
                break;
            }
        }
        legendRangeValid_ = solved && std::isfinite(low) && std::isfinite(high) && high >= low;
        // The range is found in SI (the coloring's own scale) and shown in the display unit; the
        // conversion is linear, so the colour stops stay where they are.
        const Q q = colorBy_ == 1 ? Q::Press : Q::Vel;
        pipeLegendMin_ = (float)toDisp(low, q, us_); pipeLegendMax_ = (float)toDisp(high, q, us_);
        pipeLegendTitle_ = colorBy_ == 1 ? "Pressure" : "Velocity";
        pipeLegendUnits_ = colorBy_ == 1 ? std::string(unitOf(q, us_)) + " gauge" : std::string(unitOf(q, us_));
    }
    if (!legendRangeValid_) return;
    // Reuse the shared result legend without claiming the persistent FEA/CFD scalar state.
    const bool previousOn = st.legendOn;
    const float previousMin = st.legendMin, previousMax = st.legendMax;
    st.legendOn = true; st.legendMin = pipeLegendMin_; st.legendMax = pipeLegendMax_;
    st.legendTitle.swap(pipeLegendTitle_); st.legendUnits.swap(pipeLegendUnits_);
    auto* dl = ImGui::GetForegroundDrawList();
    dl->PushClipRect(clearMin, clearMax, true);
    ui::drawLegend(st, dl, clearMin, clearMax);
    dl->PopClipRect();
    st.legendTitle.swap(pipeLegendTitle_); st.legendUnits.swap(pipeLegendUnits_);
    st.legendOn = previousOn; st.legendMin = previousMin; st.legendMax = previousMax;
}

void PipingPanel::drawFooter(AppState& st) {
    const bool flowBusy = flowRunning_ || flowPreparing_;
    const bool reqBusy = store_.inflight() > 0;
    // The running caption belongs to the form that started the work, never to another form.
    const bool mineBusy = form_ == Form::Flow ? flowBusy : reqBusy && busyForm_ == form_;
    const bool onResults = onResultsPage();
    const bool pumpPage = form_ == Form::Solve && page_[(int)Form::Solve] == 2;
    const bool cfdDraftNeedsModel = fittingSource_ == 5 && !selectedFitting_.empty() &&
        jstr(jobj(jobj(fittingDraft_, "params"), "k_model"), "kind") != "table";
    const bool cfdReadyToApply = fittingCfdState_ == "done" &&
        fittingCfdTarget_ == selectedFitting_ && !selectedFitting_.empty() &&
        fittingCfdNetwork_ == jstr(network_, "id") && fittingCfdDoc_ == store_.activeDocId(st) &&
        !jbool(fittingCfdResult_, "_ui_applied", false) && !jobj(fittingCfdResult_, "k_model").empty();

    // This form's status line only: not while the running caption says the same, and not an Ok /
    // Warn line repeating what the visible result card (its chips and warnings) already says —
    // each message once per pane. Errors always show: no card carries them.
    const bool resultShown = (onResults && hasResult(form_)) || (form_ == Form::Bom && hasResult(Form::Bom)) ||
                             (pumpPage && pumpDrive_.is_object() && !pumpDrive_.empty());
    const int tone = store_.statusTone();
    if (statusForm_ == form_ && !mineBusy && !(resultShown && (tone == 1 || tone == 2))) {
        store_.drawStatus();
        // The warnings that line counts, right under it: a chip that opens/closes their list.
        if (!warnList_.empty() && store_.status() == warnStatus_) {
            const std::string chipLabel = countNoun((long long)warnList_.size(), "warning");
            ImGui::PushID("status_warnings");
            kit::toggleChip(chipLabel.c_str(), &warnOpen_, kit::Tone::Warn,
                            warnOpen_ ? "Hide these warnings." : "Show these warnings.");
            ImGui::PopID();
            if (warnOpen_)
                for (const auto& w : warnList_) kit::inlineStatus(w.c_str(), kit::Tone::Warn);
        }
    }

    // The current page's one primary action and why it cannot run. On a Results page that has a
    // result the action is a secondary "Re-…" (the standard's Results-step rule).
    const bool have = hasResult(form_);
    bool rerun = onResults && have;
    const char* label = "Run";
    std::string blocker;
    switch (form_) {
        case Form::Network: label = network_.is_object() ? "Route pipe" : "New network"; break;
        case Form::Route:
            label = routeMode_ == 0 ? (tool_ == Tool::DrawRun ? "Stop drawing" : "Start drawing") : "Route pipe";
            if (routeMode_ != 0) blocker = routeBlocker();
            else if (!(nps_ > 0)) blocker = "Enter a nominal pipe size.";
            break;
        case Form::Fittings:
            label = cfdReadyToApply ? "Apply CFD K to fitting" :
                    cfdDraftNeedsModel ? "Compute K from CFD" :
                    fittingDirty_ ? "Apply fitting changes" :
                    (tool_ == Tool::InsertFitting ? "Stop placing fittings" :
                     fittingPalette_ <= 2 ? "Draw connected pipe" : "Place fitting");
            if (runRows_.empty() && fittingPalette_ > 2) blocker = "Draw a pipe run first.";
            break;
        case Form::Count: break;
        case Form::Solve:
            if (pumpPage) {
                label = "Size pump drive";
                blocker = pumpDriveBlocker();
            } else {
                label = rerun ? "Re-solve network" : "Solve network";
                blocker = solveBlocker();
            }
            break;
        case Form::Spec: label = rerun ? "Re-run spec check" : "Run spec check"; blocker = specBlocker(); break;
        case Form::Bom:
            rerun = have;   // one page: the result sits right under its one input
            label = have ? "Rebuild BOM" : "Build BOM";
            blocker = bomBlocker();
            break;
        case Form::Flow: label = rerun ? "Re-run internal CFD" : "Run internal CFD"; blocker = flowBlocker(st); break;
        case Form::Vessel: label = rerun ? "Re-size vessel" : "Size vessel"; blocker = vesselBlocker(); break;
        case Form::Duct: label = rerun ? "Re-size ducts" : "Size ducts"; blocker = ductBlocker(); break;
    }
    if (blocker.empty() && reqBusy && !mineBusy && form_ != Form::Network && form_ != Form::Flow)
        blocker = "Wait for the running piping request to finish.";
    kit::ActionSpec a;
    a.primaryLabel = label;
    a.primaryEnabled = blocker.empty();
    a.blocker = blocker.c_str();
    a.primaryAsSecondary = rerun;

    std::string stage;
    if (form_ == Form::Flow && flowBusy) {
        stage = flowPreparing_ ? std::string("Exporting the fluid volume") : std::string("Solving");
        if (flowRunning_ && st.simClient) {
            const std::string s = st.simClient->stage();
            if (!s.empty()) stage += " \xc2\xb7 " + s;
            a.elapsed = st.simClient->elapsedSeconds();
            a.cancelRequested = st.simClient->cancelRequested();
        }
        a.running = true;
        a.stage = stage.c_str();
        a.cancellable = flowRunning_ && st.simClient != nullptr;
        a.cancelBlocker = "the fluid volume is still exporting; Cancel appears once the solve starts.";
    } else if (form_ == Form::Fittings && fittingCfdRunning_) {
        a.running = true; a.stage = fittingCfdStage_.c_str();
        a.progress = (float)(fittingCfdPct_ / 100);
        a.cancellable = true; a.cancelRequested = fittingCfdCancel_;
    } else if (mineBusy) {
        a.running = true;
        a.stage = busyStage_.c_str();
        a.elapsed = ImGui::GetTime() - busySince_;
        a.cancellable = false;
        a.cancelBlocker = "engine sizing and solve requests finish on their own within seconds.";
    }
    switch (kit::actionFooter("action", a)) {
        case kit::FooterAction::Primary:
            switch (form_) {
                case Form::Network:
                    if (network_.is_object())
                        enterForm(st, Form::Route);
                    else
                        newNetwork(st);
                    break;
                case Form::Route:
                    if (routeMode_ == 0) setTool(st, tool_ == Tool::DrawRun ? Tool::None : Tool::DrawRun);
                    else runRoute(st);
                    break;
                case Form::Fittings:
                    if (cfdReadyToApply) {
                        json p = fittingDefinition()["params"];
                        p["k_model"] = fittingCfdResult_["k_model"];
                        submitEdit(st, json::array({{{"op", "update_fitting"}, {"id", selectedFitting_},
                            {"params", p}, {"name", fittingName_}}}), "CFD loss coefficient applied.");
                    } else if (cfdDraftNeedsModel) {
                        startFittingCfd(st);
                    } else if (fittingDirty_ && !selectedFitting_.empty()) {
                        const json definition = fittingDefinition();
                        submitEdit(st, json::array({{{"op", "update_fitting"}, {"id", selectedFitting_},
                            {"name", fittingName_}, {"params", definition["params"]}}}), "Fitting updated.");
                    } else {
                        setTool(st, tool_ == Tool::InsertFitting ? Tool::None :
                            fittingPalette_ <= 2 ? Tool::DrawRun : Tool::InsertFitting);
                        if (fittingPalette_ <= 2) say(Form::Route, fittingPalette_ == 2 ?
                            "Start or end the new run on an existing pipe to add a tee." :
                            "Corners become elbows automatically.", 0);
                    }
                    break;
                case Form::Count: break;
                case Form::Solve:
                    if (pumpPage)
                        runPumpDrive(st);
                    else
                        runSolve(st);
                    break;
                case Form::Spec: runSpecCheck(st); break;
                case Form::Bom: runBom(st); break;
                case Form::Flow: runFlow(st); break;
                case Form::Vessel: runVessel(st); break;
                case Form::Duct: runDuct(st); break;
            }
            break;
        case kit::FooterAction::Cancel:
            if (form_ == Form::Fittings && fittingCfdRunning_) cancelFittingCfd(st);
            else if (st.simClient) st.simClient->cancel();
            break;
        default:
            break;
    }
}

// ============================================================================================
// drawing — forms
// ============================================================================================

void PipingPanel::drawNetworkForm(AppState& st) {
    if (!network_.is_object()) {
        // The footer's "New network" is the one action (no second button in the body).
        kit::emptyState("study_piping", "No pipe network yet",
                        "A network holds routed pipe runs and their fittings. It is saved in this "
                        "document with its solve, B31.3 check and bill of materials.");
        return;
    }
    const bool busy = store_.inflight() > 0;
    // The header already shows the name and whether it is saved; New Network is the ribbon's and
    // Delete is the header's. This card holds what the network is designed for, with the name
    // field last (the header title is its readout).
    if (kit::cardBegin("network", "Design conditions", "settings")) {
        kit::FieldGroup g("network_fields", "Design temperature");
        if (kit::fieldNumber("design_p", "Design pressure", &designPressureKpa_,
                             fieldQ(Q::Press, us_).withMin(0.0).withHint(hintQ(1000.0, Q::Press, us_).c_str())
                                 .withTip("Gauge design pressure used by the B31.3 wall, MAWP and "
                                          "flange-rating checks.")))
            invalidateSetup(st, Form::Network);
        if (kit::fieldNumber("design_t", "Design temperature", &designTempC_,
                             fieldQ(Q::Temp, us_).withHint(hintQ(40.0, Q::Temp, us_).c_str())
                                 .withTip("Design metal temperature: sets the allowable stress and "
                                          "flange rating.")))
            invalidateSetup(st, Form::Network);
        std::string name = jstr(network_, "name", "Pipe network");
        if (kit::fieldText("name", "Name", &name,
                           kit::FieldOpts().withHint("Pipe network").withTip("The network's name in the study list."))) {
            network_["name"] = name;
            persistNetwork(st);
        }
    }
    kit::cardEnd();

    kit::sectionHeader("Runs", "route_pipe", runRows_.empty() ? nullptr : fittingsCount_.c_str(), (int)runRows_.size());
    if (runRows_.empty())
        kit::inlineStatus("No runs yet: Draw Pipe places the first run on the working plane.", kit::Tone::Info);
    for (int i = 0; i < (int)runRows_.size(); ++i) {
        const auto& r = runRows_[i];
        kit::RowStyle rs;
        rs.icon = "route_pipe";
        rs.secondary = r.detail.c_str();
        rs.selected = i == selectedRun_ && selectedFitting_.empty();
        rs.tooltip = r.id.c_str();
        ImGui::PushID(i);
        if (kit::row("run", r.label.c_str(), rs)) selectRun(st, r.id);
        ImGui::PopID();
    }
    if (!runRows_.empty()) {
        const bool haveSel = selectedRun_ >= 0 && selectedRun_ < (int)runRows_.size();
        if (kit::dangerButton("Remove selected run###rm_run", haveSel && !busy,
                              busy ? "A piping request is still running." : "Select a run first."))
            removeRun(st, selectedRun_);
    }
}

void PipingPanel::drawRouteForm(AppState& st) {
    const kit::FieldOpts len = fieldQ(Q::Len, us_);
    static const char* const kDirHints[] = {"X", "Y", "Z"};
    static const char* const modes[] = {"Draw", "Auto-route"};
    if (kit::segmented("route_mode", modes, 2, &routeMode_, ImGui::GetContentRegionAvail().x)) {
        tool_ = Tool::None; armed_ = 0;
    }
    if (routeMode_ == 0) {
        if (kit::cardBegin("working_plane", "Working plane", "route_pipe")) {
            kit::FieldGroup g("plane_fields", "Elevation offset");
            static const char* const planes[] = {"Auto", "XY", "XZ", "YZ"};
            int planeChoice = draw_.autoPlane ? 0 : std::clamp(draw_.plane, 0, 2) + 1;
            if (kit::fieldEnum("plane", "Plane", &planeChoice, planes, 4)) {
                draw_.autoPlane = planeChoice == 0;
                if (planeChoice > 0) draw_.plane = planeChoice - 1;
            }
            if (draw_.autoPlane) kit::dimText("(following the view)");
            kit::fieldNumber("offset", "Elevation offset", &draw_.planeOffsetMm, kit::FieldOpts(len));
            kit::fieldNumber("pitch", "Grid pitch", &draw_.gridMm, kit::FieldOpts(len).withMin(0));
            kit::fieldCheckbox("grid_snap", "Snap to grid", &draw_.snapGrid);
            kit::fieldCheckbox("axis_snap", "Snap to axes", &draw_.snapAxis);
            kit::fieldCheckbox("pipe_snap", "Snap to pipes", &draw_.snapPipes);
            kit::inlineStatus(tool_ == Tool::DrawRun ? "Drawing armed: click points in the viewport." :
                              "Ready: Start drawing arms the viewport tool.", kit::Tone::Info);
            kit::dimText("Click to add points; Enter finishes the run; Escape cancels. Pipe snaps connect branches.");
        }
        kit::cardEnd();
    }
    if (routeMode_ != 0) {
    if (kit::cardBegin("endpoints", "Endpoints", "route_pipe")) {
        kit::FieldGroup g("endpoint_fields", "Start direction");
        if (kit::fieldVec3("start", "Start point", start_,
                          kit::FieldOpts(len).withTip("Where the run begins (world coordinates)."))) {
            endpointFace_[0] = json();
            setupEdited_ = true;
        }
        setupEdited_ |= kit::fieldVec3("start_dir", "Start direction", startDir_,
                       kit::FieldOpts().withCellHints(kDirHints).withGroupCommit()
                           .withTip("Direction the pipe leaves the start point (0, 0, 0 lets the router choose)."));
        // Face-pick arm buttons: secondary while idle, accent "Picking…" while armed (same
        // height as the "Chain from last run" button beside it), stable ids (the label changes).
        auto pickToggle = [&](const char* id, int which, const char* idle, const char* tip) {
            ImGui::PushID(id);
            const bool on = armed_ == which;
            const bool hit = on ? kit::primaryButton("Picking\xe2\x80\xa6###arm")
                                : kit::secondaryButton((std::string(idle) + "###arm").c_str());
            sim::FaceDrop drop;
            if (sim::acceptFaceDrop(store_.activeDocId(st), drop))
                setRouteFace(st, which, drop.selector, drop.body, drop.topo);
            kit::itemTooltip(on ? "Click a face of the model in the viewport; click here again to stop." : tip);
            if (hit) armed_ = on ? 0 : which;
            ImGui::PopID();
        };
        pickToggle("pick_start", 1, "Start from face",
                   "Pick a face in the viewport: the run starts just off its center, leaving along its normal.");
        const json& runs = jarr(network_, "runs");
        if (!runs.empty()) {
            ImGui::SameLine();
            if (kit::secondaryButton("Chain from last run###chain")) {
                routeSeeded_ = false;
                seedRouteDefaults(st);
                endpointFace_[0] = json();
                setupEdited_ = true;
            }
            kit::itemTooltip("Start where the newest run ends, continuing its direction.");
        }
        if (kit::fieldVec3("end", "End point", end_,
                          kit::FieldOpts(len).withTip("Where the run ends (world coordinates)."))) {
            endpointFace_[1] = json();
            setupEdited_ = true;
        }
        setupEdited_ |= kit::fieldVec3("end_dir", "End direction", endDir_,
                       kit::FieldOpts().withCellHints(kDirHints).withGroupCommit()
                           .withTip("Direction of travel arriving at the end point (0, 0, 0 = router's choice)."));
        pickToggle("pick_end", 2, "End at face",
                   "Pick a face in the viewport: the run ends just off its center, arriving into it.");
        if (armed_ != 0) kit::dimText("Click a face of the model in the viewport.");
    }
    kit::cardEnd();
    }

    if (kit::cardBegin("pipe", "Pipe", "line")) {
        kit::FieldGroup g("pipe_fields", "Clearance");
        // The designator alone ("NPS 2", like every run, spec and BOM row): NPS is a size name,
        // not a measured inch dimension, so the field carries no unit.
        setupEdited_ |= kit::fieldNumber("nps", "NPS", &nps_,
                         kit::FieldOpts().withMin(0.0).withHint("2")
                             .withTip("Nominal pipe size designator (ASME B36.10M/B36.19M): 2 for NPS 2, "
                                      "1.5 for NPS 1-1/2."));
        setupEdited_ |= kit::fieldEnum("schedule", "Schedule", &scheduleIdx_, kSchedules, countOf(kSchedules));
        setupEdited_ |= kit::fieldEnum("material", "Material", &materialIdx_, kPipeMaterials, countOf(kPipeMaterials));
        static const char* const bends[] = {"Long radius", "Short radius"};
        if (routeMode_ == 0)
            setupEdited_ |= kit::fieldEnum("bend", "Bend radius", &bendRadius_, bends, 2);
        else kit::fieldReadout("bend", "Bend radius", "Long radius");
        if (routeMode_ != 0) {
        setupEdited_ |= kit::fieldNumber("clearance", "Clearance", &clearanceMm_,
                         kit::FieldOpts(len).withMin(0.0).withHint(hintQ(50.0, Q::Len, us_).c_str())
                             .withTip("Minimum gap kept between the pipe's outside and every body of this model."));
        bool gridSet = gridMm_ > 0.0;
        if (kit::fieldNumber("grid", "Grid pitch", &gridMm_,
                             kit::FieldOpts(len).withMin(0.0).withPresent(&gridSet)
                                 .withHint("Auto (about one OD)")
                                 .withTip("Routing grid pitch; leave empty for the engine default."))) {
            if (!gridSet) gridMm_ = 0.0;
            setupEdited_ = true;
        }
        }
        setupEdited_ |= kit::fieldText("run_name", "Run name", &runName_,
                       kit::FieldOpts().withHint("Automatic").withTip("Optional name for the new run."));
    }
    kit::cardEnd();

    if (!route_.is_object() || route_.empty()) return;
    const bool clearOk = jnum(route_, "min_clearance_mm", 0.0) + 1e-6 >= clearanceMm_;
    // The result card carries the route's warnings (wrapped); the footer lists them too.
    if (kit::cardBegin("last_route", "Last route", "route_pipe")) {
        const std::string run = runName(jstr(route_, "run_id", kEmDash));
        const std::string length = qtyQ(route_, "length_mm", Q::Len, us_);
        const std::string bends = countNoun((long long)jnum(route_, "bends"), "long-radius elbow");
        const std::string gap = qtyQ(route_, "min_clearance_mm", Q::Len, us_, 3);
        kit::keyValueRowId("run", "Run", run.c_str());
        kit::keyValueRowId("length", "Length", length.c_str());
        kit::keyValueRowId("bends", "Bends", bends.c_str());
        kit::keyValueRowId("clearance", "Min clearance", gap.c_str(), clearOk ? kit::Tone::Ok : kit::Tone::Warn);
        warningsList(route_);
    }
    kit::cardEnd();
    const json& path = jarr(route_, "path");
    if (path.empty()) return;
    const std::string ptsTitle = "Path points (" + std::to_string(path.size()) + ")";
    if (kit::cardBegin("path_points", ptsTitle.c_str(), "line", &routePathOpen_)) {
        const std::string cx = colQ("X", Q::Len, us_), cy = colQ("Y", Q::Len, us_), cz = colQ("Z", Q::Len, us_);
        if (kit::tableBegin("points", {{"#", kit::TableAlign::Right, 0.4f, true},
                                       {cx.c_str(), kit::TableAlign::Right},
                                       {cy.c_str(), kit::TableAlign::Right},
                                       {cz.c_str(), kit::TableAlign::Right}})) {
            for (std::size_t i = 0; i < path.size(); ++i) {
                double p[3];
                if (!jvec3(path[i], p)) continue;
                kit::tableRow();
                kit::tableCell(std::to_string(i).c_str());
                for (int k = 0; k < 3; ++k) kit::tableCell(kit::fmtValue(toDisp(p[k], Q::Len, us_), 5).c_str());
            }
        }
        kit::tableEnd();
    }
    kit::cardEnd();
}

void PipingPanel::drawSolveForm(AppState& st) {
    const json& runs = jarr(network_, "runs");
    if (runs.empty()) {
        if (kit::emptyState("run_study", "Nothing to solve yet",
                            "Route at least one pipe run, then fix a supply pressure and a demand.",
                            "Route pipe"))
            enterForm(st, Form::Route);
        return;
    }
    const int page = page_[(int)Form::Solve];
    if (page == 1) {
        drawSolveResults();
        return;
    }
    if (page == 2) {
        drawPumpDrive(st);
        return;
    }
    const bool gas = gasNetwork();
    if (kit::cardBegin("fluid", "Fluid", "fluid")) {
        kit::FieldGroup g("fluid_fields", "Temperature");
        if (fluidOverride_.is_object()) {
            // A handed-off fluid given by its properties (e.g. the hydraulic circuit's oil).
            const std::string desc = jstr(fluidOverride_, "name", "custom") + " \xc2\xb7 " +
                                     qtyQ(fluidOverride_, "rho", Q::Dens, us_) + " \xc2\xb7 " +
                                     qtyQ(fluidOverride_, "mu", Q::Visc, us_);
            kit::fieldReadout("handoff", "Fluid", desc.c_str());
            if (kit::secondaryButton("Use a built-in fluid###builtin")) {
                fluidOverride_ = json();
                ++fluidDisplayGeneration_[0];
                fluidIdx_ = 0;
                networkParams_["fluid"] = {{"name", "water"}, {"T", fluidTempC_ + 273.15}};
                networkParams_["network_type"] = "liquid";
                invalidateSetup(st, Form::Solve);
            }
            kit::itemTooltip("Replace the handed-off fluid properties with water or one of the built-in gases.");
        } else {
            std::vector<std::string> fluids(kFluidLabels, kFluidLabels + countOf(kFluidLabels));
            int selection = fluidIdx_;
            if (selection < 0) {
                selection = (int)fluids.size();
                fluids.push_back(jstr(jobj(networkParams_, "fluid"), "name", "Native fluid"));
            }
            if (kit::fieldCombo("fluid", "Fluid", &selection, fluids,
                               kit::FieldOpts().withTip("Native aliases are resolved by the engine; selecting a preset explicitly replaces the fluid.")) &&
                selection >= 0 && selection < countOf(kFluidKeys)) {
                fluidIdx_ = selection;
                ++fluidDisplayGeneration_[0];
                networkParams_["fluid"] = {{"name", kFluidKeys[fluidIdx_]}, {"T", fluidTempC_ + 273.15}};
                for (auto& b : boundary_) b.kind = std::min(b.kind, fluidIdx_ == 0 ? 2 : 1);
                networkParams_["network_type"] = fluidIdx_ == 0 ? "liquid" : "gas";
                invalidateSetup(st, Form::Solve);
            }
            if (kit::fieldNumber("temp", "Temperature", &fluidTempC_,
                                 fieldQ(Q::Temp, us_).withHint(hintQ(20.0, Q::Temp, us_).c_str())
                                     .withTip("Fluid temperature for its properties."))) {
                auto& fluid = networkParams_["fluid"];
                if (!fluid.is_object()) fluid = {{"name", kFluidKeys[fluidIdx_ >= 0 ? fluidIdx_ : 0]}};
                fluid["T"] = fluidTempC_ + 273.15;
                fluid.erase("T_C");
                resolveFluidDisplay(st, false);
                invalidateSetup(st, Form::Solve);
            }
        }
    }
    kit::cardEnd();

    drawRunPorts(st);
    kit::sectionHeader("Boundary conditions", "flow_bc", nullptr, (int)boundary_.size());
    const auto& labels = runLabels_;
    const char* const* kinds = gas ? kGasKinds : kLiquidKinds;
    const int nKinds = gas ? countOf(kGasKinds) : countOf(kLiquidKinds);
    int removeIdx = -1;
    bool edited = false;
    for (int i = 0; i < (int)boundary_.size(); ++i) {
        Boundary& b = boundary_[i];
        const int kind = std::clamp(b.kind, 0, nKinds - 1);
        // The card names the condition; where it sits (run, end) are its fields.
        ImGui::PushID(i);
        bool removed = false;
        if (kit::removableCard("bc", kinds[kind], boundaryIcon(gas, kind), nullptr, "Remove this condition",
                               &removed)) {
            kit::FieldGroup g("bc_fields", "Value");
            kit::secondaryButton("Drop run end here###boundary_port");
            std::string droppedRun; int droppedEnd = 0;
            if (acceptRunPort(st, droppedRun, droppedEnd))
                assignBoundaryPort(st, i, droppedRun, droppedEnd);
            if (b.run < 0) {
                kit::dimText("Native node/XYZ boundary: edit its exact address/value in Native parameters, or drop a run end to assign.");
                kit::fieldReadout("native_bc", "Address", jstr(b.native, "node", "XYZ address").c_str());
            }
            if (b.run >= 0) {
                int run = std::clamp(b.run, 0, (int)labels.size() - 1);
                if (kit::fieldCombo("run", "Run", &run, labels)) {
                    b.run = run;
                    edited = true;
                }
                edited |= kit::fieldEnum("end", "End", &b.end, kEnds, countOf(kEnds));
                const char* tip = kind == 0 ? "Gauge pressure fixed at this node."
                                  : (!gas && kind == 1) ? "Hydraulic head fixed at this node."
                                                        : "Drawn out of the network (+) or fed in (-).";
                edited |= kit::fieldNumber("value", kinds[kind], &b.value,
                                           fieldQ(boundaryQ(gas, kind), us_).withTip(tip));
            }
        }
        kit::cardEnd();
        ImGui::PopID();
        if (removed) removeIdx = i;
    }
    if (removeIdx >= 0) {
        boundary_.erase(boundary_.begin() + removeIdx);
        edited = true;
    }
    // One "+" per condition type: the type is chosen at creation.
    for (int k = 0; k < nKinds; ++k) {
        if (k > 0) ImGui::SameLine();
        const std::string add = std::string("+ ") + kinds[k] + "###add_bc_" + std::to_string(k);
        if (kit::secondaryButton(add.c_str())) {
            const bool demand = gas ? k == 1 : k == 2;
            boundary_.push_back({demand ? (int)runs.size() - 1 : 0, demand ? 1 : 0, k, boundaryDefault(gas, k)});
            edited = true;
        }
        const bool demand = gas ? k == 1 : k == 2;
        kit::itemTooltip(demand ? (gas ? "A mass flow drawn out (+) or fed in (-) at a run end."
                                       : "A flow drawn out (+) or fed in (-) at a run end.")
                                : "A fixed node: the network needs at least one.");
    }
    if (edited) invalidateSetup(st, Form::Solve);
}

void PipingPanel::drawSolveResults() {
    if (!hasResult(Form::Solve)) {
        drawNoResult("run_study", "No solution yet",
                     "Solve the network to see the flow, velocity and pressure drop of every run.");
        return;
    }
    // Outcome first, then the per-run and per-node tables (runs and nodes by name, the engine's
    // ids only in tooltips).
    const bool conv = jbool(solve_, "converged", false);
    const json& fluid = jobj(solve_, "fluid");
    const std::string fluidTxt = fluidName(jstr(fluid, "name")) + " \xc2\xb7 " + qtyQ(fluid, "rho", Q::Dens, us_);
    kit::chipFlowBegin("solve_outcome");
    kit::flowChip(conv ? "Converged" : "Not converged", conv ? kit::Tone::Ok : kit::Tone::Err, true);
    kit::flowValueChip("Iterations", fmtText(solve_, "iterations").c_str());
    kit::flowValueChip("Fluid", fluidTxt.c_str());
    kit::chipFlowEnd();
    warningsList(solve_);
    static const char* const colours[] = {"None", "Pressure", "Velocity"};
    kit::sectionHeader("Color pipes by", "study_piping");
    kit::segmented("pipe_colour", colours, countOf(colours), &colorBy_, ImGui::GetContentRegionAvail().x);

    const json& links = jarr(solve_, "links");
    const bool massFlow = !links.empty() && !jhas(links[0], "flow_m3h");
    // Node names from the runs' ends: "Run 1 start", "Run 2 end" (first run touching the node).
    std::vector<std::pair<std::string, std::string>> nodeNames;
    auto nameNode = [&](const std::string& nodeId, const std::string& runId, const char* end) {
        if (nodeId.empty()) return;
        for (const auto& kv : nodeNames)
            if (kv.first == nodeId) return;
        nodeNames.emplace_back(nodeId, runName(runId) + end);
    };
    for (const auto& l : links) {
        nameNode(jstr(l, "from"), jstr(l, "id"), " start");
        nameNode(jstr(l, "to"), jstr(l, "id"), " end");
    }
    kit::sectionHeader("Runs", "route_pipe", nullptr, (int)links.size());
    // The run column is the narrow one so every numeric header keeps its unit.
    const std::string flowCol = colQ("Flow", massFlow ? Q::MassFlow : Q::Flow, us_);
    const std::string velCol = colQ("v", Q::Vel, us_), dpCol = colQ("dp", Q::Press, us_);
    if (kit::tableBegin("links", {{"Run", kit::TableAlign::Left, 0.8f},
                                  {flowCol.c_str(), kit::TableAlign::Right, 1.3f},
                                  {velCol.c_str(), kit::TableAlign::Right, 0.95f},
                                  {dpCol.c_str(), kit::TableAlign::Right, 0.95f}})) {
        for (const auto& l : links) {
            kit::tableRow();
            const std::string id = jstr(l, "id");
            const std::string tip = "Reynolds " + val(l, "reynolds", 3) + " (engine id " + id + ")";
            kit::tableCell(runName(id).c_str(), kit::Tone::Neutral, tip.c_str());
            kit::tableCell((jhas(l, "flow_m3h") ? valQ(l, "flow_m3h", Q::Flow, us_)
                                                : valQ(l, "mass_flow_kgh", Q::MassFlow, us_)).c_str());
            kit::tableCell(valQ(l, "velocity_ms", Q::Vel, us_, 3).c_str());
            kit::tableCell(valQ(l, "dp_kpa", Q::Press, us_, 3).c_str());
        }
    }
    kit::tableEnd();
    const json& nodes = jarr(solve_, "nodes");
    kit::sectionHeader("Nodes", "flow_bc", nullptr, (int)nodes.size());
    const std::string pCol = us_ ? std::string("P (psig)") : std::string("P (kPa g)");
    const std::string headCol = colQ("Head", Q::Head, us_);
    if (kit::tableBegin("nodes", {{"Node", kit::TableAlign::Left, 1.4f},
                                  {pCol.c_str(), kit::TableAlign::Right, 1.1f},
                                  {headCol.c_str(), kit::TableAlign::Right},
                                  {"Role", kit::TableAlign::Left, 0.8f}})) {
        for (const auto& n : nodes) {
            kit::tableRow();
            const std::string id = jstr(n, "id");
            std::string name = id;
            for (const auto& kv : nodeNames)
                if (kv.first == id) name = kv.second;
            if (name == id && id.size() >= 2 && id[0] == 'n') name = "Node " + id.substr(1);
            const std::string tip = "Engine node id " + id;
            kit::tableCell(name.c_str(), kit::Tone::Neutral, tip.c_str());
            kit::tableCell(valQ(n, "pressure_kpa", Q::Press, us_).c_str());
            kit::tableCell(valQ(n, "head_m", Q::Head, us_).c_str());
            const double demand = jnum(n, "demand_m3h", jnum(n, "mass_demand_kgh", 0.0));
            if (jbool(n, "fixed", false))
                kit::tableCell("Fixed", kit::Tone::Neutral, "Pressure or head fixed by a boundary condition.");
            else if (demand > 0.0)
                kit::tableCell("Demand", kit::Tone::Neutral, "Flow drawn out of the network here.");
            else if (demand < 0.0)
                kit::tableCell("Inflow", kit::Tone::Neutral, "Flow fed into the network here.");
            else
                kit::tableCell("");
        }
    }
    kit::tableEnd();
}

void PipingPanel::drawPumpDrive(AppState& st) {
    (void)st;
    sim::defaultSupplyForUnits(driveIn_, us_);   // 460 V / 60 Hz in US units until the user picks
    if (gasNetwork()) {
        drawNoResult("pump_drive", "Liquid networks only",
                     "Pump drives are sized for liquid networks: pick water on Conditions.");
        return;
    }
    // Sized drives first (the page's outcome), then what they are sized from. The footer's
    // "Size pump drive" is the page's one action.
    if (pumpDrive_.is_object()) {
        int i = 0;
        for (auto it = pumpDrive_.begin(); it != pumpDrive_.end(); ++it, ++i) {
            ImGui::PushID(i);
            const std::string title = "Pump drive \xc2\xb7 " + driveRunName(it.key());
            sim::drawPumpDriveResult("drive_result", *it, title.c_str());
            ImGui::PopID();
        }
        if (!pumpDrive_.empty()) kit::sectionHeader("Inputs", "size_pump");
    }
    static const char* const kSources[] = {"Solved duty point", "Manual"};
    static const char* const kSourceTips[] = {"Use the pump duty from the network solve.",
                                              "Enter the run, flow and head yourself."};
    if (kit::cardBegin("duty_point", "Duty point", "size_pump")) {
        kit::FieldGroup g("duty_fields", "Pump on run");
        kit::fieldEnum("source", "Source", &driveSource_, kSources, 2, kit::FieldOpts().withItemTips(kSourceTips));
        if (driveSource_ == 0) {
            const json op = solvedDutyPoint();
            if (op.is_object()) {
                kit::fieldReadout("run", "Pump on run", driveRunName(jstr(op, "run")).c_str());
                kit::fieldReadout("flow", "Flow", valQ(op, "flow_m3h", Q::Flow, us_).c_str(),
                                  kit::FieldOpts().withUnit(unitOf(Q::Flow, us_)));
                kit::fieldReadout("head", "Head", valQ(op, "head_m", Q::Head, us_).c_str(),
                                  kit::FieldOpts().withUnit(unitOf(Q::Head, us_)));
                const bool link = jstr(op, "basis") == "pump link";
                kit::fieldReadout("basis", "Basis", link ? "Modeled pump" : "Supply node",
                                  kit::FieldOpts().withTip(link ? "The modeled pump's head and flow on its run."
                                                                : "The fixed supply node delivers the total demand "
                                                                  "at its pressure head."));
            } else {
                kit::fieldReadout("op", "Duty point", "Not solved yet",
                                  kit::FieldOpts().withTip("Solve the network with a fixed supply pressure and a "
                                                           "demand, or switch Source to Manual."));
            }
        } else {
            const auto& labels = runLabels_;
            if (!labels.empty()) {
                int run = std::clamp(driveRun_, 0, (int)labels.size() - 1);
                if (kit::fieldCombo("run", "Pump on run", &run, labels)) driveRun_ = run;
            }
            kit::fieldNumber("flow", "Flow", &driveIn_.flowM3h,
                             fieldQ(Q::Flow, us_).withMin(0.0).withRequired());
            kit::fieldNumber("head", "Head", &driveIn_.headM,
                             fieldQ(Q::Head, us_).withMin(0.0).withRequired());
        }
    }
    kit::cardEnd();
    sim::drawPumpDriveInputs("drive", driveIn_, false);
}

void PipingPanel::drawSpecForm(AppState& st) {
    if (jarr(network_, "runs").empty()) {
        if (kit::emptyState("spec_check", "Nothing to check yet", "Route a pipe run first.", "Route pipe"))
            enterForm(st, Form::Route);
        return;
    }
    const kit::FieldOpts len = fieldQ(Q::Len, us_);
    if (page_[(int)Form::Spec] == 1) {
        if (!hasResult(Form::Spec)) {
            drawNoResult("spec_check", "Not checked yet",
                         "Run the spec check to see each run's wall, MAWP and support span and every "
                         "flange rating against ASME B31.3.");
            return;
        }
    } else {
        // Plain settings icon: the spec_check art carries a badge that reads as a notification here.
        if (kit::cardBegin("basis", "Design basis (ASME B31.3)", "settings")) {
            kit::FieldGroup g("basis_fields", "Corrosion allowance");
            bool edited = false;
            edited |= kit::fieldNumber("design_p", "Design pressure", &designPressureKpa_,
                                       fieldQ(Q::Press, us_).withMin(0.0).withRequired()
                                           .withTip("Gauge design pressure."));
            edited |= kit::fieldNumber("design_t", "Design temperature", &designTempC_, fieldQ(Q::Temp, us_));
            edited |= kit::fieldNumber("corrosion", "Corrosion allowance", &corrosionMm_,
                                       kit::FieldOpts(len).withMin(0.0).withHint(hintQ(1.5, Q::Len, us_).c_str()));
            edited |= kit::fieldNumber("mill_tol", "Mill tolerance", &millTol_,
                                       kit::FieldOpts().withScale(100.0).withUnit(kit::Unit::Percent)
                                           .withRange(0.0, 0.5).withHint("12.5")
                                           .withTip("Undertolerance on the nominal wall."));
            edited |= kit::fieldNumber("joint", "Joint factor E", &jointFactor_,
                                       kit::FieldOpts().withRange(0.0, 1.0).withHint("1.0")
                                           .withTip("Longitudinal weld joint quality factor (1.0 seamless)."));
            edited |= kit::fieldNumber("density", "Fluid density", &fluidDensity_,
                                       fieldQ(Q::Dens, us_).withMin(0.0)
                                           .withTip("Contents weight for the support-span check."));
            edited |= kit::fieldNumber("insulation", "Insulation", &insulationKgm_,
                                       fieldQ(Q::LineMass, us_).withMin(0.0).withHint("0")
                                           .withTip("Insulation weight per unit length for the support-span check."));
            if (edited) invalidateSetup(st, Form::Spec);
        }
        kit::cardEnd();
        return;
    }

    const bool all = jbool(spec_, "all_pass", false);
    kit::chipFlowBegin("spec_outcome");
    kit::flowChip(all ? "All pass" : "Failures", all ? kit::Tone::Ok : kit::Tone::Err, true);
    kit::flowValueChip("Code", jstr(spec_, "code", "ASME B31.3").c_str());
    kit::chipFlowEnd();

    // One key/value card per run (nothing elided: every check is its own row).
    const json& specRuns = jarr(spec_, "runs");
    kit::sectionHeader("Runs", "route_pipe", nullptr, (int)specRuns.size());
    for (int i = 0; i < (int)specRuns.size(); ++i) {
        const json& r = specRuns[i];
        const bool pass = jbool(r, "pass", false);
        const std::string title = runName(jstr(r, "id")) + " \xc2\xb7 " + npsOf(r);
        std::vector<kit::KeyValueItem> rows;
        rows.push_back({"Result", pass ? "Pass" : "Fail", pass ? kit::Tone::Ok : kit::Tone::Err});
        rows.push_back({"Wall (nominal)", qtyQ(r, "t_nominal_mm", Q::Len, us_, 3)});
        rows.push_back({"Wall (required)", qtyQ(r, "t_required_mm", Q::Len, us_, 3)});
        if (jhas(r, "utilization")) {
            const double u = jnum(r, "utilization");
            rows.push_back({"Wall utilization", kit::fmtQuantity(u * 100.0, U(kit::Unit::Percent), 3),
                            u > 1.0 ? kit::Tone::Err : kit::Tone::Neutral});
        }
        rows.push_back({"MAWP", qtyQ(r, "mawp_kpa", Q::Press, us_)});
        rows.push_back({"Support span", qtyQ(r, "support_span_m", Q::Span, us_, 3)});
        if (!pass && fmtText(r, "suggested_schedule") != kEmDash)
            rows.push_back({"Suggested schedule", fmtText(r, "suggested_schedule"), kit::Tone::Warn});
        ImGui::PushID(i);
        kit::keyValueCard("spec_run", title.c_str(), pass ? "ok_badge" : "error_badge", rows);
        ImGui::PopID();
    }

    const json& flanges = jarr(spec_, "flanges");
    if (!flanges.empty()) {
        kit::sectionHeader("Flanges", "spec_check", nullptr, (int)flanges.size());
        const std::string ratingCol = colQ("Rating", Q::Press, us_);
        if (kit::tableBegin("flanges", {{"Flange"},
                                        {"Class", kit::TableAlign::Right},
                                        {ratingCol.c_str(), kit::TableAlign::Right},
                                        {"Result"}})) {
            for (const auto& f : flanges) {
                const bool pass = jbool(f, "pass", false);
                kit::tableRow();
                const std::string id = jstr(f, "id");
                const bool numbered = id.size() >= 2 && id[0] == 'f' &&
                                      std::all_of(id.begin() + 1, id.end(),
                                                  [](char c) { return std::isdigit((unsigned char)c) != 0; });
                const std::string tip = "Engine fitting id " + id;
                kit::tableCell((numbered ? "Flange " + id.substr(1) : id).c_str(), kit::Tone::Neutral, tip.c_str());
                kit::tableCell(fmtText(f, "flange_class").c_str());
                kit::tableCell(valQ(f, "rating_kpa", Q::Press, us_).c_str());
                kit::tableCell(pass ? "Pass" : "Fail", pass ? kit::Tone::Ok : kit::Tone::Err);
            }
        }
        kit::tableEnd();
    }
    warningsList(spec_);
}

void PipingPanel::drawBomForm(AppState& st) {
    if (jarr(network_, "runs").empty()) {
        if (kit::emptyState("bom", "Nothing to list yet", "Route a pipe run first.", "Route pipe"))
            enterForm(st, Form::Route);
        return;
    }
    if (kit::cardBegin("spools_in", "Spool breaks", "line")) {
        kit::FieldGroup g("spool_fields", "Max spool length");
        if (kit::fieldNumber("max_spool", "Max spool length", &maxSpoolMm_,
                             fieldQ(Q::Len, us_).withMin(0.0).withHint(hintQ(12000.0, Q::Len, us_).c_str())
                                 .withTip("Shop spools break at flanges and at this length (transport limit).")))
            invalidateSetup(st, Form::Bom);
    }
    kit::cardEnd();

    if (!hasResult(Form::Bom)) return;
    // Lengths in the route's length unit, like the line list: the centerline matches the routed
    // length; the cut pipe is what is left after the fittings' take-outs (elbows, tees, flanges).
    const json& t = jobj(bom_, "totals");
    long long fittings = 0;
    for (const auto& it : jarr(bom_, "items"))
        if (jstr(it, "kind") != "pipe") fittings += (long long)jnum(it, "qty", 0.0);
    auto lenOf = [&](const char* k) {   // totals are in m
        return jhas(t, k) ? fmtQ(jnum(t, k) * 1000.0, Q::Len, us_) : std::string(kEmDash);
    };
    kit::keyValueCard("totals", "Bill of materials", "results",
                      {{"Centerline length", lenOf("centreline_length_m")},
                       {"Cut pipe length", lenOf("pipe_length_m")},
                       {"Fittings", std::to_string(fittings)},
                       {"Mass", qtyQ(t, "mass_kg", Q::Mass, us_)},
                       {"Shop welds", fmtText(t, "shop_welds")},
                       {"Field welds", fmtText(t, "field_welds")}});

    const json& lines = jarr(bom_, "line_list");
    kit::sectionHeader("Line list", "line", nullptr, (int)lines.size());
    const std::string lenCol = colQ("Length", Q::Len, us_), massCol = colQ("Mass", Q::Mass, us_);
    if (kit::tableBegin("line_list", {{"Run"}, {"Size"}, {"Material"}, {lenCol.c_str(), kit::TableAlign::Right}})) {
        for (const auto& l : lines) {
            kit::tableRow();
            const std::string run =
                runName(jstr(l, "run_id")) + (jstr(l, "name").empty() ? "" : " \xc2\xb7 " + jstr(l, "name"));
            kit::tableCell(run.c_str());
            kit::tableCell((npsOf(l) + " sch " + fmtText(l, "schedule")).c_str());
            kit::tableCell(fmtText(l, "material").c_str());
            kit::tableCell(valQ(l, "length_mm", Q::Len, us_).c_str());
        }
    }
    kit::tableEnd();

    const json& items = jarr(bom_, "items");
    kit::sectionHeader("Material take-off", "bom", nullptr, (int)items.size());
    if (kit::tableBegin("items", {{"Item"}, {"Size"}, {"Qty", kit::TableAlign::Right},
                                  {massCol.c_str(), kit::TableAlign::Right}})) {
        for (const auto& it : items) {
            std::string kind = humanToken(jstr(it, "kind"));
            if (jstr(it, "kind") == "flange" && it.contains("flange_class"))
                kind += " class " + fmtText(it, "flange_class");
            const std::string size =
                npsOf(it) + (jstr(it, "schedule").empty() ? "" : " sch " + jstr(it, "schedule"));
            const std::string count = jstr(it, "kind") == "pipe" && jhas(it, "length_mm")
                                          ? qtyQ(it, "length_mm", Q::Len, us_)
                                          : fmtText(it, "qty");
            const std::string unitMass = "Unit mass " + qtyQ(it, "unit_mass_kg", Q::Mass, us_);
            kit::tableRow();
            kit::tableCell(kind.c_str());
            kit::tableCell(size.c_str());
            kit::tableCell(count.c_str());
            kit::tableCell(valQ(it, "mass_kg", Q::Mass, us_).c_str(), kit::Tone::Neutral, unitMass.c_str());
        }
    }
    kit::tableEnd();

    const json& spools = jarr(bom_, "spools");
    kit::sectionHeader("Spools", "bom", nullptr, (int)spools.size());
    if (kit::tableBegin("spools", {{"Spool"}, {"Runs"}, {lenCol.c_str(), kit::TableAlign::Right},
                                   {"Shop welds", kit::TableAlign::Right}})) {
        for (const auto& s : spools) {
            std::string runsTxt;
            for (const auto& r : jarr(s, "run_ids"))
                if (r.is_string()) runsTxt += (runsTxt.empty() ? "" : ", ") + runName(r.get<std::string>());
            const std::string id = jstr(s, "id");
            const bool numbered = id.size() >= 3 && id.compare(0, 2, "SP") == 0;
            kit::tableRow();
            kit::tableCell((numbered ? "Spool " + id.substr(2) : id).c_str());
            kit::tableCell(runsTxt.c_str());
            kit::tableCell(valQ(s, "length_mm", Q::Len, us_).c_str());
            kit::tableCell(fmtText(s, "shop_welds").c_str());
        }
    }
    kit::tableEnd();
    warningsList(bom_);
}

void PipingPanel::drawFlowForm(AppState& st) {
    (void)st;
    if (page_[(int)Form::Flow] != 1) {
        drawFlowSetup(st);
        return;
    }
    if (!hasResult(Form::Flow)) {
        drawNoResult("study_flow", "No flow result yet",
                     "Run internal CFD to see the pressure drop through the fluid volume and how it "
                     "compares with the 1-D estimate.");
        return;
    }
    auto dpQ = [&](const json& o, const char* k) { return qtyQ(o, k, Q::PressPa, us_); };
    const std::string regime = jstr(flow_, "regime");
    const std::string regimeTxt = humanToken(regime);
    const bool conv = jbool(flow_, "converged", false);
    const std::string dp = dpQ(flow_, "dp_pa");
    // The outcome chips carry convergence, regime and the pressure drop; the card the rest.
    kit::chipFlowBegin("flow_outcome");
    kit::flowChip(conv ? "Converged" : "Not converged", conv ? kit::Tone::Ok : kit::Tone::Warn, true);
    if (!regime.empty() && regime != "unknown")
        kit::flowChip(regimeTxt.c_str(), regime == "laminar" ? kit::Tone::Ok : kit::Tone::Info, true);
    kit::flowValueChip("Pressure drop", dp.c_str());
    kit::chipFlowEnd();

    std::vector<kit::KeyValueItem> rows;
    const double darcyPa = jnum(flow_, "darcy_estimate_pa", 0.0);
    const bool hasOneD = jhas(flow_, "one_d_estimate_pa");
    const json& bd = jobj(flow_, "one_d_breakdown");
    if (hasOneD) {
        // Fittings-aware 1-D comparison of the run: Darcy friction + elbow/tee/reducer K.
        rows.push_back({"1-D estimate", dpQ(flow_, "one_d_estimate_pa")});
        if (jhas(flow_, "cfd_over_one_d")) {
            const double ratio = jnum(flow_, "cfd_over_one_d");
            // Within the measured bend over-prediction band the gap is the model's known bias.
            const double bandHi = jnum(jobj(flow_, "bend_loss_band"), "k_ratio_max", 1.0);
            const kit::Tone tone = std::fabs(ratio - 1.0) <= 0.25           ? kit::Tone::Ok
                                   : (ratio > 1.0 && ratio <= bandHi) ? kit::Tone::Info
                                                                      : kit::Tone::Warn;
            rows.push_back({"CFD / 1-D", kit::fmtValue(ratio, 3), tone});
        }
    } else {
        rows.push_back({"Darcy estimate", dpQ(flow_, "darcy_estimate_pa")});
        if (jhas(flow_, "dp_pa") && darcyPa > 0.0) {
            const double ratio = jnum(flow_, "dp_pa") / darcyPa;
            rows.push_back({"CFD / Darcy", kit::fmtValue(ratio, 3),
                            std::fabs(ratio - 1.0) <= 0.25 ? kit::Tone::Ok : kit::Tone::Warn});
        }
    }
    rows.push_back({"Mean velocity", qtyQ(flow_, "mean_velocity_ms", Q::Vel, us_)});
    rows.push_back({"Reynolds", val(flow_, "reynolds")});
    rows.push_back({"Hydraulic diameter", qtyQ(flow_, "hydraulic_diameter_mm", Q::Len, us_)});
    rows.push_back({"Inlet area", qtyQ(flow_, "inlet_area_mm2", Q::Area, us_)});
    rows.push_back({"Mass imbalance", val(flow_, "mass_imbalance", 3)});
    rows.push_back({"Solver steps", fmtText(flow_, "steps")});
    rows.push_back({"Voxels", fmtText(flow_, "voxels")});
    const json& prov = jobj(flow_, "provenance");
    if (jhas(prov, "voxel_mm")) rows.push_back({"Voxel size", qtyQ(prov, "voxel_mm", Q::Len, us_, 3)});
    if (!jstr(prov, "engine").empty()) rows.push_back({"Engine", humanToken(jstr(prov, "engine"))});
    kit::keyValueCard("flow_result", "Internal flow", "study_flow", rows);
    const json& legs = jobj(flow_, "extended_legs");
    if (!legs.empty())
        kit::dimText(("Between the run's end planes; end legs extended for the solve (inlet +" +
                      qtyQ(legs, "inlet_mm", Q::Len, us_) + ", outlet +" + qtyQ(legs, "outlet_mm", Q::Len, us_) +
                      "), whole domain " + dpQ(flow_, "dp_domain_pa") + ".")
                         .c_str());
    if (hasOneD) {
        kit::sectionHeader("1-D loss breakdown", "flow_arrow", nullptr, (int)jarr(bd, "fittings").size() + 1);
        const std::string dpCol = colQ("dp", Q::PressPa, us_);
        if (kit::tableBegin("one_d", {{"Loss"}, {"Type"}, {"K", kit::TableAlign::Right},
                                      {dpCol.c_str(), kit::TableAlign::Right}})) {
            kit::tableRow();
            kit::tableCell("Straight pipe");
            kit::tableCell("Friction");
            kit::tableCell(("f " + val(bd, "friction_factor", 3)).c_str());
            kit::tableCell(valQ(bd, "friction_pa", Q::PressPa, us_).c_str());
            for (const auto& f : jarr(bd, "fittings")) {
                kit::tableRow();
                const std::string id = jstr(f, "id");
                const bool numbered = id.size() >= 2 && id[0] == 'f' &&
                                      std::all_of(id.begin() + 1, id.end(),
                                                  [](char c) { return std::isdigit((unsigned char)c) != 0; });
                kit::tableCell(id.empty() ? "Corner" : numbered ? ("Fitting " + id.substr(1)).c_str() : id.c_str());
                kit::tableCell(humanToken(jstr(f, "type")).c_str());
                kit::tableCell(val(f, "K", 3).c_str());
                kit::tableCell(valQ(f, "dp_pa", Q::PressPa, us_).c_str());
            }
        }
        kit::tableEnd();
        if (darcyPa > 0.0)
            kit::dimText(("Darcy straight-pipe friction over the flow path: " + dpQ(flow_, "darcy_estimate_pa") +
                          " (no fittings).")
                             .c_str());
    } else {
        kit::dimText("Darcy is straight-pipe friction only; pick the run this volume bores "
                     "(ports on its ends) for the fittings-aware 1-D estimate.");
    }
    const json& prof = jarr(flow_, "pressure_profile");
    if (prof.size() >= 2) {
        std::vector<double> xs, ys;
        xs.reserve(prof.size());
        ys.reserve(prof.size());
        for (const auto& p : prof) {
            xs.push_back(toDisp(jnum(p, "s_mm", (double)xs.size()), Q::Len, us_));
            ys.push_back(toDisp(jnum(p, "p_pa", 0.0), Q::PressPa, us_));
        }
        kit::sectionHeader("Pressure profile", "plots_view");
        kit::PlotSpec spec;
        spec.x = xs;
        spec.xAxis = {"Distance along the path", unitOf(Q::Len, us_)};
        spec.yAxis = {"Pressure", unitOf(Q::PressPa, us_)};
        spec.series.push_back({"Pressure", ys});
        kit::linePlot("pressure_profile", spec);
    }
    for (const auto& c : jarr(flow_, "caveats"))
        if (c.is_string()) kit::inlineStatus(c.get<std::string>().c_str(), kit::Tone::Warn);
}

void PipingPanel::drawFlowSetup(AppState& st) {
    const json& runs = jarr(network_, "runs");
    const kit::FieldOpts len = fieldQ(Q::Len, us_);
    static const char* const kSources[] = {"This document", "STL file"};
    static const char* const kSourceTips[] = {"The open part's solid is the fluid volume.",
                                              "A fluid-volume STL from disk."};
    if (kit::cardBegin("domain", "Fluid domain", "study_flow")) {
        kit::FieldGroup g("domain_fields", "Fluid volume");
        setupEdited_ |= kit::fieldEnum("source", "Source", &flowSource_, kSources, 2,
                       kit::FieldOpts().withItemTips(kSourceTips)
                           .withTip("The solid is the fluid volume (for example a pipe bore), not the pipe "
                                    "wall; the inlet and outlet sit on its end faces."));
        if (flowSource_ == 1) {
            bool browse = false;
            setupEdited_ |= kit::fieldPath("stl", "Fluid volume", &flowStl_, &browse,
                           kit::FieldOpts().withHint("Choose an STL").withRequired());
            if (browse) {
                const std::string p = ui::openFileDialog("STL mesh (*.stl)\0*.stl\0All files (*.*)\0*.*\0");
                if (!p.empty()) { flowStl_ = p; setupEdited_ = true; }
            }
        }
    }
    kit::cardEnd();
    if (!runs.empty()) drawRunPorts(st);

    static const char* const kNormHints[] = {"X", "Y", "Z"};
    if (kit::cardBegin("ports", "Ports", "flow_bc")) {
        kit::FieldGroup g("port_fields", "Outlet normal");
        if (!runs.empty()) {
            // Entry 0 = no run: the ports are entered by hand (no 1-D comparison).
            const auto& labels = flowRunLabels_;
            const bool haveRun = flowRun_ >= 0 && flowRun_ < (int)runs.size();
            int sel = haveRun ? flowRun_ + 1 : 0;
            if (kit::fieldCombo("run", "Run", &sel, labels,
                                kit::FieldOpts().withTip("Inlet and outlet come from this run's two ends; "
                                                         "the result is compared with its 1-D loss."))) {
                flowRun_ = sel - 1;
                seedFlowPorts();
                setupEdited_ = true;
            }
            if (kit::secondaryButton("Ports from run ends###seed_ports", haveRun, "Pick a run first.")) {
                seedFlowPorts();
                setupEdited_ = true;
            }
            kit::itemTooltip("Reset the inlet and outlet to the selected run's two ends.");
        }
        kit::secondaryButton("Assign inlet from run end###inlet_port");
        std::string droppedRun; int droppedEnd = 0;
        if (acceptRunPort(st, droppedRun, droppedEnd)) assignFlowPort(st, true, droppedRun, droppedEnd);
        setupEdited_ |= kit::fieldVec3("inlet", "Inlet point", inlet_, kit::FieldOpts(len));
        setupEdited_ |= kit::fieldVec3("inlet_n", "Inlet normal", inletN_,
                       kit::FieldOpts().withCellHints(kNormHints).withGroupCommit()
                           .withTip("Points into the fluid volume."));
        kit::secondaryButton("Assign outlet from run end###outlet_port");
        if (acceptRunPort(st, droppedRun, droppedEnd)) assignFlowPort(st, false, droppedRun, droppedEnd);
        setupEdited_ |= kit::fieldVec3("outlet", "Outlet point", outlet_, kit::FieldOpts(len));
        setupEdited_ |= kit::fieldVec3("outlet_n", "Outlet normal", outletN_,
                       kit::FieldOpts().withCellHints(kNormHints).withGroupCommit()
                           .withTip("Points into the fluid volume."));
    }
    kit::cardEnd();

    static const char* const kFluids[] = {"Water", "Air", "Methane", "Natural gas", "Nitrogen", "Carbon dioxide", "Custom"};
    static const char* const kRates[] = {"Mean velocity", "Volume flow"};
    if (kit::cardBegin("flow", "Flow", "fluid")) {
        kit::FieldGroup g("flow_fields", "Max solver steps");
        std::vector<std::string> fluids(kFluids, kFluids + countOf(kFluids));
        int selection = flowFluid_;
        if (selection < 0) {
            selection = (int)fluids.size();
            fluids.push_back(jstr(flowLaunchParams_, "fluid", "Native fluid"));
        }
        if (kit::fieldCombo("fluid", "Fluid", &selection, fluids) &&
            selection >= 0 && selection <= countOf(kFluidKeys)) {
            if (!flowLaunchParams_.is_object()) flowLaunchParams_ = json::object();
            flowFluid_ = selection;
            ++fluidDisplayGeneration_[1];
            flowLaunchParams_.erase("density_kgm3");
            flowLaunchParams_.erase("viscosity_pas");
            if (flowFluid_ == countOf(kFluidKeys)) flowLaunchParams_.erase("fluid");
            else flowLaunchParams_["fluid"] = kFluidKeys[flowFluid_];
            setupEdited_ = true;
        }
        if (flowFluid_ == countOf(kFluidKeys)) {
            setupEdited_ |= kit::fieldNumber("density", "Density", &flowDensity_,
                             fieldQ(Q::Dens, us_).withMin(0.0).withRequired());
            setupEdited_ |= kit::fieldNumber("viscosity", "Viscosity", &flowViscosity_,
                             fieldQ(Q::Visc, us_).withMin(0.0).withRequired()
                                 .withTip("Dynamic viscosity."));
        }
        setupEdited_ |= kit::fieldEnum("rate_mode", "Specify", &flowRateMode_, kRates, 2);
        if (flowRateMode_ == 0)
            setupEdited_ |= kit::fieldNumber("velocity", "Mean velocity", &flowVelocity_,
                             fieldQ(Q::Vel, us_).withMin(0.0).withRequired());
        else
            setupEdited_ |= kit::fieldNumber("flow_rate", "Volume flow", &flowRateM3h_,
                             fieldQ(Q::Flow, us_).withMin(0.0).withRequired());
        bool voxelSet = flowVoxelMm_ > 0.0;
        if (kit::fieldNumber("voxel", "Voxel size", &flowVoxelMm_,
                             kit::FieldOpts(len).withMin(0.0).withPresent(&voxelSet).withHint("Auto")
                                 .withTip("Lattice spacing; leave empty to let the engine resolve the bore."))) {
            if (!voxelSet) flowVoxelMm_ = 0.0;
            setupEdited_ = true;
        }
        bool stepsSet = flowMaxSteps_ > 0;
        if (kit::fieldInt("max_steps", "Max solver steps", &flowMaxSteps_,
                          kit::FieldOpts().withMin(0).withPresent(&stepsSet).withHint("Auto")
                              .withTip("LBM step budget; leave empty for the engine default."))) {
            if (!stepsSet) flowMaxSteps_ = 0;
            setupEdited_ = true;
        }
    }
    kit::cardEnd();
}

void PipingPanel::drawVesselForm(AppState& st) {
    (void)st;
    const kit::FieldOpts len = fieldQ(Q::Len, us_);
    static const char* const kSizeBy[] = {"Shell length", "Volume"};
    static const char* const kOrient[] = {"Vertical", "Horizontal"};
    if (page_[(int)Form::Vessel] == 1) {
        if (!hasResult(Form::Vessel)) {
            drawNoResult("vessel", "Not sized yet",
                         "Size the vessel to see the shell and head thickness, MAWP, hydrotest pressure "
                         "and masses (ASME VIII-1).");
            return;
        }
        const json& shell = jobj(vessel_, "shell");
        const json& heads = jobj(vessel_, "heads");
        auto lenQ = [&](const json& o, const char* k, int sig) { return qtyQ(o, k, Q::Len, us_, sig); };
        auto kpaQ = [&](const json& o, const char* k) { return qtyQ(o, k, Q::Press, us_); };
        warningsList(vessel_);
        std::string headType = fmtText(heads, "type");
        for (int i = 0; i < countOf(kHeadKeys); ++i)
            if (headType == kHeadKeys[i]) headType = kHeadLabels[i];
        kit::keyValueCard("shell", "Shell (UG-27)", "vessel",
                          {{"Thickness (required)", lenQ(shell, "t_required_mm", 3)},
                           {"Thickness (nominal)", lenQ(shell, "t_nominal_mm", 3)},
                           {"MAWP", kpaQ(shell, "mawp_kpa")}});
        kit::keyValueCard("heads", "Heads (UG-32)", "vessel",
                          {{"Type", headType},
                           {"Thickness (required)", lenQ(heads, "t_required_mm", 3)},
                           {"Thickness (nominal)", lenQ(heads, "t_nominal_mm", 3)},
                           {"MAWP", kpaQ(heads, "mawp_kpa")},
                           {"Depth", lenQ(heads, "depth_mm", 4)}});
        kit::keyValueCard("vessel", "Vessel", "vessel",
                          {{"Hydrotest (UG-99b)", kpaQ(vessel_, "hydrotest_kpa")},
                           {"Volume", qtyQ(vessel_, "volume_m3", Q::Vol, us_)},
                           {"Shell length", lenQ(vessel_, "tan_tan_length_mm", 4)},
                           {"Empty mass", qtyQ(vessel_, "mass_empty_kg", Q::Mass, us_)},
                           {"Operating mass", qtyQ(vessel_, "mass_operating_kg", Q::Mass, us_)},
                           {"Full of water", qtyQ(vessel_, "mass_full_water_kg", Q::Mass, us_)}});
        // Start another record here; Delete is the header's action.
        if (kit::secondaryButton("New vessel###new_vessel", !vesselSlot_.id.empty(), "This vessel is not saved yet.")) {
            vesselSlot_ = Slot{};
            vessel_ = json();
            page_[(int)Form::Vessel] = 0;
            say(Form::Vessel, "New vessel: the next sizing is saved as a separate study.", 0);
        }
        kit::itemTooltip("Start another vessel; this one stays saved in the document.");
        return;
    }
    if (kit::cardBegin("geometry", "Geometry", "vessel")) {
        kit::FieldGroup g("geometry_fields", "Inside diameter");
        setupEdited_ |= kit::fieldNumber("id", "Inside diameter", &vesselIdMm_,
                         kit::FieldOpts(len).withMin(0.0).withRequired());
        setupEdited_ |= kit::fieldEnum("size_by", "Size by", &vesselSizeBy_, kSizeBy, 2);
        if (vesselSizeBy_ == 0)
            setupEdited_ |= kit::fieldNumber("tt", "Shell length", &vesselTtMm_,
                             kit::FieldOpts(len).withMin(0.0).withRequired()
                                 .withTip("Tangent-to-tangent length of the cylindrical shell."));
        else
            setupEdited_ |= kit::fieldNumber("volume", "Volume", &vesselVolM3_,
                             fieldQ(Q::Vol, us_).withMin(0.0).withRequired());
        setupEdited_ |= kit::fieldEnum("heads", "Heads", &vesselHead_, kHeadLabels, countOf(kHeadLabels));
        setupEdited_ |= kit::fieldEnum("orientation", "Orientation", &vesselOrient_, kOrient, 2);
    }
    kit::cardEnd();

    if (kit::cardBegin("design", "Design (ASME VIII-1)", "settings")) {
        kit::FieldGroup g("design_fields", "Corrosion allowance");
        setupEdited_ |= kit::fieldNumber("p", "Design pressure", &vesselPKpa_,
                         fieldQ(Q::Press, us_).withMin(0.0).withRequired()
                             .withTip("Gauge design pressure."));
        setupEdited_ |= kit::fieldNumber("t", "Design temperature", &vesselTC_, fieldQ(Q::Temp, us_));
        setupEdited_ |= kit::fieldEnum("material", "Material", &vesselMaterial_, kVesselMaterials, countOf(kVesselMaterials));
        setupEdited_ |= kit::fieldNumber("ca", "Corrosion allowance", &vesselCaMm_,
                         kit::FieldOpts(len).withMin(0.0).withHint(hintQ(3.0, Q::Len, us_).c_str()));
        setupEdited_ |= kit::fieldNumber("e", "Joint efficiency E", &vesselE_,
                         kit::FieldOpts().withRange(0.0, 1.0).withHint("0.85")
                             .withTip("UW-12 weld joint efficiency (0.85 spot RT, 1.0 full RT)."));
    }
    kit::cardEnd();

    if (kit::cardBegin("contents", "Contents", "fluid")) {
        kit::FieldGroup g("contents_fields", "Fluid density");
        setupEdited_ |= kit::fieldNumber("rho", "Fluid density", &vesselRho_,
                         fieldQ(Q::Dens, us_).withMin(0.0).withHint(hintQ(1000.0, Q::Dens, us_).c_str()));
        setupEdited_ |= kit::fieldSlider("fill", "Fill", &vesselFill_, 0.0, 1.0,
                         kit::FieldOpts().withScale(100.0).withUnit(kit::Unit::Percent)
                             .withTip("Operating liquid level as a fraction of the volume."));
    }
    kit::cardEnd();
}

void PipingPanel::drawDuctForm(AppState& st) {
    (void)st;
    const int page = page_[(int)Form::Duct];
    if (page == 2) {
        if (!hasResult(Form::Duct)) {
            drawNoResult("duct", "Not sized yet",
                         "Size the ducts to see every segment's size, velocity and pressure drop and the fan "
                         "static pressure on the critical path.");
            return;
        }
        warningsList(duct_);
        std::string path;
        for (const auto& id : jarr(duct_, "critical_path"))
            if (id.is_string()) path += (path.empty() ? "" : " \xc2\xb7 ") + id.get<std::string>();
        kit::keyValueCard("duct_result", "Duct system", "duct",
                          {{"Fan static pressure", qtyQ(duct_, "fan_static_pa", Q::DuctPa, us_)},
                           {"Critical path", path.empty() ? std::string(kEmDash) : path}});
        const json& segs = jarr(duct_, "segments");
        kit::sectionHeader("Segments", "duct", nullptr, (int)segs.size());
        // Sizes read "550 × 300" for rectangular ducts, so the size column gets the extra width.
        const std::string sizeCol = colQ("Size", Q::Len, us_), velCol = colQ("v", Q::DuctVel, us_);
        const std::string dpCol = colQ("dp", Q::DuctPa, us_);
        if (kit::tableBegin("duct_segments", {{"Segment", kit::TableAlign::Left, 1.1f},
                                              {sizeCol.c_str(), kit::TableAlign::Right, 1.5f},
                                              {velCol.c_str(), kit::TableAlign::Right, 0.8f},
                                              {dpCol.c_str(), kit::TableAlign::Right, 0.8f}})) {
            for (const auto& s : segs) {
                const std::string size = jhas(s, "width_mm")
                                             ? valQ(s, "width_mm", Q::Len, us_) + " \xc3\x97 " +
                                                   valQ(s, "height_mm", Q::Len, us_)
                                             : "\xe2\x8c\x80 " + valQ(s, "diameter_mm", Q::Len, us_);
                const std::string re = "Reynolds " + val(s, "reynolds", 3);
                kit::tableRow();
                kit::tableCell(jstr(s, "id").c_str(), kit::Tone::Neutral, re.c_str());
                kit::tableCell(size.c_str());
                kit::tableCell(valQ(s, "velocity_ms", Q::DuctVel, us_, 3).c_str());
                kit::tableCell(valQ(s, "dp_total_pa", Q::DuctPa, us_).c_str());
            }
        }
        kit::tableEnd();
        // Start another record here; Delete is the header's action.
        if (kit::secondaryButton("New system###new_duct", !ductSlot_.id.empty(), "This duct system is not saved yet.")) {
            ductSlot_ = Slot{};
            duct_ = json();
            page_[(int)Form::Duct] = 0;
            say(Form::Duct, "New duct system: the next sizing is saved as a separate study.", 0);
        }
        kit::itemTooltip("Start another duct system; this one stays saved in the document.");
        return;
    }
    if (page == 1) {
        if (kit::cardBegin("design", "Design (equal friction)", "settings")) {
            kit::FieldGroup g("design_fields", "Air temperature");
            setupEdited_ |= kit::fieldNumber("friction", "Friction rate", &ductFriction_,
                             fieldQ(Q::Friction, us_).withMin(0.0).withHint(hintQ(1.0, Q::Friction, us_).c_str()));
            setupEdited_ |= kit::fieldNumber("vmax", "Max velocity", &ductVmax_,
                             fieldQ(Q::DuctVel, us_).withMin(0.0).withHint(hintQ(10.0, Q::DuctVel, us_).c_str()));
            setupEdited_ |= kit::fieldNumber("air_t", "Air temperature", &ductTempC_,
                             fieldQ(Q::Temp, us_).withHint(hintQ(20.0, Q::Temp, us_).c_str()));
            const std::string roughTip = "Galvanized steel is about " + fmtQ(0.09, Q::Len, us_, 2) + ".";
            setupEdited_ |= kit::fieldNumber("rough", "Roughness", &ductRoughMm_,
                             fieldQ(Q::Len, us_).withMin(0.0).withHint(hintQ(0.09, Q::Len, us_).c_str())
                                 .withTip(roughTip.c_str()));
        }
        kit::cardEnd();
        return;
    }

    kit::sectionHeader("Segments", "duct", nullptr, (int)ductSegs_.size(), true);
    int removeIdx = -1;
    std::string moveId, beforeId;
    for (int i = 0; i < (int)ductSegs_.size(); ++i) {
        DuctSeg& d = ductSegs_[i];
        // Issue line for the name (the run blocker names the first one; this marks the card).
        const char* idIssue = nullptr;
        if (d.id.empty()) idIssue = "Name this segment.";
        for (int j = 0; j < (int)ductSegs_.size() && !idIssue; ++j)
            if (j != i && ductSegs_[j].id == d.id) idIssue = "Another segment has this name.";
        // Parent choices: the fan, then every OTHER segment (a segment cannot feed itself).
        std::vector<std::string> parents = {"Fan (root)"};
        std::vector<int> parentIdx = {-1};
        int parentSel = 0;
        for (int j = 0; j < (int)ductSegs_.size(); ++j) {
            if (j == i) continue;
            if (d.parent == j) parentSel = (int)parents.size();
            parents.push_back(ductSegs_[j].id.empty() ? "Segment " + std::to_string(j + 1) : ductSegs_[j].id);
            parentIdx.push_back(j);
        }
        // The card is titled by the segment's name (what results and "Fed from" call it): one
        // name, edited in its first field; a numbered title only while the name is empty.
        const std::string title = d.id.empty() ? "Segment " + std::to_string(i + 1) : d.id;
        ImGui::PushID(i);
        bool removed = false;
        kit::secondaryButton(("Move " + title + "###move_segment").c_str());
        if (ImGui::BeginDragDropSource()) {
            if (!ImGui::GetDragDropPayload()) {
                json p = setupDragOrigin(st, Form::Duct);
                p["segment"] = d.id;
                const std::string encoded = p.dump();
                ImGui::SetDragDropPayload("CHUDWARE_DUCT_SEGMENT", encoded.c_str(), encoded.size() + 1, ImGuiCond_Once);
            }
            ImGui::TextUnformatted(title.c_str());
            ImGui::EndDragDropSource();
        }
        if (ImGui::BeginDragDropTarget()) {
            if (const auto* payload = ImGui::AcceptDragDropPayload("CHUDWARE_DUCT_SEGMENT")) {
                if (payload->IsDelivery() && payload->DataSize > 1 &&
                    static_cast<const char*>(payload->Data)[payload->DataSize - 1] == '\0') {
                    const json p = json::parse(static_cast<const char*>(payload->Data),
                        static_cast<const char*>(payload->Data) + payload->DataSize - 1, nullptr, false);
                    if (acceptsSetupDrag(st, Form::Duct, p)) {
                        moveId = jstr(p, "segment"); beforeId = d.id;
                    }
                }
            }
            ImGui::EndDragDropTarget();
        }
        kit::itemTooltip("Drag onto another Move chip to put this segment before it; properties and parent links stay attached.");
        if (kit::removableCard("seg", title.c_str(), "duct", nullptr, "Remove this segment", &removed)) {
            kit::FieldGroup g("seg_fields", "Fitting loss (K)");
            setupEdited_ |= kit::fieldText("id", "Name", &d.id,
                           kit::FieldOpts().withRequired().withHint("Main").withIssue(idIssue)
                               .withTip("Used by the results and by other segments' Fed from."));
            if (kit::fieldCombo("parent", "Fed from", &parentSel, parents,
                                kit::FieldOpts().withTip("The upstream segment (or the fan)."))) {
                d.parent = parentIdx[parentSel];
                setupEdited_ = true;
            }
            setupEdited_ |= kit::fieldNumber("airflow", "Airflow", &d.airflow,
                             fieldQ(Q::Airflow, us_).withMin(0.0).withRequired());
            setupEdited_ |= kit::fieldNumber("length", "Length", &d.length,
                             fieldQ(Q::Span, us_).withMin(0.0).withRequired());
            setupEdited_ |= kit::fieldNumber("k", "Fitting loss (K)", &d.k,
                             kit::FieldOpts().withMin(0.0).withHint("0")
                                 .withTip("Sum of the loss coefficients of the segment's fittings (elbows, "
                                          "dampers, takeoffs); dimensionless."));
            setupEdited_ |= kit::fieldEnum("shape", "Shape", &d.shape, kDuctShapes, countOf(kDuctShapes));
            if (d.shape == 1)
                setupEdited_ |= kit::fieldNumber("aspect", "Aspect ratio", &d.aspect,
                                 kit::FieldOpts().withMin(1.0).withHint("2").withTip("Width / height."));
        }
        kit::cardEnd();
        ImGui::PopID();
        if (removed) removeIdx = i;
    }
    if (!moveId.empty()) reorderDuct(st, moveId, beforeId);
    if (removeIdx >= 0) {
        ductSegs_.erase(ductSegs_.begin() + removeIdx);
        setupEdited_ = true;
        for (auto& d : ductSegs_) {
            if (d.parent == removeIdx) d.parent = -1;
            else if (d.parent > removeIdx) --d.parent;
        }
    }
    if (kit::secondaryButton("+ Segment###add_seg")) {
        DuctSeg d;
        // First free "Segment N" (names must be unique).
        for (int n = (int)ductSegs_.size() + 1;; ++n) {
            d.id = "Segment " + std::to_string(n);
            if (std::none_of(ductSegs_.begin(), ductSegs_.end(), [&](const DuctSeg& o) { return o.id == d.id; }))
                break;
        }
        d.parent = ductSegs_.empty() ? -1 : 0;
        ductSegs_.push_back(d);
        setupEdited_ = true;
    }
}

nlohmann::json PipingPanel::setupDefinition(Form f) const {
    if (f == Form::Vessel) return vesselDefinition();
    if (f == Form::Duct) return ductDefinition();
    if (f == Form::Flow) {
        json p = flowCallParams();
        if (flowSource_ == 1) p["stl_path"] = flowStl_;
        const auto& runs = jarr(network_, "runs");
        if (flowRun_ >= 0 && flowRun_ < (int)runs.size()) {
            p["network_id"] = netSlot_.id;
            p["run_id"] = jstr(runs[flowRun_], "id");
        }
        return p;
    }
    return networkRecord()["params"];
}

void PipingPanel::invalidateSetup(AppState& st, Form f) {
    ++setupGeneration(f);
    if (f != Form::Vessel && f != Form::Duct && f != Form::Flow) ++flowGeneration_;
    if (f == Form::Route) { persistNetwork(st); return; }
    if (f == Form::Vessel) {
        vessel_ = json();
        vesselParams_ = vesselDefinition();
        persistVessel(st);
    } else if (f == Form::Duct) {
        duct_ = json();
        ductParams_ = ductDefinition();
        persistDuct(st);
    } else if (f == Form::Flow) {
        flow_ = json();
        flowLaunchParams_ = setupDefinition(Form::Flow);
        if (flowSource_ == 1) flowLaunchParams_["source_stl"] = flowStl_;
        persistFlow(st);
    } else {
        solve_ = spec_ = bom_ = json();
        pumpDrive_ = json::object();
        persistNetwork(st);
    }
}

bool PipingPanel::configureSetup(AppState& st, Form f, const json& p, std::string& error) {
    error.clear();
    if (busy()) { error = "Wait for the current request before changing setup."; return false; }
    if (!p.is_object()) { error = "Setup must be a JSON object."; return false; }
    const auto reject = [&](const std::string& text) { error = text; return false; };
    const auto finite = [&](const auto& self, const json& value) -> bool {
        if (value.is_number()) return std::isfinite(value.get<double>());
        if (value.is_structured())
            for (const auto& child : value) if (!self(self, child)) return false;
        return !value.is_discarded();
    };
    if (!finite(finite, p)) return reject("All numeric parameters must be finite.");
    const auto number = [](const json& o, const char* key, double low, bool inclusive,
                           bool required = false) {
        if (!o.contains(key) || o[key].is_null()) return !required;
        return o[key].is_number() && (inclusive ? jnum(o, key) >= low : jnum(o, key) > low);
    };
    const auto xyz = [](const json& value) { double v[3]; return jvec3(value, v); };
    if (f == Form::Vessel) {
        if (!number(p, "inner_diameter_mm", 0, false, true) ||
            !number(p, "design_pressure_kpa", 0, false, true) ||
            (jhas(p, "tan_tan_length_mm") == jhas(p, "volume_m3")) ||
            !number(p, "tan_tan_length_mm", 0, true) || !number(p, "volume_m3", 0, false))
            return reject("Vessel needs positive diameter/pressure/volume and exactly one nonnegative length or volume.");
        if (!number(p, "corrosion_allowance_mm", 0, true) ||
            !number(p, "fluid_density_kgm3", 0, true) ||
            !number(p, "joint_efficiency", 0, false) || jnum(p, "joint_efficiency", 0.85) > 1 ||
            !number(p, "fill_fraction", 0, true) || jnum(p, "fill_fraction", 0.8) > 1 ||
            (p.contains("design_temp_c") && !p["design_temp_c"].is_number()))
            return reject("Vessel allowances/density must be nonnegative; efficiency and fill must be in range.");
        if (p.contains("material") && indexOf(kVesselMaterials, countOf(kVesselMaterials), jstr(p, "material")) < 0)
            return reject("Choose a native vessel material (SA-516-70, SA-240-304/316 or SA-285-C).");
        if (p.contains("head_type") && indexOf(kHeadKeys, countOf(kHeadKeys), jstr(p, "head_type")) < 0)
            return reject("Choose ellipsoidal_2_1, hemispherical or torispherical heads.");
        if (p.contains("orientation") && jstr(p, "orientation") != "vertical" && jstr(p, "orientation") != "horizontal")
            return reject("Orientation must be vertical or horizontal.");
        vesselTC_ = 40.0; vesselMaterial_ = 0; vesselCaMm_ = 3.0; vesselE_ = 0.85;
        vesselHead_ = 0; vesselRho_ = 1000.0; vesselFill_ = 0.8; vesselOrient_ = 0;
        adoptVessel({{"id", vesselSlot_.id}, {"params", p}});
    } else if (f == Form::Duct) {
        if (p.contains("method") && jstr(p, "method") != "equal_friction")
            return reject("Only the native equal_friction method is supported.");
        if (!p.contains("segments") || !p["segments"].is_array() || p["segments"].empty())
            return reject("Add at least one duct segment.");
        if (!number(p, "friction_pa_per_m", 0, false) || !number(p, "max_velocity_ms", 0, false) ||
            !number(p, "roughness_mm", 0, true) ||
            (p.contains("air_temp_c") && !p["air_temp_c"].is_number()))
            return reject("Duct friction and velocity must be positive; roughness nonnegative.");
        std::vector<std::string> ids, parents;
        const auto idText = [](const json& value) {
            return value.is_string() ? value.get<std::string>() :
                   value.is_number_integer() ? value.dump() : std::string();
        };
        for (const auto& s : p["segments"]) {
            if (!s.is_object()) return reject("Every duct segment must be an object.");
            const std::string id = idText(s.value("id", json()));
            if (id.empty() || std::find(ids.begin(), ids.end(), id) != ids.end() ||
                !number(s, "airflow_m3s", 0, false, true) ||
                !number(s, "length_m", 0, true, true) || !number(s, "k_fittings", 0, true) ||
                (s.contains("shape") && jstr(s, "shape") != "round" && jstr(s, "shape") != "rect") ||
                (s.contains("aspect_ratio") && !s["aspect_ratio"].is_null() && !s["aspect_ratio"].is_number()) ||
                (jstr(s, "shape") == "rect" &&
                 (jnum(s, "aspect_ratio", 2) < 1 || jnum(s, "aspect_ratio", 2) > 8)))
                return reject("Duct segments need unique IDs, positive airflow, nonnegative length/K and valid shape/aspect.");
            ids.push_back(id);
            parents.push_back(idText(s.value("parent", json())));
            if (s.contains("parent") && !s["parent"].is_null() && parents.back().empty())
                return reject("Each parent must be a segment ID or null.");
        }
        for (std::size_t i = 0; i < ids.size(); ++i) {
            std::string parent = parents[i];
            std::vector<std::string> seen = {ids[i]};
            while (!parent.empty()) {
                const auto it = std::find(ids.begin(), ids.end(), parent);
                if (it == ids.end()) return reject("A duct parent does not exist.");
                if (std::find(seen.begin(), seen.end(), parent) != seen.end())
                    return reject("Duct parent links must not form a cycle.");
                seen.push_back(parent);
                parent = parents[std::distance(ids.begin(), it)];
            }
        }
        ductFriction_ = 1.0; ductVmax_ = 10.0; ductTempC_ = 20.0; ductRoughMm_ = 0.09;
        adoptDuct({{"id", ductSlot_.id}, {"params", p}});
    } else if (f == Form::Flow) {
        for (const char* port : {"inlet", "outlet"}) {
            const auto& v = jobj(p, port);
            double n[3];
            if (!xyz(v.value("point", json())) || !jvec3(v.value("normal", json()), n) || !normalize3(n))
                return reject("Each CFD port needs an XYZ point and a nonzero normal.");
        }
        const bool hasVelocity = p.contains("mean_velocity_ms") && !p["mean_velocity_ms"].is_null();
        const bool hasFlow = p.contains("flow_m3s") && !p["flow_m3s"].is_null();
        if (hasVelocity == hasFlow ||
            !number(p, "mean_velocity_ms", 0, false, hasVelocity) ||
            !number(p, "flow_m3s", 0, false, hasFlow))
            return reject("CFD needs exactly one non-null finite positive velocity or flow.");
        const bool custom = p.contains("density_kgm3") && p.contains("viscosity_pas");
        const bool hasDensity = p.contains("density_kgm3") && !p["density_kgm3"].is_null();
        const bool hasViscosity = p.contains("viscosity_pas") && !p["viscosity_pas"].is_null();
        if (hasDensity != hasViscosity || (custom &&
            (!number(p, "density_kgm3", 0, false, true) ||
             !number(p, "viscosity_pas", 0, false, true))))
            return reject("Custom CFD density and viscosity must both be finite positive numbers.");
        for (const char* key : {"voxel_mm", "max_steps"})
            if (!number(p, key, 0, true)) return reject(std::string(key) + " must be nonnegative.");
        for (const char* key : {"voxel_mm", "max_steps", "cells_across", "max_fluid_cells", "tolerance", "roughness_mm"})
            if (p.contains(key) && p[key].is_null())
                return reject(std::string(key) + " must be numeric when supplied; omit it for the native default.");
        if (!number(p, "cells_across", 4, true) || jnum(p, "cells_across", 20) > 128 ||
            !number(p, "max_fluid_cells", 1000, true) ||
            !number(p, "tolerance", 0, false) || jnum(p, "tolerance", 0.001) >= 0.5)
            return reject("CFD cells_across must be 4-128, max_fluid_cells at least 1000 and tolerance in (0, 0.5).");
        for (const char* key : {"cells_across", "max_steps"})
            if (p.contains(key) && (!p[key].is_number_integer() || jnum(p, key) > INT_MAX))
                return reject(std::string(key) + " must be an integer in native range.");
        if (!number(p, "roughness_mm", 0, true) ||
            (p.contains("temp_c") && !p["temp_c"].is_number()) ||
            (p.contains("pressure_kpa") && !p["pressure_kpa"].is_number()))
            return reject("CFD temperatures/pressures must be numeric and roughness nonnegative.");
        if (p.contains("fluid") && !p["fluid"].is_string())
            return reject("Use a native fluid name, or a label with explicit density_kgm3 and viscosity_pas.");
        if (p.contains("inlet_profile") && jstr(p, "inlet_profile") != "auto" &&
            jstr(p, "inlet_profile") != "developed" && jstr(p, "inlet_profile") != "uniform")
            return reject("Inlet profile must be auto, developed or uniform.");
        if (p.contains("run_id")) {
            const auto& runs = jarr(network_, "runs");
            if (std::none_of(runs.begin(), runs.end(), [&](const json& r) { return jstr(r, "id") == jstr(p, "run_id"); }))
                return reject("CFD run_id must identify a run in the current network.");
        }
        if (p.contains("network_id") && jstr(p, "network_id") != netSlot_.id)
            return reject("network_id is owned by the current network.");
        for (const char* key : {"stl_path", "body"})
            if (p.contains(key) && !p[key].is_string()) return reject(std::string(key) + " must be a string.");
        if (p.contains("doc_id") && jstr(p, "doc_id") != store_.activeDocId(st))
            return reject("doc_id belongs to the active document.");
        for (const char* key : {"job_id", "progress_path"})
            if (p.contains(key)) return reject(std::string(key) + " is owned by the running job.");
        json definition = p;
        definition["network_id"] = netSlot_.id;
        definition["source_stl"] = jstr(p, "stl_path");
        const std::string previousFlowKey = flowSlotKey_;
        flowRun_ = -1;
        adoptFlow(st, {{"id", flowSlot_.id}, {"params", definition}});
        flowSlotKey_ = previousFlowKey;
    } else {
        const auto& network = jobj(p, "piping");
        if (jstr(network, "schema") != "aicad.piping/1" || jstr(network, "id").empty() ||
            !network.contains("runs") || !network["runs"].is_array() ||
            !network.contains("fittings") || !network["fittings"].is_array())
            return reject("piping must be an aicad.piping/1 document with ID, runs and fittings.");
        const auto enumValue = [](const json& o, const char* key, const char* const* values, int count) {
            return !o.contains(key) || o[key].is_null() ||
                   (o[key].is_string() && indexOf(values, count, jstr(o, key)) >= 0);
        };
        const auto validId = [](const std::string& id, const char* prefix) {
            const std::size_t n = std::strlen(prefix);
            return id.size() > n && id.compare(0, n, prefix) == 0 &&
                   std::all_of(id.begin() + n, id.end(), [](unsigned char c) { return std::isdigit(c); });
        };
        const char* const endTypes[] = {"BW", "SW", "THD", "FLG"};
        const char* const fittingTypes[] = {"elbow", "tee", "reducer", "flange", "valve", "orifice", "custom"};
        if (!enumValue(network, "material", kPipeMaterials, countOf(kPipeMaterials)))
            return reject("The network material must be a native piping material or null.");
        for (const char* group : {"design", "spec_inputs", "route_inputs"})
            if (p.contains(group) && !p[group].is_null() && !p[group].is_object())
                return reject(std::string(group) + " must be an object.");
        const auto& routing = jobj(p, "route_inputs");
        for (const char* end : {"start", "end"}) {
            if (routing.contains(end) && !routing[end].is_object())
                return reject("Route start/end must be native point/direction objects.");
            const auto& endpoint = jobj(routing, end);
            for (const char* key : {"point", "dir"})
                if (endpoint.contains(key) && !xyz(endpoint[key]))
                    return reject("Route points/directions must be XYZ vectors.");
        }
        if (!number(routing, "nps", 0, false) || !number(routing, "clearance_mm", 0, true) ||
            !number(routing, "grid_mm", 0, true) ||
            !enumValue(routing, "schedule", kSchedules, countOf(kSchedules)) ||
            !enumValue(routing, "material", kPipeMaterials, countOf(kPipeMaterials)) ||
            (routing.contains("obstacles") && !routing["obstacles"].is_array()))
            return reject("Route size, schedule/material, clearance/grid and obstacles must be valid native inputs.");
        for (const char* key : {"pressure_kpa", "temp_c"})
            if (jobj(p, "design").contains(key) && !jobj(p, "design")[key].is_number())
                return reject("Design pressure/temperature must be numeric.");
        const auto& spec = jobj(p, "spec_inputs");
        for (const char* key : {"corrosion_allowance_mm", "fluid_density_kgm3", "insulation_kgm"})
            if (!number(spec, key, 0, true)) return reject(std::string(key) + " must be nonnegative.");
        if (!number(spec, "mill_tolerance_frac", 0, true) || jnum(spec, "mill_tolerance_frac", 0.125) >= 1 ||
            !number(spec, "joint_factor", 0, false) || jnum(spec, "joint_factor", 1) > 1 ||
            !number(p, "max_spool_length_mm", 0, false))
            return reject("Use mill tolerance in [0,1), joint factor in (0,1] and positive spool length.");
        std::vector<std::string> fittingIds;
        for (const auto& fitting : network["fittings"]) {
            const std::string id = jstr(fitting, "id");
            if (!fitting.is_object() || !validId(id, "ft") ||
                std::find(fittingIds.begin(), fittingIds.end(), id) != fittingIds.end() ||
                !enumValue(fitting, "type", fittingTypes, countOf(fittingTypes)) ||
                !fitting.contains("type") || !xyz(fitting.value("origin", json())) ||
                !xyz(fitting.value("orient", json())) || !number(fitting, "nps", 0, false, true) ||
                !number(fitting, "nps2", 0, false, jstr(fitting, "type") == "reducer") ||
                !enumValue(fitting, "schedule", kSchedules, countOf(kSchedules)) ||
                !enumValue(fitting, "material", kPipeMaterials, countOf(kPipeMaterials)) ||
                (fitting.contains("params") && !fitting["params"].is_object()))
                return reject("Fittings need unique ft<n> IDs, valid type, XYZ placement/orientation and positive sizes.");
            const auto& settings = jobj(fitting, "params");
            if (jstr(fitting, "type") == "elbow") {
                const char* const radii[] = {"LR", "SR"};
                if (!enumValue(settings, "radius", radii, countOf(radii)) ||
                    (settings.contains("angle") && !settings["angle"].is_null() &&
                     (!jhas(settings, "angle") || jnum(settings, "angle") <= 0 || jnum(settings, "angle") >= 180)))
                    return reject("Elbows need LR/SR radius and a positive angle below 180 degrees.");
            }
            if (jstr(fitting, "type") == "flange") {
                const char* const faces[] = {"RF", "FF", "RTJ"};
                const auto cl = settings.value("flange_class", json(nullptr));
                if (!enumValue(settings, "flange_face", faces, countOf(faces)) ||
                    (!cl.is_null() && cl != 150 && cl != 300 && cl != 600 &&
                     cl != 900 && cl != 1500 && cl != 2500))
                    return reject("Use a native flange class and RF/FF/RTJ face.");
            }
            fittingIds.push_back(id);
        }
        std::vector<std::string> ids;
        for (const auto& r : network["runs"]) {
            const std::string id = jstr(r, "id");
            const auto& path = jarr(r, "path");
            if (!validId(id, "r") || !enumValue(r, "schedule", kSchedules, countOf(kSchedules)) ||
                !enumValue(r, "material", kPipeMaterials, countOf(kPipeMaterials)) ||
                !enumValue(r, "end_type", endTypes, countOf(endTypes)))
                return reject("Runs need r<n> IDs and native schedule/material/end types.");
            if (id.empty() || std::find(ids.begin(), ids.end(), id) != ids.end() ||
                path.size() < 2 || !number(r, "nps", 0, false, true))
                return reject("Each pipe run needs a unique ID, positive NPS and at least two XYZ path points.");
            for (std::size_t i = 0; i < path.size(); ++i)
                if (!xyz(path[i]) || (i > 0 && path[i] == path[i - 1]))
                    return reject("Run paths must contain XYZ points without zero-length segments.");
            for (const char* key : {"bend_radius", "bore_mm"})
                if (!number(r, key, 0, false)) return reject(std::string(key) + " must be positive or null.");
            ids.push_back(id);
        }
        if (p.contains("boundary") && !p["boundary"].is_array()) return reject("boundary must be an array.");
        for (const auto& b : jarr(p, "boundary")) {
            if (!b.is_object()) return reject("Every boundary must be an object.");
            int count = 0;
            for (const char* key : {"pressure", "head", "demand", "demand_lps", "mass_demand"})
                if (b.contains(key)) { if (!b[key].is_number()) return reject("Boundary values must be numeric."); ++count; }
            if (count != 1) return reject("Each boundary must have exactly one condition value.");
            if (b.contains("run") && (std::find(ids.begin(), ids.end(), jstr(b, "run")) == ids.end() ||
                (jstr(b, "end") != "start" && jstr(b, "end") != "end")))
                return reject("Boundary run/end must exist in the current network.");
            if (!b.contains("run") && !b.contains("node") && !xyz(b.value("at", json())))
                return reject("Boundary requires a run/end, node or XYZ at.");
        }
        if (p.contains("pumps") && !p["pumps"].is_null() && !p["pumps"].is_array())
            return reject("pumps must be an array or null.");
        if (p.contains("network_type") && jstr(p, "network_type") != "liquid" &&
            jstr(p, "network_type") != "gas" && jstr(p, "network_type") != "compressible")
            return reject("network_type must be liquid, gas or compressible.");
        if (p.contains("fluid") && !p["fluid"].is_null() && !p["fluid"].is_object())
            return reject("Fluid must be a native property object or null.");
        const auto& fluid = jobj(p, "fluid");
        if (fluid.contains("rho") || fluid.contains("mu")) {
            if (!number(fluid, "rho", 0, false, true) || !number(fluid, "mu", 0, false, true))
                return reject("Explicit fluid rho and mu must both be finite positive numbers.");
        } else {
            const char* temperature = fluid.contains("T") && !fluid["T"].is_null() ? "T" : "T_C";
            if ((fluid.contains(temperature) && !fluid[temperature].is_null() && !fluid[temperature].is_number()) ||
                !number(fluid, "P", 0, false))
                return reject("Fluid temperature must be numeric and absolute pressure positive.");
            const double kelvin = std::strcmp(temperature, "T") == 0 ? jnum(fluid, "T") :
                                  jnum(fluid, "T_C", 20.0) + 273.15;
            if (!(kelvin > 0.0)) return reject("Fluid absolute temperature must be positive.");
        }
        const auto& oldRuns = jarr(network_, "runs");
        const std::string previousFlow = flowRun_ >= 0 && flowRun_ < (int)oldRuns.size()
            ? jstr(oldRuns[flowRun_], "id") : std::string();
        const std::string previousSelection = selectedRun_ >= 0 && selectedRun_ < (int)oldRuns.size()
            ? jstr(oldRuns[selectedRun_], "id") : std::string();
        designPressureKpa_ = 1000.0; designTempC_ = 40.0; fluidTempC_ = 20.0;
        corrosionMm_ = 1.5; millTol_ = 0.125; jointFactor_ = 1.0;
        fluidDensity_ = 1000.0; insulationKgm_ = 0.0; maxSpoolMm_ = 12000.0;
        adoptNetwork(st, {{"id", netSlot_.id}, {"params", p}});
        route_ = json();
        flow_ = json();
        flowRun_ = selectedRun_ = -1;
        for (int i = 0; i < (int)jarr(network_, "runs").size(); ++i) {
            const std::string id = jstr(network_["runs"][i], "id");
            if (id == previousFlow) flowRun_ = i;
            if (id == previousSelection) selectedRun_ = i;
        }
        if (flowLaunchParams_.is_object()) flowLaunchParams_.erase("piping");
        if (!flowSlot_.id.empty()) {
            flowLaunchParams_ = setupDefinition(Form::Flow);
            if (flowSource_ == 1) flowLaunchParams_["source_stl"] = flowStl_;
            persistFlow(st);
        }
    }
    setupEdited_ = false;
    invalidateSetup(st, f);
    return true;
}

void PipingPanel::drawNativeEditor(AppState& st) {
    const int index = (int)form_;
    const bool open = nativeEditorOpen_[index];
    if (kit::secondaryButton(open ? "Hide native parameters###native_toggle"
                                 : "Edit native parameters###native_toggle")) {
        nativeEditorOpen_[index] = !open;
        if (!open) { nativeDraft_[index] = setupDefinition(form_).dump(2); nativeError_[index].clear(); }
    }
    if (!nativeEditorOpen_[index]) return;
    if (kit::cardBegin("native_parameters", "Native parameters", "settings")) {
        kit::dimText("Exact native inputs. Known parameters feed the existing engine actions. Unknown keys are retained, not promised solver support.");
        kit::FieldGroup group("native_fields", "Definition");
        kit::FieldOpts opts;
        opts.lines = 8;
        kit::fieldText("definition", "Definition", &nativeDraft_[index], opts);
        if (kit::secondaryButton("Apply parameters###native_apply", !busy(), "A request is running.")) {
            const json p = json::parse(nativeDraft_[index], nullptr, false);
            if (!configureSetup(st, form_, p, nativeError_[index]) && nativeError_[index].empty())
                nativeError_[index] = "Enter valid JSON.";
            if (nativeError_[index].empty()) nativeDraft_[index] = setupDefinition(form_).dump(2);
        }
        if (kit::secondaryButton("Reload current parameters###native_reload")) {
            nativeDraft_[index] = setupDefinition(form_).dump(2);
            nativeError_[index].clear();
        }
        if (!nativeError_[index].empty()) kit::inlineStatus(nativeError_[index].c_str(), kit::Tone::Err);
    }
    kit::cardEnd();
}

void PipingPanel::drawRunPorts(AppState& st) {
    kit::sectionHeader("Drag a run end", "flow_bc");
    refreshRows();
    for (const auto& run : runRows_) {
        const auto& id = run.id;
        ImGui::PushID(id.c_str());
        for (int end = 0; end < 2; ++end) {
            ImGui::PushID(end);
            const auto& label = run.portLabel[end];
            kit::secondaryButton(run.portButton[end].c_str());
            if (ImGui::BeginDragDropSource()) {
                if (!ImGui::GetDragDropPayload()) {
                    json p = setupDragOrigin(st, form_);
                    p["run"] = id; p["end"] = end;
                    const std::string encoded = p.dump();
                    ImGui::SetDragDropPayload("CHUDWARE_PIPE_PORT", encoded.c_str(), encoded.size() + 1, ImGuiCond_Once);
                }
                ImGui::TextUnformatted(label.c_str());
                ImGui::EndDragDropSource();
            }
            kit::itemTooltip("Drag onto a boundary assignment or CFD Inlet/Outlet assignment.");
            ImGui::PopID();
        }
        ImGui::PopID();
    }
}

bool PipingPanel::acceptRunPort(AppState& st, std::string& runId, int& end) {
    bool accepted = false;
    if (ImGui::BeginDragDropTarget()) {
        if (const auto* payload = ImGui::AcceptDragDropPayload("CHUDWARE_PIPE_PORT")) {
            if (payload->IsDelivery() && payload->DataSize > 1 &&
                static_cast<const char*>(payload->Data)[payload->DataSize - 1] == '\0') {
                const json p = json::parse(static_cast<const char*>(payload->Data),
                    static_cast<const char*>(payload->Data) + payload->DataSize - 1, nullptr, false);
                if (acceptsSetupDrag(st, form_, p) &&
                    p.contains("end") && p["end"].is_number_integer()) {
                    runId = jstr(p, "run");
                    const auto value = p["end"].get<int64_t>();
                    end = value == 0 ? 0 : value == 1 ? 1 : -1;
                    const auto& runs = jarr(network_, "runs");
                    accepted = end >= 0 && std::any_of(runs.begin(), runs.end(),
                        [&](const json& r) { return jstr(r, "id") == runId; });
                }
            }
        }
        ImGui::EndDragDropTarget();
    }
    return accepted;
}

bool PipingPanel::assignBoundaryPort(AppState& st, int row, const std::string& runId, int end) {
    if (busy() || row < 0 || row >= (int)boundary_.size() || (end != 0 && end != 1)) return false;
    const auto& runs = jarr(network_, "runs");
    for (int i = 0; i < (int)runs.size(); ++i) {
        if (jstr(runs[i], "id") != runId) continue;
        auto& b = boundary_[row];
        b.run = i; b.end = end;
        if (b.native.is_object()) { b.native.erase("node"); b.native.erase("at"); }
        invalidateSetup(st, Form::Solve);
        return true;
    }
    return false;
}

bool PipingPanel::assignFlowPort(AppState& st, bool inlet, const std::string& runId, int end) {
    if (busy() || (end != 0 && end != 1)) return false;
    const auto& runs = jarr(network_, "runs");
    for (int i = 0; i < (int)runs.size(); ++i) {
        if (jstr(runs[i], "id") != runId) continue;
        const auto& path = jarr(runs[i], "path");
        if (path.size() < 2) return false;
        double point[3], adjacent[3], normal[3];
        if (!jvec3(end == 0 ? path.front() : path.back(), point) ||
            !jvec3(end == 0 ? path[1] : path[path.size() - 2], adjacent)) return false;
        for (int k = 0; k < 3; ++k) normal[k] = adjacent[k] - point[k];
        if (!normalize3(normal)) return false;
        for (int k = 0; k < 3; ++k) {
            (inlet ? inlet_ : outlet_)[k] = point[k];
            (inlet ? inletN_ : outletN_)[k] = normal[k];
        }
        flowRun_ = i;
        invalidateSetup(st, Form::Flow);
        return true;
    }
    return false;
}

bool PipingPanel::reorderDuct(AppState& st, const std::string& segmentId, const std::string& beforeId) {
    if (busy() || segmentId == beforeId) return false;
    json p = ductDefinition();
    auto& segments = p["segments"];
    const auto idText = [](const json& s) {
        return s["id"].is_string() ? s["id"].get<std::string>() : s["id"].dump();
    };
    auto from = std::find_if(segments.begin(), segments.end(), [&](const json& s) { return idText(s) == segmentId; });
    if (from == segments.end()) return false;
    json moved = std::move(*from);
    segments.erase(from);
    auto to = std::find_if(segments.begin(), segments.end(), [&](const json& s) { return idText(s) == beforeId; });
    if (!beforeId.empty() && to == segments.end()) return false;
    segments.insert(to, std::move(moved));
    std::string error;
    return configureSetup(st, Form::Duct, p, error);
}

bool PipingPanel::applySetupPreset(AppState& st, Form f, int preset) {
    if (preset < 0 || preset > 1 || (f != Form::Vessel && f != Form::Duct)) return false;
    json p = setupDefinition(f);
    if (f == Form::Vessel) {
        p.update({{"inner_diameter_mm", preset == 0 ? 1500.0 : 1000.0},
                  {"tan_tan_length_mm", preset == 0 ? 4000.0 : 2000.0},
                  {"design_pressure_kpa", preset == 0 ? 1000.0 : 800.0}, {"design_temp_c", 40.0},
                  {"material", "SA-516-70"}, {"corrosion_allowance_mm", 3.0},
                  {"joint_efficiency", 0.85}, {"head_type", "ellipsoidal_2_1"},
                  {"fluid_density_kgm3", preset == 0 ? 1000.0 : 1.2},
                  {"fill_fraction", preset == 0 ? 0.8 : 0.0},
                  {"orientation", preset == 0 ? "horizontal" : "vertical"}});
        p.erase("volume_m3");
    } else {
        const json existingSegments = jarr(p, "segments");
        p.update({{"friction_pa_per_m", preset == 0 ? 1.0 : 0.8},
                  {"max_velocity_ms", preset == 0 ? 10.0 : 6.0},
                  {"air_temp_c", 20.0}, {"roughness_mm", 0.09},
                  {"segments", json::array({
                      {{"id", "Main"}, {"airflow_m3s", 2.0}, {"length_m", 20.0},
                       {"k_fittings", 1.0}, {"shape", preset == 0 ? "round" : "rect"}, {"aspect_ratio", 2.0}},
                      {{"id", "Branch A"}, {"parent", "Main"}, {"airflow_m3s", 1.2}, {"length_m", 15.0},
                       {"k_fittings", 0.5}, {"shape", preset == 0 ? "round" : "rect"}, {"aspect_ratio", 2.0}},
                      {{"id", "Branch B"}, {"parent", "Main"}, {"airflow_m3s", 0.8}, {"length_m", 30.0},
                       {"k_fittings", 2.0}, {"shape", preset == 0 ? "round" : "rect"}, {"aspect_ratio", 2.0}}
                  })}});
        for (auto& segment : p["segments"]) {
            for (const auto& existing : existingSegments) {
                if (jstr(existing, "id") != jstr(segment, "id")) continue;
                json merged = existing;
                merged.update(segment);
                if (!segment.contains("parent")) merged.erase("parent");
                segment = std::move(merged);
                break;
            }
        }
    }
    std::string error;
    return configureSetup(st, f, p, error);
}

}  // namespace ui
