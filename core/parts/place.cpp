// core/parts/place.cpp — standard-part placement and schema routing.
// See place.h for the routing table and the PlacementResult contract.
#include "parts/place.h"

#include <cmath>
#include <filesystem>
#include <random>
#include <set>
#include <string>
#include <vector>

#include "ecad/ops.h"
#include "parts/fitting_geometry.h"
#include "hydraulic/schema.h"
#include "piping/pyutil.h"
#include "piping/schema.h"

namespace chudware::parts {
namespace {

using pyutil::ojson;

// ── envelopes (mirrors place.py _ok / _err) ─────────────────────────────────

json ok_env(const std::string& domain, const json& doc_id, const json& entity_id,
            const json& detail) {
    return json{{"ok", true},         {"domain", domain}, {"doc_id", doc_id},
                {"entity_id", entity_id}, {"editable", true}, {"detail", detail},
                {"reason", nullptr},  {"hint", nullptr}};
}

json err_env(const std::string& domain, const std::string& reason, const std::string& hint = "",
             const json& detail = json::object()) {
    return json{{"ok", false},          {"domain", domain}, {"doc_id", nullptr},
                {"entity_id", nullptr}, {"editable", false}, {"detail", detail},
                {"reason", reason},     {"hint", hint}};
}

// ── Python scalar helpers ───────────────────────────────────────────────────

// Python float(v) for a JSON scalar.
double to_f(const json& v, double dflt = 0.0) {
    if (v.is_number()) return v.get<double>();
    if (v.is_boolean()) return v.get<bool>() ? 1.0 : 0.0;
    if (v.is_string()) {
        try { return std::stod(v.get<std::string>()); } catch (...) { return dflt; }
    }
    return dflt;
}

// Python truthiness.
bool truthy(const json& v) {
    if (v.is_null()) return false;
    if (v.is_boolean()) return v.get<bool>();
    if (v.is_number()) return v.get<double>() != 0.0;
    if (v.is_string()) return !v.get<std::string>().empty();
    return !v.empty();
}

// obj.get(key) — json null when absent.
json get(const json& obj, const char* key) {
    if (obj.is_object() && obj.contains(key)) return obj[key];
    return json();
}

std::string get_str(const json& obj, const char* key) {
    const json v = get(obj, key);
    return v.is_string() ? v.get<std::string>() : std::string();
}

// uuid.uuid4().hex[:12] — 12 random hex chars (structural element ids).
std::string uuid_hex12() {
    static std::mt19937_64 rng{std::random_device{}()};
    static const char* kHex = "0123456789abcdef";
    std::string out(12, '0');
    for (char& c : out) c = kHex[rng() & 0xF];
    return out;
}

json to_ojson(const json& j) { return ojson::parse(j.dump()); }
json from_ojson(const ojson& o) { return json::parse(o.dump()); }

const char* kReverseEngineerHint =
    "call reverse_engineer(source=doc_id) for parametric feature "
    "recovery where the geometry supports it";

// ── placers (one per geometry_ref kind) ─────────────────────────────────────

// core_family: parts_make RPC → new editable mech part doc.
json place_core_family(const json& gref, const json& params, const PlaceSeams& seams) {
    const std::string domain = "core_family";
    const std::string family = get_str(gref, "family");
    const std::string size = get_str(gref, "size");
    if (family.empty() || size.empty())
        return err_env(domain, "geometry_ref missing 'family' or 'size'",
                       "Fetch the PartRecord via part_get to confirm geometry_ref fields.");
    try {
        if (!seams.parts_make) throw std::runtime_error("native core seam unavailable");
        json call_params = {{"family", family}, {"size", size}};
        if (gref.contains("length_mm") && !gref["length_mm"].is_null())
            call_params["length_mm"] = gref["length_mm"];
        if (params.is_object())
            for (auto it = params.begin(); it != params.end(); ++it)
                if (it.key() != "family" && it.key() != "size")
                    call_params[it.key()] = it.value();
        const json r = seams.parts_make(call_params);
        if (!r.value("ok", false))
            return err_env(domain, r.value("error", std::string("parts_make failed")),
                           "Verify the family/size pair with list_standard_parts.");
        const json doc_id = get(r, "doc_id");
        json detail = {{"family", family},
                       {"size", size},
                       {"name", get(r, "name")},
                       {"bbox", get(r, "bbox")},
                       {"datum", get(r, "datum")}};
        if (r.contains("meta")) detail["meta"] = r["meta"];
        return ok_env(domain, doc_id, doc_id, detail);
    } catch (const std::exception& e) {
        return err_env(domain, std::string("parts_make RPC failed: ") + e.what(),
                       "Ensure the native core is available (CHUDWARE_LIBCHUDWARE).");
    }
}

// import_file: import a local CAD file (browser-acquired STEP/IGES) as a new
// editable doc via the import_file RPC (same primitive import_part uses).
json place_import_file(const json& gref, const json& /*doc*/, const json& params,
                       const PlaceSeams& seams) {
    const std::string domain = "import_file";
    for (const char* key : {"pos", "rotation"}) {
        if (!params.is_object() || !params.contains(key)) continue;
        const json& v = params[key];
        if (!v.is_array() || v.size() != 3)
            return err_env(domain, std::string(key) + " must be an array of three finite numbers.");
        for (const auto& x : v)
            if (!x.is_number() || !std::isfinite(x.get<double>()))
                return err_env(domain, std::string(key) + " must be an array of three finite numbers.");
    }
    const bool has_pose = params.is_object() &&
                          (params.contains("pos") || params.contains("rotation"));
    if (has_pose && !seams.pose_import)
        return err_env(domain, "Imported model pose seam unavailable.");
    const std::string path = get_str(gref, "path");
    if (path.empty())
        return err_env(domain, "geometry_ref missing 'path'",
                       "Fetch the PartRecord via part_get to confirm geometry_ref fields.");
    try {
        if (!seams.import_file) throw std::runtime_error("native core seam unavailable");
        const bool as_solid = params.is_object() && truthy(get(params, "as_solid"));
        const json r = seams.import_file(path, as_solid);
        const json doc_id = get(r, "doc_id");
        if (!truthy(doc_id))
            return err_env(domain, "import_file RPC returned no doc_id",
                           "The core may have rejected the file format.");
        json detail = {{"imported_from", path},
                       {"body_count", get(r, "body_count")},
                       {"hint", kReverseEngineerHint}};
        if (has_pose) {
            const json posed = seams.pose_import(doc_id.get<std::string>(), r, params);
            if (posed.contains("bbox")) detail["bbox"] = posed["bbox"];
            if (posed.contains("datum")) detail["datum"] = posed["datum"];
            if (posed.contains("body_count")) detail["body_count"] = posed["body_count"];
        }
        return ok_env(domain, doc_id, doc_id, detail);
    } catch (const std::exception& e) {
        return err_env(domain, std::string("import_file RPC failed: ") + e.what(),
                       "Ensure the native core is available (CHUDWARE_LIBCHUDWARE).");
    }
}

// generate_model: build_model seam → import produced STEP as an editable doc
// (mirrors _place_generate_model delegating to _place_import_file).
json place_generate_model(const json& gref, const json& doc, const json& params,
                          const PlaceSeams& seams) {
    const std::string domain = "generate_model";
    const json match = get(gref, "match");
    if (!truthy(match))
        return err_env(domain, "geometry_ref missing 'match' dict",
                       "Fetch the PartRecord via part_get to confirm geometry_ref fields.");
    try {
        if (!seams.build_model) throw std::runtime_error("build_model seam unavailable");
        const std::string path = seams.build_model(match);
        if (path.empty())
            return err_env(
                domain, "generate_model returned None (no geometry available)",
                "Core may be absent or the match source is 'kicad' (no mech model).");
        const json import_result =
            place_import_file(json{{"kind", "import_file"}, {"path", path}}, doc, params, seams);
        if (!import_result.value("ok", false)) {
            json detail = json::object();
            detail["step_path"] = path;
            const json id = get(import_result, "detail");
            if (id.is_object())
                for (auto it = id.begin(); it != id.end(); ++it) detail[it.key()] = it.value();
            return err_env(domain,
                           get(import_result, "reason").is_string()
                               ? get(import_result, "reason").get<std::string>()
                               : "import of generated model failed",
                           get_str(import_result, "hint"), detail);
        }
        json detail = get(import_result, "detail");
        if (!detail.is_object()) detail = json::object();
        detail["step_path"] = path;
        if (!detail.contains("hint")) detail["hint"] = kReverseEngineerHint;
        const json doc_id = get(import_result, "doc_id");
        const json entity = truthy(get(import_result, "entity_id"))
                                ? get(import_result, "entity_id")
                                : doc_id;
        return ok_env(domain, doc_id, entity, detail);
    } catch (const std::exception& e) {
        return err_env(domain, std::string("generate_model failed: ") + e.what(),
                       "Ensure engine_oracle.spec_sheet_matcher is importable.");
    }
}

// symbol_footprint: ecad sch_add_symbol op into a schematic doc (editable
// doc-schema op) via chudware::ecad::apply_ops.
json place_symbol_footprint(const json& gref, const json& doc_context, const json& params) {
    const std::string domain = "symbol_footprint";
    const std::string libid = get_str(gref, "libid");
    if (libid.empty())
        return err_env(domain, "geometry_ref missing 'libid'",
                       "Fetch the PartRecord via part_get to confirm geometry_ref fields.");

    // Resolve the doc from context or params (mirrors the oracle's precedence).
    static const std::set<std::string> kSchSchemas = {"aicad.sch/1", "aicad.ecad/1",
                                                      "aicad.pcb/1"};
    json doc;
    std::string doc_id;
    if (doc_context.is_object() &&
        kSchSchemas.count(doc_context.value("schema", std::string()))) {
        doc = doc_context;
    } else if (params.is_object() && truthy(get(params, "doc_id"))) {
        doc_id = get(params, "doc_id").get<std::string>();
    } else {
        return err_env(
            domain,
            "No schematic doc provided. Pass doc=<opened ecad doc dict> or "
            "params={'doc_id': '<id>'} from ecad_new_schematic.",
            "Open a schematic with ecad_new_schematic (schema=aicad.sch/1), "
            "then call part_place with the doc.");
    }

    try {
        // sch_add_symbol requires pos as [x, y] (ecad_ops.md §1 / _require_xy).
        json pos = json::array({0.0, 0.0});
        const json raw_pos = params.is_object() ? get(params, "pos") : json::array({0, 0});
        if (raw_pos.is_object())
            pos = json::array({to_f(get(raw_pos, "x")), to_f(get(raw_pos, "y"))});
        else if (raw_pos.is_array() && raw_pos.size() >= 2)
            pos = json::array({to_f(raw_pos[0]), to_f(raw_pos[1])});
        json op = {{"op", "sch_add_symbol"},
                   {"libid", libid},
                   {"pos", pos},
                   {"angle", params.is_object() ? to_f(get(params, "angle")) : 0.0}};
        if (truthy(get(gref, "footprint"))) op["footprint"] = gref["footprint"];
        if (params.is_object() && truthy(get(params, "value"))) op["value"] = params["value"];
        if (params.is_object() && truthy(get(params, "ref"))) op["ref"] = params["ref"];

        if (doc.is_null()) {
            // doc_id string only — return the structured op for the caller to apply.
            return ok_env(domain, doc_id, nullptr,
                          {{"op", op},
                           {"note", "Apply this op via ecad_place_symbol or apply_ops(doc, [op])."}});
        }

        // apply_ops returns {"id_map": {...}, "warnings": [...], "doc": <updated doc>}.
        const json applied = ecad::apply_ops(doc, json::array({op}));
        const json id_map = applied.value("id_map", json::object());
        const json entity_id = get(id_map, "$0");
        const json updated_doc = applied.contains("doc") ? applied["doc"] : doc;
        // Verify editable: the symbol must appear in doc["symbols"].
        const json symbols = updated_doc.value("symbols", json::array());
        bool found = false;
        if (entity_id.is_string()) {
            for (const json& s : symbols)
                if (get(s, "id") == entity_id) { found = true; break; }
        } else {
            found = !symbols.empty();
        }
        if (!found)
            return err_env(domain,
                           "Symbol was not added to doc.symbols — editability check failed",
                           "Verify the doc schema is aicad.sch/1 schematic.");
        json ref = get(op, "ref");
        for (const json& s : symbols)
            if (get(s, "id") == entity_id) { ref = get(s, "ref"); break; }
        json out = ok_env(domain, get(updated_doc, "id"), entity_id,
                          {{"libid", libid}, {"ref", ref}, {"pos", pos}});
        // The oracle mutates the caller's doc in place; across the RPC boundary
        // the updated document is handed back explicitly (additive key).
        out["doc"] = updated_doc;
        return out;
    } catch (const std::exception& e) {
        return err_env(domain, std::string("sch_add_symbol failed: ") + e.what(),
                       "Ensure engine_oracle.ecad.ops is importable.");
    }
}

// A section can be added to a structural model or built as a standalone part.
json place_section_profile(const json& gref, const json& doc_context, const json& params,
                           const PlaceSeams& seams) {
    const std::string domain = "section_profile";
    const std::string standard = get_str(gref, "standard");
    const std::string designation = get_str(gref, "designation");
    if (designation.empty())
        return err_env(domain, "geometry_ref missing 'designation'",
                       "Fetch the PartRecord via part_get to confirm geometry_ref fields.");
    if (doc_context.is_null()) {
        if (standard.empty())
            return err_env(domain, "geometry_ref missing 'standard'",
                           "Fetch the PartRecord via part_get to confirm the section standard.");
        if (params.contains("length_mm") &&
            (!params["length_mm"].is_number() ||
             !std::isfinite(params["length_mm"].get<double>()) ||
             params["length_mm"].get<double>() <= 0.0))
            return err_env(domain, "length_mm must be a positive finite number.");
        json make_params = json::object();
        for (const char* key : {"length_mm", "pos", "rotation"})
            if (params.contains(key)) make_params[key] = params[key];
        json out = place_core_family({{"family", "structural_section"},
                                      {"size", standard + ":" + designation}}, make_params, seams);
        out["domain"] = domain;
        return out;
    }
    if (!doc_context.is_object() ||
        doc_context.value("schema", std::string()) != "aicad.structure/1")
        return err_env(domain,
                       "Expected a structural doc with schema='aicad.structure/1'.",
                       "Omit doc to create a standalone mechanical part.");
    try {
        const json p = params.is_object() ? params : json::object();
        const std::string element_id =
            truthy(get(p, "id")) ? get(p, "id").get<std::string>() : uuid_hex12();
        const std::string kind = p.value("kind", std::string("beam"));
        const double length_mm = p.contains("length_mm") ? to_f(p["length_mm"]) : 1000.0;
        const std::string material = p.value("material", std::string(standard == "NDS" ? "lumber" : "steel"));
        std::string section_name = standard.empty() ? designation : standard + ":" + designation;
        const std::string shape = get_str(p, "shape");
        if (shape == "round_bar" || shape == "hex_bar" || shape == "flat_bar" ||
            shape == "plate" || shape == "sheet" || shape == "round_tube") {
            if (get_str(p, "material").empty())
                throw std::invalid_argument("raw stock requires an explicit material");
            auto dimension = [&](const char* key) {
                const json v = get(p, key);
                if (!v.is_number() || !std::isfinite(v.get<double>()) || v.get<double>() <= 0)
                    throw std::invalid_argument(std::string("raw stock requires positive ") + key);
                return v.get<double>();
            };
            auto number = [](double v) { return json(v).dump(); };
            if (shape == "round_bar") section_name = "ROUND " + number(dimension("diameter_mm"));
            else if (shape == "hex_bar") section_name = "HEX " + number(dimension("across_flats_mm"));
            else if (shape == "round_tube") {
                const double od = dimension("od_mm"), wall = dimension("wall_mm");
                if (2 * wall >= od) throw std::invalid_argument("raw stock wall must be less than half the outside diameter");
                section_name = "CHS " + number(od) + "x" + number(wall);
            } else section_name = "RECT " + number(dimension("width_mm")) + "x" +
                                       number(dimension("thickness_mm"));
        }
        static const std::set<std::string> kKinds = {"beam", "column", "post", "joist"};
        const std::string element_type = kKinds.count(kind) ? kind : "beam";
        // StructuralElement.to_dict() (engine_oracle/structural/elements.py),
        // identity 4x4 row-major transform, key order preserved.
        ojson element = ojson::object();
        element["element_type"] = element_type;
        element["id"] = element_id;
        element["section_name"] = section_name;
        element["material"] = material;
        element["length_mm"] = length_mm;
        element["transform"] = {1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0,
                                0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0};
        ojson odoc = to_ojson(doc_context);
        if (!odoc.contains("elements") || !odoc["elements"].is_array())
            odoc["elements"] = ojson::array();
        odoc["elements"].push_back(element);
        json out = ok_env(domain, get(doc_context, "id"), element_id,
                          {{"kind", kind},
                           {"section_name", section_name},
                           {"length_mm", length_mm},
                           {"material", material}});
        out["doc"] = from_ojson(odoc);
        return out;
    } catch (const std::exception& e) {
        return err_env(domain, std::string("add structural element failed: ") + e.what(),
                       "Ensure engine_oracle.structural.elements is importable.");
    }
}

// A fitting can be added to a piping model or built as a standalone part.
json place_fitting_id(const json& gref, const json& doc_context, const json& params,
                      const PlaceSeams& seams) {
    const std::string domain = "fitting_id";
    const std::string fitting_id_val = get_str(gref, "fitting_id");
    if (fitting_id_val.empty())
        return err_env(domain, "geometry_ref missing 'fitting_id'",
                       "Fetch the PartRecord via part_get to confirm geometry_ref fields.");
    try {
        const json p = params.is_object() ? params : json::object();
        const json identity = fitting_catalog_identity(fitting_id_val);
        const std::string ft_type = identity.at("type").get<std::string>();
        const double nps = identity.at("nps").get<double>();
        for (const char* key : {"type", "nps", "nps2", "flange_class"}) {
            if (p.contains(key) && !p[key].is_null() &&
                (!identity.contains(key) || p[key] != identity[key]))
                return err_env(domain, std::string(key) + " conflicts with fitting catalog identity " +
                                       fitting_id_val + ".");
        }
        if (doc_context.is_null()) {
            json make_params = json::object();
            for (const char* key : {"pos", "rotation"})
                if (p.contains(key)) make_params[key] = p[key];
            if (p.contains("schedule"))
                make_params["options"] = {{"schedule", p["schedule"]}};
            json out = place_core_family({{"family", "piping_fitting"}, {"size", fitting_id_val}},
                                         make_params, seams);
            out["domain"] = domain;
            if (out.value("ok", false)) out["detail"]["catalog_id"] = fitting_id_val;
            return out;
        }
        const std::string schema = doc_context.is_object()
                                       ? doc_context.value("schema", std::string())
                                       : std::string();
        if (schema.rfind("aicad.piping", 0) != 0)
            return err_env(domain, "Expected a piping doc with schema='aicad.piping/1'.",
                           "Omit doc to create a standalone mechanical part.");
        ojson odoc = to_ojson(doc_context);
        const std::string ft_id = truthy(get(p, "id")) ? get(p, "id").get<std::string>()
                                                       : piping::next_id(odoc, "ft");
        ojson origin = ojson::array({0.0, 0.0, 0.0});
        if (p.contains("origin") && p["origin"].is_array()) origin = to_ojson(p["origin"]);
        ojson fitting = piping::new_fitting(ft_id, ft_type, nps, origin);
        fitting["catalog_ref"] = fitting_id_val;
        if (identity.contains("nps2")) fitting["nps2"] = to_ojson(identity["nps2"]);
        if (identity.contains("flange_class"))
            fitting["params"]["flange_class"] = to_ojson(identity["flange_class"]);
        for (const auto& key : piping::fitting_param_keys(ft_type))
            if (p.contains(key) && !p[key].is_null()) fitting["params"][key] = to_ojson(p[key]);
        for (const char* k : {"standard", "schedule", "material_spec"})
            if (truthy(get(p, k))) fitting[k] = to_ojson(p[k]);
        if (!odoc.contains("fittings") || !odoc["fittings"].is_array())
            odoc["fittings"] = ojson::array();
        odoc["fittings"].push_back(fitting);
        json out = ok_env(domain, get(doc_context, "id"), ft_id,
                          {{"fitting_type", ft_type},
                           {"nps", nps},
                           {"catalog_id", fitting_id_val}});
        out["doc"] = from_ojson(odoc);
        return out;
    } catch (const std::exception& e) {
        return err_env(domain, std::string("cannot place fitting: ") + e.what(),
                       "Check the catalog fitting type, NPS and class or reducer size.");
    }
}

// fmu_component: append a component to an aicad.hydraulic/1 doc (editable
// schema op).
json place_fmu_component(const json& gref, const json& doc_context, const json& params) {
    const std::string domain = "fmu_component";
    const std::string component_id_val = get_str(gref, "component_id");
    if (component_id_val.empty())
        return err_env(domain, "geometry_ref missing 'component_id'",
                       "Fetch the PartRecord via part_get to confirm geometry_ref fields.");
    const std::string schema = doc_context.is_object()
                                   ? doc_context.value("schema", std::string())
                                   : std::string();
    if (schema.rfind("aicad.hydraulic", 0) != 0)
        return err_env(domain,
                       "No hydraulic doc provided. Pass a doc with schema='aicad.hydraulic/1'.",
                       "Create one with hydraulic tools, then call part_place with that doc.");
    try {
        const json p = params.is_object() ? params : json::object();
        ojson odoc = to_ojson(doc_context);
        const std::string comp_id = truthy(get(p, "id")) ? get(p, "id").get<std::string>()
                                                         : hydraulic::next_id(odoc, "c");
        // Catalog identity is authoritative; a type override must agree.
        const std::string comp_type = component_id_val.substr(0, component_id_val.find(':'));
        if (!hydraulic::is_component_type(comp_type))
            return err_env(domain, "Unknown hydraulic component type in catalog id: " +
                                   component_id_val + ".");
        for (const char* key : {"type", "component_type"}) {
            if (p.contains(key) && !p[key].is_null() && p[key] != comp_type)
                return err_env(domain, "Hydraulic component type conflicts with catalog id: " +
                                       component_id_val + ".");
        }
        const double x = p.contains("x") ? to_f(p["x"]) : 0.0;
        const double y = p.contains("y") ? to_f(p["y"]) : 0.0;
        const std::string name = p.value("name", std::string());
        ojson component = hydraulic::new_component(comp_id, comp_type, name, x, y);
        // Propagate any extra params (pressure_bar, displacement_cc_rev, …) —
        // top-level assignment, mirroring the oracle's component[k] = v.
        for (const char* k :
             {"displacement_cc_rev", "max_pressure_bar", "max_flow_l_min", "efficiency_vol",
              "efficiency_mech", "bore_mm", "stroke_mm"})
            if (p.contains(k) && !p[k].is_null()) component[k] = to_ojson(p[k]);
        if (!odoc.contains("components") || !odoc["components"].is_array())
            odoc["components"] = ojson::array();
        odoc["components"].push_back(component);
        json out = ok_env(domain, get(doc_context, "id"), comp_id,
                          {{"comp_type", comp_type}, {"catalog_id", component_id_val}});
        out["doc"] = from_ojson(odoc);
        return out;
    } catch (const std::exception& e) {
        return err_env(domain, std::string("add hydraulic component failed: ") + e.what(),
                       "Ensure engine_oracle.hydraulic.schema is importable.");
    }
}

// sorted(place._PLACERS) — rendered as a Python list repr for the hint.
const char* kSupportedKinds =
    "['core_family', 'fitting_id', 'fmu_component', 'generate_model', 'import_file', "
    "'section_profile', 'symbol_footprint']";

json route(const std::string& kind, const json& gref, const json& doc, const json& params,
           const PlaceSeams& seams, bool& known) {
    known = true;
    if (kind == "core_family") return place_core_family(gref, params, seams);
    if (kind == "generate_model") return place_generate_model(gref, doc, params, seams);
    if (kind == "symbol_footprint") return place_symbol_footprint(gref, doc, params);
    if (kind == "section_profile") return place_section_profile(gref, doc, params, seams);
    if (kind == "fitting_id") return place_fitting_id(gref, doc, params, seams);
    if (kind == "fmu_component") return place_fmu_component(gref, doc, params);
    if (kind == "import_file") return place_import_file(gref, doc, params, seams);
    known = false;
    return json();
}

// place.place(part_id, doc=…, params=…) — see place.py docstring.
json part_place(const json& params, const PlaceSeams& seams) {
    const std::string part_id = get_str(params, "part_id");
    if (part_id.empty())
        return err_env("unknown", "part_place: missing 'part_id'",
                       "Use part_search to discover valid part_ids.");
    const json doc = get(params, "doc");
    const json explicit_params = get(params, "params");

    // 1. Retrieve the PartRecord (pre-fetched `record` wins; else the seam).
    json record = get(params, "record");
    if (!record.is_object()) {
        try {
            if (!seams.get_record)
                throw std::runtime_error("parts_hub seam unavailable");
            record = seams.get_record(part_id);
        } catch (const std::exception& e) {
            return err_env("unknown", std::string("parts_hub.get failed: ") + e.what(),
                           "Check that engine_oracle/parts_hub is importable.");
        }
    }
    if (!record.is_object())
        return err_env("unknown",
                       "part_id not found: " + pyutil::py_repr_str(part_id),
                       "Use part_search to discover valid part_ids.");

    // 1b. Acquire-FAILURE envelope (login_required / terms_confirmation_required /
    // distributor_only / …) — an actionable degradation, surfaced directly.
    const json ok_flag = get(record, "ok");
    if (ok_flag.is_boolean() && !ok_flag.get<bool>()) {
        const std::string reason = record.value("reason", std::string("unavailable"));
        const std::string site = record.value("site", std::string());
        std::string link = get_str(record, "source_url");
        if (link.empty()) link = get_str(record, "url");
        json detail = json::object();
        for (const char* k : {"ask_user", "distributors_url", "source_url", "url", "site",
                              "title"})
            if (record.contains(k) && !record[k].is_null()) detail[k] = record[k];
        std::string hint;
        if (reason == "login_required" && !site.empty()) {
            hint = "Sign in first: run vendor_login('" + site +
                   "') to open a browser window, complete the login once, then retry "
                   "part_place";
            if (!link.empty()) hint += ". Part page: " + link;
            hint += ".";
        } else if (reason == "terms_confirmation_required") {
            hint = !link.empty()
                       ? "Accept the download terms shown at " + link +
                             ", then retry part_place."
                       : record.value("hint", std::string("Terms acceptance required."));
        } else if (reason == "distributor_only") {
            const std::string du = get_str(record, "distributors_url");
            hint = site + " sells only through distributors — no downloadable model. ";
            if (!du.empty()) hint += "Find a distributor: " + du + ". ";
            if (!link.empty()) hint += "Reference: " + link + ".";
            while (!hint.empty() && hint.back() == ' ') hint.pop_back();
        } else {
            hint = get_str(record, "hint");
            if (hint.empty()) hint = "Could not acquire a model (" + reason + ").";
        }
        return err_env(record.value("domain", std::string("catalog")),
                       "Cannot place " + pyutil::py_repr_str(part_id) + ": " + reason + ".",
                       hint, detail);
    }

    // 2. Extract and validate geometry_ref.
    const json gref = get(record, "geometry_ref");
    if (!gref.is_object())
        return err_env(record.value("domain", std::string("unknown")),
                       "PartRecord " + pyutil::py_repr_str(part_id) +
                           " has no geometry_ref — cannot place.",
                       "This part has no known instantiation recipe.");
    const std::string kind = get_str(gref, "kind");
    json place_params = get(record, "params").is_object() ? record["params"] : json::object();
    if (explicit_params.is_object())
        for (auto it = explicit_params.begin(); it != explicit_params.end(); ++it)
            place_params[it.key()] = it.value();
    const bool mechanical = kind == "core_family" || kind == "generate_model" ||
                            kind == "import_file" ||
                            ((kind == "section_profile" || kind == "fitting_id") && doc.is_null());
    if (mechanical) {
        if (!doc.is_null())
            return err_env(kind, "Existing target documents are not supported for mechanical part placement.");
        for (const char* key : {"doc_id", "document_id", "target_doc", "target_doc_id",
                                "target_document", "target_document_id", "assembly_id"})
            if ((params.contains(key) && !params[key].is_null()) ||
                (explicit_params.is_object() && explicit_params.contains(key) &&
                 !explicit_params[key].is_null()))
                return err_env(kind, "Existing target documents are not supported for mechanical part placement.");
        for (const char* key : {"pos", "rotation"}) {
            if (!place_params.contains(key)) continue;
            const json& v = place_params[key];
            if (!v.is_array() || v.size() != 3)
                return err_env(kind, std::string(key) + " must be an array of three finite numbers.");
            for (const auto& x : v)
                if (!x.is_number() || !std::isfinite(x.get<double>()))
                    return err_env(kind, std::string(key) + " must be an array of three finite numbers.");
        }
    }
    bool known = false;
    json result = route(kind, gref, doc, place_params, seams, known);
    if (!known)
        return err_env(record.value("domain", std::string("unknown")),
                       "Unknown geometry_ref kind " + pyutil::py_repr_str(kind) + ".",
                       std::string("Supported kinds: ") + kSupportedKinds);
    if (mechanical && result.value("ok", false)) {
        if (!seams.set_catalog_meta)
            return err_env(kind, "Catalog metadata persistence seam unavailable.");
        try {
            json metadata = {{"part_record", record}};
            if (kind == "core_family" || kind == "section_profile" || kind == "fitting_id") {
                metadata["geometry_quality"] = "nominal_generated_envelope";
                metadata["omissions"] = json::array({"certified dimensions, tolerances, material and mechanical properties"});
                if (get_str(gref, "family") == "shcs") {
                    metadata["omissions"].push_back("threads");
                    metadata["omissions"].push_back("socket broach point and corner radii");
                }
                result["detail"]["geometry_quality"] = metadata["geometry_quality"];
                result["detail"]["omissions"] = metadata["omissions"];
                if (result["detail"].contains("meta"))
                    metadata["generated"] = result["detail"]["meta"];
            }
            seams.set_catalog_meta(result["doc_id"].get<std::string>(), metadata);
        } catch (const std::exception& e) {
            return err_env(kind, std::string("Catalog metadata persistence failed: ") + e.what());
        }
    }

    // 4. Invariant: a successful placement MUST be editable (defensive guard).
    if (result.value("ok", false) && !result.value("editable", false)) {
        result["ok"] = false;
        result["reason"] =
            "Internal: placement succeeded but editability flag is False — "
            "DoD violation. File a bug.";
    }
    return result;
}

}  // namespace

bool place_call(const std::string& method, const json& params, json& result, std::string& err,
                const PlaceSeams& seams) {
    (void)err;  // reserved for dispatch-level failures; none today
    const json p = params.is_object() ? params : json::object();
    if (method == "part_place") {
        result = part_place(p, seams);
        return true;
    }
    if (method == "part_import_file") {
        // The import_artifact MCP tool's direct _place_import_file call:
        // gref={"kind": "import_file", "path": path}, no doc, {as_solid}.
        const std::string path = get_str(p, "path");
        result = place_import_file(json{{"kind", "import_file"}, {"path", path}}, json(), p,
                                   seams);
        return true;
    }
    return false;
}

}  // namespace chudware::parts
