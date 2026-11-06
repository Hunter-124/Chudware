// KiCad importers — see kicad.h. Line-by-line port of:
//   engine/ecad/library/sexpr.py     (parse)
//   engine/ecad/library/kicad_sym.py (_parse_pin/_parse_graphic/_parse_symbol)
//   engine/ecad/library/kicad_mod.py (_parse_pad/_parse_graphic/_parse_model/
//                                     _parse_footprint)
//   engine/ecad/formats.py           (import_kicad_sch / import_kicad_pcb)
// Pure JSON; no OCCT.

#include "ecad/kicad.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <functional>
#include <map>
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
// sexpr.py — bounded tokenizer + iterative parser
// These are import budgets, not cache/geometry identity: ordinary KiCad trees are
// shallow, but libraries and routed boards can contain many nodes.
constexpr std::size_t kMaxInputBytes = 64 * 1024 * 1024;
constexpr std::size_t kMaxNodes = 1000000;
constexpr std::size_t kMaxDepth = 128;

std::vector<std::string> tokenize(const std::string& text) {
    if (text.size() > kMaxInputBytes)
        throw std::runtime_error("KiCad input byte limit exceeded");
    std::size_t nodes = 0, depth = 0, tokens = 0;
    auto account = [&](char c) {
        if (++tokens > 2 * kMaxNodes)
            throw std::runtime_error("KiCad token limit exceeded");
        if (c == ')') {
            if (depth) --depth;
            return;
        }
        if (++nodes > kMaxNodes)
            throw std::runtime_error("KiCad node limit exceeded");
        if (c == '(' && ++depth > kMaxDepth)
            throw std::runtime_error("KiCad depth limit exceeded");
    };
    std::vector<std::string> out;
    std::size_t i = 0;
    const std::size_t n = text.size();
    while (i < n) {
        const char c = text[i];
        if (std::isspace(static_cast<unsigned char>(c))) { ++i; continue; }
        if (c == '(' || c == ')') {
            account(c);
            out.emplace_back(1, c);
            ++i;
            continue;
        }
        // KiCad embedded files use a vertical-bar-delimited data block. It can
        // span lines and is one atom, not a sequence of whitespace tokens.
        if (c == '|') {
            account(c);
            const std::size_t end = text.find('|', i + 1);
            if (end == std::string::npos)
                throw std::runtime_error("Unterminated KiCad data block");
            out.push_back(text.substr(i, end - i + 1));
            i = end + 1;
            continue;
        }
        if (c == '"') {
            account(c);
            // quoted string: "(?:[^"\\]|\\.)*"
            std::string tok(1, '"');
            std::size_t j = i + 1;
            bool terminated = false;
            while (j < n) {
                const char d = text[j];
                tok += d;
                if (d == '\\' && j + 1 < n) { tok += text[j + 1]; j += 2; continue; }
                ++j;
                if (d == '"') { terminated = true; break; }
            }
            if (!terminated) throw std::runtime_error("Unterminated KiCad string");
            out.push_back(std::move(tok));
            i = j;
            continue;
        }
        // bare symbol / number: [^\s()"]+
        account(c);
        std::size_t j = i;
        while (j < n && !std::isspace(static_cast<unsigned char>(text[j])) && text[j] != '(' &&
               text[j] != ')' && text[j] != '"')
            ++j;
        out.push_back(text.substr(i, j - i));
        i = j;
    }
    return out;
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
        const std::size_t end = tok.size() >= 2 ? tok.size() - 1 : tok.size();
        std::string decoded;
        decoded.reserve(end);
        for (std::size_t i = 1; i < end; ++i) {
            if (tok[i] != '\\' || i + 1 >= end) {
                decoded += tok[i];
                continue;
            }
            const char escaped = tok[++i];
            if (escaped == 'n') decoded += '\n';
            else if (escaped == 'r') decoded += '\r';
            else if (escaped == 't') decoded += '\t';
            else if (escaped == '"' || escaped == '\\') decoded += escaped;
            else {
                decoded += '\\';
                decoded += escaped;
            }
        }
        return decoded;
    }
    long long iv = 0;
    if (parse_int64(tok, iv)) return iv;
    char* end = nullptr;
    const double dv = std::strtod(tok.c_str(), &end);
    if (end == tok.c_str() + tok.size() && !tok.empty()) return dv;
    return tok;  // bare symbol
}

json build(const std::vector<std::string>& toks, std::size_t& pos) {
    std::vector<json> stack;
    stack.reserve(kMaxDepth);
    while (pos < toks.size()) {
        const std::string& tok = toks[pos];
        if (tok == "(") {
            stack.push_back(json::array());
            ++pos;
            continue;
        }
        if (tok == ")") {
            if (stack.empty())
                throw std::runtime_error("Unexpected ')' at token index " + std::to_string(pos));
            json completed = std::move(stack.back());
            stack.pop_back();
            ++pos;
            if (stack.empty()) return completed;
            stack.back().push_back(std::move(completed));
            continue;
        }
        json atom = parse_atom(tok);
        ++pos;
        if (stack.empty()) return atom;
        stack.back().push_back(std::move(atom));
    }
    throw std::runtime_error("Unexpected end of input: unmatched '('");
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


std::string read_text(const std::string& path) {
    std::ifstream f(std::filesystem::u8path(path), std::ios::binary);
    if (!f) throw std::runtime_error("No such file or directory: '" + path + "'");
    std::string text;
    char chunk[16384];
    while (f) {
        f.read(chunk, sizeof(chunk));
        const std::size_t n = static_cast<std::size_t>(f.gcount());
        if (n > kMaxInputBytes - text.size())
            throw std::runtime_error("KiCad input byte limit exceeded");
        text.append(chunk, n);
    }
    if (!f.eof()) throw std::runtime_error("Cannot read KiCad file: '" + path + "'");
    return text;
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
    if (source.size() > kMaxInputBytes)
        throw std::runtime_error("KiCad input byte limit exceeded");
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
        y = f64((*at)[2]);
        if (at->size() > 3) angle = f64((*at)[3]);
    }
    double length = 2.54;
    if (const json* len = find(node, "length"); len && len->size() > 1)
        length = f64((*len)[1]);
    std::string pin_name;
    if (const json* nm = find(node, "name"); nm && nm->size() > 1) pin_name = jstr((*nm)[1]);
    std::string pin_number = "1";
    if (const json* num = find(node, "number"); num && num->size() > 1)
        pin_number = jstr((*num)[1]);

    json alternates = json::array();
    for (const json* alt : findall(node, "alternate")) {
        if (alt->size() < 2) continue;
        const std::string alt_type =
            alt->size() > 2 ? map_or(pin_type_map(), lower(jstr((*alt)[2])), "passive")
                            : pin_type;
        const std::string alt_shape =
            alt->size() > 3 ? map_or(pin_shape_map(), lower(jstr((*alt)[3])), "line")
                            : pin_shape;
        alternates.push_back(json{{"name", jstr((*alt)[1])},
                                  {"type", alt_type},
                                  {"shape", alt_shape}});
    }

    // KiCad library pin positions are the electrical ends, in a +Y-up
    // coordinate system. The native schema stores the body end and an
    // outward-facing angle instead.
    const double radians = angle * 3.14159265358979323846 / 180.0;
    x += length * std::cos(radians);
    y += length * std::sin(radians);
    angle = std::fmod(angle + 180.0, 360.0);
    if (angle < 0) angle += 360.0;
    return json{{"number", pin_number}, {"name", pin_name},
                {"pos", json::array({round6(x), round6(y)})}, {"angle", angle},
                {"length", length},        {"type", pin_type},
                {"shape", pin_shape},      {"unit", unit},
                {"alt", alternates}};
}

const std::set<std::string>& sym_graphic_tags() {
    static const std::set<std::string> t{"rectangle", "polyline", "circle", "arc", "text"};
    return t;
}

// kicad_sym.py::_parse_graphic — body graphic -> Graphic dict (best-effort).
json parse_sym_graphic(const json& node, bool sheet_coords = false) {
    const std::string t = tag(node);
    if (t == "rectangle") {
        const json* start = find(node, "start");
        const json* end = find(node, "end");
        if (start && end && start->size() >= 3 && end->size() >= 3) {
            json p1 = json::array({f64((*start)[1]), f64((*start)[2])});
            json p2 = json::array({f64((*end)[1]), f64((*end)[2])});
            return json{{"kind", "rect"}, {"pts", json::array({p1, p2})},
                        {"type", "rect"}, {"p1", p1}, {"p2", p2}};
        }
    } else if (t == "polyline") {
        if (const json* pts_node = find(node, "pts")) {
            json pts = json::array();
            for (const json* xy : findall(*pts_node, "xy"))
                if (xy->size() >= 3)
                    pts.push_back(json::array({f64((*xy)[1]), f64((*xy)[2])}));
            if (!pts.empty())
                return json{{"kind", "polyline"}, {"pts", pts},
                            {"type", "polyline"}};
        }
    } else if (t == "circle") {
        const json* center = find(node, "center");
        const json* radius = find(node, "radius");
        if (center && radius && center->size() >= 3 && radius->size() >= 2) {
            json c = json::array({f64((*center)[1]), f64((*center)[2])});
            return json{{"kind", "circle"}, {"pts", json::array({c})},
                        {"radius", f64((*radius)[1])}, {"type", "circle"},
                        {"center", c}};
        }
    } else if (t == "arc") {
        const json* start = find(node, "start");
        const json* mid = find(node, "mid");
        const json* end = find(node, "end");
        if (start && mid && end && start->size() >= 3 && mid->size() >= 3 &&
            end->size() >= 3) {
            const double sign = sheet_coords ? -1.0 : 1.0;
            const double x1 = f64((*start)[1]), y1 = sign * f64((*start)[2]);
            const double x2 = f64((*mid)[1]), y2 = sign * f64((*mid)[2]);
            const double x3 = f64((*end)[1]), y3 = sign * f64((*end)[2]);
            const double d = 2.0 * (x1 * (y2 - y3) + x2 * (y3 - y1) +
                                    x3 * (y1 - y2));
            json p1 = json::array({x1, y1});
            json pm = json::array({x2, y2});
            json p2 = json::array({x3, y3});
            if (std::abs(d) > 1e-12) {
                const double q1 = x1 * x1 + y1 * y1;
                const double q2 = x2 * x2 + y2 * y2;
                const double q3 = x3 * x3 + y3 * y3;
                const double cx = (q1 * (y2 - y3) + q2 * (y3 - y1) +
                                   q3 * (y1 - y2)) / d;
                const double cy = (q1 * (x3 - x2) + q2 * (x1 - x3) +
                                   q3 * (x2 - x1)) / d;
                const double rad_to_deg = 180.0 / 3.14159265358979323846;
                const double a0 = std::atan2(y1 - cy, x1 - cx) * rad_to_deg;
                const double a1 = std::atan2(y3 - cy, x3 - cx) * rad_to_deg;
                json center = json::array({cx, cy});
                return json{{"kind", "arc"}, {"pts", json::array({center})},
                            {"radius", std::hypot(x1 - cx, y1 - cy)},
                            {"a0", a0}, {"a1", a1}, {"type", "arc"},
                            {"p1", p1}, {"mid", pm}, {"p2", p2}};
            }
            return json{{"kind", "polyline"}, {"pts", json::array({p1, pm, p2})},
                        {"type", "polyline"}};
        }
    } else if (t == "text") {
        const std::string text_val = node.size() > 1 ? jstr(node[1]) : "";
        if (const json* at = find(node, "at"); at && at->size() >= 3) {
            json p = json::array({f64((*at)[1]), f64((*at)[2])});
            double size = 1.27;
            if (const json* effects = find(node, "effects")) {
                if (const json* font = find(*effects, "font")) {
                    if (const json* sz = find(*font, "size"); sz && sz->size() > 1)
                        size = f64((*sz)[1]);
                }
            }
            return json{{"kind", "text"}, {"pts", json::array({p})},
                        {"text", text_val}, {"size", size},
                        {"type", "text"}, {"pos", p}};
        }
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

    const auto get_prop = [&](const char* k, const std::string& dflt) {
        const auto it = props.find(k);
        return it != props.end() ? it->second : dflt;
    };
    const std::string prefix =
        get_prop("Reference", raw_name.empty() ? "U" : raw_name.substr(0, 1));
    const std::string value = get_prop("Value", raw_name);
    const std::string footprint = get_prop("Footprint", "");
    const std::string datasheet = get_prop("Datasheet", "");
    const std::string description =
        get_prop("Description", get_prop("ki_description", ""));
    const std::string keyword_text =
        get_prop("Keywords", get_prop("ki_keywords", ""));

    json keywords = json::array();
    std::string word;
    for (char c : keyword_text) {
        if (std::isspace(static_cast<unsigned char>(c)) || c == ',') {
            if (!word.empty()) {
                keywords.push_back(word);
                word.clear();
            }
        } else {
            word += c;
        }
    }
    if (!word.empty()) keywords.push_back(word);

    json filters = json::array();
    if (const json* fp_filters = find(sym_node, "fp_filters")) {
        for (std::size_t i = 1; i < fp_filters->size(); ++i)
            if (!jstr((*fp_filters)[i]).empty()) filters.push_back(jstr((*fp_filters)[i]));
    }
    if (filters.empty()) {
        const std::string encoded = get_prop("ki_fp_filters", "");
        std::istringstream ss(encoded);
        while (ss >> word) filters.push_back(word);
    }

    static const std::set<std::string> kReserved{
        "Reference", "Value", "Footprint", "Datasheet", "Description",
        "Keywords", "ki_keywords", "ki_description", "ki_fp_filters"};
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

    const int units = units_seen.empty() ? 1 : *units_seen.rbegin();
    const std::string libid = lib_name.empty() ? raw_name : lib_name + ":" + raw_name;
    const bool power = find(sym_node, "power") != nullptr || prefix == "#PWR";

    return json{{"libid", libid},
                {"name", raw_name},
                {"description", description},
                {"keywords", keywords},
                {"prefix", prefix},
                {"power", power},
                {"units", units},
                {"default_footprint", footprint},
                {"footprint_filters", filters},
                {"datasheet", datasheet},
                {"pins", pins},
                {"graphics", graphics},
                // Compatibility with the original aicad.sch/1 SymbolDef readers.
                {"reference_prefix", prefix},
                {"value", value},
                {"units_count", units},
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
    static const std::set<std::string> t{"fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly", "fp_text"};
    return t;
}

// kicad_mod.py::_parse_graphic — footprint graphic (best-effort).
json parse_fp_graphic_geometry(const json& node) {
    const std::string t = tag(node);
    std::string layer = "F.SilkS";
    if (const json* layer_node = find(node, "layer"); layer_node && layer_node->size() > 1)
        layer = jstr((*layer_node)[1]);

    if (t == "fp_line" || t == "fp_rect") {
        const json* start = find(node, "start");
        const json* end = find(node, "end");
        if (start && end && start->size() >= 3 && end->size() >= 3)
            return json{{"type", t == "fp_rect" ? "rect" : "line"},
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

json parse_fp_graphic(const json& node) {
    json graphic = parse_fp_graphic_geometry(node);
    if (graphic.is_null()) return graphic;
    const json* width = find(node, "width");
    if (const json* stroke = find(node, "stroke"))
        if (const json* stroke_width = find(*stroke, "width")) width = stroke_width;
    if (width && width->size() > 1 && (*width)[1].is_number())
        graphic["width"] = (*width)[1];
    if (const json* at = find(node, "at"); at && at->size() > 3 && (*at)[3].is_number())
        graphic["angle"] = (*at)[3];
    return graphic;
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
    const json* description = find(tree, "descr");

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
                {"description", description && description->size() > 1 ? jstr((*description)[1]) : ""}};
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

bool node_yes_no(const json& node, const std::string& key, bool dflt) {
    const json* value = find(node, key);
    if (!value || value->size() < 2) return dflt;
    const std::string raw = lower(jstr((*value)[1]));
    if (raw == "yes" || raw == "true" || raw == "1") return true;
    if (raw == "no" || raw == "false" || raw == "0") return false;
    return dflt;
}

std::string node_uuid(const json& node) {
    const json* uuid = find(node, "uuid");
    return uuid && uuid->size() > 1 ? jstr((*uuid)[1]) : "";
}

// Keep all original child nodes (and any positional atoms) in order, including
// modeled children whose KiCad-only attributes must survive unrelated edits.
void retain_uuid(json& object, const json& node) {
    const std::string uuid = node_uuid(node);
    if (!uuid.empty()) object["uuid"] = uuid;
    object["kicad_tag"] = tag(node);
    object["kicad_extra"] = json::array();
    for (std::size_t i = 1; i < node.size(); ++i) object["kicad_extra"].push_back(node[i]);
}

json modeled_snapshot(const json& object) {
    json snapshot = object;
    snapshot.erase("uuid");
    snapshot.erase("kicad_extra");
    snapshot.erase("kicad_tag");
    snapshot.erase("kicad_original");
    return snapshot;
}

void snapshot_objects(json& value) {
    if (value.is_array()) {
        for (json& item : value) snapshot_objects(item);
    } else if (value.is_object()) {
        for (auto& [key, item] : value.items())
            if (key != "kicad_extra" && key != "kicad_original")
                snapshot_objects(item);
        if (value.contains("kicad_extra"))
            value["kicad_original"] = modeled_snapshot(value);
    }
}

json root_metadata(const json& tree) {
    json result{{"version", 0}, {"generator", ""}, {"generator_version", ""},
                {"uuid", node_uuid(tree)}, {"extra", json::array()}};
    for (const char* key : {"version", "generator", "generator_version"}) {
        if (const json* node = find(tree, key); node && node->size() > 1)
            result[key] = (*node)[1];
    }
    return result;
}

// KiCad 9 and earlier use "~" as the empty-name/field sentinel. KiCad 10
// treats it as literal text, so normalize retained nodes before modeling them.
void upgrade_schematic_sentinels(json& node, const std::string& parent = "") {
    if (!node.is_array() || node.empty()) return;
    const std::string kind = tag(node);
    if (kind == "name" && parent == "pin" && node.size() > 1 && node[1] == "~")
        node[1] = "";
    else if (kind == "property" && node.size() > 2 && node[2] == "~")
        node[2] = "";
    for (std::size_t i = 1; i < node.size(); ++i)
        if (node[i].is_array()) upgrade_schematic_sentinels(node[i], kind);
}

json node_points(const json& node) {
    json pts = json::array();
    if (const json* pts_node = find(node, "pts")) {
        for (const json* xy : findall(*pts_node, "xy")) {
            if (xy->size() >= 3)
                pts.push_back(json::array({round6(f64((*xy)[1])),
                                           round6(flip_y(f64((*xy)[2])))}));
        }
    }
    return pts;
}

double stroke_width(const json& node, double dflt = 0.25) {
    if (const json* stroke = find(node, "stroke")) {
        if (const json* width = find(*stroke, "width"); width && width->size() > 1)
            return f64((*width)[1]);
    }
    return dflt;
}

bool fill_enabled(const json& node) {
    if (const json* fill = find(node, "fill")) {
        if (const json* type = find(*fill, "type"); type && type->size() > 1)
            return lower(jstr((*type)[1])) != "none";
    }
    return false;
}

json parse_sch_graphic(const json& node) {
    const std::string t = tag(node);
    json out;
    if (t == "polyline") {
        json pts = node_points(node);
        if (pts.size() < 2) return json(nullptr);
        out = json{{"kind", "polyline"}, {"pts", pts}};
    } else if (t == "rectangle") {
        const json* start = find(node, "start");
        const json* end = find(node, "end");
        if (!start || !end || start->size() < 3 || end->size() < 3) return json(nullptr);
        out = json{{"kind", "rect"},
                   {"pts", json::array({
                       json::array({round6(f64((*start)[1])),
                                    round6(flip_y(f64((*start)[2])))}),
                       json::array({round6(f64((*end)[1])),
                                    round6(flip_y(f64((*end)[2])))})})}};
    } else if (t == "circle") {
        const json* center = find(node, "center");
        const json* radius = find(node, "radius");
        if (!center || !radius || center->size() < 3 || radius->size() < 2)
            return json(nullptr);
        out = json{{"kind", "circle"},
                   {"pts", json::array({json::array({
                       round6(f64((*center)[1])), round6(flip_y(f64((*center)[2])))})})},
                   {"radius", f64((*radius)[1])}};
    } else if (t == "arc") {
        json parsed = parse_sym_graphic(node, true);
        if (parsed.is_null()) return parsed;
        out["kind"] = parsed.value("kind", std::string("arc"));
        out["pts"] = parsed.value("pts", json::array());
        if (parsed.contains("radius")) out["radius"] = parsed["radius"];
        if (parsed.contains("a0")) out["a0"] = parsed["a0"];
        if (parsed.contains("a1")) out["a1"] = parsed["a1"];
        if (parsed.contains("p1")) out["kicad_start"] = parsed["p1"];
        if (parsed.contains("mid")) out["kicad_mid"] = parsed["mid"];
        if (parsed.contains("p2")) out["kicad_end"] = parsed["p2"];
    } else {
        return json(nullptr);
    }
    out["width"] = stroke_width(node);
    out["filled"] = fill_enabled(node);
    return out;
}

bool point_on_segment(const json& p, const json& a, const json& b) {
    if (!p.is_array() || !a.is_array() || !b.is_array() ||
        p.size() < 2 || a.size() < 2 || b.size() < 2)
        return false;
    const double px = p[0].get<double>(), py = p[1].get<double>();
    const double ax = a[0].get<double>(), ay = a[1].get<double>();
    const double bx = b[0].get<double>(), by = b[1].get<double>();
    const double cross = (px - ax) * (by - ay) - (py - ay) * (bx - ax);
    if (std::abs(cross) > 1e-6) return false;
    return px >= std::min(ax, bx) - 1e-6 && px <= std::max(ax, bx) + 1e-6 &&
           py >= std::min(ay, by) - 1e-6 && py <= std::max(ay, by) + 1e-6;
}

json expand_bus_label(const std::string& label) {
    json members = json::array();
    static const std::regex range_re(R"(^(.+)\[(-?\d+)\.\.(-?\d+)\]$)");
    std::smatch m;
    if (std::regex_match(label, m, range_re)) {
        const std::string prefix = m[1].str();
        const int first = std::stoi(m[2].str());
        const int last = std::stoi(m[3].str());
        const int step = first <= last ? 1 : -1;
        for (int i = first;; i += step) {
            members.push_back(prefix + std::to_string(i));
            if (i == last) break;
        }
        return members;
    }
    const std::size_t open = label.find('{');
    const std::size_t close = label.rfind('}');
    if (open != std::string::npos && close > open) {
        const std::string prefix = label.substr(0, open);
        std::istringstream ss(label.substr(open + 1, close - open - 1));
        std::string member;
        while (ss >> member) {
            json nested = expand_bus_label(member);
            if (nested.empty()) members.push_back(prefix + member);
            else for (const json& item : nested)
                members.push_back(prefix + item.get<std::string>());
        }
    }
    return members;
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

json symbol_def(const json& node, const std::string& nickname) {
    json copy = node;
    if (copy.is_array() && copy.size() > 1 && copy[1].is_string()) {
        const std::string name = copy[1].get<std::string>();
        const auto colon = name.find(':');
        if (colon != std::string::npos) copy[1] = name.substr(colon + 1);
    }
    return parse_symbol(copy, nickname);
}

json footprint_def(const json& node, const std::string& fpid) {
    return parse_footprint(node, fpid);
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

    json tree = sexpr_parse(text);
    if (tag(tree) != "kicad_sch")
        throw std::runtime_error("Expected kicad_sch root, got '" + tag(tree) + "'");
    if (const json* version = find(tree, "version"); version && version->size() > 1 &&
        (*version)[1].is_number() && f64((*version)[1]) < 20260306)
        upgrade_schematic_sentinels(tree);

    json doc = schema::new_schematic(src_name);
    doc["bus_entries"] = json::array();
    doc["texts"] = json::array();
    doc["title_block"] = json{{"title", ""}, {"company", ""}, {"rev", ""},
                              {"date", ""}, {"comment1", ""}, {"comment2", ""},
                              {"comment3", ""}, {"comment4", ""}, {"paper", "A4"}};
    doc["kicad"] = root_metadata(tree);
    doc["kicad"]["lib_symbols"] = json::object();

    json lib_cache = json::object();
    int sym_counter = 0, wire_counter = 0, junction_counter = 0, label_counter = 0,
        power_counter = 0, nc_counter = 0, bus_counter = 0, bus_entry_counter = 0,
        text_counter = 0, graphic_counter = 0, child_sheet_counter = 0;
    const std::string sheet_id = "sh1";
    std::map<std::string, int> unsupported;
    std::map<std::string, json> bus_aliases;

    static const std::regex kSubSymbolRe("_\\d+_\\d+$");
    if (const json* lib_syms = find(tree, "lib_symbols")) {
        for (std::size_t i = 1; i < lib_syms->size(); ++i) {
            const json& child = (*lib_syms)[i];
            if (!child.is_array() || tag(child) != "symbol") continue;
            const std::string sym_name_raw = child.size() > 1 ? jstr(child[1]) : "";
            if (!sym_name_raw.empty()) doc["kicad"]["lib_symbols"][sym_name_raw] = child;
            if (std::regex_search(sym_name_raw, kSubSymbolRe)) continue;
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

    if (const json* paper = find(tree, "paper"); paper && paper->size() > 1)
        doc["title_block"]["paper"] = jstr((*paper)[1]);
    if (const json* title = find(tree, "title_block")) {
        const auto assign = [&](const char* source_key, const char* dest_key) {
            if (const json* n = find(*title, source_key); n && n->size() > 1)
                doc["title_block"][dest_key] = jstr((*n)[1]);
        };
        assign("title", "title");
        assign("company", "company");
        assign("rev", "rev");
        assign("date", "date");
        for (const json* comment : findall(*title, "comment")) {
            if (comment->size() < 3) continue;
            const int number = static_cast<int>(f64((*comment)[1]));
            if (number >= 1 && number <= 4)
                doc["title_block"]["comment" + std::to_string(number)] = jstr((*comment)[2]);
        }
    }

    for (const json* alias : findall(tree, "bus_alias")) {
        if (alias->size() < 2) continue;
        json members = json::array();
        if (const json* list = find(*alias, "members")) {
            for (std::size_t i = 1; i < list->size(); ++i) {
                const std::string member = jstr((*list)[i]);
                if (!member.empty()) members.push_back(member);
            }
        }
        bus_aliases[jstr((*alias)[1])] = members;
    }

    static const std::set<std::string> kPromotedProps{
        "Reference", "Value", "Footprint", "Datasheet"};
    static const std::set<std::string> kMetadataNodes{
        "version", "generator", "generator_version", "uuid", "paper", "title_block",
        "lib_symbols", "bus_alias", "sheet_instances", "embedded_fonts"};

    static const std::vector<std::string> object_lists{
        "symbols", "power", "wires", "buses", "bus_entries", "junctions",
        "no_connect", "labels", "texts", "graphics", "sheet_symbols"};
    for (std::size_t i = 1; i < tree.size(); ++i) {
        const json& child = tree[i];
        if (!child.is_array()) continue;
        const std::string ct = tag(child);
        std::vector<std::size_t> before;
        for (const auto& key : object_lists) before.push_back(doc[key].size());

        if (ct == "symbol") {
            std::string lib_id = "Device:Unknown";
            if (const json* li = find(child, "lib_id"); li && li->size() > 1)
                lib_id = jstr((*li)[1]);

            double x = 0.0, y = 0.0, angle = 0.0;
            at_xy(child, x, y, angle);
            std::string mirror = "none";
            if (const json* mv = find(child, "mirror"); mv && mv->size() > 1) {
                const std::string value = lower(jstr((*mv)[1]));
                if (value == "x" || value == "y") mirror = value;
            }
            long long unit = 1;
            if (const json* un = find(child, "unit"); un && un->size() > 1)
                unit = static_cast<long long>(f64((*un)[1]));

            const json props = sch_inst_props(child);
            const auto get_prop = [&](const char* key, const char* dflt) {
                return props.contains(key) ? props[key].get<std::string>()
                                           : std::string(dflt);
            };
            const std::string ref = get_prop("Reference", "U?");
            const std::string value = get_prop("Value", "");
            const std::string footprint = get_prop("Footprint", "");
            const std::string datasheet = get_prop("Datasheet", "");
            json fields = json::object();
            for (const auto& [key, field_value] : props.items())
                if (!kPromotedProps.count(key)) fields[key] = field_value;

            if (!lib_cache.contains(lib_id)) {
                const std::size_t colon = lib_id.find(':');
                const std::string short_name =
                    colon == std::string::npos ? lib_id : lib_id.substr(colon + 1);
                const std::string stripped = rstrip_chars(ref, "0123456789?");
                const std::string prefix = stripped.empty() ? "U" : stripped;
                const bool power = starts_with(lib_id, "power:") || starts_with(ref, "#PWR");
                lib_cache[lib_id] = json{
                    {"libid", lib_id}, {"name", short_name}, {"description", ""},
                    {"keywords", json::array()}, {"prefix", prefix}, {"power", power},
                    {"units", 1}, {"default_footprint", footprint},
                    {"footprint_filters", json::array()}, {"datasheet", datasheet},
                    {"pins", json::array()}, {"graphics", json::array()},
                    {"reference_prefix", prefix}, {"value", value}, {"units_count", 1},
                    {"fields", json::object()}};
            }

            const json pos = json::array({round6(x), round6(y)});
            if (starts_with(lib_id, "power:") || starts_with(ref, "#PWR")) {
                const std::string symbol = lib_id.find(':') != std::string::npos
                                               ? lib_id.substr(lib_id.find(':') + 1)
                                               : lib_id;
                std::string net_name = symbol;
                if (symbol == "PWR_FLAG" && props.contains("Chudware.PowerNet") &&
                    props["Chudware.PowerNet"].is_string()) {
                    net_name = props["Chudware.PowerNet"].get<std::string>();
                    fields.erase("Chudware.PowerNet");
                }
                if (net_name.empty()) net_name = !value.empty() ? value : ref;
                json object{{"id", "p" + std::to_string(++power_counter)},
                            {"sheet", sheet_id}, {"pos", pos}, {"net", net_name},
                            {"symbol", symbol}, {"libid", lib_id}, {"ref", ref},
                            {"value", value}, {"footprint", footprint},
                            {"datasheet", datasheet}, {"angle", angle},
                            {"mirror", mirror}, {"unit", unit}, {"fields", fields},
                            {"in_bom", node_yes_no(child, "in_bom", true)},
                            {"on_board", node_yes_no(child, "on_board", true)},
                            {"dnp", node_yes_no(child, "dnp", false)}};
                doc["power"].push_back(std::move(object));
            } else {
                json object{{"id", "s" + std::to_string(++sym_counter)},
                            {"libid", lib_id}, {"sheet", sheet_id}, {"ref", ref},
                            {"value", value}, {"footprint", footprint},
                            {"datasheet", datasheet}, {"pos", pos}, {"angle", angle},
                            {"mirror", mirror}, {"unit", unit}, {"fields", fields},
                            {"in_bom", node_yes_no(child, "in_bom", true)},
                            {"on_board", node_yes_no(child, "on_board", true)},
                            {"dnp", node_yes_no(child, "dnp", false)}};
                if (const json* alternate = find(child, "alternate");
                    alternate && alternate->size() > 1)
                    object["alternate"] = jstr((*alternate)[1]);
                json pin_alternates = json::object();
                for (const json* pin : findall(child, "pin")) {
                    if (pin->size() < 2) continue;
                    if (const json* alternate = find(*pin, "alternate");
                        alternate && alternate->size() > 1)
                        pin_alternates[jstr((*pin)[1])] = jstr((*alternate)[1]);
                }
                if (!pin_alternates.empty()) object["pin_alternates"] = pin_alternates;
                doc["symbols"].push_back(std::move(object));
            }
        } else if (ct == "wire" || ct == "bus") {
            json pts = node_points(child);
            if (pts.size() >= 2) {
                if (ct == "wire") {
                    json object{{"id", "w" + std::to_string(++wire_counter)},
                                {"sheet", sheet_id}, {"pts", pts}, {"bus", false}};
                    retain_uuid(object, child);
                    doc["wires"].push_back(std::move(object));
                } else {
                    json object{{"id", "b" + std::to_string(++bus_counter)},
                                {"sheet", sheet_id},
                                {"name", "BUS" + std::to_string(bus_counter)},
                                {"pts", pts}, {"members", json::array()}};
                    retain_uuid(object, child);
                    doc["buses"].push_back(std::move(object));
                }
            }
        } else if (ct == "bus_entry") {
            const json* at = find(child, "at");
            const json* size = find(child, "size");
            if (at && at->size() >= 3 && size && size->size() >= 3) {
                json object{{"id", "be" + std::to_string(++bus_entry_counter)},
                            {"sheet", sheet_id},
                            {"pos", json::array({round6(f64((*at)[1])),
                                                 round6(flip_y(f64((*at)[2])))})},
                            {"size", json::array({round6(f64((*size)[1])),
                                                  round6(flip_y(f64((*size)[2])))})}};
                retain_uuid(object, child);
                doc["bus_entries"].push_back(std::move(object));
            }
        } else if (ct == "junction" || ct == "no_connect") {
            if (const json* at = find(child, "at"); at && at->size() >= 3) {
                const json pos = json::array({round6(f64((*at)[1])),
                                               round6(flip_y(f64((*at)[2])))});
                if (ct == "junction") {
                    json object{{"id", "j" + std::to_string(++junction_counter)},
                                {"sheet", sheet_id}, {"pos", pos}};
                    retain_uuid(object, child);
                    doc["junctions"].push_back(std::move(object));
                } else {
                    json object{{"id", "nc" + std::to_string(++nc_counter)},
                                {"sheet", sheet_id}, {"pos", pos}};
                    retain_uuid(object, child);
                    doc["no_connect"].push_back(std::move(object));
                }
            }
        } else if (ct == "label" || ct == "global_label" ||
                   ct == "hierarchical_label") {
            const std::string text_value =
                child.size() > 1 && !child[1].is_array() ? jstr(child[1]) : "";
            double x = 0.0, y = 0.0, angle = 0.0;
            at_xy(child, x, y, angle);
            if (!text_value.empty()) {
                const std::string scope =
                    ct == "label" ? "local" : (ct == "global_label" ? "global" : "hier");
                json object{{"id", "l" + std::to_string(++label_counter)},
                            {"sheet", sheet_id},
                            {"pos", json::array({round6(x), round6(y)})},
                            {"text", text_value}, {"scope", scope}, {"angle", angle}};
                retain_uuid(object, child);
                doc["labels"].push_back(std::move(object));
            }
        } else if (ct == "text" || ct == "text_box") {
            const std::string text_value =
                child.size() > 1 && !child[1].is_array() ? jstr(child[1]) : "";
            double x = 0.0, y = 0.0, angle = 0.0;
            at_xy(child, x, y, angle);
            double size = 1.27;
            if (const json* effects = find(child, "effects")) {
                if (const json* font = find(*effects, "font")) {
                    if (const json* font_size = find(*font, "size");
                        font_size && font_size->size() > 1)
                        size = f64((*font_size)[1]);
                }
            }
            json object{{"id", "tx" + std::to_string(++text_counter)},
                        {"sheet", sheet_id},
                        {"pos", json::array({round6(x), round6(y)})},
                        {"text", text_value}, {"size", size}, {"angle", angle}};
            if (ct == "text_box") {
                object["text_box"] = true;
                if (const json* box = find(child, "size"); box && box->size() >= 3)
                    object["box_size"] = json::array(
                        {round6(f64((*box)[1])), round6(f64((*box)[2]))});
            }
            retain_uuid(object, child);
            doc["texts"].push_back(std::move(object));
        } else if (ct == "polyline" || ct == "rectangle" ||
                   ct == "circle" || ct == "arc") {
            json object = parse_sch_graphic(child);
            if (!object.is_null()) {
                object["id"] = "g" + std::to_string(++graphic_counter);
                object["sheet"] = sheet_id;
                retain_uuid(object, child);
                doc["graphics"].push_back(std::move(object));
            }
        } else if (ct == "sheet") {
            const json props = sch_inst_props(child);
            const auto prop = [&](const char* key, const std::string& dflt) {
                return props.contains(key) ? props[key].get<std::string>() : dflt;
            };
            const std::string child_id = "sh" + std::to_string(++child_sheet_counter + 1);
            double x = 0.0, y = 0.0, angle = 0.0;
            at_xy(child, x, y, angle);
            json size = json::array({100.0, 75.0});
            if (const json* value = find(child, "size"); value && value->size() >= 3)
                size = json::array({f64((*value)[1]), f64((*value)[2])});

            json pins = json::array();
            json ports = json::array();
            for (const json* pin : findall(child, "pin")) {
                if (pin->size() < 2) continue;
                const std::string name = jstr((*pin)[1]);
                const std::string shape =
                    pin->size() > 2 ? map_or(pin_type_map(), lower(jstr((*pin)[2])), "passive")
                                    : "passive";
                double px = x, py = y, pin_angle = 0.0;
                at_xy(*pin, px, py, pin_angle);
                json pin_object{{"name", name},
                                {"pos", json::array({round6(px), round6(py)})},
                                {"shape", shape}, {"angle", pin_angle}};
                retain_uuid(pin_object, *pin);
                pins.push_back(pin_object);
                ports.push_back(json{{"name", name}, {"type", shape},
                                     {"pos", pin_object["pos"]}});
            }

            json sheet_fields = json::object();
            for (const auto& [key, value] : props.items()) sheet_fields[key] = value;
            json sheet_object{{"id", child_id},
                              {"name", prop("Sheetname", "Sheet" + std::to_string(child_sheet_counter))},
                              {"parent", sheet_id}, {"size", size},
                              {"instances_of", prop("Sheetfile", "")},
                              {"ports", ports}, {"fields", sheet_fields}};
            const std::string sheet_uuid = node_uuid(child);
            if (!sheet_uuid.empty()) sheet_object["uuid"] = sheet_uuid;
            json symbol_object{{"id", "ss" + std::to_string(child_sheet_counter)},
                               {"sheet", sheet_id}, {"child", child_id},
                               {"pos", json::array({round6(x), round6(y)})},
                               {"size", size}, {"pins", pins}, {"fields", sheet_fields}};
            doc["sheets"].push_back(std::move(sheet_object));
            doc["sheet_symbols"].push_back(std::move(symbol_object));
        } else if (!kMetadataNodes.count(ct)) {
            ++unsupported[ct.empty() ? "<malformed>" : ct];
        }
        bool represented = false;
        for (std::size_t k = 0; k < object_lists.size(); ++k) {
            json& list = doc[object_lists[k]];
            if (list.size() > before[k]) {
                retain_uuid(list.back(), child);
                if ((object_lists[k] == "symbols" || object_lists[k] == "power") &&
                    list.back().contains("fields")) {
                    json& fields = list.back()["fields"];
                    if (fields.contains("Chudware.SimParams") &&
                        fields["Chudware.SimParams"].is_string()) {
                        json sim = json::parse(fields["Chudware.SimParams"].get<std::string>(),
                                               nullptr, false);
                        if (!sim.is_discarded()) {
                            list.back()["sim_params"] = std::move(sim);
                            fields.erase("Chudware.SimParams");
                        }
                    }
                    if (fields.contains("Chudware.Fields") && fields["Chudware.Fields"].is_string()) {
                        json values = json::parse(fields["Chudware.Fields"].get<std::string>(),
                                                  nullptr, false);
                        if (values.is_object()) {
                            fields.erase("Chudware.Fields");
                            for (const auto& [name, value] : values.items()) fields[name] = value;
                        }
                    }
                }
                represented = true;
            }
        }
        if (!represented && ct != "version" && ct != "generator" &&
            ct != "generator_version" && ct != "uuid" && ct != "lib_symbols")
            doc["kicad"]["extra"].push_back(child);
    }

    for (json& bus : doc["buses"]) {
        for (const json& label : doc["labels"]) {
            if (label.value("sheet", std::string()) != bus.value("sheet", std::string()))
                continue;
            bool attached = false;
            const json& pts = bus["pts"];
            for (std::size_t i = 1; i < pts.size(); ++i) {
                if (point_on_segment(label["pos"], pts[i - 1], pts[i])) {
                    attached = true;
                    break;
                }
            }
            if (!attached) continue;
            const std::string name = label.value("text", std::string());
            json members = expand_bus_label(name);
            const auto alias = bus_aliases.find(name);
            if (members.empty() && alias != bus_aliases.end()) members = alias->second;
            if (!members.empty()) {
                bus["name"] = name;
                bus["members"] = members;
                break;
            }
        }
    }

    doc["warnings"] = json::array();
    for (const auto& [kind, count] : unsupported)
        doc["warnings"].push_back("kicad_unsupported_node: " + kind + " (x" +
                                  std::to_string(count) + ")");
    doc["lib_cache"] = lib_cache;
    for (const auto& key : object_lists) snapshot_objects(doc[key]);
    return doc;
}


nlohmann::json parse_kicad_sym_library(const std::string& text) {
    const json tree = sexpr_parse(text);
    if (tag(tree) != "kicad_symbol_lib")
        throw std::runtime_error("Expected kicad_symbol_lib root, got '" + tag(tree) + "'");

    json result{{"symbols", json::object()}, {"warnings", json::array()}};
    std::map<std::string, int> unsupported;
    static const std::regex kSubSymbolRe("_\\d+_\\d+$");
    for (std::size_t i = 1; i < tree.size(); ++i) {
        const json& child = tree[i];
        if (!child.is_array()) continue;
        const std::string kind = tag(child);
        if (kind == "symbol") {
            const std::string qualified = child.size() > 1 ? jstr(child[1]) : "";
            if (qualified.empty() || std::regex_search(qualified, kSubSymbolRe)) continue;
            const std::size_t colon = qualified.find(':');
            const std::string library =
                colon == std::string::npos ? "" : qualified.substr(0, colon);
            const std::string name =
                colon == std::string::npos ? qualified : qualified.substr(colon + 1);
            json patched = child;
            patched[1] = name;
            json definition = parse_symbol(patched, library);
            if (!definition.is_null()) {
                const std::string libid = definition["libid"].get<std::string>();
                result["symbols"][libid] = std::move(definition);
            }
        } else if (kind != "version" && kind != "generator" &&
                   kind != "generator_version") {
            ++unsupported[kind.empty() ? "<malformed>" : kind];
        }
    }
    for (const auto& [kind, count] : unsupported)
        result["warnings"].push_back("kicad_sym_unsupported_node: " + kind + " (x" +
                                     std::to_string(count) + ")");
    return result;
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
    doc["kicad"] = root_metadata(tree);

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
    // KiCad 10 stores net names directly on board items instead of requiring
    // a numeric root-level net table. Register those names as well.
    const auto register_named_net = [&](const json& item) {
        const json* net = find(item, "net");
        if (!net || net->size() < 2) return;
        const json& name = net->size() >= 3 ? (*net)[2] : (*net)[1];
        if (!name.is_string()) return;
        if (net->size() == 2) doc["kicad"]["named_nets"] = true;
        const std::string n = name.get<std::string>();
        if (!n.empty() && seen_net_names.insert(n).second)
            net_registry.push_back(json{{"name", n}, {"class", "default"}});
    };
    for (std::size_t i = 1; i < tree.size(); ++i) {
        const json& item = tree[i];
        const std::string kind = tag(item);
        if (kind == "segment" || kind == "arc" || kind == "via" || kind == "zone")
            register_named_net(item);
        else if (kind == "footprint")
            for (const json* pad : findall(item, "pad")) register_named_net(*pad);
    }
    // Item traversal order can change when the writer groups board objects.
    if (doc["kicad"].value("named_nets", false))
        std::sort(net_registry.begin(), net_registry.end(), [](const json& a, const json& b) {
            return a["name"].get<std::string>() < b["name"].get<std::string>();
        });
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
    if (const json* setup = find(tree, "setup")) {
        if (const json* stackup = find(*setup, "stackup")) {
            json layers = json::array();
            int dielectric_index = 0;
            for (const json* node : findall(*stackup, "layer")) {
                if (node->size() < 2) continue;
                const std::string name = jstr((*node)[1]);
                const json* type_node = find(*node, "type");
                const std::string type = type_node && type_node->size() > 1 ? lower(jstr((*type_node)[1])) : "";
                const bool copper = ends_with(name, ".Cu") || type == "copper";
                const bool dielectric = starts_with(name, "dielectric ") || type == "core" || type == "prepreg";
                if (!copper && !dielectric) continue;
                json item{{"id", copper ? name : "diel" + std::to_string(++dielectric_index)},
                          {"type", copper ? "copper" : (type.empty() ? "core" : type)}};
                for (const auto& mapping : std::vector<std::pair<const char*, const char*>>{
                         {"thickness", "thickness"}, {"epsilon_r", "er"}, {"material", "material"},
                         {"loss_tangent", "loss_tangent"}})
                    if (const json* value = find(*node, mapping.first); value && value->size() > 1)
                        item[mapping.second] = (*value)[1];
                layers.push_back(std::move(item));
            }
            if (!layers.empty()) doc["stackup"]["layers"] = std::move(layers);
        }
    }
    doc["kicad"]["stackup_original"] = doc["stackup"];

    const std::vector<std::string> cu_ids = schema::copper_layer_ids(n_cu);
    const std::set<std::string> valid_cu(cu_ids.begin(), cu_ids.end());

    int fp_counter = 0, track_counter = 0, via_counter = 0, zone_counter = 0;
    json outline_shapes = json::array();
    const std::vector<std::string> object_lists{"footprints", "tracks", "vias", "zones"};

    for (std::size_t i = 1; i < tree.size(); ++i) {
        const json& child = tree[i];
        if (!child.is_array()) continue;
        const std::string ct = tag(child);
        std::vector<std::size_t> before;
        for (const auto& key : object_lists) before.push_back(doc[key].size());
        const std::size_t outline_before = outline_shapes.size();

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
                if (const json* net_node = find(fp_child, "net"); net_node && net_node->size() >= 2)
                    pad_net = net_name_of(net_node->size() >= 3 ? (*net_node)[2] : (*net_node)[1]);
                pad["net"] = pad_net;

                json remapped = json::array();
                for (const json& lyr : pad["layers"])
                    remapped.push_back(map_layer(lyr.get<std::string>()));
                pad["layers"] = remapped;
                retain_uuid(pad, fp_child);

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
            for (const auto& mapping : std::vector<std::pair<std::string, std::string>>{
                     {"Chudware.Engineering", "eng_overrides"}, {"Chudware.Fields", "fields"},
                     {"Chudware.LibId", "libid"}, {"Chudware.OnBoard", "on_board"},
                     {"Chudware.LinkedSymbol", "linked_symbol"}}) {
                const auto property = fp_props.find(mapping.first);
                if (property == fp_props.end()) continue;
                json value = json::parse(property->second, nullptr, false);
                if (!value.is_discarded()) doc["footprints"].back()[mapping.second] = std::move(value);
            }
            auto& graphics = doc["footprints"].back()["graphics"];
            std::size_t graphic_index = 0;
            for (const json& part : child) {
                if (!part.is_array() || !fp_graphic_tags().count(tag(part))) continue;
                if (parse_fp_graphic(part).is_null()) continue;
                if (graphic_index < graphics.size()) retain_uuid(graphics[graphic_index++], part);
            }
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
                for (std::size_t k = 1; k < child.size(); ++k) {
                    if (!child[k].is_string()) continue;
                    const std::string kind = lower(child[k].get<std::string>());
                    if (kind == "micro") via_type = "micro";
                    else if (kind == "blind") {
                        bool touches_outer = false;
                        for (const auto& layer : via_layers)
                            if (layer == "F.Cu" || layer == "B.Cu") touches_outer = true;
                        via_type = touches_outer ? "blind" : "buried";
                    }
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

            double min_width = 0.25, thermal_gap = 0.3, thermal_width = 0.3;
            if (const json* width = find(child, "min_thickness"); width && width->size() > 1)
                min_width = f64((*width)[1]);
            std::string fill_style = "solid";
            if (const json* fill_node = find(child, "fill")) {
                if (const json* gap = find(*fill_node, "thermal_gap"); gap && gap->size() > 1)
                    thermal_gap = f64((*gap)[1]);
                if (const json* width = find(*fill_node, "thermal_bridge_width"); width && width->size() > 1)
                    thermal_width = f64((*width)[1]);
                if (find(*fill_node, "hatch")) fill_style = "hatch";
                if (const json* mode = find(*fill_node, "mode"); mode && mode->size() > 1 &&
                    jstr((*mode)[1]) == "hatch") fill_style = "hatch";
            }
            json filled = json::array();
            for (const json* area : findall(child, "filled_polygon")) {
                const json* area_layer = find(*area, "layer");
                if (area_layer && area_layer->size() > 1 && jstr((*area_layer)[1]) != layer) continue;
                json ring = node_points(*area);
                if (ring.size() >= 3) filled.push_back(std::move(ring));
            }

            ++zone_counter;
            doc["zones"].push_back(
                json{{"id", "z" + std::to_string(zone_counter)},
                     {"net", net_name_of(net_num)},
                     {"layer", layer},
                     {"polygon", polygon},
                     {"clearance", clearance},
                     {"min_width", min_width},
                     {"fill", fill_style},
                     {"thermal", {{"gap", thermal_gap}, {"spoke_width", thermal_width}}},
                     {"filled_polys", filled}});
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
        } else if (ct == "gr_rect" || ct == "gr_circle") {
            const json* layer = find(child, "layer");
            const json* start = find(child, ct == "gr_rect" ? "start" : "center");
            const json* end = find(child, "end");
            if (layer && layer->size() > 1 && jstr((*layer)[1]) == "Edge.Cuts" &&
                start && end && start->size() >= 3 && end->size() >= 3) {
                const json a = json::array({round6(f64((*start)[1])), round6(-f64((*start)[2]))});
                const json b = json::array({round6(f64((*end)[1])), round6(-f64((*end)[2]))});
                if (ct == "gr_rect") outline_shapes.push_back(json{{"type", "rect"}, {"p1", a}, {"p2", b}});
                else outline_shapes.push_back(json{{"type", "circle"}, {"center", a},
                    {"radius", std::hypot(f64((*end)[1]) - f64((*start)[1]),
                                           f64((*end)[2]) - f64((*start)[2]))}});
            }
        }
        bool represented = false;
        for (std::size_t k = 0; k < object_lists.size(); ++k) {
            json& list = doc[object_lists[k]];
            if (list.size() > before[k]) {
                retain_uuid(list.back(), child);
                represented = true;
            }
        }
        if (outline_shapes.size() > outline_before) {
            retain_uuid(outline_shapes.back(), child);
            represented = true;
        }
        if (!represented && (starts_with(ct, "gr_") || ct == "dimension" ||
                             ct == "group" || ct == "target")) {
            json object{{"id", "g" + std::to_string(doc["graphics"].size() + 1)},
                        {"type", ct}};
            retain_uuid(object, child);
            doc["graphics"].push_back(std::move(object));
            represented = true;
        }
        if (!represented && ct != "version" && ct != "generator" &&
            ct != "generator_version" && ct != "uuid")
            doc["kicad"]["extra"].push_back(child);
    }

    if (!outline_shapes.empty()) {
        bool closed = outline_shapes.size() >= 3;
        if (outline_shapes.size() == 1) {
            const std::string type = outline_shapes[0].value("type", std::string());
            closed = type == "rect" || type == "circle";
        }
        doc["outline"] = json{{"shapes", outline_shapes}, {"closed", closed}};
    }
    for (const auto& key : object_lists) snapshot_objects(doc[key]);
    snapshot_objects(doc["graphics"]);
    snapshot_objects(doc["outline"]["shapes"]);
    return doc;
}

}  // namespace chudware::ecad::kicad
