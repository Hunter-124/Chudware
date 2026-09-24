// ECAD schema implementation — see schema.h. Mirrors engine/ecad/schema.py.

#include "ecad/schema.h"

#include <cmath>
#include <map>
#include <regex>
#include <stdexcept>

namespace chudware::ecad::schema {

using nlohmann::json;

// ----------------------------------------------------------------- enums
const std::set<std::string>& sym_mirror() {
    static const std::set<std::string> s{"none", "x", "y"};
    return s;
}
const std::set<std::string>& label_scopes() {
    static const std::set<std::string> s{"local", "global", "hier"};
    return s;
}
const std::set<std::string>& sheet_pin_shapes() {
    static const std::set<std::string> s{"input", "output", "bidirectional", "tri_state", "passive"};
    return s;
}
const std::set<std::string>& graphic_kinds() {
    static const std::set<std::string> s{"line", "rect", "circle", "arc", "polyline"};
    return s;
}
const std::set<std::string>& pin_types() {
    static const std::set<std::string> s{"input", "output", "bidirectional", "tri_state", "passive",
                                         "power_in", "power_out", "open_collector", "open_emitter",
                                         "unspecified", "no_connect", "free"};
    return s;
}
const std::set<std::string>& tech_layers() {
    static const std::set<std::string> s{"F.SilkS", "B.SilkS", "F.Mask", "B.Mask", "F.Paste",
                                         "B.Paste", "F.Fab", "B.Fab", "F.CrtYd", "B.CrtYd",
                                         "Edge.Cuts", "Cmts.User", "Dwgs.User"};
    return s;
}
const std::set<std::string>& pad_shapes() {
    static const std::set<std::string> s{"rect", "roundrect", "circle", "oval", "trapezoid", "custom"};
    return s;
}
const std::set<std::string>& pad_types() {
    static const std::set<std::string> s{"smd", "tht", "np_tht", "connector"};
    return s;
}
const std::set<std::string>& fp_sides() {
    static const std::set<std::string> s{"top", "bottom"};
    return s;
}
const std::set<std::string>& via_types() {
    static const std::set<std::string> s{"through", "blind", "buried", "micro"};
    return s;
}
const std::set<std::string>& track_kinds() {
    static const std::set<std::string> s{"segment", "arc"};
    return s;
}
const std::set<std::string>& zone_fills() {
    static const std::set<std::string> s{"solid", "hatch"};
    return s;
}

// id prefix -> the document list key it indexes (matches _SCH_PREFIXES / _PCB_PREFIXES)
namespace {
const std::map<std::string, std::string>& sch_prefixes() {
    static const std::map<std::string, std::string> m{
        {"s", "symbols"},       {"w", "wires"},        {"j", "junctions"},
        {"l", "labels"},        {"b", "buses"},        {"be", "bus_entries"},
        {"p", "power"},         {"nc", "no_connect"},  {"sh", "sheets"},
        {"tx", "texts"},        {"g", "graphics"},     {"ss", "sheet_symbols"}};
    return m;
}
const std::map<std::string, std::string>& pcb_prefixes() {
    static const std::map<std::string, std::string> m{
        {"fp", "footprints"}, {"t", "tracks"}, {"v", "vias"}, {"z", "zones"}, {"g", "graphics"}};
    return m;
}
}  // namespace

// ----------------------------------------------------------------- factories
json new_schematic(const std::string& name) {
    return json{
        {"schema", kSchemaSch}, {"id", ""}, {"name", name.empty() ? "Schematic" : name},
        {"units", {{"grid", 2.54}}},
        {"sheets", json::array({json{{"id", "sh1"}, {"name", "root"}, {"parent", nullptr},
                                     {"size", json::array({297, 210})}, {"ports", json::array()}}})},
        {"symbols", json::array()}, {"wires", json::array()}, {"junctions", json::array()},
        {"labels", json::array()}, {"buses", json::array()}, {"bus_entries", json::array()},
        {"power", json::array()}, {"no_connect", json::array()}, {"texts", json::array()},
        {"graphics", json::array()}, {"sheet_symbols", json::array()},
        {"lib_cache", json::object()}, {"annotations", json::object()},
        {"title_block", {{"title", ""}, {"company", ""}, {"rev", ""}, {"date", ""},
                         {"comment1", ""}, {"comment2", ""}, {"comment3", ""},
                         {"comment4", ""}, {"paper", "A4"}}},
    };
}

json new_board(const std::string& name) {
    return json{
        {"schema", kSchemaPcb}, {"id", ""}, {"name", name.empty() ? "Board" : name},
        {"linked_sch", nullptr},
        {"stackup", {{"copper_layers", 2}, {"thickness", 1.6},
                     {"layers", json::array({
                         json{{"id", "F.Cu"}, {"type", "copper"}, {"thickness", 0.035}},
                         json{{"id", "diel1"}, {"type", "core"}, {"thickness", 1.53}, {"er", 4.5}},
                         json{{"id", "B.Cu"}, {"type", "copper"}, {"thickness", 0.035}}})}}},
        {"outline", {{"shapes", json::array()}, {"closed", false}}},
        {"footprints", json::array()}, {"tracks", json::array()}, {"vias", json::array()},
        {"zones", json::array()}, {"graphics", json::array()},
        {"rules", {{"clearance", 0.2}, {"track_width_min", 0.15}, {"via_diameter_min", 0.45},
                   {"via_drill_min", 0.2}, {"annular_ring_min", 0.13}, {"hole_to_hole_min", 0.25},
                   {"edge_clearance", 0.3}, {"silk_to_mask_min", 0.15}}},
        {"nets", json::array({json{{"name", "GND"}, {"class", "default"}}})},
        {"net_classes", {{"default", {{"clearance", 0.2}, {"track_width", 0.25},
                                      {"via_diameter", 0.8}, {"via_drill", 0.4}}}}},
        {"origin", json::array({0.0, 0.0})},
    };
}

bool is_schematic(const json& doc) {
    return doc.is_object() && doc.value("schema", std::string()) == kSchemaSch;
}
bool is_board(const json& doc) {
    return doc.is_object() && doc.value("schema", std::string()) == kSchemaPcb;
}
std::string doc_kind(const json& doc) {
    if (is_schematic(doc)) return "sch";
    if (is_board(doc)) return "pcb";
    return "";
}

// ----------------------------------------------------------------- id allocation
std::string next_id(const json& doc, const std::string& prefix) {
    const auto& table = is_schematic(doc) ? sch_prefixes() : pcb_prefixes();
    auto kit = table.find(prefix);
    int n = 0;
    if (kit != table.end() && doc.contains(kit->second) && doc[kit->second].is_array()) {
        const std::regex pat("^" + prefix + "([0-9]+)$");
        for (const json& it : doc[kit->second]) {
            if (!it.is_object()) continue;
            std::string id = it.value("id", std::string());
            std::smatch m;
            if (std::regex_match(id, m, pat)) {
                int v = std::atoi(m[1].str().c_str());
                if (v > n) n = v;
            }
        }
    }
    return prefix + std::to_string(n + 1);
}

// ----------------------------------------------------------------- copper layers
std::vector<std::string> copper_layer_ids(int copper_layers) {
    if (copper_layers <= 1) return {"F.Cu"};
    std::vector<std::string> out{"F.Cu"};
    for (int i = 1; i < copper_layers - 1; ++i) out.push_back("In" + std::to_string(i) + ".Cu");
    out.push_back("B.Cu");
    return out;
}
std::set<std::string> valid_layers(int copper_layers) {
    std::set<std::string> s;
    for (const std::string& l : copper_layer_ids(copper_layers)) s.insert(l);
    for (const std::string& l : tech_layers()) s.insert(l);
    return s;
}

// ----------------------------------------------------------------- geometry
namespace {
bool is_xy(const json& v) {
    return v.is_array() && v.size() == 2 && v[0].is_number() && v[1].is_number();
}

const json& require_pos(const json& entity, const char* name) {
    if (!entity.is_object() || !entity.contains("pos") || !is_xy(entity["pos"]))
        throw std::invalid_argument(std::string(name) + ".pos: must be [x,y]");
    return entity["pos"];
}

std::pair<double, double> rot(double x, double y, double deg) {
    double r = deg * 3.14159265358979323846 / 180.0;
    double c = std::cos(r), s = std::sin(r);
    return {x * c - y * s, x * s + y * c};
}
std::pair<double, double> apply_mirror(double x, double y, const std::string& mirror) {
    if (mirror == "x") return {-x, y};
    if (mirror == "y") return {x, -y};
    return {x, y};
}
std::pair<double, double> local_to_world(double x, double y, const json& pos, double angle,
                                         const std::string& mirror) {
    auto [mx, my] = apply_mirror(x, y, mirror);
    auto [rx, ry] = rot(mx, my, angle);
    return {rx + pos[0].get<double>(), ry + pos[1].get<double>()};
}
}  // namespace

std::pair<double, double> pin_world(const json& symbol, const json& pin) {
    const json& pos = require_pos(pin, "pin");
    double px = pos[0].get<double>(), py = pos[1].get<double>();
    double length = pin.contains("length") && pin["length"].is_number() ? pin["length"].get<double>()
                                                                        : 2.54;
    double a = (pin.contains("angle") && pin["angle"].is_number() ? pin["angle"].get<double>() : 0.0)
               * 3.14159265358979323846 / 180.0;
    double tx = px + length * std::cos(a), ty = py + length * std::sin(a);
    double sang = symbol.contains("angle") && symbol["angle"].is_number() ? symbol["angle"].get<double>()
                                                                          : 0.0;
    std::string mir = symbol.value("mirror", std::string("none"));
    return local_to_world(tx, ty, require_pos(symbol, "symbol"), sang, mir);
}

std::pair<double, double> pad_world(const json& footprint, const json& pad) {
    std::string mirror =
        (footprint.value("side", std::string()) == "bottom") ? "x" : "none";
    const json& pos = require_pos(pad, "pad");
    double px = pos[0].get<double>(), py = pos[1].get<double>();
    double angle = (footprint.contains("angle") && footprint["angle"].is_number())
                       ? footprint["angle"].get<double>()
                       : 0.0;
    return local_to_world(px, py, require_pos(footprint, "footprint"), angle, mirror);
}

// ----------------------------------------------------------------- validation
namespace {

std::string string_value(const json& object, const char* key,
                         const std::string& fallback = std::string()) {
    return object.is_object() && object.contains(key) && object[key].is_string()
               ? object[key].get<std::string>()
               : fallback;
}

void check_ids(const json& items, const std::string& prefix, const std::string& loc,
               std::vector<std::string>& errs) {
    std::set<std::string> seen;
    const std::regex pat("^" + prefix + "[0-9]+$");
    int i = 0;
    if (!items.is_array()) return;
    for (const json& it : items) {
        std::string li = loc + "[" + std::to_string(i) + "]";
        if (!it.is_object()) {
            errs.push_back(li + ": not an object");
            ++i;
            continue;
        }
        const std::string oid = string_value(it, "id");
        bool has_id = it.contains("id") && it["id"].is_string();
        if (!has_id || !std::regex_match(oid, pat)) {
            errs.push_back(li + ".id: missing or bad prefix (want " + prefix + "<n>) got '" + oid + "'");
        } else if (seen.count(oid)) {
            errs.push_back(li + ".id: duplicate '" + oid + "'");
        } else {
            seen.insert(oid);
        }
        ++i;
    }
}
}  // namespace

std::vector<std::string> validate_schematic(const json& doc) {
    std::vector<std::string> errs;
    if (doc.value("schema", std::string()) != kSchemaSch)
        return {std::string("schema: expected '") + kSchemaSch + "' got '" +
                doc.value("schema", std::string()) + "'"};
    if (!(doc.contains("id") && doc["id"].is_string())) errs.push_back("id: missing");

    json cache = doc.contains("lib_cache") ? doc["lib_cache"] : json::object();
    if (!cache.is_object()) {
        errs.push_back("lib_cache: must be an object");
        cache = json::object();
    }
    auto get = [&](const char* k) { return doc.contains(k) ? doc[k] : json::array(); };
    const std::vector<std::string> array_keys{
        "sheets",       "symbols",       "wires",       "junctions",
        "labels",       "buses",         "bus_entries", "power",
        "no_connect",   "texts",         "graphics",    "sheet_symbols"};
    for (const std::string& key : array_keys)
        if (doc.contains(key) && !doc[key].is_array())
            errs.push_back(key + ": must be a list");
    if (!doc.contains("sheets") || !doc["sheets"].is_array() || doc["sheets"].empty())
        errs.push_back("sheets: must contain at least one sheet");
    if (doc.contains("annotations") && !doc["annotations"].is_object())
        errs.push_back("annotations: must be an object");
    if (doc.contains("annotations") && doc["annotations"].is_object() &&
        doc["annotations"].contains("next_ref") &&
        !doc["annotations"]["next_ref"].is_object())
        errs.push_back("annotations.next_ref: must be an object");

    check_ids(get("symbols"), "s", "symbols", errs);
    check_ids(get("wires"), "w", "wires", errs);
    check_ids(get("junctions"), "j", "junctions", errs);
    check_ids(get("labels"), "l", "labels", errs);
    check_ids(get("buses"), "b", "buses", errs);
    check_ids(get("bus_entries"), "be", "bus_entries", errs);
    check_ids(get("power"), "p", "power", errs);
    check_ids(get("no_connect"), "nc", "no_connect", errs);
    check_ids(get("texts"), "tx", "texts", errs);
    check_ids(get("graphics"), "g", "graphics", errs);
    check_ids(get("sheets"), "sh", "sheets", errs);
    check_ids(get("sheet_symbols"), "ss", "sheet_symbols", errs);

    std::set<std::string> sheet_ids;
    if (get("sheets").is_array()) {
        int i = 0;
        for (const json& sheet : get("sheets")) {
            const std::string loc = "sheets[" + std::to_string(i) + "]";
            if (!sheet.is_object()) {
                ++i;
                continue;
            }
            const std::string id = string_value(sheet, "id");
            if (!id.empty()) sheet_ids.insert(id);
            if (!(sheet.contains("name") && sheet["name"].is_string()) ||
                string_value(sheet, "name").empty())
                errs.push_back(loc + ".name: required non-empty string");
            if (!is_xy(sheet.contains("size") ? sheet["size"] : json()) ||
                (is_xy(sheet.contains("size") ? sheet["size"] : json()) &&
                 (sheet["size"][0].get<double>() <= 0.0 || sheet["size"][1].get<double>() <= 0.0)))
                errs.push_back(loc + ".size: must be positive [width,height]");
            if (sheet.contains("ports")) {
                if (!sheet["ports"].is_array()) {
                    errs.push_back(loc + ".ports: must be a list");
                } else {
                    int port_index = 0;
                    for (const json& port : sheet["ports"]) {
                        const std::string ploc =
                            loc + ".ports[" + std::to_string(port_index) + "]";
                        if (!port.is_object()) {
                            errs.push_back(ploc + ": not an object");
                            ++port_index;
                            continue;
                        }
                        if (!(port.contains("name") && port["name"].is_string()) ||
                            string_value(port, "name").empty())
                            errs.push_back(ploc + ".name: required non-empty string");
                        const std::string type = string_value(port, "type", "passive");
                        if (!sheet_pin_shapes().count(type))
                            errs.push_back(ploc + ".type: bad value '" + type + "'");
                        if (!is_xy(port.contains("pos") ? port["pos"] : json()))
                            errs.push_back(ploc + ".pos: must be [x,y]");
                        ++port_index;
                    }
                }
            }
            ++i;
        }
    }
    if (get("sheets").is_array()) {
        std::map<std::string, std::string> parents;
        int i = 0;
        for (const json& sheet : get("sheets")) {
            if (!sheet.is_object()) {
                ++i;
                continue;
            }
            const std::string id = string_value(sheet, "id");
            if (sheet.contains("parent") && !sheet["parent"].is_null()) {
                if (!sheet["parent"].is_string()) {
                    errs.push_back("sheets[" + std::to_string(i) + "].parent: must be a sheet id or null");
                } else {
                    const std::string parent = sheet["parent"].get<std::string>();
                    if (!sheet_ids.count(parent))
                        errs.push_back("sheets[" + std::to_string(i) + "].parent: '" + parent +
                                       "' not a known sheet id");
                    parents[id] = parent;
                }
            }
            ++i;
        }
        for (const auto& [id, parent] : parents) {
            std::set<std::string> seen{id};
            std::string cur = parent;
            while (!cur.empty() && parents.count(cur)) {
                if (!seen.insert(cur).second) {
                    errs.push_back("sheets: parent cycle involving '" + id + "'");
                    break;
                }
                cur = parents[cur];
            }
            if (!cur.empty() && !seen.insert(cur).second)
                errs.push_back("sheets: parent cycle involving '" + id + "'");
        }
    }

    auto check_sheet = [&](const json& item, const std::string& loc) {
        const std::string sheet = string_value(item, "sheet");
        if (!(item.contains("sheet") && item["sheet"].is_string()) || !sheet_ids.count(sheet))
            errs.push_back(loc + ".sheet: '" + sheet + "' not a known sheet id");
    };

    struct RefUse {
        int unit;
        std::string libid;
        std::string loc;
    };
    std::map<std::string, std::vector<RefUse>> refs_seen;
    if (get("symbols").is_array()) {
        int i = 0;
        for (const json& s : get("symbols")) {
            const std::string loc = "symbols[" + std::to_string(i) + "]";
            if (!s.is_object()) {
                ++i;
                continue;
            }
            const std::string libid = string_value(s, "libid");
            if (!cache.contains(libid))
                errs.push_back(loc + ".libid: '" + libid + "' not in lib_cache");
            if (!is_xy(s.contains("pos") ? s["pos"] : json()))
                errs.push_back(loc + ".pos: must be [x,y]");
            check_sheet(s, loc);
            const std::string mir = string_value(s, "mirror", "none");
            if (!sym_mirror().count(mir)) errs.push_back(loc + ".mirror: bad value '" + mir + "'");
            const int unit = s.contains("unit") && s["unit"].is_number_integer()
                                 ? s["unit"].get<int>()
                                 : 1;
            if (unit < 1) errs.push_back(loc + ".unit: must be >=1");
            const std::string ref = string_value(s, "ref");
            if (!ref.empty() && ref.back() != '?') {
                for (const RefUse& prior : refs_seen[ref])
                    if (prior.unit == unit || prior.libid != libid) {
                        errs.push_back(loc + ".ref: duplicate '" + ref + "' (also " + prior.loc + ")");
                        break;
                    }
                refs_seen[ref].push_back({unit, libid, loc});
            }
            ++i;
        }
    }

    if (get("wires").is_array()) {
        int i = 0;
        for (const json& w : get("wires")) {
            const std::string loc = "wires[" + std::to_string(i) + "]";
            if (!w.is_object()) {
                ++i;
                continue;
            }
            const json& pts = w.contains("pts") ? w["pts"] : json();
            bool ok = pts.is_array() && pts.size() >= 2;
            if (ok) for (const json& p : pts) ok = ok && is_xy(p);
            if (!ok) errs.push_back(loc + ".pts: need >=2 [x,y] points");
            if (w.contains("bus") && !w["bus"].is_boolean())
                errs.push_back(loc + ".bus: must be boolean");
            check_sheet(w, loc);
            ++i;
        }
    }

    if (get("junctions").is_array()) {
        int i = 0;
        for (const json& junction : get("junctions")) {
            const std::string loc = "junctions[" + std::to_string(i) + "]";
            if (junction.is_object()) {
                if (!is_xy(junction.contains("pos") ? junction["pos"] : json()))
                    errs.push_back(loc + ".pos: must be [x,y]");
                check_sheet(junction, loc);
            }
            ++i;
        }
    }

    if (get("labels").is_array()) {
        int i = 0;
        for (const json& lb : get("labels")) {
            const std::string loc = "labels[" + std::to_string(i) + "]";
            if (!lb.is_object()) {
                ++i;
                continue;
            }
            const std::string scope = string_value(lb, "scope", "local");
            if (!label_scopes().count(scope))
                errs.push_back(loc + ".scope: bad value '" + scope + "'");
            const std::string text = string_value(lb, "text");
            if (!(lb.contains("text") && lb["text"].is_string()) || text.empty())
                errs.push_back(loc + ".text: required non-empty string");
            if (!is_xy(lb.contains("pos") ? lb["pos"] : json()))
                errs.push_back(loc + ".pos: must be [x,y]");
            check_sheet(lb, loc);
            ++i;
        }
    }

    std::set<std::string> bus_names;
    if (get("buses").is_array()) {
        int i = 0;
        for (const json& bus : get("buses")) {
            const std::string loc = "buses[" + std::to_string(i) + "]";
            if (!bus.is_object()) {
                ++i;
                continue;
            }
            const std::string name = string_value(bus, "name");
            if (!(bus.contains("name") && bus["name"].is_string()) || name.empty())
                errs.push_back(loc + ".name: required non-empty string");
            else if (!bus_names.insert(name).second)
                errs.push_back(loc + ".name: duplicate '" + name + "'");
            const json& pts = bus.contains("pts") ? bus["pts"] : json();
            bool ok = pts.is_array() && pts.size() >= 2;
            if (ok) for (const json& p : pts) ok = ok && is_xy(p);
            if (!ok) errs.push_back(loc + ".pts: need >=2 [x,y] points");
            if (bus.contains("members")) {
                if (!bus["members"].is_array()) {
                    errs.push_back(loc + ".members: must be a list");
                } else {
                    for (const json& member : bus["members"])
                        if (!member.is_string()) {
                            errs.push_back(loc + ".members: values must be strings");
                            break;
                        }
                }
            }
            check_sheet(bus, loc);
            ++i;
        }
    }
    if (get("bus_entries").is_array()) {
        int i = 0;
        for (const json& entry : get("bus_entries")) {
            const std::string loc = "bus_entries[" + std::to_string(i) + "]";
            if (entry.is_object()) {
                if (!is_xy(entry.contains("pos") ? entry["pos"] : json()))
                    errs.push_back(loc + ".pos: must be [x,y]");
                if (!is_xy(entry.contains("size") ? entry["size"] : json()))
                    errs.push_back(loc + ".size: must be [x,y]");
                if (entry.contains("bus") && !entry["bus"].is_null()) {
                    if (!entry["bus"].is_string() ||
                        !bus_names.count(entry.value("bus", std::string())))
                        errs.push_back(loc + ".bus: must name an existing bus");
                }
                if (entry.contains("member") && !entry["member"].is_null() &&
                    !entry["member"].is_string())
                    errs.push_back(loc + ".member: must be a string");
                check_sheet(entry, loc);
            }
            ++i;
        }
    }

    if (get("power").is_array()) {
        int i = 0;
        for (const json& p : get("power")) {
            const std::string loc = "power[" + std::to_string(i) + "]";
            if (p.is_object()) {
                const std::string net = string_value(p, "net");
                if (net.empty()) errs.push_back(loc + ".net: required non-empty");
                if (!is_xy(p.contains("pos") ? p["pos"] : json()))
                    errs.push_back(loc + ".pos: must be [x,y]");
                check_sheet(p, loc);
            }
            ++i;
        }
    }
    if (get("no_connect").is_array()) {
        int i = 0;
        for (const json& nc : get("no_connect")) {
            const std::string loc = "no_connect[" + std::to_string(i) + "]";
            if (nc.is_object()) {
                if (!is_xy(nc.contains("pos") ? nc["pos"] : json()))
                    errs.push_back(loc + ".pos: must be [x,y]");
                check_sheet(nc, loc);
            }
            ++i;
        }
    }
    if (get("texts").is_array()) {
        int i = 0;
        for (const json& text : get("texts")) {
            const std::string loc = "texts[" + std::to_string(i) + "]";
            if (text.is_object()) {
                if (!(text.contains("text") && text["text"].is_string()))
                    errs.push_back(loc + ".text: must be a string");
                if (!is_xy(text.contains("pos") ? text["pos"] : json()))
                    errs.push_back(loc + ".pos: must be [x,y]");
                if (!text.contains("size") || !text["size"].is_number() ||
                    text["size"].get<double>() <= 0.0)
                    errs.push_back(loc + ".size: must be positive");
                if (text.contains("angle") && !text["angle"].is_number())
                    errs.push_back(loc + ".angle: must be numeric");
                check_sheet(text, loc);
            }
            ++i;
        }
    }
    if (get("graphics").is_array()) {
        int i = 0;
        for (const json& graphic : get("graphics")) {
            const std::string loc = "graphics[" + std::to_string(i) + "]";
            if (!graphic.is_object()) {
                ++i;
                continue;
            }
            const std::string kind = graphic.value("kind", std::string());
            if (!graphic_kinds().count(kind))
                errs.push_back(loc + ".kind: bad value '" + kind + "'");
            const json& pts = graphic.contains("pts") ? graphic["pts"] : json();
            const std::size_t min_pts = kind == "line" || kind == "rect" || kind == "polyline" ? 2 : 1;
            bool ok = pts.is_array() && pts.size() >= min_pts;
            if (ok) for (const json& p : pts) ok = ok && is_xy(p);
            if (!ok) errs.push_back(loc + ".pts: insufficient [x,y] points for " + kind);
            if ((kind == "circle" || kind == "arc") &&
                (!graphic.contains("radius") || !graphic["radius"].is_number() ||
                 graphic["radius"].get<double>() <= 0.0))
                errs.push_back(loc + ".radius: required positive number for " + kind);
            if (graphic.contains("width") &&
                (!graphic["width"].is_number() || graphic["width"].get<double>() < 0.0))
                errs.push_back(loc + ".width: must be non-negative");
            if (graphic.contains("filled") && !graphic["filled"].is_boolean())
                errs.push_back(loc + ".filled: must be boolean");
            check_sheet(graphic, loc);
            ++i;
        }
    }

    if (get("sheet_symbols").is_array()) {
        int i = 0;
        for (const json& ss : get("sheet_symbols")) {
            const std::string loc = "sheet_symbols[" + std::to_string(i) + "]";
            if (!ss.is_object()) {
                ++i;
                continue;
            }
            const std::string child = string_value(ss, "child");
            if (!(ss.contains("child") && ss["child"].is_string()) || child.empty())
                errs.push_back(loc + ".child: required (id of the instantiated child sheet)");
            else if (!sheet_ids.count(child))
                errs.push_back(loc + ".child: '" + child + "' not a known sheet id");
            if (!is_xy(ss.contains("pos") ? ss["pos"] : json()))
                errs.push_back(loc + ".pos: must be [x,y]");
            if (ss.contains("size") && !is_xy(ss["size"]))
                errs.push_back(loc + ".size: must be [x,y]");
            check_sheet(ss, loc);
            std::set<std::string> pin_names;
            json pins = ss.contains("pins") ? ss["pins"] : json::array();
            if (!pins.is_array()) {
                errs.push_back(loc + ".pins: must be a list");
                pins = json::array();
            }
            int j = 0;
            for (const json& pn : pins) {
                const std::string ploc = loc + ".pins[" + std::to_string(j) + "]";
                if (!pn.is_object()) {
                    errs.push_back(ploc + ": not an object");
                    ++j;
                    continue;
                }
                const std::string nm = string_value(pn, "name");
                if (!(pn.contains("name") && pn["name"].is_string()) || nm.empty())
                    errs.push_back(ploc + ".name: required non-empty string");
                else if (!pin_names.insert(nm).second)
                    errs.push_back(ploc + ".name: duplicate '" + nm + "' on this sheet symbol");
                if (!is_xy(pn.contains("pos") ? pn["pos"] : json()))
                    errs.push_back(ploc + ".pos: must be [x,y]");
                const std::string shape = string_value(pn, "shape", "passive");
                if (!sheet_pin_shapes().count(shape))
                    errs.push_back(ploc + ".shape: bad value '" + shape + "'");
                ++j;
            }
            ++i;
        }
    }

    if (doc.contains("title_block")) {
        if (!doc["title_block"].is_object()) {
            errs.push_back("title_block: must be an object");
        } else {
            for (const char* key : {"title", "company", "rev", "date", "comment1",
                                    "comment2", "comment3", "comment4", "paper"})
                if (doc["title_block"].contains(key) && !doc["title_block"][key].is_string())
                    errs.push_back(std::string("title_block.") + key + ": must be a string");
        }
    }

    for (auto it = cache.begin(); it != cache.end(); ++it) {
        const std::string& libid = it.key();
        const json& sdef = it.value();
        if (!sdef.is_object()) {
            errs.push_back("lib_cache[" + libid + "]: not an object");
            continue;
        }
        if (sdef.contains("pins") && sdef["pins"].is_array()) {
            int j = 0;
            for (const json& pin : sdef["pins"]) {
                const std::string ploc =
                    "lib_cache[" + libid + "].pins[" + std::to_string(j) + "]";
                const std::string type = string_value(pin, "type", "passive");
                if (!pin_types().count(type))
                    errs.push_back(ploc + ".type: bad value '" + type + "'");
                if (!is_xy(pin.contains("pos") ? pin["pos"] : json()))
                    errs.push_back(ploc + ".pos: must be [x,y]");
                if (pin.contains("number") && !pin["number"].is_string())
                    errs.push_back(ploc + ".number: must be a string");
                ++j;
            }
        }
    }
    return errs;
}

std::vector<std::string> validate_board(const json& doc) {
    std::vector<std::string> errs;
    if (doc.value("schema", std::string()) != kSchemaPcb)
        return {std::string("schema: expected '") + kSchemaPcb + "' got '" +
                doc.value("schema", std::string()) + "'"};
    if (!(doc.contains("id") && doc["id"].is_string())) errs.push_back("id: missing");

    json stack = doc.contains("stackup") ? doc["stackup"] : json::object();
    int n_cu = stack.contains("copper_layers") && stack["copper_layers"].is_number_integer()
                   ? stack["copper_layers"].get<int>()
                   : 2;
    if (n_cu < 1 || n_cu > kCopperLayersMax || (n_cu % 2 == 1 && n_cu != 1))
        errs.push_back("stackup.copper_layers: " + std::to_string(n_cu) + " not a supported count");
    std::set<std::string> layers_ok = valid_layers(n_cu);
    std::vector<std::string> cu_vec = copper_layer_ids(n_cu);
    std::set<std::string> cu_ok(cu_vec.begin(), cu_vec.end());

    auto get = [&](const char* k) { return doc.contains(k) ? doc[k] : json::array(); };
    check_ids(get("footprints"), "fp", "footprints", errs);
    check_ids(get("tracks"), "t", "tracks", errs);
    check_ids(get("vias"), "v", "vias", errs);
    check_ids(get("zones"), "z", "zones", errs);

    std::set<std::string> net_names;
    if (get("nets").is_array())
        for (const json& n : get("nets"))
            if (n.is_object()) net_names.insert(n.value("name", std::string()));
    auto check_net = [&](const json& val, const std::string& loc) {
        if (!val.is_null() && val.is_string() && !net_names.count(val.get<std::string>()))
            errs.push_back(loc + ": net '" + val.get<std::string>() + "' not in board nets");
    };

    if (get("footprints").is_array()) {
        int i = 0;
        for (const json& fp : get("footprints")) {
            std::string loc = "footprints[" + std::to_string(i) + "]";
            if (!is_xy(fp.contains("pos") ? fp["pos"] : json()))
                errs.push_back(loc + ".pos: must be [x,y]");
            std::string side = fp.value("side", std::string("top"));
            if (!fp_sides().count(side)) errs.push_back(loc + ".side: bad value '" + side + "'");
            if (fp.contains("pads") && fp["pads"].is_array()) {
                int k = 0;
                for (const json& pad : fp["pads"]) {
                    std::string ploc = loc + ".pads[" + std::to_string(k) + "]";
                    if (!is_xy(pad.contains("pos") ? pad["pos"] : json()))
                        errs.push_back(ploc + ".pos: must be [x,y]");
                    std::string shape = pad.value("shape", std::string("rect"));
                    if (!pad_shapes().count(shape))
                        errs.push_back(ploc + ".shape: bad value '" + shape + "'");
                    std::string ptype = pad.value("type", std::string("smd"));
                    if (!pad_types().count(ptype))
                        errs.push_back(ploc + ".type: bad value '" + ptype + "'");
                    bool has_drill = pad.contains("drill") && !pad["drill"].is_null() &&
                                     !(pad["drill"].is_number() && pad["drill"].get<double>() == 0.0);
                    if (ptype == "smd" && has_drill)
                        errs.push_back(ploc + ": smd pad must not have a drill");
                    if ((ptype == "tht" || ptype == "np_tht") && !has_drill)
                        errs.push_back(ploc + ": " + ptype + " pad requires a drill");
                    if (pad.contains("layers") && pad["layers"].is_array())
                        for (const json& lyr : pad["layers"])
                            if (lyr.is_string() && !layers_ok.count(lyr.get<std::string>()))
                                errs.push_back(ploc + ".layers: '" + lyr.get<std::string>() +
                                               "' not in stackup");
                    check_net(pad.contains("net") ? pad["net"] : json(nullptr), ploc + ".net");
                    ++k;
                }
            }
            ++i;
        }
    }

    if (get("tracks").is_array()) {
        int i = 0;
        for (const json& t : get("tracks")) {
            std::string loc = "tracks[" + std::to_string(i) + "]";
            std::string layer = t.value("layer", std::string());
            if (!cu_ok.count(layer)) errs.push_back(loc + ".layer: '" + layer + "' not a copper layer");
            std::string kind = t.value("kind", std::string("segment"));
            if (!track_kinds().count(kind)) errs.push_back(loc + ".kind: bad value '" + kind + "'");
            check_net(t.contains("net") ? t["net"] : json(nullptr), loc + ".net");
            ++i;
        }
    }

    if (get("vias").is_array()) {
        int i = 0;
        for (const json& v : get("vias")) {
            std::string loc = "vias[" + std::to_string(i) + "]";
            std::string type = v.value("type", std::string("through"));
            if (!via_types().count(type)) errs.push_back(loc + ".type: bad value '" + type + "'");
            if (v.contains("layers") && v["layers"].is_array())
                for (const json& lyr : v["layers"])
                    if (lyr.is_string() && !cu_ok.count(lyr.get<std::string>()))
                        errs.push_back(loc + ".layers: '" + lyr.get<std::string>() +
                                       "' not a copper layer");
            check_net(v.contains("net") ? v["net"] : json(nullptr), loc + ".net");
            ++i;
        }
    }

    if (get("zones").is_array()) {
        int i = 0;
        for (const json& z : get("zones")) {
            std::string loc = "zones[" + std::to_string(i) + "]";
            std::string layer = z.value("layer", std::string());
            if (!cu_ok.count(layer)) errs.push_back(loc + ".layer: '" + layer + "' not a copper layer");
            std::string fill = z.value("fill", std::string("solid"));
            if (!zone_fills().count(fill)) errs.push_back(loc + ".fill: bad value '" + fill + "'");
            check_net(z.contains("net") ? z["net"] : json(nullptr), loc + ".net");
            ++i;
        }
    }

    json outline = doc.contains("outline") ? doc["outline"] : json::object();
    bool closed = outline.value("closed", false);
    bool has_shapes = outline.contains("shapes") && outline["shapes"].is_array() &&
                      !outline["shapes"].empty();
    if (closed && !has_shapes) errs.push_back("outline: marked closed but has no shapes");
    return errs;
}

std::vector<std::string> validate(const json& doc) {
    if (is_schematic(doc)) return validate_schematic(doc);
    if (is_board(doc)) return validate_board(doc);
    return {std::string("schema: unknown '") + doc.value("schema", std::string()) + "'"};
}

}  // namespace chudware::ecad::schema
