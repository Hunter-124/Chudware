// core/sourcing/specmatch.cpp — deterministic spec-sheet matching. Port of
// engine_oracle/spec_sheet_matcher.py: part-number normalization, the clean-room
// BOLTS/ISO lookup (tables embedded in bolts_data.h), the parametric templates,
// the GSL text preview, and the register/orchestrator lanes. NOT ported (reported
// as remainders): the KiCad symbol-library fallback (needs the deferred .kicad_sym
// parser) and the LLM datasheet-extraction lane (needs the agent provider stack —
// available only via the extract_dims seam).
#include "sourcing/sourcing.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <regex>
#include <sstream>

#include "sourcing/bolts_data.h"

namespace fs = std::filesystem;

namespace aicad {
namespace sourcing {

namespace {

// ---- small helpers ---------------------------------------------------------------------------

std::string trim(const std::string& s) {
    const auto b = s.find_first_not_of(" \t\r\n");
    const auto e = s.find_last_not_of(" \t\r\n");
    return b == std::string::npos ? std::string() : s.substr(b, e - b + 1);
}

std::string upper(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return (char)std::toupper(c); });
    return s;
}

std::string py_repr(const std::string& s) { return "'" + s + "'"; }

// Python f"{x:g}" (== C %.6g for finite doubles).
std::string fmt_g(double v) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.6g", v);
    return std::string(buf);
}

// Python re.sub with IGNORECASE on ASCII designations. `re_flags_icase` throughout.
std::string re_sub(const std::string& pattern, const std::string& repl, const std::string& s) {
    return std::regex_replace(s, std::regex(pattern, std::regex_constants::icase), repl);
}

bool re_search(const std::string& pattern, const std::string& s, std::smatch* m = nullptr) {
    const std::regex re(pattern, std::regex_constants::icase);
    if (m) return std::regex_search(s, *m, re);
    return std::regex_search(s, re);
}

// std::regex works on bytes; fold U+00D7 (×, 0xC3 0x97) to ASCII 'x' so the metric
// designation class [-x×*] behaves like Python's single-char class.
std::string fold_multiplication_sign(const std::string& s) {
    std::string out;
    for (size_t i = 0; i < s.size(); ++i) {
        if (i + 1 < s.size() && (unsigned char)s[i] == 0xC3 && (unsigned char)s[i + 1] == 0x97) {
            out += 'x';
            ++i;
        } else {
            out += s[i];
        }
    }
    return out;
}

const char* kMetricRe =
    "\\bM(\\d+(?:\\.\\d+)?)(?:[-x*](\\d+(?:\\.\\d+)?))?(?:[-x*](\\d+(?:\\.\\d+)?))?\\b";
const char* kBearingSealRe = "[-\\s]?(?:2RS1?|RS1?|2Z|ZZ|Z|DDU|LLU|LU|LB|2RU|C3|C4)+$";

// ---- part-number normalization ---------------------------------------------------------------
// Vendor/packaging suffixes, longest/most-specific first (engine_oracle order).
const char* kSuffixRe =
    "(?:-\\d{1,2}-ND|-DKR-ND|-CT-ND|-TR-ND|-ND|-DKR|-CT|-TR|-TB|-BULK|-REEL|-TAPE|-PBFREE|-EA)$";

// ---- BOLTS table access (engine/bolts_loader.py) ----------------------------------------------

std::string norm_size_key(const std::string& size) {
    std::string s = upper(trim(size));
    s.erase(std::remove(s.begin(), s.end(), ' '), s.end());
    return s;
}

// Standard.get: exact key, else case/format-tolerant compare. Null json when absent.
json std_get(const BoltStandard& std_, const std::string& size) {
    if (std_.sizes.contains(size)) return std_.sizes[size];
    const std::string want = norm_size_key(size);
    for (auto it = std_.sizes.begin(); it != std_.sizes.end(); ++it)
        if (norm_size_key(it.key()) == want) return it.value();
    return json();
}

std::string norm_designation(const std::string& name) {
    std::string s = upper(trim(name));
    std::replace(s.begin(), s.end(), '-', ' ');
    std::replace(s.begin(), s.end(), '_', ' ');
    return s;
}

const BoltStandard* standard_for_family(const std::string& family) {
    for (const BoltStandard& s : bolts_tables())
        if (s.family == family) return &s;
    return nullptr;
}

json build_match(const BoltStandard& std_, const std::string& size, const json& length_mm,
                 const json& params, const std::string& designation) {
    std::string catalog_id = std_.id + " " + size;
    if (length_mm.is_number() && length_mm.get<double>() != 0.0)
        catalog_id += "x" + fmt_g(length_mm.get<double>());
    json out;
    out["source"] = "bolts";
    out["standard"] = std_.id;
    out["standard_name"] = std_.name;
    out["family"] = std_.family;
    out["size"] = size;
    out["length_mm"] = length_mm;
    out["needs_length"] = std_.needs_length;
    out["params"] = params;
    out["catalog_id"] = catalog_id;
    out["designation"] = designation;
    return out;
}

json match_bearing_against(const std::string& mpn, const BoltStandard& std_) {
    std::string core = re_sub(kBearingSealRe, "", mpn);
    core = trim(core);
    core = re_sub("[-\\s]+$", "", core);
    core = upper(core);
    const json params = std_get(std_, core);
    if (params.is_null()) return json();
    return build_match(std_, core, json(), params, mpn);
}

json match_bearing(const std::string& mpn) {
    for (const BoltStandard& s : bolts_tables()) {
        if (s.kind != "bearing") continue;
        const json hit = match_bearing_against(mpn, s);
        if (!hit.is_null()) return hit;
    }
    return json();
}

// Family hints in a free-text designation -> catalog family key (oracle order).
const std::pair<const char*, const char*> kFamilyHints[] = {
    {"\\b(?:hex\\s*nut|nut)\\b", "hex_nut"},
    {"\\b(?:flat\\s*washer|washer)\\b", "washer_flat"},
    {"\\b(?:button\\s*head|bhcs)\\b", "bhcs"},
    {"\\b(?:flat\\s*head|fhcs|countersunk|csk)\\b", "fhcs"},
    {"\\b(?:hex\\s*bolt|hex\\s*head|hhcs)\\b", "hex_bolt"},
    {"\\b(?:socket\\s*head|shcs|cap\\s*screw)\\b", "shcs"},
};
const char* kDefaultFastenerFamily = "shcs";

// Parse "M3" / "M3x8" / "M8x1.25x20": a single trailing number is the LENGTH; the
// pitch only appears when both are given. Returns false when no M-thread token.
bool parse_metric(const std::string& mpn, std::string& thread, json& length_mm) {
    std::smatch m;
    if (!re_search(kMetricRe, fold_multiplication_sign(mpn), &m)) return false;
    thread = "M" + m[1].str();
    const std::string pitch = m[2].str();
    std::string length = m[3].str();
    if (length.empty() && !pitch.empty()) length = pitch;
    length_mm = length.empty() ? json() : json(std::stod(length));
    return true;
}

json match_metric_fastener(const std::string& mpn) {
    std::string thread;
    json length_mm;
    if (!parse_metric(mpn, thread, length_mm)) return json();

    std::string family = kDefaultFastenerFamily;
    for (const auto& hint : kFamilyHints)
        if (re_search(hint.first, mpn)) {
            family = hint.second;
            break;
        }

    const BoltStandard* std_ = standard_for_family(family);
    if (!std_) return json();
    const json params = std_get(*std_, thread);
    if (params.is_null()) return json();
    return build_match(*std_, thread, length_mm, params, mpn);
}

json match_explicit_standard(const std::string& mpn) {
    const std::string norm = norm_designation(mpn);
    // designation_index in table order: id then aliases (Python dict insertion order).
    for (const BoltStandard& s : bolts_tables()) {
        std::vector<std::string> names;
        names.push_back(s.id);
        names.insert(names.end(), s.aliases.begin(), s.aliases.end());
        for (const std::string& name : names) {
            const std::string key = norm_designation(name);
            if (key.empty() || norm.find(key) == std::string::npos) continue;
            // A standard was named; find the size token in the remainder.
            if (s.kind == "fastener") {
                std::string thread;
                json length_mm;
                if (parse_metric(mpn, thread, length_mm)) {
                    const json params = std_get(s, thread);
                    if (!params.is_null()) return build_match(s, thread, length_mm, params, mpn);
                }
            }
            if (s.kind == "bearing") return match_bearing_against(mpn, s);
        }
    }
    return json();
}

// ---- templates (spec_sheet_matcher.py §6) ------------------------------------------------------

struct SpecTemplate {
    std::string name;
    std::vector<std::string> required;
    std::vector<std::string> optional;
    std::string description;
};

const std::vector<SpecTemplate>& templates() {
    static const std::vector<SpecTemplate> t = {
        {"capacitor_radial", {"body_diameter_mm", "body_length_mm"},
         {"lead_spacing_mm", "lead_diameter_mm", "capacitance_uf", "voltage_v", "tolerance_pct"},
         "Radial (can) electrolytic capacitor body"},
        {"capacitor_axial", {"body_diameter_mm", "body_length_mm"},
         {"lead_diameter_mm", "capacitance_uf", "voltage_v", "tolerance_pct"},
         "Axial-lead capacitor body"},
        {"cylinder", {"diameter_mm", "height_mm"}, {}, "Generic cylindrical body"},
        {"box", {"length_mm", "width_mm", "height_mm"}, {}, "Generic rectangular body"},
    };
    return t;
}

const SpecTemplate* find_template(const std::string& name) {
    for (const SpecTemplate& t : templates())
        if (t.name == name) return &t;
    return nullptr;
}

std::string known_templates_repr() {
    // sorted(TEMPLATES) as a Python list repr.
    std::vector<std::string> names;
    for (const SpecTemplate& t : templates()) names.push_back(t.name);
    std::sort(names.begin(), names.end());
    std::string out = "[";
    for (size_t i = 0; i < names.size(); ++i) out += (i ? ", " : "") + py_repr(names[i]);
    return out + "]";
}

void require_template(const std::string& name) {
    if (!find_template(name))
        throw ProviderError("unknown template " + py_repr(name) +
                            "; known: " + known_templates_repr());
}

// Python: float coercion tolerating "6.3 mm" / "470uF" strings; null when uncoercible.
json coerce_num(const json& v) {
    if (v.is_number()) return json(v.get<double>());
    if (v.is_boolean()) return json();
    if (v.is_string()) {
        const std::string s = v.get<std::string>();
        std::smatch m;
        if (std::regex_search(s, m, std::regex("[-+]?\\d*\\.?\\d+"))) {
            try { return json(std::stod(m.str())); } catch (...) { return json(); }
        }
    }
    return json();
}

json param_cell(double v) { return {{"value", v}, {"expr", nullptr}}; }

json cylinder_ops(double radius_mm, double height_mm, const std::string& name) {
    json feat;
    feat["type"] = "cylinder";
    feat["name"] = name;
    feat["params"] = {{"radius", param_cell(radius_mm)}, {"height", param_cell(height_mm)}};
    json op;
    op["op"] = "add_feature";
    op["feature"] = feat;
    return json::array({op});
}

json box_ops(double length_mm, double width_mm, double height_mm, const std::string& name) {
    json feat;
    feat["type"] = "box";
    feat["name"] = name;
    feat["params"] = {{"length", param_cell(length_mm)},
                      {"width", param_cell(width_mm)},
                      {"height", param_cell(height_mm)}};
    json op;
    op["op"] = "add_feature";
    op["feature"] = feat;
    return json::array({op});
}

double dim_value(const json& dims, const std::string& key) {
    const json v = dims.value(key, json());
    if (!v.is_number()) throw ProviderError("missing required dim " + py_repr(key));
    return v.get<double>();
}

}  // namespace

// ---- normalization / matching ------------------------------------------------------------------

std::string normalize_mpn(const std::string& pn) {
    std::string s = trim(pn);
    // Peel packaging notation in parentheses/brackets, e.g. "GRM188(Cut Tape)".
    s = trim(re_sub("[\\(\\[][^)\\]]*[\\)\\]]\\s*$", "", s));
    std::string prev;
    while (prev != s && !s.empty()) {
        prev = s;
        s = trim(re_sub(kSuffixRe, "", s));
    }
    return s.empty() ? trim(pn) : s;
}

json match_bolts(const std::string& mpn) {
    const std::string norm = normalize_mpn(mpn);
    if (norm.empty()) return json();
    const json hit = match_explicit_standard(norm);
    if (!hit.is_null()) return hit;
    const json brg = match_bearing(norm);
    if (!brg.is_null()) return brg;
    return match_metric_fastener(norm);
}

json match_part_number(const std::string& mpn, bool generate, const Seams& seams) {
    const std::string normalized = normalize_mpn(mpn);
    const json match = match_bolts(mpn);  // KiCad lane not ported (needs .kicad_sym parser)
    if (match.is_null()) {
        return {{"matched", false},
                {"source", nullptr},
                {"mpn", mpn},
                {"normalized", normalized},
                {"params", nullptr},
                {"model_path", nullptr}};
    }
    json model_path;
    if (generate && match.contains("family") && match.value("source", "") == "bolts" &&
        seams.build_model) {
        const std::string p = seams.build_model(match);
        if (!p.empty()) model_path = json(p);
    }
    json out;
    out["matched"] = true;
    out["source"] = match.value("source", json());
    out["mpn"] = mpn;
    out["normalized"] = normalized;
    out["catalog_id"] = match.value("catalog_id", json());
    out["params"] = match.value("params", json());
    out["model_path"] = model_path;
    for (const char* key : {"standard", "standard_name", "family", "size", "length_mm",
                            "needs_length"})
        if (match.contains(key)) out[key] = match[key];
    return out;
}

// ---- templates / preview / register ------------------------------------------------------------

json list_spec_templates() {
    json out = json::array();
    for (const SpecTemplate& t : templates())
        out.push_back({{"name", t.name},
                       {"required", t.required},
                       {"optional", t.optional},
                       {"description", t.description}});
    return out;
}

std::string gsl_preview(const json& dims, const std::string& tmpl, const std::string& mpn) {
    const SpecTemplate* t = find_template(tmpl);
    const std::string head = tmpl + (mpn.empty() ? "" : "  [" + mpn + "]");
    std::vector<std::string> lines;
    lines.push_back("spec-sheet part: " + head);
    lines.push_back("template: " + (t ? t->description : tmpl));
    std::vector<std::string> keys;
    if (t) {
        keys = t->required;
        keys.insert(keys.end(), t->optional.begin(), t->optional.end());
    } else if (dims.is_object()) {
        for (auto it = dims.begin(); it != dims.end(); ++it) keys.push_back(it.key());
        std::sort(keys.begin(), keys.end());
    }
    for (const std::string& key : keys) {
        if (!dims.is_object() || !dims.contains(key)) continue;
        const json num = coerce_num(dims[key]);
        if (num.is_null()) continue;
        const std::string unit = key.size() >= 3 && key.compare(key.size() - 3, 3, "_mm") == 0
                                     ? "mm"
                                     : "";
        std::string line = "  " + key + " = " + fmt_g(num.get<double>()) +
                           (unit.empty() ? "" : " " + unit);
        // Python .rstrip() — trailing whitespace only, the two-space indent stays.
        const auto e = line.find_last_not_of(" \t\r\n");
        lines.push_back(e == std::string::npos ? std::string() : line.substr(0, e + 1));
    }
    std::string out;
    for (size_t i = 0; i < lines.size(); ++i) out += (i ? "\n" : "") + lines[i];
    return out;
}

json template_build_ops(const std::string& tmpl, const json& dims) {
    require_template(tmpl);
    if (tmpl == "capacitor_radial" || tmpl == "capacitor_axial")
        return cylinder_ops(dim_value(dims, "body_diameter_mm") / 2.0,
                            dim_value(dims, "body_length_mm"), "CapBody");
    if (tmpl == "cylinder")
        return cylinder_ops(dim_value(dims, "diameter_mm") / 2.0,
                            dim_value(dims, "height_mm"), "Body");
    return box_ops(dim_value(dims, "length_mm"), dim_value(dims, "width_mm"),
                   dim_value(dims, "height_mm"), "Body");
}

json register_from_spec_sheet(const json& dims, const std::string& tmpl,
                              const std::string& mpn_in, const std::string& description,
                              const Seams& seams) {
    const SpecTemplate* t = find_template(tmpl);
    require_template(tmpl);
    const std::string mpn = trim(mpn_in);
    if (mpn.empty())
        throw ProviderError("mpn is required to register a spec-sheet part");
    std::vector<std::string> missing;
    for (const std::string& k : t->required)
        if (!dims.is_object() || !dims.contains(k)) missing.push_back(k);
    if (!missing.empty()) {
        std::string repr = "[";
        for (size_t i = 0; i < missing.size(); ++i)
            repr += (i ? ", " : "") + py_repr(missing[i]);
        repr += "]";
        throw ProviderError("cannot build " + py_repr(tmpl) + ": missing required dims " + repr +
                            " (spec-sheet parts are never built from guessed dimensions)");
    }

    // numeric dims: template keys only, coerced (Python: float(v) after _coerce_num).
    json numeric = json::object();
    if (dims.is_object())
        for (auto it = dims.begin(); it != dims.end(); ++it) {
            const bool is_key =
                std::find(t->required.begin(), t->required.end(), it.key()) != t->required.end() ||
                std::find(t->optional.begin(), t->optional.end(), it.key()) != t->optional.end();
            if (!is_key) continue;
            const json num = coerce_num(it.value());
            if (!num.is_null()) numeric[it.key()] = num;
        }
    const std::string preview = gsl_preview(numeric, tmpl, mpn);

    std::string model_path;
    if (seams.build_spec_model) model_path = seams.build_spec_model(tmpl, numeric);

    json out;
    out["registered"] = false;
    out["source"] = "spec_sheet";
    out["mpn"] = mpn;
    out["template"] = tmpl;
    out["params"] = numeric;
    out["model_path"] = model_path.empty() ? json() : json(model_path);
    out["preview"] = preview;
    out["vendor_part"] = nullptr;
    if (model_path.empty()) {
        out["reason"] = "no editable model produced (native core unavailable offline)";
        return out;
    }

    // Register the built model as a source="spec_sheet" VendorPart (vendor registry).
    json specs = numeric;
    specs["template"] = tmpl;
    json rec;
    try {
        rec = register_local_cad("spec_sheet", mpn, model_path,
                                 description.empty() ? preview.substr(0, preview.find('\n'))
                                                     : description,
                                 "", specs, "spec_sheet");
    } catch (const std::exception& e) {
        out["reason"] = std::string("vendor registration failed: ") + e.what();
        return out;
    }
    out["registered"] = true;
    out["vendor_part"] = rec;
    return out;
}

json spec_sheet_match(const std::string& text_or_url, const std::string& mpn,
                      const std::string& tmpl, const json* dims, const Seams& seams) {
    const SpecTemplate* t = find_template(tmpl);
    require_template(tmpl);

    // Extraction: pre-extracted dims, the extract_dims seam, or the contract "unknown".
    json extracted_dims = json::object();
    std::string extracted_mpn, extracted_description;
    std::vector<std::string> missing;
    std::string degrade_reason;
    if (dims && dims->is_object()) {
        for (auto it = dims->begin(); it != dims->end(); ++it) {
            const json num = coerce_num(it.value());
            if (!num.is_null()) extracted_dims[it.key()] = num;
        }
        // mpn/description may ride along in a pre-extracted params object.
        if (dims->contains("mpn") && (*dims)["mpn"].is_string())
            extracted_mpn = (*dims)["mpn"].get<std::string>();
        if (dims->contains("description") && (*dims)["description"].is_string())
            extracted_description = (*dims)["description"].get<std::string>();
    } else if (seams.extract_dims) {
        const json ext = seams.extract_dims(text_or_url, tmpl);
        if (ext.is_object()) {
            const json d = ext.value("dims", json::object());
            if (d.is_object()) extracted_dims = d;
            if (ext.contains("mpn") && ext["mpn"].is_string())
                extracted_mpn = ext["mpn"].get<std::string>();
            if (ext.contains("description") && ext["description"].is_string())
                extracted_description = ext["description"].get<std::string>();
        }
    } else {
        degrade_reason =
            "datasheet dimension extraction (the LLM lane) is not available in the C++ "
            "core; pass pre-extracted 'dims' in params or route via the Python worker";
    }
    // Only template keys survive (Python: ai_extract_dims keeps tmpl.keys only).
    json kept = json::object();
    if (extracted_dims.is_object())
        for (auto it = extracted_dims.begin(); it != extracted_dims.end(); ++it) {
            const bool is_key = std::find(t->required.begin(), t->required.end(), it.key()) !=
                                    t->required.end() ||
                                std::find(t->optional.begin(), t->optional.end(), it.key()) !=
                                    t->optional.end();
            if (is_key) kept[it.key()] = it.value();
        }
    for (const std::string& k : t->required)
        if (!kept.contains(k)) missing.push_back(k);

    const std::string resolved_mpn =
        !trim(mpn).empty() ? trim(mpn) : trim(extracted_mpn);

    json base;
    base["matched"] = false;
    base["source"] = "spec_sheet";
    base["mpn"] = resolved_mpn.empty() ? json() : json(resolved_mpn);
    base["template"] = tmpl;
    base["params"] = kept;
    base["missing_required"] = missing;
    base["model_path"] = nullptr;
    base["preview"] = gsl_preview(kept, tmpl, resolved_mpn);
    base["registered"] = false;
    base["vendor_part"] = nullptr;
    if (!missing.empty() || resolved_mpn.empty()) {
        if (resolved_mpn.empty())
            base["reason"] = "no manufacturer part number supplied or extracted";
        else if (!degrade_reason.empty())
            base["reason"] = degrade_reason;
        return base;
    }

    const json reg = register_from_spec_sheet(kept, tmpl, resolved_mpn,
                                              extracted_description, seams);
    base["matched"] = true;
    base["model_path"] = reg.value("model_path", json());
    base["preview"] = reg.value("preview", std::string());
    base["registered"] = reg.value("registered", false);
    base["vendor_part"] = reg.value("vendor_part", json());
    if (reg.contains("reason")) base["reason"] = reg["reason"];
    return base;
}

}  // namespace sourcing
}  // namespace aicad
