// core/dfm/process_profiles.cpp — see process_profiles.h. Mirrors
// engine_oracle/dfm/process_profiles.py: the load-bearing section-5 numbers, the
// cue-table resolver with the FDM prior and the (a)/(b)/(c) ambiguity-gate rules,
// and Python's dict-ordering / formatting semantics (stable sorts, insertion-order
// max, repr-style error messages, %-formatting).
#include "dfm/process_profiles.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cmath>
#include <regex>

#include "materials/materials.h"

namespace chudware {
namespace dfm {

namespace {

// Python round(x, ndigits): round-half-EVEN on the scaled value.
double pyround(double v, int ndigits) {
    const double scale = std::pow(10.0, ndigits);
    const double scaled = v * scale;
    const double fl = std::floor(scaled);
    const double diff = scaled - fl;
    double r;
    if (diff > 0.5)
        r = fl + 1.0;
    else if (diff < 0.5)
        r = fl;
    else
        r = (std::fmod(fl, 2.0) == 0.0) ? fl : fl + 1.0;
    return r / scale;
}

// Python f"{v:g}" — 6 significant digits, trailing zeros stripped (printf %g).
std::string py_g(double v) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%g", v);
    return buf;
}

// Python f"{v:+.2f}".
std::string py_signed2(double v) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%+.2f", v);
    return buf;
}

// Python f"{v:.2f}".
std::string py_fixed2(double v) {
    char buf[64];
    std::snprintf(buf, sizeof buf, "%.2f", v);
    return buf;
}

std::string lower(const std::string& s) {
    std::string out = s;
    for (char& ch : out) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
    return out;
}

std::string upper(const std::string& s) {
    std::string out = s;
    for (char& ch : out) ch = static_cast<char>(std::toupper(static_cast<unsigned char>(ch)));
    return out;
}

std::string strip(const std::string& s) {
    const size_t first = s.find_first_not_of(" \t\n\r\f\v");
    if (first == std::string::npos) return "";
    const size_t last = s.find_last_not_of(" \t\n\r\f\v");
    return s.substr(first, last - first + 1);
}

// Python _norm(s): str(s or "").strip().lower().
std::string norm(const json& s) {
    if (!s.is_string()) return "";
    return strip(lower(s.get<std::string>()));
}

// Python repr(str) for the simple ids these messages carry: single-quoted.
std::string repr_s(const std::string& s) { return "'" + s + "'"; }

// Python repr(list[str]): ['a', 'b'].
std::string repr_list(const std::vector<std::string>& items) {
    std::string out = "[";
    for (size_t i = 0; i < items.size(); ++i) {
        if (i) out += ", ";
        out += repr_s(items[i]);
    }
    return out + "]";
}

// Density (g/cm^3) from the unified materials registry, falling back to the
// oracle's literal when the registry is unreachable — Python's _reg_density.
double reg_density(const std::string& name, double fallback) {
    const json d = materials::density(name);
    return d.is_number() ? d.get<double>() : fallback;
}

// ---------------------------------------------------------------- profile model
struct Profile {
    std::string process;
    std::string material_name;
    std::string density_lookup;  // registry alias for the default density
    json density_fallback;
    json min_wall_mm;
    json min_feature_mm;
    json hole_comp_mm;
    json tolerance_band_mm;
    json fit_classes_mm;  // {"slip","location","press","free"} values double|null
    json draft;           // {"required","min_deg"}
    json overhang;        // {"max_deg","max_bridge_mm"} int|null
    json thread;          // {"strategy","printed_ok_min"}
};

// The load-bearing numbers from the plan's section-5 table (FDM default). Order is
// significant: KNOWN_PROCESSES = this order.
const std::vector<Profile>& profiles() {
    static const std::vector<Profile> k = {
        {"fdm", "PLA", "pla", 1.24,
         1.0, 0.8, 0.20, 0.40,
         {{"slip", 0.40}, {"location", 0.20}, {"press", 0.10}, {"free", 0.50}},
         {{"required", false}, {"min_deg", 0.0}},
         {{"max_deg", 45}, {"max_bridge_mm", 5}},
         {{"strategy", "heat_set_insert"}, {"printed_ok_min", "M5"}}},
        {"cnc_mill", "Aluminium 6061-T6", "6061", 2.70,
         0.5, 0.5, 0.0, 0.05,
         {{"slip", 0.05}, {"location", 0.02}, {"press", -0.02}, {"free", 0.10}},
         {{"required", false}, {"min_deg", 0.0}},
         {{"max_deg", nullptr}, {"max_bridge_mm", nullptr}},
         {{"strategy", "tapped"}, {"printed_ok_min", nullptr}}},
        {"lathe", "Aluminium 6061-T6", "6061", 2.70,
         0.5, 0.5, 0.0, 0.05,
         {{"slip", 0.05}, {"location", 0.02}, {"press", -0.02}, {"free", 0.10}},
         {{"required", false}, {"min_deg", 0.0}},
         {{"max_deg", nullptr}, {"max_bridge_mm", nullptr}},
         {{"strategy", "single_point_or_tapped"}, {"printed_ok_min", nullptr}}},
        {"sheet_metal", "Steel (mild)", "steel", 7.85,
         1.0, 1.0, 0.0, 0.10,
         {{"slip", nullptr}, {"location", nullptr}, {"press", nullptr}, {"free", nullptr}},
         {{"required", false}, {"min_deg", 0.0}},
         {{"max_deg", nullptr}, {"max_bridge_mm", nullptr}},
         {{"strategy", "clinch_or_weld_nut"}, {"printed_ok_min", nullptr}}},
        {"laser", "Acrylic", "acrylic", 1.18,
         1.0, 0.5, 0.0, 0.10,
         {{"slip", nullptr}, {"location", nullptr}, {"press", nullptr}, {"free", nullptr}},
         {{"required", false}, {"min_deg", 0.0}},
         {{"max_deg", nullptr}, {"max_bridge_mm", nullptr}},
         {{"strategy", "none"}, {"printed_ok_min", nullptr}}},
        {"injection", "ABS", "abs", 1.04,
         1.0, 0.5, 0.0, 0.10,
         {{"slip", 0.10}, {"location", 0.05}, {"press", 0.02}, {"free", 0.20}},
         {{"required", true}, {"min_deg", 1.0}},
         {{"max_deg", nullptr}, {"max_bridge_mm", nullptr}},
         {{"strategy", "molded"}, {"printed_ok_min", nullptr}}},
        // Manufacturing Network, Sand Casting Designers & Buyers Guide,
        // process norms: light-alloy wall 3 mm and draft 1 degree.
        {"cast", "Light alloy (sand casting)", "", nullptr,
         3.0, nullptr, nullptr, nullptr,
         {{"slip", nullptr}, {"location", nullptr}, {"press", nullptr}, {"free", nullptr}},
         {{"required", true}, {"min_deg", 1.0}},
         {{"max_deg", nullptr}, {"max_bridge_mm", nullptr}},
         {{"strategy", nullptr}, {"printed_ok_min", nullptr}}},
        // Formlabs Form 4 Design Guide, Grey Resin, 50 micron layers.
        {"sla", "Grey resin", "", nullptr,
         0.2, nullptr, nullptr, nullptr,
         {{"slip", nullptr}, {"location", nullptr}, {"press", nullptr}, {"free", nullptr}},
         {{"required", false}, {"min_deg", nullptr}},
         {{"max_deg", nullptr}, {"max_bridge_mm", nullptr}},
         {{"strategy", nullptr}, {"printed_ok_min", nullptr}}},
        // Formlabs Fuse 1 design specifications, Nylon 12, vertical walls.
        {"sls", "Nylon 12", "", nullptr,
         0.6, nullptr, nullptr, nullptr,
         {{"slip", nullptr}, {"location", nullptr}, {"press", nullptr}, {"free", nullptr}},
         {{"required", false}, {"min_deg", nullptr}},
         {{"max_deg", nullptr}, {"max_bridge_mm", nullptr}},
         {{"strategy", nullptr}, {"printed_ok_min", nullptr}}},
        // Tempus 3D HP Multi Jet Fusion guide, unsupported wall.
        {"mjf", "Nylon", "", nullptr,
         0.5, nullptr, nullptr, nullptr,
         {{"slip", nullptr}, {"location", nullptr}, {"press", nullptr}, {"free", nullptr}},
         {{"required", false}, {"min_deg", nullptr}},
         {{"max_deg", nullptr}, {"max_bridge_mm", nullptr}},
         {{"strategy", nullptr}, {"printed_ok_min", nullptr}}},
    };
    return k;
}

const Profile* find_profile(const std::string& key) {
    for (const Profile& p : profiles())
        if (p.process == key) return &p;
    return nullptr;
}
// Each rule is installed with its published scope and provenance. Missing rules
// are deliberately absent rather than manufacturing a process-independent limit.
void add_rules(const std::string& process, json& params) {
    json rules = json::object();
    json sources = json::object();
    const auto add = [&](const char* key, json value, const char* source) {
        rules[key] = std::move(value);
        sources[key] = source;
    };
    if (process == "cnc_mill" || process == "lathe") {
        // machining_reference.json, drilled-hole-depth-capability, 5:1 row:
        // conventional twist-drill planning guidance, not a standard limit.
        add("hole_max_depth_to_dia_standard", 5.0, "machining_reference.json drilled-hole-depth-capability, 5:1 row: conventional twist-drill planning guidance, pilot and peck as needed; not a standard guarantee");
        // AIVON Hole Design Guidelines for CNC Machining, hole spacing and
        // edge distance: below 1.5 hole diameters risks breakout/burrs.
        // https://www.aivon.com/blog/cnc-design/hole-design-guidelines-for-cnc-machining/
        add("hole_to_edge_diameter_factor", 1.5,
            "AIVON Hole Design Guidelines for CNC Machining, hole spacing and edge distance: 1.5 hole diameters as a production planning screen; source does not specify datum, conservatively interpreted as edge ligament");
    }
    if (process == "cnc_mill") {
        // machining_reference.json, internal-corner-radius-cutter: D/2
        // geometric minimum; recommended 0.55D = 1.1 cutter radii.
        add("internal_corner_radius_tool_ratio_min", 1.0, "machining_reference.json internal-corner-radius-cutter: geometric minimum D/2, one cutter radius");
        add("internal_corner_radius_tool_ratio_recommended", 1.1, "machining_reference.json internal-corner-radius-cutter: recommended finish clearance starts at 0.55D, 1.1 cutter radii; planning guidance");
    }
    if (process == "fdm") {
        // Prusa Knowledge Base, Extrusion Multiplier Calibration: 0.4 mm
        // nozzle, 0.45 mm line width and 0.1-0.2 mm layers; Layers and
        // Perimeters: Prusa profiles use at least two perimeters.
        add("nozzle_mm", 0.4, "Prusa Extrusion Multiplier Calibration, standard 0.4 mm nozzle profiles, mm");
        add("line_width_mm", 0.45, "Prusa Extrusion Multiplier Calibration, 0.4 mm nozzle profiles: extrusion width, mm");
        add("layer_height_mm", 0.2, "Prusa Extrusion Multiplier Calibration, 0.4 mm nozzle profiles: example layer height, mm");
        add("perimeters", 2, "Prusa Layers and Perimeters, Perimeters: minimum outlines in original Prusa profiles");
        // Formlabs, Minimum Wall Thickness for 3D Printing, process table:
        // free-standing FDM vertical wire diameter.
        add("min_pin_mm", 3.0, "Formlabs Minimum Wall Thickness for 3D Printing, process table: FDM vertical wire diameter, mm");
        // MeshRA, Chamfer vs Fillet for 3D Printing: recommended starting
        // heuristic for an internal radius is the adjacent wall thickness.
        add("internal_corner_radius_wall_ratio", 1.0, "MeshRA Chamfer vs Fillet for 3D Printing: recommended starting heuristic, internal radius >= adjacent wall thickness");
    } else if (process == "sheet_metal") {
        // Protolabs Sheet Metal Fabrication Design Guidelines, material
        // thickness range and common features (flange >= 4t, holes/slots >= t).
        add("min_thickness_mm", 0.609, "Protolabs Sheet Metal Fabrication Design Guidelines, material thickness range: minimum gauge, mm");
        add("max_thickness_mm", 6.35, "Protolabs Sheet Metal Fabrication Design Guidelines, material thickness range: maximum gauge, mm");
        add("min_flange_length_thickness_factor", 4.0, "Protolabs Sheet Metal Fabrication Design Guidelines, common features: flange length >= 4t");
        add("min_hole_dia_thickness_factor", 1.0, "Protolabs Sheet Metal Fabrication Design Guidelines, common features: hole diameter >= t");
        add("min_slot_width_thickness_factor", 1.0, "Protolabs Sheet Metal Fabrication Design Guidelines, common features: slot width >= t");
        // MJM Manufacturing Sheet Metal DFM Guide, sections 03-04:
        // hole edge to part edge >= 2t; hole to bend >= 2.5t + r;
        // bend relief width >= t.
        add("hole_to_bend_thickness_factor", 2.5, "MJM Sheet Metal DFM Guide, section 04: hole edge to bend >= 2.5t + r");
        add("hole_to_edge_thickness_factor", 2.0, "MJM Sheet Metal DFM Guide, section 04: hole edge to part edge >= 2t");
        add("relief_width_min_thickness_factor", 1.0, "MJM Sheet Metal DFM Guide, section 03: relief width >= t");
        // Protolabs, How and When to Add Bend Reliefs, 90-degree bends:
        // relief depth = material thickness + inside bend radius.
        add("relief_depth_over_bend_radius_extra", 0.0, "Protolabs How and When to Add Bend Reliefs, 90-degree bend: depth >= t + r + 0 mm");
        // Fabcon Sheet Metal DFM, Bend Radius Recommendations by Material:
        // 5052-H32 1t-1.5t and 6061-T6 3t across grain are starting points,
        // not a guarantee for grain direction, gauge or the actual press brake.
        // https://fabcon.com/articles/sheet-metal-fabrication/sheet-metal-design-dfm-guide/
        add("min_bend_radius_per_thickness",
            {{"aluminum_6061_t6", 3.0}, {"alu_6061", 3.0},
             {"aluminum_6061_t6_plate_unwelded_min240", 3.0}, {"5052-h32", 1.0}},
            "Fabcon Sheet Metal DFM, Bend Radius Recommendations by Material: 6061-T6 3t and 5052-H32 1t across grain starting points; confirm gauge, grain and tooling");
    } else if (process == "injection") {
        // Protolabs Injection Molding Wall Thickness Guidelines: ABS range
        // 0.045-0.140 in, boss/rib 40-60% of wall, draft 1 degree/inch.
        add("wall_min_mm", 1.143, "Protolabs Injection Molding Wall Thickness Guidelines, ABS chart: 0.045 in minimum wall, mm");
        add("wall_max_mm", 3.556, "Protolabs Injection Molding Wall Thickness Guidelines, ABS chart: 0.140 in maximum wall, mm");
        add("rib_thickness_ratio_max", 0.6, "Protolabs Injection Molding Wall Thickness Guidelines, ribs and gussets: maximum 60% of nominal wall");
        add("boss_wall_ratio_max", 0.6, "Protolabs Injection Molding Wall Thickness Guidelines, bosses: maximum 60% of surrounding wall");
        add("draft_min_deg_per_mm_depth", 1.0 / 25.4, "Protolabs Injection Molding Wall Thickness Guidelines: 1 degree per inch of cavity depth, deg/mm");
        // Evok Polymers, Injection Molding Wall Thickness, Maintain Uniform
        // Wall Thickness: amorphous ABS, PC, PS +/-25%; semi-crystalline
        // nylon, PP, PBT +/-15%. https://evokpoly.com/feeds/blog/injection-molding-wall-thickness
        add("wall_uniformity_deviation_by_material",
            {{"abs", 0.25}, {"polycarbonate", 0.25}, {"polystyrene", 0.25},
             {"nylon", 0.15}, {"polypropylene", 0.15}, {"pbt", 0.15}},
            "Evok Polymers Injection Molding Wall Thickness, Maintain Uniform Wall Thickness: +/-25% amorphous, +/-15% semi-crystalline; material-specific design guidance");
        // Same source, Use Gradual Transitions: minimum corner radius 0.5t.
        add("internal_corner_radius_wall_ratio", 0.5,
            "Evok Polymers Injection Molding Wall Thickness, Use Gradual Transitions: corner radius >= 0.5 wall thickness");
        // Protolabs Draft Angle Guidelines for Injection Molding: 1-2 degrees
        // works well in most situations; confirm against specific geometry.
        sources["draft.min_deg"] = "Protolabs Draft Angle Guidelines for Injection Molding: 1-2 degrees works well in most situations";
    } else if (process == "cast") {
        // Manufacturing Network, Sand Casting Designers & Buyers Guide,
        // process norms and Radii: light-alloy walls and internal radius/wall.
        add("wall_min_mm", 3.0, "Manufacturing Network Sand Casting Designers & Buyers Guide, process norms: light alloy minimum wall, mm");
        add("internal_corner_radius_wall_ratio", 1.0, "Manufacturing Network Sand Casting Designers & Buyers Guide, Radii: typical internal radius >= wall thickness");
        // Manufacturing Network Sand Casting Designers & Buyers Guide,
        // Draft Angles: 1 degree is possible, typical 2 degrees.
        sources["draft.min_deg"] = "Manufacturing Network Sand Casting Designers & Buyers Guide, Draft Angles: 1 degree is possible";
    } else if (process == "sla") {
        // Formlabs Form 4 Design Guide, Grey Resin at 50 microns, feature sections.
        add("min_wall_mm_supported", 0.2, "Formlabs Form 4 Design Guide, supported walls: Grey Resin minimum, mm");
        add("min_wall_mm_unsupported", 0.2, "Formlabs Form 4 Design Guide, unsupported walls: Grey Resin minimum, mm");
        add("min_pin_mm", 0.3, "Formlabs Form 4 Design Guide, vertical wire: minimum diameter for a 7 mm tall wire, mm");
        add("min_hole_mm", 0.5, "Formlabs Form 4 Design Guide, minimum hole diameter, mm");
        add("drain_hole_min_mm", 0.75, "Formlabs Form 4 Design Guide, minimum resin drain hole diameter, mm");
    } else if (process == "sls") {
        // Formlabs Fuse 1 Design Specifications, Nylon 12; conservative
        // vertical-wall guideline also applies to horizontal walls.
        add("min_wall_mm_supported", 0.6, "Formlabs Fuse 1 Design Specifications, supported vertical walls, Nylon 12, mm");
        add("min_wall_mm_unsupported", 0.6, "Formlabs Fuse 1 Design Specifications, unsupported vertical walls, Nylon 12, mm");
        add("min_pin_mm", 0.8, "Formlabs Fuse 1 Design Specifications, pin/wire diameter, Nylon 12, mm");
        add("min_hole_mm", 1.0, "Formlabs Fuse 1 Design Specifications, minimum hole diameter, Nylon 12, mm");
        add("drain_hole_min_mm", 3.5, "Formlabs Fuse 1 Design Specifications, minimum powder drain hole diameter, at least two holes, mm");
    } else if (process == "mjf") {
        // Tempus 3D, How to Design for HP Multi Jet Fusion, Design Essentials.
        add("min_wall_mm_supported", 0.4, "Tempus 3D HP Multi Jet Fusion Design Guide, supported wall thickness, mm");
        add("min_wall_mm_unsupported", 0.5, "Tempus 3D HP Multi Jet Fusion Design Guide, unsupported wall thickness, mm");
        add("min_pin_mm", 0.9, "Tempus 3D HP Multi Jet Fusion Design Guide, unsupported wire diameter, mm");
        add("drain_hole_min_mm", 4.0, "Tempus 3D HP Multi Jet Fusion Design Guide, single powder escape hole diameter, mm");
    }
    params["rules"] = std::move(rules);
    params["rule_sources"] = std::move(sources);
}


// ---------------------------------------------------------------- resolver data

// Explicit process keywords that short-circuit the resolver, checked as substrings
// in this order (more specific multi-word phrases first).
const std::vector<std::pair<std::string, std::string>>& explicit_keywords() {
    static const std::vector<std::pair<std::string, std::string>> k = {
        {"3d print", "fdm"}, {"3-d print", "fdm"}, {"3d-print", "fdm"},
        {"fdm", "fdm"}, {"fff", "fdm"}, {"printed", "fdm"}, {"print", "fdm"},
        {"injection mold", "injection"}, {"injection-mold", "injection"},
        {"injection", "injection"}, {"molded", "injection"}, {"moulded", "injection"},
        {"mold", "injection"}, {"mould", "injection"},
        {"sheet metal", "sheet_metal"}, {"sheet-metal", "sheet_metal"},
        {"laser cut", "laser"}, {"laser-cut", "laser"}, {"laser", "laser"},
        {"cnc", "cnc_mill"}, {"machine from", "cnc_mill"}, {"machined", "cnc_mill"},
        {"machine", "cnc_mill"}, {"mill", "cnc_mill"},
        {"turned", "lathe"}, {"turn ", "lathe"}, {"lathe", "lathe"},
    };
    return k;
}

const std::vector<std::string>& metal_words() {
    static const std::vector<std::string> k = {
        "steel", "stainless", "aluminum", "aluminium", "alu", "titanium", "brass",
        "bronze", "copper", "iron", "metal", "carbon steel", "mild steel", "6061",
    };
    return k;
}

const std::vector<std::string>& plastic_words() {
    static const std::vector<std::string> k = {
        "pla", "abs", "petg", "plastic", "nylon", "tpu", "resin", "polycarbonate", "pc",
    };
    return k;
}

struct Cue {
    std::string process;
    double weight;
    std::vector<std::string> patterns;
};

const std::vector<Cue>& cues() {
    static const std::vector<Cue> k = {
        {"sheet_metal", 1.0,
         {R"(\bbend(s|ing)?\b)", R"(\bflange(s)?\b)", R"(\bfold(ed|s)?\b)",
          R"(\bbent\b)", R"(\bgauge\b)",
          R"(\b(constant|uniform)[- ]thickness\b)",
          R"(\b\d+(\.\d+)?\s?mm\s+(?:\w+\s+){0,2}?(plate|sheet)\b)"}},
        {"sheet_metal", 0.5, {R"(\bbracket\b)", R"(\benclosure\b)", R"(\bchassis\b)"}},
        {"cnc_mill", 1.0,
         {R"(\btap(ped|ping)?\b)", R"(\bthreaded\b)", R"(\bthread(s)?\b)",
          R"(\btight[- ]toleranc)", R"(\bprecision\b)", R"(\breamed\b)"}},
        {"cnc_mill", 0.7,
         {R"(\bflat(ness)?\b)", R"(\bmust be flat\b)", R"(\bsurface finish\b)", R"(\bpocket\b)"}},
        {"lathe", 1.0,
         {R"(\bturn(ed|ing)?\b)", R"(\blathe\b)", R"(\bbody of revolution\b)",
          R"(\bspindle\b)", R"(\bshaft\b(?=.*\bround\b))"}},
        {"lathe", 0.6, {R"(\bround\s+(bar|stock|rod)\b)", R"(\bconcentric\b)"}},
        {"injection", 1.0, {R"(\bdraft\b)", R"(\bmolded\b)", R"(\bmoulded\b)", R"(\binjection\b)"}},
        {"injection", 0.8,
         {R"(\b(\d{4,})\s*(units|pieces|pcs|off|qty)\b)",
          R"(\bmass[- ]produc)", R"(\bhigh[- ]volume\b)"}},
        {"laser", 1.0, {R"(\bgasket\b)", R"(\blaser\b)", R"(\bkerf\b)"}},
        {"laser", 0.6,
         {R"(\bpanel\b)", R"(\bflat\s+(profile|cut)\b)", R"(\bcut\s+from\s+(sheet|acrylic)\b)"}},
    };
    return k;
}

// Insertion order of the Python cue_scores dict = first-appearance order in _CUES.
const std::vector<std::string>& cue_insertion_order() {
    static const std::vector<std::string> k = {
        "sheet_metal", "cnc_mill", "lathe", "injection", "laser",
    };
    return k;
}

// An insertion-ordered string->double map replicating Python dict semantics.
struct Scores {
    std::vector<std::pair<std::string, double>> items;  // insertion order
    void add(const std::string& k, double w) {
        for (auto& kv : items)
            if (kv.first == k) {
                kv.second += w;
                return;
            }
        items.emplace_back(k, w);
    }
    double get(const std::string& k) const {
        for (const auto& kv : items)
            if (kv.first == k) return kv.second;
        return 0.0;
    }
    bool has(const std::string& k) const {
        for (const auto& kv : items)
            if (kv.first == k) return true;
        return false;
    }
    // Python max(keys, key=scores.get): FIRST maximal in insertion order.
    std::string first_max() const {
        std::string best;
        double bestv = 0;
        bool first = true;
        for (const auto& kv : items)
            if (first || kv.second > bestv) {
                best = kv.first;
                bestv = kv.second;
                first = false;
            }
        return best;
    }
};

bool matches_any(const std::vector<std::string>& patterns, const std::string& text) {
    for (const std::string& p : patterns)
        if (std::regex_search(text, std::regex(p))) return true;
    return false;
}

// Python _spec_text: flatten a request string OR a normalized-intake dict.
std::string spec_text(const json& request_or_spec) {
    if (request_or_spec.is_object()) {
        std::string joined;
        for (const char* key : {"object", "request", "summary", "text"}) {
            const auto it = request_or_spec.find(key);
            if (it != request_or_spec.end() && it->is_string()) {
                if (!joined.empty()) joined += " ";
                joined += it->get<std::string>();
            }
        }
        for (const char* key : {"features", "constraints", "notes"}) {
            const auto it = request_or_spec.find(key);
            if (it != request_or_spec.end() && it->is_array())
                for (const json& x : *it) {
                    if (!joined.empty()) joined += " ";
                    joined += x.is_string() ? x.get<std::string>() : x.dump();
                }
        }
        return strip(lower(joined));
    }
    return norm(request_or_spec);
}

std::string explicit_process(const std::string& text) {
    // Check specific processes before generic "print" and "mold" substrings.
    static const std::vector<std::pair<std::regex, const char*>> specific = {
        {std::regex(R"(\b(sla|stereolithography)\b)"), "sla"},
        {std::regex(R"(\b(sls|selective laser sintering)\b)"), "sls"},
        {std::regex(R"(\b(mjf|multi[ -]jet fusion)\b)"), "mjf"},
        {std::regex(R"(\b(cast|casting|sand[ -]cast)\b)"), "cast"},
        {std::regex(R"(\bwater[ -]?jet\b)"), "waterjet"},
    };
    for (const auto& [pattern, process] : specific)
        if (std::regex_search(text, pattern)) return process;
    for (const auto& [kw, proc] : explicit_keywords())
        if (text.find(kw) != std::string::npos) return proc;
    return "";
}

bool word_in_text(const std::string& word, const std::string& text) {
    return std::regex_search(text, std::regex("\\b" + word + "\\b"));
}

bool material_is_metal(const json& material, const std::string& text) {
    const std::string m = norm(material);
    for (const std::string& w : metal_words())
        if (m.find(w) != std::string::npos) return true;
    for (const std::string& w : metal_words())
        if (word_in_text(w, text)) return true;
    return false;
}

bool material_is_plastic(const json& material, const std::string& text) {
    const std::string m = norm(material);
    for (const std::string& w : plastic_words())
        if (m.find(w) != std::string::npos) return true;
    for (const std::string& w : plastic_words())
        if (word_in_text(w, text)) return true;
    return false;
}

Scores score_cues(const std::string& text) {
    Scores scores;
    // Each cue group counts once; insertion order = first appearance in _CUES.
    for (const std::string& proc : cue_insertion_order()) {
        double total = 0.0;
        bool any = false;
        for (const Cue& c : cues())
            if (c.process == proc && matches_any(c.patterns, text)) {
                total += c.weight;
                any = true;
            }
        if (any) scores.add(proc, total);
    }
    return scores;
}

// Two distinct processes each carrying a STRONG cue (>= 1.0) -> the ambiguous pair.
// Sorted by (-score, cnc_mill first on ties), stable over insertion order.
std::pair<std::string, std::string> strong_tie(const Scores& scores) {
    std::vector<std::pair<std::string, double>> strong;
    for (const auto& kv : scores.items)
        if (kv.second >= 1.0) strong.push_back(kv);
    if (strong.size() < 2) return {"", ""};
    std::stable_sort(strong.begin(), strong.end(),
                     [](const auto& a, const auto& b) {
                         if (a.second != b.second) return a.second > b.second;
                         const int ra = a.first == "cnc_mill" ? 0 : 1;
                         const int rb = b.first == "cnc_mill" ? 0 : 1;
                         return ra < rb;
                     });
    return {strong[0].first, strong[1].first};
}

// Python _quantity_from.
struct Qty {
    bool valid = false;
    int value = 0;
};
Qty quantity_from(const std::string& text, const json& quantity) {
    if (!quantity.is_null()) {
        try {
            if (quantity.is_number())
                return {true, quantity.get<int>()};
            if (quantity.is_string())
                return {true, std::stoi(quantity.get<std::string>())};
            if (quantity.is_boolean())
                return {true, quantity.get<bool>() ? 1 : 0};
        } catch (...) {
        }
        return {};
    }
    std::smatch m;
    if (std::regex_search(text, m, std::regex(R"(\b(\d{2,})\s*(units|pieces|pcs|off|qty|quantity)\b)")))
        return {true, std::stoi(m[1].str())};
    return {};
}

// Python _PRETTY.get(proc, proc).
std::string pretty(const std::string& proc) {
    static const std::vector<std::pair<std::string, std::string>> k = {
        {"fdm", "FDM 3D print"},
        {"cnc_mill", "CNC from plate/billet"},
        {"sheet_metal", "sheet metal (laser+bend)"},
        {"injection", "injection mold"},
        {"lathe", "lathe (turned)"},
        {"laser", "laser cut"},
    };
    for (const auto& [id, label] : k)
        if (id == proc) return label;
    return proc;
}

json two_way_clarification(const std::string& a, const std::string& b) {
    const std::string pa = pretty(a);
    const std::string pb = pretty(b);
    return {
        {"ask", "Make this by " + pa + " or " + pb + "? [suggested: " + pa + "]"},
        {"suggested", a},
        {"options", {a, b}},
    };
}

json resolution(const std::string& chosen, double confidence, const json& runner_up,
                bool ambiguous, bool must_block, const json& clarification,
                const std::vector<std::string>& assumptions, const std::string& why) {
    return {
        {"chosen", chosen},
        {"confidence", pyround(confidence, 3)},
        {"runner_up", runner_up},
        {"ambiguous", ambiguous},
        {"must_block", must_block},
        {"clarification", clarification},
        {"assumptions", assumptions},
        {"why", why},
    };
}

// Python _runner_up_after(chosen, scores).
json runner_up_after(const std::string& chosen, const Scores& scores) {
    Scores others;
    for (const auto& kv : scores.items)
        if (kv.first != chosen) others.add(kv.first, kv.second);
    if (others.items.empty()) return json();
    return others.first_max();
}

}  // namespace

std::vector<std::string> known_processes() {
    std::vector<std::string> out;
    for (const Profile& p : profiles()) out.push_back(p.process);
    return out;
}

json get_profile(const std::string& process, const json* overrides) {
    const std::string key = strip(lower(process));
    const Profile* p = find_profile(key);
    if (!p)
        throw DfmError("Unknown process " + repr_s(process) +
                       ". Known processes: " + repr_list(known_processes()) + ".");
    json params = {
        {"min_wall_mm", p->min_wall_mm},
        {"min_feature_mm", p->min_feature_mm},
        {"hole_comp_mm", p->hole_comp_mm},
        {"tolerance_band_mm", p->tolerance_band_mm},
        {"fit_classes_mm", p->fit_classes_mm},
        {"draft", p->draft},
        {"overhang", p->overhang},
        {"thread", p->thread},
    };
    add_rules(key, params);
    if (overrides && overrides->is_object() && !overrides->empty()) {
        const auto hc = overrides->find("hole_comp");
        if (hc != overrides->end() && !hc->is_null() && hc->is_number())
            params["hole_comp_mm"] = hc->get<double>();
        const auto xy = overrides->find("xy_comp");
        if (xy != overrides->end() && !xy->is_null() && xy->is_number()) {
            const double v = xy->get<double>();
            params["xy_comp_mm"] = v;
            if (params["tolerance_band_mm"].is_number())
                params["tolerance_band_mm"] =
                    pyround(params["tolerance_band_mm"].get<double>() + v, 4);
        }
        std::vector<std::string> unknown;
        for (auto it = overrides->begin(); it != overrides->end(); ++it)
            if (it.key() != "xy_comp" && it.key() != "hole_comp") unknown.push_back(it.key());
        if (!unknown.empty()) {
            std::sort(unknown.begin(), unknown.end());
            json extra = json::object();
            for (const std::string& k : unknown) extra[k] = (*overrides)[k];
            params["_extra_overrides"] = std::move(extra);
        }
    }
    return {
        {"process", p->process},
        {"material",
         {{"name", p->material_name},
          {"density_g_cm3", p->density_fallback.is_number()
                                ? json(reg_density(p->density_lookup, p->density_fallback.get<double>()))
                                : json(nullptr)}}},
        {"params", std::move(params)},
    };
}

double fit_clearance(const std::string& process, const std::string& fit_class) {
    const std::string key = strip(lower(process));
    const Profile* p = find_profile(key);
    if (!p)
        throw DfmError("Unknown process " + repr_s(process) +
                       ". Known: " + repr_list(known_processes()) + ".");
    const std::string fc = strip(lower(fit_class));
    const auto it = p->fit_classes_mm.find(fc);
    if (it == p->fit_classes_mm.end()) {
        std::vector<std::string> known;
        for (auto fit = p->fit_classes_mm.begin(); fit != p->fit_classes_mm.end(); ++fit)
            known.push_back(fit.key());
        std::sort(known.begin(), known.end());
        throw DfmError("Unknown fit class " + repr_s(fit_class) +
                       ". Known: " + repr_list(known) + ".");
    }
    if (it->is_null()) {
        const std::string detail = (key == "sheet_metal" || key == "laser")
            ? " (it is a flat/sheet process; holes are cut to size)." : ".";
        throw DfmError("Process " + repr_s(key) + " has no diametral " + repr_s(fc) +
                       " fit class" + detail);
    }
    return it->get<double>();
}

json modeled_dim(double nominal_mm, const std::string& fit_class, const std::string& process,
                 bool is_hole) {
    const std::string key = strip(lower(process));
    const double clr = fit_clearance(key, fit_class);
    const Profile* p = find_profile(key);
    const double hole_comp = is_hole && p->hole_comp_mm.is_number()
                                 ? p->hole_comp_mm.get<double>() : 0.0;
    const double nominal = nominal_mm;
    const double modeled = nominal + clr + hole_comp;
    const std::string fc = strip(lower(fit_class));
    std::string bits = p->process + " " + fc + " clearance " + py_signed2(clr) + " mm";
    if (is_hole && hole_comp != 0.0)
        bits += ", + printed-hole comp " + py_fixed2(hole_comp) + " mm";
    return {
        {"modeled_mm", pyround(modeled, 4)},
        {"clearance_mm", clr},
        {"hole_comp_mm", hole_comp},
        {"rationale", py_g(nominal) + " mm nominal, " + bits},
    };
}

json resolve_process(const json& request_or_spec, const json& material,
                     const json& quantity) {
    const std::string text = spec_text(request_or_spec);
    const Qty qty = quantity_from(text, quantity);
    const bool has_explicit_qty = !quantity.is_null() || qty.valid;
    const bool is_metal = material_is_metal(material, text);
    const bool is_plastic = material_is_plastic(material, text);
    const bool has_explicit_material = !norm(material).empty() || is_metal || is_plastic;

    // ---- 1. explicit process keyword short-circuits everything -------------------
    const std::string explicit_proc = explicit_process(text);
    if (explicit_proc == "waterjet")
        throw DfmError("Unknown process 'waterjet'. Known processes: " +
                       repr_list(known_processes()) + ".");
    if (!explicit_proc.empty()) {
        if (explicit_proc == "fdm" && is_metal) {
            const json clar = {
                {"ask",
                 "You asked to 3D-print this, but named a metal material. FDM cannot "
                 "print metal. Switch to a metal process (CNC mill / sheet metal), or "
                 "keep FDM with a plastic?"},
                {"suggested", "cnc_mill"},
                {"options", {"cnc_mill", "sheet_metal", "fdm"}},
            };
            return resolution("fdm", 0.99, runner_up_after("fdm", Scores{}), true, true,
                              clar, {},
                              "explicit 3D-print cue but metal material is impossible for FDM");
        }
        return resolution(explicit_proc, 0.95, json(), false, false, json(), {},
                          "explicit '" + explicit_proc + "' process cue in the request");
    }

    // ---- 2. explicit metal material pins a non-FDM (metal) process ---------------
    const Scores cue_scores = score_cues(text);
    if (is_metal) {
        Scores metal_scores;
        for (const auto& kv : cue_scores.items)
            if (kv.first == "cnc_mill" || kv.first == "sheet_metal" || kv.first == "lathe")
                metal_scores.add(kv.first, kv.second);
        const auto [lead, other] = strong_tie(metal_scores);
        if (!lead.empty()) {
            return resolution(
                lead, 0.5, other, true, false,
                two_way_clarification(lead, other),
                {"metal material -> not FDM, but two strong metal cues compete"},
                "metal pins a metal process but '" + lead + "' and '" + other +
                    "' cues compete; asking which");
        }
        std::string chosen = "cnc_mill";
        json runner;
        if (!metal_scores.items.empty()) {
            chosen = metal_scores.first_max();
            runner = runner_up_after(chosen, metal_scores);
        }
        return resolution(
            chosen, 0.9, runner, false, false, json(),
            {"metal material -> " + chosen + " (FDM impossible)"},
            "explicit metal material pins a metal process");
    }

    // ---- 3. score cues against the FDM prior --------------------------------------
    const double fdm_prior = 0.8 + (is_plastic ? 0.4 : 0.0);
    Scores scores;
    scores.add("fdm", fdm_prior);
    for (const auto& kv : cue_scores.items) scores.add(kv.first, kv.second);
    if (qty.valid && qty.value >= 1000) scores.add("injection", 1.0);

    Scores non_fdm;
    for (const auto& kv : scores.items)
        if (kv.first != "fdm") non_fdm.add(kv.first, kv.second);

    // ranked: stable sort by score desc (ties keep insertion order).
    std::vector<std::pair<std::string, double>> ranked = scores.items;
    std::stable_sort(ranked.begin(), ranked.end(),
                     [](const auto& a, const auto& b) { return a.second > b.second; });
    const std::string leader = ranked[0].first;
    const double lead_score = ranked[0].second;
    const json runner_up = ranked.size() > 1 ? json(ranked[1].first) : json();

    // ---- 3a. two strong non-FDM cues compete (rule (b)) ---------------------------
    const auto [a, b] = strong_tie(non_fdm);
    if (!a.empty()) {
        return resolution(
            a, 0.5, b, true, false,
            two_way_clarification(a, b),
            {"process ambiguous: two strong non-FDM cues compete"},
            "two non-FDM cues compete (" + a + " vs " + b + "); asking which");
    }

    // ---- 3b. a non-FDM leader within margin of FDM, no anchor (rule (a)) ---------
    if (leader != "fdm") {
        const double fdm_score = scores.get("fdm");
        const bool within_margin = (lead_score - fdm_score) < 1.0001;
        if (within_margin && !has_explicit_material && !has_explicit_qty) {
            return resolution(
                leader, 0.5, "fdm", true, false,
                two_way_clarification(leader, "fdm"),
                {leader + " cue present but within margin of the FDM default"},
                "'" + leader +
                    "' cue scores within margin of the FDM default with no material/qty anchor");
        }
        const double conf = std::min(0.9, 0.6 + (lead_score - fdm_score) * 0.15);
        return resolution(
            leader, pyround(conf, 2), runner_up, false, false, json(),
            {leader + " selected over the FDM default by cue margin"},
            "'" + leader + "' cues outscore the FDM default");
    }

    // ---- 4. FDM wins on the prior (the common, no-ask path) -----------------------
    const double conf = is_plastic ? 0.9 : 0.86;
    const std::string why = is_plastic
                                ? "explicit plastic material with no non-FDM cues"
                                : "no non-FDM cues; FDM is the default prior";
    return resolution("fdm", conf,
                      runner_up.is_string() && runner_up.get<std::string>() != "fdm"
                          ? runner_up
                          : json(),
                      false, false, json(), {"process FDM/PLA assumed (default)"}, why);
}

std::string process_block_text(const json& profile) {
    const json params = profile.contains("params") ? profile["params"] : profile;
    const std::string process =
        profile.contains("process") && profile["process"].is_string()
            ? profile["process"].get<std::string>()
            : "fdm";
    std::string mat_name;
    if (profile.contains("material") && profile["material"].is_object() &&
        profile["material"].contains("name") && profile["material"]["name"].is_string())
        mat_name = profile["material"]["name"].get<std::string>();

    std::vector<std::string> lines;
    std::string head =
        "PROCESS BLOCK — manufacturing = " + upper(pretty(process));
    if (!mat_name.empty()) head += " / " + mat_name;
    if (profile.contains("assumed") && profile["assumed"].is_boolean() &&
        profile["assumed"].get<bool>())
        head += "  (assumed default — confirm if wrong)";
    lines.push_back(head);
    lines.push_back(
        "Commit dimensions that PRINT/MACHINE on the first try, not textbook anchors.");

    const auto num_or_null = [&params](const char* key, double& out) -> bool {
        const auto it = params.find(key);
        if (it == params.end() || it->is_null() || !it->is_number()) return false;
        out = it->get<double>();
        return true;
    };
    double mw, mf;
    if (num_or_null("min_wall_mm", mw)) {
        std::string wall = "  - Min wall " + py_g(mw) + " mm";
        if (num_or_null("min_feature_mm", mf))
            wall += "; smallest feature " + py_g(mf) + " mm";
        lines.push_back(wall + ". Walls thinner than this warp or fail to form.");
    }

    const json fits =
        params.contains("fit_classes_mm") && params["fit_classes_mm"].is_object()
            ? params["fit_classes_mm"]
            : json::object();
    std::string fit_bits;
    for (const char* name : {"slip", "location", "press", "free"}) {
        const auto it = fits.find(name);
        if (it != fits.end() && !it->is_null() && it->is_number()) {
            if (!fit_bits.empty()) fit_bits += ", ";
            fit_bits += std::string(name) + " " + py_signed2(it->get<double>());
        }
    }
    if (!fit_bits.empty()) {
        lines.push_back("  - Diametral fit clearance to ADD to the receptacle (mm): " +
                        fit_bits + ".");
        std::string slip;
        const auto it = fits.find("slip");
        if (it != fits.end() && !it->is_null() && it->is_number())
            slip = py_signed2(it->get<double>());
        lines.push_back("    e.g. a slip-fit bore for a Dx shaft is modeled Ø(x" + slip +
                        "); pick the class by how it must mate.");
    }

    double hc;
    if (num_or_null("hole_comp_mm", hc) && hc != 0.0)
        lines.push_back("  - Printed holes shrink: oversize every printed bore diameter by " +
                        py_g(hc) + " mm to land on nominal.");

    if (params.contains("thread") && params["thread"].is_object()) {
        const json& thread = params["thread"];
        if (thread.contains("strategy") && thread["strategy"].is_string() &&
            !thread["strategy"].get<std::string>().empty()) {
            std::string strat = thread["strategy"].get<std::string>();
            std::replace(strat.begin(), strat.end(), '_', ' ');
            std::string note;
            if (thread.contains("printed_ok_min") && thread["printed_ok_min"].is_string() &&
                !thread["printed_ok_min"].get<std::string>().empty())
                note = " (printed threads only ≥ " +
                       thread["printed_ok_min"].get<std::string>() + ")";
            lines.push_back("  - Threads: " + strat + note + ".");
        }
    }

    if (params.contains("draft") && params["draft"].is_object()) {
        const json& draft = params["draft"];
        if (draft.contains("required") && draft["required"].is_boolean() &&
            draft["required"].get<bool>()) {
            double min_deg = 0.0;
            if (draft.contains("min_deg") && draft["min_deg"].is_number())
                min_deg = draft["min_deg"].get<double>();
            lines.push_back("  - Draft REQUIRED ≥ " + py_g(min_deg) +
                            "° on every face along the pull, or it won't release.");
        }
    }

    if (params.contains("overhang") && params["overhang"].is_object()) {
        const json& over = params["overhang"];
        if (over.contains("max_deg") && over["max_deg"].is_number()) {
            std::string b;
            if (over.contains("max_bridge_mm") && over["max_bridge_mm"].is_number()) {
                const double bridge = over["max_bridge_mm"].get<double>();
                if (bridge != 0.0)
                    b = "; bridges over " + py_g(bridge) + " mm need support";
            }
            lines.push_back("  - Overhangs steeper than " +
                            py_g(over["max_deg"].get<double>()) +
                            "° from vertical need support or a chamfer" + b + ".");
        }
    }

    std::string out;
    for (size_t i = 0; i < lines.size(); ++i) {
        if (i) out += "\n";
        out += lines[i];
    }
    return out;
}

bool call(const std::string& method, const json& params, json& result, std::string& err) {
    err.clear();
    const auto str_arg = [&params](const char* key) -> std::string {
        const auto it = params.find(key);
        return it != params.end() && it->is_string() ? it->get<std::string>() : "";
    };
    try {
        json value;
        if (method == "dfm_known_processes")
            value = known_processes();
        else if (method == "dfm_get_profile") {
            const auto ov = params.find("overrides");
            value = get_profile(str_arg("process"),
                                ov != params.end() && ov->is_object() ? &*ov : nullptr);
        } else if (method == "dfm_resolve_process") {
            const auto req = params.find("request");
            const json request = req != params.end() ? *req : json("");
            const auto mat = params.find("material");
            const auto qty = params.find("quantity");
            value = resolve_process(request, mat != params.end() ? *mat : json(),
                                    qty != params.end() ? *qty : json());
        } else if (method == "dfm_fit_clearance")
            value = fit_clearance(str_arg("process"), str_arg("fit_class"));
        else if (method == "dfm_modeled_dim") {
            double nominal = 0.0;
            const auto n = params.find("nominal_mm");
            if (n != params.end() && n->is_number()) nominal = n->get<double>();
            const auto ih = params.find("is_hole");
            const bool is_hole = ih != params.end() && ih->is_boolean() && ih->get<bool>();
            value = modeled_dim(nominal, str_arg("fit_class"), str_arg("process"), is_hole);
        } else if (method == "dfm_process_block_text") {
            const auto pr = params.find("profile");
            value = process_block_text(pr != params.end() && pr->is_object() ? *pr
                                                                             : json::object());
        } else
            return false;
        result = {{"ok", true}, {"result", std::move(value)}};
        return true;
    } catch (const DfmError& e) {
        result = {{"ok", false}, {"error", e.what()}};
        return true;
    }
}

}  // namespace dfm
}  // namespace chudware
