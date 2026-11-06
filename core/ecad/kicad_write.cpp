// KiCad 10 serialization.  KiCad remains an external process; this module
// reads and writes its documented S-expression interchange format only.
#include "ecad/kicad.h"
#include "ecad/kicad_libs.h"
#include "ecad/schema.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <map>
#include <random>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace chudware::ecad::kicad {
using nlohmann::json;
namespace {

std::string uuid4() {
    thread_local std::mt19937_64 rng(std::random_device{}());
    unsigned char b[16];
    for (auto& c : b) c = static_cast<unsigned char>(rng());
    b[6] = static_cast<unsigned char>((b[6] & 15) | 0x40);
    b[8] = static_cast<unsigned char>((b[8] & 63) | 0x80);
    char out[37];
    std::snprintf(out, sizeof(out), "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                  b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]);
    return out;
}
void ensure_uuid(json& object) {
    if (object.is_object() && (!object.contains("uuid") || !object["uuid"].is_string() || object["uuid"].get<std::string>().empty()))
        object["uuid"] = uuid4();
}
void assign_list(json& object, const char* key) {
    if (!object.contains(key) || !object[key].is_array()) return;
    for (auto& item : object[key]) ensure_uuid(item);
}
std::string node_tag(const json& n) {
    return n.is_array() && !n.empty() && n[0].is_string() ? n[0].get<std::string>() : "";
}
json* child(json& n, const std::string& key) {
    if (!n.is_array()) return nullptr;
    for (std::size_t i = 1; i < n.size(); ++i)
        if (node_tag(n[i]) == key) return &n[i];
    return nullptr;
}
const json* child(const json& n, const std::string& key) {
    if (!n.is_array()) return nullptr;
    for (std::size_t i = 1; i < n.size(); ++i)
        if (node_tag(n[i]) == key) return &n[i];
    return nullptr;
}
void replace_child(json& n, json value) {
    if (json* old = child(n, node_tag(value))) *old = std::move(value);
    else n.push_back(std::move(value));
}
void remove_children(json& n, const std::string& key) {
    for (std::size_t i = n.size(); i > 1; --i)
        if (node_tag(n[i - 1]) == key) n.erase(i - 1);
}
std::string string_at(const json& j, const char* key, const std::string& fallback = "") {
    return j.is_object() && j.contains(key) && j[key].is_string() ? j[key].get<std::string>() : fallback;
}
double number_at(const json& j, const char* key, double fallback) {
    return j.is_object() && j.contains(key) && j[key].is_number() ? j[key].get<double>() : fallback;
}
double coordinate(const json& p, std::size_t i) {
    return p.is_array() && p.size() > i && p[i].is_number() ? p[i].get<double>() : 0.;
}
json xy(const char* key, const json& p, bool flip = true) {
    return json::array({key, coordinate(p, 0), (flip ? -1. : 1.) * coordinate(p, 1)});
}
json at(const json& obj, const char* key = "pos", bool angle = true) {
    const json p = obj.value(key, json::array({0, 0}));
    json result = xy("at", p);
    if (angle) result.push_back(number_at(obj, "angle", 0));
    return result;
}
json pts(const json& points) {
    json n = json::array({"pts"});
    if (points.is_array()) for (const auto& p : points) n.push_back(xy("xy", p));
    return n;
}
json effects(double size = 1.27) {
    return json::array({"effects", json::array({"font", json::array({"size", size, size})})});
}
json stroke(double width = 0.) {
    return json::array({"stroke", json::array({"width", width}), json::array({"type", "default"})});
}
json raw_node(const json& object) {
    if (object.contains("kicad_tag") && object["kicad_tag"].is_string() &&
        object.contains("kicad_extra") && object["kicad_extra"].is_array()) {
        json node = json::array({object["kicad_tag"]});
        for (const auto& item : object["kicad_extra"]) node.push_back(item);
        return node;
    }
    return json();
}
bool changed(const json& object, const char* key) {
    if (!object.contains("kicad_original") || !object["kicad_original"].is_object()) return true;
    const json& old = object["kicad_original"];
    if (object.contains(key) != old.contains(key)) return true;
    return object.contains(key) && object[key] != old[key];
}
void set_uuid(json& node, const json& object) {
    const std::string id = string_at(object, "uuid");
    if (id.empty()) throw std::runtime_error("KiCad document has unassigned UUIDs; call assign_uuids first");
    if (const json* old = child(node, "uuid"); old && old->size() > 1 && (*old)[1] == id) return;
    replace_child(node, json::array({"uuid", id}));
}
void set_property(json& node, const std::string& name, const std::string& value, const json& pos, bool board = false) {
    for (std::size_t i = 1; i < node.size(); ++i) {
        json& p = node[i];
        if (node_tag(p) == "property" && p.size() >= 3 && p[1] == name) { p[2] = value; return; }
        if (node_tag(p) == "fp_text" && p.size() >= 3 &&
            ((name == "Reference" && p[1] == "reference") || (name == "Value" && p[1] == "value"))) {
            p[2] = value; return;
        }
    }
    json position = xy("at", pos);
    position.push_back(0);
    json p = json::array({"property", name, value, std::move(position), effects()});
    if (board) p.insert(p.begin() + 4, json::array({"layer", "F.Fab"}));
    if (name.rfind("Chudware.", 0) == 0) {
        if (board) p.insert(p.end() - 1, json::array({"hide", "yes"}));
        else p.back().push_back(json::array({"hide", "yes"}));
    }
    node.push_back(std::move(p));
}
void erase_property(json& node, const std::string& name) {
    for (std::size_t i = node.size(); i > 1; --i)
        if (node_tag(node[i - 1]) == "property" && node[i - 1].size() > 1 && node[i - 1][1] == name)
            node.erase(i - 1);
}
std::string quote(const std::string& s) {
    std::string out = "\"";
    for (char c : s) {
        switch (c) { case '\\': out += "\\\\"; break; case '"': out += "\\\""; break;
            case '\n': out += "\\n"; break; case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break; default: out += c; }
    }
    return out + '"';
}
bool bare_argument(const std::string& tag, const std::string& parent, std::size_t index, const std::string& s) {
    static const std::set<std::string> literal_tags{"property", "symbol", "footprint", "lib_id", "lib_name", "paper", "title", "date", "rev", "company", "comment", "name", "number", "uuid", "tstamp", "path", "project", "page", "sheetname", "sheetfile", "descr", "description", "tags", "model", "layer", "layers", "net", "net_name", "text", "text_box", "gr_text", "gr_text_box", "label", "global_label", "hierarchical_label", "bus_alias", "members", "reference", "value", "generator", "generator_version", "material", "color", "copper_finish", "outputdirectory", "font_name", "face", "url", "filename", "file", "data", "embedded_file", "exclude_from_sim"};
    if (tag == "pin") {
        static const std::set<std::string> pin_types{
            "input", "output", "bidirectional", "tri_state", "passive", "unspecified",
            "power_in", "power_out", "open_collector", "open_emitter", "no_connect", "free"};
        if (parent == "sheet") return index == 2;
        return index == 2 || (index == 1 && pin_types.count(s));
    }
    if (tag == "pad") return index == 2 || index == 3;
    if (tag == "fp_text") return index == 1;
    if (tag == "type") return parent != "layer";
    if (tag == "layer" && index > 1 && (s == "knockout" || s == "addsublayer")) return true;
    if (tag == "data" && s.size() >= 2 && s.front() == '|' && s.back() == '|') return true;
    if (tag == "alternate") return index > 1;
    if (literal_tags.count(tag)) {
        if (tag == "exclude_from_sim") return s == "yes" || s == "no";
        return false;
    }
    if (s == "yes" || s == "no" || s == "hide" || s == "locked" ||
        s == "front" || s == "back" || s == "none" || s == "solid" ||
        s == "default" || s == "input" || s == "output" || s == "passive" ||
        s == "bidirectional" || s == "tri_state" || s == "power_in" || s == "power_out" ||
        s == "line" || s == "circle" || s == "oval" || s == "rect" || s == "roundrect" ||
        s == "x" || s == "y" || s == "left" || s == "right" || s == "top" || s == "bottom" ||
        s == "mirror" || s == "normal" || s == "smd" || s == "through_hole" ||
        s == "exclude_from_pos_files" || s == "exclude_from_bom" || s == "virtual" ||
        s == "inset" || s == "edge" || s == "hatch" || s == "full" || s == "free" ||
        s == "signal" || s == "user" || s == "power" || s == "mixed" || s == "jumper" ||
        s == "blind" || s == "micro" || s == "thru_hole" || s == "np_thru_hole" ||
        s == "bold" || s == "italic" || s == "filled" || s == "unlocked" ||
        s == "outward" || s == "inward" || s == "outline" ||
        s == "allow_soldermask_bridges" || s == "allow_missing_courtyard" ||
        s == "board_only" || s == "dnp" || s == "demorgan" || s == "top_left" ||
        s == "top_right" || s == "bottom_left" || s == "bottom_right" ||
        s == "allowed" || s == "not_allowed" || s == "global" || s == "round" ||
        s == "fillet") return true;
    return s.size() > 2 && s[0] == '0' && s[1] == 'x';
}
void render(std::ostringstream& out, const json& n, int depth, const std::string& parent = "") {
    if (!n.is_array() || n.empty()) throw std::runtime_error("Invalid KiCad S-expression node");
    const std::string tag = node_tag(n);
    out << '(';
    if (n[0].is_string()) out << n[0].get<std::string>();
    else out << n[0].dump();
    bool children = false;
    for (std::size_t i = 1; i < n.size(); ++i) {
        const json& v = n[i];
        if (v.is_array()) {
            children = true;
            out << '\n' << std::string(static_cast<std::size_t>(depth + 1), '\t');
            render(out, v, depth + 1, tag);
        } else if (v.is_string()) {
            const std::string s = v.get<std::string>();
            out << ' ' << (bare_argument(tag, parent, i, s) ? s : quote(s));
        } else if (v.is_number()) {
            if (v.is_number_float() && !std::isfinite(v.get<double>()))
                throw std::runtime_error("Non-finite KiCad coordinate");
            out << ' ' << v.dump();
        } else throw std::runtime_error("Invalid KiCad S-expression atom");
    }
    if (children) out << '\n' << std::string(static_cast<std::size_t>(depth), '\t');
    out << ')';
}
std::string serialize(const json& root) {
    std::ostringstream out;
    render(out, root, 0);
    out << '\n';
    return out.str();
}
json synthetic_symbol(const std::string& libid, const json& def) {
    const auto colon = libid.find(':');
    const std::string name = string_at(def, "name", colon == std::string::npos ? libid : libid.substr(colon + 1));
    json result = json::array({"symbol", libid});
    if (def.value("power", false)) result.push_back(json::array({"power"}));
    for (const auto& field : std::vector<std::pair<std::string, std::string>>{
            {"Reference", string_at(def, "prefix", "U")}, {"Value", string_at(def, "value", name)},
            {"Footprint", string_at(def, "default_footprint")}, {"Datasheet", string_at(def, "datasheet")}})
        result.push_back(json::array({"property", field.first, field.second, json::array({"at", 0, 0, 0}), effects()}));
    json body = json::array({"symbol", name + "_0_1"});
    if (def.contains("graphics") && def["graphics"].is_array()) {
        for (const auto& g : def["graphics"]) {
            const std::string kind = string_at(g, "kind", string_at(g, "type"));
            const json points = g.value("pts", json::array());
            json shape;
            if ((kind == "rect" || kind == "rectangle") && points.size() >= 2)
                shape = json::array({"rectangle", xy("start", points[0], false), xy("end", points[1], false)});
            else if ((kind == "polyline" || kind == "line") && points.size() >= 2) {
                json path = json::array({"pts"});
                for (const auto& point : points) path.push_back(xy("xy", point, false));
                shape = json::array({"polyline", std::move(path)});
            } else if (kind == "circle" && !points.empty())
                shape = json::array({"circle", xy("center", points[0], false), json::array({"radius", number_at(g, "radius", 1)})});
            else continue;
            shape.push_back(stroke(number_at(g, "width", 0)));
            shape.push_back(json::array({"fill", json::array({"type", "none"})}));
            body.push_back(std::move(shape));
        }
    }
    result.push_back(std::move(body));
    std::map<int, json> units;
    if (def.contains("pins") && def["pins"].is_array()) for (const auto& p : def["pins"]) {
        const int unit = p.value("unit", 1);
        if (!units.count(unit)) units[unit] = json::array({"symbol", name + "_" + std::to_string(unit) + "_1"});
        const std::string pname = string_at(p, "name");
        const double length = number_at(p, "length", 2.54);
        const double angle = number_at(p, "angle", 0);
        const double radians = angle * 3.14159265358979323846 / 180.0;
        const json pos = p.value("pos", json::array({0, 0}));
        const json pin_at = json::array({"at", coordinate(pos, 0) + length * std::cos(radians),
                                        coordinate(pos, 1) + length * std::sin(radians),
                                        std::fmod(angle + 180.0 + 360.0, 360.0)});
        units[unit].push_back(json::array({"pin", string_at(p, "type", "passive"), string_at(p, "shape", "line"),
            pin_at, json::array({"length", length}),
            json::array({"name", pname, effects()}),
            json::array({"number", string_at(p, "number", "1"), effects()})}));
    }
    for (auto& entry : units) result.push_back(std::move(entry.second));
    return result;
}

void move_properties(json& node, const json& object) {
    if (!changed(object, "pos") || !object.contains("kicad_original") ||
        !object["kicad_original"].contains("pos")) return;
    const json& old = object["kicad_original"]["pos"];
    const json now = object.value("pos", json::array({0, 0}));
    const double dx = coordinate(now, 0) - coordinate(old, 0);
    const double dy = -(coordinate(now, 1) - coordinate(old, 1));
    for (auto& part : node) {
        if (node_tag(part) != "property") continue;
        json* p = child(part, "at");
        if (p && p->size() >= 3 && (*p)[1].is_number() && (*p)[2].is_number()) {
            (*p)[1] = (*p)[1].get<double>() + dx;
            (*p)[2] = (*p)[2].get<double>() + dy;
        }
    }
}

std::string power_libid(const json& object) {
    const std::string net = string_at(object, "net", "GND");
    std::string symbol = string_at(object, "symbol", net);
    if (symbol.empty()) symbol = net;
    const std::string symbol_id = symbol.find(':') == std::string::npos ?
        "power:" + symbol : symbol;
    const bool imported = object.contains("kicad_original") && object["kicad_original"].is_object();
    if (imported && changed(object, "symbol")) return symbol_id;
    const std::string libid = string_at(object, "libid", symbol_id);
    if (libid == "power:PWR_FLAG") return libid;
    if (imported && changed(object, "net") && !net.empty()) return "power:" + net;
    return libid;
}

json schematic_symbol(const json& object, bool power, const std::string& root_uuid) {
    json n = raw_node(object);
    const bool fresh = n.is_null();
    const std::string libid = power ? power_libid(object) : string_at(object, "libid", "Device:Unknown");
    const json pos = object.value("pos", json::array({0, 0}));
    const std::string ref = string_at(object, "ref", power ? "#PWR?" : "U?");
    if (fresh) {
        n = json::array({"symbol", json::array({"lib_id", libid}), at(object),
                         json::array({"unit", object.value("unit", 1)}),
                         json::array({"in_bom", object.value("in_bom", true) ? "yes" : "no"}),
                         json::array({"on_board", object.value("on_board", true) ? "yes" : "no"}),
                         json::array({"dnp", object.value("dnp", false) ? "yes" : "no"})});
    } else {
        move_properties(n, object);
        if (changed(object, "pos") || changed(object, "angle")) replace_child(n, at(object));
        if (changed(object, "libid") || (power && (changed(object, "symbol") || changed(object, "net"))))
            replace_child(n, json::array({"lib_id", libid}));
        if (changed(object, "unit")) replace_child(n, json::array({"unit", object.value("unit", 1)}));
        for (const char* key : {"in_bom", "on_board", "dnp"})
            if (changed(object, key)) replace_child(n, json::array({key, object.value(key, std::string(key) != "dnp") ? "yes" : "no"}));
    }
    if (fresh || changed(object, "mirror")) {
        const std::string mirror = string_at(object, "mirror", "none");
        if (mirror == "none") remove_children(n, "mirror");
        else replace_child(n, json::array({"mirror", mirror}));
    }
    for (const auto& field : std::vector<std::pair<const char*, const char*>>{
             {"ref", "Reference"}, {"value", "Value"}, {"footprint", "Footprint"}, {"datasheet", "Datasheet"}}) {
        const bool power_value = power && std::string(field.first) == "value";
        const bool flag = power && libid == "power:PWR_FLAG";
        if (fresh || changed(object, field.first) || (power_value && !flag && changed(object, "net"))) {
            const std::string value = std::string(field.first) == "ref" ? ref :
                power_value && flag ? string_at(object, "value", "PWR_FLAG") :
                power_value && changed(object, "net") ? string_at(object, "net") :
                string_at(object, field.first, power_value ? string_at(object, "net") : "");
            set_property(n, field.second, value, pos);
        }
    }
    if (power && (fresh || changed(object, "net") || changed(object, "symbol") || changed(object, "libid"))) {
        if (libid == "power:PWR_FLAG")
            set_property(n, "Chudware.PowerNet", string_at(object, "net"), pos);
        else
            erase_property(n, "Chudware.PowerNet");
    }
    if (fresh || changed(object, "fields")) {
        if (object.contains("kicad_original") && object["kicad_original"].contains("fields") &&
            object["kicad_original"]["fields"].is_object())
            for (const auto& [name, value] : object["kicad_original"]["fields"].items())
                if (!object.contains("fields") || !object["fields"].is_object() ||
                    !object["fields"].contains(name) || !object["fields"][name].is_string())
                    erase_property(n, name);
        json typed = json::object();
        if (object.contains("fields") && object["fields"].is_object()) {
            for (const auto& [name, value] : object["fields"].items()) {
                if (value.is_string()) set_property(n, name, value.get<std::string>(), pos);
                else typed[name] = value;
            }
        }
        if (!typed.empty()) set_property(n, "Chudware.Fields", typed.dump(), pos);
        else erase_property(n, "Chudware.Fields");
    }
    if (fresh || changed(object, "sim_params")) {
        if (object.contains("sim_params") && !object["sim_params"].is_null())
            set_property(n, "Chudware.SimParams", object["sim_params"].dump(), pos);
        else erase_property(n, "Chudware.SimParams");
    }
    if (object.contains("kicad_pins") && object["kicad_pins"].is_object()) {
        for (const auto& [number, id] : object["kicad_pins"].items()) {
            json* pin = nullptr;
            for (auto& part : n)
                if (node_tag(part) == "pin" && part.size() > 1 && part[1] == number) {
                    pin = &part;
                    break;
                }
            if (pin) replace_child(*pin, json::array({"uuid", id}));
            else n.push_back(json::array({"pin", number, json::array({"uuid", id})}));
        }
    }
    if (fresh || object.value("kicad_copy", false)) {
        if (!fresh) remove_children(n, "instances");
        n.push_back(json::array({"instances", json::array({"project", "chudware",
            json::array({"path", "/" + root_uuid, json::array({"reference", ref}),
                         json::array({"unit", object.value("unit", 1)})})})}));
    } else if (changed(object, "ref") || changed(object, "unit")) {
        if (json* instances = child(n, "instances")) for (auto& project : *instances)
            if (node_tag(project) == "project") for (auto& path : project)
                if (node_tag(path) == "path") {
                    if (changed(object, "ref")) replace_child(path, json::array({"reference", ref}));
                    if (changed(object, "unit")) replace_child(path, json::array({"unit", object.value("unit", 1)}));
                }
    }
    set_uuid(n, object);
    return n;
}

json schematic_graphic(const json& object) {
    json n = raw_node(object);
    if (!n.is_null() && !changed(object, "kind") && !changed(object, "pts") &&
        !changed(object, "width") && !changed(object, "filled") &&
        !changed(object, "radius") && !changed(object, "a0") && !changed(object, "a1")) {
        set_uuid(n, object);
        return n;
    }
    const std::string kind = string_at(object, "kind", string_at(object, "type", "polyline"));
    const json points = object.value("pts", json::array());
    if (kind == "rect" && points.size() >= 2)
        n = json::array({"rectangle", xy("start", points[0]), xy("end", points[1])});
    else if (kind == "circle" && !points.empty())
        n = json::array({"circle", xy("center", points[0]), json::array({"radius", number_at(object, "radius", 1)})});
    else if (kind == "arc" && object.contains("kicad_start") && object.contains("kicad_mid") && object.contains("kicad_end"))
        n = json::array({"arc", xy("start", object["kicad_start"]), xy("mid", object["kicad_mid"]), xy("end", object["kicad_end"])});
    else n = json::array({"polyline", pts(points)});
    n.push_back(stroke(number_at(object, "width", 0)));
    n.push_back(json::array({"fill", json::array({"type", object.value("filled", false) ? "background" : "none"})}));
    set_uuid(n, object);
    return n;
}

json schematic_object(const json& object, const std::string& category, const json& doc) {
    const std::string root_uuid = string_at(doc.at("kicad"), "uuid");
    if (category == "symbols" || category == "power")
        return schematic_symbol(object, category == "power", root_uuid);
    if (category == "graphics") return schematic_graphic(object);
    json n = raw_node(object);
    const bool fresh = n.is_null();
    if (category == "wires" || category == "buses") {
        if (fresh) n = json::array({category == "wires" ? "wire" : "bus", pts(object.value("pts", json::array())), stroke()});
        else if (changed(object, "pts")) replace_child(n, pts(object.value("pts", json::array())));
    } else if (category == "junctions" || category == "no_connect") {
        if (fresh) {
            n = json::array({category == "junctions" ? "junction" : "no_connect", at(object, "pos", false)});
            if (category == "junctions") {
                n.push_back(json::array({"diameter", 0}));
                n.push_back(json::array({"color", 0, 0, 0, 0}));
            }
        } else if (changed(object, "pos")) replace_child(n, at(object, "pos", false));
    } else if (category == "bus_entries") {
        if (fresh) n = json::array({"bus_entry", at(object, "pos", false), xy("size", object.value("size", json::array({2.54, -2.54}))), stroke()});
        else {
            if (changed(object, "pos")) replace_child(n, at(object, "pos", false));
            if (changed(object, "size")) replace_child(n, xy("size", object.value("size", json::array())));
        }
    } else if (category == "labels" || category == "texts") {
        const std::string scope = string_at(object, "scope", "local");
        const std::string kind = category == "texts" ? (object.value("text_box", false) ? "text_box" : "text") :
            (scope == "global" ? "global_label" : scope == "hier" ? "hierarchical_label" : "label");
        if (fresh) {
            n = json::array({kind, string_at(object, "text"), at(object)});
            if (category == "labels" && scope != "local") n.push_back(json::array({"shape", "input"}));
            if (category == "texts" && object.value("text_box", false))
                n.push_back(xy("size", object.value("box_size", json::array({25.4, 12.7})), false));
            n.push_back(effects(number_at(object, "size", 1.27)));
        } else {
            if (changed(object, "scope") || changed(object, "text_box")) n[0] = kind;
            if (changed(object, "text") && n.size() > 1) n[1] = string_at(object, "text");
            if (changed(object, "pos") || changed(object, "angle")) replace_child(n, at(object));
            if (changed(object, "box_size")) replace_child(n, xy("size", object.value("box_size", json::array()), false));
            if (changed(object, "size")) replace_child(n, effects(number_at(object, "size", 1.27)));
        }
    } else if (category == "sheet_symbols") {
        const json* sheet = nullptr;
        if (doc.contains("sheets") && doc["sheets"].is_array())
            for (const auto& item : doc["sheets"])
                if (string_at(item, "id") == string_at(object, "child")) { sheet = &item; break; }
        const std::string name = sheet ? string_at(*sheet, "name", "Sheet") : "Sheet";
        const std::string file = sheet ? string_at(*sheet, "instances_of", name + ".kicad_sch") : name + ".kicad_sch";
        if (fresh) n = json::array({"sheet", at(object, "pos", false), xy("size", object.value("size", json::array({100, 75})), false),
                                    stroke(), json::array({"fill", json::array({"color", 0, 0, 0, 0})})});
        else {
            if (changed(object, "pos")) replace_child(n, at(object, "pos", false));
            if (changed(object, "size")) replace_child(n, xy("size", object.value("size", json::array()), false));
        }
        set_property(n, "Sheetname", name, object.value("pos", json::array()));
        set_property(n, "Sheetfile", file, object.value("pos", json::array()));
        if (fresh || changed(object, "pins")) {
            remove_children(n, "pin");
            if (object.contains("pins") && object["pins"].is_array()) for (const auto& pin : object["pins"]) {
                json p = raw_node(pin);
                if (p.is_null()) p = json::array({"pin", string_at(pin, "name"), string_at(pin, "shape", "passive"), at(pin), effects()});
                else {
                    if (changed(pin, "name")) p[1] = string_at(pin, "name");
                    if (changed(pin, "shape")) p[2] = string_at(pin, "shape", "passive");
                    if (changed(pin, "pos") || changed(pin, "angle")) replace_child(p, at(pin));
                }
                set_uuid(p, pin);
                n.push_back(std::move(p));
            }
        }
        if (fresh || object.value("kicad_copy", false)) {
            if (!fresh) remove_children(n, "instances");
            n.push_back(json::array({"instances", json::array({"project", "chudware",
                json::array({"path", "/" + root_uuid, json::array({"page", "2"})})})}));
        }
    }
    set_uuid(n, object);
    return n;
}

using NetCodes = std::map<std::string, int>;
int net_code(const json& object, const NetCodes& nets) {
    const std::string name = string_at(object, "net");
    const auto it = nets.find(name);
    return it == nets.end() ? 0 : it->second;
}
json pad_node(const json& object, json base, const NetCodes& nets, bool named_nets) {
    json n = raw_node(object);
    const bool fresh = n.is_null();
    if (fresh) n = base.is_array() ? std::move(base) : json::array({"pad", "", "smd", "rect"});
    while (n.size() < 4) n.push_back("");
    if (fresh || changed(object, "number")) n[1] = string_at(object, "number");
    if (fresh || changed(object, "type")) {
        const std::string type = string_at(object, "type", "smd");
        n[2] = type == "tht" ? "thru_hole" : type == "np_tht" ? "np_thru_hole" :
               type == "connector" ? "connect" : type;
    }
    if (fresh || changed(object, "shape")) n[3] = string_at(object, "shape", "rect");
    if (fresh || changed(object, "pos") || changed(object, "angle")) replace_child(n, at(object));
    if (fresh || changed(object, "size")) replace_child(n, xy("size", object.value("size", json::array({1, 1})), false));
    if (fresh || changed(object, "layers")) {
        json layers = json::array({"layers"});
        if (object.contains("layers") && object["layers"].is_array())
            for (const auto& layer : object["layers"]) layers.push_back(layer);
        if (layers.size() == 1) { layers.push_back("F.Cu"); layers.push_back("F.Paste"); layers.push_back("F.Mask"); }
        replace_child(n, std::move(layers));
    }
    if (fresh || changed(object, "roundrect_ratio")) {
        if (string_at(object, "shape") == "roundrect")
            replace_child(n, json::array({"roundrect_rratio", number_at(object, "roundrect_ratio", 0.25)}));
        else remove_children(n, "roundrect_rratio");
    }
    if (fresh || changed(object, "drill")) {
        remove_children(n, "drill");
        if (object.contains("drill") && !object["drill"].is_null()) {
            const json& d = object["drill"];
            if (d.is_number()) n.push_back(json::array({"drill", d}));
            else if (d.is_object()) {
                json drill = json::array({"drill"});
                if (string_at(d, "shape") == "oval") drill.push_back("oval");
                drill.push_back(number_at(d, "diameter", 0.6));
                if (d.contains("width") && d["width"].is_number()) drill.push_back(d["width"]);
                if (d.contains("offset")) drill.push_back(xy("offset", d["offset"]));
                n.push_back(std::move(drill));
            }
        }
    }
    if (fresh || changed(object, "net")) {
        remove_children(n, "net");
        const std::string name = string_at(object, "net");
        if (!name.empty()) {
            if (named_nets) n.push_back(json::array({"net", name}));
            else if (const int code = net_code(object, nets))
                n.push_back(json::array({"net", code, name}));
        }
    }
    set_uuid(n, object);
    return n;
}
bool footprint_graphic(const std::string& tag) {
    return tag == "fp_line" || tag == "fp_rect" || tag == "fp_circle" || tag == "fp_arc" ||
           tag == "fp_poly" || tag == "fp_text";
}
json synthesize_footprint_graphic(const json& graphic) {
    const std::string kind = string_at(graphic, "type", "line");
    json n;
    if (kind == "line" || kind == "rect") n = json::array({kind == "rect" ? "fp_rect" : "fp_line",
        xy("start", graphic.value("p1", json::array())), xy("end", graphic.value("p2", json::array()))});
    else if (kind == "arc") {
        const json p1 = graphic.value("p1", json::array({0, 0}));
        const json p2 = graphic.value("p2", json::array({0, 0}));
        const json mid = graphic.value("mid", json::array({(coordinate(p1, 0) + coordinate(p2, 0)) / 2,
                                                            (coordinate(p1, 1) + coordinate(p2, 1)) / 2}));
        n = json::array({"fp_arc", xy("start", p1), xy("mid", mid), xy("end", p2)});
    } else if (kind == "circle") {
        const json center = graphic.value("center", json::array({0, 0}));
        const json end = json::array({coordinate(center, 0) + number_at(graphic, "radius", 1), coordinate(center, 1)});
        n = json::array({"fp_circle", xy("center", center), xy("end", end)});
    } else if (kind == "polygon" || kind == "polyline") {
        n = json::array({"fp_poly", pts(graphic.value("pts", json::array()))});
    } else if (kind == "text") {
        n = json::array({"fp_text", "user", string_at(graphic, "text"), at(graphic),
                         json::array({"layer", string_at(graphic, "layer", "F.SilkS")}), effects()});
        set_uuid(n, graphic);
        return n;
    } else throw std::runtime_error("Unsupported native footprint graphic: " + kind);
    n.push_back(stroke(number_at(graphic, "width", 0.12)));
    n.push_back(json::array({"layer", string_at(graphic, "layer", "F.SilkS")}));
    set_uuid(n, graphic);
    return n;
}
json footprint_node(const json& object, const NetCodes& nets, bool named_nets) {
    json n = raw_node(object);
    const bool fresh = n.is_null();
    if (fresh) {
        if (!object.value("_chudware_custom", false)) {
            const auto lib = kicad_libs::footprint_sexpr(string_at(object, "fpid"));
            if (lib && lib->is_array()) n = *lib;
        }
        if (n.is_null()) n = json::array({"footprint", string_at(object, "fpid", "Unknown")});
        if (n.size() < 2) n.push_back(string_at(object, "fpid", "Unknown"));
        n[1] = string_at(object, "fpid", "Unknown");
        remove_children(n, "version");
        remove_children(n, "generator");
        remove_children(n, "generator_version");
    }
    if (fresh || changed(object, "fpid")) n[1] = string_at(object, "fpid", "Unknown");
    if (fresh || changed(object, "pos") || changed(object, "angle")) replace_child(n, at(object));
    if (fresh || changed(object, "side"))
        replace_child(n, json::array({"layer", string_at(object, "side", "top") == "bottom" ? "B.Cu" : "F.Cu"}));
    const json pos = object.value("pos", json::array({0, 0}));
    if (fresh || changed(object, "ref")) set_property(n, "Reference", string_at(object, "ref", "U?"), pos, true);
    if (fresh || changed(object, "value")) set_property(n, "Value", string_at(object, "value"), pos, true);
    for (const auto& mapping : std::vector<std::pair<const char*, const char*>>{
             {"eng_overrides", "Chudware.Engineering"}, {"fields", "Chudware.Fields"},
             {"libid", "Chudware.LibId"}, {"on_board", "Chudware.OnBoard"},
             {"linked_symbol", "Chudware.LinkedSymbol"}}) {
        if (!fresh && !changed(object, mapping.first)) continue;
        if (object.contains(mapping.first) && !object[mapping.first].is_null())
            set_property(n, mapping.second, object[mapping.first].dump(), pos, true);
        else erase_property(n, mapping.second);
    }
    if (fresh || changed(object, "attr")) {
        const std::string attr = string_at(object, "attr", "smd");
        replace_child(n, json::array({"attr", attr == "tht" ? "through_hole" : attr}));
    }
    json rebuilt = json::array();
    std::size_t pad_index = 0, graphic_index = 0;
    const json pads = object.value("pads", json::array());
    const json graphics = object.value("graphics", json::array());
    for (auto& part : n) {
        if (node_tag(part) == "pad") {
            if (pads.is_array() && pad_index < pads.size())
                rebuilt.push_back(pad_node(pads[pad_index++], part, nets, named_nets));
        } else {
            if (footprint_graphic(node_tag(part)) && graphics.is_array() && graphic_index < graphics.size()) {
                // The native importer recognizes this same set, but may reject
                // malformed graphics; match their retained node when available.
                const json& g = graphics[graphic_index];
                const json original = raw_node(g);
                if (original.is_null() || original == part) {
                    set_uuid(part, g);
                    ++graphic_index;
                }
            }
            rebuilt.push_back(std::move(part));
        }
    }
    if (pads.is_array()) while (pad_index < pads.size())
        rebuilt.push_back(pad_node(pads[pad_index++], json(), nets, named_nets));
    if (fresh && graphics.is_array()) while (graphic_index < graphics.size())
        rebuilt.push_back(synthesize_footprint_graphic(graphics[graphic_index++]));
    n = std::move(rebuilt);
    if ((fresh || changed(object, "model_3d")) && object.contains("model_3d")) {
        if (!object["model_3d"].is_string() || object["model_3d"].get<std::string>().empty())
            remove_children(n, "model");
        else if (json* model = child(n, "model"); model && model->size() > 1)
            (*model)[1] = object["model_3d"];
        else n.push_back(json::array({"model", object["model_3d"],
            json::array({"offset", json::array({"xyz", 0, 0, 0})}),
            json::array({"scale", json::array({"xyz", 1, 1, 1})}),
            json::array({"rotate", json::array({"xyz", 0, 0, 0})})}));
    }
    set_uuid(n, object);
    return n;
}
json arc_endpoint(const json& object, const char* angle_key) {
    const json center = object.value("center", json::array({0, 0}));
    const double angle = number_at(object, angle_key, 0) * 3.14159265358979323846 / 180.;
    const double radius = number_at(object, "radius", 0);
    return json::array({coordinate(center, 0) + radius * std::cos(angle),
                        coordinate(center, 1) + radius * std::sin(angle)});
}
json arc_mid(const json& object) {
    if (object.contains("mid")) return object["mid"];
    const json center = object.value("center", json::array({0, 0}));
    const json p1 = object.value("p1", json::array({0, 0}));
    const double radius = object.contains("radius") ? number_at(object, "radius", 0) :
        std::hypot(coordinate(p1, 0) - coordinate(center, 0),
                   coordinate(p1, 1) - coordinate(center, 1));
    const double angle = (number_at(object, "a0", 0) + number_at(object, "a1", 0)) *
                         3.14159265358979323846 / 360.;
    return json::array({coordinate(center, 0) + radius * std::cos(angle),
                        coordinate(center, 1) + radius * std::sin(angle)});
}
json board_object(const json& object, const std::string& category, const NetCodes& nets, bool named_nets) {
    if (category == "footprints") return footprint_node(object, nets, named_nets);
    json n = raw_node(object);
    const bool fresh = n.is_null();
    if (category == "tracks") {
        const bool arc = string_at(object, "kind", "segment") == "arc";
        if (fresh) n = json::array({arc ? "arc" : "segment"});
        if (fresh || changed(object, "kind")) n[0] = arc ? "arc" : "segment";
        if (fresh || changed(object, "p1")) replace_child(n, xy("start", object.value("p1", json::array())));
        if (arc && (fresh || changed(object, "mid") || changed(object, "center") || changed(object, "a0") || changed(object, "a1")))
            replace_child(n, xy("mid", arc_mid(object)));
        if (fresh || changed(object, "p2")) replace_child(n, xy("end", object.value("p2", json::array())));
        if (fresh || changed(object, "width")) replace_child(n, json::array({"width", number_at(object, "width", 0.25)}));
        if (fresh || changed(object, "layer")) replace_child(n, json::array({"layer", string_at(object, "layer", "F.Cu")}));
        if (fresh || changed(object, "net"))
            replace_child(n, named_nets ? json::array({"net", string_at(object, "net")}) :
                                          json::array({"net", net_code(object, nets)}));
    } else if (category == "vias") {
        if (fresh) n = json::array({"via"});
        if (fresh || changed(object, "at")) replace_child(n, at(object, "at", false));
        if (fresh || changed(object, "diameter")) replace_child(n, json::array({"size", number_at(object, "diameter", 0.8)}));
        if (fresh || changed(object, "drill")) replace_child(n, json::array({"drill", number_at(object, "drill", 0.4)}));
        if (fresh || changed(object, "layers")) {
            json layers = json::array({"layers"});
            for (const auto& layer : object.value("layers", json::array({"F.Cu", "B.Cu"}))) layers.push_back(layer);
            replace_child(n, std::move(layers));
        }
        if (fresh || changed(object, "net"))
            replace_child(n, named_nets ? json::array({"net", string_at(object, "net")}) :
                                          json::array({"net", net_code(object, nets)}));
        if (fresh || changed(object, "type")) {
            const std::string type = string_at(object, "type", "through");
            remove_children(n, "type");
            for (std::size_t i = n.size(); i > 1; --i)
                if (n[i - 1].is_string() && (n[i - 1] == "blind" || n[i - 1] == "micro"))
                    n.erase(i - 1);
            if (type != "through") n.insert(n.begin() + 1, type == "micro" ? "micro" : "blind");
        }
    } else if (category == "zones") {
        if (fresh) n = json::array({"zone"});
        if (fresh || changed(object, "net")) {
            replace_child(n, named_nets ? json::array({"net", string_at(object, "net")}) :
                                          json::array({"net", net_code(object, nets)}));
            if (!named_nets || child(n, "net_name"))
                replace_child(n, json::array({"net_name", string_at(object, "net")}));
        }
        if (fresh || changed(object, "layer")) replace_child(n, json::array({"layer", string_at(object, "layer", "F.Cu")}));
        if (fresh) n.push_back(json::array({"hatch", "edge", 0.5}));
        if (fresh || changed(object, "clearance"))
            replace_child(n, json::array({"connect_pads", json::array({"clearance", number_at(object, "clearance", 0.3)})}));
        if (fresh || changed(object, "min_width")) replace_child(n, json::array({"min_thickness", number_at(object, "min_width", 0.25)}));
        if (fresh || changed(object, "fill") || changed(object, "thermal")) {
            const json thermal = object.value("thermal", json::object());
            json fill = json::array({"fill", "yes", json::array({"thermal_gap", number_at(thermal, "gap", 0.3)}),
                                     json::array({"thermal_bridge_width", number_at(thermal, "spoke_width", 0.3)})});
            if (string_at(object, "fill") == "hatch") fill.push_back(json::array({"mode", "hatch"}));
            replace_child(n, std::move(fill));
        }
        if (fresh || changed(object, "polygon")) {
            replace_child(n, json::array({"polygon", pts(object.value("polygon", json::array()))}));
            remove_children(n, "filled_polygon");
            remove_children(n, "filled_segments");
        }
        if (fresh || changed(object, "filled_polys")) {
            remove_children(n, "filled_polygon");
            remove_children(n, "filled_segments");
            if (object.contains("filled_polys") && object["filled_polys"].is_array())
                for (const auto& ring : object["filled_polys"])
                    if (ring.is_array() && ring.size() >= 3)
                        n.push_back(json::array({"filled_polygon",
                            json::array({"layer", string_at(object, "layer", "F.Cu")}), pts(ring)}));
        }
    } else if (category == "outline") {
        const std::string kind = string_at(object, "type", "line");
        if (fresh) n = json::array({kind == "arc" ? "gr_arc" : kind == "rect" ? "gr_rect" :
                                   kind == "circle" ? "gr_circle" : "gr_line"});
        if (kind == "circle") {
            if (fresh || changed(object, "center")) replace_child(n, xy("center", object.value("center", json::array())));
            if (fresh || changed(object, "center") || changed(object, "radius")) {
                json p = object.value("center", json::array({0, 0}));
                p[0] = coordinate(p, 0) + number_at(object, "radius", 1);
                replace_child(n, xy("end", p));
            }
        } else {
            const bool arc_geometry = kind == "arc" &&
                (fresh || changed(object, "center") || changed(object, "radius") ||
                 changed(object, "a0") || changed(object, "a1"));
            if (fresh || changed(object, "p1") || arc_geometry)
                replace_child(n, xy("start", object.contains("p1") ? object["p1"] : arc_endpoint(object, "a0")));
            if (kind == "arc" && (arc_geometry || changed(object, "mid")))
                replace_child(n, xy("mid", arc_mid(object)));
            if (fresh || changed(object, "p2") || arc_geometry)
                replace_child(n, xy("end", object.contains("p2") ? object["p2"] : arc_endpoint(object, "a1")));
        }
        if (fresh) {
            n.push_back(stroke(0.05));
            if (kind == "rect" || kind == "circle") n.push_back(json::array({"fill", "none"}));
            n.push_back(json::array({"layer", "Edge.Cuts"}));
        }
    } else if (category == "graphics") {
        if (fresh) throw std::runtime_error("Board graphic has no KiCad representation");
    }
    set_uuid(n, object);
    return n;
}

json default_layers(int count) {
    json layers = json::array({"layers"});
    layers.push_back(json::array({0, "F.Cu", "signal"}));
    for (int i = 1; i < count - 1; ++i)
        layers.push_back(json::array({2 * i + 2, "In" + std::to_string(i) + ".Cu", "signal"}));
    layers.push_back(json::array({2, "B.Cu", "signal"}));
    for (const auto& entry : std::vector<std::pair<int, std::string>>{
             {9, "F.Adhes"}, {11, "B.Adhes"}, {13, "F.Paste"}, {15, "B.Paste"},
             {5, "F.SilkS"}, {7, "B.SilkS"}, {1, "F.Mask"}, {3, "B.Mask"},
             {17, "Dwgs.User"}, {19, "Cmts.User"}, {21, "Eco1.User"}, {23, "Eco2.User"},
             {25, "Edge.Cuts"}, {27, "Margin"}, {31, "F.CrtYd"}, {29, "B.CrtYd"},
             {35, "F.Fab"}, {33, "B.Fab"}})
        layers.push_back(json::array({entry.first, entry.second, "user"}));
    return layers;
}

bool stackup_material_layer(const json& layer) {
    if (node_tag(layer) != "layer" || layer.size() < 2 || !layer[1].is_string()) return false;
    const std::string name = layer[1].get<std::string>();
    const json* type = child(layer, "type");
    const std::string kind = type && type->size() > 1 && (*type)[1].is_string() ?
                             (*type)[1].get<std::string>() : "";
    return (name.size() >= 3 && name.compare(name.size() - 3, 3, ".Cu") == 0) ||
           name.rfind("dielectric ", 0) == 0 || kind == "copper" || kind == "core" || kind == "prepreg";
}

void write_stackup(json& setup, const json& layers) {
    if (!layers.is_array()) return;
    json old = child(setup, "stackup") ? *child(setup, "stackup") : json::array({"stackup"});
    std::map<std::string, json> originals;
    for (const auto& layer : old)
        if (stackup_material_layer(layer)) originals[layer[1].get<std::string>()] = layer;
    json replacements = json::array();
    int dielectric = 0;
    for (const auto& layer : layers) {
        if (!layer.is_object()) continue;
        const std::string type = string_at(layer, "type", "core");
        const bool copper = type == "copper";
        const std::string name = copper ? string_at(layer, "id") : "dielectric " + std::to_string(++dielectric);
        json node = originals.count(name) ? originals[name] : json::array({"layer", name});
        replace_child(node, json::array({"type", type}));
        for (const auto& mapping : std::vector<std::pair<const char*, const char*>>{
                 {"thickness", "thickness"}, {"er", "epsilon_r"}, {"material", "material"},
                 {"loss_tangent", "loss_tangent"}}) {
            if (layer.contains(mapping.first) && !layer[mapping.first].is_null())
                replace_child(node, json::array({mapping.second, layer[mapping.first]}));
            else remove_children(node, mapping.second);
        }
        replacements.push_back(std::move(node));
    }
    json result = json::array({"stackup"});
    bool inserted = false;
    for (std::size_t i = 1; i < old.size(); ++i) {
        if (stackup_material_layer(old[i])) {
            if (!inserted) {
                for (auto& layer : replacements) result.push_back(std::move(layer));
                inserted = true;
            }
        } else result.push_back(old[i]);
    }
    if (!inserted)
        for (auto& layer : replacements) result.push_back(std::move(layer));
    replace_child(setup, std::move(result));
}

void update_copper_layers(json& node, int count) {
    json generated = default_layers(count);
    json result = json::array({"layers"});
    for (int i = 1; i <= count && static_cast<std::size_t>(i) < generated.size(); ++i) {
        json entry = generated[i];
        for (std::size_t j = 1; j < node.size(); ++j)
            if (node[j].is_array() && node[j].size() > 1 && node[j][1] == entry[1]) {
                entry = node[j];
                break;
            }
        result.push_back(std::move(entry));
    }
    for (std::size_t i = 1; i < node.size(); ++i) {
        const json& entry = node[i];
        if (entry.is_array() && entry.size() > 1 && entry[1].is_string()) {
            const std::string name = entry[1].get<std::string>();
            if (name.size() >= 3 && name.compare(name.size() - 3, 3, ".Cu") == 0) continue;
        }
        result.push_back(entry);
    }
    node = std::move(result);
}
json root_extra(const json& doc) {
    if (doc.contains("kicad") && doc["kicad"].is_object() &&
        doc["kicad"].contains("extra") && doc["kicad"]["extra"].is_array())
        return doc["kicad"]["extra"];
    return json::array();
}
void write_title(json& extra, const json& doc) {
    const json title = doc.value("title_block", json::object());
    bool have_paper = false, have_title = false;
    for (auto& node : extra) {
        if (node_tag(node) == "paper") {
            have_paper = true;
            if (node.size() > 1 && title.contains("paper")) node[1] = title["paper"];
        } else if (node_tag(node) == "title_block") {
            have_title = true;
            for (const char* key : {"title", "company", "rev", "date"})
                if (title.contains(key)) {
                    json* part = child(node, key);
                    if (part && part->size() > 1) (*part)[1] = title[key];
                    else if (title[key].is_string() && !title[key].get<std::string>().empty())
                        node.push_back(json::array({key, title[key]}));
                }
            for (int i = 1; i <= 4; ++i) {
                const std::string key = "comment" + std::to_string(i);
                if (!title.contains(key)) continue;
                bool found = false;
                for (auto& part : node)
                    if (node_tag(part) == "comment" && part.size() >= 3 && part[1] == i) {
                        part[2] = title[key]; found = true;
                    }
                if (!found && title[key].is_string() && !title[key].get<std::string>().empty())
                    node.push_back(json::array({"comment", i, title[key]}));
            }
        }
    }
    const bool imported = doc.contains("kicad") && doc["kicad"].contains("version") &&
                          doc["kicad"]["version"].is_number() && doc["kicad"]["version"] != 0;
    if (!have_paper && !imported) extra.push_back(json::array({"paper", string_at(title, "paper", "A4")}));
    if (!have_title && !imported) {
        json block = json::array({"title_block"});
        for (const char* key : {"title", "company", "rev", "date"})
            if (!string_at(title, key).empty()) block.push_back(json::array({key, title[key]}));
        if (block.size() > 1) extra.push_back(std::move(block));
    }
}
}  // namespace

void assign_uuids(json& doc) {
    if (!schema::is_schematic(doc) && !schema::is_board(doc))
        throw std::runtime_error("Expected aicad.sch/1 or aicad.pcb/1 document");
    if (!doc.contains("kicad") || !doc["kicad"].is_object()) doc["kicad"] = json::object();
    if (schema::is_schematic(doc)) {
        ensure_uuid(doc["kicad"]);
        for (const char* key : {"symbols", "power", "wires", "buses", "bus_entries", "junctions",
                                "no_connect", "labels", "texts", "graphics", "sheet_symbols"}) assign_list(doc, key);
        // Power ports created through native ops do not carry reference designators.
        // Allocate stable, distinct KiCad references before writing or saving.
        std::set<std::string> refs;
        for (const char* key : {"symbols", "power"})
            if (doc.contains(key) && doc[key].is_array())
                for (const auto& item : doc[key]) {
                    const std::string ref = string_at(item, "ref");
                    if (!ref.empty()) refs.insert(ref);
                }
        if (doc.contains("power") && doc["power"].is_array()) {
            int pwr = 1, flag = 1;
            for (auto& port : doc["power"]) {
                if (!port.is_object() || !string_at(port, "ref").empty()) continue;
                const bool is_flag = power_libid(port) == "power:PWR_FLAG";
                int& number = is_flag ? flag : pwr;
                const std::string prefix = is_flag ? "#FLG" : "#PWR";
                std::string ref;
                do {
                    ref = prefix + (number < 10 ? "0" : "") + std::to_string(number);
                    ++number;
                } while (refs.count(ref));
                port["ref"] = ref;
                refs.insert(ref);
            }
        }
        if (doc.contains("sheet_symbols") && doc["sheet_symbols"].is_array()) {
            for (auto& sheet : doc["sheet_symbols"]) {
                assign_list(sheet, "pins");
                if (doc.contains("sheets") && doc["sheets"].is_array())
                    for (auto& definition : doc["sheets"])
                        if (string_at(definition, "id") == string_at(sheet, "child"))
                            definition["uuid"] = sheet["uuid"];
            }
        }
        for (const char* key : {"wires", "buses"}) if (doc.contains(key) && doc[key].is_array()) {
            for (auto& wire : doc[key]) {
                if (!wire.contains("pts") || !wire["pts"].is_array() || wire["pts"].size() <= 2) continue;
                if (wire.contains("kicad_extra") && !changed(wire, "pts")) continue;
                if (!wire.contains("kicad_segments") || !wire["kicad_segments"].is_array())
                    wire["kicad_segments"] = json::array();
                while (wire["kicad_segments"].size() < wire["pts"].size() - 2)
                    wire["kicad_segments"].push_back(uuid4());
            }
        }
        for (const char* key : {"symbols", "power"}) if (doc.contains(key) && doc[key].is_array()) {
            for (auto& symbol : doc[key]) {
                const bool copy = symbol.value("kicad_copy", false);
                if (symbol.contains("kicad_extra") && !copy) continue;
                if (copy) {
                    const json original = raw_node(symbol);
                    for (const auto& pin : original) {
                        if (node_tag(pin) != "pin" || pin.size() < 2 || !pin[1].is_string()) continue;
                        if (!symbol.contains("kicad_pins") || !symbol["kicad_pins"].is_object())
                            symbol["kicad_pins"] = json::object();
                        const std::string number = pin[1].get<std::string>();
                        if (!symbol["kicad_pins"].contains(number)) symbol["kicad_pins"][number] = uuid4();
                    }
                }
                const std::string libid = std::string(key) == "power" ? power_libid(symbol) : string_at(symbol, "libid");
                json definition;
                if (doc.contains("lib_cache") && doc["lib_cache"].is_object() && doc["lib_cache"].contains(libid))
                    definition = doc["lib_cache"][libid];
                else if (const auto library = kicad_libs::symbol_sexpr(libid)) {
                    const auto colon = libid.find(':');
                    definition = symbol_def(*library, colon == std::string::npos ? "" : libid.substr(0, colon));
                }
                if (!definition.is_object() || !definition.contains("pins") || !definition["pins"].is_array()) continue;
                if (!symbol.contains("kicad_pins") || !symbol["kicad_pins"].is_object())
                    symbol["kicad_pins"] = json::object();
                for (const auto& pin : definition["pins"]) {
                    const int unit = pin.value("unit", 1);
                    if (unit != 0 && unit != symbol.value("unit", 1)) continue;
                    const std::string number = string_at(pin, "number");
                    if (!number.empty() && !symbol["kicad_pins"].contains(number)) symbol["kicad_pins"][number] = uuid4();
                }
            }
        }
    } else {
        for (const char* key : {"footprints", "tracks", "vias", "zones", "graphics"}) assign_list(doc, key);
        if (doc.contains("outline") && doc["outline"].is_object()) assign_list(doc["outline"], "shapes");
        if (doc.contains("footprints") && doc["footprints"].is_array()) for (auto& fp : doc["footprints"]) {
            assign_list(fp, "pads");
            assign_list(fp, "graphics");
        }
    }
}

std::string write_kicad_sch(const json& doc) {
    if (!schema::is_schematic(doc)) throw std::runtime_error("Expected aicad.sch/1 document");
    if (!doc.contains("kicad") || !doc["kicad"].is_object() || string_at(doc["kicad"], "uuid").empty())
        throw std::runtime_error("KiCad document has unassigned UUIDs; call assign_uuids first");
    const bool imported = doc["kicad"].contains("version") &&
                          doc["kicad"]["version"].is_number() && doc["kicad"]["version"] != 0;
    json root = json::array({"kicad_sch", json::array({"version", 20260306}),
        json::array({"generator", "chudware"}), json::array({"generator_version", "10.0"}),
        json::array({"uuid", doc["kicad"]["uuid"]})});
    json extra = root_extra(doc);
    write_title(extra, doc);
    bool sheet_instances = false;
    for (const auto& node : extra) if (node_tag(node) == "sheet_instances") sheet_instances = true;
    if (!sheet_instances && !imported)
        extra.push_back(json::array({"sheet_instances", json::array({"path", "/", json::array({"page", "1"})})}));
    for (const auto& node : extra) root.push_back(node);
    json libs = json::array({"lib_symbols"});
    std::map<std::string, json> definitions;
    if (doc["kicad"].contains("lib_symbols") && doc["kicad"]["lib_symbols"].is_object())
        for (const auto& [id, node] : doc["kicad"]["lib_symbols"].items())
            if (node.is_array()) definitions[id] = node;
    for (const char* key : {"symbols", "power"}) {
        if (!doc.contains(key) || !doc[key].is_array()) continue;
        for (const auto& object : doc[key]) {
            const std::string id = std::string(key) == "power" ? power_libid(object) : string_at(object, "libid");
            if (id.empty() || definitions.count(id)) continue;
            const json* cached = doc.contains("lib_cache") && doc["lib_cache"].is_object() &&
                                 doc["lib_cache"].contains(id) ? &doc["lib_cache"][id] : nullptr;
            if (cached && cached->is_object() && cached->value("_chudware_custom", false)) {
                definitions[id] = synthetic_symbol(id, *cached);
                continue;
            }
            auto library = kicad_libs::symbol_sexpr(id);
            if (library && library->is_array()) {
                json node = *library;
                if (node.size() > 1) node[1] = id;
                definitions[id] = std::move(node);
            } else {
                json def = cached ? *cached : json::object();
                if (std::string(key) == "power" && def.empty()) {
                    const std::string name = string_at(object, "net", "GND");
                    def = json{{"name", name}, {"prefix", "#PWR"}, {"value", name}, {"power", true},
                               {"pins", json::array({json{{"number", "1"}, {"name", name},
                                   {"type", "power_in"}, {"pos", json::array({0, 0})}, {"length", 0.0}}})}};
                }
                definitions[id] = synthetic_symbol(id, def);
            }
        }
    }
    for (const auto& [id, node] : definitions) libs.push_back(node);
    root.push_back(std::move(libs));
    for (const std::string category : {"symbols", "power", "wires", "buses", "bus_entries",
                                       "junctions", "no_connect", "labels", "texts", "graphics", "sheet_symbols"}) {
        if (!doc.contains(category) || !doc[category].is_array()) continue;
        for (const auto& object : doc[category]) {
            if (!object.is_object()) continue;
            if ((category == "wires" || category == "buses") && object.contains("pts") &&
                object["pts"].is_array() && object["pts"].size() > 2 &&
                (!object.contains("kicad_extra") || changed(object, "pts"))) {
                if (!object.contains("kicad_segments") || !object["kicad_segments"].is_array() ||
                    object["kicad_segments"].size() < object["pts"].size() - 2)
                    throw std::runtime_error("KiCad wire segments have unassigned UUIDs; call assign_uuids first");
                for (std::size_t i = 0; i + 1 < object["pts"].size(); ++i) {
                    json segment = object;
                    segment["pts"] = json::array({object["pts"][i], object["pts"][i + 1]});
                    if (i) {
                        segment["uuid"] = object["kicad_segments"][i - 1];
                        segment.erase("kicad_extra");
                        segment.erase("kicad_tag");
                        segment.erase("kicad_original");
                    }
                    root.push_back(schematic_object(segment, category, doc));
                }
            } else root.push_back(schematic_object(object, category, doc));
        }
    }
    return serialize(root);
}

std::string write_kicad_pcb(const json& doc) {
    if (!schema::is_board(doc)) throw std::runtime_error("Expected aicad.pcb/1 document");
    json root = json::array({"kicad_pcb", json::array({"version", 20260206}),
        json::array({"generator", "chudware"}), json::array({"generator_version", "10.0"})});
    json extra = root_extra(doc);
    const bool imported = doc.contains("kicad") && doc["kicad"].is_object() &&
                          doc["kicad"].contains("version") &&
                          doc["kicad"]["version"].is_number() && doc["kicad"]["version"] != 0;
    const bool named_nets = imported && doc["kicad"].value("named_nets", false);
    std::set<std::string> used;
    for (const char* key : {"tracks", "vias", "zones"}) if (doc.contains(key) && doc[key].is_array())
        for (const auto& item : doc[key]) if (!string_at(item, "net").empty()) used.insert(string_at(item, "net"));
    if (doc.contains("footprints") && doc["footprints"].is_array()) for (const auto& fp : doc["footprints"])
        if (fp.contains("pads") && fp["pads"].is_array()) for (const auto& pad : fp["pads"])
            if (!string_at(pad, "net").empty()) used.insert(string_at(pad, "net"));
    const std::set<std::string> item_nets = used;
    if (doc.contains("nets") && doc["nets"].is_array())
        for (const auto& net : doc["nets"]) if (!string_at(net, "name").empty()) used.insert(string_at(net, "name"));
    for (std::size_t i = extra.size(); i > 0; --i) {
        const json& node = extra[i - 1];
        if (node_tag(node) == "net" && node.size() >= 3 && node[2].is_string()) {
            const std::string name = node[2].get<std::string>();
            if (!name.empty() && !used.count(name)) extra.erase(i - 1);
        }
    }
    const json stack = doc.value("stackup", json::object());
    const json baseline = doc.contains("kicad") && doc["kicad"].is_object() &&
                          doc["kicad"].contains("stackup_original") ?
                          doc["kicad"]["stackup_original"] : json();
    const bool layer_stack_changed = !baseline.is_object() ||
        baseline.value("layers", json::array()) != stack.value("layers", json::array());
    const bool copper_count_changed = !baseline.is_object() ||
        baseline.value("copper_layers", 2) != stack.value("copper_layers", 2);
    bool has_general = false, has_layers = false, has_setup = false;
    NetCodes nets;
    int next_net = 1;
    for (auto& node : extra) {
        const std::string kind = node_tag(node);
        if (kind == "general") {
            has_general = true;
            if (json* th = child(node, "thickness"); th && th->size() > 1)
                (*th)[1] = number_at(stack, "thickness", 1.6);
        } else if (kind == "layers") {
            has_layers = true;
            if (copper_count_changed) update_copper_layers(node, stack.value("copper_layers", 2));
        } else if (kind == "setup") {
            has_setup = true;
            if (layer_stack_changed) write_stackup(node, stack.value("layers", json::array()));
        }
        else if (kind == "net" && node.size() >= 3 && node[1].is_number_integer() && node[2].is_string()) {
            const int code = node[1].get<int>();
            nets[node[2].get<std::string>()] = code;
            next_net = std::max(next_net, code + 1);
        }
    }
    if (!has_general) root.push_back(json::array({"general", json::array({"thickness", number_at(stack, "thickness", 1.6)})}));
    if (!has_layers) root.push_back(default_layers(stack.value("copper_layers", 2)));
    if (!has_setup) {
        json setup = json::array({"setup", json::array({"pad_to_mask_clearance", 0})});
        if (layer_stack_changed) write_stackup(setup, stack.value("layers", json::array()));
        root.push_back(std::move(setup));
    }
    for (const auto& node : extra) root.push_back(node);
    if (!nets.count("") && !imported) root.push_back(json::array({"net", 0, ""}));
    nets[""] = 0;
    for (const auto& name : used) if (!nets.count(name)) {
        nets[name] = next_net++;
        if (!named_nets || !item_nets.count(name))
            root.push_back(json::array({"net", nets[name], name}));
    }
    for (const std::string category : {"footprints", "tracks", "vias", "zones", "graphics"})
        if (doc.contains(category) && doc[category].is_array())
            for (const auto& object : doc[category])
                if (object.is_object()) root.push_back(board_object(object, category, nets, named_nets));
    if (doc.contains("outline") && doc["outline"].contains("shapes") && doc["outline"]["shapes"].is_array())
        for (const auto& shape : doc["outline"]["shapes"])
            if (shape.is_object()) root.push_back(board_object(shape, "outline", nets, named_nets));
    return serialize(root);
}
}  // namespace chudware::ecad::kicad
