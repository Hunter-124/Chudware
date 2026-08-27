// Electrical Rules Check implementation — see erc.h.
// OCCT-free, deterministic, pure JSON, and advisory for every input.

#include "ecad/erc.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <exception>
#include <map>
#include <set>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include "ecad/schema.h"

namespace chudware::ecad {

using nlohmann::json;

namespace {

constexpr double kPointTolerance = 1e-6;

std::string jstr(const json& v, const std::string& dflt = "") {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number()) return v.dump();
    return dflt;
}

std::string field_string(const json& obj, const std::string& key,
                         const std::string& dflt = "") {
    if (!obj.is_object() || !obj.contains(key)) return dflt;
    return jstr(obj[key], dflt);
}

bool field_bool(const json& obj, const std::string& key, bool dflt) {
    if (!obj.is_object() || !obj.contains(key) || !obj[key].is_boolean()) return dflt;
    return obj[key].get<bool>();
}

int field_int(const json& obj, const std::string& key, int dflt) {
    if (!obj.is_object() || !obj.contains(key) || !obj[key].is_number_integer()) return dflt;
    return obj[key].get<int>();
}


double py_round(double v) {
    const double fl = std::floor(v);
    const double diff = v - fl;
    if (diff < 0.5) return fl;
    if (diff > 0.5) return fl + 1.0;
    return std::fmod(fl, 2.0) == 0.0 ? fl : fl + 1.0;
}

double snap_coord(double v) { return py_round(v * 1e6) / 1e6 + 0.0; }

struct Point {
    double x = 0.0;
    double y = 0.0;

    bool operator==(const Point& other) const { return x == other.x && y == other.y; }
};

struct SheetPoint {
    std::string sheet;
    Point p;

    bool operator<(const SheetPoint& other) const {
        return std::tie(sheet, p.x, p.y) < std::tie(other.sheet, other.p.x, other.p.y);
    }
};

bool read_point(const json& value, Point& out) {
    if (!value.is_array() || value.size() < 2 || !value[0].is_number() ||
        !value[1].is_number())
        return false;
    out.x = snap_coord(value[0].get<double>());
    out.y = snap_coord(value[1].get<double>());
    return true;
}

json point_json(const Point& p) { return json::array({p.x, p.y}); }

json clean_xy(const json& value) {
    Point p;
    return read_point(value, p) ? point_json(p) : json(nullptr);
}

json make_violation(const std::string& rule, const std::string& severity, const json& net,
                    std::vector<std::string> pins, const json& xy, const std::string& message) {
    std::sort(pins.begin(), pins.end());
    pins.erase(std::unique(pins.begin(), pins.end()), pins.end());
    json pins_arr = json::array();
    for (const std::string& pin : pins) pins_arr.push_back(pin);
    const json net_out = net.is_string() ? net : json(nullptr);
    return json{{"rule", rule},
                {"severity", severity},
                {"net", net_out},
                {"pins", pins_arr},
                {"xy", clean_xy(xy)},
                {"message", message}};
}

int drive_level(const std::string& type) {
    if (type == "power_out") return 4;
    if (type == "output") return 3;
    if (type == "bidirectional" || type == "tri_state" || type == "open_collector" ||
        type == "open_emitter")
        return 2;
    if (type == "input" || type == "power_in") return 0;
    return 1;  // passive, free, unspecified, no_connect, and forward-compatible unknowns
}

bool is_strong_driver(const std::string& type) {
    return type == "output" || type == "power_out";
}

struct PairRule {
    std::string rule;
    std::string severity;
};

PairRule pair_rule(const std::string& a, const std::string& b) {
    if (a == "free" || b == "free") return {};
    if (a == "no_connect" || b == "no_connect") return {"pin_type_conflict", "error"};
    if (a == "unspecified" || b == "unspecified")
        return {"pin_type_unspecified", "warning"};

    const bool a_drives = a == "output" || a == "power_out";
    const bool b_drives = b == "output" || b == "power_out";
    const bool a_shared = a == "bidirectional" || a == "tri_state" ||
                          a == "open_collector" || a == "open_emitter";
    const bool b_shared = b == "bidirectional" || b == "tri_state" ||
                          b == "open_collector" || b == "open_emitter";
    if ((a_drives && b_shared) || (b_drives && a_shared))
        return {"pin_type_conflict", "warning"};
    return {};
}

std::string lower_ascii(std::string value) {
    std::transform(value.begin(), value.end(), value.begin(), [](unsigned char c) {
        return static_cast<char>(std::tolower(c));
    });
    return value;
}

bool one_edit_or_transposition(const std::string& lhs, const std::string& rhs) {
    const std::string a = lower_ascii(lhs);
    const std::string b = lower_ascii(rhs);
    if (a == b) return lhs != rhs;
    if (a.size() == b.size()) {
        std::vector<size_t> diff;
        for (size_t i = 0; i < a.size(); ++i)
            if (a[i] != b[i]) diff.push_back(i);
        if (diff.size() == 1) return true;
        return diff.size() == 2 && diff[1] == diff[0] + 1 && a[diff[0]] == b[diff[1]] &&
               a[diff[1]] == b[diff[0]];
    }
    const std::string& shorter = a.size() < b.size() ? a : b;
    const std::string& longer = a.size() < b.size() ? b : a;
    if (longer.size() != shorter.size() + 1) return false;
    size_t i = 0;
    size_t k = 0;
    bool skipped = false;
    while (i < shorter.size() && k < longer.size()) {
        if (shorter[i] == longer[k]) {
            ++i;
            ++k;
        } else if (!skipped) {
            skipped = true;
            ++k;
        } else {
            return false;
        }
    }
    return true;
}

bool point_on_segment(const Point& p, const Point& a, const Point& b) {
    const double abx = b.x - a.x;
    const double aby = b.y - a.y;
    const double apx = p.x - a.x;
    const double apy = p.y - a.y;
    const double length = std::sqrt(abx * abx + aby * aby);
    if (length <= kPointTolerance) return p == a;
    const double cross = abx * apy - aby * apx;
    if (std::fabs(cross) > kPointTolerance * length) return false;
    const double dot = apx * abx + apy * aby;
    return dot >= -kPointTolerance * length &&
           dot <= length * length + kPointTolerance * length;
}

struct WirePath {
    std::string sheet;
    std::vector<Point> pts;
};

std::vector<WirePath> collect_wire_paths(const json& sch) {
    std::vector<const json*> ordered;
    if (sch.is_object() && sch.contains("wires") && sch["wires"].is_array()) {
        for (const json& wire : sch["wires"])
            if (wire.is_object()) ordered.push_back(&wire);
    }
    std::sort(ordered.begin(), ordered.end(), [](const json* a, const json* b) {
        return field_string(*a, "id") < field_string(*b, "id");
    });
    std::vector<WirePath> paths;
    for (const json* wire : ordered) {
        WirePath path;
        path.sheet = field_string(*wire, "sheet");
        if (wire->contains("pts") && (*wire)["pts"].is_array()) {
            for (const json& value : (*wire)["pts"]) {
                Point p;
                if (read_point(value, p)) path.pts.push_back(p);
            }
        }
        paths.push_back(std::move(path));
    }
    return paths;
}

bool point_touches_wire(const std::vector<WirePath>& wires, const std::string& sheet,
                        const Point& p) {
    for (const WirePath& wire : wires) {
        if (wire.sheet != sheet) continue;
        for (size_t i = 0; i + 1 < wire.pts.size(); ++i)
            if (point_on_segment(p, wire.pts[i], wire.pts[i + 1])) return true;
        if (wire.pts.size() == 1 && wire.pts[0] == p) return true;
    }
    return false;
}

struct PinInfo {
    std::string type;
    std::string ref;
    std::string name;
    std::string sym;
    std::string sheet;
    Point p;
    double raw_x = 0.0;
    double raw_y = 0.0;
    bool has_xy = false;
};

void collect_pin_info(const json& sch, const std::set<SheetPoint>& nc_points,
                      std::map<std::string, PinInfo>& pin_info,
                      std::map<std::string, bool>& pin_is_nc,
                      std::map<SheetPoint, std::vector<std::string>>& pins_at_point) {
    const json* cache = nullptr;
    if (sch.is_object() && sch.contains("lib_cache") && sch["lib_cache"].is_object())
        cache = &sch["lib_cache"];
    if (!cache || !sch.contains("symbols") || !sch["symbols"].is_array()) return;

    std::vector<const json*> symbols;
    for (const json& symbol : sch["symbols"])
        if (symbol.is_object()) symbols.push_back(&symbol);
    std::sort(symbols.begin(), symbols.end(), [](const json* a, const json* b) {
        return field_string(*a, "id") < field_string(*b, "id");
    });

    for (const json* symbol : symbols) {
        const std::string libid = field_string(*symbol, "libid");
        if (!cache->contains(libid) || !(*cache)[libid].is_object()) continue;
        const json& def = (*cache)[libid];
        if (!def.contains("pins") || !def["pins"].is_array()) continue;
        const int unit = field_int(*symbol, "unit", 1);
        const std::string sid = field_string(*symbol, "id");
        const std::string sheet = field_string(*symbol, "sheet");
        for (const json& pin : def["pins"]) {
            if (!pin.is_object()) continue;
            const int pin_unit = field_int(pin, "unit", 1);
            if (pin_unit != 0 && pin_unit != unit) continue;
            try {
                const auto world = schema::pin_world(*symbol, pin);
                PinInfo info;
                info.type = field_string(pin, "type", "passive");
                info.ref = field_string(*symbol, "ref");
                info.name = field_string(pin, "name");
                info.sym = sid;
                info.sheet = sheet;
                info.p = {snap_coord(world.first), snap_coord(world.second)};
                info.raw_x = world.first;
                info.raw_y = world.second;
                info.has_xy = true;
                const std::string pinref = sid + "." + field_string(pin, "number");
                pin_info[pinref] = info;
                const SheetPoint key{sheet, info.p};
                pin_is_nc[pinref] = nc_points.count(key) != 0;
                pins_at_point[key].push_back(pinref);
            } catch (...) {
                // A malformed library pin is advisory input, never an ERC exception.
            }
        }
    }
    for (auto& item : pins_at_point) std::sort(item.second.begin(), item.second.end());
}

json pin_xy(const std::map<std::string, PinInfo>& pin_info, const std::string& pinref) {
    const auto it = pin_info.find(pinref);
    return it != pin_info.end() && it->second.has_xy ? point_json(it->second.p) : json(nullptr);
}

json first_pin_xy(const std::map<std::string, PinInfo>& pin_info,
                  const std::vector<std::string>& pinrefs) {
    for (const std::string& pinref : pinrefs) {
        const json xy = pin_xy(pin_info, pinref);
        if (xy.is_array()) return xy;
    }
    return nullptr;
}

std::set<std::string> info_labels(const json& net_info, const std::string& net) {
    std::set<std::string> labels;
    if (!net_info.is_object() || !net_info.contains(net) || !net_info[net].is_object())
        return labels;
    const json& info = net_info[net];
    if (!info.contains("labels") || !info["labels"].is_array()) return labels;
    for (const json& label : info["labels"]) {
        const std::string text = jstr(label);
        if (!text.empty()) labels.insert(text);
    }
    return labels;
}

bool power_port_matches_net(const json& port, const std::string& net,
                            const std::set<std::string>& labels) {
    const std::string name = field_string(port, "net");
    return !name.empty() && (name == net || labels.count(name) != 0);
}

bool is_power_flag(const json& port) {
    const std::string symbol = lower_ascii(field_string(port, "symbol"));
    const std::string libid = lower_ascii(field_string(port, "libid"));
    return symbol == "pwr_flag" || libid == "power:pwr_flag" || libid == "pwr_flag";
}

const json* first_matching_power_port(const json& sch, const std::string& net,
                                      const std::set<std::string>& labels) {
    if (!sch.is_object() || !sch.contains("power") || !sch["power"].is_array()) return nullptr;
    for (const json& port : sch["power"])
        if (port.is_object() && power_port_matches_net(port, net, labels)) return &port;
    return nullptr;
}

bool net_has_power_flag(const json& sch, const std::string& net,
                        const std::set<std::string>& labels) {
    if (!sch.is_object() || !sch.contains("power") || !sch["power"].is_array()) return false;
    for (const json& port : sch["power"])
        if (port.is_object() && power_port_matches_net(port, net, labels) && is_power_flag(port))
            return true;
    return false;
}

bool net_has_named_label(const json& sch, const std::string& net,
                         const std::set<std::string>& labels) {
    if (!labels.empty()) return true;
    if (!sch.is_object() || !sch.contains("labels") || !sch["labels"].is_array()) return false;
    for (const json& label : sch["labels"])
        if (label.is_object() && field_string(label, "text") == net) return true;
    return false;
}

json label_location(const json& sch, const std::set<std::string>& names) {
    if (!sch.is_object() || !sch.contains("labels") || !sch["labels"].is_array()) return nullptr;
    std::vector<const json*> labels;
    for (const json& label : sch["labels"])
        if (label.is_object() && names.count(field_string(label, "text"))) labels.push_back(&label);
    std::sort(labels.begin(), labels.end(), [](const json* a, const json* b) {
        return field_string(*a, "id") < field_string(*b, "id");
    });
    for (const json* label : labels) {
        const json xy = label->contains("pos") ? clean_xy((*label)["pos"]) : json(nullptr);
        if (xy.is_array()) return xy;
    }
    return nullptr;
}

std::string join_strings(const std::vector<std::string>& values) {
    std::string joined;
    for (size_t i = 0; i < values.size(); ++i) {
        if (i) joined += ", ";
        joined += values[i];
    }
    return joined;
}

std::string join_set(const std::set<std::string>& values) {
    return join_strings(std::vector<std::string>(values.begin(), values.end()));
}

bool symbol_is_power(const json& symbol, const json& cache) {
    const std::string libid = field_string(symbol, "libid");
    if (cache.is_object() && cache.contains(libid) && cache[libid].is_object() &&
        field_bool(cache[libid], "power", false))
        return true;
    const std::string lower = lower_ascii(libid);
    return lower.rfind("power:", 0) == 0;
}

json member_xy(const json& member) {
    return member.is_object() && member.contains("xy") ? clean_xy(member["xy"]) : json(nullptr);
}

std::string member_pinref(const json& member) {
    return field_string(member, "sym") + "." + field_string(member, "pin");
}

void sort_violations(json& out) {
    std::stable_sort(out.begin(), out.end(), [](const json& a, const json& b) {
        const std::string ra = field_string(a, "rule");
        const std::string rb = field_string(b, "rule");
        if (ra != rb) return ra < rb;
        const std::string na = a.contains("net") && a["net"].is_string()
                                   ? a["net"].get<std::string>()
                                   : "";
        const std::string nb = b.contains("net") && b["net"].is_string()
                                   ? b["net"].get<std::string>()
                                   : "";
        if (na != nb) return na < nb;
        const json& pa = a["pins"];
        const json& pb = b["pins"];
        const size_t count = std::min(pa.size(), pb.size());
        for (size_t i = 0; i < count; ++i) {
            const std::string p1 = jstr(pa[i]);
            const std::string p2 = jstr(pb[i]);
            if (p1 != p2) return p1 < p2;
        }
        if (pa.size() != pb.size()) return pa.size() < pb.size();
        const bool a_empty = !a["xy"].is_array();
        const bool b_empty = !b["xy"].is_array();
        if (a_empty || b_empty) {
            if (a_empty != b_empty) return a_empty;
            return false;
        }
        for (size_t i = 0; i < 2; ++i) {
            const double p1 = a["xy"][i].get<double>();
            const double p2 = b["xy"][i].get<double>();
            if (p1 != p2) return p1 < p2;
        }
        return false;
    });
}

json run_erc_impl(const json& sch, const json& netlist) {
    json out = json::array();
    if (!schema::is_schematic(sch)) {
        const std::string schema_name =
            sch.is_object() && sch.contains("schema") ? jstr(sch["schema"], "null") : "null";
        out.push_back(make_violation("not_a_schematic", "warning", nullptr, {}, nullptr,
                                     "ERC requires an aicad.sch/1 document (schema=" +
                                         schema_name + ")"));
        return out;
    }

    const json nets = netlist.is_object() && netlist.contains("nets") &&
                              netlist["nets"].is_object()
                          ? netlist["nets"]
                          : json::object();
    const json by_pin = netlist.is_object() && netlist.contains("by_pin") &&
                                netlist["by_pin"].is_object()
                            ? netlist["by_pin"]
                            : json::object();
    const json net_info = netlist.is_object() && netlist.contains("net_info") &&
                                  netlist["net_info"].is_object()
                              ? netlist["net_info"]
                              : json::object();
    const json net_buses = netlist.is_object() && netlist.contains("buses") &&
                                   netlist["buses"].is_object()
                               ? netlist["buses"]
                               : json::object();

    std::set<SheetPoint> nc_points;
    std::vector<const json*> nc_markers;
    if (sch.contains("no_connect") && sch["no_connect"].is_array()) {
        for (const json& marker : sch["no_connect"]) {
            if (!marker.is_object()) continue;
            Point p;
            if (!marker.contains("pos") || !read_point(marker["pos"], p)) continue;
            nc_points.insert({field_string(marker, "sheet"), p});
            nc_markers.push_back(&marker);
        }
    }
    std::sort(nc_markers.begin(), nc_markers.end(), [](const json* a, const json* b) {
        return field_string(*a, "id") < field_string(*b, "id");
    });

    std::map<std::string, PinInfo> pin_info;
    std::map<std::string, bool> pin_is_nc;
    std::map<SheetPoint, std::vector<std::string>> pins_at_point;
    collect_pin_info(sch, nc_points, pin_info, pin_is_nc, pins_at_point);
    const std::vector<WirePath> wires = collect_wire_paths(sch);

    auto net_size = [&](const std::string& net) -> int {
        if (net.empty() || !nets.contains(net) || !nets[net].is_array()) return 0;
        return static_cast<int>(nets[net].size());
    };

    std::map<std::string, json> member_xy_by_pin;
    for (auto net_it = nets.begin(); net_it != nets.end(); ++net_it) {
        if (!net_it.value().is_array()) continue;
        for (const json& member : net_it.value()) {
            if (!member.is_object()) continue;
            const std::string pinref = member_pinref(member);
            const json xy = member_xy(member);
            if (!pinref.empty() && xy.is_array()) member_xy_by_pin[pinref] = xy;
        }
    }

    // Per-pin connectivity and no-connect rules.
    std::vector<std::string> pin_keys;
    for (auto it = by_pin.begin(); it != by_pin.end(); ++it) pin_keys.push_back(it.key());
    std::sort(pin_keys.begin(), pin_keys.end());
    for (const std::string& pinref : pin_keys) {
        const std::string net = by_pin[pinref].is_string() ? by_pin[pinref].get<std::string>() : "";
        const auto info_it = pin_info.find(pinref);
        const std::string type = info_it != pin_info.end() ? info_it->second.type : "passive";
        json xy = pin_xy(pin_info, pinref);
        if (!xy.is_array() && member_xy_by_pin.count(pinref)) xy = member_xy_by_pin[pinref];
        const int size = net_size(net);
        const bool is_nc = pin_is_nc.count(pinref) && pin_is_nc[pinref];

        if ((net.empty() || size <= 1) && type != "no_connect" && !is_nc) {
            out.push_back(make_violation(
                "pin_not_connected", "warning", net.empty() ? json(nullptr) : json(net), {pinref},
                xy, "pin " + pinref + " (" + type + ") is not connected to any net"));
            if (!net.empty() && size == 1)
                out.push_back(make_violation(
                    "floating_net_single_pin", "warning", net, {pinref}, xy,
                    "net " + net + " has a single pin (" + pinref + ") — likely a wiring slip"));
        }

        if (is_nc && info_it != pin_info.end()) {
            const PinInfo& info = info_it->second;
            const SheetPoint key{info.sheet, info.p};
            const std::set<std::string> labels = info_labels(net_info, net);
            bool conductive = size > 1 || point_touches_wire(wires, info.sheet, info.p) ||
                              (pins_at_point.count(key) && pins_at_point[key].size() > 1) ||
                              first_matching_power_port(sch, net, labels) != nullptr ||
                              !labels.empty();
            if (!conductive && sch.contains("labels") && sch["labels"].is_array()) {
                for (const json& label : sch["labels"]) {
                    Point p;
                    if (label.is_object() && field_string(label, "sheet") == info.sheet &&
                        label.contains("pos") && read_point(label["pos"], p) && p == info.p) {
                        conductive = true;
                        break;
                    }
                }
            }
            if (conductive)
                out.push_back(make_violation(
                    "no_connect_connected", "error", net.empty() ? json(nullptr) : json(net),
                    {pinref}, xy,
                    "pin " + pinref +
                        " is flagged no-connect but has a conductive wire, pin, label, or power attachment"));
        }
    }

    // A no-connect marker with no pin beneath it cannot suppress an unconnected-pin warning.
    for (const json* marker : nc_markers) {
        Point p;
        read_point((*marker)["pos"], p);
        const SheetPoint key{field_string(*marker, "sheet"), p};
        if (!pins_at_point.count(key) || pins_at_point[key].empty())
            out.push_back(make_violation(
                "no_connect_dangling", "warning", nullptr, {}, point_json(p),
                "no-connect marker " + field_string(*marker, "id", "<unnamed>") +
                    " does not touch a symbol pin"));
    }

    // Per-net drive, power, pin-matrix, and label rules.
    std::vector<std::string> net_keys;
    for (auto it = nets.begin(); it != nets.end(); ++it) net_keys.push_back(it.key());
    std::sort(net_keys.begin(), net_keys.end());
    for (const std::string& net : net_keys) {
        if (!nets[net].is_array()) continue;
        const json& members = nets[net];
        std::vector<std::string> types;
        std::vector<std::string> pinrefs;
        std::vector<json> member_locations;
        for (const json& member : members) {
            if (!member.is_object()) continue;
            types.push_back(field_string(member, "type", "passive"));
            pinrefs.push_back(member_pinref(member));
            member_locations.push_back(member_xy(member));
        }
        const json net_xy = !member_locations.empty() ? member_locations.front() : json(nullptr);
        const std::set<std::string> labels = info_labels(net_info, net);
        const json* power_port = first_matching_power_port(sch, net, labels);
        const bool named_intentionally = net_has_named_label(sch, net, labels) || power_port;

        std::vector<std::string> strong;
        int max_drive = 0;
        bool any_power_in = false;
        bool local_power_source = false;
        std::vector<std::string> power_inputs;
        for (size_t i = 0; i < types.size(); ++i) {
            max_drive = std::max(max_drive, drive_level(types[i]));
            if (is_strong_driver(types[i])) strong.push_back(pinrefs[i]);
            if (types[i] == "power_out") local_power_source = true;
            if (types[i] == "power_in") {
                any_power_in = true;
                power_inputs.push_back(pinrefs[i]);
            }
        }

        if (strong.size() >= 2) {
            std::sort(strong.begin(), strong.end());
            json strong_xy = first_pin_xy(pin_info, strong);
            if (!strong_xy.is_array()) strong_xy = net_xy;
            out.push_back(make_violation(
                "conflicting_drivers", "error", net, strong, strong_xy,
                "net " + net + " has " + std::to_string(strong.size()) + " strong drivers (" +
                    join_strings(strong) + ") — output-output short"));
        }

        for (size_t i = 0; i < types.size(); ++i) {
            for (size_t k = i + 1; k < types.size(); ++k) {
                const PairRule rule = pair_rule(types[i], types[k]);
                if (rule.severity.empty()) continue;
                json xy = i < member_locations.size() ? member_locations[i] : json(nullptr);
                if (!xy.is_array()) xy = pin_xy(pin_info, pinrefs[i]);
                out.push_back(make_violation(
                    rule.rule, rule.severity, net, {pinrefs[i], pinrefs[k]}, xy,
                    "pins " + pinrefs[i] + " (" + types[i] + ") and " + pinrefs[k] + " (" +
                        types[k] + ") are an incompatible electrical-type combination on net " +
                        net));
            }
        }

        bool has_power_source = local_power_source || net_has_power_flag(sch, net, labels);
        if (net_info.contains(net) && net_info[net].is_object() &&
            net_info[net].contains("has_power_source") &&
            net_info[net]["has_power_source"].is_boolean())
            has_power_source = net_info[net]["has_power_source"].get<bool>();

        if (any_power_in && !has_power_source) {
            json power_xy = first_pin_xy(pin_info, power_inputs);
            if (!power_xy.is_array()) power_xy = net_xy;
            if (!power_xy.is_array() && power_port && power_port->contains("pos"))
                power_xy = (*power_port)["pos"];
            if (power_port) {
                out.push_back(make_violation(
                    "power_flag_needed", "warning", net, power_inputs, power_xy,
                    "supply net " + net +
                        " has power-input pins but no real power-output source; add a PWR_FLAG when an external source intentionally drives it"));
            } else {
                out.push_back(make_violation(
                    "power_not_driven", "error", net, power_inputs, power_xy,
                    "net " + net + " has power-input pins but no power_out pin or PWR_FLAG source"));
            }
        }

        if (pinrefs.size() > 1 && max_drive <= 1 && !named_intentionally)
            out.push_back(make_violation(
                "no_driver", "warning", net, pinrefs, net_xy,
                "net " + net + " has no electrical driver (maximum drive level " +
                    std::to_string(max_drive) + ")"));

        if (net_info.contains(net) && net_info[net].is_object()) {
            std::set<std::string> distinct = info_labels(net_info, net);
            const size_t suffix = net.find('@');
            if (suffix != std::string::npos && distinct.count(net) &&
                distinct.count(net.substr(0, suffix)))
                distinct.erase(net);  // collision-disambiguated display name, not a second label
            if (distinct.size() > 1) {
                json xy = label_location(sch, distinct);
                if (!xy.is_array()) xy = net_xy;
                out.push_back(make_violation(
                    "conflicting_labels", "error", net, pinrefs, xy,
                    "connectivity class " + net + " carries conflicting labels: " +
                        join_set(distinct)));
            }
        }
    }

    // Net names that are likely case or one-keystroke mistakes, reported once per pair.
    int similar_count = 0;
    for (size_t i = 0; i < net_keys.size() && similar_count < 20; ++i) {
        for (size_t k = i + 1; k < net_keys.size() && similar_count < 20; ++k) {
            if (net_keys[i].rfind("N$", 0) == 0 || net_keys[k].rfind("N$", 0) == 0) continue;
            if (!one_edit_or_transposition(net_keys[i], net_keys[k])) continue;
            std::vector<std::string> pins;
            if (nets[net_keys[i]].is_array())
                for (const json& member : nets[net_keys[i]])
                    if (member.is_object()) pins.push_back(member_pinref(member));
            if (nets[net_keys[k]].is_array())
                for (const json& member : nets[net_keys[k]])
                    if (member.is_object()) pins.push_back(member_pinref(member));
            json xy = nullptr;
            if (nets[net_keys[i]].is_array() && !nets[net_keys[i]].empty())
                xy = member_xy(nets[net_keys[i]][0]);
            if (!xy.is_array() && nets[net_keys[k]].is_array() && !nets[net_keys[k]].empty())
                xy = member_xy(nets[net_keys[k]][0]);
            if (!xy.is_array()) xy = label_location(sch, {net_keys[i], net_keys[k]});
            out.push_back(make_violation(
                "similar_labels", "warning", net_keys[i], pins, xy,
                "net names '" + net_keys[i] + "' and '" + net_keys[k] +
                    "' differ only by case or one character edit/transposition"));
            ++similar_count;
        }
    }

    // Symbol reference, BOM, footprint, and pin-grid rules.
    std::vector<const json*> symbols;
    if (sch.contains("symbols") && sch["symbols"].is_array())
        for (const json& symbol : sch["symbols"])
            if (symbol.is_object()) symbols.push_back(&symbol);
    std::sort(symbols.begin(), symbols.end(), [](const json* a, const json* b) {
        const std::string ar = field_string(*a, "ref", "~");
        const std::string br = field_string(*b, "ref", "~");
        if (ar != br) return ar < br;
        return field_string(*a, "id") < field_string(*b, "id");
    });

    const json cache = sch.contains("lib_cache") && sch["lib_cache"].is_object()
                           ? sch["lib_cache"]
                           : json::object();
    std::map<std::string, std::vector<const json*>> symbols_by_ref;
    for (const json* symbol : symbols) {
        const std::string ref = field_string(*symbol, "ref");
        const std::string sid = field_string(*symbol, "id");
        const json xy = symbol->contains("pos") ? (*symbol)["pos"] : json(nullptr);
        const bool unannotated = ref.empty() || ref.back() == '?';
        if (unannotated) {
            out.push_back(make_violation("unannotated_ref", "warning", nullptr, {sid}, xy,
                                         "symbol " + sid + " has an unannotated ref '" + ref +
                                             "'"));
        } else {
            symbols_by_ref[ref].push_back(symbol);
        }

        const bool power_symbol = symbol_is_power(*symbol, cache);
        if (field_bool(*symbol, "on_board", true) && field_string(*symbol, "footprint").empty() &&
            !power_symbol)
            out.push_back(make_violation(
                "missing_footprint", "warning", nullptr, {sid}, xy,
                "on-board symbol " + sid + " (" + ref + ") has no footprint assigned"));
        if (field_bool(*symbol, "in_bom", true) && field_string(*symbol, "value").empty() &&
            !power_symbol)
            out.push_back(make_violation("missing_value", "warning", nullptr, {sid}, xy,
                                         "BOM symbol " + sid + " (" + ref +
                                             ") has an empty value"));
    }

    for (const auto& item : symbols_by_ref) {
        const std::string& ref = item.first;
        const std::vector<const json*>& group = item.second;
        if (group.size() < 2) continue;
        std::set<std::string> libids;
        std::map<int, std::vector<const json*>> by_unit;
        for (const json* symbol : group) {
            libids.insert(field_string(*symbol, "libid"));
            by_unit[field_int(*symbol, "unit", 1)].push_back(symbol);
        }
        if (libids.size() > 1) {
            std::vector<std::string> ids;
            for (const json* symbol : group) ids.push_back(field_string(*symbol, "id"));
            const json xy = group.front()->contains("pos") ? (*group.front())["pos"] : json(nullptr);
            out.push_back(make_violation(
                "duplicate_ref", "error", nullptr, ids, xy,
                "ref '" + ref + "' is shared by symbols from different libraries"));
        }
        for (const auto& unit_group : by_unit) {
            if (unit_group.second.size() < 2) continue;
            std::vector<std::string> ids;
            for (const json* symbol : unit_group.second) ids.push_back(field_string(*symbol, "id"));
            const json xy = unit_group.second.front()->contains("pos")
                                ? (*unit_group.second.front())["pos"]
                                : json(nullptr);
            out.push_back(make_violation(
                "unit_ref_conflict", "error", nullptr, ids, xy,
                "ref '" + ref + "' has multiple symbols assigned to unit " +
                    std::to_string(unit_group.first)));
        }
    }

    double grid = 2.54;
    if (sch.contains("units") && sch["units"].is_object() && sch["units"].contains("grid") &&
        sch["units"]["grid"].is_number() && sch["units"]["grid"].get<double>() > 0.0)
        grid = sch["units"]["grid"].get<double>();
    int off_grid_count = 0;
    for (const auto& item : pin_info) {
        if (off_grid_count >= 20 || !item.second.has_xy) break;
        const Point& p = item.second.p;
        const double raw_x = item.second.raw_x;
        const double raw_y = item.second.raw_y;
        const bool x_off =
            std::fabs(raw_x - std::round(raw_x / grid) * grid) > kPointTolerance;
        const bool y_off =
            std::fabs(raw_y - std::round(raw_y / grid) * grid) > kPointTolerance;
        if (!x_off && !y_off) continue;
        const std::string net = by_pin.contains(item.first) && by_pin[item.first].is_string()
                                    ? by_pin[item.first].get<std::string>()
                                    : "";
        out.push_back(make_violation(
            "symbol_off_grid", "warning", net.empty() ? json(nullptr) : json(net), {item.first},
            point_json(p), "pin tip " + item.first + " is off the " + std::to_string(grid) +
                               " mm document grid"));
        ++off_grid_count;
    }

    // Keep the legacy identical-label disconnected-class check.
    std::map<std::string, std::vector<const json*>> labels_by_name;
    if (sch.contains("labels") && sch["labels"].is_array())
        for (const json& label : sch["labels"])
            if (label.is_object() && !field_string(label, "text").empty())
                labels_by_name[field_string(label, "text")].push_back(&label);
    for (const auto& item : labels_by_name) {
        if (nets.contains(item.first) || item.second.size() < 2) continue;
        std::set<SheetPoint> points;
        for (const json* label : item.second) {
            Point p;
            if (label->contains("pos") && read_point((*label)["pos"], p))
                points.insert({field_string(*label, "sheet"), p});
        }
        if (points.size() >= 2)
            out.push_back(make_violation(
                "multiple_nets_label", "error", item.first, {},
                label_location(sch, {item.first}),
                "label '" + item.first + "' is placed on " + std::to_string(points.size()) +
                    " disconnected points but no net carries it"));
    }

    // Bus expansion/reference consistency.
    struct BusGeom {
        std::string id;
        std::string name;
        std::string sheet;
        std::vector<Point> pts;
    };
    std::vector<BusGeom> bus_geometries;
    std::map<std::string, std::string> bus_id_to_name;
    if (sch.contains("buses") && sch["buses"].is_array()) {
        for (const json& bus : sch["buses"]) {
            if (!bus.is_object()) continue;
            BusGeom geom;
            geom.id = field_string(bus, "id");
            geom.name = field_string(bus, "name");
            geom.sheet = field_string(bus, "sheet");
            if (bus.contains("pts") && bus["pts"].is_array())
                for (const json& value : bus["pts"]) {
                    Point p;
                    if (read_point(value, p)) geom.pts.push_back(p);
                }
            if (!geom.id.empty() && !geom.name.empty()) bus_id_to_name[geom.id] = geom.name;
            bus_geometries.push_back(std::move(geom));
        }
    }
    std::sort(bus_geometries.begin(), bus_geometries.end(), [](const BusGeom& a, const BusGeom& b) {
        return std::tie(a.name, a.id) < std::tie(b.name, b.id);
    });

    auto bus_at_point = [&](const std::string& sheet, const Point& p) -> std::string {
        for (const BusGeom& bus : bus_geometries) {
            if (bus.sheet != sheet || bus.name.empty()) continue;
            for (size_t i = 0; i + 1 < bus.pts.size(); ++i)
                if (point_on_segment(p, bus.pts[i], bus.pts[i + 1])) return bus.name;
            if (bus.pts.size() == 1 && bus.pts[0] == p) return bus.name;
        }
        return "";
    };

    std::map<std::string, std::set<std::string>> bus_references;
    for (auto it = net_buses.begin(); it != net_buses.end(); ++it) {
        if (!it.value().is_array()) continue;
        std::set<std::string> expansion;
        for (const json& member : it.value()) {
            const std::string name = jstr(member);
            if (!name.empty()) expansion.insert(name);
        }
        for (const auto& label_group : labels_by_name)
            if (expansion.count(label_group.first)) bus_references[it.key()].insert(label_group.first);
    }

    struct BusEntryRef {
        std::string id;
        std::string bus;
        std::string member;
        json xy;
    };
    std::vector<BusEntryRef> entry_refs;
    if (sch.contains("bus_entries") && sch["bus_entries"].is_array()) {
        for (const json& entry : sch["bus_entries"]) {
            if (!entry.is_object()) continue;
            std::string bus = field_string(entry, "bus");
            if (bus_id_to_name.count(bus)) bus = bus_id_to_name[bus];
            Point p;
            const bool has_point = entry.contains("pos") && read_point(entry["pos"], p);
            if (bus.empty() && has_point) bus = bus_at_point(field_string(entry, "sheet"), p);
            BusEntryRef ref{field_string(entry, "id"), bus, field_string(entry, "member"),
                            has_point ? point_json(p) : json(nullptr)};
            entry_refs.push_back(ref);
            if (!ref.bus.empty() && !ref.member.empty()) bus_references[ref.bus].insert(ref.member);
        }
    }
    std::sort(entry_refs.begin(), entry_refs.end(), [](const BusEntryRef& a, const BusEntryRef& b) {
        return std::tie(a.bus, a.member, a.id) < std::tie(b.bus, b.member, b.id);
    });

    for (auto it = net_buses.begin(); it != net_buses.end(); ++it) {
        if (!it.value().is_array()) continue;
        json bus_xy = nullptr;
        for (const BusGeom& bus : bus_geometries)
            if (bus.name == it.key() && !bus.pts.empty()) {
                bus_xy = point_json(bus.pts.front());
                break;
            }
        for (const json& value : it.value()) {
            const std::string member = jstr(value);
            if (member.empty() || bus_references[it.key()].count(member)) continue;
            out.push_back(make_violation(
                "bus_member_mismatch", "warning", member, {}, bus_xy,
                "bus " + it.key() + " expands to member " + member +
                    " but no bus entry or scalar label references it"));
        }
    }
    for (const BusEntryRef& entry : entry_refs) {
        if (entry.bus.empty() || entry.member.empty()) continue;
        bool found = false;
        if (net_buses.contains(entry.bus) && net_buses[entry.bus].is_array())
            for (const json& member : net_buses[entry.bus])
                if (jstr(member) == entry.member) found = true;
        if (!found)
            out.push_back(make_violation(
                "bus_member_mismatch", "warning", entry.member, {}, entry.xy,
                "bus entry " + entry.id + " resolves member " + entry.member +
                    " outside bus " + entry.bus + " expansion"));
    }

    // Hierarchy mismatch and parent-side sheet-pin connectivity.
    if (sch.contains("sheet_symbols") && sch["sheet_symbols"].is_array()) {
        std::map<std::string, std::map<std::string, const json*>> hier_labels;
        for (const auto& item : labels_by_name)
            for (const json* label : item.second)
                if (field_string(*label, "scope") == "hier")
                    hier_labels[field_string(*label, "sheet")][item.first] = label;

        std::map<std::string, std::set<std::string>> pins_for_child;
        std::vector<const json*> sheet_symbols;
        for (const json& symbol : sch["sheet_symbols"])
            if (symbol.is_object()) sheet_symbols.push_back(&symbol);
        std::sort(sheet_symbols.begin(), sheet_symbols.end(), [](const json* a, const json* b) {
            return field_string(*a, "id") < field_string(*b, "id");
        });

        std::map<SheetPoint, int> sheet_pins_at_point;
        for (const json* sheet_symbol : sheet_symbols) {
            if (!sheet_symbol->contains("pins") || !(*sheet_symbol)["pins"].is_array()) continue;
            for (const json& pin : (*sheet_symbol)["pins"]) {
                if (!pin.is_object()) continue;
                Point p;
                bool has_point = pin.contains("pos") && read_point(pin["pos"], p);
                if (!has_point && sheet_symbol->contains("pos"))
                    has_point = read_point((*sheet_symbol)["pos"], p);
                if (has_point)
                    ++sheet_pins_at_point[{field_string(*sheet_symbol, "sheet"), p}];
            }
        }

        for (const json* sheet_symbol : sheet_symbols) {
            const std::string id = field_string(*sheet_symbol, "id");
            const std::string child = field_string(*sheet_symbol, "child");
            const std::string sheet = field_string(*sheet_symbol, "sheet");
            std::vector<const json*> pins;
            if (sheet_symbol->contains("pins") && (*sheet_symbol)["pins"].is_array())
                for (const json& pin : (*sheet_symbol)["pins"])
                    if (pin.is_object()) pins.push_back(&pin);
            std::sort(pins.begin(), pins.end(), [](const json* a, const json* b) {
                return field_string(*a, "name") < field_string(*b, "name");
            });
            for (const json* pin : pins) {
                const std::string name = field_string(*pin, "name");
                if (!name.empty()) pins_for_child[child].insert(name);
                json xy = pin->contains("pos") ? clean_xy((*pin)["pos"]) : json(nullptr);
                if (!xy.is_array() && sheet_symbol->contains("pos"))
                    xy = clean_xy((*sheet_symbol)["pos"]);
                if (!name.empty() && !hier_labels[child].count(name))
                    out.push_back(make_violation(
                        "hier_pin_no_label", "warning", nullptr, {id + "." + name}, xy,
                        "sheet pin '" + name + "' on " + id +
                            " has no matching hier label in child sheet '" + child + "'"));

                Point p;
                if (xy.is_array() && read_point(xy, p)) {
                    const SheetPoint key{sheet, p};
                    const bool touches_pin =
                        (pins_at_point.count(key) && !pins_at_point[key].empty()) ||
                        sheet_pins_at_point[key] > 1;
                    if (!touches_pin && !point_touches_wire(wires, sheet, p))
                        out.push_back(make_violation(
                            "sheet_pin_not_connected", "warning", nullptr, {id + "." + name}, xy,
                            "parent-side sheet pin '" + name + "' on " + id +
                                " touches no wire or symbol pin"));
                }
            }
        }

        for (const auto& sheet_group : hier_labels) {
            if (!pins_for_child.count(sheet_group.first)) continue;
            for (const auto& label : sheet_group.second) {
                if (pins_for_child[sheet_group.first].count(label.first)) continue;
                const json xy = label.second->contains("pos") ? (*label.second)["pos"] : json(nullptr);
                out.push_back(make_violation(
                    "hier_label_no_pin", "warning", nullptr,
                    {sheet_group.first + ":" + label.first}, xy,
                    "hier label '" + label.first + "' in sheet '" + sheet_group.first +
                        "' is not exposed as a sheet pin on any symbol instantiating it"));
            }
        }
    }

    sort_violations(out);
    return out;
}

}  // namespace

json run_erc(const json& sch, const json& netlist) {
    try {
        return run_erc_impl(sch, netlist);
    } catch (const std::exception& error) {
        return json::array({make_violation(
            "erc_input_invalid", "warning", nullptr, {}, nullptr,
            std::string("ERC could not fully inspect malformed advisory input: ") + error.what())});
    } catch (...) {
        return json::array({make_violation("erc_input_invalid", "warning", nullptr, {}, nullptr,
                                           "ERC could not fully inspect malformed advisory input")});
    }
}

}  // namespace chudware::ecad
