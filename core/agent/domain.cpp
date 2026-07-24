#include "agent/domain.h"

#include <algorithm>
#include <cctype>
#include <string>
#include <utility>
#include <vector>

namespace chudware {
namespace agent {

namespace {

std::string lower(const std::string& s) {
    std::string o = s;
    std::transform(o.begin(), o.end(), o.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return o;
}

bool has_word(const std::string& hay, const std::string& w) {
    size_t p = hay.find(w);
    while (p != std::string::npos) {
        const bool lok = (p == 0) || !std::isalnum((unsigned char)hay[p - 1]);
        const size_t e = p + w.size();
        const bool rok = (e >= hay.size()) || !std::isalnum((unsigned char)hay[e]);
        if (lok && rok) return true;
        p = hay.find(w, p + 1);
    }
    return false;
}

bool any_word(const std::string& hay, std::initializer_list<const char*> ws) {
    for (const char* w : ws)
        if (has_word(hay, w)) return true;
    return false;
}

}  // namespace

const char* DomainVerdict::label() const {
    switch (domain) {
        case Domain::Modeling: return "modeling";
        case Domain::Sheetmetal: return "sheetmetal";
        case Domain::Assembly: return "assembly";
        case Domain::Cam: return "cam";
        case Domain::Ecad: return "ecad";
        case Domain::Drawing: return "drawing";
        case Domain::Simulation: return "simulation";
        case Domain::Sourcing: return "sourcing";
        case Domain::Research: return "research";
    }
    return "modeling";
}

DomainVerdict classify_domain(const std::string& request) {
    const std::string r = lower(request);

    // Collect matched keywords so the Routing card can show *why*. First-match
    // precedence used to mis-route multi-domain briefs (e.g. a rocket-plane
    // airframe that also named an MCU) straight to ECAD because "mcu" sat at the
    // top of the list. We now score domains, treat weak electronics tokens as
    // non-decisive when mechanical design cues dominate, and only force
    // Simulation on real analysis intent (not "design X with good aerodynamics").
    auto collect = [&](std::initializer_list<const char*> ws) {
        std::vector<std::string> hits;
        for (const char* w : ws)
            if (has_word(r, w)) hits.emplace_back(w);
        return hits;
    };
    auto join_hits = [](const std::vector<std::string>& hits) {
        std::string s;
        for (size_t i = 0; i < hits.size() && i < 6; ++i) {
            if (i) s += ", ";
            s += hits[i];
        }
        if (hits.size() > 6) s += ", …";
        return s;
    };
    auto reason_for = [&](const char* label, const std::vector<std::string>& hits) {
        if (hits.empty()) return std::string(label);
        return std::string(label) + ": " + join_hits(hits);
    };

    // --- ECAD: strong board/schematic cues vs weak component nouns ---
    // Weak terms (mcu, voltage, resistor, …) appear inside multi-domain mech briefs
    // ("rocket plane + flight computer MCU") and must not outrank airframe design.
    const auto ecad_strong = collect({"pcb", "schematic", "netlist", "footprint", "kicad",
                                      "gerber", "printed circuit"});
    // "printed circuit" is multi-word — also catch via find.
    std::vector<std::string> ecad_strong_all = ecad_strong;
    if (r.find("printed circuit") != std::string::npos) {
        bool already = false;
        for (const auto& h : ecad_strong_all)
            if (h == "printed circuit") { already = true; break; }
        if (!already) ecad_strong_all.emplace_back("printed circuit");
    }
    const auto ecad_weak = collect({"circuit", "trace", "copper", "erc", "drc", "resistor",
                                    "capacitor", "voltage", "microcontroller", "mcu"});

    // --- CAM ---
    const auto cam_hits = collect({"toolpath", "gcode", "g-code", "machining", "mill", "milling",
                                   "lathe", "cnc", "post", "setup", "stock", "feeds", "speeds",
                                   "endmill", "roughing", "finishing"});
    bool cam_extra = r.find("cam job") != std::string::npos;

    // --- Simulation: core analysis terms; "aerodynamic(s)/airflow" need analyze intent ---
    // so a design brief that merely wants good aerodynamics stays Modeling.
    const auto sim_core = collect({"fea", "cfd", "strain", "thermal", "modal", "buckling",
                                   "fatigue", "simulation", "simulate", "deflection",
                                   "displacement", "factor of safety"});
    const auto sim_aero = collect({"airflow", "aerodynamic", "aerodynamics"});
    const auto stress_load = collect({"stress", "load"});
    const auto analyze_intent = collect({"analyze", "analyse", "analysis", "simulate",
                                         "simulation", "fea", "cfd", "run a", "run the"});
    const bool sim_aero_ok = !sim_aero.empty() && !analyze_intent.empty();
    const bool sim_stress_ok = !stress_load.empty() && !analyze_intent.empty();
    std::vector<std::string> sim_hits = sim_core;
    if (sim_aero_ok) sim_hits.insert(sim_hits.end(), sim_aero.begin(), sim_aero.end());
    if (sim_stress_ok) {
        for (const auto& h : stress_load) sim_hits.push_back(h);
        for (const auto& h : analyze_intent) {
            bool dup = false;
            for (const auto& s : sim_hits)
                if (s == h) { dup = true; break; }
            if (!dup) sim_hits.push_back(h);
        }
    }

    // --- Drawing ---
    const auto draw_hits = collect({"drawing", "dimension", "gd&t", "gdt", "titleblock",
                                    "section view", "detail view", "annotate",
                                    "tolerance callout"});
    bool draw_extra = r.find("engineering drawing") != std::string::npos;

    // --- Assembly ---
    const auto asm_hits = collect({"assembly", "assemble", "mate", "mates", "components",
                                   "fastened", "bolt together", "screw together", "linkage",
                                   "mechanism", "gearbox"});

    // --- Sheet metal ---
    const auto sm_hits = collect({"sheetmetal", "sheet metal", "flange", "hem", "bend", "jog",
                                  "flat pattern", "gauge", "sm_"});
    bool sm_extra = r.find("sheet-metal") != std::string::npos ||
                    (has_word(r, "enclosure") &&
                     (has_word(r, "steel") || has_word(r, "aluminum") ||
                      has_word(r, "aluminium") || has_word(r, "gauge")));

    // --- Sourcing / parts library (McMaster, catalog, vendor CAD) ---
    const auto src_hits = collect({"bom", "vendor", "sourcing", "purchase", "catalog",
                                   "part number", "mpn", "datasheet", "off-the-shelf",
                                   "mcmaster", "parts library", "part library",
                                   "standard part", "standard parts"});

    // --- Research (no geometry) ---
    const auto research_hits = collect({"research", "look up", "find out", "what is",
                                        "material properties", "yield strength",
                                        "standard for", "specification"});
    const bool research_geom = any_word(r, {"model", "build", "make", "design", "part", "sketch",
                                            "download", "import", "assemble"});

    // --- Mechanical / aero design cues (used to demote weak ECAD) ---
    auto mech_hits = collect({"rocket", "plane", "airframe", "wing", "fuselage", "aircraft",
                              "airplane", "glider", "fin", "fins", "payload bay", "body tube",
                              "bracket", "standoff", "washer", "housing", "chassis",
                              "manifold", "gear", "shaft", "bearing", "hinge", "knob",
                              "plate", "enclosure", "manufacturability", "mechanical",
                              "aero", "aerodynamic", "aerodynamics", "boost-glide",
                              "boost glide", "airfoil", "propeller", "drone", "uav"});
    // Prefix / multi-word cues not cleanly word-bounded by has_word.
    if (r.find("manufactur") != std::string::npos) {
        bool has = false;
        for (const auto& h : mech_hits)
            if (h.find("manufactur") == 0) { has = true; break; }
        if (!has) mech_hits.emplace_back("manufactur*");
    }
    if (r.find("3d print") != std::string::npos || r.find("3d-print") != std::string::npos)
        mech_hits.emplace_back("3d-print");

    // Decisive ECAD: strong board terms always win; weak component nouns only win when
    // the request is not primarily a mechanical/aero design brief.
    const bool weak_ecad_only = ecad_strong_all.empty() && !ecad_weak.empty();
    const bool mech_dominates_weak_ecad =
        weak_ecad_only && mech_hits.size() >= 2 && mech_hits.size() >= ecad_weak.size();
    if (!ecad_strong_all.empty() || (weak_ecad_only && !mech_dominates_weak_ecad)) {
        std::vector<std::string> all = ecad_strong_all;
        all.insert(all.end(), ecad_weak.begin(), ecad_weak.end());
        return {Domain::Ecad, reason_for("electronics/PCB terms", all)};
    }

    if (!cam_hits.empty() || cam_extra)
        return {Domain::Cam, cam_extra && cam_hits.empty()
                                 ? std::string("CAM/machining terms: cam job")
                                 : reason_for("CAM/machining terms", cam_hits)};

    if (!sim_hits.empty())
        return {Domain::Simulation, reason_for("simulation/analysis terms", sim_hits)};

    if (!draw_hits.empty() || draw_extra)
        return {Domain::Drawing, draw_extra && draw_hits.empty()
                                     ? std::string("2D drawing terms: engineering drawing")
                                     : reason_for("2D drawing terms", draw_hits)};

    if (!asm_hits.empty())
        return {Domain::Assembly, reason_for("assembly/mate terms", asm_hits)};

    if (!sm_hits.empty() || sm_extra) {
        std::vector<std::string> hits = sm_hits;
        if (sm_extra && hits.empty()) hits.emplace_back("enclosure+stock");
        return {Domain::Sheetmetal, reason_for("sheet-metal terms", hits)};
    }

    if (!src_hits.empty())
        return {Domain::Sourcing, reason_for("sourcing/BOM terms", src_hits)};

    if (!research_hits.empty() && !research_geom)
        return {Domain::Research, reason_for("research/lookup, no geometry", research_hits)};

    // Mech-dominated multi-domain brief that only had weak ECAD tokens: surface the
    // mechanical matches so the Routing card still explains the call.
    if (mech_dominates_weak_ecad)
        return {Domain::Modeling, reason_for("mechanical design (default; weak ECAD demoted)",
                                            mech_hits)};

    if (!mech_hits.empty())
        return {Domain::Modeling, reason_for("solid modeling", mech_hits)};

    // Default: solid part modeling.
    return {Domain::Modeling, "solid modeling (default)"};
}

namespace {

// Product-capability cues intake must ACCEPT (not reject as "not a CAD design
// request"). Mirrors the app's real surfaces: Parts Library / McMaster, Evaluate,
// CAM, Simulation, ECAD, Reverse Engineering, Drawing, sourcing/BOM.
struct CapHit {
    const char* label;
    bool geometry;  // false => admin/analysis surface; must not run the modeling pipeline
    std::initializer_list<const char*> words;
};

// Project/file/PLM administration cues. These describe work on FILES and RECORDS,
// never on geometry.
bool count_hits(const std::string& r, std::initializer_list<const char*> ws, int* out) {
    int n = 0;
    for (const char* w : ws)
        if (r.find(w) != std::string::npos) ++n;
    *out = n;
    return n > 0;
}

int admin_cue_count(const std::string& r) {
    int n = 0;
    count_hits(r,
               {"clean up", "cleanup", "cleaned up", "clean it up", "file system", "filesystem",
                "folder", "directory", "directories", "organize", "organise", "organized",
                "deprecate", "deprecated", "depricate", "depricated", "rename", "renaming",
                "archive", "version control", "version-control", "plm", "vault", "check in",
                "check-in", "checkin", "checkpoint", "revision history", "project history",
                "initialize project", "initialize the project", "tracked", "tracking",
                "move the unrelated", "move all", "separate the", "seperate the"},
               &n);
    return n;
}

// Cues that a part is actually being asked for. Deliberately narrow: bare words like
// "model" or "part" appear constantly in admin prose ("major modeling changes",
// "initialize the parts") and must not count.
int build_cue_count(const std::string& r) {
    int n = 0;
    count_hits(r,
               {"model a ", "model the ", "design a ", "design the ", "build a ", "build the ",
                "make a ", "make the ", "create a ", "create the ", "sketch", "extrude",
                "revolve", "fillet", "chamfer", "counterbore", "bracket", "housing", "flange",
                "gear", "shaft", "bearing", "enclosure", "standoff", "mounting hole"},
               &n);
    return n;
}

std::pair<CapabilityKind, std::string> match_capability(const std::string& request) {
    const std::string r = lower(request);
    static const CapHit kCaps[] = {
        {"parts-library/McMaster fetch", true,
         {"mcmaster", "parts library", "part library", "standard part", "standard parts",
          "catalog part", "vendor cad", "vendor part", "download a model", "download model",
          "import a model", "import model", "fetch a part", "fetch part", "off-the-shelf",
          "off the shelf", "grabcad", "digikey", "mouser", "fastenal", "traceparts",
          "3d contentcentral", "contentcentral"}},
        {"measure/evaluate", false,
         {"measure", "evaluate", "mass properties", "mass props", "interference",
          "clearance check", "fitment", "section analysis"}},
        {"CAM/toolpaths", true,
         {"toolpath", "gcode", "g-code", "cam job", "post process", "cnc", "machining"}},
        {"simulation/analysis", true,
         {"fea", "cfd", "simulation", "simulate", "finite element", "factor of safety"}},
        {"ECAD/PCB", true,
         {"pcb", "schematic", "netlist", "footprint", "kicad", "gerber", "printed circuit"}},
        {"reverse-engineering", true,
         {"reverse engineer", "reverse-engineer", "reverse engineering",
          "rebuild imported", "from scan", "from mesh", "from stl", "from step"}},
        {"engineering drawing", true,
         {"engineering drawing", "detail drawing", "section view", "titleblock", "gd&t", "gdt"}},
        {"sourcing/BOM", false,
         {"bom", "bill of materials", "sourcing", "part number", "mpn", "datasheet"}},
        {"PLM/version-control", false,
         {"plm", "version control", "version-control", "check in", "check-in", "checkin",
          "checkpoint", "vault", "project history", "part history", "revision history",
          "tag a release", "release version", "deprecate", "deprecated", "retire a part",
          "initialize project", "initialize the project"}},
    };
    // Admin dominance wins over every keyword table below. A brief that is mostly
    // "clean up the folders, move the unrelated .step files out, deprecate the old
    // parts, track it in PLM" is project administration even though it name-drops
    // parts and modeling — and feeding it to the modeling pipeline turns each plan
    // step into a bogus extruded plate.
    const int admin_n = admin_cue_count(r);
    const int build_n = build_cue_count(r);
    if (admin_n >= 3 && admin_n > build_n)
        return {CapabilityKind::NonGeometry,
                "capability: PLM/project administration (" + std::to_string(admin_n) +
                    " admin cues vs " + std::to_string(build_n) + " build cues)"};

    for (const CapHit& cap : kCaps) {
        for (const char* w : cap.words) {
            if (has_word(r, w) || r.find(w) != std::string::npos)
                return {cap.geometry ? CapabilityKind::Geometry : CapabilityKind::NonGeometry,
                        std::string("capability: ") + cap.label + " (" + w + ")"};
        }
    }
    // Broader multi-word phrases that may not be word-bounded cleanly.
    if (r.find("parts library") != std::string::npos ||
        r.find("part library") != std::string::npos ||
        (r.find("download") != std::string::npos &&
         (r.find("mcmaster") != std::string::npos || r.find("model") != std::string::npos ||
          r.find("cad") != std::string::npos || r.find("step") != std::string::npos)))
        return {CapabilityKind::Geometry, "capability: parts-library/McMaster fetch"};
    return {CapabilityKind::None, {}};
}

}  // namespace

bool is_capability_request(const std::string& request) {
    return match_capability(request).first != CapabilityKind::None;
}

std::string capability_request_reason(const std::string& request) {
    return match_capability(request).second;
}

CapabilityKind capability_kind(const std::string& request) {
    return match_capability(request).first;
}

bool is_admin_request(const std::string& request) {
    const std::string r = lower(request);
    const int admin_n = admin_cue_count(r);
    return admin_n >= 3 && admin_n > build_cue_count(r);
}

bool is_general_work_request(const std::string& request) {
    const std::string r = lower(request);
    // Explicit build verbs/part nouns ("model a …", "bracket", "shaft", …) are a
    // clear geometry request — run the modeling pipeline.
    if (build_cue_count(r) > 0) return false;
    // Any file/disk/PLM/organize cue keeps it general even when it name-drops a
    // part ("organize the parts library", "clean up the bracket exports").
    if (admin_cue_count(r) > 0) return true;
    // No build cue and no admin cue: a geometry-PRODUCING capability (parts-library
    // fetch, CAM, drawing, …) still opts into the pipeline; a non-geometry
    // capability (measure/PLM/BOM) or nothing at all is general work.
    if (capability_kind(request) == CapabilityKind::Geometry) return false;
    // Default lane: general work runs as one free linear turn.
    return true;
}

std::vector<std::string> tool_classes_for_domain(Domain d) {
    // Every agent gets the safe read/inspect + doc-query core; the domain adds its
    // own mutating class(es). "sourcing" is added where standard parts are relevant.
    std::vector<std::string> base = {"read", "inspect", "measure"};
    auto with = [&](std::initializer_list<const char*> extra) {
        for (const char* e : extra) base.push_back(e);
        return base;
    };
    switch (d) {
        case Domain::Modeling: return with({"modeling", "sketch", "sourcing"});
        case Domain::Sheetmetal: return with({"modeling", "sketch", "sheetmetal"});
        case Domain::Assembly: return with({"assembly", "modeling", "sourcing"});
        case Domain::Cam: return with({"cam"});
        case Domain::Ecad: return with({"ecad"});
        case Domain::Drawing: return with({"drawing"});
        case Domain::Simulation: return with({"simulation"});
        case Domain::Sourcing: return with({"sourcing"});
        case Domain::Research: return with({"research"});
    }
    return base;
}

std::vector<std::string> context_sheets_for_domain(Domain d) {
    switch (d) {
        case Domain::Modeling:
            return {"box", "cylinder", "extrude", "revolve", "loft", "sweep", "fillet",
                    "chamfer", "shell", "pattern", "mirror", "hole", "thread", "hardware"};
        case Domain::Sheetmetal: return {"sheetmetal", "box", "hole"};
        case Domain::Assembly: return {"hardware", "fits", "bearings", "threads"};
        case Domain::Cam: return {};        // CAM knowledge is in the CAM guide, not sheets
        case Domain::Ecad: return {"ecad"};
        case Domain::Drawing: return {"drawing"};
        case Domain::Simulation: return {"materials"};
        case Domain::Sourcing: return {"hardware", "fits", "threads", "bearings"};
        case Domain::Research: return {"research", "materials"};
    }
    return {};
}

}  // namespace agent
}  // namespace chudware
