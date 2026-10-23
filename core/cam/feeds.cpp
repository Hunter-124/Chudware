// core/cam/feeds.cpp — feeds & speeds calculator + builtin material cutting-data
// database. Values are clean-room, drawn from public machining-handbook ranges
// (Machinery's Handbook / tool-maker speed-&-feed charts are dimensional facts,
// not copyrightable); they are conservative defaults a user is expected to tune.
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI (mirrors geom/plane.cpp)
#include "cam/feeds.h"

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>

namespace chudware {
namespace cam {

static double clampd(double v, double lo, double hi) { return std::max(lo, std::min(hi, v)); }
static double num(const json& j, double d) { return j.is_number() ? j.get<double>() : d; }

// ───────────────────────────────────────────────────── MaterialData JSON ──
json MaterialData::to_json() const {
    return json{{"id", id}, {"name", name}, {"group", group}, {"density_g_cm3", density_g_cm3},
                {"sfm_mill", sfm_mill}, {"sfm_drill", sfm_drill}, {"sfm_turn", sfm_turn},
                {"hss_factor", hss_factor}, {"fz_ref", fz_ref}, {"fz_ref_dia", fz_ref_dia},
                {"stepover_frac", stepover_frac}, {"stepdown_frac", stepdown_frac},
                {"plunge_frac", plunge_frac}, {"cut_feed_ref", cut_feed_ref}};
}
MaterialData MaterialData::from_json(const json& j) {
    MaterialData m;
    m.id = j.value("id", std::string());
    m.name = j.value("name", m.id);
    m.group = j.value("group", std::string("steel"));
    m.density_g_cm3 = num(j.value("density_g_cm3", json(0.0)), 0);
    m.sfm_mill = num(j.value("sfm_mill", json(0.0)), 0);
    m.sfm_drill = num(j.value("sfm_drill", json(0.0)), 0);
    m.sfm_turn = num(j.value("sfm_turn", json(0.0)), 0);
    m.hss_factor = num(j.value("hss_factor", json(0.4)), 0.4);
    m.fz_ref = num(j.value("fz_ref", json(0.05)), 0.05);
    m.fz_ref_dia = num(j.value("fz_ref_dia", json(6.0)), 6.0);
    m.stepover_frac = num(j.value("stepover_frac", json(0.4)), 0.4);
    m.stepdown_frac = num(j.value("stepdown_frac", json(1.0)), 1.0);
    m.plunge_frac = num(j.value("plunge_frac", json(0.3)), 0.3);
    m.cut_feed_ref = num(j.value("cut_feed_ref", json(0.0)), 0);
    return m;
}

// ───────────────────────────────────────────────────── builtin DB table ──
// {id, name, group, density, sfm_mill, sfm_drill, sfm_turn, hss_factor,
//  fz_ref, fz_ref_dia, stepover_frac, stepdown_frac, plunge_frac, cut_feed_ref}
namespace {
MaterialData M(const char* id, const char* name, const char* grp, double dens, double sm,
               double sd, double st, double hssf, double fz, double soF, double sdF,
               double cutref = 0) {
    MaterialData m;
    m.id = id; m.name = name; m.group = grp; m.density_g_cm3 = dens;
    m.sfm_mill = sm; m.sfm_drill = sd; m.sfm_turn = st; m.hss_factor = hssf;
    m.fz_ref = fz; m.fz_ref_dia = 6.0; m.stepover_frac = soF; m.stepdown_frac = sdF;
    m.plunge_frac = 0.3; m.cut_feed_ref = cutref;
    return m;
}
std::vector<MaterialData> builtin_table() {
    return {
        //  id            name                group         dens   mill drill turn  hss   fz    so   sd   cutref
        M("aluminum",     "Aluminum 6061",    "nonferrous", 2.70,  900, 300, 800, 0.45, 0.060, 0.45, 1.0),
        M("aluminum_cast","Cast Aluminum",    "nonferrous", 2.66,  700, 250, 650, 0.45, 0.050, 0.40, 1.0),
        M("brass",        "Brass",            "nonferrous", 8.50,  600, 250, 650, 0.50, 0.050, 0.45, 1.0),
        M("bronze",       "Bronze",           "nonferrous", 8.80,  400, 180, 450, 0.45, 0.045, 0.40, 1.0),
        M("copper",       "Copper",           "nonferrous", 8.96,  400, 200, 450, 0.45, 0.050, 0.40, 1.0),
        M("steel_mild",   "Mild Steel 1018",  "steel",      7.87,  380, 90,  400, 0.40, 0.040, 0.35, 0.8),
        M("steel_alloy",  "Alloy Steel 4140", "steel",      7.85,  300, 75,  330, 0.38, 0.035, 0.30, 0.6),
        M("steel_tool",   "Tool Steel",       "steel",      7.80,  200, 55,  220, 0.35, 0.030, 0.25, 0.5),
        M("stainless_304","Stainless 304",    "stainless",  8.00,  250, 60,  300, 0.40, 0.035, 0.30, 0.5),
        M("stainless_316","Stainless 316",    "stainless",  8.00,  220, 55,  270, 0.40, 0.033, 0.28, 0.5),
        M("cast_iron",    "Cast Iron",        "cast_iron",  7.20,  300, 90,  350, 0.45, 0.045, 0.40, 0.6),
        M("titanium",     "Titanium Ti-6Al-4V","titanium",  4.43,  150, 40,  150, 0.30, 0.030, 0.20, 0.3),
        M("inconel",      "Inconel 718",      "hardened",   8.19,  90,  25,  100, 0.30, 0.025, 0.15, 0.2),
        M("hardened_steel","Hardened Steel >45HRC","hardened",7.80, 120, 30,  130, 0.30, 0.020, 0.12, 0.3),
        M("magnesium",    "Magnesium",        "nonferrous", 1.74, 1200, 400,1000, 0.50, 0.080, 0.50, 1.0),
        M("plastic",      "Plastic (ABS/PC)", "plastic",    1.10, 1000, 400,1000, 0.50, 0.080, 0.60, 1.0),
        M("acetal",       "Acetal/Delrin",    "plastic",    1.41, 1000, 500,1000, 0.50, 0.080, 0.60, 1.0),
        M("acrylic",      "Acrylic/PMMA",     "plastic",    1.18,  800, 300, 800, 0.50, 0.060, 0.50, 1.0),
        M("hdpe",         "HDPE/UHMW",        "plastic",    0.96, 1200, 500,1000, 0.55, 0.090, 0.60, 1.0),
        M("wood_hard",    "Hardwood",         "wood",       0.75,  900, 300, 0,   0.50, 0.100, 0.80, 1.0),
        M("wood_soft",    "Softwood/Plywood", "wood",       0.50, 1000, 350, 0,   0.55, 0.120, 0.90, 1.0),
        M("mdf",          "MDF",              "wood",       0.75,  900, 300, 0,   0.55, 0.100, 0.90, 1.0),
        M("foam",         "Modeling Foam",    "wood",       0.10, 1500, 500, 0,   0.60, 0.200, 1.00, 1.0),
        M("carbon_fiber", "Carbon Fiber/CFRP","composite",  1.60,  300, 120, 300, 0.35, 0.030, 0.30, 0.4),
        M("g10",          "G10/FR4",          "composite",  1.85,  300, 120, 300, 0.35, 0.030, 0.30, 0.4),
    };
}
}  // namespace

// ───────────────────────────────────────────────────────────── CuttingDB ──
CuttingDB::CuttingDB() : mats_(builtin_table()) {}
void CuttingDB::merge(const json& overrides) {
    if (!overrides.is_object() || !overrides.contains("materials")) return;
    for (auto& mj : overrides["materials"]) {
        MaterialData m = MaterialData::from_json(mj);
        if (m.id.empty()) continue;
        bool replaced = false;
        for (auto& e : mats_)
            if (e.id == m.id) { e = m; replaced = true; break; }
        if (!replaced) mats_.push_back(m);
    }
}
void CuttingDB::merge_cam(const json& registry) {
    if (!registry.is_object() || !registry.contains("materials")) return;
    const json& mats = registry["materials"];
    if (!mats.is_object()) return;
    for (auto it = mats.begin(); it != mats.end(); ++it) {
        const json& rec = it.value();
        if (!rec.is_object() || !rec.contains("cam")) continue;
        const json& cam = rec["cam"];
        if (!cam.is_object()) continue;
        std::string gid = cam.value("id", std::string());
        if (gid.empty()) continue;
        MaterialData* m = nullptr;
        for (auto& e : mats_) if (e.id == gid) { m = &e; break; }
        if (!m) continue;  // registry names a cutting group absent from the in-code base -> skip
        // Overwrite ONLY the feeds fields present in the cam section; identity is preserved.
        if (cam.contains("sfm_mill"))      m->sfm_mill      = num(cam["sfm_mill"], m->sfm_mill);
        if (cam.contains("sfm_drill"))     m->sfm_drill     = num(cam["sfm_drill"], m->sfm_drill);
        if (cam.contains("sfm_turn"))      m->sfm_turn      = num(cam["sfm_turn"], m->sfm_turn);
        if (cam.contains("hss_factor"))    m->hss_factor    = num(cam["hss_factor"], m->hss_factor);
        if (cam.contains("fz_ref"))        m->fz_ref        = num(cam["fz_ref"], m->fz_ref);
        if (cam.contains("fz_ref_dia"))    m->fz_ref_dia    = num(cam["fz_ref_dia"], m->fz_ref_dia);
        if (cam.contains("stepover_frac")) m->stepover_frac = num(cam["stepover_frac"], m->stepover_frac);
        if (cam.contains("stepdown_frac")) m->stepdown_frac = num(cam["stepdown_frac"], m->stepdown_frac);
        if (cam.contains("plunge_frac"))   m->plunge_frac   = num(cam["plunge_frac"], m->plunge_frac);
        if (cam.contains("cut_feed_ref"))  m->cut_feed_ref  = num(cam["cut_feed_ref"], m->cut_feed_ref);
    }
}
const MaterialData* CuttingDB::find(const std::string& id) const {
    for (auto& m : mats_) if (m.id == id) return &m;
    return nullptr;
}
std::vector<std::string> CuttingDB::material_ids() const {
    std::vector<std::string> ids;
    for (auto& m : mats_) ids.push_back(m.id);
    return ids;
}
json CuttingDB::to_json() const {
    json a = json::array();
    for (auto& m : mats_) a.push_back(m.to_json());
    return json{{"materials", a}};
}
// Resolve the engine materials registry.json location: env override first (tests + explicit
// installs point at it), then the %APPDATA%/Chudware (XDG on *nix) data root — mirrors
// core/agent/paths.cpp without linking chudware_agent (cam stays dependency-light). Empty path when
// no root is discoverable -> the in-code table stands.
namespace {
std::filesystem::path resolve_registry_path() {
    namespace fs = std::filesystem;
    if (const char* env = std::getenv("CHUDWARE_MATERIALS_REGISTRY"); env && env[0])
        return fs::path(env);
    fs::path root;
#ifdef _WIN32
    if (const char* base = std::getenv("APPDATA"); base && base[0]) root = fs::path(base) / "Chudware";
#else
    if (const char* xdg = std::getenv("XDG_DATA_HOME"); xdg && xdg[0])
        root = fs::path(xdg) / "Chudware";
    else if (const char* home = std::getenv("HOME"); home && home[0])
        root = fs::path(home) / ".local" / "share" / "Chudware";
#endif
    if (!root.empty()) {
        const fs::path p = root / "materials" / "registry.json";
        if (fs::exists(p)) return p;
    }
#ifdef CHUDWARE_BUNDLED_MATERIALS_REGISTRY
    return fs::path(CHUDWARE_BUNDLED_MATERIALS_REGISTRY);  // bundled repo seed (last resort)
#endif
    return {};
}
}  // namespace

const CuttingDB& CuttingDB::builtin() {
    // Seed the in-code table (the byte-identical fallback), then fold the registry's cam
    // sections on top when the registry is reachable. Absent/unparseable registry -> the
    // in-code table stands, so headless/offline builds are unaffected.
    static CuttingDB db = [] {
        CuttingDB d;
        try {
            std::filesystem::path p = resolve_registry_path();
            if (!p.empty()) {
                std::ifstream fh(p, std::ios::binary);
                if (fh) {
                    json doc = json::parse(fh, nullptr, /*allow_exceptions=*/false);
                    if (!doc.is_discarded()) d.merge_cam(doc);
                }
            }
        } catch (...) {
            // Registry unreadable/malformed -> keep the in-code table (never throw from builtin()).
        }
        return d;
    }();
    return db;
}

// ───────────────────────────────────────────────────────────── Feeds ──
json Feeds::to_json() const {
    return json{{"rpm", rpm}, {"feed", feed}, {"plunge", plunge}, {"lead_feed", lead_feed},
                {"ramp_feed", ramp_feed}, {"stepover", stepover}, {"stepdown", stepdown},
                {"sfm", sfm}, {"fz", fz}, {"notes", notes}};
}

// SFM -> RPM for a given cutting diameter (mm).
static double sfm_to_rpm(double sfm, double dia_mm) {
    if (dia_mm <= 1e-6) return 0;
    // Vc[m/min] = sfm * 0.3048 ; rpm = Vc*1000 / (pi*D)
    return (sfm * 0.3048 * 1000.0) / (M_PI * dia_mm);
}

Feeds compute_feeds(const Machine& mach, const Tool& tool, const Operation& op,
                    const std::string& material_id, const CuttingDB& db) {
    Feeds f;
    const MaterialData* mat = db.find(material_id);
    if (!mat) mat = db.find("aluminum");
    if (!mat) { f.notes.push_back("no material data; using zeros"); return f; }

    std::string cls = tool_class(tool.type);
    double dia = std::max(0.1, tool.diameter);

    // per-tool override block for this material group, if any
    double sfm_ov = 0, fz_ov = 0, so_ov = 0, sd_ov = 0;
    if (tool.cutting_overrides.is_object() && tool.cutting_overrides.contains(material_id)) {
        const json& o = tool.cutting_overrides[material_id];
        sfm_ov = num(o.value("sfm", json(0.0)), 0);
        fz_ov = num(o.value("fz", json(0.0)), 0);
        so_ov = num(o.value("stepover", json(0.0)), 0);
        sd_ov = num(o.value("stepdown", json(0.0)), 0);
    }

    bool hss = (tool.tool_material == "hss" || tool.tool_material == "cobalt");
    double hssf = hss ? mat->hss_factor : 1.0;

    // ── 2D cutting heads (laser/plasma/waterjet): feed-only, no spindle ──
    if (cls == "laser" || cls == "plasma" || cls == "waterjet") {
        double thick = std::max(1.0, op.heights.top - op.heights.bottom);
        double base = mat->cut_feed_ref > 0 ? mat->cut_feed_ref : 2000.0;
        f.feed = base / thick;     // thicker -> slower
        f.rpm = 0;
        f.notes.push_back("cut feed scaled by " + std::to_string(thick) + "mm thickness");
        f.feed = clampd(f.feed, 50.0, mach.max_feed);
        f.lead_feed = f.feed;
        f.plunge = f.feed;
        return f;
    }

    // ── drilling / boring / reaming ──
    if (cls == "drill") {
        double sfm = sfm_ov > 0 ? sfm_ov : mat->sfm_drill * hssf;
        f.sfm = sfm;
        f.rpm = sfm_to_rpm(sfm, dia);
        // feed per rev ~ a fraction of diameter (0.01..0.04 D), conservative
        double fpr = clampd(0.02 * dia, 0.02, 0.30);
        f.feed = f.rpm * fpr;          // plunge feed for a drill IS the cut feed
        f.plunge = f.feed;
        f.fz = fpr;
        f.notes.push_back("drill: feed/rev " + std::to_string(fpr) + "mm");
    } else if (cls == "tap") {
        double sfm = sfm_ov > 0 ? sfm_ov : mat->sfm_drill * 0.3 * hssf;  // taps run slow
        f.rpm = sfm_to_rpm(std::max(20.0, sfm), dia);
        double pitch = tool.thread_pitch > 0 ? tool.thread_pitch : (op.thread_pitch > 0 ? op.thread_pitch : 1.0);
        f.feed = f.rpm * pitch;        // synchronized: feed = rpm * pitch
        f.plunge = f.feed;
        f.fz = pitch;
        f.sfm = sfm;
        f.notes.push_back("tap: synchronized feed = rpm * pitch(" + std::to_string(pitch) + ")");
    } else if (cls == "turning") {
        double sfm = sfm_ov > 0 ? sfm_ov : mat->sfm_turn * hssf;
        f.sfm = sfm;
        // reference rpm at the part's start diameter if known, else a 50mm reference
        double ref_dia = op.geometry.contains("max_dia") ? num(op.geometry["max_dia"], 50.0) : 50.0;
        f.rpm = clampd(sfm_to_rpm(sfm, std::max(1.0, ref_dia)), mach.min_rpm, op.max_turn_rpm > 0 ? op.max_turn_rpm : mach.max_rpm);
        double fpr = (op.type == OpType::TurnFinish) ? 0.12 : 0.25;  // mm/rev
        // Keep the programmed mm/rev chipload when the operation overrides RPM.
        f.feed = (op.rpm > 0 ? op.rpm : f.rpm) * fpr;
        f.plunge = f.feed * 0.5;
        f.fz = fpr;
        f.notes.push_back("turning: feed/rev " + std::to_string(fpr) + "mm");
    } else {
        // ── milling (endmill / ball / bull / face / chamfer / thread / slot) ──
        double sfm = sfm_ov > 0 ? sfm_ov : mat->sfm_mill * hssf;
        f.sfm = sfm;
        f.rpm = sfm_to_rpm(sfm, dia);
        // chip load scales with diameter from the reference; clamp to sane band
        double fz = fz_ov > 0 ? fz_ov : mat->fz_ref * clampd(dia / mat->fz_ref_dia, 0.4, 3.0);
        if (cls == "ballmill") fz *= 0.7;     // ball tips take a lighter chip
        if (cls == "chamfer") fz *= 0.6;
        int flutes = std::max(1, tool.flutes);
        f.fz = fz;
        f.feed = f.rpm * fz * flutes;
        f.plunge = f.feed * mat->plunge_frac;
        f.notes.push_back("mill: fz " + std::to_string(fz) + "mm x " + std::to_string(flutes) + " flutes");
    }

    // stepover / stepdown defaults (radial / axial)
    f.stepover = so_ov > 0 ? so_ov : mat->stepover_frac * dia;
    f.stepdown = sd_ov > 0 ? sd_ov : mat->stepdown_frac * dia;
    if (cls == "ballmill") f.stepover = so_ov > 0 ? so_ov : 0.15 * dia;  // fine for finish
    f.lead_feed = f.feed * 0.5;
    f.ramp_feed = f.feed * 0.5;

    // machine clamps
    if (mach.max_rpm > 0) f.rpm = std::min(f.rpm, mach.max_rpm);
    if (mach.min_rpm > 0) f.rpm = std::max(f.rpm, mach.min_rpm);
    if (mach.max_feed > 0) {
        f.feed = std::min(f.feed, mach.max_feed);
        f.plunge = std::min(f.plunge, mach.max_feed);
        f.lead_feed = std::min(f.lead_feed, mach.max_feed);
        f.ramp_feed = std::min(f.ramp_feed, mach.max_feed);
    }
    return f;
}

void apply_feeds(Operation& op, const Feeds& f) {
    if (op.rpm == 0) op.rpm = f.rpm;
    if (op.feed == 0) op.feed = f.feed;
    if (op.plunge == 0) op.plunge = f.plunge;
    if (op.lead_feed == 0) op.lead_feed = f.lead_feed;
    if (op.ramp_feed == 0) op.ramp_feed = f.ramp_feed;
    if (op.stepover == 0) op.stepover = f.stepover;
    if (op.stepdown == 0) op.stepdown = f.stepdown;
}

// ───────────────────────────────────────────────────── MachineDB ──
namespace {
Machine mk_machine(MachineKind kind, const char* name, double tx, double ty, double tz,
                   double rxy, double rz, double max_rpm, double min_rpm, double max_feed,
                   double tool_change_s, double accel) {
    Machine m;
    m.kind = kind;
    m.name = name;
    m.travel = {tx, ty, tz};
    m.rapid_xy = rxy;
    m.rapid_z = rz;
    m.max_rpm = max_rpm;
    m.min_rpm = min_rpm;
    m.max_feed = max_feed;
    m.tool_change_s = tool_change_s;
    m.accel = accel;
    return m;
}

std::vector<MachineRecord> builtin_machines() {
    // Values must match engine_oracle/research/data/machines.json exactly.
    return {
        MachineRecord{"benchtop_vmc", "Benchtop VMC",
                      mk_machine(MachineKind::Mill3, "Benchtop VMC", 300, 300, 150,
                                 5000, 3000, 15000, 100, 5000, 6.0, 0.0),
                      "grbl",
                      "Generic benchtop VMC defaults (matches engine_oracle/research/machining.py)."},
        MachineRecord{"tormach_770", "Tormach 770",
                      mk_machine(MachineKind::Mill3, "Tormach 770", 355, 280, 405,
                                 5000, 3000, 10000, 100, 5000, 8.0, 0.0),
                      "centroid", "Tormach 770 Series 3."},
        MachineRecord{"haas_vf2", "Haas VF-2",
                      mk_machine(MachineKind::Mill3, "Haas VF-2", 762, 406, 508,
                                 12000, 12000, 8100, 100, 8000, 3.5, 0.0),
                      "haas", "Haas VF-2 vertical machining center."},
        MachineRecord{"grbl_router", "GRBL gantry router",
                      mk_machine(MachineKind::Router, "GRBL gantry router", 600, 600, 100,
                                 5000, 3000, 24000, 8000, 6000, 0.0, 0.0),
                      "grbl", "Typical CNC router (Shapeoko-class) running GRBL."},
        MachineRecord{"lathe_basic", "Basic CNC lathe",
                      mk_machine(MachineKind::Lathe, "Basic CNC lathe", 200, 0, 400,
                                 5000, 3000, 4000, 50, 3000, 4.0, 0.0),
                      "linuxcnc", "Small 2-axis CNC lathe."},
    };
}

MachineRecord record_from_json(const json& j) {
    MachineRecord r;
    r.id = j.value("id", std::string());
    r.label = j.value("label", r.id);
    r.post_dialect = j.value("post_dialect", std::string("grbl"));
    r.notes = j.value("notes", std::string());
    r.machine.kind = machine_kind_from_string(j.value("kind", std::string("mill3")));
    r.machine.name = r.label;
    if (j.contains("travel_mm") && j["travel_mm"].is_array() && j["travel_mm"].size() >= 3) {
        r.machine.travel.x = num(j["travel_mm"][0], 0);
        r.machine.travel.y = num(j["travel_mm"][1], 0);
        r.machine.travel.z = num(j["travel_mm"][2], 0);
    } else if (j.contains("travel")) {
        r.machine.travel = vec3_from_json(j["travel"]);
    }
    r.machine.rapid_xy = num(j.value("rapid_xy_mm_min", j.value("rapid_xy", json(5000.0))), 5000);
    r.machine.rapid_z = num(j.value("rapid_z_mm_min", j.value("rapid_z", json(3000.0))), 3000);
    r.machine.max_rpm = num(j.value("max_rpm", json(24000.0)), 24000);
    r.machine.min_rpm = num(j.value("min_rpm", json(0.0)), 0);
    r.machine.max_feed = num(j.value("max_feed_mm_min", j.value("max_feed", json(10000.0))), 10000);
    r.machine.tool_change_s = num(j.value("tool_change_s", json(6.0)), 6.0);
    r.machine.accel = num(j.value("accel_mm_s2", j.value("accel", json(0.0))), 0);
    return r;
}

std::filesystem::path resolve_machines_registry_path() {
    namespace fs = std::filesystem;
    if (const char* env = std::getenv("CHUDWARE_MACHINES_REGISTRY"); env && env[0])
        return fs::path(env);
    return {};
}
}  // namespace

json machine_record_json(const MachineRecord& r) {
    return json{
        {"id", r.id},
        {"label", r.label},
        {"kind", to_string(r.machine.kind)},
        {"travel_mm", json::array({r.machine.travel.x, r.machine.travel.y, r.machine.travel.z})},
        {"rapid_xy_mm_min", r.machine.rapid_xy},
        {"rapid_z_mm_min", r.machine.rapid_z},
        {"max_rpm", r.machine.max_rpm},
        {"min_rpm", r.machine.min_rpm},
        {"max_feed_mm_min", r.machine.max_feed},
        {"tool_change_s", r.machine.tool_change_s},
        {"accel_mm_s2", r.machine.accel},
        {"post_dialect", r.post_dialect},
        {"notes", r.notes},
    };
}

MachineDB::MachineDB() : machines_(builtin_machines()) {}

void MachineDB::merge(const json& doc) {
    if (!doc.is_object() || !doc.contains("machines") || !doc["machines"].is_array()) return;
    for (const auto& j : doc["machines"]) {
        if (!j.is_object()) continue;
        MachineRecord r = record_from_json(j);
        if (r.id.empty()) continue;
        bool replaced = false;
        for (auto& existing : machines_) {
            if (existing.id == r.id) {
                existing = std::move(r);
                replaced = true;
                break;
            }
        }
        if (!replaced) machines_.push_back(std::move(r));
    }
}

const MachineRecord* MachineDB::find_record(const std::string& id) const {
    for (const auto& m : machines_)
        if (m.id == id) return &m;
    return nullptr;
}

const Machine* MachineDB::find(const std::string& id) const {
    const MachineRecord* r = find_record(id);
    return r ? &r->machine : nullptr;
}

std::vector<std::string> MachineDB::machine_ids() const {
    std::vector<std::string> ids;
    ids.reserve(machines_.size());
    for (const auto& m : machines_) ids.push_back(m.id);
    return ids;
}

json MachineDB::to_json() const {
    json arr = json::array();
    for (const auto& m : machines_) arr.push_back(machine_record_json(m));
    return json{{"machines", arr}};
}

const MachineDB& MachineDB::builtin() {
    static MachineDB db = [] {
        MachineDB d;
        try {
            std::filesystem::path p = resolve_machines_registry_path();
            if (!p.empty()) {
                std::ifstream fh(p, std::ios::binary);
                if (fh) {
                    json doc = json::parse(fh, nullptr, /*allow_exceptions=*/false);
                    if (!doc.is_discarded()) d.merge(doc);
                }
            }
        } catch (...) {
            // Unreadable/malformed registry -> keep built-ins.
        }
        return d;
    }();
    return db;
}

const Machine& MachineDB::fallback() {
    static const Machine* fb = []() -> const Machine* {
        const Machine* m = MachineDB::builtin().find("benchtop_vmc");
        if (m) return m;
        static Machine def = mk_machine(MachineKind::Mill3, "Benchtop VMC", 300, 300, 150,
                                        5000, 3000, 15000, 100, 5000, 6.0, 0.0);
        return &def;
    }();
    return *fb;
}

}  // namespace cam
}  // namespace chudware
