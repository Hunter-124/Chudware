// ECAD schema implementation — see schema.h. Mirrors engine/ecad/schema.py.

#include "ecad/schema.h"

#include <cmath>
#include <map>
#include <regex>

namespace aicad::ecad::schema {

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
        {"s", "symbols"}, {"w", "wires"}, {"j", "junctions"}, {"l", "labels"},
        {"bus", "buses"}, {"pw", "power"}, {"nc", "no_connect"}, {"sh", "sheets"},
        {"g", "graphics"}, {"hs", "sheet_symbols"}};
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
        {"labels", json::array()}, {"buses", json::array()}, {"power", json::array()},
        {"no_connect", json::array()}, {"graphics", json::array()}, {"sheet_symbols", json::array()},
        {"lib_cache", json::object()}, {"annotations", json::object()},
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
    double px = pin["pos"][0].get<double>(), py = pin["pos"][1].get<double>();
    double length = pin.contains("length") && pin["length"].is_number() ? pin["length"].get<double>()
                                                                        : 2.54;
    double a = (pin.contains("angle") && pin["angle"].is_number() ? pin["angle"].get<double>() : 0.0)
               * 3.14159265358979323846 / 180.0;
    double tx = px + length * std::cos(a), ty = py + length * std::sin(a);
    double sang = symbol.contains("angle") && symbol["angle"].is_number() ? symbol["angle"].get<double>()
                                                                          : 0.0;
    std::string mir = symbol.value("mirror", std::string("none"));
    return local_to_world(tx, ty, symbol["pos"], sang, mir);
}

// ----------------------------------------------------------------- validation
namespace {
bool is_xy(const json& v) {
    return v.is_array() && v.size() == 2 && v[0].is_number() && v[1].is_number();
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
        std::string oid = it.value("id", std::string());
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
    check_ids(get("symbols"), "s", "symbols", errs);
    check_ids(get("wires"), "w", "wires", errs);
    check_ids(get("junctions"), "j", "junctions", errs);
    check_ids(get("labels"), "l", "labels", errs);
    check_ids(get("power"), "pw", "power", errs);
    check_ids(get("no_connect"), "nc", "no_connect", errs);
    check_ids(get("sheet_symbols"), "hs", "sheet_symbols", errs);

    std::map<std::string, std::string> refs_seen;
    if (get("symbols").is_array()) {
        int i = 0;
        for (const json& s : get("symbols")) {
            std::string loc = "symbols[" + std::to_string(i) + "]";
            std::string libid = s.value("libid", std::string());
            if (!cache.contains(libid))
                errs.push_back(loc + ".libid: '" + libid + "' not in lib_cache");
            if (!is_xy(s.contains("pos") ? s["pos"] : json()))
                errs.push_back(loc + ".pos: must be [x,y]");
            std::string mir = s.value("mirror", std::string("none"));
            if (!sym_mirror().count(mir)) errs.push_back(loc + ".mirror: bad value '" + mir + "'");
            std::string ref = s.value("ref", std::string());
            if (!ref.empty() && ref.back() != '?') {
                if (refs_seen.count(ref))
                    errs.push_back(loc + ".ref: duplicate '" + ref + "' (also " + refs_seen[ref] + ")");
                refs_seen[ref] = loc;
            }
            ++i;
        }
    }

    if (get("wires").is_array()) {
        int i = 0;
        for (const json& w : get("wires")) {
            const json& pts = w.contains("pts") ? w["pts"] : json();
            bool ok = pts.is_array() && pts.size() >= 2;
            if (ok) for (const json& p : pts) ok = ok && is_xy(p);
            if (!ok) errs.push_back("wires[" + std::to_string(i) + "].pts: need >=2 [x,y] points");
            ++i;
        }
    }

    if (get("labels").is_array()) {
        int i = 0;
        for (const json& lb : get("labels")) {
            std::string scope = lb.value("scope", std::string("local"));
            if (!label_scopes().count(scope))
                errs.push_back("labels[" + std::to_string(i) + "].scope: bad value '" + scope + "'");
            std::string text = lb.value("text", std::string());
            if (!(lb.contains("text") && lb["text"].is_string()) || text.empty())
                errs.push_back("labels[" + std::to_string(i) + "].text: required non-empty string");
            ++i;
        }
    }

    if (get("power").is_array()) {
        int i = 0;
        for (const json& p : get("power")) {
            std::string net = p.value("net", std::string());
            if (net.empty()) errs.push_back("power[" + std::to_string(i) + "].net: required non-empty");
            ++i;
        }
    }

    // hierarchical sheet symbols
    std::set<std::string> sheet_ids;
    if (get("sheets").is_array())
        for (const json& s : get("sheets"))
            if (s.is_object()) sheet_ids.insert(s.value("id", std::string()));
    if (get("sheet_symbols").is_array()) {
        int i = 0;
        for (const json& hs : get("sheet_symbols")) {
            std::string loc = "sheet_symbols[" + std::to_string(i) + "]";
            std::string child = hs.value("child", std::string());
            if (!(hs.contains("child") && hs["child"].is_string()) || child.empty())
                errs.push_back(loc + ".child: required (id of the instantiated child sheet)");
            else if (!sheet_ids.empty() && !sheet_ids.count(child))
                errs.push_back(loc + ".child: '" + child + "' not a known sheet id");
            if (!is_xy(hs.contains("pos") ? hs["pos"] : json()))
                errs.push_back(loc + ".pos: must be [x,y]");
            std::set<std::string> pin_names;
            json pins = hs.contains("pins") ? hs["pins"] : json::array();
            if (!pins.is_array()) {
                errs.push_back(loc + ".pins: must be a list");
                pins = json::array();
            }
            int j = 0;
            for (const json& pn : pins) {
                std::string ploc = loc + ".pins[" + std::to_string(j) + "]";
                if (!pn.is_object()) {
                    errs.push_back(ploc + ": not an object");
                    ++j;
                    continue;
                }
                std::string nm = pn.value("name", std::string());
                if (!(pn.contains("name") && pn["name"].is_string()) || nm.empty())
                    errs.push_back(ploc + ".name: required non-empty string");
                else if (pin_names.count(nm))
                    errs.push_back(ploc + ".name: duplicate '" + nm + "' on this sheet symbol");
                else
                    pin_names.insert(nm);
                if (!is_xy(pn.contains("pos") ? pn["pos"] : json()))
                    errs.push_back(ploc + ".pos: must be [x,y]");
                std::string shape = pn.value("shape", std::string("passive"));
                if (!sheet_pin_shapes().count(shape))
                    errs.push_back(ploc + ".shape: bad value '" + shape + "'");
                ++j;
            }
            ++i;
        }
    }

    // embedded symbol defs (pins)
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
                std::string ploc = "lib_cache[" + libid + "].pins[" + std::to_string(j) + "]";
                std::string type = pin.value("type", std::string("passive"));
                if (!pin_types().count(type)) errs.push_back(ploc + ".type: bad value '" + type + "'");
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

}  // namespace aicad::ecad::schema
