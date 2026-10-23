// core/cam/feeds.h — feeds & speeds: a material cutting-data database and the
// calculator that turns (material, tool, operation) into rpm / feed / plunge /
// stepover / stepdown. Fully customizable: callers may pass override tables and
// per-tool overrides; the builtin DB is the prior.
//
// Conventions: SFM = surface feet per minute (imperial cutting-speed convention,
// widely tabulated); Vc (m/min) is derived. fz = feed per tooth (mm/tooth, "chip
// load"). All outputs are metric (rpm, mm/min, mm).
#pragma once

#include <string>
#include <vector>

#include "cam/model.h"

namespace chudware {
namespace cam {

// One material group's machining data. `sfm_*` are baseline surface speeds for a
// carbide tool; HSS is derived by `hss_factor`. `fz_ref` is a reference chip load
// at `fz_ref_dia` mm; the calculator scales chip load with tool diameter.
struct MaterialData {
    std::string id;          // "aluminum", "steel_mild", ...
    std::string name;
    std::string group;       // "nonferrous"|"steel"|"stainless"|"cast_iron"|"titanium"|
                             // "plastic"|"wood"|"composite"|"hardened"
    double density_g_cm3 = 0;
    double sfm_mill = 0;     // milling surface speed (carbide), SFM
    double sfm_drill = 0;    // drilling surface speed (carbide/HSS), SFM
    double sfm_turn = 0;     // turning surface speed (carbide), SFM
    double hss_factor = 0.4; // HSS speed = carbide * hss_factor
    double fz_ref = 0.05;    // reference chip load (mm/tooth) at fz_ref_dia
    double fz_ref_dia = 6.0; // diameter the reference chip load is quoted at (mm)
    double stepover_frac = 0.4;  // default radial stepover as fraction of tool dia
    double stepdown_frac = 1.0;  // default axial DOC as fraction of tool dia
    double plunge_frac = 0.3;    // plunge feed as fraction of cutting feed
    // 2D-cutting (laser/plasma/waterjet) reference cut feed per mm thickness:
    double cut_feed_ref = 0; // mm/min at 1mm thickness (0 = N/A)

    json to_json() const;
    static MaterialData from_json(const json&);
};

// The cutting-data database: builtin materials + user additions/overrides.
class CuttingDB {
public:
    CuttingDB();                          // loads the builtin table
    void merge(const json& overrides);    // {"materials": [MaterialData...]} merge/replace by id
    // Fold the engine materials registry's per-material `cam` sub-sections onto this DB.
    // `registry` is the parsed registry.json ({"materials": {key: {..,
    // "cam": {"id", sfm_*, ...}}}}); for each material carrying a `cam` object, the 10 feeds
    // fields are overwritten on the in-code row named by `cam.id` — identity (id/name/group/
    // density) is preserved, and a `cam.id` with no in-code row is skipped. Additive to
    // merge(): this only touches cam feeds fields, never adds/replaces whole rows.
    void merge_cam(const json& registry);
    const MaterialData* find(const std::string& id) const;
    std::vector<std::string> material_ids() const;
    json to_json() const;                 // {"materials": [...]} (full effective table)

    static const CuttingDB& builtin();    // shared default instance
private:
    std::vector<MaterialData> mats_;
};

// Resolved feeds & speeds for one operation.
struct Feeds {
    double rpm = 0;
    double feed = 0;        // cutting feed, mm/min
    double plunge = 0;      // mm/min
    double lead_feed = 0;   // mm/min
    double ramp_feed = 0;   // mm/min
    double stepover = 0;    // mm
    double stepdown = 0;    // mm
    double sfm = 0;         // resolved surface speed (SFM) — informational
    double fz = 0;          // resolved chip load (mm/tooth) — informational
    std::vector<std::string> notes;

    json to_json() const;
};

// Compute feeds/speeds for (material, tool, op). Any nonzero field already set on
// `op` (rpm/feed/plunge/stepover/stepdown) is preserved (treated as a user override);
// only unset (0) fields are computed. Clamps to the machine envelope.
Feeds compute_feeds(const Machine&, const Tool&, const Operation&,
                    const std::string& material_id, const CuttingDB& db = CuttingDB::builtin());

// Apply computed feeds back onto an operation in place (fills only unset fields).
void apply_feeds(Operation&, const Feeds&);

// ──────────────────────────────────────────────────── machine profile DB ──
// Built-in machine presets (aicad.machines/1) + optional CHUDWARE_MACHINES_REGISTRY
// override file that merges/replaces by id. Wire Machine fields map to the
// contract JSON shape via machine_record_json / MachineDB::to_json.
struct MachineRecord {
    std::string id;
    std::string label;
    Machine machine;
    std::string post_dialect;
    std::string notes;
};

class MachineDB {
public:
    MachineDB();                              // loads the five built-in presets
    void merge(const json& doc);              // aicad.machines/1 {"machines":[...]} by id
    const Machine* find(const std::string& id) const;
    const MachineRecord* find_record(const std::string& id) const;
    std::vector<std::string> machine_ids() const;
    json to_json() const;                     // {"machines":[contract records…]}
    static const MachineDB& builtin();
    static const Machine& fallback();         // benchtop_vmc-style default
private:
    std::vector<MachineRecord> machines_;
};

// Map a MachineRecord (or Machine + metadata) to the aicad.machines/1 wire shape.
json machine_record_json(const MachineRecord& r);

}  // namespace cam
}  // namespace chudware
