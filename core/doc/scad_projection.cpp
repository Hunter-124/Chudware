#include "doc/scad_projection.h"

#include "util/expr.h"

#include <cmath>
#include <iomanip>
#include <locale>
#include <map>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

namespace chudware::doc::scad {
namespace {
using json = nlohmann::json;

constexpr std::size_t kMaxSourceBytes = 4 * 1024 * 1024;
constexpr std::size_t kMaxDocumentBytes = 4 * 1024 * 1024;
constexpr std::size_t kMaxDocumentNodes = 50000;
constexpr std::size_t kMaxFeatures = 2048;

// Check the existing JSON without serializing or copying it. In particular, metadata
// passed to restore_native must face the same bounds before regeneration.
void check_document_bounds(const json& doc) {
    std::vector<std::pair<const json*, std::size_t>> pending{{&doc, 0}};
    std::size_t nodes = 0, bytes = 0;
    auto charge = [&](std::size_t n) {
        if (n > kMaxDocumentBytes - bytes)
            throw std::length_error("native projection document size limit exceeded");
        bytes += n;
    };
    while (!pending.empty()) {
        const json* v = pending.back().first;
        const std::size_t depth = pending.back().second;
        pending.pop_back();
        if (depth > 64) throw std::length_error("native projection document nesting limit exceeded");
        if (++nodes > kMaxDocumentNodes)
            throw std::length_error("native projection document node limit exceeded");
        charge(16);
        if (v->is_string()) {
            const auto& s = v->get_ref<const std::string&>();
            if (s.size() > 16384) throw std::length_error("native projection string limit exceeded");
            charge(s.size());
        } else if (v->is_array() || v->is_object()) {
            if (v->size() > kMaxDocumentNodes - nodes - pending.size())
                throw std::length_error("native projection document node limit exceeded");
            for (auto it = v->begin(); it != v->end(); ++it) {
                if (v->is_object()) {
                    if (it.key().size() > 512) throw std::length_error("native projection key limit exceeded");
                    charge(it.key().size());
                }
                pending.emplace_back(&it.value(), depth + 1);
            }
        } else if (v->is_binary() || v->is_discarded()) {
            throw std::runtime_error("unsupported native document JSON value");
        }
    }
}

void append_source(std::string& out, const std::string& text, std::size_t& total) {
    if (text.size() > kMaxSourceBytes - total)
        throw std::length_error("native projection source size limit exceeded");
    total += text.size();
    out += text;
}

std::string number(double n) {
    if (!std::isfinite(n)) throw std::runtime_error("non-finite numeric value");
    if (n == 0) n = 0;  // Do not emit negative zero.
    std::ostringstream out;
    out.imbue(std::locale::classic());
    out << std::setprecision(17) << n;
    return out.str();
}

double resolve(const json& v, const std::map<std::string, double>& env) {
    if (v.is_number()) return v.get<double>();
    if (v.is_string()) return chudware::util::evaluate(v.get<std::string>(), env);
    if (v.is_object()) {
        if (v.contains("expr") && v["expr"].is_string() && !v["expr"].get<std::string>().empty())
            return chudware::util::evaluate(v["expr"].get<std::string>(), env);
        if (v.contains("value") && v["value"].is_number()) return v["value"].get<double>();
    }
    throw std::runtime_error("expected a numeric literal or native dimension expression");
}


struct Field {
    json value;
    std::string path;
};

// The compiler overlays params and then refs onto the flat feature.
std::map<std::string, Field> fields(const json& f, std::size_t i) {
    std::map<std::string, Field> result;
    const std::string base = "/features/" + std::to_string(i);
    for (auto it = f.begin(); it != f.end(); ++it)
        if (it.key() != "params" && it.key() != "refs") result[it.key()] = {it.value(), base + "/" + it.key()};
    for (const char* section : {"params", "refs"}) {
        if (!f.contains(section)) continue;
        if (!f[section].is_object()) throw std::runtime_error(std::string(section) + " must be an object");
        for (auto it = f[section].begin(); it != f[section].end(); ++it)
            result[it.key()] = {it.value(), base + "/" + section + "/" + it.key()};
    }
    return result;
}

struct Projection {
    const json& doc;
    double factor;
    json blockers = json::array();
    json parameters = json::object();
    json schema = json::array();
    json mapping = json::object();
    std::map<std::string, double> env;
    std::string declarations, modules;
    std::size_t source_bytes = 0;
    std::vector<std::string> order;
    std::map<std::string, std::string> bodies, superseded;
    std::set<std::string> consumed, seen_ids;
    std::size_t index = 0;
    std::string id, type;

    void block(const std::string& option, const std::string& reason) {
        blockers.push_back({{"id", id}, {"type", type}, {"option", option}, {"reason", reason}});
    }
    std::string current() const {
        for (auto it = order.rbegin(); it != order.rend(); ++it)
            if (!consumed.count(*it)) return *it;
        return {};
    }
    std::string scalar(const Field& field, const std::string& key, const char* kind) {
        const double v = resolve(field.value, env);
        if (!std::isfinite(v)) throw std::runtime_error(key + " is non-finite");
        const bool length = std::string(kind) == "length";
        const double scad_value = length ? v / factor : v;
        if (!std::isfinite(scad_value)) throw std::runtime_error(key + " is outside source-unit range");
        const std::string name = "p" + std::to_string(index) + "_" + key;
        append_source(declarations, name + " = " + number(scad_value) + ";\n", source_bytes);
        if (field.value.is_string())
            parameters[name] = {{"value", v}, {"expr", field.value}};
        else if (field.value.is_object() && field.value.contains("expr") &&
                 field.value["expr"].is_string() && !field.value["expr"].get<std::string>().empty())
            parameters[name] = {{"value", v}, {"expr", field.value["expr"]}};
        else
            parameters[name] = v;
        schema.push_back({{"name", name}, {"kind", kind}, {"label", id + " " + key}});
        mapping[name] = field.path;
        return name;
    }
    std::string vector(const std::map<std::string, Field>& f, const std::string& key,
                       const char* kind, const std::string& fallback) {
        auto it = f.find(key);
        if (it == f.end()) return fallback;
        if (!it->second.value.is_array() || it->second.value.size() != 3)
            throw std::runtime_error(key + " must be a numeric three-vector");
        std::string out = "[";
        for (int j = 0; j != 3; ++j) {
            // Native opt_vec3/primitive placement accepts literal numeric components only.
            const auto& item = it->second.value[j];
            if (!item.is_number()) throw std::runtime_error(key + " components must be numeric literals");
            Field part{item, it->second.path + "/" + std::to_string(j)};
            if (j) out += ", ";
            out += scalar(part, key + std::to_string(j), kind);
        }
        return out + "]";
    }
    std::string required(const std::map<std::string, Field>& f, const char* key, const char* kind) {
        auto it = f.find(key);
        if (it == f.end()) throw std::runtime_error(std::string("missing ") + key);
        return scalar(it->second, key, kind);
    }
    std::string string_field(const std::map<std::string, Field>& f, const char* key, const std::string& def) {
        auto it = f.find(key);
        if (it == f.end()) return def;
        if (!it->second.value.is_string()) throw std::runtime_error(std::string(key) + " must be a string");
        return it->second.value.get<std::string>();
    }
    bool bool_field(const std::map<std::string, Field>& f, const char* key, bool def) {
        auto it = f.find(key);
        if (it == f.end()) return def;
        if (!it->second.value.is_boolean()) throw std::runtime_error(std::string(key) + " must be boolean");
        return it->second.value.get<bool>();
    }
    void feature(const json& raw) {
        id = raw.is_object() && raw.contains("id") && raw["id"].is_string() ? raw["id"].get<std::string>() : "<missing>";
        type = raw.is_object() && raw.contains("type") && raw["type"].is_string() ? raw["type"].get<std::string>() : "<missing>";
        if (!raw.is_object()) { block("feature", "feature must be an object"); return; }
        if (id == "<missing>" || id.empty() || !seen_ids.insert(id).second) { block("id", "missing or duplicate feature id"); return; }
        if (id.size() > 256) { block("id", "feature id exceeds projection limit"); return; }
        if (raw.contains("suppressed")) {
            if (!raw["suppressed"].is_boolean()) { block("suppressed", "must be boolean"); return; }
            if (raw["suppressed"].get<bool>()) return;
        }
        for (const char* section : {"params", "refs"}) {
            if (!raw.contains(section)) continue;
            if (!raw[section].is_object()) { block(section, "must be an object"); return; }
            if (raw[section].contains("id") || raw[section].contains("type")) {
                block(section, "nested id/type is not supported"); return;
            }
            if (raw[section].contains("suppressed")) {
                if (!raw[section]["suppressed"].is_boolean()) { block("suppressed", "must be boolean"); return; }
                if (raw[section]["suppressed"].get<bool>()) return;
            }
        }
        const bool primitive = type == "box" || type == "cylinder" || type == "sphere" || type == "cone";
        if (!primitive && type != "boolean" && type != "transform") {
            block("type", "feature type is not representable by the managed SCAD projection"); return;
        }
        try {
            auto f = fields(raw, index);
            std::set<std::string> allowed = {"id", "type", "name", "suppressed", "folder"};
            if (primitive) {
                for (const char* s : {"length", "width", "height", "radius", "r1", "r2", "centered", "at", "at_offset", "mode", "merge"}) allowed.insert(s);
            } else if (type == "boolean") {
                for (const char* s : {"op", "a", "b", "targets", "tools"}) allowed.insert(s);
            } else {
                for (const char* s : {"body", "translate", "rotate", "axis", "angle", "pivot", "copy"}) allowed.insert(s);
            }
            bool bad = false;
            for (const auto& entry : f) if (!allowed.count(entry.first)) {
                block(entry.first, "option is not supported by the managed projection"); bad = true;
            }
            // A key for a different primitive is not semantically meaningful here.
            if (primitive) {
                std::set<std::string> dims;
                if (type == "box") dims = {"length", "width", "height"};
                if (type == "cylinder") dims = {"radius", "height"};
                if (type == "sphere") dims = {"radius"};
                if (type == "cone") dims = {"r1", "r2", "height"};
                for (const char* k : {"length", "width", "height", "radius", "r1", "r2"})
                    if (f.count(k) && !dims.count(k)) { block(k, "option does not apply to this primitive"); bad = true; }
            }
            if (bad) return;
            std::string expression;
            if (primitive) {
                const bool center = bool_field(f, "centered", true);
                const std::string at = vector(f, "at", "length", "[0, 0, 0]");
                const std::string off = vector(f, "at_offset", "length", "[0, 0, 0]");
                std::string shape;
                if (type == "box") {
                    const auto length = required(f, "length", "length");
                    const auto width = required(f, "width", "length");
                    const auto height = required(f, "height", "length");
                    shape = "cube([" + length + ", " + width + ", " + height + "], center=" + (center ? "true" : "false") + ");";
                } else if (type == "cylinder") {
                    const auto radius = required(f, "radius", "length");
                    const auto height = required(f, "height", "length");
                    shape = "cylinder(r=" + radius + ", h=" + height + ", center=" + (center ? "true" : "false") + ");";
                } else if (type == "sphere") {
                    shape = "sphere(r=" + required(f, "radius", "length") + ");";
                } else {
                    const auto r1 = required(f, "r1", "length");
                    const auto r2 = required(f, "r2", "length");
                    const auto height = required(f, "height", "length");
                    shape = "cylinder(r1=" + r1 + ", r2=" + r2 + ", h=" + height + ", center=" + (center ? "true" : "false") + ");";
                }
                for (const char* k : {"length", "width", "height", "radius", "r1", "r2"}) if (f.count(k)) {
                    double v = resolve(f.at(k).value, env);
                    if (!std::isfinite(v) || ((std::string(k) == "r1" || std::string(k) == "r2") ? v < 0 : v <= 0)) throw std::runtime_error(std::string(k) + " is outside the valid range");
                    if (type == "box" && v < 1e-7) throw std::runtime_error(std::string(k) + " is below native modelling tolerance");
                }
                if (type == "cone" && resolve(f.at("r1").value, env) == 0 && resolve(f.at("r2").value, env) == 0) throw std::runtime_error("cone radii cannot both be zero");
                std::string valid;
                const std::string prefix = "p" + std::to_string(index) + "_";
                for (const char* k : {"length", "width", "height", "radius", "r1", "r2"}) if (f.count(k)) {
                    if (!valid.empty()) valid += " && ";
                    const std::string name = prefix + k;
                    if (type == "box") valid += name + "*" + number(factor) + " >= 1e-7";
                    else valid += name + ((std::string(k) == "r1" || std::string(k) == "r2") ? " >= 0" : " > 0");
                }
                if (type == "cone") valid += " && (" + prefix + "r1 > 0 || " + prefix + "r2 > 0)";
                shape = "assert(" + valid + ", \"invalid native primitive dimensions\") " + shape;
                expression = "translate(" + at + ") translate(" + off + ") " + shape;
                const std::string mode = string_field(f, "mode", "boss");
                if (mode != "boss" && mode != "add" && mode != "cut" && mode != "new") throw std::runtime_error("unknown primitive mode");
                const bool merge = bool_field(f, "merge", true);
                const std::string target = current();
                if (merge && mode == "cut") {
                    if (target.empty()) throw std::runtime_error("cut requires a live body");
                    expression = "difference() {\n" + bodies.at(target) + "();\n" + expression + "\n}";
                    consumed.insert(target);
                } else if (merge && mode != "new" && !target.empty()) {
                    // Native additive primitives select ALL touching live bodies using OCCT;
                    // this cannot be inferred from a source program or a bounding box.
                    block("mode", "automatic additive merge depends on native contact; use explicit boolean with separate bodies"); return;
                }
            } else if (type == "boolean") {
                auto operand = [&](const char* key, const char* list) -> std::string {
                    if (f.count(list)) {
                        const json& v = f.at(list).value;
                        if (!v.is_array() || v.size() != 1) throw std::runtime_error(std::string(list) + " must contain exactly one direct feature id");
                        if (v[0].is_string()) return v[0].get<std::string>();
                        if (v[0].is_object() && v[0].value("kind", std::string()) == "body" && v[0].contains("owner_feature") && v[0]["owner_feature"].is_string()) return v[0]["owner_feature"].get<std::string>();
                        throw std::runtime_error(std::string(list) + " contains an unsupported selector");
                    }
                    return string_field(f, key, "");
                };
                std::string a = operand("a", "targets"), b = operand("b", "tools");
                if (a.empty() || b.empty() || a == b || !bodies.count(a) || !bodies.count(b) || consumed.count(a) || consumed.count(b)) throw std::runtime_error("boolean requires two distinct live prior bodies");
                std::string op = string_field(f, "op", "");
                if (op == "fuse" || op == "combine") op = "union";
                else if (op == "cut" || op == "subtract") op = "difference";
                else if (op == "common" || op == "intersect") op = "intersection";
                else throw std::runtime_error("unsupported boolean operation");
                expression = op + "() {\n" + bodies.at(a) + "();\n" + bodies.at(b) + "();\n}";
                consumed.insert(a); consumed.insert(b);
            } else {
                std::string body;
                if (f.count("body")) {
                    const json& b = f.at("body").value;
                    if (b.is_string()) body = b.get<std::string>();
                    else if (b.is_object() && b.value("kind", std::string()) == "body" && b.contains("owner_feature") && b["owner_feature"].is_string()) body = b["owner_feature"].get<std::string>();
                    else throw std::runtime_error("unsupported body selector");
                } else body = current();
                std::set<std::string> guard;
                while (consumed.count(body) && superseded.count(body) && guard.insert(body).second) body = superseded.at(body);
                if (body.empty() || consumed.count(body) || !bodies.count(body)) throw std::runtime_error("transform requires a live prior body");
                std::string t = vector(f, "translate", "length", "[0, 0, 0]");
                std::string pivot = vector(f, "pivot", "length", "[0, 0, 0]");
                std::string axis = "[0, 0, 1]", angle = "0";
                json axis_value = json::array({0, 0, 1});
                json angle_value = 0.0;
                if (f.count("rotate") && f.at("rotate").value.is_object()) {
                    const auto& r = f.at("rotate");
                    for (auto it = r.value.begin(); it != r.value.end(); ++it)
                        if (it.key() != "axis" && it.key() != "angle") throw std::runtime_error("unsupported rotate option " + it.key());
                    std::map<std::string, Field> rf;
                    for (auto it = r.value.begin(); it != r.value.end(); ++it) rf[it.key()] = {it.value(), r.path + "/" + it.key()};
                    axis = vector(rf, "axis", "number", axis);
                    if (rf.count("axis")) axis_value = rf.at("axis").value;
                    if (rf.count("angle")) angle_value = rf.at("angle").value;
                    else if (f.count("angle")) angle_value = f.at("angle").value;
                    if (rf.count("angle")) angle = scalar(rf.at("angle"), "rotate_angle", "angle");
                    else if (f.count("angle")) angle = scalar(f.at("angle"), "angle", "angle");
                } else {
                    axis = f.count("rotate") ? vector(f, "rotate", "number", axis) : vector(f, "axis", "number", axis);
                    if (f.count("rotate")) axis_value = f.at("rotate").value;
                    else if (f.count("axis")) axis_value = f.at("axis").value;
                    if (f.count("angle")) angle_value = f.at("angle").value;
                    if (f.count("angle")) angle = scalar(f.at("angle"), "angle", "angle");
                }
                const double av = resolve(angle_value, env);
                const double x = axis_value[0].get<double>(), y = axis_value[1].get<double>(), z = axis_value[2].get<double>();
                if (std::abs(av) > 1e-9 && !(std::hypot(x, y, z) > 1e-12))
                    throw std::runtime_error("rotate axis has zero length");
                expression = "assert(abs(" + angle + ") <= 1e-9 || norm(" + axis + ") > 1e-12, \"invalid native rotation axis\") "
                    "translate(" + t + ") translate(" + pivot + ") rotate(a=(abs(" + angle + ") <= 1e-9 ? 0 : " + angle + "), v=(norm(" + axis + ") > 1e-12 ? " + axis + " : [0,0,1])) translate(-" + pivot + ") {\n" + bodies.at(body) + "();\n}";
                if (!bool_field(f, "copy", false)) { consumed.insert(body); superseded[body] = id; }
            }
            // Keep each graph node once; references call modules instead of recursively
            // embedding predecessor source, including for shared/copy branches.
            const std::string module = "cw_feature_" + std::to_string(index);
            append_source(modules, "module " + module + "() {\n" + expression + "\n}\n", source_bytes);
            bodies[id] = module;
            order.push_back(id);
        } catch (const std::length_error&) { throw; }
          catch (const std::exception& e) { block("parameters", e.what()); }
    }
    json run() {
        if (!std::isfinite(factor) || factor <= 0) return {{"ok", false}, {"report", {{"blockers", json::array({{{"option", "mm_per_source_unit"}, {"reason", "must be finite and positive"}}})}}}};
        if (!doc.is_object() || !doc.contains("features") || !doc["features"].is_array()) return {{"ok", false}, {"report", {{"blockers", json::array({{{"option", "features"}, {"reason", "native document must contain a feature array"}}})}}}};
        check_document_bounds(doc);
        if (doc["features"].size() > kMaxFeatures)
            throw std::length_error("native projection feature limit exceeded");
        if (doc.contains("schema") && doc["schema"] != "aicad.part/1")
            block("schema", "only native part documents are representable");
        if (doc.contains("components") && !doc["components"].is_null() && !doc["components"].empty())
            block("components", "assembly components are not representable");
        if (doc.contains("rollback_after") && !doc["rollback_after"].is_null()) block("rollback_after", "rolled-back history is not supported");
        if (doc.contains("configurations") && !doc["configurations"].is_null() && !doc["configurations"].empty()) block("configurations", "configuration overrides are not supported");
        if (doc.contains("equations") && doc["equations"].is_object() && doc["equations"].contains("globals") && doc["equations"]["globals"].is_array()) {
            for (const auto& g : doc["equations"]["globals"]) {
                if (!g.is_object() || !g.contains("name") || !g["name"].is_string() || !g.contains("expr") || !g["expr"].is_string()) continue;
                try { env[g["name"].get<std::string>()] = chudware::util::evaluate(g["expr"].get<std::string>(), env); } catch (const std::exception&) {}
            }
        }
        for (index = 0; index < doc["features"].size(); ++index) feature(doc["features"][index]);
        if (order.empty() && blockers.empty()) block("features", "document has no representable solid");
        if (!blockers.empty()) return {{"ok", false}, {"report", {{"blockers", blockers}}}};
        std::string body;
        append_source(body, "union() {\n", source_bytes);
        for (const auto& owner : order)
            if (!consumed.count(owner)) append_source(body, bodies.at(owner) + "();\n", source_bytes);
        append_source(body, "}\n", source_bytes);
        std::string header;
        append_source(header, "// Managed native projection. Source coordinates are scaled to millimetres by the host.\n", source_bytes);
        append_source(header, "\n", source_bytes);
        std::string source;
        source.reserve(source_bytes);
        source += header;
        source += declarations;
        source += modules;
        source += body;
        json report = {{"geometry_fidelity", "faceted"}, {"native_geometry", "analytic"}, {"history_recovered", false}, {"native_history_preserved", true}, {"mm_per_source_unit", factor}, {"supported", "box, cylinder, sphere, cone, explicit boolean, rigid transform; no automatic additive contact merge"}, {"body_fidelity", "surviving native bodies are emitted as a SCAD union"}, {"blockers", json::array()}};
        json params = {{"source", source}, {"files", json::array()}, {"parameters", parameters}, {"parameter_schema", schema}, {"mm_per_source_unit", factor}, {"native_projection", {{"version", 1}, {"document", doc}, {"mapping", mapping}}}};
        return {{"ok", true}, {"params", params}, {"report", report}};
    }
};
}  // namespace

json project_native(const json& document, double mm_per_source_unit) {
    try { return Projection{document, mm_per_source_unit}.run(); }
    catch (const std::exception& e) { return {{"ok", false}, {"report", {{"blockers", json::array({{{"reason", e.what()}}})}}}}; }
}

json restore_native(const json& params) {
    try {
        if (!params.is_object() || !params.contains("native_projection") || !params["native_projection"].is_object()) throw std::runtime_error("no managed native projection metadata");
        for (auto it = params.begin(); it != params.end(); ++it) {
            if (it.key() != "source" && it.key() != "files" && it.key() != "parameters" &&
                it.key() != "parameter_schema" && it.key() != "mm_per_source_unit" &&
                it.key() != "native_projection")
                throw std::runtime_error("unsupported managed projection option: " + it.key());
        }
        const json& meta = params["native_projection"];
        if (meta.value("version", 0) != 1 || !meta.contains("document")) throw std::runtime_error("unsupported projection metadata version");
        if (!params.contains("mm_per_source_unit") || !params["mm_per_source_unit"].is_number()) throw std::runtime_error("missing source-unit factor");
        double factor = params["mm_per_source_unit"].get<double>();
        json expected = project_native(meta["document"], factor);
        if (!expected.value("ok", false)) return expected;
        const json& original = expected["params"];
        // Regenerate executable source and every structural input, never trust an embedded hash.
        for (const char* key : {"source", "files", "parameter_schema"})
            if (!params.contains(key) || params[key] != original[key]) throw std::runtime_error(std::string("changed projection ") + key + "; native history cannot be restored");
        if (!meta.contains("mapping") || meta["mapping"] != original["native_projection"]["mapping"]) throw std::runtime_error("changed projection parameter mapping");
        if (!params.contains("parameters") || !params["parameters"].is_object()) throw std::runtime_error("missing projection parameters");
        if (params["parameters"].size() != original["parameters"].size()) throw std::runtime_error("projection parameter set changed");
        json doc = meta["document"];
        for (auto it = original["parameters"].begin(); it != original["parameters"].end(); ++it) {
            if (!params["parameters"].contains(it.key())) throw std::runtime_error("projection parameter set changed");
            const json& value = params["parameters"][it.key()];
            if (value == it.value()) continue;
            if (value.is_number()) {
                if (!std::isfinite(value.get<double>())) throw std::runtime_error("projection override must be finite");
            } else if (value.is_object()) {
                if (!value.contains("value") || !value["value"].is_number() ||
                    !std::isfinite(value["value"].get<double>()) ||
                    (value.contains("expr") && !value["expr"].is_null() && !value["expr"].is_string()))
                    throw std::runtime_error("invalid numeric expression override");
                for (auto v = value.begin(); v != value.end(); ++v)
                    if (v.key() != "value" && v.key() != "expr") throw std::runtime_error("unsupported override field");
            } else throw std::runtime_error("projection override must be numeric");
            const std::string path_string = meta["mapping"][it.key()].get<std::string>();
            json::json_pointer path(path_string);
            const auto slash = path_string.rfind('/');
            const bool vector_component = doc.at(json::json_pointer(path_string.substr(0, slash))).is_array();
            if (vector_component && !value.is_number())
                throw std::runtime_error("native vector components require literal numeric overrides");
            json& target = doc.at(path);
            if (target.is_object()) {
                target["value"] = value.is_object() ? value["value"] : value;
                target["expr"] = value.is_object() && value.contains("expr") ? value["expr"] : json(nullptr);
            } else {
                target = value;
            }
        }
        json updated = project_native(doc, factor);
        if (!updated.value("ok", false)) return updated;
        updated["report"]["history_recovered"] = true;
        return {{"ok", true}, {"document", doc}, {"report", updated["report"]}};
    } catch (const std::exception& e) {
        return {{"ok", false}, {"report", {{"blockers", json::array({{{"reason", e.what()}}})}}}};
    }
}
}  // namespace chudware::doc::scad
