// sim_common — non-widget helpers shared by every Simulation-section panel (Piping, Hydraulics,
// the SimManager study editors, topology): tolerant JSON accessors, number/text formatting for
// engine payloads, the engine RPC envelope rules, the document-dirty mark, and StudyRecordStore —
// the persisted-study-record + async-RPC machinery the Piping and Hydraulics panels share.
//
// Widgets do NOT live here (they are ui::kit / kit_form / kit_plot / kit_panel). The two UI-ish
// helpers below (toneOf, warningsList) only map onto existing kit primitives.
#pragma once

#include <cstddef>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "../widgets/kit.h"

struct AppState;
namespace core { struct RpcResult; }

namespace ui {
namespace sim {

// ---- JSON accessors -----------------------------------------------------------------------------
// All of them tolerate a non-object `o` (and a missing / wrong-typed member): they never throw.
//
//   const json& p = jobj(rec, "params");          // {} when absent or not an object
//   for (const auto& w : jarr(res, "warnings")) …  // [] when absent or not an array
//   double bore = jnum(p, "bore_mm", 63.0);        // fallback unless the member is a number
//   if (jhas(res, "dp_pa")) …                      // member present AND numeric
//   std::string name = jstr(rec, "name", "Pipe network");
//   bool ok = jbool(res, "ok", false);
//   double pt[3]; if (jvec3(node["point"], pt)) …  // exactly 3 numbers

// True when `o` is an object whose member `k` is a number.
bool jhas(const nlohmann::json& o, const char* k);
// Member `k` as double; `fb` unless it is a number.
double jnum(const nlohmann::json& o, const char* k, double fb = 0.0);
// Member `k` as string; `fb` unless it is a string.
std::string jstr(const nlohmann::json& o, const char* k, const std::string& fb = std::string());
// Member `k` as bool; `fb` when absent OR not a boolean. (The SimManager's older simJsonBool reads a
// present non-boolean as false; this one keeps the fallback — the engine never sends a non-boolean
// `ok`/flag, so the two agree on every well-formed payload. New code uses jbool.)
bool jbool(const nlohmann::json& o, const char* k, bool fb);
// Member `k` when it is an object, else a shared empty object (reference stays valid forever).
const nlohmann::json& jobj(const nlohmann::json& o, const char* k);
// Member `k` when it is an array, else a shared empty array (reference stays valid forever).
const nlohmann::json& jarr(const nlohmann::json& o, const char* k);
// `v` is an array of exactly 3 numbers → out[0..2] = them, true. Otherwise false; `out` may have
// been partly written before the first non-number.
bool jvec3(const nlohmann::json& v, double out[3]);

// ---- formatting ---------------------------------------------------------------------------------
// Fixed-point text: fmtNum(1.234, 1) == "1.2".
std::string fmtNum(double v, int prec);
// Numeric member `k` with `prec` decimals, or an em dash when it is absent / not a number.
//   "dp " + fmtKey(res, "dp_pa", 0) + " Pa"
std::string fmtKey(const nlohmann::json& o, const char* k, int prec);
// Any member as display text: string as-is, integer exact, other numbers with 2 decimals,
// boolean "yes"/"no", absent or null an em dash, anything else its JSON dump.
std::string fmtText(const nlohmann::json& o, const char* k);

// ---- fixed key tables ---------------------------------------------------------------------------
//   static const char* const kHeads[] = {"ellipsoidal_2_1", "hemispherical"};
//   int i = indexOf(kHeads, countOf(kHeads), jstr(p, "head_type"));   // -1 when unknown
template <std::size_t N>
constexpr int countOf(const char* const (&)[N]) {
    return (int)N;
}
int indexOf(const char* const* items, int n, const std::string& key);

// Count + noun with the right number: countNoun(1, "link") == "1 link", countNoun(3, "link") ==
// "3 links", countNoun(2, "assembly", "assemblies") == "2 assemblies". Status lines use it instead of
// "1 links".
std::string countNoun(long long n, const char* one, const char* many = nullptr);

// ---- engine RPC envelopes -----------------------------------------------------------------------
// Engine tools answer {ok:true, …} on success and {ok:false, reason, hint} on refusal; protocol
// errors are {code, message} WITHOUT an ok key. Results may carry a design-code string in `code`
// ("ASME B31.3"), so only an ok-less object with a `code` is a protocol error.
bool protocolError(const nlohmann::json& res);
// Engine tool call succeeded: transport ok, object result, not a protocol error, ok == true.
bool envelopeOk(const core::RpcResult& r);
// sim_study_add / _edit / _delete succeeded: as envelopeOk, but a MISSING ok counts as success
// (those methods answer without an ok key).
bool studyRpcOk(const core::RpcResult& r);
// Human reason for a failed call: "reason — hint" (or message/error), else the transport error,
// else "the engine gave no reply".
std::string envelopeWhy(const nlohmann::json& res, const std::string& transportErr);
// A result as stored in a study record: the agent-advisory keys (next_actions, hint) dropped.
nlohmann::json stripAdvice(nlohmann::json r);

// ---- tones / small UI mappings ------------------------------------------------------------------
// Panel status tone int → kit tone: 0 Neutral, 1 Ok, 2 Warn, 3 Err (anything else Neutral).
kit::Tone toneOf(int tone);
// One Warn inline status per string in res["warnings"] (non-strings skipped).
void warningsList(const nlohmann::json& res);

// ---- document -----------------------------------------------------------------------------------
// A study write landed in document `docId`: mark its tab dirty (and adopted, for an agent hand-off
// tab) so the save prompt / title reflect it. No-op when no tab shows that doc.
void markDocDirty(AppState& st, const std::string& docId);

// ---- StudyRecordStore ---------------------------------------------------------------------------
// The persisted-record + async-RPC core of a panel whose work saves into the document as
// sim_study_* records (Piping: piping_network / vessel / duct_system / pipe_flow; Hydraulics:
// hydraulic_circuit). It owns, for the panel:
//   * the document's record mirror (from sim_study_list) and the panel's doc id,
//   * Slots — one per record the panel writes — with add→edit sequencing: a save issued while the
//     slot's sim_study_add is in flight is parked and re-sent as an edit once the add returns its id,
//   * re-sync of each slot against a fresh list (adopt records another client — an agent —
//     changed; forget records deleted elsewhere, keeping local state as unsaved work),
//   * the status line (text + tone + sticky lastError), and
//   * an engine call wrapper whose completion runs only while the store (i.e. the panel) is alive.
// The store captures `this` in RPC callbacks guarded by a weak_ptr, so it is neither copyable nor
// movable: keep it as a direct member of the (never moved) panel.
//
// Adoption sketch (Hydraulics):
//   StudyRecordStore store_{{"[hydraulics]", "Open a part to save this circuit inside a document.",
//                            "Could not save the circuit into the document; retry.",
//                            "Could not delete the circuit from the document.", 60000}};
//   StudyRecordStore::Slot slot_;
//   void setStudies(AppState&, const std::string& docId, const std::vector<json>& recs) {
//       store_.setRecords(docId, recs);
//       const bool localEmpty = !circuit_.is_object() || jarr(circuit_, "components").empty();
//       store_.sync(slot_, "hydraulic_circuit", localEmpty, [this](const json& r) { adopt(r); });
//   }
//   void adopt(const json& rec) { slot_.bind(rec); /* load params/results from rec */ }
//   void persist(AppState& st) { if (circuit_.is_object()) store_.saveRecord(st, slot_, record()); }
//   store_.call(st, "hydraulic_simulate", params, this, &HydraulicPanel::onSimulate);
//   store_.setStatus("Simulated 5 s.", 1);  …  store_.drawStatus();
class StudyRecordStore {
public:
    // One persisted record the panel writes.
    struct Slot {
        std::string id;            // engine study id ("" = not saved yet)
        bool addPending = false;   // sim_study_add in flight
        bool resave = false;       // a newer record arrived while the add was in flight
        nlohmann::json pending;    // that newer record
        std::string synced;        // dump of the record last written/adopted (change detection)

        // Reset and point this slot at `rec` (an adopted record): id = rec.id, synced = rec.dump().
        // Call first thing in the panel's adopt*(rec).
        void bind(const nlohmann::json& rec);
    };

    // Panel-specific wording; strings are copied.
    struct Config {
        std::string logTag;          // console prefix, e.g. "[piping]"
        std::string noDocStatus;     // Warn status when a save has no document to land in
        std::string saveFailedStatus;   // Err status when sim_study_add/_edit fails
        std::string deleteFailedStatus; // Err status when sim_study_delete fails
        int callTimeoutMs = 120000;  // default deadline of call()
    };

    // What sync() did to the slot.
    enum class Sync { Unchanged, Forgotten, Adopted };

    // Completion of call(): runs on the GL thread inside st.rpc.drain(), only while the store lives.
    // ok = envelopeOk; res = the result object ({} when not an object); why = envelopeWhy ("" if ok).
    using Done = std::function<void(AppState& st, bool ok, const nlohmann::json& res,
                                    const std::string& why)>;

    explicit StudyRecordStore(Config cfg);
    StudyRecordStore(const StudyRecordStore&) = delete;
    StudyRecordStore& operator=(const StudyRecordStore&) = delete;

    // ---- record mirror ----
    // Replace the mirror with this document's records of the panel's types (sim_study_list) and
    // remember the doc id. Follow with one sync() per slot.
    void setRecords(const std::string& docId, const std::vector<nlohmann::json>& records);
    const std::vector<nlohmann::json>& records() const { return records_; }
    int count() const { return (int)records_.size(); }
    // Record with this id (nullptr for "" / unknown). Valid until the mirror next changes.
    const nlohmann::json* find(const std::string& id) const;
    // First record of `type` in mirror order (nullptr when none).
    const nlohmann::json* firstOfType(const char* type) const;
    // The doc the records belong to: the setRecords doc, else the live mirror doc, else "".
    std::string activeDocId(const AppState& st) const;

    // Re-sync `slot` after setRecords. Skipped while its add is in flight. A saved slot whose record
    // vanished is forgotten (id + synced cleared; local state stays as unsaved work) → Forgotten; one
    // whose record differs from what the slot last wrote/adopted is re-adopted → Adopted. An unsaved
    // slot adopts the first record of `type` when `localEmpty` (nothing local would be clobbered)
    // and `type` is non-null → Adopted. `adopt` must call slot.bind(rec) (it reloads the panel).
    Sync sync(Slot& slot, const char* type, bool localEmpty,
              const std::function<void(const nlohmann::json&)>& adopt);

    // Write `record` (no "id" needed) into the document through `slot`: sim_study_edit when the slot
    // has an id (the local mirror updates immediately), else sim_study_add (the mirror updates when
    // the id returns). A save while the add is in flight is parked and re-sent as an edit after it.
    // No document → noDocStatus (Warn). Failure → console log + saveFailedStatus (Err). Success marks
    // the document dirty. `slot` must outlive the call (a panel member).
    void saveRecord(AppState& st, Slot& slot, nlohmann::json record);
    // Delete the slot's record (sim_study_delete): no-op while unsaved, without a document, or while
    // its add is in flight. The slot resets and the record leaves the mirror immediately.
    void deleteRecord(AppState& st, Slot& slot);

    // ---- engine calls ----
    // Issue an engine RPC. Counts toward inflight() until it completes and clears lastError().
    // timeoutMs < 0 → Config::callTimeoutMs.
    void call(AppState& st, const char* method, nlohmann::json params, Done done, int timeoutMs = -1);
    // Member-function form: store_.call(st, "piping_route", p, this, &PipingPanel::onRoute);
    template <class Panel>
    void call(AppState& st, const char* method, nlohmann::json params, Panel* self,
              void (Panel::*done)(AppState&, bool, const nlohmann::json&, const std::string&),
              int timeoutMs = -1) {
        call(st, method, std::move(params),
             [self, done](AppState& s, bool ok, const nlohmann::json& res, const std::string& why) {
                 (self->*done)(s, ok, res, why);
             },
             timeoutMs);
    }
    int inflight() const { return inflight_; }

    // ---- status line ----
    // tone: 0 neutral, 1 ok, 2 warn, 3 error (error also becomes lastError()).
    void setStatus(const std::string& text, int tone);
    // Drop the status line (text + tone). Statuses are per form: a panel calls this when it switches
    // forms so one form's result never shows on another. lastError() is kept (it belongs to the
    // action, cleared when the next call() starts).
    void clearStatus();
    // The status as one elided footer line (kit::footerStatus; nothing when empty).
    void drawStatus() const;
    const std::string& status() const { return status_; }
    int statusTone() const { return statusTone_; }
    // Last error text of the most recent action ("" = none; cleared when call() starts).
    const std::string& lastError() const { return lastError_; }

private:
    void upsertLocal(const nlohmann::json& saved);

    Config cfg_;
    std::shared_ptr<int> alive_;   // callbacks hold a weak_ptr: a destroyed store drops them
    std::string docId_;
    std::vector<nlohmann::json> records_;
    int inflight_ = 0;
    std::string status_;
    int statusTone_ = 0;
    std::string lastError_;
};

// ---- display units ------------------------------------------------------------------------------
// THE one SI -> display converter of the simulation panels (Piping, Hydraulics, pump drive).
// Engine values and every stored member stay SI; DISPLAY and typed INPUT follow the Simulation
// pane's unit system (SimManager::unitSys(st).us). In SI the stored unit shows unchanged; in US
// customary each SI unit converts, by exact factors, to its usual US unit (mm -> in, m -> ft,
// bar/kPa/Pa -> psi, L/min and m³/h -> gpm, m³/s -> cfm, °C -> °F, kg -> lb, N -> lbf, ...), or
// to an explicit `usUnit` of the same quantity (e.g. Pa -> "in. w.g." for ducts, m/s -> "fpm").
// Units it does not know (rpm, cc/rev, kW, kJ, s, %, °, cSt) pass through unchanged.
struct Disp {
    const char* unit;   // display spelling
    double scale;       // display = stored * scale + offset
    double offset;
};
namespace unit_detail {
struct UnitDef {
    std::string name;   // kit::unitText canonical spelling, or the US spelling
    int dim;            // quantity: units convert only within one dim
    double toBase;      // base = value * toBase + offset (base: SI coherent unit, K)
    double offset;
    const char* usDefault;  // US unit for an SI unit (nullptr: a US unit, or no US counterpart)
};
inline const std::vector<UnitDef>& unitDefs() {
    using kit::Unit;
    static const std::vector<UnitDef> defs = [] {
        constexpr double kIn = 0.0254, kFt = 0.3048, kLb = 0.45359237, kLbf = 4.4482216152605;
        constexpr double kGal = 3.785411784e-3, kPsi = 6894.757293168, kInWg = 249.08891;
        auto t = [](Unit u) { return std::string(kit::unitText(u)); };
        enum { Len, Press, Flow, Vel, Temp, Mass, Force, Torque, Dens, Area, Vol, MassFlow, Visc,
               Inertia, LinMass, Damping, PressGrad };
        return std::vector<UnitDef>{
            {t(Unit::mm), Len, 1e-3, 0, "in"},      {t(Unit::m), Len, 1, 0, "ft"},
            {t(Unit::in), Len, kIn, 0, nullptr},    {t(Unit::ft), Len, kFt, 0, nullptr},
            {t(Unit::Pa), Press, 1, 0, "psi"},      {t(Unit::kPa), Press, 1e3, 0, "psi"},
            {t(Unit::bar), Press, 1e5, 0, "psi"},   {t(Unit::psi), Press, kPsi, 0, nullptr},
            {"in. w.g.", Press, kInWg, 0, nullptr},
            {t(Unit::L_min), Flow, 1e-3 / 60.0, 0, "gpm"}, {t(Unit::m3_h), Flow, 1.0 / 3600.0, 0, "gpm"},
            {t(Unit::m3_s), Flow, 1, 0, "cfm"},     {t(Unit::gpm), Flow, kGal / 60.0, 0, nullptr},
            {"cfm", Flow, kFt * kFt * kFt / 60.0, 0, nullptr},
            {t(Unit::m_s), Vel, 1, 0, "ft/s"},      {t(Unit::mm_s), Vel, 1e-3, 0, "in/s"},
            {"ft/s", Vel, kFt, 0, nullptr},         {"in/s", Vel, kIn, 0, nullptr},
            {"fpm", Vel, kFt / 60.0, 0, nullptr},
            {t(Unit::DegC), Temp, 1, 273.15, "\xc2\xb0" "F"},
            {t(Unit::DegF), Temp, 5.0 / 9.0, 459.67 * 5.0 / 9.0, nullptr},
            {t(Unit::kg), Mass, 1, 0, "lb"},        {t(Unit::lb), Mass, kLb, 0, nullptr},
            {t(Unit::N), Force, 1, 0, "lbf"},       {t(Unit::kN), Force, 1e3, 0, "lbf"},
            {t(Unit::lbf), Force, kLbf, 0, nullptr},
            {t(Unit::N_m), Torque, 1, 0, "lbf\xc2\xb7" "ft"}, {"lbf\xc2\xb7" "ft", Torque, kLbf * kFt, 0, nullptr},
            {t(Unit::kg_m3), Dens, 1, 0, "lb/ft\xc2\xb3"}, {"lb/ft\xc2\xb3", Dens, kLb / (kFt * kFt * kFt), 0, nullptr},
            {t(Unit::mm2), Area, 1e-6, 0, "in\xc2\xb2"}, {t(Unit::cm2), Area, 1e-4, 0, "in\xc2\xb2"},
            {t(Unit::in2), Area, kIn * kIn, 0, nullptr},
            {t(Unit::L), Vol, 1e-3, 0, "gal"},      {t(Unit::m3), Vol, 1, 0, "gal"},
            {"gal", Vol, kGal, 0, nullptr},
            {"kg/h", MassFlow, 1.0 / 3600.0, 0, "lb/h"}, {"lb/h", MassFlow, kLb / 3600.0, 0, nullptr},
            {t(Unit::Pa_s), Visc, 1, 0, "cP"},      {"cP", Visc, 1e-3, 0, nullptr},
            {t(Unit::kg_m2), Inertia, 1, 0, "lb\xc2\xb7" "ft\xc2\xb2"},
            {"lb\xc2\xb7" "ft\xc2\xb2", Inertia, kLb * kFt * kFt, 0, nullptr},
            {"kg/m", LinMass, 1, 0, "lb/ft"},       {"lb/ft", LinMass, kLb / kFt, 0, nullptr},
            {"N\xc2\xb7s/m", Damping, 1, 0, "lbf\xc2\xb7s/in"},
            {"lbf\xc2\xb7s/in", Damping, kLbf / kIn, 0, nullptr},
            {"Pa/m", PressGrad, 1, 0, "in. w.g./100 ft"},
            {"in. w.g./100 ft", PressGrad, kInWg / (100.0 * kFt), 0, nullptr},
        };
    }();
    return defs;
}
// The unit named `name`: exact spelling first ("cP" is its own US unit even though kit::unitText
// folds it into mPa·s), then its kit::unitText canonical spelling.
inline const UnitDef* unitDef(const char* name) {
    const char* raw = name ? name : "";
    const char* canon = kit::unitText(raw);
    for (const auto& d : unitDefs())
        if (d.name == raw) return &d;
    for (const auto& d : unitDefs())
        if (d.name == canon) return &d;
    return nullptr;
}
}  // namespace unit_detail

// Display unit and conversion of a stored value in `siUnit` (any kit::unitText spelling).
inline Disp disp(const char* siUnit, bool us, const char* usUnit = nullptr) {
    const char* canon = kit::unitText(siUnit ? siUnit : "");
    const unit_detail::UnitDef* from = unit_detail::unitDef(canon);
    if (!us || !from || !from->usDefault) return {canon, 1.0, 0.0};
    const unit_detail::UnitDef* to = unit_detail::unitDef(usUnit ? usUnit : from->usDefault);
    if (!to || to->dim != from->dim) return {canon, 1.0, 0.0};
    // display = (stored * from.toBase + from.offset - to.offset) / to.toBase
    return {to->name.c_str(), from->toBase / to->toBase, (from->offset - to->offset) / to->toBase};
}
inline double toDisplay(double stored, const Disp& d) { return stored * d.scale + d.offset; }
// A stored value in `siUnit` as display text ("2,436 psi" / "168 bar").
inline std::string fmtU(double v, const char* siUnit, bool us, int sigFigs = 4, const char* usUnit = nullptr) {
    const Disp d = disp(siUnit, us, usUnit);
    return kit::fmtQuantity(toDisplay(v, d), d.unit[0] ? d.unit : nullptr, sigFigs);
}
// Numeric member `k` (times `scale`, giving `siUnit`) as display text, or an em dash when absent.
inline std::string qtyU(const nlohmann::json& o, const char* k, const char* siUnit, bool us,
                        double scale = 1.0, int sigFigs = 4) {
    return jhas(o, k) ? fmtU(jnum(o, k) * scale, siUnit, us, sigFigs) : std::string("\xe2\x80\x94");
}
// Field options that show and take a stored `siUnit` value in the display unit.
inline kit::FieldOpts fieldU(const char* siUnit, bool us, const char* usUnit = nullptr) {
    const Disp d = disp(siUnit, us, usUnit);
    return kit::FieldOpts().withUnit(d.unit).withScale(d.scale, d.offset);
}
// Power stays in kW (the motor catalogs' unit); US customary adds hp: "12.3 kW (16.5 hp)".
inline std::string fmtPower(double kw, bool us, int sigFigs = 4) {
    std::string s = kit::fmtQuantity(kw, kit::unitText(kit::Unit::kW), sigFigs);
    if (us) s += " (" + kit::fmtQuantity(kw / 0.745699871582270, kit::unitText(kit::Unit::hp), sigFigs) + ")";
    return s;
}
inline std::string powerQty(const nlohmann::json& o, const char* k, bool us, int sigFigs = 4) {
    return jhas(o, k) ? fmtPower(jnum(o, k), us, sigFigs) : std::string("\xe2\x80\x94");
}

}  // namespace sim
}  // namespace ui
