// Schematic connectivity and netlist emission — see netlist.h.
// OCCT-free, deterministic, and pure JSON.

#include "ecad/netlist.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "ecad/schema.h"

namespace chudware::ecad {

using nlohmann::json;

namespace {

constexpr double kSnapInv = 1000000.0;
constexpr const char* kSyntheticSheet = "\x1finternal:bus-member";

struct Node {
    std::string sheet;
    std::int64_t x = 0;
    std::int64_t y = 0;

    bool operator==(const Node& o) const {
        return sheet == o.sheet && x == o.x && y == o.y;
    }
    bool operator<(const Node& o) const {
        if (sheet != o.sheet) return sheet < o.sheet;
        if (x != o.x) return x < o.x;
        return y < o.y;
    }
};

double py_round(double v) {
    double fl = std::floor(v);
    double diff = v - fl;
    if (diff < 0.5) return fl;
    if (diff > 0.5) return fl + 1.0;
    return std::fmod(fl, 2.0) == 0.0 ? fl : fl + 1.0;
}

double get_num(const json& v, double dflt = 0.0) {
    return v.is_number() ? v.get<double>() : dflt;
}

std::string jstr(const json& v, const std::string& dflt = "") {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number()) return v.dump();
    return dflt;
}

bool jbool(const json& object, const char* key, bool dflt) {
    return object.contains(key) && object[key].is_boolean() ? object[key].get<bool>() : dflt;
}

Node snap_xy(const std::string& sheet, double x, double y) {
    return Node{sheet, static_cast<std::int64_t>(py_round(x * kSnapInv)),
                static_cast<std::int64_t>(py_round(y * kSnapInv))};
}

Node snap(const std::string& sheet, const json& p) {
    double x = p.is_array() && !p.empty() ? get_num(p[0]) : 0.0;
    double y = p.is_array() && p.size() > 1 ? get_num(p[1]) : 0.0;
    return snap_xy(sheet, x, y);
}

double mm(std::int64_t ticks) {
    return static_cast<double>(ticks) / kSnapInv + 0.0;
}

class UnionFind {
public:
    void add(const Node& p) {
        if (parent_.find(p) == parent_.end()) parent_[p] = p;
    }

    Node find(const Node& p) {
        add(p);
        Node root = p;
        while (!(parent_[root] == root)) root = parent_[root];
        Node cur = p;
        while (!(parent_[cur] == root)) {
            Node next = parent_[cur];
            parent_[cur] = root;
            cur = next;
        }
        return root;
    }

    void unite(const Node& a, const Node& b) {
        Node ra = find(a);
        Node rb = find(b);
        if (ra == rb) return;
        if (rb < ra) std::swap(ra, rb);
        parent_[rb] = ra;
    }

    std::map<Node, std::vector<Node>> classes() {
        std::vector<Node> nodes;
        nodes.reserve(parent_.size());
        for (const auto& kv : parent_) nodes.push_back(kv.first);
        std::map<Node, std::vector<Node>> out;
        for (const Node& node : nodes) out[find(node)].push_back(node);
        for (auto& kv : out) std::sort(kv.second.begin(), kv.second.end());
        return out;
    }

private:
    std::map<Node, Node> parent_;
};

struct Segment {
    Node a;
    Node b;
    std::string id;
    std::size_t index = 0;
};

using SegmentIndex = std::map<std::string, std::vector<Segment>>;

bool on_segment(const Node& p, const Node& a, const Node& b) {
    if (p.sheet != a.sheet || a.sheet != b.sheet) return false;
    long double abx = static_cast<long double>(b.x) - a.x;
    long double aby = static_cast<long double>(b.y) - a.y;
    long double apx = static_cast<long double>(p.x) - a.x;
    long double apy = static_cast<long double>(p.y) - a.y;
    long double length = std::sqrt(abx * abx + aby * aby);
    if (length <= 1.0L) return p == a;
    long double cross = abx * apy - aby * apx;
    if (std::fabs(cross) > length) return false;
    long double dot = apx * abx + apy * aby;
    return -length <= dot && dot <= length * length + length;
}

bool attach_to_segments(UnionFind& uf, const Node& point, const SegmentIndex& index) {
    auto sheet_it = index.find(point.sheet);
    if (sheet_it == index.end()) return false;
    bool attached = false;
    for (const Segment& segment : sheet_it->second) {
        if (!on_segment(point, segment.a, segment.b)) continue;
        uf.unite(point, segment.a);
        uf.unite(point, segment.b);
        attached = true;
    }
    return attached;
}

std::vector<const json*> sorted_entities(const json& doc, const char* key) {
    std::vector<const json*> out;
    if (doc.contains(key) && doc[key].is_array())
        for (const json& item : doc[key])
            if (item.is_object()) out.push_back(&item);
    std::sort(out.begin(), out.end(), [](const json* a, const json* b) {
        return a->value("id", std::string()) < b->value("id", std::string());
    });
    return out;
}

std::vector<Node> add_polyline(UnionFind& uf, SegmentIndex& index, const json& entity) {
    const std::string sheet = entity.value("sheet", std::string());
    const std::string id = entity.value("id", std::string());
    std::vector<Node> points;
    if (entity.contains("pts") && entity["pts"].is_array())
        for (const json& point : entity["pts"]) points.push_back(snap(sheet, point));
    for (const Node& point : points) uf.add(point);
    for (std::size_t i = 0; i + 1 < points.size(); ++i) {
        uf.unite(points[i], points[i + 1]);
        index[sheet].push_back(Segment{points[i], points[i + 1], id, i});
    }
    return points;
}

void attach_polyline_endpoints(UnionFind& uf, const std::vector<std::vector<Node>>& polylines,
                               const SegmentIndex& index) {
    for (const std::vector<Node>& points : polylines) {
        if (points.empty()) continue;
        attach_to_segments(uf, points.front(), index);
        if (points.size() > 1) attach_to_segments(uf, points.back(), index);
    }
}

std::string trim(std::string value) {
    auto non_space = [](unsigned char c) { return !std::isspace(c); };
    value.erase(value.begin(), std::find_if(value.begin(), value.end(), non_space));
    value.erase(std::find_if(value.rbegin(), value.rend(), non_space).base(), value.end());
    return value;
}

bool unsigned_number(const std::string& value) {
    if (value.empty()) return false;
    for (unsigned char c : value)
        if (!std::isdigit(c)) return false;
    return true;
}

void append_unique(std::vector<std::string>& out, const std::string& value) {
    if (!value.empty() && std::find(out.begin(), out.end(), value) == out.end())
        out.push_back(value);
}

std::vector<std::string> expand_bus_spec(const std::string& raw) {
    const std::string spec = trim(raw);
    std::vector<std::string> out;
    if (spec.size() >= 2 && spec.front() == '{' && spec.back() == '}') {
        const std::string body = spec.substr(1, spec.size() - 2);
        std::size_t start = 0;
        while (start <= body.size()) {
            std::size_t comma = body.find(',', start);
            append_unique(out, trim(body.substr(start, comma == std::string::npos
                                                           ? std::string::npos
                                                           : comma - start)));
            if (comma == std::string::npos) break;
            start = comma + 1;
        }
        return out;
    }

    std::size_t left = spec.rfind('[');
    if (left != std::string::npos && !spec.empty() && spec.back() == ']') {
        const std::string body = spec.substr(left + 1, spec.size() - left - 2);
        const std::size_t dots = body.find("..");
        if (dots != std::string::npos) {
            const std::string first = body.substr(0, dots);
            const std::string last = body.substr(dots + 2);
            if (unsigned_number(first) && unsigned_number(last)) {
                try {
                    const long long begin = std::stoll(first);
                    const long long end = std::stoll(last);
                    const long long step = end >= begin ? 1 : -1;
                    const std::string base = spec.substr(0, left);
                    for (long long value = begin;; value += step) {
                        append_unique(out, base + std::to_string(value));
                        if (value == end) break;
                    }
                    return out;
                } catch (...) {
                    // Treat an out-of-range expression as a plain alias.
                }
            }
        }
    }
    append_unique(out, spec);
    return out;
}

std::vector<std::string> expanded_members(const json& bus, const std::string& name) {
    std::vector<std::string> out;
    if (bus.contains("members")) {
        const json& members = bus["members"];
        if (members.is_array()) {
            for (const json& member : members) {
                std::vector<std::string> expanded = expand_bus_spec(jstr(member));
                for (const std::string& value : expanded) append_unique(out, value);
            }
        } else {
            std::vector<std::string> expanded = expand_bus_spec(jstr(members));
            for (const std::string& value : expanded) append_unique(out, value);
        }
    }
    if (out.empty()) out = expand_bus_spec(name);
    return out;
}

std::string bus_name(const json& bus) {
    if (bus.contains("name") && bus["name"].is_string()) return trim(bus["name"].get<std::string>());
    if (bus.contains("members") && bus["members"].is_string())
        return trim(bus["members"].get<std::string>());
    if (bus.contains("members") && bus["members"].is_array()) {
        std::string name = "{";
        bool first = true;
        for (const json& member : bus["members"]) {
            const std::string value = trim(jstr(member));
            if (value.empty()) continue;
            if (!first) name += ',';
            name += value;
            first = false;
        }
        name += '}';
        if (!first) return name;
    }
    return {};
}

struct PinRec {
    std::string sym, ref, pin, name, type, sheet, libid;
    Node node;
};

struct LabelRec {
    std::string id, name, scope, sheet;
    Node node;
};

struct SheetPinRec {
    std::string name, sheet;
    Node node;
};

struct PowerRec {
    std::string id, net, symbol;
    Node node;
};

struct BusGeom {
    std::string id, sheet, name;
    std::vector<std::string> members;
    std::vector<Node> points;
};

struct BusNamePoint {
    std::string name;
    Node node;
};

struct BusEntryRec {
    std::string id, sheet, requested_bus, requested_member;
    Node bus_side, wire_side;
    bool bus_attached = false;
    bool wire_attached = false;
};

struct NameChoice {
    std::string base, sheet;
    int priority = 0;
    int subpriority = 0;
};

bool better_name(const NameChoice& candidate, const NameChoice& current) {
    if (candidate.priority != current.priority) return candidate.priority > current.priority;
    if (candidate.subpriority != current.subpriority)
        return candidate.subpriority < current.subpriority;
    if (candidate.base != current.base) return candidate.base < current.base;
    return candidate.sheet < current.sheet;
}

std::string first_sheet(const std::set<std::string>& sheets) {
    return sheets.empty() ? std::string("global") : *sheets.begin();
}

}  // namespace

json build_netlist(const json& sch) {
    std::vector<std::string> warnings;
    if (!schema::is_schematic(sch)) {
        const std::string value = sch.contains("schema") ? sch["schema"].dump() : "null";
        return json{{"nets", json::object()},
                    {"by_pin", json::object()},
                    {"by_object", json::object()},
                    {"components", json::array()},
                    {"net_info", json::object()},
                    {"buses", json::object()},
                    {"warnings", json::array({"not a schematic document (schema=" + value + ")"})}};
    }

    UnionFind uf;
    SegmentIndex wire_segments;
    std::vector<std::vector<Node>> wire_polylines;
    std::map<std::string, Node> scalar_objects;
    std::map<std::string, Node> bus_objects;
    std::map<std::string, std::string> direct_object_names;
    for (const json* wire : sorted_entities(sch, "wires")) {
        std::vector<Node> points = add_polyline(uf, wire_segments, *wire);
        if (!points.empty()) scalar_objects[wire->value("id", std::string())] = points.front();
        wire_polylines.push_back(std::move(points));
    }
    attach_polyline_endpoints(uf, wire_polylines, wire_segments);

    UnionFind bus_uf;
    SegmentIndex bus_segments;
    std::vector<std::vector<Node>> bus_polylines;
    std::vector<BusGeom> bus_geometries;
    std::map<std::string, std::vector<std::string>> bus_catalog;
    std::map<std::string, std::set<std::string>> buses_by_member;
    std::vector<BusNamePoint> bus_name_points;

    for (const json* bus : sorted_entities(sch, "buses")) {
        BusGeom geom;
        geom.id = bus->value("id", std::string());
        geom.sheet = bus->value("sheet", std::string());
        geom.name = bus_name(*bus);
        geom.members = expanded_members(*bus, geom.name);
        geom.points = add_polyline(bus_uf, bus_segments, *bus);
        bus_polylines.push_back(geom.points);
        if (geom.name.empty()) {
            warnings.push_back("bus_unnamed: bus " + geom.id + " has no name");
        } else {
            std::vector<std::string>& catalog_members = bus_catalog[geom.name];
            for (const std::string& member : geom.members) {
                append_unique(catalog_members, member);
                buses_by_member[member].insert(geom.name);
            }
            if (!geom.points.empty()) {
                direct_object_names[geom.id] = geom.name;
                bus_name_points.push_back(BusNamePoint{geom.name, geom.points.front()});
                bus_objects[geom.id] = geom.points.front();
            }
        }
        bus_geometries.push_back(std::move(geom));
    }
    attach_polyline_endpoints(bus_uf, bus_polylines, bus_segments);

    for (const json* junction : sorted_entities(sch, "junctions")) {
        const std::string id = junction->value("id", std::string());
        const Node point = snap(junction->value("sheet", std::string()),
                                junction->contains("pos") ? (*junction)["pos"]
                                                          : json::array({0, 0}));
        const bool scalar_attached = attach_to_segments(uf, point, wire_segments);
        const bool bus_attached = attach_to_segments(bus_uf, point, bus_segments);
        if (scalar_attached)
            scalar_objects[id] = point;
        else if (bus_attached)
            bus_objects[id] = point;
    }

    std::vector<PinRec> pin_records;
    const json empty_cache = json::object();
    const json& cache = sch.contains("lib_cache") && sch["lib_cache"].is_object()
                            ? sch["lib_cache"]
                            : empty_cache;
    for (const json* symbol : sorted_entities(sch, "symbols")) {
        const std::string libid = symbol->value("libid", std::string());
        if (!cache.contains(libid) || !cache[libid].is_object()) continue;
        const json& definition = cache[libid];
        if (!definition.contains("pins") || !definition["pins"].is_array()) continue;
        const int unit = symbol->contains("unit") && (*symbol)["unit"].is_number_integer()
                             ? (*symbol)["unit"].get<int>()
                             : 1;
        std::vector<const json*> pins;
        for (const json& pin : definition["pins"])
            if (pin.is_object()) pins.push_back(&pin);
        std::stable_sort(pins.begin(), pins.end(), [](const json* a, const json* b) {
            const std::string an = jstr(a->contains("number") ? (*a)["number"] : json(""));
            const std::string bn = jstr(b->contains("number") ? (*b)["number"] : json(""));
            if (an != bn) return an < bn;
            const int au = a->contains("unit") && (*a)["unit"].is_number_integer()
                               ? (*a)["unit"].get<int>()
                               : 1;
            const int bu = b->contains("unit") && (*b)["unit"].is_number_integer()
                               ? (*b)["unit"].get<int>()
                               : 1;
            if (au != bu) return au < bu;
            return jstr(a->contains("name") ? (*a)["name"] : json("")) <
                   jstr(b->contains("name") ? (*b)["name"] : json(""));
        });
        for (const json* pin : pins) {
            const int pin_unit = pin->contains("unit") && (*pin)["unit"].is_number_integer()
                                     ? (*pin)["unit"].get<int>()
                                     : 1;
            if (pin_unit != unit && pin_unit != 0) continue;
            auto [x, y] = schema::pin_world(*symbol, *pin);
            const std::string sheet = symbol->value("sheet", std::string());
            const Node node = snap_xy(sheet, x, y);
            uf.add(node);
            attach_to_segments(uf, node, wire_segments);
            pin_records.push_back(PinRec{
                symbol->value("id", std::string()),
                symbol->value("ref", std::string()),
                jstr(pin->contains("number") ? (*pin)["number"] : json("")),
                jstr(pin->contains("name") ? (*pin)["name"] : json("")),
                jstr(pin->contains("type") ? (*pin)["type"] : json("passive"), "passive"),
                sheet,
                libid,
                node,
            });
        }
    }

    std::vector<LabelRec> labels;
    std::map<std::pair<std::string, std::string>, std::vector<Node>> label_groups;
    std::map<std::pair<std::string, std::string>, Node> hierarchy_labels;
    for (const json* label : sorted_entities(sch, "labels")) {
        const std::string id = label->value("id", std::string());
        const std::string name = jstr(label->contains("text") ? (*label)["text"] : json(""));
        if (name.empty()) continue;
        const std::string sheet = label->value("sheet", std::string());
        const std::string scope = jstr(label->contains("scope") ? (*label)["scope"] : json("local"),
                                       "local");
        const Node node = snap(sheet, label->contains("pos") ? (*label)["pos"]
                                                             : json::array({0, 0}));
        if (bus_catalog.count(name) && attach_to_segments(bus_uf, node, bus_segments)) {
            bus_name_points.push_back(BusNamePoint{name, node});
            bus_objects[id] = node;
            direct_object_names[id] = name;
            continue;
        }
        uf.add(node);
        attach_to_segments(uf, node, wire_segments);
        const std::string group_sheet = scope == "global" ? "\x1fglobal" : sheet;
        label_groups[{group_sheet, name}].push_back(node);
        labels.push_back(LabelRec{id, name, scope, sheet, node});
        scalar_objects[id] = node;
        if (scope == "hier" && !hierarchy_labels.count({sheet, name}))
            hierarchy_labels[{sheet, name}] = node;
    }
    for (auto& kv : label_groups) {
        std::vector<Node>& anchors = kv.second;
        std::sort(anchors.begin(), anchors.end());
        for (std::size_t i = 1; i < anchors.size(); ++i) uf.unite(anchors[0], anchors[i]);
    }

    std::vector<SheetPinRec> sheet_pins;
    for (const json* sheet_symbol : sorted_entities(sch, "sheet_symbols")) {
        const std::string parent = sheet_symbol->value("sheet", std::string());
        const std::string child = sheet_symbol->value("child", std::string());
        std::vector<const json*> pins;
        if (sheet_symbol->contains("pins") && (*sheet_symbol)["pins"].is_array())
            for (const json& pin : (*sheet_symbol)["pins"])
                if (pin.is_object()) pins.push_back(&pin);
        std::stable_sort(pins.begin(), pins.end(), [](const json* a, const json* b) {
            return jstr(a->contains("name") ? (*a)["name"] : json("")) <
                   jstr(b->contains("name") ? (*b)["name"] : json(""));
        });
        for (const json* pin : pins) {
            const std::string name = jstr(pin->contains("name") ? (*pin)["name"] : json(""));
            if (name.empty()) continue;
            const Node node = snap(parent, pin->contains("pos") ? (*pin)["pos"]
                                                                : json::array({0, 0}));
            uf.add(node);
            attach_to_segments(uf, node, wire_segments);
            sheet_pins.push_back(SheetPinRec{name, parent, node});
            auto child_label = hierarchy_labels.find({child, name});
            if (child_label != hierarchy_labels.end()) uf.unite(node, child_label->second);
        }
    }

    std::vector<PowerRec> powers;
    std::map<std::string, std::vector<Node>> power_groups;
    for (const json* power : sorted_entities(sch, "power")) {
        const std::string id = power->value("id", std::string());
        const std::string net = jstr(power->contains("net") ? (*power)["net"] : json(""));
        if (net.empty()) continue;
        const std::string sheet = power->value("sheet", std::string());
        const Node node = snap(sheet, power->contains("pos") ? (*power)["pos"]
                                                             : json::array({0, 0}));
        uf.add(node);
        attach_to_segments(uf, node, wire_segments);
        power_groups[net].push_back(node);
        powers.push_back(PowerRec{id, net,
                                  jstr(power->contains("symbol") ? (*power)["symbol"] : json("")),
                                  node});
        scalar_objects[id] = node;
    }
    for (auto& kv : power_groups) {
        std::vector<Node>& anchors = kv.second;
        std::sort(anchors.begin(), anchors.end());
        for (std::size_t i = 1; i < anchors.size(); ++i) uf.unite(anchors[0], anchors[i]);
    }

    std::map<std::pair<std::string, std::string>, Node> member_anchors;
    std::map<Node, std::string> member_anchor_sheet;
    std::map<Node, std::string> member_anchor_name;
    std::map<Node, std::set<std::string>> member_anchor_buses;
    std::int64_t synthetic_index = 1;
    for (const BusGeom& bus : bus_geometries) {
        if (bus.name.empty()) continue;
        for (const std::string& member : bus.members) {
            const auto key = std::make_pair(bus.sheet, member);
            auto inserted = member_anchors.emplace(key, Node{kSyntheticSheet, synthetic_index, 0});
            if (inserted.second) {
                ++synthetic_index;
                uf.add(inserted.first->second);
                member_anchor_sheet[inserted.first->second] = bus.sheet;
                member_anchor_name[inserted.first->second] = member;
            }
            member_anchor_buses[inserted.first->second].insert(bus.name);
        }
    }

    std::vector<BusEntryRec> entries;
    for (const json* entry : sorted_entities(sch, "bus_entries")) {
        const std::string sheet = entry->value("sheet", std::string());
        const json pos = entry->contains("pos") ? (*entry)["pos"] : json::array({0, 0});
        const json size = entry->contains("size") ? (*entry)["size"] : json::array({0, 0});
        const double px = pos.is_array() && !pos.empty() ? get_num(pos[0]) : 0.0;
        const double py = pos.is_array() && pos.size() > 1 ? get_num(pos[1]) : 0.0;
        const double dx = size.is_array() && !size.empty() ? get_num(size[0]) : 0.0;
        const double dy = size.is_array() && size.size() > 1 ? get_num(size[1]) : 0.0;
        BusEntryRec rec;
        rec.id = entry->value("id", std::string());
        rec.sheet = sheet;
        rec.requested_bus = jstr(entry->contains("bus") ? (*entry)["bus"] : json(""));
        rec.requested_member = jstr(entry->contains("member") ? (*entry)["member"] : json(""));
        rec.bus_side = snap_xy(sheet, px, py);
        rec.wire_side = snap_xy(sheet, px + dx, py + dy);
        rec.bus_attached = attach_to_segments(bus_uf, rec.bus_side, bus_segments);
        rec.wire_attached = attach_to_segments(uf, rec.wire_side, wire_segments);
        entries.push_back(rec);
    }

    std::map<Node, std::set<std::string>> names_by_bus_root;
    std::sort(bus_name_points.begin(), bus_name_points.end(), [](const BusNamePoint& a,
                                                                 const BusNamePoint& b) {
        if (a.node < b.node) return true;
        if (b.node < a.node) return false;
        return a.name < b.name;
    });
    for (const BusNamePoint& name : bus_name_points)
        names_by_bus_root[bus_uf.find(name.node)].insert(name.name);

    for (const auto& kv : bus_objects) {
        auto names = names_by_bus_root.find(bus_uf.find(kv.second));
        if (!direct_object_names.count(kv.first) && names != names_by_bus_root.end() &&
            !names->second.empty())
            direct_object_names[kv.first] = *names->second.begin();
    }

    std::map<Node, std::set<std::string>> labels_before_entries;
    for (const LabelRec& label : labels) labels_before_entries[uf.find(label.node)].insert(label.name);

    std::map<std::string, Node> resolved_entry_nodes;
    for (const BusEntryRec& entry : entries) {
        auto unresolved = [&](const std::string& reason) {
            warnings.push_back("bus_entry_unresolved: bus entry " + entry.id + " " + reason);
        };
        if (!entry.bus_attached) {
            unresolved("does not touch a bus");
            continue;
        }
        if (!entry.wire_attached) {
            unresolved("does not touch a scalar wire");
            continue;
        }
        const Node bus_root = bus_uf.find(entry.bus_side);
        const std::set<std::string>& root_names = names_by_bus_root[bus_root];
        std::string selected_bus;
        if (!entry.requested_bus.empty()) {
            if (!root_names.count(entry.requested_bus)) {
                unresolved("requests bus '" + entry.requested_bus + "' but touches a different bus");
                continue;
            }
            selected_bus = entry.requested_bus;
        } else if (!root_names.empty()) {
            selected_bus = *root_names.begin();
        } else {
            unresolved("touches an unnamed bus");
            continue;
        }
        auto catalog = bus_catalog.find(selected_bus);
        if (catalog == bus_catalog.end() || catalog->second.empty()) {
            unresolved("bus '" + selected_bus + "' has no members");
            continue;
        }

        std::string member = entry.requested_member;
        if (member.empty()) {
            const Node scalar_root = uf.find(entry.wire_side);
            auto label_set = labels_before_entries.find(scalar_root);
            if (label_set != labels_before_entries.end()) {
                for (const std::string& label : label_set->second) {
                    if (std::find(catalog->second.begin(), catalog->second.end(), label) !=
                        catalog->second.end()) {
                        member = label;
                        break;
                    }
                }
            }
        }
        if (member.empty() ||
            std::find(catalog->second.begin(), catalog->second.end(), member) ==
                catalog->second.end()) {
            unresolved(member.empty() ? "has no member and its scalar wire has no member label"
                                      : "member '" + member + "' is not in bus '" + selected_bus + "'");
            continue;
        }
        auto anchor = member_anchors.find({entry.sheet, member});
        if (anchor == member_anchors.end()) {
            unresolved("member '" + member + "' is not defined on sheet '" + entry.sheet + "'");
            continue;
        }
        uf.unite(entry.wire_side, anchor->second);
        member_anchor_buses[anchor->second].insert(selected_bus);
        resolved_entry_nodes[entry.id] = entry.wire_side;
    }

    for (const LabelRec& label : labels) {
        if (!buses_by_member.count(label.name)) continue;
        if (label.scope == "global") {
            for (const auto& kv : member_anchors)
                if (kv.first.second == label.name) uf.unite(label.node, kv.second);
        } else {
            auto anchor = member_anchors.find({label.sheet, label.name});
            if (anchor != member_anchors.end()) uf.unite(label.node, anchor->second);
        }
    }

    const auto classes = uf.classes();
    std::map<Node, Node> root_of;
    for (const auto& kv : classes)
        for (const Node& node : kv.second) root_of[node] = kv.first;

    std::map<Node, std::set<std::string>> sheets_by_root;
    for (const auto& kv : classes) {
        for (const Node& node : kv.second) {
            if (node.sheet != kSyntheticSheet) sheets_by_root[kv.first].insert(node.sheet);
            auto physical_sheet = member_anchor_sheet.find(node);
            if (physical_sheet != member_anchor_sheet.end())
                sheets_by_root[kv.first].insert(physical_sheet->second);
        }
    }

    std::map<Node, NameChoice> names;
    auto consider_name = [&](const Node& node, const std::string& name, const std::string& sheet,
                             int priority, int subpriority) {
        if (name.empty()) return;
        const Node root = root_of.at(node);
        NameChoice candidate{name, sheet, priority, subpriority};
        auto current = names.find(root);
        if (current == names.end() || better_name(candidate, current->second)) names[root] = candidate;
    };

    for (const PowerRec& power : powers) consider_name(power.node, power.net, power.node.sheet, 3, 0);
    for (const LabelRec& label : labels)
        if (label.scope == "global") consider_name(label.node, label.name, label.sheet, 2, 0);
    for (const auto& kv : member_anchor_name)
        consider_name(kv.first, kv.second, member_anchor_sheet[kv.first], 1, 0);
    for (const LabelRec& label : labels)
        if (label.scope != "global") consider_name(label.node, label.name, label.sheet, 1, 1);
    for (const SheetPinRec& pin : sheet_pins) consider_name(pin.node, pin.name, pin.sheet, 1, 2);

    std::size_t automatic = 1;
    for (const auto& kv : classes) {
        if (!names.count(kv.first))
            names[kv.first] = NameChoice{"N$" + std::to_string(automatic++),
                                         first_sheet(sheets_by_root[kv.first]), 0, 0};
    }

    std::map<std::string, std::vector<Node>> roots_by_base;
    for (const auto& kv : names) roots_by_base[kv.second.base].push_back(kv.first);
    std::map<Node, std::string> display_by_root;
    std::set<std::string> used_display_names;
    std::set<std::string> reserved_base_names;
    for (const auto& kv : roots_by_base) reserved_base_names.insert(kv.first);
    for (auto& kv : roots_by_base) {
        std::vector<Node>& roots = kv.second;
        std::sort(roots.begin(), roots.end(), [&](const Node& a, const Node& b) {
            const NameChoice& an = names.at(a);
            const NameChoice& bn = names.at(b);
            if (an.priority != bn.priority) return an.priority > bn.priority;
            if (an.subpriority != bn.subpriority) return an.subpriority < bn.subpriority;
            return a < b;
        });
        for (std::size_t i = 0; i < roots.size(); ++i) {
            const Node root = roots[i];
            std::string display = kv.first;
            if (i > 0) {
                const std::string sheet = names[root].sheet.empty()
                                              ? first_sheet(sheets_by_root[root])
                                              : names[root].sheet;
                display = kv.first + "@" + sheet;
                int suffix = 2;
                while (used_display_names.count(display) ||
                       (reserved_base_names.count(display) && display != kv.first))
                    display = kv.first + "@" + sheet + "#" + std::to_string(suffix++);
            }
            display_by_root[root] = display;
            used_display_names.insert(display);
        }
    }

    std::map<Node, std::set<std::string>> labels_by_root;
    for (const LabelRec& label : labels) labels_by_root[root_of.at(label.node)].insert(label.name);
    for (const SheetPinRec& pin : sheet_pins) labels_by_root[root_of.at(pin.node)].insert(pin.name);
    for (const auto& kv : member_anchor_name) labels_by_root[root_of.at(kv.first)].insert(kv.second);
    for (const auto& kv : display_by_root) {
        const NameChoice& choice = names.at(kv.first);
        if (choice.priority > 0 && kv.second != choice.base) {
            labels_by_root[kv.first].insert(choice.base);
            labels_by_root[kv.first].insert(kv.second);
        }
    }

    std::map<Node, std::set<std::string>> buses_by_root;
    for (const auto& kv : member_anchor_buses) {
        const Node root = root_of.at(kv.first);
        buses_by_root[root].insert(kv.second.begin(), kv.second.end());
    }

    std::sort(pin_records.begin(), pin_records.end(), [](const PinRec& a, const PinRec& b) {
        const std::string ar = a.ref.empty() ? "~" : a.ref;
        const std::string br = b.ref.empty() ? "~" : b.ref;
        if (ar != br) return ar < br;
        if (a.sym != b.sym) return a.sym < b.sym;
        if (a.pin != b.pin) return a.pin < b.pin;
        return a.node < b.node;
    });

    struct NetMember {
        std::string sym, pin, ref, type, sheet;
        Node node;
    };
    std::map<Node, std::vector<NetMember>> members_by_root;
    std::map<std::string, std::string> by_pin;
    std::map<std::string, std::map<std::string, std::string>> pins_by_symbol;
    for (const PinRec& pin : pin_records) {
        const Node root = root_of.at(pin.node);
        const std::string& display = display_by_root.at(root);
        by_pin[pin.sym + "." + pin.pin] = display;
        auto& symbol_pins = pins_by_symbol[pin.sym];
        auto existing = symbol_pins.find(pin.pin);
        if (existing == symbol_pins.end() || existing->second.empty()) symbol_pins[pin.pin] = display;
        members_by_root[root].push_back(
            NetMember{pin.sym, pin.pin, pin.ref, pin.type, pin.sheet, pin.node});
    }

    json nets_out = json::object();
    json net_info_out = json::object();
    const std::set<std::string> driver_types{"bidirectional", "open_collector", "open_emitter",
                                             "output", "power_out", "tri_state"};
    std::map<Node, bool> power_source_by_root;
    for (const PowerRec& power : powers)
        if (power.symbol == "PWR_FLAG") power_source_by_root[root_of.at(power.node)] = true;

    std::size_t net_index = 1;
    for (const auto& class_entry : classes) {
        const Node& root = class_entry.first;
        std::vector<NetMember> members = members_by_root[root];
        std::sort(members.begin(), members.end(), [](const NetMember& a, const NetMember& b) {
            const std::string ar = a.ref.empty() ? "~" : a.ref;
            const std::string br = b.ref.empty() ? "~" : b.ref;
            if (ar != br) return ar < br;
            if (a.sym != b.sym) return a.sym < b.sym;
            return a.pin < b.pin;
        });
        json member_json = json::array();
        std::set<std::string> drivers;
        for (const NetMember& member : members) {
            member_json.push_back(json{{"sym", member.sym},
                                       {"pin", member.pin},
                                       {"ref", member.ref},
                                       {"type", member.type},
                                       {"xy", json::array({mm(member.node.x), mm(member.node.y)})},
                                       {"sheet", member.sheet}});
            if (driver_types.count(member.type)) drivers.insert(member.type);
            if (member.type == "power_out") power_source_by_root[root] = true;
        }
        const std::string& display = display_by_root.at(root);
        nets_out[display] = member_json;

        json sheets = json::array();
        for (const std::string& sheet : sheets_by_root[root]) sheets.push_back(sheet);
        json label_values = json::array();
        for (const std::string& label : labels_by_root[root]) label_values.push_back(label);
        json driver_values = json::array();
        for (const std::string& driver : drivers) driver_values.push_back(driver);
        json bus = nullptr;
        if (!buses_by_root[root].empty()) bus = *buses_by_root[root].begin();
        net_info_out[display] = json{{"id", "n" + std::to_string(net_index++)},
                                     {"sheets", sheets},
                                     {"labels", label_values},
                                     {"bus", bus},
                                     {"members", static_cast<int>(members.size())},
                                     {"drivers", driver_values},
                                     {"has_power_source", power_source_by_root[root]}};
    }

    json components = json::array();
    std::map<std::string, std::vector<const json*>> component_groups;
    for (const json* symbol : sorted_entities(sch, "symbols")) {
        const std::string ref = symbol->value("ref", std::string());
        const std::string sid = symbol->value("id", std::string());
        const bool annotated = !ref.empty() && ref.find('?') == std::string::npos;
        component_groups[annotated ? "ref:" + ref : "sym:" + sid].push_back(symbol);
    }
    std::vector<std::vector<const json*>> sorted_groups;
    for (auto& kv : component_groups) {
        std::vector<const json*>& group = kv.second;
        std::sort(group.begin(), group.end(), [](const json* a, const json* b) {
            return a->value("id", std::string()) < b->value("id", std::string());
        });
        sorted_groups.push_back(group);
    }
    std::sort(sorted_groups.begin(), sorted_groups.end(), [](const auto& a, const auto& b) {
        const std::string ar = a.front()->value("ref", std::string());
        const std::string br = b.front()->value("ref", std::string());
        const std::string ark = ar.empty() ? "~" : ar;
        const std::string brk = br.empty() ? "~" : br;
        if (ark != brk) return ark < brk;
        return a.front()->value("id", std::string()) < b.front()->value("id", std::string());
    });

    for (const std::vector<const json*>& group : sorted_groups) {
        const json& first = *group.front();
        const std::string ref = first.value("ref", std::string());
        const std::string libid = first.value("libid", std::string());
        const std::string value = first.value("value", std::string());
        bool mismatch = false;
        bool dnp = false;
        bool in_bom = true;
        bool on_board = true;
        json fields = json::object();
        json syms = json::array();
        std::map<std::string, std::string> merged_pins;
        for (const json* symbol : group) {
            const std::string sid = symbol->value("id", std::string());
            syms.push_back(sid);
            if (symbol->value("libid", std::string()) != libid ||
                symbol->value("value", std::string()) != value)
                mismatch = true;
            dnp = dnp || jbool(*symbol, "dnp", false);
            in_bom = in_bom && jbool(*symbol, "in_bom", true);
            on_board = on_board && jbool(*symbol, "on_board", true);
            if (symbol->contains("fields") && (*symbol)["fields"].is_object()) {
                std::vector<std::string> keys;
                for (auto it = (*symbol)["fields"].begin(); it != (*symbol)["fields"].end(); ++it)
                    keys.push_back(it.key());
                std::sort(keys.begin(), keys.end());
                for (const std::string& key : keys)
                    if (!fields.contains(key)) fields[key] = (*symbol)["fields"][key];
            }
            auto pin_map = pins_by_symbol.find(sid);
            if (pin_map != pins_by_symbol.end()) {
                for (const auto& pin : pin_map->second) {
                    auto existing = merged_pins.find(pin.first);
                    if (existing == merged_pins.end() || existing->second.empty())
                        merged_pins[pin.first] = pin.second;
                }
            }
        }
        if (mismatch)
            warnings.push_back("multi_unit_mismatch: component " + ref +
                               " has conflicting libid or value across units");
        json pins = json::object();
        for (const auto& pin : merged_pins) pins[pin.first] = pin.second;
        std::string datasheet;
        if (fields.contains("Datasheet")) datasheet = jstr(fields["Datasheet"]);
        if (datasheet.empty() && fields.contains("datasheet")) datasheet = jstr(fields["datasheet"]);
        components.push_back(json{{"ref", ref},
                                  {"libid", libid},
                                  {"value", value},
                                  {"footprint", first.value("footprint", std::string())},
                                  {"sym", first.value("id", std::string())},
                                  {"syms", syms},
                                  {"pins", pins},
                                  {"fields", fields},
                                  {"dnp", dnp},
                                  {"in_bom", in_bom},
                                  {"on_board", on_board},
                                  {"datasheet", datasheet},
                                  {"units", static_cast<int>(group.size())}});
    }

    json by_pin_out = json::object();
    for (const auto& kv : by_pin) by_pin_out[kv.first] = kv.second;

    json by_object_out = json::object();
    for (const auto& kv : scalar_objects) {
        auto root = root_of.find(kv.second);
        if (root != root_of.end()) by_object_out[kv.first] = display_by_root.at(root->second);
    }
    for (const auto& kv : resolved_entry_nodes)
        by_object_out[kv.first] = display_by_root.at(root_of.at(kv.second));
    for (const auto& kv : direct_object_names) by_object_out[kv.first] = kv.second;
    for (const json* marker : sorted_entities(sch, "no_connect")) {
        const std::string id = marker->value("id", std::string());
        const Node point = snap(marker->value("sheet", std::string()),
                                marker->contains("pos") ? (*marker)["pos"]
                                                        : json::array({0, 0}));
        std::string net;
        for (const PinRec& pin : pin_records) {
            if (pin.node == point) {
                net = display_by_root.at(root_of.at(pin.node));
                break;
            }
        }
        by_object_out[id] = net;
    }

    json buses_out = json::object();
    for (const auto& kv : bus_catalog) {
        json members = json::array();
        for (const std::string& member : kv.second) members.push_back(member);
        buses_out[kv.first] = members;
    }

    std::sort(warnings.begin(), warnings.end());
    warnings.erase(std::unique(warnings.begin(), warnings.end()), warnings.end());
    json warnings_out = json::array();
    for (const std::string& warning : warnings) warnings_out.push_back(warning);

    return json{{"nets", nets_out},
                {"by_pin", by_pin_out},
                {"by_object", by_object_out},
                {"components", components},
                {"net_info", net_info_out},
                {"buses", buses_out},
                {"warnings", warnings_out}};
}

}  // namespace chudware::ecad
