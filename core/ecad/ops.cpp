// ECAD ops implementation — see ops.h. Mirrors engine/ecad/ops.py exactly.

#include "ecad/ops.h"
#include "ecad/op_registry.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <regex>
#include <set>
#include <vector>

#include "ecad/schema.h"

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
// engine.ecad.library is not present in the C++ core (matches Python ImportError
// branch): a caller-provided def wins, else a minimal stub + a warning.
json stub_def(const std::string& libid, const std::string& kind) {
    std::string name = libid;
    auto p = libid.find(':');
    if (p != std::string::npos) name = libid.substr(p + 1);
    if (name.empty()) name = libid;
    if (kind == "footprint") return json{{"fpid", libid}, {"name", name}, {"pads", json::array()}};
    return json{
        {"libid", libid}, {"name", name}, {"description", "stub for " + libid},
        {"reference_prefix", "U"}, {"value", name}, {"units_count", 1}, {"power", false},
        {"pins", json::array({
            json{{"number", "1"}, {"name", "1"}, {"pos", json::array({-2.54, 0.0})},
                 {"angle", 0.0}, {"length", 2.54}, {"type", "passive"}, {"unit", 1}},
            json{{"number", "2"}, {"name", "2"}, {"pos", json::array({2.54, 0.0})},
                 {"angle", 180.0}, {"length", 2.54}, {"type", "passive"}, {"unit", 1}}})}};
}

void ensure_lib_def(json& doc, const std::string& libid, const json& caller_def,
                    json& warnings, int index, const std::string& kind) {
    if (!doc.contains("lib_cache") || !doc["lib_cache"].is_object()) doc["lib_cache"] = json::object();
    json& cache = doc["lib_cache"];
    if (cache.contains(libid)) return;
    if (caller_def.is_object()) {
        cache[libid] = caller_def;
        return;
    }
    warnings.push_back("op[" + std::to_string(index) +
                       "]: engine.ecad.library not available; using a stub for '" + libid + "'");
    cache[libid] = stub_def(libid, kind);
}

std::string ref_prefix(const json& doc, const std::string& libid) {
    if (doc.contains("lib_cache") && doc["lib_cache"].is_object() && doc["lib_cache"].contains(libid)) {
        const json& sdef = doc["lib_cache"][libid];
        if (sdef.is_object() && sdef.contains("reference_prefix") &&
            sdef["reference_prefix"].is_string()) {
            std::string p = sdef["reference_prefix"].get<std::string>();
            if (!p.empty()) return p;
        }
    }
    std::string name = libid;
    auto pp = libid.find(':');
    if (pp != std::string::npos) name = libid.substr(pp + 1);
    if (name.empty()) return "U";
    char c = name[0];
    return std::string(1, (char)std::toupper((unsigned char)c));
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
    if (!op.contains("libid") || !op["libid"].is_string() || op["libid"].get<std::string>().empty())
        throw invalid(index, "sch_add_symbol requires a 'libid' string");
    std::string libid = op["libid"];
    ensure_lib_def(doc, libid, op.contains("def") ? op["def"] : json(), warnings, index, "symbol");
    std::string sid = next_id(doc, "s");
    std::string ref;
    if (op.contains("ref") && op["ref"].is_string() && !op["ref"].get<std::string>().empty())
        ref = op["ref"].get<std::string>();
    else
        ref = next_ref(doc, ref_prefix(doc, libid));
    json def_val = (doc["lib_cache"].contains(libid) && doc["lib_cache"][libid].is_object())
                       ? doc["lib_cache"][libid].value("value", json(""))
                       : json("");
    json sym = {
        {"id", sid}, {"libid", libid}, {"sheet", op.value("sheet", std::string("sh1"))},
        {"ref", ref}, {"value", op.contains("value") ? op["value"] : def_val},
        {"pos", require_xy(op, "pos", index)},
        {"angle", op.contains("angle") ? op["angle"].get<double>() : 0.0},
        {"mirror", op.value("mirror", std::string("none"))},
        {"unit", op.contains("unit") ? op["unit"].get<int>() : 1},
        {"fields", op.contains("fields") ? op["fields"] : json::object()},
        {"in_bom", true}, {"on_board", true}, {"dnp", op.value("dnp", false)}};
    if (op.contains("footprint") && !op["footprint"].is_null()) sym["footprint"] = op["footprint"];
    if (op.contains("sim_params") && !op["sim_params"].is_null()) sym["sim_params"] = op["sim_params"];
    if (!schema::sym_mirror().count(sym["mirror"].get<std::string>()))
        throw invalid(index, "mirror must be one of [none, x, y]");
    doc["symbols"].push_back(sym);
    id_map["$" + std::to_string(index)] = sid;
}

void op_sch_edit_symbol(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    json sid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* sym = find(doc["symbols"], sid.is_string() ? sid.get<std::string>() : std::string());
    if (!sym)
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no symbol '" + sid.dump() + "'",
                      {{"index", index}, {"reason", "unknown symbol " + sid.dump()}});
    json p = json::object();
    for (const char* k : {"pos", "angle", "mirror", "ref", "value", "footprint", "fields", "dnp",
                          "sim_params"})
        if (op.contains(k)) p[k] = op[k];
    if (p.contains("mirror") && !p["mirror"].is_null() &&
        !schema::sym_mirror().count(p["mirror"].get<std::string>()))
        throw invalid(index, "mirror must be one of [none, x, y]");
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
    const json& pts = op.contains("pts") ? op["pts"] : json();
    bool ok = pts.is_array() && pts.size() >= 2;
    if (ok) for (const json& p : pts) ok = ok && p.is_array() && p.size() == 2;
    if (!ok) throw invalid(index, "sch_add_wire requires 'pts': >=2 [x,y] points");
    std::vector<std::array<double, 2>> raw;
    for (const json& p : pts) raw.push_back({p[0].get<double>(), p[1].get<double>()});
    json clean = orthogonalize(raw);
    if (clean.size() < 2) throw invalid(index, "sch_add_wire: degenerate (all points coincident)");
    std::string wid = next_id(doc, "w");
    doc["wires"].push_back({{"id", wid}, {"sheet", op.value("sheet", std::string("sh1"))},
                            {"pts", clean}, {"bus", false}});
    id_map["$" + std::to_string(index)] = wid;
}

void op_sch_edit_wire(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    json wid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* wire = find(doc["wires"], wid.is_string() ? wid.get<std::string>() : std::string());
    if (!wire)
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no wire '" + wid.dump() + "'",
                      {{"index", index}, {"reason", "unknown wire " + wid.dump()}});
    const json& pts = op.contains("pts") ? op["pts"] : json();
    bool ok = pts.is_array() && pts.size() >= 2;
    if (ok) for (const json& p : pts) ok = ok && p.is_array() && p.size() == 2;
    if (!ok) throw invalid(index, "sch_edit_wire requires 'pts': >=2 [x,y] points");
    std::vector<std::array<double, 2>> raw;
    for (const json& p : pts) raw.push_back({p[0].get<double>(), p[1].get<double>()});
    json clean = orthogonalize(raw);
    if (clean.size() < 2) throw invalid(index, "sch_edit_wire: degenerate (all points coincident)");
    (*wire)["pts"] = clean;
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
    std::string jid = next_id(doc, "j");
    doc["junctions"].push_back({{"id", jid}, {"sheet", op.value("sheet", std::string("sh1"))},
                                {"pos", require_xy(op, "pos", index)}});
    id_map["$" + std::to_string(index)] = jid;
}

void op_sch_delete_junction(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    json jid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    std::string j = jid.is_string() ? jid.get<std::string>() : std::string();
    if (!find(doc["junctions"], j))
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no junction '" + j + "'",
                      {{"index", index}, {"reason", "unknown junction " + j}});
    json kept = json::array();
    for (const json& x : doc["junctions"])
        if (x.value("id", std::string()) != j) kept.push_back(x);
    doc["junctions"] = kept;
}

void op_sch_add_label(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    if (!op.contains("text") || !op["text"].is_string() || op["text"].get<std::string>().empty())
        throw invalid(index, "sch_add_label requires a non-empty 'text'");
    std::string scope = op.value("scope", std::string("local"));
    if (!schema::label_scopes().count(scope))
        throw invalid(index, "scope must be one of [global, hier, local]");
    std::string lid = next_id(doc, "l");
    doc["labels"].push_back({{"id", lid}, {"sheet", op.value("sheet", std::string("sh1"))},
                             {"pos", require_xy(op, "pos", index)}, {"text", op["text"]},
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
    if (op.contains("scope") && !op["scope"].is_null() &&
        !schema::label_scopes().count(op["scope"].get<std::string>()))
        throw invalid(index, "scope must be one of [global, hier, local]");
    if (op.contains("text") &&
        (!op["text"].is_string() || op["text"].get<std::string>().empty()))
        throw invalid(index, "sch_edit_label.text must be a non-empty string");
    json p = json::object();
    for (const char* k : {"pos", "text", "scope", "angle"})
        if (op.contains(k)) p[k] = op[k];
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
    if (!op.contains("net") || !op["net"].is_string() || op["net"].get<std::string>().empty())
        throw invalid(index, "sch_add_power requires a non-empty 'net'");
    std::string net = op["net"];
    std::string pid = next_id(doc, "pw");
    doc["power"].push_back({{"id", pid}, {"sheet", op.value("sheet", std::string("sh1"))},
                            {"pos", require_xy(op, "pos", index)}, {"net", net},
                            {"symbol", op.value("symbol", net)}});
    id_map["$" + std::to_string(index)] = pid;
}

void op_sch_add_no_connect(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    std::string nid = next_id(doc, "nc");
    doc["no_connect"].push_back({{"id", nid}, {"sheet", op.value("sheet", std::string("sh1"))},
                                 {"pos", require_xy(op, "pos", index)}});
    id_map["$" + std::to_string(index)] = nid;
}

std::pair<double, double> pin_world_for(const json& doc, const json& sym, const std::string& pin_no,
                                        int index) {
    std::string libid = sym.value("libid", std::string());
    int unit = sym.contains("unit") && sym["unit"].is_number() ? sym["unit"].get<int>() : 1;
    if (doc.contains("lib_cache") && doc["lib_cache"].contains(libid)) {
        const json& sdef = doc["lib_cache"][libid];
        if (sdef.is_object() && sdef.contains("pins"))
            for (const json& pin : sdef["pins"]) {
                int pu = pin.contains("unit") && pin["unit"].is_number() ? pin["unit"].get<int>() : 1;
                if (pu != unit && pu != 0) continue;
                std::string num = pin.contains("number") && pin["number"].is_string()
                                      ? pin["number"].get<std::string>()
                                      : (pin.contains("number") ? pin["number"].dump() : "");
                std::string nm = pin.contains("name") && pin["name"].is_string()
                                     ? pin["name"].get<std::string>()
                                     : (pin.contains("name") ? pin["name"].dump() : "");
                if (num == pin_no || nm == pin_no) return schema::pin_world(sym, pin);
            }
    }
    throw OpError("bad_ref", "op[" + std::to_string(index) + "]: symbol '" +
                                 sym.value("id", std::string()) + "' has no pin '" + pin_no + "'",
                  {{"index", index},
                   {"reason", "no pin " + pin_no + " on " + sym.value("id", std::string())}});
}

void op_sch_connect(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    if (!op.contains("from") || !op["from"].is_object() || !op.contains("to") || !op["to"].is_object())
        throw invalid(index, "sch_connect requires 'from' and 'to' = {sym, pin}");
    auto endpoint = [&](const json& ref) -> std::pair<double, double> {
        json sid = resolve_placeholder(ref.contains("sym") ? ref["sym"] : json(), id_map, index);
        std::string s = sid.is_string() ? sid.get<std::string>() : std::string();
        json* sym = find(doc["symbols"], s);
        if (!sym)
            throw OpError("bad_ref", "op[" + std::to_string(index) +
                                         "]: sch_connect references unknown symbol '" + s + "'",
                          {{"index", index}, {"reason", "unknown symbol " + s}});
        std::string pin = ref.contains("pin") && ref["pin"].is_string()
                              ? ref["pin"].get<std::string>()
                              : (ref.contains("pin") ? ref["pin"].dump() : "");
        return pin_world_for(doc, *sym, pin, index);
    };
    auto [ax, ay] = endpoint(op["from"]);
    auto [bx, by] = endpoint(op["to"]);
    std::vector<std::array<double, 2>> pts;
    if (std::abs(ax - bx) < 1e-6 || std::abs(ay - by) < 1e-6)
        pts = {{ax, ay}, {bx, by}};
    else
        pts = {{ax, ay}, {bx, ay}, {bx, by}};
    json clean = orthogonalize(pts);
    std::string wid = next_id(doc, "w");
    std::string sheet = op.contains("sheet")
                            ? op["sheet"].get<std::string>()
                            : (op["from"].contains("sheet") && op["from"]["sheet"].is_string()
                                   ? op["from"]["sheet"].get<std::string>()
                                   : std::string("sh1"));
    doc["wires"].push_back({{"id", wid}, {"sheet", sheet}, {"pts", clean}, {"bus", false}});
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

void op_sch_add_sheet(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    if (!doc.contains("sheets") || !doc["sheets"].is_array()) doc["sheets"] = json::array();
    json parent = op.contains("parent") ? op["parent"] : json("sh1");
    if (!parent.is_null() &&
        !find(doc["sheets"], parent.is_string() ? parent.get<std::string>() : std::string()))
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no parent sheet '" +
                                            parent.dump() + "'",
                      {{"index", index}, {"reason", "unknown sheet " + parent.dump()}});
    std::string shid = next_id(doc, "sh");
    json size = op.contains("size") ? op["size"] : json::array({297, 210});
    doc["sheets"].push_back({{"id", shid}, {"name", op.value("name", shid)}, {"parent", parent},
                             {"size", size}, {"ports", json::array()}});
    id_map["$" + std::to_string(index)] = shid;
}

void op_sch_add_sheet_symbol(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    json child = resolve_placeholder(op.contains("child") ? op["child"] : json(), id_map, index);
    if (!child.is_string() || child.get<std::string>().empty())
        throw invalid(index, "sch_add_sheet_symbol requires a 'child' sheet id");
    std::string c = child.get<std::string>();
    if (!find(doc["sheets"], c))
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no child sheet '" + c + "'",
                      {{"index", index}, {"reason", "unknown sheet " + c}});
    std::string hid = next_id(doc, "hs");
    json pins_out = json::array();
    std::set<std::string> seen;
    if (op.contains("pins") && op["pins"].is_array()) {
        int k = 0;
        for (const json& pn : op["pins"]) {
            if (!pn.is_object()) throw invalid(index, "pins[" + std::to_string(k) + "] must be an object");
            if (!pn.contains("name") || !pn["name"].is_string() || pn["name"].get<std::string>().empty())
                throw invalid(index, "pins[" + std::to_string(k) + "].name must be a non-empty string");
            std::string name = pn["name"];
            if (seen.count(name))
                throw invalid(index, "pins[" + std::to_string(k) + "].name duplicate '" + name + "'");
            seen.insert(name);
            std::string shape = pn.value("shape", std::string("passive"));
            if (!schema::sheet_pin_shapes().count(shape))
                throw invalid(index, "pins[" + std::to_string(k) +
                                         "].shape must be one of [bidirectional, input, output, "
                                         "passive, tri_state]");
            const json& pos = pn.contains("pos") ? pn["pos"] : json();
            bool ok = pos.is_array() && pos.size() == 2;
            if (ok) for (const json& cc : pos) ok = ok && cc.is_number() && !cc.is_boolean();
            if (!ok) throw invalid(index, "pins[" + std::to_string(k) + "].pos must be [x, y]");
            pins_out.push_back({{"name", name}, {"pos", json::array({pos[0].get<double>(),
                                                                     pos[1].get<double>()})},
                                {"shape", shape}});
            ++k;
        }
    }
    doc["sheet_symbols"].push_back({{"id", hid}, {"sheet", op.value("sheet", std::string("sh1"))},
                                    {"child", c}, {"pos", require_xy(op, "pos", index)},
                                    {"pins", pins_out}});
    id_map["$" + std::to_string(index)] = hid;
}

void op_sch_add_sheet_pin(json& doc, const json& op, int index, json& id_map) {
    require_sch(doc, index);
    json hid = resolve_placeholder(op.contains("id") ? op["id"] : json(), id_map, index);
    json* hs = find(doc["sheet_symbols"], hid.is_string() ? hid.get<std::string>() : std::string());
    if (!hs)
        throw OpError("unknown_object", "op[" + std::to_string(index) + "]: no sheet symbol '" +
                                            hid.dump() + "'",
                      {{"index", index}, {"reason", "unknown sheet symbol " + hid.dump()}});
    if (!op.contains("name") || !op["name"].is_string() || op["name"].get<std::string>().empty())
        throw invalid(index, "sch_add_sheet_pin requires a non-empty 'name'");
    std::string name = op["name"];
    if (!hs->contains("pins") || !(*hs)["pins"].is_array()) (*hs)["pins"] = json::array();
    for (const json& p : (*hs)["pins"])
        if (p.is_object() && p.value("name", std::string()) == name)
            throw invalid(index, "sheet pin '" + name + "' already exists on " +
                                     (hid.is_string() ? hid.get<std::string>() : std::string()));
    std::string shape = op.value("shape", std::string("passive"));
    if (!schema::sheet_pin_shapes().count(shape))
        throw invalid(index, "shape must be one of [bidirectional, input, output, passive, tri_state]");
    (*hs)["pins"].push_back({{"name", name}, {"pos", require_xy(op, "pos", index)}, {"shape", shape}});
}

void op_sch_import_kicad(json& doc, const json&, int index, json& warnings) {
    require_sch(doc, index);
    warnings.push_back("op[" + std::to_string(index) +
                       "]: sch_import_kicad is a best-effort stub (library/kicad_sch importer not "
                       "wired yet); no objects imported");
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
                       "at the integrator (session/worker) layer where the schematic doc + netlist "
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

    if (k == "sch_add_symbol")            op_sch_add_symbol(doc, op, index, id_map, warnings);
    else if (k == "sch_edit_symbol")      op_sch_edit_symbol(doc, op, index, id_map);
    else if (k == "sch_delete_symbol")    op_sch_delete_symbol(doc, op, index, id_map);
    else if (k == "sch_add_wire")         op_sch_add_wire(doc, op, index, id_map);
    else if (k == "sch_edit_wire")        op_sch_edit_wire(doc, op, index, id_map);
    else if (k == "sch_delete_wire")      op_sch_delete_wire(doc, op, index, id_map);
    else if (k == "sch_add_junction")     op_sch_add_junction(doc, op, index, id_map);
    else if (k == "sch_delete_junction")  op_sch_delete_junction(doc, op, index, id_map);
    else if (k == "sch_add_label")        op_sch_add_label(doc, op, index, id_map);
    else if (k == "sch_edit_label")       op_sch_edit_label(doc, op, index, id_map);
    else if (k == "sch_delete_label")     op_sch_delete_label(doc, op, index, id_map);
    else if (k == "sch_add_power")        op_sch_add_power(doc, op, index, id_map);
    else if (k == "sch_add_no_connect")   op_sch_add_no_connect(doc, op, index, id_map);
    else if (k == "sch_connect")          op_sch_connect(doc, op, index, id_map);
    else if (k == "sch_annotate")         op_sch_annotate(doc, op, index);
    else if (k == "sch_set_grid")         op_sch_set_grid(doc, op, index);
    else if (k == "sch_add_sheet")        op_sch_add_sheet(doc, op, index, id_map);
    else if (k == "sch_add_sheet_symbol") op_sch_add_sheet_symbol(doc, op, index, id_map);
    else if (k == "sch_add_sheet_pin")    op_sch_add_sheet_pin(doc, op, index, id_map);
    else if (k == "sch_import_kicad")     op_sch_import_kicad(doc, op, index, warnings);
    else if (k == "pcb_set_stackup")      op_pcb_set_stackup(doc, op, index);
    else if (k == "pcb_set_outline")      op_pcb_set_outline(doc, op, index);
    else if (k == "pcb_add_footprint")    op_pcb_add_footprint(doc, op, index, id_map);
    else if (k == "pcb_edit_footprint")   op_pcb_edit_footprint(doc, op, index, id_map);
    else if (k == "pcb_delete_footprint") op_pcb_delete_footprint(doc, op, index, id_map);
    else if (k == "pcb_add_track")        op_pcb_add_track(doc, op, index, id_map);
    else if (k == "pcb_add_tracks")       op_pcb_add_tracks(doc, op, index, id_map);
    else if (k == "pcb_delete_track")     op_pcb_delete_track(doc, op, index, id_map);
    else if (k == "pcb_add_via")          op_pcb_add_via(doc, op, index, id_map);
    else if (k == "pcb_delete_via")       op_pcb_delete_via(doc, op, index, id_map);
    else if (k == "pcb_add_zone")         op_pcb_add_zone(doc, op, index, id_map);
    else if (k == "pcb_edit_zone")        op_pcb_edit_zone(doc, op, index, id_map);
    else if (k == "pcb_delete_zone")      op_pcb_delete_zone(doc, op, index, id_map);
    else if (k == "pcb_set_rules")        op_pcb_set_rules(doc, op, index);
    else if (k == "pcb_assign_net")       op_pcb_assign_net(doc, op, index, id_map);
    else if (k == "pcb_update_from_sch")  op_pcb_update_from_sch(doc, op, index, warnings);
    else if (k == "pcb_autoplace")        op_pcb_autoplace(doc, op, index, warnings);
    else if (k == "rename_doc")           op_rename_doc(doc, op, index);
    else if (k == "set_field")            op_set_field(doc, op, index, id_map);
    else if (k == "nop") { /* transactional no-op */ }
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
