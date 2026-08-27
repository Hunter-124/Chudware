#include "ecad/schexport.h"

#include <algorithm>
#include <cctype>
#include <map>
#include <set>
#include <sstream>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

namespace chudware::ecad::fab {
namespace {

using json = nlohmann::json;

std::string json_text(const json& v) {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_null()) return "";
    if (v.is_boolean()) return v.get<bool>() ? "true" : "false";
    return v.dump();
}

std::string object_text(const json& o, const char* key, const std::string& fallback = "") {
    if (!o.is_object()) return fallback;
    auto it = o.find(key);
    return it == o.end() ? fallback : json_text(*it);
}

bool object_bool(const json& o, const char* key, bool fallback) {
    if (!o.is_object()) return fallback;
    auto it = o.find(key);
    return it != o.end() && it->is_boolean() ? it->get<bool>() : fallback;
}

std::string lower_ascii(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

int natural_compare(const std::string& a, const std::string& b) {
    std::size_t ia = 0;
    std::size_t ib = 0;
    while (ia < a.size() && ib < b.size()) {
        const bool da = std::isdigit(static_cast<unsigned char>(a[ia])) != 0;
        const bool db = std::isdigit(static_cast<unsigned char>(b[ib])) != 0;
        if (da && db) {
            const std::size_t a0 = ia;
            const std::size_t b0 = ib;
            while (ia < a.size() && std::isdigit(static_cast<unsigned char>(a[ia]))) ++ia;
            while (ib < b.size() && std::isdigit(static_cast<unsigned char>(b[ib]))) ++ib;
            std::size_t as = a0;
            std::size_t bs = b0;
            while (as < ia && a[as] == '0') ++as;
            while (bs < ib && b[bs] == '0') ++bs;
            const std::size_t alen = ia - as;
            const std::size_t blen = ib - bs;
            if (alen != blen) return alen < blen ? -1 : 1;
            const int digits = a.compare(as, alen, b, bs, blen);
            if (digits != 0) return digits < 0 ? -1 : 1;
            const std::size_t arun = ia - a0;
            const std::size_t brun = ib - b0;
            if (arun != brun) return arun < brun ? -1 : 1;
            continue;
        }

        const unsigned char ca = static_cast<unsigned char>(std::tolower(
            static_cast<unsigned char>(a[ia])));
        const unsigned char cb = static_cast<unsigned char>(std::tolower(
            static_cast<unsigned char>(b[ib])));
        if (ca != cb) return ca < cb ? -1 : 1;
        ++ia;
        ++ib;
    }
    if (ia != a.size() || ib != b.size()) return ia == a.size() ? -1 : 1;
    if (a == b) return 0;
    return a < b ? -1 : 1;
}

bool natural_less(const std::string& a, const std::string& b) {
    return natural_compare(a, b) < 0;
}

std::string sexpr_string(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 2);
    out.push_back('"');
    for (char c : s) {
        if (c == '\\' || c == '"') {
            out.push_back('\\');
            out.push_back(c);
        } else if (c == '\n') {
            out += "\\n";
        } else if (c == '\r') {
            out += "\\r";
        } else if (c == '\t') {
            out += "\\t";
        } else {
            out.push_back(c);
        }
    }
    out.push_back('"');
    return out;
}

std::string csv_field(const std::string& s) {
    const bool quote = s.find(',') != std::string::npos || s.find('"') != std::string::npos ||
                       s.find('\n') != std::string::npos || s.find('\r') != std::string::npos;
    if (!quote) return s;
    std::string out = "\"";
    for (char c : s) {
        if (c == '"') out += "\"\"";
        else out.push_back(c);
    }
    out.push_back('"');
    return out;
}

std::vector<std::pair<std::string, std::string>> sorted_fields(const json& comp) {
    std::vector<std::pair<std::string, std::string>> out;
    if (!comp.is_object()) return out;
    auto fit = comp.find("fields");
    if (fit == comp.end() || !fit->is_object()) return out;
    for (auto it = fit->begin(); it != fit->end(); ++it)
        out.emplace_back(it.key(), json_text(it.value()));
    std::sort(out.begin(), out.end());
    return out;
}

std::string field_ci(const std::vector<std::pair<std::string, std::string>>& fields,
                     const std::string& wanted) {
    for (const auto& [key, value] : fields)
        if (key == wanted) return value;
    const std::string low = lower_ascii(wanted);
    for (const auto& [key, value] : fields)
        if (lower_ascii(key) == low) return value;
    return "";
}

std::string first_ref(const std::string& refs) {
    const std::size_t comma = refs.find(',');
    return refs.substr(0, comma);
}

std::string comment_text(std::string s) {
    for (char& c : s)
        if (c == '\r' || c == '\n' || c == ';') c = ' ';
    return s;
}

struct SimDef {
    json sim;
    std::map<std::string, std::string> name_to_num;
};

const SimDef* symbol_sim_table(const std::string& libid) {
    static const std::map<std::string, SimDef> table = [] {
        std::map<std::string, SimDef> out;
        const json rc = json::array({"1", "2"});
        out["Device:R"] = {json{{"model", "R"}, {"pin_order", rc}}, {{"~", "2"}}};
        out["Device:C"] = {json{{"model", "C"}, {"pin_order", rc}}, {{"~", "2"}}};
        out["Device:C_pol"] = {
            json{{"model", "C"}, {"pin_order", rc}}, {{"+", "1"}, {"-", "2"}}};
        out["Device:L"] = {json{{"model", "L"}, {"pin_order", rc}}, {{"~", "2"}}};

        const json diode = json::array({"A", "K"});
        out["Device:D"] = {
            json{{"model", "D"}, {"pin_order", diode}}, {{"K", "K"}, {"A", "A"}}};
        out["Device:LED"] = {
            json{{"model", "D"},
                 {"pin_order", diode},
                 {"params", json{{"IS", 2.3e-19}, {"N", 2.0}, {"RS", 2.0}}}},
            {{"K", "K"}, {"A", "A"}}};
        out["Device:Zener"] = {
            json{{"model", "D"}, {"pin_order", diode}}, {{"K", "K"}, {"A", "A"}}};

        const json bjt = json::array({"C", "B", "E"});
        out["Device:Q_NPN"] = {
            json{{"model", "NPN"}, {"pin_order", bjt}},
            {{"B", "B"}, {"C", "C"}, {"E", "E"}}};
        out["Device:Q_PNP"] = {
            json{{"model", "PNP"}, {"pin_order", bjt}},
            {{"B", "B"}, {"C", "C"}, {"E", "E"}}};

        const json fet = json::array({"D", "G", "S"});
        out["Device:NMOS"] = {
            json{{"model", "NMOS"}, {"pin_order", fet}},
            {{"G", "G"}, {"D", "D"}, {"S", "S"}}};
        out["Device:PMOS"] = {
            json{{"model", "PMOS"}, {"pin_order", fet}},
            {{"G", "G"}, {"D", "D"}, {"S", "S"}}};

        out["Amplifier_Operational:OPAMP_dual"] = {
            json{{"model", "OPAMP"}, {"pin_order", json::array({"IN+", "IN-", "OUT"})}},
            {{"IN-", "1"}, {"IN+", "2"}, {"OUT", "3"}}};
        out["Timer:NE555"] = {
            json{{"model", "SUBCKT"},
                 {"subckt", "NE555"},
                 {"pin_order", json::array({"GND", "TRIG", "OUT", "RST", "CV", "THR",
                                             "DIS", "VCC"})}},
            {{"GND", "1"}, {"TRIG", "2"}, {"OUT", "3"}, {"RST", "4"},
             {"CV", "5"}, {"THR", "6"}, {"DIS", "7"}, {"VCC", "8"}}};

        out["power:GND"] = {json{{"model", "GND"}}, {{"GND", "1"}}};
        out["power:VCC"] = {json{{"model", "VCC"}}, {{"VCC", "1"}}};
        out["power:+5V"] = {json{{"model", "+5V"}}, {{"+5V", "1"}}};
        out["power:+3V3"] = {json{{"model", "+3V3"}}, {{"+3V3", "1"}}};
        out["power:-5V"] = {json{{"model", "-5V"}}, {{"-5V", "1"}}};

        const json source = json::array({"+", "-"});
        out["Simulation_SPICE:V_source"] = {
            json{{"model", "V"}, {"pin_order", source}}, {{"+", "+"}, {"-", "-"}}};
        out["Simulation_SPICE:I_source"] = {
            json{{"model", "I"}, {"pin_order", source}}, {{"+", "+"}, {"-", "-"}}};
        out["Device:Switch"] = {
            json{{"model", "S"}, {"pin_order", rc}}, {{"A", "1"}, {"B", "2"}}};
        return out;
    }();
    auto it = table.find(libid);
    return it == table.end() ? nullptr : &it->second;
}

void overlay(json& dst, const json& src) {
    if (!src.is_object()) return;
    if (!dst.is_object()) dst = json::object();
    for (auto it = src.begin(); it != src.end(); ++it) dst[it.key()] = it.value();
}

std::string spice_identifier(const std::string& raw, const std::string& fallback) {
    std::string out;
    out.reserve(raw.size());
    for (unsigned char c : raw) {
        if (std::isalnum(c) || c == '_' || c == '$' || c == '.' || c == '+' || c == '-')
            out.push_back(static_cast<char>(c));
        else
            out.push_back('_');
    }
    if (out.empty()) out = fallback;
    if (!out.empty() && out.front() == '.') out.insert(out.begin(), '_');
    return out;
}

std::string upper_ascii(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
    return s;
}

bool is_ground_name(const std::string& net) {
    const std::string u = upper_ascii(net);
    return u == "0" || u == "GND" || u == "GNDA" || u == "VSS";
}

std::string scalar_spice(const json& v) {
    if (v.is_string()) return spice_identifier(v.get<std::string>(), "0");
    if (v.is_number()) return v.dump();
    if (v.is_boolean()) return v.get<bool>() ? "1" : "0";
    return "";
}

std::string parameter_spice(const json& v) {
    if (v.is_string()) {
        std::string s = v.get<std::string>();
        for (char& c : s)
            if (std::isspace(static_cast<unsigned char>(c)) || c == ';') c = '_';
        return s;
    }
    if (v.is_number()) return v.dump();
    if (v.is_boolean()) return v.get<bool>() ? "1" : "0";
    return "";
}

std::string model_param_text(const json& params) {
    if (!params.is_object()) return "";
    std::string out;
    for (auto it = params.begin(); it != params.end(); ++it) {
        const std::string value = parameter_spice(it.value());
        if (value.empty()) continue;
        if (!out.empty()) out += " ";
        out += spice_identifier(it.key(), "P") + "=" + value;
    }
    return out;
}

struct ModelRegistry {
    std::map<std::string, std::string> signature_to_name;
    std::map<std::string, std::string> definitions;
    std::set<std::string> used_names;

    std::string add(const std::string& requested, const std::string& type, const json& params) {
        const std::string ptext = model_param_text(params);
        const std::string signature = upper_ascii(type) + "|" + ptext;
        auto prior = signature_to_name.find(signature);
        if (prior != signature_to_name.end()) return prior->second;

        std::string base = spice_identifier(requested, "CHUDWARE_" + upper_ascii(type));
        if (!std::isalpha(static_cast<unsigned char>(base.front())) && base.front() != '_')
            base = "M_" + base;
        std::string name = base;
        int suffix = 2;
        while (used_names.count(upper_ascii(name))) name = base + "_" + std::to_string(suffix++);
        used_names.insert(upper_ascii(name));
        signature_to_name[signature] = name;
        definitions[name] = ".model " + name + " " + upper_ascii(type) +
                            (ptext.empty() ? "" : "(" + ptext + ")");
        return name;
    }
};

std::string kind_for_model(const std::string& model, const std::string& ref) {
    static const std::map<std::string, std::string> known = {
        {"R", "R"},       {"C", "C"},       {"L", "L"},       {"V", "V"},
        {"I", "I"},       {"D", "D"},       {"NPN", "Q"},     {"PNP", "Q"},
        {"NMOS", "M"},    {"PMOS", "M"},    {"OPAMP", "X"},   {"SUBCKT", "X"},
        {"S", "S"},
    };
    const std::string upper = upper_ascii(model);
    auto it = known.find(upper);
    if (it != known.end()) return it->second;
    if (!ref.empty()) {
        const char c = static_cast<char>(std::toupper(static_cast<unsigned char>(ref.front())));
        if (std::string("RCLVIDQMS").find(c) != std::string::npos) return std::string(1, c);
    }
    return "";
}

std::string unique_element_name(const std::string& kind, const std::string& ref,
                                std::set<std::string>& used) {
    std::string clean = spice_identifier(ref, "unnamed");
    const char want = kind == "X" ? 'X' : kind.front();
    if (clean.empty() || std::toupper(static_cast<unsigned char>(clean.front())) != want)
        clean.insert(clean.begin(), want);
    std::string name = clean;
    int suffix = 2;
    while (used.count(upper_ascii(name))) name = clean + "_" + std::to_string(suffix++);
    used.insert(upper_ascii(name));
    return name;
}

const json* object_child(const json& o, const char* key) {
    if (!o.is_object()) return nullptr;
    auto it = o.find(key);
    return it == o.end() ? nullptr : &*it;
}

std::string join_tokens(const std::vector<std::string>& values) {
    std::string out;
    for (const std::string& value : values) {
        if (!out.empty()) out += " ";
        out += value;
    }
    return out;
}

}  // namespace

std::string write_sch_netlist(const json& sch, const json& netlist) {
    std::vector<std::string> problems;
    if (!sch.is_object()) problems.push_back("schematic document is not an object");
    if (!netlist.is_object()) problems.push_back("netlist is not an object");

    std::ostringstream out;
    out << "(export (version \"E\")\n";
    out << "  (design (source "
        << sexpr_string(sch.is_object() ? object_text(sch, "name", "Untitled") : "Untitled")
        << ") (date \"\") (tool \"chudware\"))\n";
    out << "  (components\n";

    std::vector<const json*> components;
    if (netlist.is_object()) {
        const json* comps = object_child(netlist, "components");
        if (comps == nullptr || !comps->is_array()) {
            problems.push_back("netlist.components is missing or not an array");
        } else {
            for (const json& comp : *comps) {
                if (comp.is_object()) components.push_back(&comp);
                else problems.push_back("netlist.components contains a non-object entry");
            }
        }
    }
    std::sort(components.begin(), components.end(), [](const json* a, const json* b) {
        const std::string ar = object_text(*a, "ref", "?");
        const std::string br = object_text(*b, "ref", "?");
        const int cmp = natural_compare(ar, br);
        if (cmp != 0) return cmp < 0;
        return object_text(*a, "sym") < object_text(*b, "sym");
    });

    for (const json* cp : components) {
        const json& comp = *cp;
        const std::string ref = object_text(comp, "ref", "?");
        const std::string value = object_text(comp, "value");
        const std::string footprint = object_text(comp, "footprint");
        std::string datasheet = object_text(comp, "datasheet");
        const auto fields = sorted_fields(comp);
        if (datasheet.empty()) datasheet = field_ci(fields, "Datasheet");
        const std::string libid = object_text(comp, "libid");
        const std::size_t colon = libid.find(':');
        const std::string lib = colon == std::string::npos ? "" : libid.substr(0, colon);
        const std::string part = colon == std::string::npos ? libid : libid.substr(colon + 1);

        out << "    (comp (ref " << sexpr_string(ref) << ") (value " << sexpr_string(value)
            << ") (footprint " << sexpr_string(footprint) << ") (datasheet "
            << sexpr_string(datasheet) << ")\n";
        out << "      (fields";
        for (const auto& [name, field_value] : fields)
            out << " (field (name " << sexpr_string(name) << ") "
                << sexpr_string(field_value) << ")";
        out << ")\n";
        out << "      (libsource (lib " << sexpr_string(lib) << ") (part "
            << sexpr_string(part) << ")))\n";
    }
    out << "  )\n";
    out << "  (nets\n";

    std::vector<std::string> net_names;
    const json* nets = netlist.is_object() ? object_child(netlist, "nets") : nullptr;
    if (nets == nullptr || !nets->is_object()) {
        problems.push_back("netlist.nets is missing or not an object");
    } else {
        for (auto it = nets->begin(); it != nets->end(); ++it) net_names.push_back(it.key());
        std::sort(net_names.begin(), net_names.end());
    }

    int code = 1;
    for (const std::string& net_name : net_names) {
        const json& members = (*nets)[net_name];
        std::vector<const json*> nodes;
        if (!members.is_array()) {
            problems.push_back("net '" + net_name + "' members are not an array");
        } else {
            for (const json& member : members) {
                if (member.is_object()) nodes.push_back(&member);
                else problems.push_back("net '" + net_name + "' contains a non-object node");
            }
        }
        std::sort(nodes.begin(), nodes.end(), [](const json* a, const json* b) {
            const std::string ar = object_text(*a, "ref", "?");
            const std::string br = object_text(*b, "ref", "?");
            const int rcmp = natural_compare(ar, br);
            if (rcmp != 0) return rcmp < 0;
            const std::string ap = object_text(*a, "pin", "?");
            const std::string bp = object_text(*b, "pin", "?");
            const int pcmp = natural_compare(ap, bp);
            if (pcmp != 0) return pcmp < 0;
            return object_text(*a, "sym") < object_text(*b, "sym");
        });

        out << "    (net (code " << code++ << ") (name " << sexpr_string(net_name) << ")";
        for (const json* np : nodes) {
            out << "\n      (node (ref " << sexpr_string(object_text(*np, "ref", "?"))
                << ") (pin " << sexpr_string(object_text(*np, "pin", "?"))
                << ") (pintype " << sexpr_string(object_text(*np, "type", "unspecified"))
                << "))";
        }
        out << ")\n";
    }
    out << "  )\n";

    std::sort(problems.begin(), problems.end());
    problems.erase(std::unique(problems.begin(), problems.end()), problems.end());
    for (const std::string& problem : problems)
        out << "  (comment " << sexpr_string("malformed input: " + problem) << ")\n";
    out << ")\n";
    return out.str();
}

std::string write_sch_bom(const json& netlist, const std::string& fmt, bool exclude_dnp) {
    struct GroupKey {
        std::string value;
        std::string footprint;
        std::vector<std::pair<std::string, std::string>> fields;
        bool operator<(const GroupKey& other) const {
            return std::tie(value, footprint, fields) <
                   std::tie(other.value, other.footprint, other.fields);
        }
    };
    struct Item {
        std::string ref;
        std::string datasheet;
    };
    struct Row {
        std::string refs;
        int qty = 0;
        std::string value;
        std::string footprint;
        std::string datasheet;
        std::string mpn;
        std::vector<std::pair<std::string, std::string>> fields;
        std::string sort_tie;
    };

    std::vector<std::string> problems;
    std::map<GroupKey, std::vector<Item>> groups;
    const json* components = netlist.is_object() ? object_child(netlist, "components") : nullptr;
    if (!netlist.is_object()) problems.push_back("netlist is not an object");
    if (components == nullptr || !components->is_array()) {
        problems.push_back("netlist.components is missing or not an array");
    } else {
        for (const json& comp : *components) {
            if (!comp.is_object()) {
                problems.push_back("netlist.components contains a non-object entry");
                continue;
            }
            if (!object_bool(comp, "in_bom", true)) continue;
            if (exclude_dnp && object_bool(comp, "dnp", false)) continue;

            GroupKey key{object_text(comp, "value"), object_text(comp, "footprint"),
                         sorted_fields(comp)};
            std::string datasheet = object_text(comp, "datasheet");
            if (datasheet.empty()) datasheet = field_ci(key.fields, "Datasheet");
            groups[key].push_back(Item{object_text(comp, "ref", "?"), datasheet});
        }
    }

    std::vector<Row> rows;
    for (auto& [key, items] : groups) {
        std::sort(items.begin(), items.end(), [](const Item& a, const Item& b) {
            const int cmp = natural_compare(a.ref, b.ref);
            if (cmp != 0) return cmp < 0;
            return a.datasheet < b.datasheet;
        });
        std::string refs;
        for (const Item& item : items) {
            if (!refs.empty()) refs += ",";
            refs += item.ref;
        }

        std::vector<std::pair<std::string, std::string>> extras;
        for (const auto& field : key.fields) {
            const std::string low = lower_ascii(field.first);
            if (low != "mpn" && low != "datasheet" && low != "refs" && low != "qty" &&
                low != "value" && low != "footprint")
                extras.push_back(field);
        }
        std::string tie = key.value + "\n" + key.footprint;
        for (const auto& [name, value] : key.fields) tie += "\n" + name + "=" + value;
        rows.push_back(Row{refs, static_cast<int>(items.size()), key.value, key.footprint,
                           items.empty() ? "" : items.front().datasheet,
                           field_ci(key.fields, "MPN"), extras, tie});
    }
    std::sort(rows.begin(), rows.end(), [](const Row& a, const Row& b) {
        const int cmp = natural_compare(first_ref(a.refs), first_ref(b.refs));
        if (cmp != 0) return cmp < 0;
        return a.sort_tie < b.sort_tie;
    });

    std::set<std::string> extra_set;
    for (const Row& row : rows)
        for (const auto& [name, value] : row.fields) {
            (void)value;
            extra_set.insert(name);
        }
    std::vector<std::string> columns = {
        "Refs", "Qty", "Value", "Footprint", "Datasheet", "MPN"};
    columns.insert(columns.end(), extra_set.begin(), extra_set.end());
    if (!problems.empty()) columns.push_back("Comment");

    auto field_value = [](const Row& row, const std::string& column) {
        for (const auto& [name, value] : row.fields)
            if (name == column) return value;
        return std::string();
    };

    std::sort(problems.begin(), problems.end());
    problems.erase(std::unique(problems.begin(), problems.end()), problems.end());
    std::string problem_text;
    for (const std::string& problem : problems) {
        if (!problem_text.empty()) problem_text += "; ";
        problem_text += "malformed input: " + problem;
    }

    if (fmt == "json") {
        nlohmann::ordered_json result = nlohmann::ordered_json::array();
        for (const Row& row : rows) {
            nlohmann::ordered_json obj = nlohmann::ordered_json::object();
            for (const std::string& column : columns) {
                if (column == "Refs") obj[column] = row.refs;
                else if (column == "Qty") obj[column] = row.qty;
                else if (column == "Value") obj[column] = row.value;
                else if (column == "Footprint") obj[column] = row.footprint;
                else if (column == "Datasheet") obj[column] = row.datasheet;
                else if (column == "MPN") obj[column] = row.mpn;
                else if (column == "Comment") obj[column] = "";
                else obj[column] = field_value(row, column);
            }
            result.push_back(std::move(obj));
        }
        if (!problem_text.empty()) {
            nlohmann::ordered_json obj = nlohmann::ordered_json::object();
            for (const std::string& column : columns) {
                if (column == "Qty") obj[column] = 0;
                else obj[column] = column == "Comment" ? problem_text : "";
            }
            result.push_back(std::move(obj));
        }
        return result.dump(2);
    }

    std::string out;
    for (std::size_t i = 0; i < columns.size(); ++i) {
        if (i) out += ",";
        out += csv_field(columns[i]);
    }
    out += "\n";
    for (const Row& row : rows) {
        for (std::size_t i = 0; i < columns.size(); ++i) {
            if (i) out += ",";
            const std::string& column = columns[i];
            std::string value;
            if (column == "Refs") value = row.refs;
            else if (column == "Qty") value = std::to_string(row.qty);
            else if (column == "Value") value = row.value;
            else if (column == "Footprint") value = row.footprint;
            else if (column == "Datasheet") value = row.datasheet;
            else if (column == "MPN") value = row.mpn;
            else if (column != "Comment") value = field_value(row, column);
            out += csv_field(value);
        }
        out += "\n";
    }
    if (!problem_text.empty()) {
        for (std::size_t i = 0; i < columns.size(); ++i) {
            if (i) out += ",";
            std::string value;
            if (columns[i] == "Qty") value = "0";
            else if (columns[i] == "Comment") value = problem_text;
            out += csv_field(value);
        }
        out += "\n";
    }
    return out;
}

std::string write_spice_deck(const json& sch, const json& netlist) {
    std::vector<std::string> top_problems;
    if (!sch.is_object()) top_problems.push_back("schematic document is not an object");
    if (!netlist.is_object()) top_problems.push_back("netlist is not an object");

    std::map<std::string, const json*> symbols;
    if (sch.is_object()) {
        const json* placed = object_child(sch, "symbols");
        if (placed != nullptr && placed->is_array())
            for (const json& sym : *placed)
                if (sym.is_object()) {
                    const std::string id = object_text(sym, "id");
                    if (!id.empty()) symbols[id] = &sym;
                }
    }

    std::set<std::string> all_nets;
    const json* components_json = netlist.is_object() ? object_child(netlist, "components") : nullptr;
    std::vector<const json*> components;
    if (components_json == nullptr || !components_json->is_array()) {
        top_problems.push_back("netlist.components is missing or not an array");
    } else {
        for (const json& comp : *components_json) {
            if (!comp.is_object()) {
                top_problems.push_back("netlist.components contains a non-object entry");
                continue;
            }
            components.push_back(&comp);
            const json* pins = object_child(comp, "pins");
            if (pins != nullptr && pins->is_object())
                for (auto it = pins->begin(); it != pins->end(); ++it)
                    if (it.value().is_string() && !it.value().get<std::string>().empty())
                        all_nets.insert(it.value().get<std::string>());
        }
    }
    std::sort(components.begin(), components.end(), [](const json* a, const json* b) {
        const std::string ar = object_text(*a, "ref", "?");
        const std::string br = object_text(*b, "ref", "?");
        const int cmp = natural_compare(ar, br);
        if (cmp != 0) return cmp < 0;
        return object_text(*a, "sym") < object_text(*b, "sym");
    });

    std::map<std::string, std::string> net_tokens;
    std::set<std::string> used_net_tokens{"0"};
    for (const std::string& net : all_nets) {
        if (is_ground_name(net)) {
            net_tokens[net] = "0";
            continue;
        }
        const std::string base = spice_identifier(net, "N");
        std::string token = base;
        int suffix = 2;
        while (used_net_tokens.count(upper_ascii(token)))
            token = base + "_" + std::to_string(suffix++);
        used_net_tokens.insert(upper_ascii(token));
        net_tokens[net] = token;
    }

    ModelRegistry models;
    std::set<std::string> used_element_names;
    std::vector<std::string> cards;
    bool need_opamp = false;
    bool need_ne555 = false;

    for (const json* cp : components) {
        const json& comp = *cp;
        const std::string ref = object_text(comp, "ref", "?");
        const std::string libid = object_text(comp, "libid");
        auto skipped = [&](const std::string& why) {
            cards.push_back("* skipped " + comment_text(ref) + " (" + comment_text(libid) + "): " +
                            comment_text(why));
        };

        json sim = json(nullptr);
        std::map<std::string, std::string> name_to_num;
        if (const SimDef* builtin = symbol_sim_table(libid)) {
            sim = builtin->sim;
            name_to_num = builtin->name_to_num;
        }

        if (sch.is_object()) {
            const json* cache = object_child(sch, "lib_cache");
            if (cache != nullptr && cache->is_object()) {
                auto def_it = cache->find(libid);
                if (def_it != cache->end() && def_it->is_object()) {
                    if (const json* def_sim = object_child(*def_it, "sim")) overlay(sim, *def_sim);
                    if (const json* pins = object_child(*def_it, "pins"); pins != nullptr && pins->is_array())
                        for (const json& pin : *pins)
                            if (pin.is_object()) {
                                const std::string number = object_text(pin, "number");
                                const std::string name = object_text(pin, "name");
                                if (!number.empty()) {
                                    name_to_num[number] = number;
                                    if (!name.empty()) name_to_num[name] = number;
                                }
                            }
                }
            }
        }

        std::vector<std::string> symbol_ids;
        const std::string primary = object_text(comp, "sym");
        if (!primary.empty()) symbol_ids.push_back(primary);
        if (const json* syms = object_child(comp, "syms"); syms != nullptr && syms->is_array())
            for (const json& id : *syms)
                if (id.is_string() &&
                    std::find(symbol_ids.begin(), symbol_ids.end(), id.get<std::string>()) ==
                        symbol_ids.end())
                    symbol_ids.push_back(id.get<std::string>());
        std::sort(symbol_ids.begin(), symbol_ids.end(), natural_less);
        for (const std::string& id : symbol_ids) {
            auto sit = symbols.find(id);
            if (sit == symbols.end()) continue;
            const json& sym = *sit->second;
            if (const json* inline_sim = object_child(sym, "sim")) overlay(sim, *inline_sim);
            if (const json* inline_params = object_child(sym, "sim_params")) overlay(sim, *inline_params);
            if (const json* pin_names = object_child(sym, "pin_name_to_number");
                pin_names != nullptr && pin_names->is_object())
                for (auto it = pin_names->begin(); it != pin_names->end(); ++it)
                    name_to_num[it.key()] = json_text(it.value());
        }
        if (const json* inline_sim = object_child(comp, "sim")) overlay(sim, *inline_sim);
        if (const json* inline_params = object_child(comp, "sim_params")) overlay(sim, *inline_params);
        if (const json* pin_names = object_child(comp, "pin_name_to_number");
            pin_names != nullptr && pin_names->is_object())
            for (auto it = pin_names->begin(); it != pin_names->end(); ++it)
                name_to_num[it.key()] = json_text(it.value());

        if (!sim.is_object() || object_text(sim, "model").empty()) {
            skipped("no sim model");
            continue;
        }
        const std::string model = object_text(sim, "model");
        const std::string model_upper = upper_ascii(model);
        if (model_upper == "GND" || model_upper == "VCC" || model_upper == "+5V" ||
            model_upper == "+3V3" || model_upper == "-5V")
            continue;
        const std::string kind = kind_for_model(model, ref);
        if (kind.empty()) {
            skipped("unsupported sim model '" + model + "'");
            continue;
        }

        const json* pins = object_child(comp, "pins");
        if (pins == nullptr || !pins->is_object()) {
            skipped("pins are missing or not an object");
            continue;
        }
        std::vector<std::string> pin_order;
        if (const json* order = object_child(sim, "pin_order"); order != nullptr && order->is_array())
            for (const json& pin : *order) pin_order.push_back(json_text(pin));
        if (pin_order.empty()) {
            for (auto it = pins->begin(); it != pins->end(); ++it) pin_order.push_back(it.key());
            std::sort(pin_order.begin(), pin_order.end(), natural_less);
        }

        std::vector<std::string> nodes;
        std::string bad_pin;
        for (const std::string& requested_pin : pin_order) {
            auto pin_it = pins->find(requested_pin);
            if (pin_it == pins->end()) {
                auto mapped = name_to_num.find(requested_pin);
                if (mapped != name_to_num.end()) pin_it = pins->find(mapped->second);
            }
            if (pin_it == pins->end() || !pin_it->is_string() || pin_it->get<std::string>().empty()) {
                bad_pin = requested_pin;
                break;
            }
            const std::string net = pin_it->get<std::string>();
            auto token = net_tokens.find(net);
            nodes.push_back(token == net_tokens.end() ? spice_identifier(net, "N") : token->second);
        }
        if (!bad_pin.empty()) {
            skipped("pin '" + bad_pin + "' has no resolved net");
            continue;
        }
        if (nodes.empty()) {
            skipped("sim mapping has no pins");
            continue;
        }

        const std::string element = unique_element_name(kind, ref, used_element_names);
        const json* params = object_child(sim, "params");
        const json empty_params = json::object();
        const json& model_params = params != nullptr && params->is_object() ? *params : empty_params;

        if (kind == "R" || kind == "C" || kind == "L") {
            const json* sim_value = object_child(sim, "value");
            const std::string value = sim_value != nullptr && !sim_value->is_null()
                                          ? scalar_spice(*sim_value)
                                          : object_text(comp, "value");
            if (value.empty()) {
                skipped("missing component value");
                continue;
            }
            cards.push_back(element + " " + join_tokens(nodes) + " " + value);
        } else if (kind == "V" || kind == "I") {
            std::vector<std::string> tail;
            if (const json* dc = object_child(sim, "dc")) {
                const std::string value = scalar_spice(*dc);
                if (!value.empty()) tail.push_back("DC " + value);
            } else if (const json* sim_value = object_child(sim, "value")) {
                const std::string token = scalar_spice(*sim_value);
                if (!token.empty()) tail.push_back("DC " + token);
            } else {
                const std::string value = object_text(comp, "value");
                tail.push_back("DC " + (value.empty() ? "0" : value));
            }
            if (const json* waveform = object_child(sim, "waveform");
                waveform != nullptr && waveform->is_object()) {
                const std::string type = upper_ascii(object_text(*waveform, "type", "DC"));
                std::vector<std::string> values;
                const std::vector<std::string> keys =
                    type == "PULSE" ? std::vector<std::string>{"v1", "v2", "td", "tr", "tf", "pw", "per"} :
                    type == "SIN" ? std::vector<std::string>{"vo", "va", "f", "td", "theta"} :
                    std::vector<std::string>{};
                for (const std::string& key : keys)
                    if (const json* value = object_child(*waveform, key.c_str())) {
                        const std::string token = scalar_spice(*value);
                        if (!token.empty()) values.push_back(token);
                    }
                if (!values.empty()) tail.push_back(type + "(" + join_tokens(values) + ")");
            }
            if (const json* ac = object_child(sim, "ac"); ac != nullptr) {
                if (ac->is_object()) {
                    const json* mag = object_child(*ac, "mag");
                    const json* phase = object_child(*ac, "phase");
                    const std::string mag_value = mag == nullptr ? "1" : scalar_spice(*mag);
                    const std::string phase_value = phase == nullptr ? "0" : scalar_spice(*phase);
                    tail.push_back("AC " + mag_value + " " + phase_value);
                } else {
                    const std::string mag = scalar_spice(*ac);
                    if (!mag.empty()) tail.push_back("AC " + mag + " 0");
                }
            }
            cards.push_back(element + " " + join_tokens(nodes) + " " + join_tokens(tail));
        } else if (kind == "D") {
            const std::string requested = model_upper == "D" ? "CHUDWARE_D" : model;
            const std::string model_name = models.add(requested, "D", model_params);
            cards.push_back(element + " " + join_tokens(nodes) + " " + model_name);
        } else if (kind == "Q") {
            if (nodes.size() < 3) {
                skipped("BJT model needs three pins");
                continue;
            }
            const std::string type = model_upper == "PNP" ? "PNP" : "NPN";
            const std::string requested =
                (model_upper == "NPN" || model_upper == "PNP") ? "CHUDWARE_" + type : model;
            const std::string model_name = models.add(requested, type, model_params);
            cards.push_back(element + " " + join_tokens(nodes) + " " + model_name);
        } else if (kind == "M") {
            if (nodes.size() < 3) {
                skipped("MOSFET model needs at least three pins");
                continue;
            }
            if (nodes.size() == 3) nodes.push_back(nodes[2]);
            const std::string type = model_upper == "PMOS" ? "PMOS" : "NMOS";
            const std::string requested =
                (model_upper == "NMOS" || model_upper == "PMOS") ? "CHUDWARE_" + type : model;
            const std::string model_name = models.add(requested, type, model_params);
            cards.push_back(element + " " + join_tokens(nodes) + " " + model_name);
        } else if (kind == "S") {
            if (nodes.size() < 2) {
                skipped("switch model needs two pins");
                continue;
            }
            while (nodes.size() < 4) nodes.push_back("0");
            const std::string model_name = models.add("CHUDWARE_SW", "SW", model_params);
            cards.push_back(element + " " + join_tokens(nodes) + " " + model_name);
        } else if (kind == "X") {
            const std::string subckt = upper_ascii(object_text(sim, "subckt",
                                                               model_upper == "OPAMP" ? "OPAMP" : ""));
            if (subckt == "OPAMP") need_opamp = true;
            else if (subckt == "NE555") need_ne555 = true;
            else {
                skipped("subckt '" + subckt + "' has no self-contained definition");
                continue;
            }
            cards.push_back(element + " " + join_tokens(nodes) + " " + subckt);
        }
    }

    std::ostringstream out;
    out << ".title "
        << comment_text(sch.is_object() ? object_text(sch, "name", "Untitled") : "Untitled")
        << "\n";
    std::sort(top_problems.begin(), top_problems.end());
    top_problems.erase(std::unique(top_problems.begin(), top_problems.end()), top_problems.end());
    for (const std::string& problem : top_problems)
        out << "* malformed input: " << comment_text(problem) << "\n";
    for (const auto& [name, definition] : models.definitions) {
        (void)name;
        out << definition << "\n";
    }
    if (need_ne555)
        out << ".model Dclamp D(IS=1e-9 N=1 RS=10)\n";
    if (!models.definitions.empty() || need_ne555) out << "\n";
    for (const std::string& card : cards) out << card << "\n";

    if (need_opamp) {
        out << "\n.subckt OPAMP inp inn out\n"
               "Rin inp inn 1e12\n"
               "Egain ope 0 inp inn 200000\n"
               "Rp ope opi 1k\n"
               "Cp opi 0 15.915u\n"
               "Eout outb 0 opi 0 1\n"
               "Rout outb out 10\n"
               ".ends OPAMP\n";
    }
    if (need_ne555) {
        out << "\n.subckt NE555 gnd trig out rst cv thr dis vcc\n"
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
    }
    out << ".end\n";
    return out.str();
}

}  // namespace chudware::ecad::fab
