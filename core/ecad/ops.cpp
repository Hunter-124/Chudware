// ECAD ops implementation — see ops.h. Mirrors engine/ecad/ops.py exactly.

#include "ecad/ops.h"
#include "ecad/op_registry.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <map>
#include <limits>
#include <regex>
#include <set>
#include <vector>

#include "ecad/kicad.h"
#include "ecad/schema.h"
#include "ecad/symlib.h"

namespace chudware::ecad {

using nlohmann::json;

namespace {

OpError invalid(int index, const std::string& reason) {
    return OpError("invalid_op", "op[" + std::to_string(index) + "]: " + reason,
                   {{"index", index}, {"reason", reason}});
}

// next_id seeded from the working doc (ids already appended -> distinct per batch).
std::string next_id(const json& doc, const std::string& prefix) {
    return schema::next_id(doc, prefix);
}

// Resolve a "$N" placeholder to its assigned id; non-placeholders pass through.
// A "$N" not in id_map (forward / current / produced-no-id) is an error.
json resolve_placeholder(const json& value, const json& id_map, int index) {
    if (value.is_string()) {
        const std::string& s = value.get_ref<const std::string&>();
        if (!s.empty() && s[0] == '$') {
            if (!id_map.contains(s))
                throw invalid(index,
                              "placeholder '" + s + "' does not refer to an earlier created object");
            return id_map[s];
        }
    }
    return value;
}

void resolve_deep(json& obj, const json& id_map, int index) {
    if (obj.is_object()) {
        for (auto it = obj.begin(); it != obj.end(); ++it) resolve_deep(it.value(), id_map, index);
    } else if (obj.is_array()) {
        for (json& v : obj) resolve_deep(v, id_map, index);
    } else if (obj.is_string()) {
        obj = resolve_placeholder(obj, id_map, index);
    }
}

// Patch: present keys replace; a null value clears the key.
void patch(json& target, const json& p) {
    for (auto it = p.begin(); it != p.end(); ++it) {
        if (it.value().is_null())
            target.erase(it.key());
        else
            target[it.key()] = it.value();
    }
}

json* find(json& items, const std::string& oid) {
    if (!items.is_array()) return nullptr;
    for (json& it : items)
        if (it.is_object() && it.value("id", std::string()) == oid) return &it;
    return nullptr;
}

const json* find(const json& items, const std::string& oid) {
    if (!items.is_array()) return nullptr;
    for (const json& item : items)
        if (item.is_object() && item.value("id", std::string()) == oid) return &item;
    return nullptr;
}

std::vector<double> require_xy(const json& op, const std::string& key, int index) {
    if (!op.contains(key)) throw invalid(index, "'" + key + "' must be [x, y]");
    const json& v = op[key];
    bool ok = v.is_array() && v.size() == 2;
    if (ok)
        for (const json& c : v)
            ok = ok && c.is_number() && !c.is_boolean();
    if (!ok) throw invalid(index, "'" + key + "' must be [x, y]");
    return {v[0].get<double>(), v[1].get<double>()};
}

json require_points(const json& op, const std::string& key, std::size_t min_points, int index) {
    if (!op.contains(key) || !op[key].is_array() || op[key].size() < min_points)
        throw invalid(index, "'" + key + "' must contain at least " +
                                 std::to_string(min_points) + " [x, y] points");
    json out = json::array();
    for (const json& point : op[key]) {
        if (!point.is_array() || point.size() != 2 || !point[0].is_number() ||
            point[0].is_boolean() || !point[1].is_number() || point[1].is_boolean())
            throw invalid(index, "'" + key + "' must contain only [x, y] points");
        out.push_back(json::array({point[0].get<double>(), point[1].get<double>()}));
    }
    return out;
}

std::string require_sheet(const json& doc, const json& value, int index,
                          const std::string& field = "sheet") {
    if (!value.is_string() || value.get<std::string>().empty())
        throw invalid(index, "'" + field + "' must be a sheet id");
    const std::string id = value.get<std::string>();
    if (!doc.contains("sheets") || !find(doc["sheets"], id))
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no sheet '" + id + "'",
                      {{"index", index}, {"reason", "unknown sheet " + id}});
    return id;
}

bool erase_id(json& items, const std::string& id) {
    if (!items.is_array()) return false;
    json kept = json::array();
    bool found = false;
    for (const json& item : items) {
        if (item.is_object() && item.value("id", std::string()) == id)
            found = true;
        else
            kept.push_back(item);
    }
    if (found) items = std::move(kept);
    return found;
}

void require_sch(const json& doc, int index) {
    if (!schema::is_schematic(doc))
        throw invalid(index, "this op is only valid on a schematic document");
}
void require_pcb(const json& doc, int index) {
    if (!schema::is_board(doc)) throw invalid(index, "this op is only valid on a board document");
}

// Drop consecutive duplicate points; merge colinear interior vertices (keeps endpoints).
json orthogonalize(const std::vector<std::array<double, 2>>& pts) {
    std::vector<std::array<double, 2>> clean;
    for (const auto& p : pts) {
        if (clean.empty() || clean.back() != p) clean.push_back(p);
    }
    if (clean.size() <= 2) {
        json out = json::array();
        for (const auto& p : clean) out.push_back(json::array({p[0], p[1]}));
        return out;
    }
    std::vector<std::array<double, 2>> outv{clean[0]};
    for (size_t i = 1; i + 1 < clean.size(); ++i) {
        auto a = outv.back(), b = clean[i], c = clean[i + 1];
        double cross = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0]);
        if (std::abs(cross) < 1e-9) continue;
        outv.push_back(b);
    }
    outv.push_back(clean.back());
    json out = json::array();
    for (const auto& p : outv) out.push_back(json::array({p[0], p[1]}));
    return out;
}

std::set<std::string> net_names(const json& doc) {
    std::set<std::string> out;
    if (doc.contains("nets") && doc["nets"].is_array())
        for (const json& n : doc["nets"])
            if (n.is_object()) out.insert(n.value("name", std::string()));
    return out;
}

void require_net(const json& doc, const json& net, int index) {
    if (!net.is_null()) {
        std::string nm = net.is_string() ? net.get<std::string>() : net.dump();
        if (!net_names(doc).count(net.is_string() ? net.get<std::string>() : std::string()))
            throw OpError("net_not_found", "op[" + std::to_string(index) + "]: net '" + nm +
                                               "' not in board nets",
                          {{"index", index}, {"reason", "net '" + nm + "' not found"}});
    }
}

// ----------------------------------------------------------------- library coupling
json generated_symbol_def(const std::string& libid, const std::string& kind) {
    std::string name = libid;
    const auto p = libid.find(':');
    if (p != std::string::npos) name = libid.substr(p + 1);
    if (name.empty()) name = libid;
    if (kind == "footprint")
        return json{{"fpid", libid}, {"name", name}, {"pads", json::array()},
                    {"generated", true}};
    return json{
        {"libid", libid}, {"name", name}, {"description", "generic 2-pin body"},
        {"prefix", "U"}, {"reference_prefix", "U"}, {"value", name},
        {"power", false}, {"units", 1}, {"units_count", 1},
        {"default_footprint", ""}, {"datasheet", ""}, {"generated", true},
        {"pins", json::array({
            json{{"number", "1"}, {"name", "1"}, {"pos", json::array({-2.54, 0.0})},
                 {"angle", 0.0}, {"length", 2.54}, {"type", "passive"},
                 {"shape", "line"}, {"unit", 1}},
            json{{"number", "2"}, {"name", "2"}, {"pos", json::array({2.54, 0.0})},
                 {"angle", 180.0}, {"length", 2.54}, {"type", "passive"},
                 {"shape", "line"}, {"unit", 1}}})},
        {"graphics", json::array()}};
}

void ensure_lib_def(json& doc, const std::string& libid, const json& caller_def,
                    json& warnings, int, const std::string& kind) {
    if (!doc.contains("lib_cache") || !doc["lib_cache"].is_object())
        doc["lib_cache"] = json::object();
    json& cache = doc["lib_cache"];
    if (caller_def.is_object()) {
        json def = caller_def;
        def["libid"] = libid;
        cache[libid] = std::move(def);
        return;
    }
    if (cache.contains(libid) && cache[libid].is_object()) return;
    if (kind == "symbol") {
        json def = symlib::resolve_symbol(libid);
        if (def.is_object()) {
            def["libid"] = libid;
            cache[libid] = std::move(def);
            return;
        }
    }
    warnings.push_back("symbol_not_in_library: " + libid +
                       " — placed a generic 2-pin body");
    cache[libid] = generated_symbol_def(libid, kind);
}

std::string ref_prefix(const json& doc, const std::string& libid) {
    if (doc.contains("lib_cache") && doc["lib_cache"].is_object() &&
        doc["lib_cache"].contains(libid)) {
        const json& def = doc["lib_cache"][libid];
        if (def.is_object()) {
            for (const char* key : {"prefix", "reference_prefix"})
                if (def.contains(key) && def[key].is_string() &&
                    !def[key].get<std::string>().empty())
                    return def[key].get<std::string>();
        }
    }
    std::string name = libid;
    const auto pp = libid.find(':');
    if (pp != std::string::npos) name = libid.substr(pp + 1);
    if (name.empty()) return "U";
    const char c = name[0];
    return std::string(1, static_cast<char>(std::toupper(static_cast<unsigned char>(c))));
}

std::set<std::string> pin_set(const json& def) {
    std::set<std::string> out;
    if (!def.is_object() || !def.contains("pins") || !def["pins"].is_array()) return out;
    for (const json& pin : def["pins"]) {
        if (!pin.is_object() || !pin.contains("number")) continue;
        out.insert(pin["number"].is_string() ? pin["number"].get<std::string>()
                                             : pin["number"].dump());
    }
    return out;
}

std::string next_ref(const json& doc, const std::string& prefix) {
    const std::regex pat("^" + prefix + "([0-9]+)$");
    std::set<int> used;
    if (doc.contains("symbols") && doc["symbols"].is_array())
        for (const json& s : doc["symbols"]) {
            if (!s.is_object()) continue;
            std::smatch m;
            std::string ref = s.value("ref", std::string());
            if (std::regex_match(ref, m, pat)) used.insert(std::atoi(m[1].str().c_str()));
        }
    int n = 1;
    while (used.count(n)) ++n;
    return prefix + std::to_string(n);
}

// ----------------------------------------------------------------- schematic ops
void op_sch_add_symbol(json& doc, const json& op, int index, json& id_map, json& warnings) {
    require_sch(doc, index);
    if (!op.contains("libid") || !op["libid"].is_string() ||
        op["libid"].get<std::string>().empty())
        throw invalid(index, "sch_add_symbol requires a 'libid' string");
    const std::string libid = op["libid"];
    ensure_lib_def(doc, libid, op.contains("def") ? op["def"] : json(), warnings, index,
                   "symbol");
    const json& def = doc["lib_cache"][libid];
    const std::string sheet = require_sheet(
        doc, op.contains("sheet") ? op["sheet"] : json("sh1"), index);
    for (const char* key : {"in_bom", "on_board", "dnp"})
        if (op.contains(key) && !op[key].is_boolean())
            throw invalid(index, std::string("'") + key + "' must be boolean");
    if (op.contains("unit") &&
        (!op["unit"].is_number_integer() || op["unit"].is_boolean() ||
         op["unit"].get<int>() < 1))
        throw invalid(index, "'unit' must be a positive integer");
    if (op.contains("angle") &&
        (!op["angle"].is_number() || op["angle"].is_boolean()))
        throw invalid(index, "'angle' must be numeric");
    if (op.contains("fields") && !op["fields"].is_object())
        throw invalid(index, "'fields' must be an object");
    if (op.contains("mirror") && !op["mirror"].is_string())
        throw invalid(index, "'mirror' must be a string");
    if (op.contains("ref") &&
        (!op["ref"].is_string() || op["ref"].get<std::string>().empty()))
        throw invalid(index, "'ref' must be a non-empty string");
    for (const char* key : {"footprint", "datasheet"})
        if (op.contains(key) && !op[key].is_null() && !op[key].is_string())
            throw invalid(index, std::string("'") + key + "' must be a string or null");
    const std::string mirror =
        op.contains("mirror") ? op["mirror"].get<std::string>() : std::string("none");
    if (!schema::sym_mirror().count(mirror))
        throw invalid(index, "mirror must be one of [none, x, y]");

    const std::string sid = next_id(doc, "s");
    const std::string ref =
        op.contains("ref") && op["ref"].is_string() && !op["ref"].get<std::string>().empty()
            ? op["ref"].get<std::string>()
            : next_ref(doc, ref_prefix(doc, libid));
    const json def_value =
        def.contains("value") ? def["value"]
                              : json(def.contains("name") && def["name"].is_string()
                                         ? def["name"].get<std::string>()
                                         : std::string());
    const std::string def_datasheet =
        def.contains("datasheet") && def["datasheet"].is_string()
            ? def["datasheet"].get<std::string>()
            : std::string();
    json sym{
        {"id", sid},
        {"libid", libid},
        {"sheet", sheet},
        {"ref", ref},
        {"value", op.contains("value") ? op["value"] : def_value},
        {"pos", require_xy(op, "pos", index)},
        {"angle", op.contains("angle") ? op["angle"].get<double>() : 0.0},
        {"mirror", mirror},
        {"unit", op.contains("unit") ? op["unit"].get<int>() : 1},
        {"fields", op.contains("fields") ? op["fields"] : json::object()},
        {"in_bom", op.value("in_bom", true)},
        {"on_board", op.value("on_board", true)},
        {"dnp", op.value("dnp", false)},
        {"datasheet", op.contains("datasheet") ? op["datasheet"] : json(def_datasheet)}};
    if (op.contains("footprint")) {
        if (!op["footprint"].is_null()) sym["footprint"] = op["footprint"];
    } else if (def.contains("default_footprint") && def["default_footprint"].is_string()) {
        const std::string footprint = def["default_footprint"];
        if (!footprint.empty()) sym["footprint"] = footprint;
    }
    if (op.contains("sim_params") && !op["sim_params"].is_null())
        sym["sim_params"] = op["sim_params"];
    doc["symbols"].push_back(std::move(sym));
    id_map["$" + std::to_string(index)] = sid;
}

void op_sch_edit_symbol(json& doc, const json& op, int index, json& id_map,
                        json& warnings) {
    require_sch(doc, index);
    const json sid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* sym = find(doc["symbols"], sid.is_string() ? sid.get<std::string>() : std::string());
    if (!sym)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no symbol '" + sid.dump() + "'",
                      {{"index", index}, {"reason", "unknown symbol " + sid.dump()}});

    json p = json::object();
    for (const char* key : {"pos",       "angle",     "mirror",    "ref",
                            "value",     "footprint", "fields",    "dnp",
                            "sim_params", "in_bom",    "on_board",  "unit",
                            "datasheet", "sheet",     "libid"})
        if (op.contains(key)) p[key] = op[key];
    if (p.contains("pos")) p["pos"] = require_xy(op, "pos", index);
    if (p.contains("sheet"))
        p["sheet"] = require_sheet(doc, p["sheet"], index);
    if (p.contains("mirror") &&
        (!p["mirror"].is_string() ||
         !schema::sym_mirror().count(p["mirror"].get<std::string>())))
        throw invalid(index, "mirror must be one of [none, x, y]");
    if (p.contains("angle") &&
        (!p["angle"].is_number() || p["angle"].is_boolean()))
        throw invalid(index, "'angle' must be numeric");
    for (const char* key : {"dnp", "in_bom", "on_board"})
        if (p.contains(key) && !p[key].is_boolean())
            throw invalid(index, std::string("'") + key + "' must be boolean");
    if (p.contains("unit") &&
        (!p["unit"].is_number_integer() || p["unit"].is_boolean() ||
         p["unit"].get<int>() < 1))
        throw invalid(index, "'unit' must be a positive integer");
    if (p.contains("fields") && !p["fields"].is_null() && !p["fields"].is_object())
        throw invalid(index, "'fields' must be an object or null");
    for (const char* key : {"ref", "footprint", "datasheet"})
        if (p.contains(key) && !p[key].is_null() && !p[key].is_string())
            throw invalid(index, std::string("'") + key + "' must be a string or null");
    if (p.contains("libid")) {
        if (!p["libid"].is_string() || p["libid"].get<std::string>().empty())
            throw invalid(index, "'libid' must be a non-empty string");
        const std::string old_libid = sym->value("libid", std::string());
        const std::set<std::string> old_pins =
            doc["lib_cache"].contains(old_libid) ? pin_set(doc["lib_cache"][old_libid])
                                                 : std::set<std::string>{};
        const std::string new_libid = p["libid"].get<std::string>();
        ensure_lib_def(doc, new_libid, json(), warnings, index, "symbol");
        const std::set<std::string> new_pins = pin_set(doc["lib_cache"][new_libid]);
        if (old_pins != new_pins)
            warnings.push_back("pin_count_changed: " + old_libid + " -> " + new_libid);
    }
    patch(*sym, p);
}

void op_sch_delete_symbol(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    json sid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    std::string s = sid.is_string() ? sid.get<std::string>() : std::string();
    if (!find(doc["symbols"], s))
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no symbol '" + s + "'",
                      {{"index", index}, {"reason", "unknown symbol " + s}});
    json kept = json::array();
    for (const json& x : doc["symbols"])
        if (x.value("id", std::string()) != s) kept.push_back(x);
    doc["symbols"] = kept;
}

void op_sch_add_wire(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json pts = require_points(op, "pts", 2, index);
    std::vector<std::array<double, 2>> raw;
    for (const json& point : pts)
        raw.push_back({point[0].get<double>(), point[1].get<double>()});
    const json clean = orthogonalize(raw);
    if (clean.size() < 2)
        throw invalid(index, "sch_add_wire: degenerate (all points coincident)");
    if (op.contains("bus") && !op["bus"].is_boolean())
        throw invalid(index, "'bus' must be boolean");
    const std::string wid = next_id(doc, "w");
    doc["wires"].push_back(
        {{"id", wid},
         {"sheet", require_sheet(doc, op.contains("sheet") ? op["sheet"] : json("sh1"),
                                  index)},
         {"pts", clean},
         {"bus", op.value("bus", false)}});
    id_map["$" + std::to_string(index)] = wid;
}

void op_sch_edit_wire(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json wid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* wire = find(doc["wires"], wid.is_string() ? wid.get<std::string>() : std::string());
    if (!wire)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no wire '" + wid.dump() + "'",
                      {{"index", index}, {"reason", "unknown wire " + wid.dump()}});
    if (!op.contains("pts") && !op.contains("bus"))
        throw invalid(index, "sch_edit_wire requires 'pts' and/or 'bus'");
    if (op.contains("pts")) {
        const json pts = require_points(op, "pts", 2, index);
        std::vector<std::array<double, 2>> raw;
        for (const json& point : pts)
            raw.push_back({point[0].get<double>(), point[1].get<double>()});
        const json clean = orthogonalize(raw);
        if (clean.size() < 2)
            throw invalid(index, "sch_edit_wire: degenerate (all points coincident)");
        (*wire)["pts"] = clean;
    }
    if (op.contains("bus")) {
        if (!op["bus"].is_boolean()) throw invalid(index, "'bus' must be boolean");
        (*wire)["bus"] = op["bus"];
    }
}

void op_sch_delete_wire(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    json wid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    std::string w = wid.is_string() ? wid.get<std::string>() : std::string();
    if (!find(doc["wires"], w))
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no wire '" + w + "'",
                      {{"index", index}, {"reason", "unknown wire " + w}});
    json kept = json::array();
    for (const json& x : doc["wires"])
        if (x.value("id", std::string()) != w) kept.push_back(x);
    doc["wires"] = kept;
}

void op_sch_add_junction(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const std::string jid = next_id(doc, "j");
    doc["junctions"].push_back(
        {{"id", jid},
         {"sheet", require_sheet(doc, op.contains("sheet") ? op["sheet"] : json("sh1"),
                                  index)},
         {"pos", require_xy(op, "pos", index)}});
    id_map["$" + std::to_string(index)] = jid;
}

void op_sch_edit_junction(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json jid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* junction =
        find(doc["junctions"], jid.is_string() ? jid.get<std::string>() : std::string());
    if (!junction)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no junction '" + jid.dump() + "'",
                      {{"index", index}, {"reason", "unknown junction " + jid.dump()}});
    (*junction)["pos"] = require_xy(op, "pos", index);
}

void op_sch_delete_junction(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json jid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    const std::string id = jid.is_string() ? jid.get<std::string>() : std::string();
    if (!erase_id(doc["junctions"], id))
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no junction '" + id + "'",
                      {{"index", index}, {"reason", "unknown junction " + id}});
}

void op_sch_add_label(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    if (!op.contains("text") || !op["text"].is_string() || op["text"].get<std::string>().empty())
        throw invalid(index, "sch_add_label requires a non-empty 'text'");
    if (op.contains("scope") && !op["scope"].is_string())
        throw invalid(index, "scope must be one of [global, hier, local]");
    const std::string scope =
        op.contains("scope") ? op["scope"].get<std::string>() : std::string("local");
    if (!schema::label_scopes().count(scope))
        throw invalid(index, "scope must be one of [global, hier, local]");
    if (op.contains("angle") &&
        (!op["angle"].is_number() || op["angle"].is_boolean()))
        throw invalid(index, "sch_add_label.angle must be numeric");
    const std::string lid = next_id(doc, "l");
    doc["labels"].push_back(
        {{"id", lid},
         {"sheet", require_sheet(doc, op.contains("sheet") ? op["sheet"] : json("sh1"),
                                  index)},
         {"pos", require_xy(op, "pos", index)},
         {"text", op["text"]},
         {"scope", scope},
         {"angle", op.contains("angle") ? op["angle"].get<double>() : 0.0}});
    id_map["$" + std::to_string(index)] = lid;
}

void op_sch_edit_label(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    json lid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* lab = find(doc["labels"], lid.is_string() ? lid.get<std::string>() : std::string());
    if (!lab)
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no label '" + lid.dump() + "'",
                      {{"index", index}, {"reason", "unknown label " + lid.dump()}});
    if (op.contains("scope") &&
        (!op["scope"].is_string() ||
         !schema::label_scopes().count(op["scope"].get<std::string>())))
        throw invalid(index, "scope must be one of [global, hier, local]");
    if (op.contains("text") &&
        (!op["text"].is_string() || op["text"].get<std::string>().empty()))
        throw invalid(index, "sch_edit_label.text must be a non-empty string");
    if (op.contains("angle") &&
        (!op["angle"].is_number() || op["angle"].is_boolean()))
        throw invalid(index, "sch_edit_label.angle must be numeric");
    json p = json::object();
    for (const char* key : {"pos", "text", "scope", "angle"})
        if (op.contains(key)) p[key] = op[key];
    if (p.contains("pos")) p["pos"] = require_xy(op, "pos", index);
    patch(*lab, p);
}

void op_sch_delete_label(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    json lid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    std::string l = lid.is_string() ? lid.get<std::string>() : std::string();
    if (!find(doc["labels"], l))
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no label '" + l + "'",
                      {{"index", index}, {"reason", "unknown label " + l}});
    json kept = json::array();
    for (const json& x : doc["labels"])
        if (x.value("id", std::string()) != l) kept.push_back(x);
    doc["labels"] = kept;
}

void op_sch_add_power(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    if (!op.contains("net") || !op["net"].is_string() ||
        op["net"].get<std::string>().empty())
        throw invalid(index, "sch_add_power requires a non-empty 'net'");
    if (op.contains("symbol") && !op["symbol"].is_string())
        throw invalid(index, "sch_add_power.symbol must be a string");
    const std::string net = op["net"];
    const std::string pid = next_id(doc, "p");
    doc["power"].push_back(
        {{"id", pid},
         {"sheet", require_sheet(doc, op.contains("sheet") ? op["sheet"] : json("sh1"),
                                  index)},
         {"pos", require_xy(op, "pos", index)},
         {"net", net},
         {"symbol", op.contains("symbol") ? op["symbol"] : json(net)}});
    id_map["$" + std::to_string(index)] = pid;
}

void op_sch_edit_power(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json pid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* power = find(doc["power"], pid.is_string() ? pid.get<std::string>() : std::string());
    if (!power)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no power port '" + pid.dump() + "'",
                      {{"index", index}, {"reason", "unknown power port " + pid.dump()}});
    json p = json::object();
    for (const char* key : {"pos", "net", "symbol"})
        if (op.contains(key)) p[key] = op[key];
    if (p.empty()) throw invalid(index, "sch_edit_power requires a patch");
    if (p.contains("pos")) p["pos"] = require_xy(op, "pos", index);
    if (p.contains("net") &&
        (!p["net"].is_string() || p["net"].get<std::string>().empty()))
        throw invalid(index, "sch_edit_power.net must be a non-empty string");
    if (p.contains("symbol") && !p["symbol"].is_string())
        throw invalid(index, "sch_edit_power.symbol must be a string");
    patch(*power, p);
}

void op_sch_delete_power(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json pid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    const std::string id = pid.is_string() ? pid.get<std::string>() : std::string();
    if (!erase_id(doc["power"], id))
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no power port '" + id + "'",
                      {{"index", index}, {"reason", "unknown power port " + id}});
}

void op_sch_add_no_connect(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const std::string nid = next_id(doc, "nc");
    doc["no_connect"].push_back(
        {{"id", nid},
         {"sheet", require_sheet(doc, op.contains("sheet") ? op["sheet"] : json("sh1"),
                                  index)},
         {"pos", require_xy(op, "pos", index)}});
    id_map["$" + std::to_string(index)] = nid;
}

void op_sch_delete_no_connect(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json nid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    const std::string id = nid.is_string() ? nid.get<std::string>() : std::string();
    if (!erase_id(doc["no_connect"], id))
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no no-connect '" + id + "'",
                      {{"index", index}, {"reason", "unknown no-connect " + id}});
}

bool bus_name_exists(const json& doc, const std::string& name,
                     const std::string& except_id = std::string()) {
    if (!doc.contains("buses") || !doc["buses"].is_array()) return false;
    for (const json& bus : doc["buses"])
        if (bus.is_object() && bus.value("name", std::string()) == name &&
            bus.value("id", std::string()) != except_id)
            return true;
    return false;
}

void require_bus_name(const json& doc, const json& value, int index) {
    if (!value.is_string() || value.get<std::string>().empty() ||
        !bus_name_exists(doc, value.get<std::string>()))
        throw OpError("bad_ref",
                      "op[" + std::to_string(index) + "]: bus entry references unknown bus '" +
                          (value.is_string() ? value.get<std::string>() : value.dump()) + "'",
                      {{"index", index}, {"reason", "unknown bus"}});
}

void op_sch_add_bus(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    if (!op.contains("name") || !op["name"].is_string() ||
        op["name"].get<std::string>().empty())
        throw invalid(index, "sch_add_bus requires a non-empty 'name'");
    const std::string name = op["name"];
    if (bus_name_exists(doc, name))
        throw OpError("duplicate_ref",
                      "op[" + std::to_string(index) + "]: bus '" + name + "' already exists",
                      {{"index", index}, {"reason", "duplicate bus " + name}});
    const std::string id = next_id(doc, "b");
    doc["buses"].push_back(
        {{"id", id},
         {"name", name},
         {"pts", require_points(op, "pts", 2, index)},
         {"sheet", require_sheet(doc, op.contains("sheet") ? op["sheet"] : json("sh1"),
                                  index)}});
    id_map["$" + std::to_string(index)] = id;
}

void op_sch_edit_bus(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json bid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* bus = find(doc["buses"], bid.is_string() ? bid.get<std::string>() : std::string());
    if (!bus)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no bus '" + bid.dump() + "'",
                      {{"index", index}, {"reason", "unknown bus " + bid.dump()}});
    if (!op.contains("name") && !op.contains("pts"))
        throw invalid(index, "sch_edit_bus requires 'name' and/or 'pts'");
    if (op.contains("name")) {
        if (!op["name"].is_string() || op["name"].get<std::string>().empty())
            throw invalid(index, "sch_edit_bus.name must be a non-empty string");
        const std::string old_name = bus->value("name", std::string());
        const std::string new_name = op["name"];
        const std::string id = bus->value("id", std::string());
        if (bus_name_exists(doc, new_name, id))
            throw OpError("duplicate_ref",
                          "op[" + std::to_string(index) + "]: bus '" + new_name +
                              "' already exists",
                          {{"index", index}, {"reason", "duplicate bus " + new_name}});
        (*bus)["name"] = new_name;
        if (doc.contains("bus_entries") && doc["bus_entries"].is_array())
            for (json& entry : doc["bus_entries"])
                if (entry.is_object() && entry.value("bus", std::string()) == old_name)
                    entry["bus"] = new_name;
    }
    if (op.contains("pts")) (*bus)["pts"] = require_points(op, "pts", 2, index);
}

void op_sch_delete_bus(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json bid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    const std::string id = bid.is_string() ? bid.get<std::string>() : std::string();
    json* bus = find(doc["buses"], id);
    if (!bus)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no bus '" + id + "'",
                      {{"index", index}, {"reason", "unknown bus " + id}});
    const std::string name = bus->value("name", std::string());
    erase_id(doc["buses"], id);
    if (doc.contains("bus_entries") && doc["bus_entries"].is_array()) {
        json kept = json::array();
        for (const json& entry : doc["bus_entries"])
            if (!entry.is_object() ||
                (entry.value("bus", std::string()) != name &&
                 entry.value("bus", std::string()) != id))
                kept.push_back(entry);
        doc["bus_entries"] = std::move(kept);
    }
}

void op_sch_add_bus_entry(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    if (op.contains("bus") && !op["bus"].is_null())
        require_bus_name(doc, op["bus"], index);
    if (op.contains("member") && !op["member"].is_null() &&
        !op["member"].is_string())
        throw invalid(index, "sch_add_bus_entry.member must be a string");
    const std::string id = next_id(doc, "be");
    json entry{
        {"id", id},
        {"pos", require_xy(op, "pos", index)},
        {"size", op.contains("size") ? json(require_xy(op, "size", index))
                                     : json::array({2.54, 2.54})},
        {"sheet", require_sheet(doc, op.contains("sheet") ? op["sheet"] : json("sh1"),
                                 index)}};
    if (op.contains("bus") && !op["bus"].is_null()) entry["bus"] = op["bus"];
    if (op.contains("member") && !op["member"].is_null()) entry["member"] = op["member"];
    doc["bus_entries"].push_back(std::move(entry));
    id_map["$" + std::to_string(index)] = id;
}

void op_sch_delete_bus_entry(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json eid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    const std::string id = eid.is_string() ? eid.get<std::string>() : std::string();
    if (!erase_id(doc["bus_entries"], id))
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no bus entry '" + id + "'",
                      {{"index", index}, {"reason", "unknown bus entry " + id}});
}

void op_sch_add_text(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    if (!op.contains("text") || !op["text"].is_string())
        throw invalid(index, "sch_add_text requires a 'text' string");
    if (op.contains("size") &&
        (!op["size"].is_number() || op["size"].is_boolean() ||
         op["size"].get<double>() <= 0.0))
        throw invalid(index, "sch_add_text.size must be positive");
    if (op.contains("angle") &&
        (!op["angle"].is_number() || op["angle"].is_boolean()))
        throw invalid(index, "sch_add_text.angle must be numeric");
    const std::string id = next_id(doc, "tx");
    doc["texts"].push_back(
        {{"id", id},
         {"pos", require_xy(op, "pos", index)},
         {"text", op["text"]},
         {"size", op.value("size", 1.27)},
         {"angle", op.value("angle", 0.0)},
         {"sheet", require_sheet(doc, op.contains("sheet") ? op["sheet"] : json("sh1"),
                                  index)}});
    id_map["$" + std::to_string(index)] = id;
}

void op_sch_edit_text(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json tid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* text = find(doc["texts"], tid.is_string() ? tid.get<std::string>() : std::string());
    if (!text)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no text '" + tid.dump() + "'",
                      {{"index", index}, {"reason", "unknown text " + tid.dump()}});
    json p = json::object();
    for (const char* key : {"pos", "text", "size", "angle"})
        if (op.contains(key)) p[key] = op[key];
    if (p.empty()) throw invalid(index, "sch_edit_text requires a patch");
    if (p.contains("pos")) p["pos"] = require_xy(op, "pos", index);
    if (p.contains("text") && !p["text"].is_string())
        throw invalid(index, "sch_edit_text.text must be a string");
    if (p.contains("size") &&
        (!p["size"].is_number() || p["size"].is_boolean() ||
         p["size"].get<double>() <= 0.0))
        throw invalid(index, "sch_edit_text.size must be positive");
    if (p.contains("angle") &&
        (!p["angle"].is_number() || p["angle"].is_boolean()))
        throw invalid(index, "sch_edit_text.angle must be numeric");
    patch(*text, p);
}

void op_sch_delete_text(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json tid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    const std::string id = tid.is_string() ? tid.get<std::string>() : std::string();
    if (!erase_id(doc["texts"], id))
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no text '" + id + "'",
                      {{"index", index}, {"reason", "unknown text " + id}});
}

void validate_graphic(json& graphic, int index) {
    if (!graphic.contains("kind") || !graphic["kind"].is_string())
        throw invalid(index, "graphic kind must be one of [arc, circle, line, polyline, rect]");
    const std::string kind = graphic["kind"];
    if (!schema::graphic_kinds().count(kind))
        throw invalid(index, "graphic kind must be one of [arc, circle, line, polyline, rect]");
    const std::size_t min_points =
        kind == "line" || kind == "rect" || kind == "polyline" ? 2 : 1;
    json wrapper{{"pts", graphic.contains("pts") ? graphic["pts"] : json()}};
    graphic["pts"] = require_points(wrapper, "pts", min_points, index);
    if (kind == "circle" || kind == "arc") {
        if (!graphic.contains("radius") || !graphic["radius"].is_number() ||
            graphic["radius"].is_boolean() || graphic["radius"].get<double>() <= 0.0)
            throw invalid(index, "graphic radius must be positive for " + kind);
    }
    for (const char* key : {"radius", "a0", "a1", "width"})
        if (graphic.contains(key) &&
            (!graphic[key].is_number() || graphic[key].is_boolean()))
            throw invalid(index, std::string("graphic ") + key + " must be numeric");
    if (graphic.contains("width") && graphic["width"].get<double>() < 0.0)
        throw invalid(index, "graphic width must be non-negative");
    if (graphic.contains("filled") && !graphic["filled"].is_boolean())
        throw invalid(index, "graphic filled must be boolean");
}

void op_sch_add_graphic(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    json graphic{
        {"id", next_id(doc, "g")},
        {"kind", op.contains("kind") ? op["kind"] : json()},
        {"pts", op.contains("pts") ? op["pts"] : json()},
        {"width", op.contains("width") ? op["width"] : json(0.25)},
        {"filled", op.contains("filled") ? op["filled"] : json(false)},
        {"sheet", require_sheet(doc, op.contains("sheet") ? op["sheet"] : json("sh1"),
                                 index)}};
    for (const char* key : {"radius", "a0", "a1"})
        if (op.contains(key) && !op[key].is_null()) graphic[key] = op[key];
    validate_graphic(graphic, index);
    const std::string id = graphic["id"];
    doc["graphics"].push_back(std::move(graphic));
    id_map["$" + std::to_string(index)] = id;
}

void op_sch_edit_graphic(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json gid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* graphic =
        find(doc["graphics"], gid.is_string() ? gid.get<std::string>() : std::string());
    if (!graphic)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no graphic '" + gid.dump() + "'",
                      {{"index", index}, {"reason", "unknown graphic " + gid.dump()}});
    json candidate = *graphic;
    json p = json::object();
    for (const char* key : {"kind", "pts", "radius", "a0", "a1", "width", "filled"})
        if (op.contains(key)) p[key] = op[key];
    if (p.empty()) throw invalid(index, "sch_edit_graphic requires a patch");
    patch(candidate, p);
    validate_graphic(candidate, index);
    *graphic = std::move(candidate);
}

void op_sch_delete_graphic(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json gid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    const std::string id = gid.is_string() ? gid.get<std::string>() : std::string();
    if (!erase_id(doc["graphics"], id))
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no graphic '" + id + "'",
                      {{"index", index}, {"reason", "unknown graphic " + id}});
}

using RoutePoint = std::array<double, 2>;

struct RouteBBox {
    double xmin = std::numeric_limits<double>::infinity();
    double ymin = std::numeric_limits<double>::infinity();
    double xmax = -std::numeric_limits<double>::infinity();
    double ymax = -std::numeric_limits<double>::infinity();

    void include(const RoutePoint& point) {
        xmin = std::min(xmin, point[0]);
        ymin = std::min(ymin, point[1]);
        xmax = std::max(xmax, point[0]);
        ymax = std::max(ymax, point[1]);
    }
};

RoutePoint world_point(const json& symbol, const json& local) {
    json point_pin{{"pos", local}, {"length", 0.0}, {"angle", 0.0}};
    const auto [x, y] = schema::pin_world(symbol, point_pin);
    return {x, y};
}

int symbol_unit(const json& symbol) {
    return symbol.contains("unit") && symbol["unit"].is_number_integer()
               ? symbol["unit"].get<int>()
               : 1;
}

bool pin_on_unit(const json& pin, int unit) {
    const int pin_unit = pin.contains("unit") && pin["unit"].is_number_integer()
                             ? pin["unit"].get<int>()
                             : 1;
    return pin_unit == 0 || pin_unit == unit;
}

std::string pin_number(const json& pin) {
    if (!pin.contains("number")) return {};
    return pin["number"].is_string() ? pin["number"].get<std::string>()
                                     : pin["number"].dump();
}

RoutePoint pin_root_world(const json& symbol, const json& pin) {
    json root_pin = pin;
    root_pin["length"] = 0.0;
    const auto [x, y] = schema::pin_world(symbol, root_pin);
    return {x, y};
}

RoutePoint pin_tip_world(const json& symbol, const json& pin) {
    const auto [x, y] = schema::pin_world(symbol, pin);
    return {x, y};
}

RouteBBox symbol_route_bbox(const json& doc, const json& symbol, double grid) {
    RouteBBox bbox;
    const std::string libid = symbol.value("libid", std::string());
    const json def = doc.contains("lib_cache") && doc["lib_cache"].is_object() &&
                             doc["lib_cache"].contains(libid)
                         ? doc["lib_cache"][libid]
                         : json::object();
    if (def.contains("graphics") && def["graphics"].is_array()) {
        for (const json& graphic : def["graphics"]) {
            if (!graphic.is_object()) continue;
            if (graphic.contains("pts") && graphic["pts"].is_array())
                for (const json& point : graphic["pts"])
                    if (point.is_array() && point.size() == 2 && point[0].is_number() &&
                        point[1].is_number())
                        bbox.include(world_point(symbol, point));
            const std::string kind = graphic.value("kind", std::string());
            if ((kind == "circle" || kind == "arc") && graphic.contains("radius") &&
                graphic["radius"].is_number() && graphic.contains("pts") &&
                graphic["pts"].is_array() && !graphic["pts"].empty()) {
                const RoutePoint center = world_point(symbol, graphic["pts"][0]);
                const double radius = std::abs(graphic["radius"].get<double>());
                bbox.include({center[0] - radius, center[1] - radius});
                bbox.include({center[0] + radius, center[1] + radius});
            }
            if (kind == "text" && graphic.contains("size") && graphic["size"].is_number() &&
                graphic.contains("pts") && graphic["pts"].is_array() &&
                !graphic["pts"].empty()) {
                const RoutePoint center = world_point(symbol, graphic["pts"][0]);
                const double half = std::abs(graphic["size"].get<double>()) * 0.5;
                bbox.include({center[0] - half, center[1] - half});
                bbox.include({center[0] + half, center[1] + half});
            }
        }
    }
    if (def.contains("pins") && def["pins"].is_array()) {
        const int unit = symbol_unit(symbol);
        for (const json& pin : def["pins"]) {
            if (!pin.is_object() || !pin_on_unit(pin, unit)) continue;
            bbox.include(pin_root_world(symbol, pin));
            bbox.include(pin_tip_world(symbol, pin));
        }
    }
    if (!std::isfinite(bbox.xmin)) {
        const json pos = symbol.contains("pos") ? symbol["pos"] : json::array({0.0, 0.0});
        bbox.include({pos[0].get<double>(), pos[1].get<double>()});
    }
    const double inflate = grid * 0.5;
    bbox.xmin -= inflate;
    bbox.ymin -= inflate;
    bbox.xmax += inflate;
    bbox.ymax += inflate;
    return bbox;
}

struct RouteEndpoint {
    const json* symbol = nullptr;
    std::string symbol_id;
    std::string ref;
    std::string pin;
    RoutePoint root{};
    RoutePoint tip{};
    RoutePoint escape{};
    RouteBBox bbox;
};

RouteEndpoint route_endpoint(const json& doc, const json& ref, const json& id_map,
                             int index, double grid) {
    const json sid =
        resolve_placeholder(ref.contains("sym") ? ref["sym"] : json(), id_map, index);
    const std::string symbol_id =
        sid.is_string() ? sid.get<std::string>() : std::string();
    const json* symbol = find(doc["symbols"], symbol_id);
    if (!symbol)
        throw OpError("bad_ref",
                      "op[" + std::to_string(index) +
                          "]: sch_connect references unknown symbol '" + symbol_id + "'",
                      {{"index", index}, {"reason", "unknown symbol " + symbol_id}});
    const std::string requested =
        ref.contains("pin") && ref["pin"].is_string()
            ? ref["pin"].get<std::string>()
            : (ref.contains("pin") ? ref["pin"].dump() : std::string());
    const std::string libid = symbol->value("libid", std::string());
    if (doc.contains("lib_cache") && doc["lib_cache"].is_object() &&
        doc["lib_cache"].contains(libid)) {
        const json& def = doc["lib_cache"][libid];
        if (def.is_object() && def.contains("pins") && def["pins"].is_array()) {
            const int unit = symbol_unit(*symbol);
            for (const json& pin : def["pins"]) {
                if (!pin.is_object() || !pin_on_unit(pin, unit)) continue;
                const std::string number = pin_number(pin);
                const std::string name =
                    pin.contains("name") && pin["name"].is_string()
                        ? pin["name"].get<std::string>()
                        : (pin.contains("name") ? pin["name"].dump() : std::string());
                if (number != requested && name != requested) continue;
                RouteEndpoint endpoint;
                endpoint.symbol = symbol;
                endpoint.symbol_id = symbol_id;
                endpoint.ref = symbol->value("ref", symbol_id);
                endpoint.pin = number.empty() ? requested : number;
                endpoint.root = pin_root_world(*symbol, pin);
                endpoint.tip = pin_tip_world(*symbol, pin);
                double dx = endpoint.tip[0] - endpoint.root[0];
                double dy = endpoint.tip[1] - endpoint.root[1];
                double length = std::hypot(dx, dy);
                if (length < 1e-9) {
                    json direction_pin = pin;
                    direction_pin["length"] = 1.0;
                    const RoutePoint direction_tip = pin_tip_world(*symbol, direction_pin);
                    dx = direction_tip[0] - endpoint.root[0];
                    dy = direction_tip[1] - endpoint.root[1];
                    length = std::hypot(dx, dy);
                }
                if (length < 1e-9) {
                    dx = 1.0;
                    dy = 0.0;
                    length = 1.0;
                }
                endpoint.escape = {endpoint.tip[0] + grid * dx / length,
                                   endpoint.tip[1] + grid * dy / length};
                endpoint.bbox = symbol_route_bbox(doc, *symbol, grid);
                return endpoint;
            }
        }
    }
    throw OpError("bad_ref",
                  "op[" + std::to_string(index) + "]: symbol '" + symbol_id +
                      "' has no pin '" + requested + "'",
                  {{"index", index}, {"reason", "no pin " + requested + " on " + symbol_id}});
}

struct OtherPin {
    std::string symbol_id;
    std::string pin;
    RoutePoint tip;
};

std::vector<OtherPin> other_pin_tips(const json& doc, const std::string& sheet) {
    std::vector<OtherPin> tips;
    if (!doc.contains("symbols") || !doc["symbols"].is_array()) return tips;
    for (const json& symbol : doc["symbols"]) {
        if (!symbol.is_object() || symbol.value("sheet", std::string()) != sheet) continue;
        const std::string libid = symbol.value("libid", std::string());
        if (!doc.contains("lib_cache") || !doc["lib_cache"].is_object() ||
            !doc["lib_cache"].contains(libid))
            continue;
        const json& def = doc["lib_cache"][libid];
        if (!def.is_object() || !def.contains("pins") || !def["pins"].is_array()) continue;
        const int unit = symbol_unit(symbol);
        for (const json& pin : def["pins"]) {
            if (!pin.is_object() || !pin_on_unit(pin, unit)) continue;
            tips.push_back({symbol.value("id", std::string()), pin_number(pin),
                            pin_tip_world(symbol, pin)});
        }
    }
    std::sort(tips.begin(), tips.end(), [](const OtherPin& a, const OtherPin& b) {
        if (a.symbol_id != b.symbol_id) return a.symbol_id < b.symbol_id;
        return a.pin < b.pin;
    });
    return tips;
}

bool same_point(const RoutePoint& a, const RoutePoint& b, double tol = 1e-6) {
    return std::abs(a[0] - b[0]) <= tol && std::abs(a[1] - b[1]) <= tol;
}

bool point_on_segment(const RoutePoint& point, const RoutePoint& a, const RoutePoint& b) {
    constexpr double tol = 1e-6;
    const double cross =
        (point[0] - a[0]) * (b[1] - a[1]) - (point[1] - a[1]) * (b[0] - a[0]);
    const double scale = std::max(1.0, std::hypot(b[0] - a[0], b[1] - a[1]));
    if (std::abs(cross) > tol * scale) return false;
    return point[0] >= std::min(a[0], b[0]) - tol &&
           point[0] <= std::max(a[0], b[0]) + tol &&
           point[1] >= std::min(a[1], b[1]) - tol &&
           point[1] <= std::max(a[1], b[1]) + tol;
}

bool segment_crosses_bbox(const RoutePoint& a, const RoutePoint& b,
                          const RouteBBox& bbox) {
    constexpr double tol = 1e-6;
    if (std::abs(a[0] - b[0]) <= tol) {
        const double x = a[0];
        if (x <= bbox.xmin + tol || x >= bbox.xmax - tol) return false;
        return std::max(std::min(a[1], b[1]), bbox.ymin) <
               std::min(std::max(a[1], b[1]), bbox.ymax) - tol;
    }
    if (std::abs(a[1] - b[1]) <= tol) {
        const double y = a[1];
        if (y <= bbox.ymin + tol || y >= bbox.ymax - tol) return false;
        return std::max(std::min(a[0], b[0]), bbox.xmin) <
               std::min(std::max(a[0], b[0]), bbox.xmax) - tol;
    }
    return true;
}

std::vector<RoutePoint> clean_route(const std::vector<RoutePoint>& points) {
    const json cleaned = orthogonalize(points);
    std::vector<RoutePoint> route;
    route.reserve(cleaned.size());
    for (const json& point : cleaned)
        route.push_back({point[0].get<double>(), point[1].get<double>()});
    return route;
}

std::vector<RoutePoint> full_route(const RouteEndpoint& a, const RouteEndpoint& b,
                                   const std::vector<RoutePoint>& core) {
    std::vector<RoutePoint> points{a.tip, a.escape};
    for (const RoutePoint& point : core)
        if (!same_point(points.back(), point)) points.push_back(point);
    if (!same_point(points.back(), b.escape)) points.push_back(b.escape);
    if (!same_point(points.back(), b.tip)) points.push_back(b.tip);
    return clean_route(points);
}

bool route_is_clear(const std::vector<RoutePoint>& core,
                    const std::vector<RoutePoint>& route,
                    const RouteEndpoint& a, const RouteEndpoint& b,
                    const std::vector<OtherPin>& pins) {
    if (segment_crosses_bbox(a.tip, a.escape, b.bbox) ||
        segment_crosses_bbox(b.tip, b.escape, a.bbox))
        return false;
    for (std::size_t i = 1; i < core.size(); ++i)
        if (segment_crosses_bbox(core[i - 1], core[i], a.bbox) ||
            segment_crosses_bbox(core[i - 1], core[i], b.bbox))
            return false;
    for (std::size_t i = 1; i < route.size(); ++i)
        for (const OtherPin& pin : pins) {
            if ((pin.symbol_id == a.symbol_id && pin.pin == a.pin) ||
                (pin.symbol_id == b.symbol_id && pin.pin == b.pin))
                continue;
            if (point_on_segment(pin.tip, route[i - 1], route[i])) return false;
        }
    return true;
}

double floor_grid(double value, double grid) {
    return std::floor(value / grid) * grid;
}

double ceil_grid(double value, double grid) {
    return std::ceil(value / grid) * grid;
}

void append_candidate(std::vector<std::vector<RoutePoint>>& candidates,
                      std::vector<RoutePoint> candidate) {
    candidate = clean_route(candidate);
    for (const auto& existing : candidates)
        if (existing == candidate) return;
    candidates.push_back(std::move(candidate));
}

void op_sch_connect(json& doc, const json& op, int index, json& id_map,
                    json& warnings) {
    require_sch(doc, index);
    if (!op.contains("from") || !op["from"].is_object() || !op.contains("to") ||
        !op["to"].is_object())
        throw invalid(index, "sch_connect requires 'from' and 'to' = {sym, pin}");
    const double grid =
        doc.contains("units") && doc["units"].is_object() &&
                doc["units"].contains("grid") && doc["units"]["grid"].is_number() &&
                doc["units"]["grid"].get<double>() > 0.0
            ? doc["units"]["grid"].get<double>()
            : 2.54;
    const RouteEndpoint a = route_endpoint(doc, op["from"], id_map, index, grid);
    const RouteEndpoint b = route_endpoint(doc, op["to"], id_map, index, grid);
    const std::string sheet =
        op.contains("sheet")
            ? op["sheet"].get<std::string>()
            : (op["from"].contains("sheet") && op["from"]["sheet"].is_string()
                   ? op["from"]["sheet"].get<std::string>()
                   : std::string("sh1"));
    const std::vector<OtherPin> pins = other_pin_tips(doc, sheet);

    const double union_xmin = std::min(a.bbox.xmin, b.bbox.xmin);
    const double union_xmax = std::max(a.bbox.xmax, b.bbox.xmax);
    const double union_ymin = std::min(a.bbox.ymin, b.bbox.ymin);
    const double union_ymax = std::max(a.bbox.ymax, b.bbox.ymax);
    const double left = floor_grid(union_xmin - grid, grid);
    const double right = ceil_grid(union_xmax + grid, grid);
    const double bottom = floor_grid(union_ymin - grid, grid);
    const double top = ceil_grid(union_ymax + grid, grid);
    const double outside_x =
        std::abs(a.escape[0] - left) + std::abs(b.escape[0] - left) <=
                std::abs(a.escape[0] - right) + std::abs(b.escape[0] - right)
            ? left
            : right;
    const double outside_y =
        std::abs(a.escape[1] - bottom) + std::abs(b.escape[1] - bottom) <=
                std::abs(a.escape[1] - top) + std::abs(b.escape[1] - top)
            ? bottom
            : top;

    std::vector<std::vector<RoutePoint>> candidates;
    if (std::abs(a.escape[0] - b.escape[0]) <= 1e-6 ||
        std::abs(a.escape[1] - b.escape[1]) <= 1e-6)
        append_candidate(candidates, {a.escape, b.escape});
    append_candidate(candidates,
                     {a.escape, {b.escape[0], a.escape[1]}, b.escape});
    append_candidate(candidates,
                     {a.escape, {a.escape[0], b.escape[1]}, b.escape});
    append_candidate(candidates,
                     {a.escape, {outside_x, a.escape[1]},
                      {outside_x, b.escape[1]}, b.escape});
    append_candidate(candidates,
                     {a.escape, {a.escape[0], outside_y},
                      {b.escape[0], outside_y}, b.escape});

    std::vector<RoutePoint> chosen;
    for (const auto& core : candidates) {
        const std::vector<RoutePoint> route = full_route(a, b, core);
        if (route_is_clear(core, route, a, b, pins)) {
            chosen = route;
            break;
        }
    }

    if (chosen.empty()) {
        const std::size_t attempts = pins.size() + 4;
        for (std::size_t step = 1; step <= attempts && chosen.empty(); ++step) {
            const double pad = grid * static_cast<double>(step);
            const std::array<std::vector<RoutePoint>, 4> around{{
                {a.escape, {floor_grid(union_xmin - pad, grid), a.escape[1]},
                 {floor_grid(union_xmin - pad, grid), b.escape[1]}, b.escape},
                {a.escape, {ceil_grid(union_xmax + pad, grid), a.escape[1]},
                 {ceil_grid(union_xmax + pad, grid), b.escape[1]}, b.escape},
                {a.escape, {a.escape[0], floor_grid(union_ymin - pad, grid)},
                 {b.escape[0], floor_grid(union_ymin - pad, grid)}, b.escape},
                {a.escape, {a.escape[0], ceil_grid(union_ymax + pad, grid)},
                 {b.escape[0], ceil_grid(union_ymax + pad, grid)}, b.escape},
            }};
            for (const auto& raw : around) {
                const std::vector<RoutePoint> core = clean_route(raw);
                const std::vector<RoutePoint> route = full_route(a, b, core);
                if (route_is_clear(core, route, a, b, pins)) {
                    chosen = route;
                    break;
                }
            }
        }
    }

    if (chosen.empty()) {
        const std::vector<RoutePoint> direct_core =
            clean_route({a.escape, {b.escape[0], a.escape[1]}, b.escape});
        chosen = full_route(a, b, direct_core);
        warnings.push_back("sch_connect_unrouted: " + a.ref + "." + a.pin + " -> " +
                           b.ref + "." + b.pin + " may cross other pins");
    }

    json points = json::array();
    for (const RoutePoint& point : chosen)
        points.push_back(json::array({point[0], point[1]}));
    const std::string wid = next_id(doc, "w");
    doc["wires"].push_back(
        {{"id", wid}, {"sheet", sheet}, {"pts", std::move(points)}, {"bus", false}});
    id_map["$" + std::to_string(index)] = wid;
}

void op_sch_annotate(json& doc, const json& op, int index) {
    require_sch(doc, index);
    std::string scope = op.value("scope", std::string("all"));
    if (scope != "all" && scope != "sheet")
        throw invalid(index, "sch_annotate.scope must be 'all' or 'sheet'");
    std::string mode = op.value("mode", std::string("keep"));
    if (mode != "keep" && mode != "reset")
        throw invalid(index, "sch_annotate.mode must be 'keep' or 'reset'");
    std::string sheet = op.value("sheet", std::string("sh1"));
    json& syms = doc["symbols"];

    auto prefix_of = [&](const json& sym) -> std::string {
        std::string ref = sym.value("ref", std::string());
        std::string base = ref;
        while (!base.empty() && base.back() == '?') base.pop_back();
        std::smatch m;
        if (std::regex_search(base, m, std::regex("^([A-Za-z_]+)"))) return m[1].str();
        return ref_prefix(doc, sym.value("libid", std::string()));
    };

    // in-scope symbol pointers (preserve doc order)
    std::vector<json*> in_scope;
    for (json& s : syms)
        if (scope == "all" || s.value("sheet", std::string()) == sheet) in_scope.push_back(&s);
    if (mode == "reset")
        for (json* s : in_scope) (*s)["ref"] = prefix_of(*s) + "?";

    std::map<std::string, std::set<int>> used;
    for (const json& s : syms) {
        std::string ref = s.value("ref", std::string());
        std::smatch m;
        if (std::regex_match(ref, m, std::regex("^([A-Za-z_]+)([0-9]+)$")))
            used[m[1].str()].insert(std::atoi(m[2].str().c_str()));
    }
    // needs: in-scope symbols whose ref ends with '?' or is empty, sorted by (x, y, id)
    std::vector<json*> needs;
    for (json* s : in_scope) {
        std::string ref = (*s).value("ref", std::string());
        if ((!ref.empty() && ref.back() == '?') || ref.empty()) needs.push_back(s);
    }
    std::sort(needs.begin(), needs.end(), [](json* a, json* b) {
        double ax = (*a).contains("pos") ? (*a)["pos"][0].get<double>() : 0.0;
        double ay = (*a).contains("pos") ? (*a)["pos"][1].get<double>() : 0.0;
        double bx = (*b).contains("pos") ? (*b)["pos"][0].get<double>() : 0.0;
        double by = (*b).contains("pos") ? (*b)["pos"][1].get<double>() : 0.0;
        if (ax != bx) return ax < bx;
        if (ay != by) return ay < by;
        return (*a).value("id", std::string()) < (*b).value("id", std::string());
    });
    for (json* s : needs) {
        std::string prefix = prefix_of(*s);
        std::set<int>& u = used[prefix];
        int n = 1;
        while (u.count(n)) ++n;
        u.insert(n);
        (*s)["ref"] = prefix + std::to_string(n);
    }
}

void op_sch_set_grid(json& doc, const json& op, int index) {
    require_sch(doc, index);
    if (!op.contains("grid") || !op["grid"].is_number() || op["grid"].is_boolean() ||
        op["grid"].get<double>() <= 0)
        throw invalid(index, "sch_set_grid requires a positive numeric 'grid' (mm)");
    if (!doc.contains("units") || !doc["units"].is_object()) doc["units"] = json::object();
    doc["units"]["grid"] = op["grid"].get<double>();
}

void op_sch_set_title_block(json& doc, const json& op, int index) {
    require_sch(doc, index);
    if (!doc.contains("title_block") || !doc["title_block"].is_object())
        doc["title_block"] = {{"title", ""},    {"company", ""},  {"rev", ""},
                              {"date", ""},     {"comment1", ""}, {"comment2", ""},
                              {"comment3", ""}, {"comment4", ""}, {"paper", "A4"}};
    json p = json::object();
    if (op.contains("patch")) {
        if (!op["patch"].is_object())
            throw invalid(index, "sch_set_title_block.patch must be an object");
        p = op["patch"];
    } else {
        for (const char* key : {"title", "company", "rev", "date", "comment1",
                                "comment2", "comment3", "comment4", "paper"})
            if (op.contains(key)) p[key] = op[key];
    }
    if (p.empty()) throw invalid(index, "sch_set_title_block requires a patch");
    for (const char* key : {"title", "company", "rev", "date", "comment1",
                            "comment2", "comment3", "comment4", "paper"}) {
        if (!p.contains(key)) continue;
        if (!p[key].is_null() && !p[key].is_string())
            throw invalid(index, std::string("title_block.") + key + " must be a string");
        doc["title_block"][key] =
            p[key].is_null() ? json(std::string(key) == "paper" ? "A4" : "") : p[key];
    }
}

void op_sch_add_sheet(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    if (!doc.contains("sheets") || !doc["sheets"].is_array()) doc["sheets"] = json::array();
    json parent = resolve_placeholder(op.contains("parent") ? op["parent"] : json("sh1"),
                                      id_map, index);
    if (!parent.is_null()) parent = require_sheet(doc, parent, index, "parent");
    const std::string shid = next_id(doc, "sh");
    if (op.contains("name") &&
        (!op["name"].is_string() || op["name"].get<std::string>().empty()))
        throw invalid(index, "sch_add_sheet.name must be non-empty");
    const std::string name =
        op.contains("name") ? op["name"].get<std::string>() : shid;
    json size_op{{"size", op.contains("size") ? op["size"] : json::array({297.0, 210.0})}};
    const std::vector<double> size = require_xy(size_op, "size", index);
    if (size[0] <= 0.0 || size[1] <= 0.0)
        throw invalid(index, "sch_add_sheet.size must be positive");
    doc["sheets"].push_back({{"id", shid}, {"name", name}, {"parent", parent},
                             {"size", size}, {"ports", json::array()}});
    id_map["$" + std::to_string(index)] = shid;
}

void op_sch_edit_sheet(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json shid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* sheet = find(doc["sheets"], shid.is_string() ? shid.get<std::string>() : std::string());
    if (!sheet)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no sheet '" + shid.dump() + "'",
                      {{"index", index}, {"reason", "unknown sheet " + shid.dump()}});
    json p = json::object();
    for (const char* key : {"name", "size"})
        if (op.contains(key)) p[key] = op[key];
    if (p.empty()) throw invalid(index, "sch_edit_sheet requires a patch");
    if (p.contains("name") &&
        (!p["name"].is_string() || p["name"].get<std::string>().empty()))
        throw invalid(index, "sch_edit_sheet.name must be non-empty");
    if (p.contains("size")) {
        p["size"] = require_xy(op, "size", index);
        if (p["size"][0].get<double>() <= 0.0 || p["size"][1].get<double>() <= 0.0)
            throw invalid(index, "sch_edit_sheet.size must be positive");
    }
    patch(*sheet, p);
}

void op_sch_delete_sheet(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json shid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    const std::string id = shid.is_string() ? shid.get<std::string>() : std::string();
    if (!find(doc["sheets"], id))
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no sheet '" + id + "'",
                      {{"index", index}, {"reason", "unknown sheet " + id}});
    if (doc["sheets"].size() <= 1)
        throw invalid(index, "cannot delete the last schematic sheet");
    erase_id(doc["sheets"], id);
    for (json& sheet : doc["sheets"])
        if (sheet.is_object() && sheet.contains("parent") && sheet["parent"].is_string() &&
            sheet["parent"].get<std::string>() == id)
            sheet["parent"] = nullptr;
    for (const char* key : {"symbols", "wires", "junctions", "labels", "buses",
                            "bus_entries", "power", "no_connect", "texts", "graphics"}) {
        if (!doc.contains(key) || !doc[key].is_array()) continue;
        json kept = json::array();
        for (const json& item : doc[key])
            if (!item.is_object() || item.value("sheet", std::string()) != id)
                kept.push_back(item);
        doc[key] = std::move(kept);
    }
    if (doc.contains("sheet_symbols") && doc["sheet_symbols"].is_array()) {
        json kept = json::array();
        for (const json& symbol : doc["sheet_symbols"])
            if (!symbol.is_object() ||
                (symbol.value("sheet", std::string()) != id &&
                 symbol.value("child", std::string()) != id))
                kept.push_back(symbol);
        doc["sheet_symbols"] = std::move(kept);
    }
}

json checked_sheet_pin(const json& source, int index, const std::string& context) {
    if (!source.contains("name") || !source["name"].is_string() ||
        source["name"].get<std::string>().empty())
        throw invalid(index, context + ".name must be a non-empty string");
    if (source.contains("shape") && !source["shape"].is_string())
        throw invalid(index, context + ".shape must be a string");
    const std::string shape =
        source.contains("shape") ? source["shape"].get<std::string>() : std::string("passive");
    if (!schema::sheet_pin_shapes().count(shape))
        throw invalid(index, context +
                                 ".shape must be one of [bidirectional, input, output, passive, "
                                 "tri_state]");
    return json{{"name", source["name"]},
                {"pos", require_xy(source, "pos", index)},
                {"shape", shape}};
}

void op_sch_add_sheet_symbol(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json child =
        resolve_placeholder(op.contains("child") ? op["child"] : json(), id_map, index);
    const std::string child_id = require_sheet(doc, child, index, "child");
    const std::string id = next_id(doc, "ss");
    json pins = json::array();
    std::set<std::string> names;
    if (op.contains("pins")) {
        if (!op["pins"].is_array()) throw invalid(index, "pins must be a list");
        int pin_index = 0;
        for (const json& source : op["pins"]) {
            if (!source.is_object())
                throw invalid(index, "pins[" + std::to_string(pin_index) + "] must be an object");
            json pin = checked_sheet_pin(source, index,
                                         "pins[" + std::to_string(pin_index) + "]");
            const std::string name = pin["name"];
            if (!names.insert(name).second)
                throw invalid(index, "duplicate sheet pin '" + name + "'");
            pins.push_back(std::move(pin));
            ++pin_index;
        }
    }
    json symbol{{"id", id},
                {"sheet", require_sheet(doc, op.contains("sheet") ? op["sheet"] : json("sh1"),
                                         index)},
                {"child", child_id},
                {"pos", require_xy(op, "pos", index)},
                {"pins", std::move(pins)}};
    if (op.contains("size")) symbol["size"] = require_xy(op, "size", index);
    doc["sheet_symbols"].push_back(std::move(symbol));
    id_map["$" + std::to_string(index)] = id;
}

void op_sch_edit_sheet_symbol(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json ssid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* symbol =
        find(doc["sheet_symbols"], ssid.is_string() ? ssid.get<std::string>() : std::string());
    if (!symbol)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no sheet symbol '" + ssid.dump() + "'",
                      {{"index", index}, {"reason", "unknown sheet symbol " + ssid.dump()}});
    json p = json::object();
    for (const char* key : {"pos", "child", "size"})
        if (op.contains(key)) p[key] = op[key];
    if (p.empty()) throw invalid(index, "sch_edit_sheet_symbol requires a patch");
    if (p.contains("pos")) p["pos"] = require_xy(op, "pos", index);
    if (p.contains("size")) p["size"] = require_xy(op, "size", index);
    if (p.contains("child")) {
        p["child"] = resolve_placeholder(p["child"], id_map, index);
        p["child"] = require_sheet(doc, p["child"], index, "child");
    }
    patch(*symbol, p);
}

void op_sch_delete_sheet_symbol(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json ssid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    const std::string id = ssid.is_string() ? ssid.get<std::string>() : std::string();
    if (!erase_id(doc["sheet_symbols"], id))
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no sheet symbol '" + id + "'",
                      {{"index", index}, {"reason", "unknown sheet symbol " + id}});
}

void op_sch_add_sheet_pin(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json ssid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* symbol =
        find(doc["sheet_symbols"], ssid.is_string() ? ssid.get<std::string>() : std::string());
    if (!symbol)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no sheet symbol '" + ssid.dump() + "'",
                      {{"index", index}, {"reason", "unknown sheet symbol " + ssid.dump()}});
    if (!symbol->contains("pins") || !(*symbol)["pins"].is_array())
        (*symbol)["pins"] = json::array();
    json pin = checked_sheet_pin(op, index, "sch_add_sheet_pin");
    const std::string name = pin["name"];
    for (const json& existing : (*symbol)["pins"])
        if (existing.is_object() && existing.value("name", std::string()) == name)
            throw OpError("duplicate_ref",
                          "op[" + std::to_string(index) + "]: sheet pin '" + name +
                              "' already exists",
                          {{"index", index}, {"reason", "duplicate sheet pin " + name}});
    (*symbol)["pins"].push_back(std::move(pin));
}

json* find_sheet_pin(json& symbol, const std::string& name) {
    if (!symbol.contains("pins") || !symbol["pins"].is_array()) return nullptr;
    for (json& pin : symbol["pins"])
        if (pin.is_object() && pin.value("name", std::string()) == name) return &pin;
    return nullptr;
}

void op_sch_edit_sheet_pin(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json ssid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* symbol =
        find(doc["sheet_symbols"], ssid.is_string() ? ssid.get<std::string>() : std::string());
    if (!symbol)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no sheet symbol '" + ssid.dump() + "'",
                      {{"index", index}, {"reason", "unknown sheet symbol " + ssid.dump()}});
    if (!op.contains("pin") || !op["pin"].is_string())
        throw invalid(index, "sch_edit_sheet_pin requires a 'pin' name");
    const std::string old_name = op["pin"];
    json* pin = find_sheet_pin(*symbol, old_name);
    if (!pin)
        throw OpError("bad_ref",
                      "op[" + std::to_string(index) + "]: no sheet pin '" + old_name + "'",
                      {{"index", index}, {"reason", "unknown sheet pin " + old_name}});
    json candidate = *pin;
    json p = json::object();
    for (const char* key : {"name", "pos", "shape"})
        if (op.contains(key)) p[key] = op[key];
    if (p.empty()) throw invalid(index, "sch_edit_sheet_pin requires a patch");
    patch(candidate, p);
    candidate = checked_sheet_pin(candidate, index, "sch_edit_sheet_pin");
    const std::string new_name = candidate["name"];
    if (new_name != old_name && find_sheet_pin(*symbol, new_name))
        throw OpError("duplicate_ref",
                      "op[" + std::to_string(index) + "]: sheet pin '" + new_name +
                          "' already exists",
                      {{"index", index}, {"reason", "duplicate sheet pin " + new_name}});
    *pin = std::move(candidate);
}

void op_sch_delete_sheet_pin(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    const json ssid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* symbol =
        find(doc["sheet_symbols"], ssid.is_string() ? ssid.get<std::string>() : std::string());
    if (!symbol)
        throw OpError("unknown_object",
                      "op[" + std::to_string(index) + "]: no sheet symbol '" + ssid.dump() + "'",
                      {{"index", index}, {"reason", "unknown sheet symbol " + ssid.dump()}});
    if (!op.contains("pin") || !op["pin"].is_string())
        throw invalid(index, "sch_delete_sheet_pin requires a 'pin' name");
    const std::string name = op["pin"];
    if (!find_sheet_pin(*symbol, name))
        throw OpError("bad_ref",
                      "op[" + std::to_string(index) + "]: no sheet pin '" + name + "'",
                      {{"index", index}, {"reason", "unknown sheet pin " + name}});
    json kept = json::array();
    for (const json& pin : (*symbol)["pins"])
        if (!pin.is_object() || pin.value("name", std::string()) != name)
            kept.push_back(pin);
    (*symbol)["pins"] = std::move(kept);
}

void op_sch_import_kicad(json& doc, const json& op, int index, json& id_map,
                         json& warnings) {
    require_sch(doc, index);
    if (!op.contains("path") || !op["path"].is_string() ||
        op["path"].get<std::string>().empty())
        throw invalid(index, "sch_import_kicad requires a non-empty 'path'");
    if (op.contains("mode") && !op["mode"].is_string())
        throw invalid(index, "sch_import_kicad.mode must be 'merge' or 'replace'");
    const std::string mode =
        op.contains("mode") ? op["mode"].get<std::string>() : std::string("merge");
    if (mode != "merge" && mode != "replace")
        throw invalid(index, "sch_import_kicad.mode must be 'merge' or 'replace'");

    json imported;
    try {
        imported = kicad::import_kicad_sch(op["path"].get<std::string>());
    } catch (const std::exception& exc) {
        throw OpError("io_error",
                      "op[" + std::to_string(index) + "]: cannot import '" +
                          op["path"].get<std::string>() + "': " + exc.what(),
                      {{"index", index}, {"reason", exc.what()}});
    }
    if (!schema::is_schematic(imported))
        throw OpError("io_error",
                      "op[" + std::to_string(index) +
                          "]: importer did not return a schematic document",
                      {{"index", index}, {"reason", "invalid imported schematic"}});
    if (imported.contains("warnings") && imported["warnings"].is_array())
        for (const json& warning : imported["warnings"])
            warnings.push_back(warning.is_string() ? warning.get<std::string>() : warning.dump());

    struct EntitySpec {
        const char* key;
        const char* prefix;
    };
    const std::array<EntitySpec, 12> specs{{
        {"sheets", "sh"},      {"symbols", "s"},       {"wires", "w"},
        {"junctions", "j"},    {"labels", "l"},        {"buses", "b"},
        {"bus_entries", "be"}, {"power", "p"},         {"no_connect", "nc"},
        {"texts", "tx"},       {"graphics", "g"},      {"sheet_symbols", "ss"},
    }};
    json allocator = doc;
    std::map<std::string, std::string> remap;
    for (const EntitySpec& spec : specs) {
        if (!imported.contains(spec.key) || !imported[spec.key].is_array())
            imported[spec.key] = json::array();
        if (!allocator.contains(spec.key) || !allocator[spec.key].is_array())
            allocator[spec.key] = json::array();
        for (json& item : imported[spec.key]) {
            if (!item.is_object()) continue;
            const std::string old_id = item.value("id", std::string());
            const std::string new_id = next_id(allocator, spec.prefix);
            allocator[spec.key].push_back({{"id", new_id}});
            item["id"] = new_id;
            if (!old_id.empty()) {
                remap[old_id] = new_id;
                id_map[old_id] = new_id;
            }
        }
    }
    for (const EntitySpec& spec : specs)
        for (json& item : imported[spec.key]) {
            if (!item.is_object()) continue;
            for (const char* ref_key : {"sheet", "parent", "child"}) {
                if (!item.contains(ref_key) || !item[ref_key].is_string()) continue;
                const auto mapped = remap.find(item[ref_key].get<std::string>());
                if (mapped != remap.end()) item[ref_key] = mapped->second;
            }
        }

    if (!doc.contains("lib_cache") || !doc["lib_cache"].is_object())
        doc["lib_cache"] = json::object();
    if (imported.contains("lib_cache") && imported["lib_cache"].is_object())
        for (auto it = imported["lib_cache"].begin(); it != imported["lib_cache"].end(); ++it)
            doc["lib_cache"][it.key()] = it.value();

    for (const EntitySpec& spec : specs) {
        if (mode == "replace") {
            doc[spec.key] = imported[spec.key];
        } else {
            if (!doc.contains(spec.key) || !doc[spec.key].is_array())
                doc[spec.key] = json::array();
            for (json& item : imported[spec.key])
                doc[spec.key].push_back(std::move(item));
        }
    }
    if (mode == "replace" && imported.contains("title_block") &&
        imported["title_block"].is_object())
        doc["title_block"] = imported["title_block"];
}

// ----------------------------------------------------------------- PCB ops
double default_track_width(const json& doc, const json& net) {
    json classes = doc.contains("net_classes") ? doc["net_classes"] : json::object();
    std::string cls_name = "default";
    if (doc.contains("nets") && doc["nets"].is_array())
        for (const json& n : doc["nets"])
            if (n.is_object() && n.contains("name") && net.is_string() &&
                n["name"] == net) {
                cls_name = n.value("class", std::string("default"));
                break;
            }
    json cls = classes.contains(cls_name) ? classes[cls_name]
               : (classes.contains("default") ? classes["default"] : json::object());
    return cls.is_object() && cls.contains("track_width") && cls["track_width"].is_number()
               ? cls["track_width"].get<double>()
               : 0.25;
}

bool outline_closes(const json& shapes, double tol = 1e-3) {
    if (!shapes.is_array() || shapes.empty()) return false;
    std::vector<std::array<double, 2>> pts;
    for (const json& s : shapes) {
        std::string t = s.value("type", std::string());
        if (t == "circle") continue;
        if (t == "line") {
            pts.push_back({s["p1"][0].get<double>(), s["p1"][1].get<double>()});
            pts.push_back({s["p2"][0].get<double>(), s["p2"][1].get<double>()});
        } else if (t == "arc") {
            double cx = s["center"][0].get<double>(), cy = s["center"][1].get<double>();
            double r = s["radius"].get<double>();
            double a0 = s["a0"].get<double>() * 3.14159265358979323846 / 180.0;
            double a1 = s["a1"].get<double>() * 3.14159265358979323846 / 180.0;
            pts.push_back({cx + r * std::cos(a0), cy + r * std::sin(a0)});
            pts.push_back({cx + r * std::cos(a1), cy + r * std::sin(a1)});
        } else {
            return false;
        }
    }
    if (pts.empty()) return true;
    std::vector<bool> matched(pts.size(), false);
    for (size_t i = 0; i < pts.size(); ++i) {
        if (matched[i]) continue;
        for (size_t j = 0; j < pts.size(); ++j) {
            if (i == j || matched[j]) continue;
            if (std::abs(pts[i][0] - pts[j][0]) < tol && std::abs(pts[i][1] - pts[j][1]) < tol) {
                matched[i] = matched[j] = true;
                break;
            }
        }
    }
    for (bool m : matched) if (!m) return false;
    return true;
}

void op_pcb_set_stackup(json& doc, const json& op, int index) {
    require_pcb(doc, index);
    if (!doc.contains("stackup") || !doc["stackup"].is_object()) doc["stackup"] = json::object();
    json& stack = doc["stackup"];
    if (op.contains("copper_layers")) {
        if (!op["copper_layers"].is_number_integer() || op["copper_layers"].is_boolean())
            throw invalid(index, "copper_layers must be 1..32");
        int n = op["copper_layers"].get<int>();
        if (n < 1 || n > schema::kCopperLayersMax) throw invalid(index, "copper_layers must be 1..32");
        if (n != 1 && n % 2 == 1) throw invalid(index, "copper_layers must be 1 or an even count");
        stack["copper_layers"] = n;
    }
    if (op.contains("thickness") && !op["thickness"].is_null())
        stack["thickness"] = op["thickness"].get<double>();
    if (op.contains("layers") && !op["layers"].is_null()) stack["layers"] = op["layers"];
}

void op_pcb_set_outline(json& doc, const json& op, int index) {
    require_pcb(doc, index);
    if (!op.contains("shapes") || !op["shapes"].is_array())
        throw invalid(index, "pcb_set_outline requires a 'shapes' list");
    bool closed = op.value("closed", true);
    if (closed && !outline_closes(op["shapes"]))
        throw OpError("outline_not_closed",
                      "op[" + std::to_string(index) + "]: outline shapes do not form a closed loop",
                      {{"index", index}, {"reason", "outline not closed"}});
    doc["outline"] = {{"shapes", op["shapes"]}, {"closed", closed}};
}

json resolve_fp_def(const json&, const std::string& fpid, const json& caller_def) {
    if (caller_def.is_object()) return caller_def;
    return json{{"fpid", fpid}, {"pads", json::array()}};
}

void op_pcb_add_footprint(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    if (!op.contains("fpid") || !op["fpid"].is_string() || op["fpid"].get<std::string>().empty())
        throw invalid(index, "pcb_add_footprint requires an 'fpid' string");
    std::string fpid = op["fpid"];
    std::string side = op.value("side", std::string("top"));
    if (!schema::fp_sides().count(side)) throw invalid(index, "side must be one of [bottom, top]");
    json fpdef = resolve_fp_def(doc, fpid, op.contains("def") ? op["def"] : json());
    std::string fid = next_id(doc, "fp");
    json pads = (op.contains("pads") && !op["pads"].is_null())
                    ? op["pads"]
                    : (fpdef.contains("pads") ? fpdef["pads"] : json::array());
    json fp = {{"id", fid}, {"fpid", fpid}, {"ref", op.value("ref", std::string())},
               {"value", op.contains("value") ? op["value"] : fpdef.value("value", json(""))},
               {"pos", require_xy(op, "pos", index)},
               {"angle", op.contains("angle") ? op["angle"].get<double>() : 0.0},
               {"side", side}, {"pads", pads},
               {"graphics", fpdef.contains("graphics") ? fpdef["graphics"] : json::array()},
               {"attr", fpdef.value("attr", std::string("smd"))}};
    if (op.contains("linked_symbol") && !op["linked_symbol"].is_null())
        fp["linked_symbol"] = resolve_placeholder(op["linked_symbol"], id_map, index);
    doc["footprints"].push_back(fp);
    id_map["$" + std::to_string(index)] = fid;
}

void op_pcb_edit_footprint(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    json fid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* fp = find(doc["footprints"], fid.is_string() ? fid.get<std::string>() : std::string());
    if (!fp)
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no footprint '" +
                                            fid.dump() + "'",
                      {{"index", index}, {"reason", "unknown footprint " + fid.dump()}});
    if (op.contains("side") && !op["side"].is_null() &&
        !schema::fp_sides().count(op["side"].get<std::string>()))
        throw invalid(index, "side must be one of [bottom, top]");
    json p = json::object();
    for (const char* k : {"pos", "angle", "side", "ref", "value"})
        if (op.contains(k)) p[k] = op[k];
    patch(*fp, p);
}

void op_pcb_delete_footprint(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    json fid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    std::string f = fid.is_string() ? fid.get<std::string>() : std::string();
    if (!find(doc["footprints"], f))
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no footprint '" + f + "'",
                      {{"index", index}, {"reason", "unknown footprint " + f}});
    json kept = json::array();
    for (const json& x : doc["footprints"])
        if (x.value("id", std::string()) != f) kept.push_back(x);
    doc["footprints"] = kept;
}

void check_copper_layer(const json& doc, const json& layer, int index, const char* what) {
    int n = doc.contains("stackup") && doc["stackup"].contains("copper_layers") &&
                    doc["stackup"]["copper_layers"].is_number_integer()
                ? doc["stackup"]["copper_layers"].get<int>()
                : 2;
    std::vector<std::string> cu = schema::copper_layer_ids(n);
    std::string l = layer.is_string() ? layer.get<std::string>() : layer.dump();
    bool ok = false;
    for (const std::string& c : cu) if (c == l) ok = true;
    if (!ok)
        throw OpError("layer_not_in_stackup",
                      "op[" + std::to_string(index) + "]: " + what + " layer '" + l +
                          "' is not a copper layer in the stackup",
                      {{"index", index}, {"reason", "layer '" + l + "' not a copper layer"}});
}

void op_pcb_add_track(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    check_copper_layer(doc, op.contains("layer") ? op["layer"] : json(), index, "track");
    json net = op.contains("net") ? op["net"] : json(nullptr);
    require_net(doc, net, index);
    std::string kind = op.value("kind", std::string("segment"));
    if (!schema::track_kinds().count(kind)) throw invalid(index, "kind must be one of [arc, segment]");
    double width = (op.contains("width") && !op["width"].is_null()) ? op["width"].get<double>()
                                                                    : default_track_width(doc, net);
    std::string tid = next_id(doc, "t");
    json track = {{"id", tid}, {"net", net}, {"layer", op["layer"]}, {"width", width}, {"kind", kind},
                  {"p1", require_xy(op, "p1", index)}, {"p2", require_xy(op, "p2", index)}};
    if (kind == "arc") {
        track["center"] = require_xy(op, "center", index);
        track["a0"] = op.contains("a0") ? op["a0"].get<double>() : 0.0;
        track["a1"] = op.contains("a1") ? op["a1"].get<double>() : 0.0;
    }
    doc["tracks"].push_back(track);
    id_map["$" + std::to_string(index)] = tid;
}

void op_pcb_add_tracks(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    check_copper_layer(doc, op.contains("layer") ? op["layer"] : json(), index, "track");
    json net = op.contains("net") ? op["net"] : json(nullptr);
    require_net(doc, net, index);
    const json& pts = op.contains("pts") ? op["pts"] : json();
    bool ok = pts.is_array() && pts.size() >= 2;
    if (ok) for (const json& p : pts) ok = ok && p.is_array() && p.size() == 2;
    if (!ok) throw invalid(index, "pcb_add_tracks requires 'pts': >=2 [x,y] points");
    double width = (op.contains("width") && !op["width"].is_null()) ? op["width"].get<double>()
                                                                    : default_track_width(doc, net);
    std::vector<std::array<double, 2>> raw;
    for (const json& p : pts) raw.push_back({p[0].get<double>(), p[1].get<double>()});
    json clean = orthogonalize(raw);
    if (clean.size() < 2) throw invalid(index, "pcb_add_tracks: degenerate (all points coincident)");
    std::vector<std::string> new_ids;
    for (size_t i = 0; i + 1 < clean.size(); ++i) {
        std::string tid = next_id(doc, "t");
        new_ids.push_back(tid);
        doc["tracks"].push_back({{"id", tid}, {"net", net}, {"layer", op["layer"]}, {"width", width},
                                 {"kind", "segment"}, {"p1", clean[i]}, {"p2", clean[i + 1]}});
    }
    if (!new_ids.empty()) {
        id_map["$" + std::to_string(index)] = new_ids[0];
        for (size_t k = 1; k < new_ids.size(); ++k)
            id_map["$" + std::to_string(index) + "#" + std::to_string(k)] = new_ids[k];
    }
}

void op_pcb_delete_track(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    json tid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    std::string t = tid.is_string() ? tid.get<std::string>() : std::string();
    if (!find(doc["tracks"], t))
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no track '" + t + "'",
                      {{"index", index}, {"reason", "unknown track " + t}});
    json kept = json::array();
    for (const json& x : doc["tracks"])
        if (x.value("id", std::string()) != t) kept.push_back(x);
    doc["tracks"] = kept;
}

void op_pcb_add_via(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    json net = op.contains("net") ? op["net"] : json(nullptr);
    require_net(doc, net, index);
    std::string vtype = op.value("type", std::string("through"));
    if (!schema::via_types().count(vtype))
        throw invalid(index, "via type must be one of [blind, buried, micro, through]");
    int n = doc.contains("stackup") && doc["stackup"].contains("copper_layers") &&
                    doc["stackup"]["copper_layers"].is_number_integer()
                ? doc["stackup"]["copper_layers"].get<int>()
                : 2;
    std::vector<std::string> cu = schema::copper_layer_ids(n);
    json layers = (op.contains("layers") && !op["layers"].is_null() && !op["layers"].empty())
                      ? op["layers"]
                      : json::array({cu.front(), cu.back()});
    for (const json& lyr : layers) {
        std::string l = lyr.is_string() ? lyr.get<std::string>() : lyr.dump();
        bool ok = false;
        for (const std::string& c : cu) if (c == l) ok = true;
        if (!ok)
            throw OpError("layer_not_in_stackup",
                          "op[" + std::to_string(index) + "]: via layer '" + l + "' is not a copper layer",
                          {{"index", index}, {"reason", "layer '" + l + "' not a copper layer"}});
    }
    std::string vid = next_id(doc, "v");
    json cls = (doc.contains("net_classes") && doc["net_classes"].is_object() &&
                doc["net_classes"].contains("default"))
                   ? doc["net_classes"]["default"]
                   : json::object();
    double drill = op.contains("drill") ? op["drill"].get<double>()
                                        : (cls.contains("via_drill") ? cls["via_drill"].get<double>() : 0.4);
    double diameter = op.contains("diameter")
                          ? op["diameter"].get<double>()
                          : (cls.contains("via_diameter") ? cls["via_diameter"].get<double>() : 0.8);
    doc["vias"].push_back({{"id", vid}, {"net", net}, {"at", require_xy(op, "at", index)},
                           {"drill", drill}, {"diameter", diameter}, {"layers", layers},
                           {"type", vtype}});
    id_map["$" + std::to_string(index)] = vid;
}

void op_pcb_delete_via(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    json vid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    std::string v = vid.is_string() ? vid.get<std::string>() : std::string();
    if (!find(doc["vias"], v))
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no via '" + v + "'",
                      {{"index", index}, {"reason", "unknown via " + v}});
    json kept = json::array();
    for (const json& x : doc["vias"])
        if (x.value("id", std::string()) != v) kept.push_back(x);
    doc["vias"] = kept;
}

void op_pcb_add_zone(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    if (!op.contains("net") || !op["net"].is_string() || op["net"].get<std::string>().empty())
        throw invalid(index, "pcb_add_zone requires a 'net' name");
    json net = op["net"];
    require_net(doc, net, index);
    check_copper_layer(doc, op.contains("layer") ? op["layer"] : json(), index, "zone");
    const json& poly = op.contains("polygon") ? op["polygon"] : json();
    bool ok = poly.is_array() && poly.size() >= 3;
    if (ok) for (const json& p : poly) ok = ok && p.is_array() && p.size() == 2;
    if (!ok) throw invalid(index, "pcb_add_zone requires 'polygon': >=3 [x,y] points");
    std::string zid = next_id(doc, "z");
    json polyout = json::array();
    for (const json& p : poly) polyout.push_back(json::array({p[0].get<double>(), p[1].get<double>()}));
    json zone = {{"id", zid}, {"net", net}, {"layer", op["layer"]}, {"polygon", polyout},
                 {"clearance", op.contains("clearance") ? op["clearance"].get<double>() : 0.3},
                 {"fill", op.value("fill", std::string("solid"))}};
    if (!schema::zone_fills().count(zone["fill"].get<std::string>()))
        throw invalid(index, "zone fill must be one of [hatch, solid]");
    if (op.contains("thermal") && !op["thermal"].is_null()) zone["thermal"] = op["thermal"];
    doc["zones"].push_back(zone);
    id_map["$" + std::to_string(index)] = zid;
}

void op_pcb_edit_zone(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    json zid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* zone = find(doc["zones"], zid.is_string() ? zid.get<std::string>() : std::string());
    if (!zone)
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no zone '" + zid.dump() + "'",
                      {{"index", index}, {"reason", "unknown zone " + zid.dump()}});
    json p = json::object();
    if (op.contains("patch") && op["patch"].is_object()) {
        p = op["patch"];
    } else {
        for (const char* k : {"net", "layer", "polygon", "clearance", "fill", "thermal"})
            if (op.contains(k)) p[k] = op[k];
    }
    if (p.contains("net") && !p["net"].is_null()) require_net(doc, p["net"], index);
    if (p.contains("fill") && !p["fill"].is_null() &&
        !schema::zone_fills().count(p["fill"].get<std::string>()))
        throw invalid(index, "zone fill must be one of [hatch, solid]");
    zone->erase("filled_polys");
    patch(*zone, p);
}

void op_pcb_delete_zone(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    json zid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    std::string z = zid.is_string() ? zid.get<std::string>() : std::string();
    if (!find(doc["zones"], z))
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no zone '" + z + "'",
                      {{"index", index}, {"reason", "unknown zone " + z}});
    json kept = json::array();
    for (const json& x : doc["zones"])
        if (x.value("id", std::string()) != z) kept.push_back(x);
    doc["zones"] = kept;
}

void op_pcb_set_rules(json& doc, const json& op, int index) {
    require_pcb(doc, index);
    if (op.contains("net_classes") && op["net_classes"].is_object()) {
        if (!doc.contains("net_classes") || !doc["net_classes"].is_object())
            doc["net_classes"] = json::object();
        json& nc = doc["net_classes"];
        for (auto it = op["net_classes"].begin(); it != op["net_classes"].end(); ++it) {
            if (it.value().is_null())
                nc.erase(it.key());
            else
                nc[it.key()] = it.value();
        }
    }
    json p = json::object();
    if (op.contains("patch") && op["patch"].is_object()) {
        p = op["patch"];
    } else {
        for (auto it = op.begin(); it != op.end(); ++it)
            if (it.key() != "op" && it.key() != "net_classes" && it.key() != "patch")
                p[it.key()] = it.value();
    }
    if (!p.empty()) {
        if (!doc.contains("rules") || !doc["rules"].is_object()) doc["rules"] = json::object();
        patch(doc["rules"], p);
    }
}

void op_pcb_assign_net(json& doc, const json& op, int index, json& id_map) {
    require_pcb(doc, index);
    if (!op.contains("pad") || !op["pad"].is_object() || !op["pad"].contains("fp") ||
        !op["pad"].contains("pad"))
        throw invalid(index, "pcb_assign_net requires 'pad': {fp, pad}");
    json net = op.contains("net") ? op["net"] : json(nullptr);
    require_net(doc, net, index);
    json fid = resolve_placeholder(op["pad"]["fp"], id_map, index);
    json* fp = find(doc["footprints"], fid.is_string() ? fid.get<std::string>() : std::string());
    if (!fp)
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no footprint '" +
                                            fid.dump() + "'",
                      {{"index", index}, {"reason", "unknown footprint " + fid.dump()}});
    std::string pad_no = op["pad"]["pad"].is_string() ? op["pad"]["pad"].get<std::string>()
                                                      : op["pad"]["pad"].dump();
    if (fp->contains("pads") && (*fp)["pads"].is_array())
        for (json& pad : (*fp)["pads"]) {
            std::string num = pad.contains("number") && pad["number"].is_string()
                                  ? pad["number"].get<std::string>()
                                  : (pad.contains("number") ? pad["number"].dump() : "");
            std::string pid = pad.contains("id") && pad["id"].is_string()
                                  ? pad["id"].get<std::string>()
                                  : (pad.contains("id") ? pad["id"].dump() : "");
            if (num == pad_no || pid == pad_no) {
                pad["net"] = net;
                return;
            }
        }
    throw OpError("bad_ref", "op[" + std::to_string(index) + "]: footprint '" +
                                 (fid.is_string() ? fid.get<std::string>() : std::string()) +
                                 "' has no pad '" + pad_no + "'",
                  {{"index", index}, {"reason", "no pad " + pad_no}});
}

void op_pcb_update_from_sch(json& doc, const json& op, int index, json& warnings) {
    require_pcb(doc, index);
    if (!op.contains("sch_doc_id") || !op["sch_doc_id"].is_string() ||
        op["sch_doc_id"].get<std::string>().empty())
        throw invalid(index, "pcb_update_from_sch requires a 'sch_doc_id'");
    std::string sch_id = op["sch_doc_id"];
    doc["linked_sch"] = sch_id;
    warnings.push_back("op[" + std::to_string(index) + "]: pcb_update_from_sch recorded linked_sch='" +
                       sch_id + "' but is a best-effort stub — the netlist-driven reconciliation runs "
                       "at the session/worker layer where the schematic doc + netlist "
                       "are available");
}

void op_pcb_autoplace(json& doc, const json& op, int index, json& warnings) {
    require_pcb(doc, index);
    double ox = doc.contains("origin") ? doc["origin"][0].get<double>() : 0.0;
    double oy = doc.contains("origin") ? doc["origin"][1].get<double>() : 0.0;
    json area = (op.contains("area") && !op["area"].is_null())
                    ? op["area"]
                    : json::array({ox, oy, 100.0, 100.0});
    double x0 = area[0].get<double>(), y0 = area[1].get<double>(), w = area[2].get<double>();
    double pitch = 12.7;
    int cols = std::max(1, (int)(w / pitch));
    // unplaced = pos == [0,0], sorted by id
    std::vector<json*> unplaced;
    if (doc.contains("footprints") && doc["footprints"].is_array())
        for (json& f : doc["footprints"]) {
            bool at_origin = f.contains("pos") && f["pos"].is_array() && f["pos"].size() == 2 &&
                             f["pos"][0].get<double>() == 0.0 && f["pos"][1].get<double>() == 0.0;
            if (at_origin) unplaced.push_back(&f);
        }
    std::sort(unplaced.begin(), unplaced.end(),
              [](json* a, json* b) { return (*a).value("id", std::string()) < (*b).value("id", std::string()); });
    for (size_t i = 0; i < unplaced.size(); ++i)
        (*unplaced[i])["pos"] = json::array({x0 + (int)(i % cols) * pitch, y0 + (int)(i / cols) * pitch});
    if (!unplaced.empty())
        warnings.push_back("op[" + std::to_string(index) + "]: pcb_autoplace placed " +
                           std::to_string(unplaced.size()) +
                           " footprint(s) on a grid (heuristic; connectivity-aware placement is a "
                           "follow-up)");
}

// ----------------------------------------------------------------- shared ops
void op_rename_doc(json& doc, const json& op, int index) {
    if (!op.contains("name") || !op["name"].is_string() || op["name"].get<std::string>().empty())
        throw invalid(index, "rename_doc requires a non-empty 'name'");
    doc["name"] = op["name"];
}

void op_set_field(json& doc, const json& op, int index, json& id_map) {
    if (!op.contains("target") || !op["target"].is_object() || !op.contains("key") ||
        !op["key"].is_string() || op["key"].get<std::string>().empty())
        throw invalid(index, "set_field requires 'target':{kind,id} and a string 'key'");
    const json& target = op["target"];
    std::string key = op["key"];
    json oid = resolve_placeholder(target.contains("id") ? target["id"] : json(), id_map, index);
    std::string list_key = target.contains("kind") && target["kind"].is_string()
                               ? target["kind"].get<std::string>()
                               : std::string();
    json* obj = nullptr;
    if (!list_key.empty() && doc.contains(list_key))
        obj = find(doc[list_key], oid.is_string() ? oid.get<std::string>() : std::string());
    if (!obj)
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: target '" + list_key +
                                            "'/'" + oid.dump() + "' not found",
                      {{"index", index}, {"reason", "unknown target " + list_key + "/" + oid.dump()}});
    if (!obj->contains("fields") || !(*obj)["fields"].is_object()) (*obj)["fields"] = json::object();
    if (!op.contains("value") || op["value"].is_null())
        (*obj)["fields"].erase(key);
    else
        (*obj)["fields"][key] = op["value"];
}

// ----------------------------------------------------------------- dispatch
void apply_one(json& doc, const json& op, int index, json& id_map, json& warnings) {
    if (!op.is_object()) throw invalid(index, "op must be an object");
    if (!op.contains("op") || !op["op"].is_string())
        throw invalid(index, "op.op (the op name) is required");
    std::string k = op["op"];

    if (k == "nop") { /* transactional no-op */ }
    else if (k == "pcb_add_footprint")    op_pcb_add_footprint(doc, op, index, id_map);
    else if (k == "pcb_add_track")        op_pcb_add_track(doc, op, index, id_map);
    else if (k == "pcb_add_tracks")       op_pcb_add_tracks(doc, op, index, id_map);
    else if (k == "pcb_add_via")          op_pcb_add_via(doc, op, index, id_map);
    else if (k == "pcb_add_zone")         op_pcb_add_zone(doc, op, index, id_map);
    else if (k == "pcb_assign_net")       op_pcb_assign_net(doc, op, index, id_map);
    else if (k == "pcb_autoplace")        op_pcb_autoplace(doc, op, index, warnings);
    else if (k == "pcb_delete_footprint") op_pcb_delete_footprint(doc, op, index, id_map);
    else if (k == "pcb_delete_track")     op_pcb_delete_track(doc, op, index, id_map);
    else if (k == "pcb_delete_via")       op_pcb_delete_via(doc, op, index, id_map);
    else if (k == "pcb_delete_zone")      op_pcb_delete_zone(doc, op, index, id_map);
    else if (k == "pcb_edit_footprint")   op_pcb_edit_footprint(doc, op, index, id_map);
    else if (k == "pcb_edit_zone")        op_pcb_edit_zone(doc, op, index, id_map);
    else if (k == "pcb_set_outline")      op_pcb_set_outline(doc, op, index);
    else if (k == "pcb_set_rules")        op_pcb_set_rules(doc, op, index);
    else if (k == "pcb_set_stackup")      op_pcb_set_stackup(doc, op, index);
    else if (k == "pcb_update_from_sch")  op_pcb_update_from_sch(doc, op, index, warnings);
    else if (k == "rename_doc")           op_rename_doc(doc, op, index);
    else if (k == "sch_add_bus")          op_sch_add_bus(doc, op, index, id_map);
    else if (k == "sch_add_bus_entry")    op_sch_add_bus_entry(doc, op, index, id_map);
    else if (k == "sch_add_graphic")      op_sch_add_graphic(doc, op, index, id_map);
    else if (k == "sch_add_junction")     op_sch_add_junction(doc, op, index, id_map);
    else if (k == "sch_add_label")        op_sch_add_label(doc, op, index, id_map);
    else if (k == "sch_add_no_connect")   op_sch_add_no_connect(doc, op, index, id_map);
    else if (k == "sch_add_power")        op_sch_add_power(doc, op, index, id_map);
    else if (k == "sch_add_sheet")        op_sch_add_sheet(doc, op, index, id_map);
    else if (k == "sch_add_sheet_pin")    op_sch_add_sheet_pin(doc, op, index, id_map);
    else if (k == "sch_add_sheet_symbol") op_sch_add_sheet_symbol(doc, op, index, id_map);
    else if (k == "sch_add_symbol")       op_sch_add_symbol(doc, op, index, id_map, warnings);
    else if (k == "sch_add_text")         op_sch_add_text(doc, op, index, id_map);
    else if (k == "sch_add_wire")         op_sch_add_wire(doc, op, index, id_map);
    else if (k == "sch_annotate")         op_sch_annotate(doc, op, index);
    else if (k == "sch_connect")          op_sch_connect(doc, op, index, id_map, warnings);
    else if (k == "sch_delete_bus")       op_sch_delete_bus(doc, op, index, id_map);
    else if (k == "sch_delete_bus_entry") op_sch_delete_bus_entry(doc, op, index, id_map);
    else if (k == "sch_delete_graphic")   op_sch_delete_graphic(doc, op, index, id_map);
    else if (k == "sch_delete_junction")  op_sch_delete_junction(doc, op, index, id_map);
    else if (k == "sch_delete_label")     op_sch_delete_label(doc, op, index, id_map);
    else if (k == "sch_delete_no_connect") op_sch_delete_no_connect(doc, op, index, id_map);
    else if (k == "sch_delete_power")     op_sch_delete_power(doc, op, index, id_map);
    else if (k == "sch_delete_sheet")     op_sch_delete_sheet(doc, op, index, id_map);
    else if (k == "sch_delete_sheet_pin") op_sch_delete_sheet_pin(doc, op, index, id_map);
    else if (k == "sch_delete_sheet_symbol") op_sch_delete_sheet_symbol(doc, op, index, id_map);
    else if (k == "sch_delete_symbol")    op_sch_delete_symbol(doc, op, index, id_map);
    else if (k == "sch_delete_text")      op_sch_delete_text(doc, op, index, id_map);
    else if (k == "sch_delete_wire")      op_sch_delete_wire(doc, op, index, id_map);
    else if (k == "sch_edit_bus")         op_sch_edit_bus(doc, op, index, id_map);
    else if (k == "sch_edit_graphic")     op_sch_edit_graphic(doc, op, index, id_map);
    else if (k == "sch_edit_junction")    op_sch_edit_junction(doc, op, index, id_map);
    else if (k == "sch_edit_label")       op_sch_edit_label(doc, op, index, id_map);
    else if (k == "sch_edit_power")       op_sch_edit_power(doc, op, index, id_map);
    else if (k == "sch_edit_sheet")       op_sch_edit_sheet(doc, op, index, id_map);
    else if (k == "sch_edit_sheet_pin")   op_sch_edit_sheet_pin(doc, op, index, id_map);
    else if (k == "sch_edit_sheet_symbol") op_sch_edit_sheet_symbol(doc, op, index, id_map);
    else if (k == "sch_edit_symbol")      op_sch_edit_symbol(doc, op, index, id_map, warnings);
    else if (k == "sch_edit_text")        op_sch_edit_text(doc, op, index, id_map);
    else if (k == "sch_edit_wire")        op_sch_edit_wire(doc, op, index, id_map);
    else if (k == "sch_import_kicad")     op_sch_import_kicad(doc, op, index, id_map, warnings);
    else if (k == "sch_set_grid")         op_sch_set_grid(doc, op, index);
    else if (k == "sch_set_title_block")  op_sch_set_title_block(doc, op, index);
    else if (k == "set_field")            op_set_field(doc, op, index, id_map);
    else
        throw OpError("invalid_op", unknown_ecad_op_message(index, k),
                      {{"index", index}, {"reason", "unsupported op '" + k + "'"},
                       {"suggestions", suggest_ecad_ops(k, 5)}});
}

std::string join_errs(const std::vector<std::string>& errs, size_t n) {
    std::string out;
    for (size_t i = 0; i < errs.size() && i < n; ++i) {
        if (i) out += "; ";
        out += errs[i];
    }
    return out;
}

}  // namespace

json apply_ops(const json& doc, const json& ops) {
    if (!ops.is_array())
        throw OpError("invalid_op", "ops must be a list", {{"index", -1}, {"reason", "ops not a list"}});
    if (!(schema::is_schematic(doc) || schema::is_board(doc)))
        throw OpError("invalid_op",
                      "document is neither sch nor pcb ('" + doc.value("schema", std::string()) + "')",
                      {{"index", -1}, {"reason", "unknown doc kind"}});

    json working = doc;  // deep copy
    json id_map = json::object();
    json warnings = json::array();

    int index = 0;
    for (const json& op : ops) {
        apply_one(working, op, index, id_map, warnings);
        ++index;
    }

    std::vector<std::string> errs = schema::validate(working);
    if (!errs.empty()) {
        json all = json::array();
        for (const std::string& e : errs) all.push_back(e);
        throw OpError("invalid_op", "resulting document failed validation: " + join_errs(errs, 5),
                      {{"index", -1}, {"reason", errs.empty() ? "invalid document" : errs[0]},
                       {"all_errors", all}});
    }
    return json{{"id_map", id_map}, {"warnings", warnings}, {"doc", working}};
}

}  // namespace chudware::ecad
