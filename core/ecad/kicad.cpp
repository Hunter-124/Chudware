// KiCad importers — see kicad.h. Line-by-line port of:
//   engine/ecad/library/sexpr.py     (parse)
//   engine/ecad/library/kicad_sym.py (_parse_pin/_parse_graphic/_parse_symbol)
//   engine/ecad/library/kicad_mod.py (_parse_pad/_parse_graphic/_parse_model/
//                                     _parse_footprint)
//   engine/ecad/formats.py           (import_kicad_sch / import_kicad_pcb)
// Pure JSON; no OCCT.

#include "ecad/kicad.h"

#include <cctype>
#include <cmath>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <map>
#include <random>
#include <regex>
#include <set>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

#include "ecad/schema.h"

namespace chudware::ecad::kicad {

using nlohmann::json;

namespace {

// ---------------------------------------------------------------------------
// sexpr.py — tokenizer + recursive-descent parser
// ---------------------------------------------------------------------------

std::vector<std::string> tokenize(const std::string& text) {
    std::vector<std::string> out;
    std::size_t i = 0;
    const std::size_t n = text.size();
    while (i < n) {
        const char c = text[i];
        if (std::isspace(static_cast<unsigned char>(c))) { ++i; continue; }
        if (c == '(' || c == ')') { out.emplace_back(1, c); ++i; continue; }
        if (c == '"') {
            // quoted string: "(?:[^"\\]|\\.)*"
            std::string tok(1, '"');
            std::size_t j = i + 1;
            while (j < n) {
                const char d = text[j];
                tok += d;
                if (d == '\\' && j + 1 < n) { tok += text[j + 1]; j += 2; continue; }
                ++j;
                if (d == '"') break;
            }
            out.push_back(std::move(tok));
            i = j;
            continue;
        }
        // bare symbol / number: [^\s()"]+
        std::size_t j = i;
        while (j < n && !std::isspace(static_cast<unsigned char>(text[j])) && text[j] != '(' &&
               text[j] != ')' && text[j] != '"')
            ++j;
        out.push_back(text.substr(i, j - i));
        i = j;
    }
    return out;
}

// Python str.replace(a, b) — all occurrences, left to right.
std::string replace_all(std::string s, const std::string& a, const std::string& b) {
    if (a.empty()) return s;
    std::size_t pos = 0;
    while ((pos = s.find(a, pos)) != std::string::npos) {
        s.replace(pos, a.size(), b);
        pos += b.size();
    }
    return s;
}

bool parse_int64(const std::string& tok, long long& out) {
    // Python int(tok): optional sign + digits (no whitespace in tokens).
    std::size_t i = (tok.empty() || (tok[0] != '+' && tok[0] != '-')) ? 0 : 1;
    if (i >= tok.size()) return false;
    for (; i < tok.size(); ++i)
        if (!std::isdigit(static_cast<unsigned char>(tok[i]))) return false;
    out = std::strtoll(tok.c_str(), nullptr, 10);
    return true;
}

json parse_atom(const std::string& tok) {
    if (!tok.empty() && tok.front() == '"') {
        // decode escapes; slice off surrounding quotes (sexpr.py::_parse_atom)
        std::string inner = tok.size() >= 2 ? tok.substr(1, tok.size() - 2) : std::string();
        inner = replace_all(inner, "\\\"", "\"");
        inner = replace_all(inner, "\\\\", "\\");
        inner = replace_all(inner, "\\n", "\n");
        return inner;
    }
    long long iv = 0;
    if (parse_int64(tok, iv)) return iv;
    char* end = nullptr;
    const double dv = std::strtod(tok.c_str(), &end);
    if (end == tok.c_str() + tok.size() && !tok.empty()) return dv;
    return tok;  // bare symbol
}

json build(const std::vector<std::string>& toks, std::size_t& pos) {
    const std::string& tok = toks[pos];
    if (tok == "(") {
        ++pos;
        json arr = json::array();
        while (pos < toks.size() && toks[pos] != ")") arr.push_back(build(toks, pos));
        if (pos >= toks.size())
            throw std::runtime_error("Unexpected end of input: unmatched '('");
        ++pos;  // consume ')'
        return arr;
    }
    if (tok == ")")
        throw std::runtime_error("Unexpected ')' at token index " + std::to_string(pos));
    ++pos;
    return parse_atom(tok);
}

// ---------------------------------------------------------------------------
// kicad_sym/kicad_mod shared node helpers (_tag/_children/_find/_findall)
// ---------------------------------------------------------------------------

std::string tag(const json& node) {
    if (node.is_array() && !node.empty() && node[0].is_string())
        return node[0].get<std::string>();
    return "";
}

const json* find(const json& node, const std::string& t) {
    if (!node.is_array()) return nullptr;
    for (std::size_t i = 1; i < node.size(); ++i)
        if (node[i].is_array() && tag(node[i]) == t) return &node[i];
    return nullptr;
}

std::vector<const json*> findall(const json& node, const std::string& t) {
    std::vector<const json*> out;
    if (!node.is_array()) return out;
    for (std::size_t i = 1; i < node.size(); ++i)
        if (node[i].is_array() && tag(node[i]) == t) out.push_back(&node[i]);
    return out;
}

// Python str(atom).
std::string jstr(const json& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number()) return v.dump();
    return "";
}

// Python float(atom) — throws on a non-number like the oracle does.
double f64(const json& v) {
    if (v.is_number()) return v.get<double>();
    throw std::runtime_error("could not convert string to float: '" + jstr(v) + "'");
}

std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

double flip_y(double y) { return -y; }  // KiCad +y-down -> +y-up

// Python round(): round-half-to-even (erc.cpp::py_round twin).
double py_round(double v) {
    double fl = std::floor(v);
    double diff = v - fl;
    if (diff < 0.5) return fl;
    if (diff > 0.5) return fl + 1.0;
    return (std::fmod(fl, 2.0) == 0.0) ? fl : fl + 1.0;
}

// Python round(x, 6) as used by the importers (scaling form, see erc.cpp::snap_xy).
double round6(double x) { return py_round(x * 1e6) / 1e6 + 0.0; }

std::string uuid4() {
    static std::mt19937_64 rng(std::random_device{}());
    unsigned char b[16];
    for (unsigned char& byte : b) byte = static_cast<unsigned char>(rng() & 0xff);
    b[6] = static_cast<unsigned char>((b[6] & 0x0f) | 0x40);  // version 4
    b[8] = static_cast<unsigned char>((b[8] & 0x3f) | 0x80);  // variant 1
    char buf[37];
    std::snprintf(buf, sizeof(buf),
                  "%02x%02x%02x%02x-%02x%02x-%02x%02x-%02x%02x-%02x%02x%02x%02x%02x%02x",
                  b[0], b[1], b[2], b[3], b[4], b[5], b[6], b[7], b[8], b[9], b[10], b[11],
                  b[12], b[13], b[14], b[15]);
    return std::string(buf);
}

std::string read_text(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw std::runtime_error("No such file or directory: '" + path + "'");
    std::ostringstream ss;
    ss << f.rdbuf();
    return ss.str();
}

// pathlib.Path(source).stem
std::string path_stem(const std::string& path) {
    const std::size_t slash = path.find_last_of("/\\");
    const std::string base = slash == std::string::npos ? path : path.substr(slash + 1);
    const std::size_t dot = base.find_last_of('.');
    if (dot == std::string::npos || dot == 0) return base;
    return base.substr(0, dot);
}

bool raw_sexpr(const std::string& source) {
    // source.strip().startswith("(")
    const std::size_t i = source.find_first_not_of(" \t\r\n");
    return i != std::string::npos && source[i] == '(';
}

bool starts_with(const std::string& s, const std::string& p) { return s.rfind(p, 0) == 0; }
bool ends_with(const std::string& s, const std::string& p) {
    return s.size() >= p.size() && s.compare(s.size() - p.size(), p.size(), p) == 0;
}

// Python str.rstrip(chars)
std::string rstrip_chars(std::string s, const std::string& chars) {
    while (!s.empty() && chars.find(s.back()) != std::string::npos) s.pop_back();
    return s;
}

// ---------------------------------------------------------------------------
// kicad_sym.py — symbol library parsing
// ---------------------------------------------------------------------------

const std::map<std::string, std::string>& pin_type_map() {
    static const std::map<std::string, std::string> m{
        {"input", "input"},         {"output", "output"},
        {"bidirectional", "bidirectional"}, {"tri_state", "tri_state"},
        {"passive", "passive"},     {"power_in", "power_in"},
        {"power_input", "power_in"}, {"power_out", "power_out"},
        {"power_output", "power_out"}, {"open_collector", "open_collector"},
        {"open_emitter", "open_emitter"}, {"unspecified", "unspecified"},
        {"no_connect", "no_connect"}, {"free", "free"}};
    return m;
}

const std::map<std::string, std::string>& pin_shape_map() {
    static const std::map<std::string, std::string> m{
        {"line", "line"},           {"inverted", "inverted"},
        {"clock", "clock"},         {"inverted_clock", "inverted_clock"},
        {"input_low", "input_low"}, {"clock_low", "clock_low"},
        {"output_low", "output_low"}, {"edge_clock_high", "edge_clock_high"},
        {"non_logic", "non_logic"}, {"", "line"}};
    return m;
}

std::string map_or(const std::map<std::string, std::string>& m, const std::string& k,
                   const std::string& dflt) {
    const auto it = m.find(k);
    return it != m.end() ? it->second : dflt;
}

// kicad_sym.py::_unit_from_name — 'R_1_1' -> 1 (first part of rsplit("_", 2)).
int unit_from_name(const std::string& name) {
    const std::size_t last = name.rfind('_');
    if (last == std::string::npos) return 1;
    const std::size_t prev = name.rfind('_', last - 1);
    if (prev == std::string::npos) return 1;
    long long v = 0;
    if (parse_int64(name.substr(prev + 1, last - prev - 1), v)) return static_cast<int>(v);
    return 1;
}

// kicad_sym.py::_parse_pin — (pin <type> <shape> (at x y angle) (length l)
// (name "~") (number "1")) -> Pin dict.
json parse_pin(const json& node, int unit) {
    const std::string pin_type_raw = node.size() > 1 ? jstr(node[1]) : "passive";
    const std::string pin_shape_raw = node.size() > 2 ? jstr(node[2]) : "line";
    const std::string pin_type = map_or(pin_type_map(), lower(pin_type_raw), "passive");
    const std::string pin_shape = map_or(pin_shape_map(), lower(pin_shape_raw), "line");

    double x = 0.0, y = 0.0, angle = 0.0;
    if (const json* at = find(node, "at"); at && at->size() >= 3) {
        x = f64((*at)[1]);
        y = flip_y(f64((*at)[2]));
        if (at->size() > 3) angle = f64((*at)[3]);
    }
    double length = 2.54;
    if (const json* len = find(node, "length"); len && len->size() > 1)
        length = f64((*len)[1]);
    std::string pin_name = "~";
    if (const json* nm = find(node, "name"); nm && nm->size() > 1) pin_name = jstr((*nm)[1]);
    if (pin_name == "~") pin_name = "";
    std::string pin_number = "1";
    if (const json* num = find(node, "number"); num && num->size() > 1)
        pin_number = jstr((*num)[1]);

    return json{{"number", pin_number}, {"name", pin_name},
                {"pos", json::array({x, y})}, {"angle", angle},
                {"length", length},        {"type", pin_type},
                {"shape", pin_shape},      {"unit", unit}};
}

const std::set<std::string>& sym_graphic_tags() {
    static const std::set<std::string> t{"rectangle", "polyline", "circle", "arc", "text"};
    return t;
}

// kicad_sym.py::_parse_graphic — body graphic -> Graphic dict (best-effort).
json parse_sym_graphic(const json& node) {
    const std::string t = tag(node);
    if (t == "rectangle") {
        const json* start = find(node, "start");
        const json* end = find(node, "end");
        if (start && end && start->size() >= 3 && end->size() >= 3)
            return json{{"type", "rect"},
                        {"p1", json::array({f64((*start)[1]), flip_y(f64((*start)[2]))})},
                        {"p2", json::array({f64((*end)[1]), flip_y(f64((*end)[2]))})}};
    } else if (t == "polyline") {
        if (const json* pts_node = find(node, "pts")) {
            json pts = json::array();
            for (const json* xy : findall(*pts_node, "xy"))
                if (xy->size() >= 3)
                    pts.push_back(json::array({f64((*xy)[1]), flip_y(f64((*xy)[2]))}));
            if (!pts.empty()) return json{{"type", "polyline"}, {"pts", pts}};
        }
    } else if (t == "circle") {
        const json* center = find(node, "center");
        const json* radius = find(node, "radius");
        if (center && radius && center->size() >= 3 && radius->size() >= 2)
            return json{
                {"type", "circle"},
                {"center", json::array({f64((*center)[1]), flip_y(f64((*center)[2]))})},
                {"radius", f64((*radius)[1])}};
    } else if (t == "arc") {
        const json* start = find(node, "start");
        const json* mid = find(node, "mid");
        const json* end = find(node, "end");
        if (start && end && start->size() >= 3 && end->size() >= 3) {
            json g{{"type", "arc"},
                   {"p1", json::array({f64((*start)[1]), flip_y(f64((*start)[2]))})},
                   {"p2", json::array({f64((*end)[1]), flip_y(f64((*end)[2]))})}};
            if (mid && mid->size() >= 3)
                g["mid"] = json::array({f64((*mid)[1]), flip_y(f64((*mid)[2]))});
            return g;
        }
    } else if (t == "text") {
        const std::string text_val = node.size() > 1 ? jstr(node[1]) : "";
        if (const json* at = find(node, "at"); at && at->size() >= 3)
            return json{{"type", "text"},
                        {"text", text_val},
                        {"pos", json::array({f64((*at)[1]), flip_y(f64((*at)[2]))})}};
    }
    return json(nullptr);
}

// kicad_sym.py::_parse_symbol — (symbol "Name" ...) -> SymbolDef (or null).
json parse_symbol(const json& sym_node, const std::string& lib_name) {
    if (tag(sym_node) != "symbol" || sym_node.size() < 2) return json(nullptr);
    const std::string raw_name = jstr(sym_node[1]);

    std::map<std::string, std::string> props;
    for (const json* prop : findall(sym_node, "property"))
        if (prop->size() >= 3) props[jstr((*prop)[1])] = jstr((*prop)[2]);

    const auto ref_it = props.find("Reference");
    const std::string reference_prefix =
        ref_it != props.end() ? ref_it->second
                              : (raw_name.empty() ? "U" : raw_name.substr(0, 1));
    const auto get_prop = [&](const char* k, const std::string& dflt) {
        const auto it = props.find(k);
        return it != props.end() ? it->second : dflt;
    };
    const std::string value = get_prop("Value", raw_name);
    const std::string footprint = get_prop("Footprint", "");
    const std::string description = get_prop("Description", "");
    const std::string keywords = get_prop("Keywords", "");

    static const std::set<std::string> kReserved{"Reference", "Value",    "Footprint",
                                                 "Datasheet", "Description", "Keywords",
                                                 "ki_keywords"};
    json fields = json::object();
    for (const auto& [k, v] : props)
        if (!kReserved.count(k)) fields[k] = v;

    json pins = json::array();
    json graphics = json::array();
    std::set<int> units_seen;

    const std::function<void(const json&, int)> harvest = [&](const json& node, int unit) {
        if (!node.is_array()) return;
        for (std::size_t i = 1; i < node.size(); ++i) {
            const json& child = node[i];
            if (!child.is_array()) continue;
            const std::string ct = tag(child);
            if (ct == "pin") {
                pins.push_back(parse_pin(child, unit));
                units_seen.insert(unit);
            } else if (sym_graphic_tags().count(ct)) {
                json g = parse_sym_graphic(child);
                if (!g.is_null()) graphics.push_back(g);
            } else if (ct == "symbol") {
                const std::string sub_name = child.size() > 1 ? jstr(child[1]) : "";
                harvest(child, unit_from_name(sub_name));
            }
        }
    };
    harvest(sym_node, 1);

    const int units_count = units_seen.empty() ? 1 : *units_seen.rbegin();
    const std::string libid = lib_name + ":" + raw_name;

    return json{{"libid", libid},
                {"name", raw_name},
                {"description", description},
                {"keywords", keywords},
                {"reference_prefix", reference_prefix},
                {"value", value},
                {"footprint_filters", footprint.empty() ? json::array() : json::array({footprint})},
                {"pins", pins},
                {"graphics", graphics},
                {"units_count", units_count},
                {"power", reference_prefix == "#PWR"},
                {"fields", fields}};
}

// ---------------------------------------------------------------------------
// kicad_mod.py — footprint parsing
// ---------------------------------------------------------------------------

const std::map<std::string, std::string>& pad_type_map() {
    static const std::map<std::string, std::string> m{
        {"smd", "smd"},     {"thru_hole", "tht"},   {"np_thru_hole", "np_tht"},
        {"connect", "connector"}, {"connector", "connector"}};
    return m;
}

const std::map<std::string, std::string>& pad_shape_map() {
    static const std::map<std::string, std::string> m{
        {"rect", "rect"},   {"roundrect", "roundrect"}, {"circle", "circle"},
        {"oval", "oval"},   {"trapezoid", "trapezoid"}, {"custom", "custom"},
        {"square", "rect"}};
    return m;
}

// kicad_mod.py::_at_xy — (x, y_flipped, angle) from an (at x y [angle]) node.
void at_xy(const json& node, double& x, double& y, double& angle) {
    x = y = angle = 0.0;
    if (const json* at = find(node, "at"); at && at->size() >= 3) {
        x = f64((*at)[1]);
        y = flip_y(f64((*at)[2]));
        if (at->size() > 3) angle = f64((*at)[3]);
    }
}

// kicad_mod.py::_parse_pad — (pad "1" smd rect (at ...) (size w h) ...) -> Pad.
json parse_pad(const json& node, int index) {
    const std::string number = node.size() > 1 ? jstr(node[1]) : std::to_string(index);
    const std::string pad_type_raw = node.size() > 2 ? lower(jstr(node[2])) : "smd";
    const std::string pad_shape_raw = node.size() > 3 ? lower(jstr(node[3])) : "rect";
    const std::string pad_type = map_or(pad_type_map(), pad_type_raw, "smd");
    const std::string pad_shape = map_or(pad_shape_map(), pad_shape_raw, "rect");

    double x, y, angle;
    at_xy(node, x, y, angle);

    double w = 1.0, h = 1.0;
    if (const json* size = find(node, "size"); size && size->size() >= 3) {
        w = f64((*size)[1]);
        h = f64((*size)[2]);
    }

    json layers = json::array();
    if (const json* layers_node = find(node, "layers"))
        for (std::size_t i = 1; i < layers_node->size(); ++i)
            if ((*layers_node)[i].is_string()) layers.push_back((*layers_node)[i]);

    double roundrect_ratio = 0.0;
    if (const json* rr = find(node, "roundrect_rratio"); rr && rr->size() > 1)
        roundrect_ratio = f64((*rr)[1]);

    json drill = json(nullptr);
    if (const json* drill_node = find(node, "drill")) {
        // (drill [oval] diameter [width] (offset x y))
        if (drill_node->size() > 1) {
            const json& first = (*drill_node)[1];
            std::string d_shape;
            double diameter;
            if (first.is_string() && first.get<std::string>() == "oval") {
                d_shape = "oval";
                diameter = drill_node->size() > 2 ? f64((*drill_node)[2]) : 0.6;
            } else {
                d_shape = "round";
                try {
                    diameter = f64(first);
                } catch (const std::exception&) {
                    diameter = 0.6;
                }
            }
            json offset = json::array({0.0, 0.0});
            if (const json* off = find(*drill_node, "offset"); off && off->size() >= 3)
                offset = json::array({f64((*off)[1]), flip_y(f64((*off)[2]))});
            drill = json{{"diameter", diameter}, {"shape", d_shape}, {"offset", offset}};
        }
    }

    return json{{"id", "p" + std::to_string(index)},
                {"number", number},
                {"pos", json::array({x, y})},
                {"angle", angle},
                {"shape", pad_shape},
                {"size", json::array({w, h})},
                {"roundrect_ratio", roundrect_ratio},
                {"type", pad_type},
                {"layers", layers},
                {"drill", drill},
                {"net", nullptr},
                {"pin", number}};
}

const std::set<std::string>& fp_graphic_tags() {
    static const std::set<std::string> t{"fp_line", "fp_circle", "fp_arc", "fp_poly", "fp_text"};
    return t;
}

// kicad_mod.py::_parse_graphic — footprint graphic (best-effort).
json parse_fp_graphic(const json& node) {
    const std::string t = tag(node);
    std::string layer = "F.SilkS";
    if (const json* layer_node = find(node, "layer"); layer_node && layer_node->size() > 1)
        layer = jstr((*layer_node)[1]);

    if (t == "fp_line") {
        const json* start = find(node, "start");
        const json* end = find(node, "end");
        if (start && end && start->size() >= 3 && end->size() >= 3)
            return json{{"type", "line"},
                        {"p1", json::array({f64((*start)[1]), flip_y(f64((*start)[2]))})},
                        {"p2", json::array({f64((*end)[1]), flip_y(f64((*end)[2]))})},
                        {"layer", layer}};
    } else if (t == "fp_circle") {
        const json* center = find(node, "center");
        const json* end = find(node, "end");
        if (center && end && center->size() >= 3 && end->size() >= 3) {
            const double cx = f64((*center)[1]), cy = f64((*center)[2]);
            const double ex = f64((*end)[1]), ey = f64((*end)[2]);
            const double radius = std::sqrt((ex - cx) * (ex - cx) + (ey - cy) * (ey - cy));
            return json{{"type", "circle"},
                        {"center", json::array({cx, flip_y(cy)})},
                        {"radius", radius},
                        {"layer", layer}};
        }
    } else if (t == "fp_arc") {
        const json* start = find(node, "start");
        const json* mid = find(node, "mid");
        const json* end = find(node, "end");
        if (start && end && start->size() >= 3 && end->size() >= 3) {
            json g{{"type", "arc"},
                   {"p1", json::array({f64((*start)[1]), flip_y(f64((*start)[2]))})},
                   {"p2", json::array({f64((*end)[1]), flip_y(f64((*end)[2]))})},
                   {"layer", layer}};
            if (mid && mid->size() >= 3)
                g["mid"] = json::array({f64((*mid)[1]), flip_y(f64((*mid)[2]))});
            return g;
        }
    } else if (t == "fp_text") {
        const std::string text_val = node.size() > 2 ? jstr(node[2]) : "";
        if (const json* at = find(node, "at"); at && at->size() >= 3)
            return json{{"type", "text"},
                        {"text", text_val},
                        {"pos", json::array({f64((*at)[1]), flip_y(f64((*at)[2]))})},
                        {"layer", layer}};
    } else if (t == "fp_poly") {
        if (const json* pts_node = find(node, "pts")) {
            json pts = json::array();
            for (const json* xy : findall(*pts_node, "xy"))
                if (xy->size() >= 3)
                    pts.push_back(json::array({f64((*xy)[1]), flip_y(f64((*xy)[2]))}));
            if (!pts.empty()) return json{{"type", "polygon"}, {"pts", pts}, {"layer", layer}};
        }
    }
    return json(nullptr);
}

// kicad_mod.py::_parse_model — (model path (offset (xyz ...)) (scale ...) (rotate ...)).
json parse_model(const json& node) {
    if (tag(node) != "model") return json(nullptr);
    const std::string path = node.size() > 1 ? jstr(node[1]) : "";

    const auto xyz_of = [](const json* n) {
        json out = json::array({0.0, 0.0, 0.0});
        if (n) {
            if (const json* xyz = find(*n, "xyz"); xyz && xyz->size() >= 4)
                out = json::array({f64((*xyz)[1]), f64((*xyz)[2]), f64((*xyz)[3])});
        }
        return out;
    };
    const json* offset_node = find(node, "offset");
    const json* scale_node = find(node, "scale");
    const json* rotate_node = find(node, "rotate");

    return json{{"path", path},
                {"offset", xyz_of(offset_node)},
                {"scale", scale_node ? xyz_of(scale_node) : json::array({1.0, 1.0, 1.0})},
                {"rotate", xyz_of(rotate_node)}};
}

// kicad_mod.py::_parse_footprint — (footprint ...) root node -> footprint dict.
json parse_footprint(const json& tree, const std::string& fpid_override = "") {
    if (tag(tree) != "footprint")
        throw std::runtime_error("Expected footprint root, got '" + tag(tree) + "'");
    const std::string fp_name = tree.size() > 1 ? jstr(tree[1]) : "Unknown";
    const std::string fpid = fpid_override.empty() ? fp_name : fpid_override;

    std::string attr = "smd";
    if (const json* attr_node = find(tree, "attr"))
        for (std::size_t i = 1; i < attr_node->size(); ++i) {
            const std::string v = lower(jstr((*attr_node)[i]));
            if (v == "smd" || v == "virtual") { attr = v; break; }
            if (v == "through_hole" || v == "tht") { attr = "tht"; break; }
        }

    json pads = json::array();
    json graphics = json::array();
    json model_3d = json(nullptr);

    for (std::size_t i = 1; i < tree.size(); ++i) {
        const json& child = tree[i];
        if (!child.is_array()) continue;
        const std::string ct = tag(child);
        if (ct == "pad") {
            pads.push_back(parse_pad(child, static_cast<int>(pads.size()) + 1));
        } else if (fp_graphic_tags().count(ct)) {
            json g = parse_fp_graphic(child);
            if (!g.is_null()) graphics.push_back(g);
        } else if (ct == "model") {
            json m = parse_model(child);
            if (!m.is_null()) model_3d = m;
        }
    }

    return json{{"fpid", fpid},
                {"pads", pads},
                {"graphics", graphics},
                {"model_3d", model_3d.is_null() ? json(nullptr) : model_3d["path"]},
                {"model_3d_transform", model_3d},
                {"attr", attr},
                {"description", ""}};
}

// ---------------------------------------------------------------------------
// formats.py — _map_layer (identity today: every mapped value equals its key,
// In*.Cu and unknown names pass through; kept for fidelity with the oracle)
// ---------------------------------------------------------------------------

std::string map_layer(const std::string& raw) { return raw; }

// formats.py::import_kicad_sch — symbol-instance property set
json sch_inst_props(const json& child) {
    json props = json::object();
    for (const json* prop : findall(child, "property"))
        if (prop->size() >= 3) props[jstr((*prop)[1])] = jstr((*prop)[2]);
    return props;
}

}  // namespace

// ---------------------------------------------------------------------------
// sexpr.py::parse (public)
// ---------------------------------------------------------------------------

json sexpr_parse(const std::string& text) {
    const std::vector<std::string> toks = tokenize(text);
    if (toks.empty()) throw std::runtime_error("Empty input");
    std::size_t pos = 0;
    json tree = build(toks, pos);
    if (pos < toks.size())
        throw std::runtime_error("Trailing content at token " + std::to_string(pos) + ": '" +
                                 toks[pos] + "'");
    return tree;
}

// ---------------------------------------------------------------------------
// formats.py::import_kicad_sch
// ---------------------------------------------------------------------------

json import_kicad_sch(const std::string& source) {
    std::string text, src_name;
    if (raw_sexpr(source)) {
        text = source;
        src_name = "inline";
    } else {
        text = read_text(source);
        src_name = path_stem(source);
    }

    const json tree = sexpr_parse(text);
    if (tag(tree) != "kicad_sch")
        throw std::runtime_error("Expected kicad_sch root, got '" + tag(tree) + "'");

    json doc = schema::new_schematic(src_name);
    doc["id"] = uuid4();  // Python: str(uuid.uuid4())

    json lib_cache = json::object();
    int sym_counter = 0, wire_counter = 0, junction_counter = 0, label_counter = 0,
        power_counter = 0, nc_counter = 0;

    // ---- 1. embedded lib_symbols block -> lib_cache ----
    static const std::regex kSubSymbolRe("_\\d+_\\d+$");
    if (const json* lib_syms = find(tree, "lib_symbols")) {
        for (std::size_t i = 1; i < lib_syms->size(); ++i) {
            const json& child = (*lib_syms)[i];
            if (!child.is_array() || tag(child) != "symbol") continue;
            const std::string sym_name_raw = child.size() > 1 ? jstr(child[1]) : "";
            if (std::regex_search(sym_name_raw, kSubSymbolRe))
                continue;  // skip sub-symbol entries
            const std::size_t colon = sym_name_raw.find(':');
            const std::string lib_nm =
                colon != std::string::npos ? sym_name_raw.substr(0, colon) : "Device";
            const std::string raw_nm =
                colon != std::string::npos ? sym_name_raw.substr(colon + 1) : sym_name_raw;
            json patched = child;
            patched[1] = raw_nm;
            const json sdef = parse_symbol(patched, lib_nm);
            if (!sdef.is_null()) lib_cache[sdef["libid"].get<std::string>()] = sdef;
        }
    }
    doc["lib_cache"] = lib_cache;

    // ---- 2. placed symbol instances (flat = root sheet) ----
    const std::string sheet_id = "sh1";
    static const std::set<std::string> kReservedProps{
        "Reference", "Value",    "Footprint",     "Datasheet", "Description",
        "Keywords",  "ki_keywords", "ki_fp_filters", "ki_description"};

    for (std::size_t i = 1; i < tree.size(); ++i) {
        const json& child = tree[i];
        if (!child.is_array()) continue;
        const std::string ct = tag(child);

        if (ct == "symbol") {
            ++sym_counter;
            std::string lib_id = "Device:Unknown";
            if (const json* li = find(child, "lib_id"); li && li->size() > 1)
                lib_id = jstr((*li)[1]);

            double x = 0.0, y = 0.0, angle = 0.0;
            at_xy(child, x, y, angle);

            std::string mirror = "none";
            if (const json* mv = find(child, "mirror"); mv && mv->size() > 1) {
                const std::string v = lower(jstr((*mv)[1]));
                if (v == "x") mirror = "x";
                else if (v == "y") mirror = "y";
            }

            long long unit = 1;
            if (const json* un = find(child, "unit"); un && un->size() > 1)
                unit = static_cast<long long>(f64((*un)[1]));

            const json props = sch_inst_props(child);
            const auto get_prop = [&](const char* k, const char* dflt) {
                return props.contains(k) ? props[k].get<std::string>() : std::string(dflt);
            };
            const std::string ref = get_prop("Reference", "U?");
            const std::string value = get_prop("Value", "");
            const std::string footprint = get_prop("Footprint", "");

            json fields = json::object();
            for (const auto& [k, v] : props.items())
                if (!kReservedProps.count(k)) fields[k] = v;

            // Stub definition when the lib_id is missing from lib_cache (best-effort).
            if (!lib_cache.contains(lib_id)) {
                const std::string short_nm =
                    lib_id.find(':') != std::string::npos
                        ? lib_id.substr(lib_id.find(':') + 1)
                        : lib_id;
                const std::string stripped = rstrip_chars(ref, "0123456789?");
                lib_cache[lib_id] = json{
                    {"libid", lib_id},
                    {"name", short_nm},
                    {"description", ""},
                    {"keywords", ""},
                    {"reference_prefix", stripped.empty() ? "U" : stripped},
                    {"value", value},
                    {"footprint_filters",
                     footprint.empty() ? json::array() : json::array({footprint})},
                    {"pins", json::array()},
                    {"graphics", json::array()},
                    {"units_count", 1},
                    {"power", starts_with(lib_id, "power:") || starts_with(ref, "#PWR")},
                    {"fields", json::object()}};
            }

            const json pos = json::array({round6(x), round6(y)});
            // Power port detection: lib_id starts with "power:" or ref starts with "#PWR".
            if (starts_with(lib_id, "power:") || starts_with(ref, "#PWR")) {
                std::string net_name = lib_id.find(':') != std::string::npos
                                           ? lib_id.substr(lib_id.find(':') + 1)
                                           : lib_id;
                if (net_name.empty()) net_name = !value.empty() ? value : ref;
                ++power_counter;
                doc["power"].push_back(json{{"id", "pw" + std::to_string(power_counter)},
                                            {"sheet", sheet_id},
                                            {"pos", pos},
                                            {"net", net_name},
                                            {"symbol", net_name}});
            } else {
                doc["symbols"].push_back(
                    json{{"id", "s" + std::to_string(sym_counter)},
                         {"libid", lib_id},       {"sheet", sheet_id},
                         {"ref", ref},            {"value", value},
                         {"footprint", footprint}, {"pos", pos},
                         {"angle", angle},        {"mirror", mirror},
                         {"unit", unit},          {"fields", fields},
                         {"in_bom", true},        {"on_board", true},
                         {"dnp", false}});
            }
        } else if (ct == "wire" || ct == "bus") {
            json pts = json::array();
            if (const json* pts_node = find(child, "pts"))
                for (const json* xy : findall(*pts_node, "xy"))
                    if (xy->size() >= 3)
                        pts.push_back(
                            json::array({round6(f64((*xy)[1])), round6(flip_y(f64((*xy)[2])))}));
            if (pts.size() >= 2) {
                if (ct == "wire") {
                    ++wire_counter;
                    doc["wires"].push_back(json{{"id", "w" + std::to_string(wire_counter)},
                                                {"sheet", sheet_id},
                                                {"pts", pts},
                                                {"bus", false}});
                } else {
                    doc["buses"].push_back(
                        json{{"id", "bus" + std::to_string(doc["buses"].size() + 1)},
                             {"sheet", sheet_id},
                             {"pts", pts},
                             {"members", json::array()}});
                }
            }
        } else if (ct == "junction") {
            if (const json* at = find(child, "at"); at && at->size() >= 3) {
                ++junction_counter;
                doc["junctions"].push_back(
                    json{{"id", "j" + std::to_string(junction_counter)},
                         {"sheet", sheet_id},
                         {"pos", json::array({round6(f64((*at)[1])),
                                              round6(flip_y(f64((*at)[2])))})}});
            }
        } else if (ct == "label" || ct == "global_label" || ct == "hierarchical_label") {
            const std::string text_val =
                child.size() > 1 && !child[1].is_array() ? jstr(child[1]) : "";
            double lx = 0.0, ly = 0.0, langle = 0.0;
            if (const json* at = find(child, "at"); at && at->size() >= 3) {
                lx = f64((*at)[1]);
                ly = flip_y(f64((*at)[2]));
                if (at->size() > 3) langle = f64((*at)[3]);
            }
            if (!text_val.empty()) {
                ++label_counter;
                const std::string scope = ct == "label"      ? "local"
                                          : ct == "global_label" ? "global"
                                                                 : "hier";
                doc["labels"].push_back(json{{"id", "l" + std::to_string(label_counter)},
                                             {"sheet", sheet_id},
                                             {"pos", json::array({round6(lx), round6(ly)})},
                                             {"text", text_val},
                                             {"scope", scope},
                                             {"angle", langle}});
            }
        } else if (ct == "no_connect") {
            if (const json* at = find(child, "at"); at && at->size() >= 3) {
                ++nc_counter;
                doc["no_connect"].push_back(
                    json{{"id", "nc" + std::to_string(nc_counter)},
                         {"sheet", sheet_id},
                         {"pos", json::array({round6(f64((*at)[1])),
                                              round6(flip_y(f64((*at)[2])))})}});
            }
        }
        // Unknown nodes: skip silently (tolerant per spec).
    }

    doc["lib_cache"] = lib_cache;
    return doc;
}

// ---------------------------------------------------------------------------
// formats.py::import_kicad_pcb
// ---------------------------------------------------------------------------

json import_kicad_pcb(const std::string& source) {
    std::string text, src_name;
    if (raw_sexpr(source)) {
        text = source;
        src_name = "inline";
    } else {
        text = read_text(source);
        src_name = path_stem(source);
    }

    const json tree = sexpr_parse(text);
    if (tag(tree) != "kicad_pcb")
        throw std::runtime_error("Expected kicad_pcb root, got '" + tag(tree) + "'");

    json doc = schema::new_board(src_name);
    doc["id"] = uuid4();  // Python: str(uuid.uuid4())

    // ---- 1. board thickness ----
    if (const json* general = find(tree, "general"))
        if (const json* th = find(*general, "thickness"); th && th->size() > 1)
            doc["stackup"]["thickness"] = f64((*th)[1]);

    // ---- 2. nets ----
    std::map<long long, std::string> net_map{{0, ""}};  // net 0 = unconnected
    json net_registry = json::array();
    std::set<std::string> seen_net_names;
    for (std::size_t i = 1; i < tree.size(); ++i) {
        const json& child = tree[i];
        if (!child.is_array() || tag(child) != "net" || child.size() < 3) continue;
        const long long net_num =
            child[1].is_number() ? static_cast<long long>(child[1].get<double>()) : 0;
        const std::string net_name = jstr(child[2]);
        net_map[net_num] = net_name;
        if (!net_name.empty() && !seen_net_names.count(net_name)) {
            seen_net_names.insert(net_name);
            net_registry.push_back(json{{"name", net_name}, {"class", "default"}});
        }
    }
    // GND is always present.
    if (!seen_net_names.count("GND")) {
        net_registry.insert(net_registry.begin(), json{{"name", "GND"}, {"class", "default"}});
        seen_net_names.insert("GND");
    }
    doc["nets"] = net_registry;

    // formats.py::_net_name — resolve a net number/name to a registered name (or null).
    const auto net_name_of = [&](const json& v) -> json {
        if (v.is_null()) return json(nullptr);
        std::string n;
        if (v.is_number()) {
            const auto it = net_map.find(static_cast<long long>(v.get<double>()));
            n = it != net_map.end() ? it->second : "";
        } else {
            n = jstr(v);
        }
        return (!n.empty() && seen_net_names.count(n)) ? json(n) : json(nullptr);
    };

    // ---- 3. copper layer count ----
    std::vector<std::string> copper_layers;
    if (const json* layers_node = find(tree, "layers")) {
        for (std::size_t i = 1; i < layers_node->size(); ++i) {
            const json& entry = (*layers_node)[i];
            if (!entry.is_array() || entry.size() < 3) continue;
            const std::string lname = jstr(entry[1]);
            const std::string ltype = entry.size() > 2 ? lower(jstr(entry[2])) : "";
            if (ltype == "signal" || lname == "F.Cu" || lname == "B.Cu" ||
                (starts_with(lname, "In") && ends_with(lname, ".Cu")))
                copper_layers.push_back(lname);
        }
    }
    int n_cu = copper_layers.empty() ? 2 : static_cast<int>(copper_layers.size());
    if (n_cu < 2) n_cu = 2;
    if (n_cu > 1 && n_cu % 2 == 1) n_cu += 1;  // even layer counts (>= 2)
    doc["stackup"]["copper_layers"] = n_cu;

    const std::vector<std::string> cu_ids = schema::copper_layer_ids(n_cu);
    const std::set<std::string> valid_cu(cu_ids.begin(), cu_ids.end());

    int fp_counter = 0, track_counter = 0, via_counter = 0, zone_counter = 0;
    json outline_shapes = json::array();

    for (std::size_t i = 1; i < tree.size(); ++i) {
        const json& child = tree[i];
        if (!child.is_array()) continue;
        const std::string ct = tag(child);

        if (ct == "footprint") {
            ++fp_counter;
            const json fp_def = parse_footprint(child);

            double fx = 0.0, fy = 0.0, fangle = 0.0;
            at_xy(child, fx, fy, fangle);

            std::string fp_layer = "F.Cu";
            if (const json* ln = find(child, "layer"); ln && ln->size() > 1)
                fp_layer = jstr((*ln)[1]);
            const std::string side = starts_with(fp_layer, "B") ? "bottom" : "top";

            // Properties (ref/value); fp_text fallback for older KiCad.
            std::map<std::string, std::string> fp_props;
            for (const json* prop : findall(child, "property"))
                if (prop->size() >= 3) fp_props[jstr((*prop)[1])] = jstr((*prop)[2]);
            for (const json* fpt : findall(child, "fp_text"))
                if (fpt->size() >= 3) {
                    const std::string label = lower(jstr((*fpt)[1]));
                    const std::string val = jstr((*fpt)[2]);
                    if (label == "reference") fp_props.emplace("Reference", val);
                    else if (label == "value") fp_props.emplace("Value", val);
                }
            const auto ref_it = fp_props.find("Reference");
            const std::string fp_ref =
                ref_it != fp_props.end() ? ref_it->second : "U" + std::to_string(fp_counter);
            const auto val_it = fp_props.find("Value");
            const std::string fp_value = val_it != fp_props.end() ? val_it->second : "";

            // Re-parse pads inline to bind (net N "name") assignments.
            json rebuilt_pads = json::array();
            int pad_idx = 0;
            for (std::size_t j = 1; j < child.size(); ++j) {
                const json& fp_child = child[j];
                if (!fp_child.is_array() || tag(fp_child) != "pad") continue;
                ++pad_idx;
                json pad = parse_pad(fp_child, pad_idx);

                json pad_net = json(nullptr);
                if (const json* net_node = find(fp_child, "net"); net_node && net_node->size() >= 3) {
                    const std::string name_str = jstr((*net_node)[2]);
                    if (!name_str.empty() && seen_net_names.count(name_str))
                        pad_net = name_str;
                    else if ((*net_node)[1].is_number())
                        pad_net = net_name_of((*net_node)[1]);
                }
                pad["net"] = pad_net;

                json remapped = json::array();
                for (const json& lyr : pad["layers"])
                    remapped.push_back(map_layer(lyr.get<std::string>()));
                pad["layers"] = remapped;

                rebuilt_pads.push_back(std::move(pad));
            }

            doc["footprints"].push_back(
                json{{"id", "fp" + std::to_string(fp_counter)},
                     {"fpid", fp_def.value("fpid", std::string("Unknown"))},
                     {"ref", fp_ref},
                     {"value", fp_value},
                     {"pos", json::array({round6(fx), round6(fy)})},
                     {"angle", fangle},
                     {"side", side},
                     {"pads", rebuilt_pads},
                     {"graphics", fp_def.value("graphics", json::array())},
                     {"model_3d", fp_def["model_3d"]},
                     {"attr", fp_def.value("attr", std::string("smd"))},
                     {"linked_symbol", nullptr}});
        } else if (ct == "segment") {
            const json* start = find(child, "start");
            const json* end = find(child, "end");
            if (start && end && start->size() >= 3 && end->size() >= 3) {
                std::string layer = "F.Cu";
                if (const json* ln = find(child, "layer"); ln && ln->size() > 1)
                    layer = map_layer(jstr((*ln)[1]));
                if (!valid_cu.count(layer)) layer = "F.Cu";

                json net_num = json(0);
                if (const json* nn = find(child, "net"); nn && nn->size() > 1) net_num = (*nn)[1];

                double width = 0.25;
                if (const json* wn = find(child, "width"); wn && wn->size() > 1)
                    width = f64((*wn)[1]);

                ++track_counter;
                doc["tracks"].push_back(
                    json{{"id", "t" + std::to_string(track_counter)},
                         {"net", net_name_of(net_num)},
                         {"layer", layer},
                         {"width", width},
                         {"kind", "segment"},
                         {"p1", json::array({round6(f64((*start)[1])),
                                             round6(flip_y(f64((*start)[2])))})},
                         {"p2", json::array({round6(f64((*end)[1])),
                                             round6(flip_y(f64((*end)[2])))})}});
            }
        } else if (ct == "arc") {
            // (arc (start x y) (mid x y) (end x y) (width w) (layer L) (net N))
            std::string layer = "F.Cu";
            if (const json* ln = find(child, "layer"); ln && ln->size() > 1)
                layer = map_layer(jstr((*ln)[1]));

            const json* start = find(child, "start");
            const json* end = find(child, "end");
            const json* mid = find(child, "mid");

            if (valid_cu.count(layer) && start && end && start->size() >= 3 && end->size() >= 3) {
                json net_num = json(0);
                if (const json* nn = find(child, "net"); nn && nn->size() > 1) net_num = (*nn)[1];

                double width = 0.25;
                if (const json* wn = find(child, "width"); wn && wn->size() > 1)
                    width = f64((*wn)[1]);

                json track{{"id", "t" + std::to_string(track_counter + 1)},
                           {"net", net_name_of(net_num)},
                           {"layer", layer},
                           {"width", width},
                           {"kind", "arc"},
                           {"p1", json::array({round6(f64((*start)[1])),
                                               round6(flip_y(f64((*start)[2])))})},
                           {"p2", json::array({round6(f64((*end)[1])),
                                               round6(flip_y(f64((*end)[2])))})}};
                if (mid && mid->size() >= 3)
                    track["mid"] = json::array({round6(f64((*mid)[1])),
                                                round6(flip_y(f64((*mid)[2])))});
                ++track_counter;
                doc["tracks"].push_back(std::move(track));
            } else if (layer == "Edge.Cuts" && start && end && start->size() >= 3 &&
                       end->size() >= 3) {
                json arc_shape{
                    {"type", "arc"},
                    {"p1", json::array({round6(f64((*start)[1])),
                                        round6(flip_y(f64((*start)[2])))})},
                    {"p2", json::array({round6(f64((*end)[1])),
                                        round6(flip_y(f64((*end)[2])))})}};
                if (mid && mid->size() >= 3)
                    arc_shape["mid"] = json::array({round6(f64((*mid)[1])),
                                                    round6(flip_y(f64((*mid)[2])))});
                outline_shapes.push_back(std::move(arc_shape));
            }
        } else if (ct == "via") {
            if (const json* at = find(child, "at"); at && at->size() >= 3) {
                const json via_at = json::array({round6(f64((*at)[1])),
                                                 round6(flip_y(f64((*at)[2])))});

                double drill_d = 0.4;
                if (const json* dn = find(child, "drill"); dn && dn->size() > 1)
                    drill_d = f64((*dn)[1]);
                double diameter = 0.8;
                if (const json* sn = find(child, "size"); sn && sn->size() > 1)
                    diameter = f64((*sn)[1]);

                json via_layers = json::array();
                if (const json* vln = find(child, "layers"))
                    for (std::size_t k = 1; k < vln->size(); ++k)
                        if ((*vln)[k].is_string()) {
                            const std::string ml = map_layer((*vln)[k].get<std::string>());
                            if (valid_cu.count(ml)) via_layers.push_back(ml);
                        }
                if (via_layers.empty()) via_layers = json::array({"F.Cu", "B.Cu"});

                json net_num = json(0);
                if (const json* nn = find(child, "net"); nn && nn->size() > 1) net_num = (*nn)[1];

                std::string via_type = "through";
                if (const json* vt = find(child, "type"); vt && vt->size() > 1) {
                    const std::string v = lower(jstr((*vt)[1]));
                    if (v == "through" || v == "blind" || v == "buried" || v == "micro")
                        via_type = v;
                }

                ++via_counter;
                doc["vias"].push_back(json{{"id", "v" + std::to_string(via_counter)},
                                           {"net", net_name_of(net_num)},
                                           {"at", via_at},
                                           {"drill", drill_d},
                                           {"diameter", diameter},
                                           {"layers", via_layers},
                                           {"type", via_type}});
            }
        } else if (ct == "zone") {
            std::string layer = "F.Cu";
            if (const json* ln = find(child, "layer"); ln && ln->size() > 1)
                layer = map_layer(jstr((*ln)[1]));
            if (!valid_cu.count(layer)) layer = "F.Cu";

            json net_num = json(0);
            if (const json* nn = find(child, "net"); nn && nn->size() > 1) net_num = (*nn)[1];

            json polygon = json::array();
            const json* polygon_node = find(child, "polygon");
            if (!polygon_node) polygon_node = find(child, "filled_polygon");
            if (polygon_node)
                if (const json* pts_node = find(*polygon_node, "pts"))
                    for (const json* xy : findall(*pts_node, "xy"))
                        if (xy->size() >= 3)
                            polygon.push_back(json::array(
                                {round6(f64((*xy)[1])), round6(flip_y(f64((*xy)[2])))}));

            double clearance = 0.3;
            if (const json* cp = find(child, "connect_pads"))
                if (const json* cl = find(*cp, "clearance"); cl && cl->size() > 1)
                    clearance = f64((*cl)[1]);

            std::string fill_style = "solid";
            if (const json* fill_node = find(child, "fill"))
                for (std::size_t k = 1; k < fill_node->size(); ++k)
                    if ((*fill_node)[k].is_array() && tag((*fill_node)[k]) == "hatch") {
                        fill_style = "hatch";
                        break;
                    }

            ++zone_counter;
            doc["zones"].push_back(
                json{{"id", "z" + std::to_string(zone_counter)},
                     {"net", net_name_of(net_num)},
                     {"layer", layer},
                     {"polygon", polygon},
                     {"clearance", clearance},
                     {"min_width", 0.25},
                     {"fill", fill_style},
                     {"thermal", {{"gap", 0.3}, {"spoke_width", 0.3}}},
                     {"filled_polys", json::array()}});
        } else if (ct == "gr_line") {
            std::string layer = "Dwgs.User";
            if (const json* ln = find(child, "layer"); ln && ln->size() > 1)
                layer = map_layer(jstr((*ln)[1]));

            const json* start = find(child, "start");
            const json* end = find(child, "end");
            if (start && end && start->size() >= 3 && end->size() >= 3) {
                const json shape{
                    {"type", "line"},
                    {"p1", json::array({round6(f64((*start)[1])),
                                        round6(flip_y(f64((*start)[2])))})},
                    {"p2", json::array({round6(f64((*end)[1])),
                                        round6(flip_y(f64((*end)[2])))})}};
                if (layer == "Edge.Cuts") outline_shapes.push_back(shape);
                // else: non-Edge.Cuts gr_line ignored (same as the oracle)
            }
        } else if (ct == "gr_arc") {
            std::string layer = "Dwgs.User";
            if (const json* ln = find(child, "layer"); ln && ln->size() > 1)
                layer = map_layer(jstr((*ln)[1]));

            if (layer == "Edge.Cuts") {
                const json* start = find(child, "start");
                const json* end = find(child, "end");
                const json* mid = find(child, "mid");
                if (start && end && start->size() >= 3 && end->size() >= 3) {
                    json arc_shape{
                        {"type", "arc"},
                        {"p1", json::array({round6(f64((*start)[1])),
                                            round6(flip_y(f64((*start)[2])))})},
                        {"p2", json::array({round6(f64((*end)[1])),
                                            round6(flip_y(f64((*end)[2])))})}};
                    if (mid && mid->size() >= 3)
                        arc_shape["mid"] = json::array({round6(f64((*mid)[1])),
                                                        round6(flip_y(f64((*mid)[2])))});
                    outline_shapes.push_back(std::move(arc_shape));
                }
            }
        }
        // Unknown nodes: skip silently.
    }

    if (!outline_shapes.empty())
        doc["outline"] = json{{"shapes", outline_shapes},
                              {"closed", outline_shapes.size() >= 3}};
    return doc;
}

}  // namespace chudware::ecad::kicad
