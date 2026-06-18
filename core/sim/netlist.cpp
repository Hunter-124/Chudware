// SPICE netlist parser implementation — see netlist.h.
//
// Direct port of engine/sim/spice.py:parse_spice_deck + engine/sim/subckt.py.
// Every structural decision (token splitting on whitespace, value-suffix scaling,
// case handling for keys/keywords vs. preserved names, ground canonicalisation,
// .model/.param/.options, source waveform/AC/DC parsing, comment + continuation
// preprocessing, .subckt collection + recursive X-instance expansion with name
// prefixing + node remapping) mirrors the oracle so the emitted Circuit dict
// matches for the differential harness (tools/cpp_netlist_parity.py).
//
// Float values: Python stores _parse_value(...) results as Python floats; here we
// emit them as JSON doubles. The harness compares numerically with a tiny rel
// tolerance, so exact decimal text need not match.

#include "sim/netlist.h"

#include <algorithm>
#include <cctype>
#include <map>
#include <optional>
#include <regex>
#include <sstream>
#include <string>
#include <vector>

namespace aicad::sim {

using nlohmann::json;

namespace {

// ---------------------------------------------------------------------------
// small string helpers
// ---------------------------------------------------------------------------
std::string lower(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower((unsigned char)c));
    return s;
}
std::string upper(std::string s) {
    for (char& c : s) c = static_cast<char>(std::toupper((unsigned char)c));
    return s;
}
std::string strip(const std::string& s) {
    size_t a = 0, b = s.size();
    while (a < b && std::isspace((unsigned char)s[a])) ++a;
    while (b > a && std::isspace((unsigned char)s[b - 1])) --b;
    return s.substr(a, b - a);
}
std::string rstrip(const std::string& s) {
    size_t b = s.size();
    while (b > 0 && std::isspace((unsigned char)s[b - 1])) --b;
    return s.substr(0, b);
}
// Python str.split() with no args: split on arbitrary whitespace, drop empties.
std::vector<std::string> ws_split(const std::string& s) {
    std::vector<std::string> out;
    std::istringstream iss(s);
    std::string tok;
    while (iss >> tok) out.push_back(tok);
    return out;
}
bool starts_with(const std::string& s, const std::string& p) {
    return s.size() >= p.size() && s.compare(0, p.size(), p) == 0;
}

// ---------------------------------------------------------------------------
// Value-suffix table + parser  (mirrors spice._SUFFIX / _parse_value)
// ---------------------------------------------------------------------------
const std::map<std::string, double>& suffix_table() {
    static const std::map<std::string, double> t = {
        {"meg", 1e6}, {"g", 1e9}, {"k", 1e3}, {"m", 1e-3}, {"u", 1e-6},
        {"n", 1e-9},  {"p", 1e-12}, {"f", 1e-15}, {"mil", 25.4e-6},
    };
    return t;
}

// ^([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)(meg|mil|[gkmunpf])?$  (case-insensitive)
const std::regex& suffix_re() {
    static const std::regex r(
        R"(^([+-]?[0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)(meg|mil|[gkmunpf])?$)",
        std::regex::icase);
    return r;
}

// Returns nullopt on a token that doesn't match (Python raises ValueError).
std::optional<double> parse_value(const std::string& token) {
    std::string t = strip(token);
    std::smatch m;
    if (!std::regex_match(t, m, suffix_re())) return std::nullopt;
    double num = std::stod(m[1].str());
    std::string suf = lower(m[2].matched ? m[2].str() : std::string());
    if (!suf.empty()) {
        auto it = suffix_table().find(suf);
        if (it != suffix_table().end()) num *= it->second;
    }
    return num;
}

// spice._safe_val: parse, defaulting to 0.0 on failure.
double safe_val(const std::string& s) {
    auto v = parse_value(s);
    return v ? *v : 0.0;
}

// spice._gnd: "0"/"gnd" (case-insensitive) -> "0", else the original name.
std::string gnd(const std::string& name) {
    std::string l = lower(name);
    if (l == "0" || l == "gnd") return "0";
    return name;
}

// ---------------------------------------------------------------------------
// k=v matcher: re.match(r"(\w+)=(.+)", tok)  (anchored at start; \w = [A-Za-z0-9_])
// Returns true + fills key/val on match. The regex is NOT case-insensitive in
// Python; the caller decides whether to upper/lower the key.
// ---------------------------------------------------------------------------
bool match_kv(const std::string& tok, std::string& key, std::string& val,
              bool icase = false) {
    auto flags = std::regex::ECMAScript;
    if (icase) flags |= std::regex::icase;
    std::regex re(R"((\w+)=(.+))", flags);
    std::smatch m;
    if (!std::regex_search(tok, m, re) || m.position(0) != 0) return false;
    key = m[1].str();
    val = m[2].str();
    return true;
}

// ---------------------------------------------------------------------------
// _preprocess: strip comments, join continuations, return logical lines.
// ---------------------------------------------------------------------------
std::vector<std::string> preprocess(const std::string& text) {
    // Python text.splitlines(): split on \n, also handles \r\n. Normalize \r\n/\r.
    std::vector<std::string> physical;
    {
        std::string cur;
        for (size_t i = 0; i < text.size(); ++i) {
            char c = text[i];
            if (c == '\n') {
                physical.push_back(cur);
                cur.clear();
            } else if (c == '\r') {
                physical.push_back(cur);
                cur.clear();
                if (i + 1 < text.size() && text[i + 1] == '\n') ++i;
            } else {
                cur.push_back(c);
            }
        }
        if (!cur.empty()) physical.push_back(cur);
    }

    std::vector<std::string> logical;
    bool have_current = false;
    std::string current;

    for (const std::string& raw : physical) {
        // strip inline ; comment: raw.split(";")[0].rstrip()
        std::string line = rstrip(raw.substr(0, raw.find(';')));

        // full-line comment: line.lstrip().startswith("*")
        {
            std::string ls = line;
            size_t a = 0;
            while (a < ls.size() && std::isspace((unsigned char)ls[a])) ++a;
            if (a < ls.size() && ls[a] == '*') continue;
        }

        if (!line.empty() && line[0] == '+') {
            if (!have_current) {
                have_current = true;
                current = "";
            }
            current = current + " " + strip(line.substr(1));
        } else {
            if (have_current) logical.push_back(current);
            have_current = true;
            current = strip(line);
        }
    }
    if (have_current && !current.empty()) logical.push_back(current);

    std::vector<std::string> out;
    for (auto& l : logical)
        if (!l.empty()) out.push_back(l);
    return out;
}

// ---------------------------------------------------------------------------
// _parse_source_params: V/I waveform / AC / DC tail
// ---------------------------------------------------------------------------
// returns the inner of FIRST  KW(...)  occurrence in `full` (regex KW\(([^)]*)\)).
bool paren_inner(const std::string& full, const std::string& kw, std::string& inner) {
    std::regex re(kw + R"(\(([^)]*)\))", std::regex::icase);
    std::smatch m;
    if (std::regex_search(full, m, re)) {
        inner = m[1].str();
        return true;
    }
    return false;
}

json parse_source_params(const std::vector<std::string>& tokens) {
    json params = json::object();
    size_t i = 0;
    auto join_from = [&](size_t k) {
        std::string s;
        for (size_t j = k; j < tokens.size(); ++j) {
            if (j > k) s += " ";
            s += tokens[j];
        }
        return s;
    };
    auto skip_to_paren = [&](size_t from) -> size_t {
        size_t skip = 1;
        for (size_t j = from; j < tokens.size(); ++j) {
            if (tokens[j].find(')') != std::string::npos) {
                skip = j - from + 1;
                break;
            }
        }
        return skip;
    };

    while (i < tokens.size()) {
        std::string tok = upper(tokens[i]);
        if (starts_with(tok, "PULSE(") || tok == "PULSE") {
            std::string full = join_from(i), inner;
            if (paren_inner(full, "PULSE", inner)) {
                std::vector<std::string> vals = ws_split(inner);
                static const char* keys[] = {"v1", "v2", "td", "tr", "tf", "pw", "per"};
                json wf = {{"type", "PULSE"}};
                for (size_t k = 0; k < vals.size() && k < 7; ++k) wf[keys[k]] = safe_val(vals[k]);
                params["waveform"] = wf;
            }
            i += skip_to_paren(i);
        } else if (starts_with(tok, "SIN(") || tok == "SIN") {
            std::string full = join_from(i), inner;
            if (paren_inner(full, "SIN", inner)) {
                std::vector<std::string> vals = ws_split(inner);
                static const char* keys[] = {"vo", "va", "f", "td", "theta"};
                json wf = {{"type", "SIN"}};
                for (size_t k = 0; k < vals.size() && k < 5; ++k) wf[keys[k]] = safe_val(vals[k]);
                params["waveform"] = wf;
            }
            i += skip_to_paren(i);
        } else if (starts_with(tok, "PWL(") || tok == "PWL") {
            std::string full = join_from(i), inner;
            if (paren_inner(full, "PWL", inner)) {
                std::vector<std::string> nums = ws_split(inner);
                json pairs = json::array();
                for (size_t k = 0; k + 1 < nums.size(); k += 2)
                    pairs.push_back(json::array({safe_val(nums[k]), safe_val(nums[k + 1])}));
                params["waveform"] = {{"type", "PWL"}, {"points", pairs}};
            }
            i += skip_to_paren(i);
        } else if (tok == "AC") {
            json ac;
            ac["mag"] = (i + 1 < tokens.size()) ? safe_val(tokens[i + 1]) : 1.0;
            ac["phase"] = (i + 2 < tokens.size()) ? safe_val(tokens[i + 2]) : 0.0;
            params["ac"] = ac;
            i += 3;
        } else if (tok == "DC") {
            params["dc"] = (i + 1 < tokens.size()) ? safe_val(tokens[i + 1]) : 0.0;
            i += 2;
        } else {
            // bare DC value
            auto v = parse_value(tokens[i]);
            if (v) params["dc"] = *v;
            i += 1;
        }
    }
    return params;
}

// ---------------------------------------------------------------------------
// _parse_model_card
// ---------------------------------------------------------------------------
// _MODEL_PARAM_RE = re.compile(r"(\w+)\s*=\s*([^\s,)]+)") .findall(inner)
void parse_model_card(const std::vector<std::string>& tokens, std::string& name,
                      std::string& type_str, json& params) {
    name = tokens[0];
    std::string rest;
    for (size_t k = 1; k < tokens.size(); ++k) {
        if (k > 1) rest += " ";
        rest += tokens[k];
    }
    // re.match(r"(\w+)\s*\(?(.*?)\)?$", rest, re.DOTALL)
    std::smatch m;
    std::regex re(R"((\w+)\s*\(?([\s\S]*?)\)?$)");
    type_str = "";
    params = json::object();
    if (!std::regex_match(rest, m, re)) return;
    type_str = upper(m[1].str());
    std::string inner = m[2].str();
    std::regex pre(R"((\w+)\s*=\s*([^\s,)]+))");
    for (auto it = std::sregex_iterator(inner.begin(), inner.end(), pre);
         it != std::sregex_iterator(); ++it) {
        params[upper((*it)[1].str())] = safe_val((*it)[2].str());
    }
}

// ---------------------------------------------------------------------------
// _parse_behavioral_rhs : ^\s*([VvIi])\s*=\s*(.+)$  (DOTALL)
// ---------------------------------------------------------------------------
bool parse_behavioral_rhs(const std::vector<std::string>& rest, std::string& btype,
                          std::string& expr) {
    std::string tail;
    for (size_t k = 0; k < rest.size(); ++k) {
        if (k) tail += " ";
        tail += rest[k];
    }
    tail = strip(tail);
    std::smatch m;
    std::regex re(R"(^\s*([VvIi])\s*=\s*([\s\S]+)$)");
    if (!std::regex_match(tail, m, re)) {
        btype = "V";
        return false;
    }
    btype = upper(m[1].str());
    expr = strip(m[2].str());
    return !expr.empty();
}

json make_dev(const std::string& kind, const std::string& name,
              const std::vector<std::string>& nodes, const json& value,
              const json& model, const json& params) {
    json d;
    d["kind"] = kind;
    d["name"] = name;
    d["nodes"] = nodes;
    d["value"] = value;
    d["model"] = model;
    d["params"] = params;
    return d;
}

// ---------------------------------------------------------------------------
// _parse_element  — single element card -> Device or null
// ---------------------------------------------------------------------------
json parse_element(const std::vector<std::string>& tokens) {
    if (tokens.empty()) return json(nullptr);
    const std::string& name = tokens[0];
    char kl = static_cast<char>(std::toupper((unsigned char)name[0]));

    auto N = [&](size_t idx) { return gnd(tokens[idx]); };

    if (kl == 'R' || kl == 'C' || kl == 'L') {
        if (tokens.size() < 4) return json(nullptr);
        std::vector<std::string> nodes = {N(1), N(2)};
        json value = nullptr;
        json params = json::object();
        auto v = parse_value(tokens[3]);
        if (v) value = *v;
        for (size_t t = 4; t < tokens.size(); ++t) {
            std::string k, vstr;
            if (match_kv(tokens[t], k, vstr, /*icase*/ true)) {
                auto pv = parse_value(vstr);
                if (pv) params[lower(k)] = *pv;
                else params[lower(k)] = vstr;
            }
        }
        return make_dev(std::string(1, kl), name, nodes, value, nullptr, params);
    }

    if (kl == 'V' || kl == 'I') {
        if (tokens.size() < 3) return json(nullptr);
        std::vector<std::string> nodes = {N(1), N(2)};
        std::vector<std::string> rest(tokens.begin() + 3, tokens.end());
        json value = nullptr;
        if (!rest.empty()) {
            std::string up0 = upper(rest[0]);
            bool is_kw = starts_with(up0, "PULSE") || starts_with(up0, "SIN") ||
                         starts_with(up0, "PWL") || starts_with(up0, "AC") ||
                         starts_with(up0, "DC");
            if (!is_kw) {
                auto v = parse_value(rest[0]);
                if (v) {
                    value = *v;
                    rest.erase(rest.begin());
                }
            }
        }
        json src = parse_source_params(rest);
        if (value.is_null() && src.contains("DC")) {
            value = src["DC"];
            src.erase("DC");
        }
        return make_dev(std::string(1, kl), name, nodes, value, nullptr, src);
    }

    if (kl == 'D') {
        if (tokens.size() < 3) return json(nullptr);
        std::vector<std::string> nodes = {N(1), N(2)};
        json model = (tokens.size() > 3) ? json(tokens[3]) : json(nullptr);
        json params = json::object();
        for (size_t t = 4; t < tokens.size(); ++t) {
            std::string k, vstr;
            if (match_kv(tokens[t], k, vstr)) {
                auto pv = parse_value(vstr);
                if (pv) params[upper(k)] = *pv;
                else params[upper(k)] = vstr;
            }
        }
        return make_dev("D", name, nodes, nullptr, model, params);
    }

    if (kl == 'Q') {
        if (tokens.size() < 4) return json(nullptr);
        std::vector<std::string> nodes = {N(1), N(2), N(3)};
        json model = (tokens.size() > 4) ? json(tokens[4]) : json(nullptr);
        return make_dev("Q", name, nodes, nullptr, model, json::object());
    }

    if (kl == 'M') {
        if (tokens.size() < 6) return json(nullptr);
        std::vector<std::string> nodes = {N(1), N(2), N(3), N(4)};
        json model = (tokens.size() > 5) ? json(tokens[5]) : json(nullptr);
        json params = json::object();
        for (size_t t = 6; t < tokens.size(); ++t) {
            std::string k, vstr;
            if (match_kv(tokens[t], k, vstr)) {
                auto pv = parse_value(vstr);
                if (pv) params[upper(k)] = *pv;
                else params[upper(k)] = vstr;
            }
        }
        return make_dev("M", name, nodes, nullptr, model, params);
    }

    if (kl == 'E' || kl == 'G') {
        if (tokens.size() < 6) return json(nullptr);
        std::vector<std::string> nodes = {N(1), N(2), N(3), N(4)};
        double gain = (tokens.size() > 5) ? safe_val(tokens[5]) : 1.0;
        return make_dev(std::string(1, kl), name, nodes, nullptr, nullptr,
                        json{{"gain", gain}});
    }

    if (kl == 'F' || kl == 'H') {
        if (tokens.size() < 5) return json(nullptr);
        std::vector<std::string> nodes = {N(1), N(2)};
        std::string ctrl = tokens[3];
        double gain = (tokens.size() > 4) ? safe_val(tokens[4]) : 1.0;
        return make_dev(std::string(1, kl), name, nodes, nullptr, nullptr,
                        json{{"gain", gain}, {"ctrl", ctrl}});
    }

    if (kl == 'K') {
        if (tokens.size() < 4) return json(nullptr);
        std::string l1 = tokens[1], l2 = tokens[2];
        double k = safe_val(tokens[3]);
        return make_dev("K", name, std::vector<std::string>{}, k, nullptr,
                        json{{"inductors", json::array({l1, l2})}, {"k", k}});
    }

    if (kl == 'B') {
        if (tokens.size() < 4) return json(nullptr);
        std::vector<std::string> nodes = {N(1), N(2)};
        std::vector<std::string> rest(tokens.begin() + 3, tokens.end());
        std::string btype, expr;
        if (!parse_behavioral_rhs(rest, btype, expr)) return json(nullptr);
        return make_dev("B", name, nodes, nullptr, nullptr,
                        json{{"btype", btype}, {"expr", expr}});
    }

    return json(nullptr);  // unknown element letter -> skip
}

// ===========================================================================
// subckt.py port
// ===========================================================================
struct SubcktDef {
    std::string name;
    std::vector<std::string> ports;
    std::map<std::string, std::string> params;  // lower keys
    std::vector<std::string> lines;
};

// _parse_subckt_header: tokens after ".subckt" -> (name, ports, params)
// k=v matched with ^(\w+)=(.+)$ (full anchored); params lower-keyed.
void parse_subckt_header(const std::vector<std::string>& toks, std::string& name,
                         std::vector<std::string>& ports,
                         std::map<std::string, std::string>& params) {
    name = toks[0];
    ports.clear();
    params.clear();
    std::regex re(R"(^(\w+)=(.+)$)");
    for (size_t k = 1; k < toks.size(); ++k) {
        std::smatch m;
        if (std::regex_match(toks[k], m, re))
            params[lower(m[1].str())] = m[2].str();
        else
            ports.push_back(toks[k]);
    }
}

// collect_subckts: split into (defs keyed by UPPER name, remaining top lines).
void collect_subckts(const std::vector<std::string>& lines,
                     std::map<std::string, SubcktDef>& subckts,
                     std::vector<std::string>& top) {
    subckts.clear();
    top.clear();
    size_t i = 0, n = lines.size();
    while (i < n) {
        const std::string& line = lines[i];
        std::vector<std::string> toks = ws_split(line);
        if (!toks.empty() && lower(toks[0]) == ".subckt") {
            std::vector<std::string> hdr(toks.begin() + 1, toks.end());
            std::string name;
            std::vector<std::string> ports;
            std::map<std::string, std::string> params;
            parse_subckt_header(hdr, name, ports, params);
            std::vector<std::string> body;
            ++i;
            int depth = 1;
            while (i < n) {
                const std::string& inner = lines[i];
                std::vector<std::string> itoks = ws_split(inner);
                if (!itoks.empty()) {
                    std::string head = lower(itoks[0]);
                    if (head == ".subckt")
                        ++depth;
                    else if (head == ".ends") {
                        --depth;
                        if (depth == 0) break;
                    }
                }
                body.push_back(inner);
                ++i;
            }
            subckts[upper(name)] = SubcktDef{name, ports, params, body};
        } else {
            top.push_back(line);
        }
        ++i;
    }
}

// _parse_instance: Xname n1 n2 ... SUBCKTNAME [k=v ...]
// subckt name = last bare (non k=v) token.
bool parse_instance(const std::vector<std::string>& tokens, std::string& inst,
                    std::vector<std::string>& conn, std::string& subname) {
    inst = tokens[0];
    std::vector<size_t> bare;
    std::regex re(R"(^(\w+)=(.+)$)");
    for (size_t idx = 1; idx < tokens.size(); ++idx) {
        std::smatch m;
        if (std::regex_match(tokens[idx], m, re)) {
            // param token; ignored
        } else {
            bare.push_back(idx);
        }
    }
    if (bare.empty()) return false;  // SubcktError in Python
    size_t sub_idx = bare.back();
    subname = tokens[sub_idx];
    conn.clear();
    for (size_t j = 0; j + 1 < bare.size(); ++j) conn.push_back(tokens[bare[j]]);
    return true;
}

std::string gnd_or(const std::string& name) {
    std::string l = lower(name);
    return (l == "0" || l == "gnd") ? "0" : name;
}

// _remap_node: ports->conn, ground->"0", else "<inst>.<node>" (cached in local).
std::string remap_node(const std::string& node,
                       const std::map<std::string, std::string>& port_map,
                       const std::string& inst,
                       std::map<std::string, std::string>& local) {
    auto pit = port_map.find(node);
    if (pit != port_map.end()) return pit->second;
    std::string l = lower(node);
    if (l == "0" || l == "gnd") return "0";
    auto lit = local.find(node);
    if (lit == local.end()) {
        std::string mapped = inst + "." + node;
        local[node] = mapped;
        return mapped;
    }
    return lit->second;
}

// _NODE_COUNT
int node_count(char letter) {
    switch (letter) {
        case 'R': case 'C': case 'L': case 'V': case 'I': case 'D': case 'F':
        case 'H':
            return 2;
        case 'Q':
            return 3;
        case 'M': case 'E': case 'G': case 'S':
            return 4;
        default:
            return -1;
    }
}

// _remap_element_line: new_name = "<name>:<inst>"; remap first nc node tokens;
// for F/H, the control source (rest[0]) is also suffixed ":<inst>".
bool remap_element_line(const std::vector<std::string>& toks,
                        const std::map<std::string, std::string>& port_map,
                        const std::string& inst,
                        std::map<std::string, std::string>& local,
                        std::vector<std::string>& out) {
    const std::string& name = toks[0];
    char letter = static_cast<char>(std::toupper((unsigned char)name[0]));
    int nc = node_count(letter);
    if (nc < 0) return false;
    std::string new_name = name + ":" + inst;
    std::vector<std::string> nodes;
    for (int k = 0; k < nc && (size_t)(1 + k) < toks.size(); ++k)
        nodes.push_back(remap_node(toks[1 + k], port_map, inst, local));
    std::vector<std::string> rest(toks.begin() + std::min((size_t)(1 + nc), toks.size()),
                                  toks.end());
    if ((letter == 'F' || letter == 'H') && !rest.empty())
        rest[0] = rest[0] + ":" + inst;
    out.clear();
    out.push_back(new_name);
    for (auto& nd : nodes) out.push_back(nd);
    for (auto& r : rest) out.push_back(r);
    return true;
}

// _expand_body (recursive). Appends devices.
void expand_body(const SubcktDef& sub, const std::string& inst,
                 const std::map<std::string, std::string>& port_map,
                 std::map<std::string, std::string>& local,
                 const std::map<std::string, SubcktDef>& subckts,
                 std::vector<json>& devices, int depth) {
    if (depth > 50) return;  // Python raises SubcktError; harness decks never hit this
    for (const std::string& line : sub.lines) {
        std::vector<std::string> toks = ws_split(line);
        if (toks.empty()) continue;
        const std::string& head = toks[0];
        if (head[0] == '*') continue;
        if (head[0] == '.') continue;
        if (std::toupper((unsigned char)head[0]) == 'X') {
            std::string ninst, nsubname;
            std::vector<std::string> nconn;
            if (!parse_instance(toks, ninst, nconn, nsubname)) continue;
            auto cit = subckts.find(upper(nsubname));
            if (cit == subckts.end()) continue;
            const SubcktDef& child = cit->second;
            if (nconn.size() != child.ports.size()) continue;
            std::vector<std::string> mapped_conn;
            for (auto& c : nconn) mapped_conn.push_back(remap_node(c, port_map, inst, local));
            std::map<std::string, std::string> child_port_map;
            for (size_t k = 0; k < child.ports.size(); ++k)
                child_port_map[child.ports[k]] = mapped_conn[k];
            std::map<std::string, std::string> child_local;
            expand_body(child, inst + "." + ninst, child_port_map, child_local,
                        subckts, devices, depth + 1);
            continue;
        }
        std::vector<std::string> remapped;
        if (!remap_element_line(toks, port_map, inst, local, remapped)) continue;
        json dev = parse_element(remapped);
        if (!dev.is_null()) devices.push_back(dev);
    }
}

// expand_instances over top-level X lines.
void expand_instances(const std::vector<std::string>& top_lines,
                      const std::map<std::string, SubcktDef>& subckts,
                      std::vector<json>& devices) {
    for (const std::string& line : top_lines) {
        std::vector<std::string> toks = ws_split(line);
        if (toks.empty()) continue;
        if (toks[0][0] == '.' || toks[0][0] == '*') continue;
        if (std::toupper((unsigned char)toks[0][0]) != 'X') continue;
        std::string inst, subname;
        std::vector<std::string> conn;
        if (!parse_instance(toks, inst, conn, subname)) continue;
        auto sit = subckts.find(upper(subname));
        if (sit == subckts.end()) continue;
        const SubcktDef& sub = sit->second;
        if (conn.size() != sub.ports.size()) continue;
        std::map<std::string, std::string> port_map;
        for (size_t k = 0; k < sub.ports.size(); ++k) port_map[sub.ports[k]] = gnd_or(conn[k]);
        std::map<std::string, std::string> local;
        expand_body(sub, inst, port_map, local, subckts, devices, 0);
    }
}

// Built-in subckts (OPAMP, NE555) — parsed once.
const std::map<std::string, SubcktDef>& builtin_subckts() {
    static std::map<std::string, SubcktDef> lib = [] {
        const char* OPAMP =
            ".subckt OPAMP inp inn out\n"
            "Rin inp inn 1e12\n"
            "Egain ope 0 inp inn 200000\n"
            "Rp ope opi 1k\n"
            "Cp opi 0 15.915u\n"
            "Eout outb 0 opi 0 1\n"
            "Rout outb out 10\n"
            ".ends OPAMP\n";
        const char* NE555 =
            ".subckt NE555 gnd trig out rst cv thr dis vcc\n"
            "Gva 0 vth vcc 0 0.3333m\n"
            "Gvq 0 vth qn 0 0.3333m\n"
            "Rvth vth 0 1k\n"
            "Ecmp cmpr 0 vth thr 200\n"
            "Rcmp cmpr q 10k\n"
            "Dqh q vcc Dclamp\n"
            "Dql 0 q Dclamp\n"
            "Rq q 0 100k\n"
            "Cq q 0 47n\n"
            "Eqn qn 0 q 0 1\n"
            "Eout outraw 0 q 0 1\n"
            "Rout outraw out 10\n"
            "Eqlowr qlowr 0 vcc q 1\n"
            "Rqlow qlowr qlow 10k\n"
            "Dqlowh qlow vcc Dclamp\n"
            "Dqlowl 0 qlow Dclamp\n"
            "Rqlowl qlow 0 100k\n"
            "Gdis dis 0 qlow 0 0.3m\n"
            "Rdis dis 0 1e9\n"
            "Ddisl 0 dis Dclamp\n"
            ".ends NE555\n";
        std::map<std::string, SubcktDef> out;
        for (const char* frag : {OPAMP, NE555}) {
            std::map<std::string, SubcktDef> defs;
            std::vector<std::string> tp;
            collect_subckts(preprocess(frag), defs, tp);
            for (auto& kv : defs) out[kv.first] = kv.second;
        }
        return out;
    }();
    return lib;
}

// BUILTIN_MODELS that the built-in subckts reference.
void add_builtin_models(json& models) {
    if (!models.contains("Dclamp"))
        models["Dclamp"] = json{{"type", "D"}, {"IS", 1e-9}, {"N", 1.0}, {"RS", 10.0}};
}

// _make_circuit: nodes start with "0", then dedup in device order.
json make_circuit(const std::vector<json>& devices, const json& models,
                  const json& params) {
    json node_set = json::array();
    std::vector<std::string> seen;
    auto have = [&](const std::string& s) {
        for (auto& x : seen)
            if (x == s) return true;
        return false;
    };
    node_set.push_back("0");
    seen.push_back("0");
    for (const auto& dev : devices) {
        for (const auto& nd : dev["nodes"]) {
            std::string n = nd.get<std::string>();
            if (!have(n)) {
                node_set.push_back(n);
                seen.push_back(n);
            }
        }
    }
    json c;
    c["nodes"] = node_set;
    c["devices"] = devices;
    c["models"] = models;
    c["params"] = params;
    return c;
}

}  // namespace

// ---------------------------------------------------------------------------
// PUBLIC: parse_netlist  (== spice.parse_spice: parse_spice_deck, drop study)
// ---------------------------------------------------------------------------
json parse_netlist(const std::string& text) {
    std::vector<std::string> raw_lines = preprocess(text);

    std::map<std::string, SubcktDef> deck_subckts;
    std::vector<std::string> lines;
    collect_subckts(raw_lines, deck_subckts, lines);

    // subckts = builtins, then deck defs override
    std::map<std::string, SubcktDef> subckts = builtin_subckts();
    for (auto& kv : deck_subckts) subckts[kv.first] = kv.second;

    std::vector<json> devices;
    json models = json::object();
    json global_params = json::object();
    global_params["TEMP"] = 27;  // int 27, matching Python's literal
    std::vector<std::string> x_lines;

    for (const std::string& line : lines) {
        std::vector<std::string> tokens = ws_split(line);
        if (tokens.empty()) continue;
        std::string first = upper(tokens[0]);

        if (first == ".TITLE") continue;
        if (first == ".TRAN" || first == ".DC" || first == ".AC" || first == ".OP")
            continue;  // analyses -> study (discarded here)

        if (first == ".MODEL") {
            if (tokens.size() >= 3) {
                std::vector<std::string> rest(tokens.begin() + 1, tokens.end());
                std::string name, type_str;
                json mparams;
                parse_model_card(rest, name, type_str, mparams);
                json entry = json::object();
                entry["type"] = type_str;
                for (auto it = mparams.begin(); it != mparams.end(); ++it)
                    entry[it.key()] = it.value();
                models[name] = entry;
            }
            continue;
        }

        if (first == ".PARAM") {
            for (size_t t = 1; t < tokens.size(); ++t) {
                std::string k, vstr;
                if (match_kv(tokens[t], k, vstr)) {
                    auto pv = parse_value(vstr);
                    if (pv) global_params[upper(k)] = *pv;
                    else global_params[upper(k)] = vstr;
                }
            }
            continue;
        }

        if (first == ".OPTIONS") {
            json options = json::object();
            for (size_t t = 1; t < tokens.size(); ++t) {
                std::string k, vstr;
                if (match_kv(tokens[t], k, vstr)) {
                    std::string key = lower(k);
                    auto pv = parse_value(vstr);
                    if (pv) options[key] = *pv;
                    else options[key] = vstr;
                }
            }
            if (options.contains("temp")) global_params["TEMP"] = options["temp"];
            continue;
        }

        if (first == ".PROBE" || first == ".PRINT" || first == ".MEASURE")
            continue;  // probes -> study (discarded here)

        if (first == ".END" || first == ".ENDS" || first == ".SUBCKT") continue;
        if (!first.empty() && first[0] == '.') continue;

        if (std::toupper((unsigned char)tokens[0][0]) == 'X') {
            x_lines.push_back(line);
            continue;
        }

        json dev = parse_element(tokens);
        if (!dev.is_null()) devices.push_back(dev);
    }

    if (!x_lines.empty()) {
        expand_instances(x_lines, subckts, devices);
        add_builtin_models(models);
    }

    return make_circuit(devices, models, global_params);
}

// ---------------------------------------------------------------------------
// PUBLIC: netlist_to_spice_circuit  (== spice.netlist_to_spice_circuit)
//
// Maps a schematic netlist dict -> (Circuit, warnings). Mirrors the Python oracle
// exactly for components carrying an inline "sim" mapping; the SYMBOLS-library
// fallback (resolve sim by libid) is intentionally NOT reproduced (the C++ core
// does not port the starter SYMBOL library) — such components are skipped with a
// "no sim model — skipped" warning, EXACTLY as the oracle does for an unknown libid.
// ---------------------------------------------------------------------------
namespace {

// spice._model_to_kind: map a sim.model string to a SPICE kind letter.
std::string model_to_kind(const std::string& model_name, const std::string& ref) {
    static const std::map<std::string, std::string> kmap = {
        {"R", "R"}, {"C", "C"}, {"L", "L"}, {"V", "V"}, {"I", "I"}, {"D", "D"},
        {"NPN", "Q"}, {"PNP", "Q"}, {"NMOS", "M"}, {"PMOS", "M"},
        {"OPAMP", "OPAMP"}, {"S", "S"},
    };
    auto it = kmap.find(model_name);
    if (it != kmap.end()) return it->second;
    char letter = ref.empty() ? 'X' : (char)std::toupper((unsigned char)ref[0]);
    static const std::string spice_letters = "RCLVIDQMEGFH";
    if (spice_letters.find(letter) != std::string::npos) return std::string(1, letter);
    return "X";
}

// ---------------------------------------------------------------------------
// Starter SYMBOL sim-library port  (engine/ecad/library/starter.py SYMBOLS[].sim)
//
// The Python oracle, when a component carries no inline "sim", falls back to the
// starter symbol library: it reads SYMBOLS[libid]["sim"] for the SPICE model spec
// and builds a pin-name->pin-number map from SYMBOLS[libid]["pins"] (used when a
// sim.pin_order entry is a pin NAME rather than a number-keyed netlist pin).
//
// That table is reproduced here as a self-contained map so aicad_sim stays free of
// any dependency on aicad_ecad (aicad_ecad already depends on aicad_sim — adding
// the reverse edge would be a cycle). Only the two fields the resolver consumes are
// ported: the `sim` dict and the pin name->number map. Every SYMBOLS entry that has
// a `sim` field is covered (R/C/C_pol/L/D/LED/Zener/Q_NPN/Q_PNP/NMOS/PMOS/OPAMP_dual/
// NE555/power-rails/V_source/I_source/Switch).
struct SymbolSim {
    json sim;                                       // SYMBOLS[libid]["sim"]
    std::vector<std::pair<std::string, std::string>> name_to_num;  // pin name -> number
};

const std::map<std::string, SymbolSim>& symbol_sim_table() {
    static const std::map<std::string, SymbolSim> t = [] {
        std::map<std::string, SymbolSim> m;
        // Two-terminal passives: pins numbered "1"/"2", both named "~".
        json rc_order = json::array({"1", "2"});
        m["Device:R"]     = {json{{"model", "R"}, {"pin_order", rc_order}}, {{"~", "2"}}};
        m["Device:C"]     = {json{{"model", "C"}, {"pin_order", rc_order}}, {{"~", "2"}}};
        m["Device:C_pol"] = {json{{"model", "C"}, {"pin_order", rc_order}}, {{"+", "1"}, {"-", "2"}}};
        m["Device:L"]     = {json{{"model", "L"}, {"pin_order", rc_order}}, {{"~", "2"}}};
        // Diodes: pins numbered "K"/"A" (number == name), pin_order [A, K].
        json d_order = json::array({"A", "K"});
        m["Device:D"]     = {json{{"model", "D"}, {"pin_order", d_order}}, {{"K", "K"}, {"A", "A"}}};
        m["Device:LED"]   = {json{{"model", "D"}, {"pin_order", d_order}}, {{"K", "K"}, {"A", "A"}}};
        m["Device:Zener"] = {json{{"model", "D"}, {"pin_order", d_order}}, {{"K", "K"}, {"A", "A"}}};
        // BJTs: pins numbered "B"/"C"/"E" (number == name), pin_order [B, C, E].
        json q_order = json::array({"B", "C", "E"});
        m["Device:Q_NPN"] = {json{{"model", "NPN"}, {"pin_order", q_order}},
                             {{"B", "B"}, {"C", "C"}, {"E", "E"}}};
        m["Device:Q_PNP"] = {json{{"model", "PNP"}, {"pin_order", q_order}},
                             {{"B", "B"}, {"C", "C"}, {"E", "E"}}};
        // MOSFETs: pins numbered "G"/"D"/"S" (number == name), pin_order [G, D, S].
        json fet_order = json::array({"G", "D", "S"});
        m["Device:NMOS"] = {json{{"model", "NMOS"}, {"pin_order", fet_order}},
                            {{"G", "G"}, {"D", "D"}, {"S", "S"}}};
        m["Device:PMOS"] = {json{{"model", "PMOS"}, {"pin_order", fet_order}},
                            {{"G", "G"}, {"D", "D"}, {"S", "S"}}};
        // Dual op-amp: number-keyed pins (1,2,3,5,6,7,8,4) with NAMEd pin_order.
        // name->number from the symbol's pins (later pins win on duplicate names,
        // mirroring Python's dict build order: unit2 IN-/IN+/OUT, then V+/V-).
        m["Amplifier_Operational:OPAMP_dual"] = {
            json{{"model", "OPAMP"},
                 {"pin_order", json::array({"IN+", "IN-", "V+", "V-", "OUT"})}},
            {{"IN-", "5"}, {"IN+", "6"}, {"OUT", "7"}, {"V+", "8"}, {"V-", "4"}}};
        // NE555 SUBCKT: number-keyed pins 1..8 with NAMEd pin_order.
        m["Timer:NE555"] = {
            json{{"model", "SUBCKT"},
                 {"subckt", "NE555"},
                 {"pin_order", json::array({"GND", "TRIG", "OUT", "RST", "CV", "THR", "DIS", "VCC"})}},
            {{"GND", "1"}, {"TRIG", "2"}, {"OUT", "3"}, {"RST", "4"},
             {"CV", "5"}, {"THR", "6"}, {"DIS", "7"}, {"VCC", "8"}}};
        // Power rails: single pin "1"; sim has only a model marker (skipped silently).
        m["power:GND"]  = {json{{"model", "GND"}},  {{"GND", "1"}}};
        m["power:VCC"]  = {json{{"model", "VCC"}},  {{"VCC", "1"}}};
        m["power:+5V"]  = {json{{"model", "+5V"}},  {{"+5V", "1"}}};
        m["power:+3V3"] = {json{{"model", "+3V3"}}, {{"+3V3", "1"}}};
        m["power:-5V"]  = {json{{"model", "-5V"}},  {{"-5V", "1"}}};
        // Sources: number-keyed pins "+"/"-" (number == name), pin_order [+, -].
        json src_order = json::array({"+", "-"});
        m["Simulation_SPICE:V_source"] = {json{{"model", "V"}, {"pin_order", src_order}},
                                          {{"+", "+"}, {"-", "-"}}};
        m["Simulation_SPICE:I_source"] = {json{{"model", "I"}, {"pin_order", src_order}},
                                          {{"+", "+"}, {"-", "-"}}};
        // SPST switch: pins "1"/"2", names A/B, pin_order [1, 2].
        m["Device:Switch"] = {json{{"model", "S"}, {"pin_order", rc_order}},
                              {{"A", "1"}, {"B", "2"}}};
        return m;
    }();
    return t;
}

// spice._resolve_subckt_name: pick the built-in subckt name for a SUBCKT/OPAMP part.
std::string resolve_subckt_name(const std::string& model_name, const json& sim) {
    if (model_name == "OPAMP") return "OPAMP";
    if (sim.contains("subckt") && sim["subckt"].is_string() &&
        !sim["subckt"].get<std::string>().empty())
        return sim["subckt"].get<std::string>();
    return model_name;
}

// spice._map_subckt_nodes: map a component's ordered nodes onto a subckt's ports.
// Returns true + fills `out` on success; false (with a warning) on a mismatch.
bool map_subckt_nodes(const std::string& ref, const std::string& model_name,
                      const std::vector<std::string>& node_list,
                      const std::vector<std::string>& ports,
                      std::vector<std::string>& out, std::vector<std::string>& warn) {
    if (node_list.size() == ports.size()) {
        out = node_list;
        return true;
    }
    if (model_name == "OPAMP" && node_list.size() == 5 && ports.size() == 3) {
        out = {node_list[0], node_list[1], node_list[4]};  // [IN+, IN-, OUT]
        return true;
    }
    std::string ps;
    for (size_t i = 0; i < ports.size(); ++i) {
        if (i) ps += ", ";
        ps += "'" + ports[i] + "'";
    }
    warn.push_back(ref + ": " + std::to_string(node_list.size()) +
                   " nodes do not match subckt ports [" + ps + "] — skipped");
    return false;
}

}  // namespace

json netlist_to_spice_circuit(const json& netlist, const json& sim_params) {
    std::vector<std::string> warn_msgs;
    std::vector<json> devices;
    json models = json::object();
    json params = json::object();
    params["TEMP"] = 27;  // int 27, matching Python's literal
    if (sim_params.is_object())
        for (auto it = sim_params.begin(); it != sim_params.end(); ++it)
            params[it.key()] = it.value();

    const std::map<std::string, SubcktDef>& subckts = builtin_subckts();
    std::vector<std::string> x_lines;  // X-instance cards, expanded after the loop

    const json& comps = (netlist.is_object() && netlist.contains("components") &&
                         netlist["components"].is_array())
                            ? netlist["components"]
                            : json::array();

    for (const json& comp : comps) {
        std::string ref = comp.value("ref", std::string("?"));
        std::string libid = comp.value("libid", std::string());
        std::string value = comp.value("value", std::string());
        json pins = comp.contains("pins") && comp["pins"].is_object() ? comp["pins"]
                                                                      : json::object();
        bool dnp = comp.value("dnp", false);

        if (dnp) {
            warn_msgs.push_back(ref + ": DNP — skipped");
            continue;
        }

        // Resolve sim dict — prefer one carried on the component, then fall back to the
        // starter SYMBOL library (symbol_sim_table) by libid, EXACTLY as the oracle does
        // (engine/sim/spice.py: comp.get("sim") or SYMBOLS[libid]["sim"]).
        const std::map<std::string, SymbolSim>& sym_tbl = symbol_sim_table();
        auto sym_it = sym_tbl.find(libid);
        json sim = comp.contains("sim") && comp["sim"].is_object() ? comp["sim"] : json(nullptr);
        if (sim.is_null() && sym_it != sym_tbl.end()) sim = sym_it->second.sim;
        if (sim.is_null()) {
            warn_msgs.push_back(ref + " (" + libid + "): no sim model — skipped");
            continue;
        }

        std::string model_name = sim.value("model", std::string());

        // Power / ground symbols — not real devices, skip silently.
        if (model_name == "GND" || model_name == "VCC" || model_name == "+5V" ||
            model_name == "+3V3" || model_name == "-5V")
            continue;

        std::vector<std::string> pin_order;
        if (sim.contains("pin_order") && sim["pin_order"].is_array())
            for (const json& po : sim["pin_order"])
                pin_order.push_back(po.is_string() ? po.get<std::string>() : po.dump());

        // A sim.pin_order entry may be a pin NUMBER (matching the number-keyed netlist
        // pins) OR a pin NAME. The Python oracle builds name->number from the SymbolDef
        // (SYMBOLS[libid]["pins"]); reproduce that from the symbol_sim_table. A component
        // may ALSO carry an optional "pin_name_to_number" override (applied on top, for
        // libids the starter library doesn't know).
        std::map<std::string, std::string> name_to_num;
        if (sym_it != sym_tbl.end())
            for (const auto& kv : sym_it->second.name_to_num) name_to_num[kv.first] = kv.second;
        if (comp.contains("pin_name_to_number") && comp["pin_name_to_number"].is_object())
            for (auto it = comp["pin_name_to_number"].begin();
                 it != comp["pin_name_to_number"].end(); ++it)
                name_to_num[it.key()] = it.value().is_string() ? it.value().get<std::string>()
                                                               : it.value().dump();

        // Build the ordered node list.
        std::vector<std::string> node_list;
        for (const std::string& pname : pin_order) {
            const json* net = pins.contains(pname) ? &pins[pname] : nullptr;
            if (net == nullptr) {
                auto nit = name_to_num.find(pname);
                if (nit != name_to_num.end() && pins.contains(nit->second))
                    net = &pins[nit->second];
            }
            if (net == nullptr) {
                std::string keys;
                bool f = true;
                for (auto it = pins.begin(); it != pins.end(); ++it) {
                    if (!f) keys += ", ";
                    f = false;
                    keys += "'" + it.key() + "'";
                }
                warn_msgs.push_back(ref + ": pin '" + pname +
                                    "' not found in netlist pins [" + keys + "]");
                node_list.push_back("?");
            } else {
                node_list.push_back(gnd(net->is_string() ? net->get<std::string>() : net->dump()));
            }
        }

        // If pin_order is empty, fall back to sorted pin order (pins keys are sorted).
        if (pin_order.empty()) {
            std::vector<std::string> keys;
            for (auto it = pins.begin(); it != pins.end(); ++it) keys.push_back(it.key());
            std::sort(keys.begin(), keys.end());
            for (const std::string& k : keys)
                node_list.push_back(gnd(pins[k].is_string() ? pins[k].get<std::string>()
                                                            : pins[k].dump()));
        }

        // Subcircuit-backed parts -> emit an X instance, expand later.
        if (model_name == "SUBCKT" || model_name == "OPAMP") {
            std::string sub_name = resolve_subckt_name(model_name, sim);
            auto sit = subckts.find(upper(sub_name));
            if (sit == subckts.end()) {
                warn_msgs.push_back(ref + " (" + libid + "): subckt '" + sub_name +
                                    "' not built-in — skipped");
                continue;
            }
            std::vector<std::string> inst_nodes;
            if (!map_subckt_nodes(ref, model_name, node_list, sit->second.ports, inst_nodes,
                                  warn_msgs))
                continue;
            std::string line = "X" + ref;
            for (const std::string& n : inst_nodes) line += " " + n;
            line += " " + sit->second.name;
            x_lines.push_back(line);
            continue;
        }

        // Parse value (best-effort; leave null on failure, matching the oracle).
        json float_value = nullptr;
        if (!value.empty()) {
            auto fv = parse_value(value);
            if (fv) float_value = *fv;
        }

        std::string kind = model_to_kind(model_name, ref);
        devices.push_back(make_dev(kind, ref, node_list, float_value, nullptr, json::object()));
    }

    // Inline X instances (built-in subckts) + pull in the models the built-ins need.
    if (!x_lines.empty()) {
        expand_instances(x_lines, subckts, devices);
        add_builtin_models(models);
    }

    json circuit = make_circuit(devices, models, params);
    json warnings = json::array();
    for (const std::string& w : warn_msgs) warnings.push_back(w);
    return json{{"circuit", circuit}, {"warnings", warnings}};
}

}  // namespace aicad::sim
