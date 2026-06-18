// Netlist builder implementation — see netlist.h. Mirrors engine/ecad/netlist.py
// exactly: snap tolerance, union-find root rule, naming priority + ordering, and
// the {nets, by_pin, components, warnings} shape. OCCT-free; pure JSON.

#include "ecad/netlist.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "ecad/schema.h"

namespace aicad::ecad {

using nlohmann::json;

namespace {

// Grid snap tolerance (mm) — netlist.md §2.1 fixes this at 1e-6.
constexpr double kSnapTol = 1e-6;
constexpr double kSnapInv = 1.0 / kSnapTol;  // 1e6

// A canonical snapped point. Comparison is lexicographic on (x, y) — matches
// Python's tuple ordering used by the union-find root rule and net sort.
struct Point {
    double x = 0.0;
    double y = 0.0;
    bool operator==(const Point& o) const { return x == o.x && y == o.y; }
    bool operator<(const Point& o) const { return x < o.x || (x == o.x && y < o.y); }
    bool operator<=(const Point& o) const { return !(o < *this); }
};

// Python round() is round-half-to-even (banker's). std::round is round-half-away.
// Replicate Python's banker's rounding for byte-exact snapping.
double py_round(double v) {
    double fl = std::floor(v);
    double diff = v - fl;
    if (diff < 0.5) return fl;
    if (diff > 0.5) return fl + 1.0;
    // exactly .5 -> round to even
    double ev = std::fmod(fl, 2.0);
    return (ev == 0.0) ? fl : fl + 1.0;
}

double get_num(const json& v, double dflt = 0.0) {
    return v.is_number() ? v.get<double>() : dflt;
}

// Snap an [x, y] (json array) to the grid; "+ 0.0" normalizes -0.0 -> 0.0.
Point snap(const json& p) {
    double px = (p.is_array() && p.size() >= 1) ? get_num(p[0]) : 0.0;
    double py = (p.is_array() && p.size() >= 2) ? get_num(p[1]) : 0.0;
    Point out;
    out.x = py_round(px * kSnapInv) / kSnapInv + 0.0;
    out.y = py_round(py * kSnapInv) / kSnapInv + 0.0;
    return out;
}

std::string jstr(const json& v, const std::string& dflt = "") {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number()) {
        // mirror str(number) only where the oracle does str() on a possibly-numeric field;
        // pin numbers etc. are normally strings, so this is a rare fallback.
        return v.dump();
    }
    return dflt;
}

// ── union-find over snapped points ───────────────────────────────────────────
// Root rule: the lexicographically-smaller point always wins, so a class's root is
// its minimum point regardless of union order (matches netlist.py _UnionFind).
class UnionFind {
public:
    void add(const Point& p) {
        if (parent_.find(p) == parent_.end()) parent_[p] = p;
    }

    Point find(const Point& p) {
        add(const_cast<Point&>(p));
        Point root = p;
        while (!(parent_[root] == root)) root = parent_[root];
        // path compression
        Point cur = p;
        while (!(parent_[cur] == root)) {
            Point nxt = parent_[cur];
            parent_[cur] = root;
            cur = nxt;
        }
        return root;
    }

    void unite(const Point& a, const Point& b) {
        Point ra = find(a), rb = find(b);
        if (ra == rb) return;
        Point lo = (ra <= rb) ? ra : rb;
        Point hi = (ra <= rb) ? rb : ra;
        parent_[hi] = lo;
    }

    // root -> sorted member points (deterministic).
    std::map<Point, std::vector<Point>> classes() {
        std::map<Point, std::vector<Point>> out;
        // collect keys first (find() mutates the map)
        std::vector<Point> keys;
        keys.reserve(parent_.size());
        for (auto& kv : parent_) keys.push_back(kv.first);
        for (const Point& p : keys) out[find(p)].push_back(p);
        for (auto& kv : out) std::sort(kv.second.begin(), kv.second.end());
        return out;
    }

private:
    std::map<Point, Point> parent_;
};

// True if point p lies on the closed segment a-b (within snap tolerance).
bool on_segment(const Point& p, const Point& a, const Point& b) {
    double abx = b.x - a.x, aby = b.y - a.y;
    double apx = p.x - a.x, apy = p.y - a.y;
    double seg_len = std::sqrt(abx * abx + aby * aby);
    if (seg_len <= kSnapTol) return p == a;  // degenerate segment: coincidence test
    double cross = abx * apy - aby * apx;
    if (std::fabs(cross) > kSnapTol * seg_len) return false;
    double dot = apx * abx + apy * aby;
    return (-kSnapTol * seg_len <= dot) && (dot <= seg_len * seg_len + kSnapTol * seg_len);
}

}  // namespace

json build_netlist(const json& sch) {
    std::vector<std::string> warnings;
    if (!schema::is_schematic(sch)) {
        std::string sval = sch.contains("schema") ? sch["schema"].dump() : "null";
        return json{{"nets", json::object()},
                    {"by_pin", json::object()},
                    {"components", json::array()},
                    {"warnings", json::array({"not a schematic document (schema=" + sval + ")"})}};
    }

    UnionFind uf;

    // ── §2.2 Wires ────────────────────────────────────────────────────────────
    // wire_pts: wire id -> snapped vertex list (sorted by wire id)
    std::map<std::string, std::vector<Point>> wire_pts;
    std::map<std::string, std::string> wire_sheet;

    {
        // sort wires by id
        std::vector<const json*> wires;
        if (sch.contains("wires") && sch["wires"].is_array())
            for (const json& w : sch["wires"]) wires.push_back(&w);
        std::sort(wires.begin(), wires.end(), [](const json* a, const json* b) {
            return a->value("id", std::string()) < b->value("id", std::string());
        });
        for (const json* wp : wires) {
            const json& w = *wp;
            std::string wid = w.value("id", std::string());
            std::string sheet = w.value("sheet", std::string());
            std::vector<Point> pts;
            if (w.contains("pts") && w["pts"].is_array())
                for (const json& p : w["pts"]) pts.push_back(snap(p));
            wire_pts[wid] = pts;
            wire_sheet[wid] = sheet;
            for (const Point& p : pts) uf.add(p);
            for (size_t i = 0; i + 1 < pts.size(); ++i) uf.unite(pts[i], pts[i + 1]);
        }
    }

    // ── §2.2(b) Junctions (sorted by id) ───────────────────────────────────────
    {
        std::vector<const json*> junctions;
        if (sch.contains("junctions") && sch["junctions"].is_array())
            for (const json& j : sch["junctions"]) junctions.push_back(&j);
        std::sort(junctions.begin(), junctions.end(), [](const json* a, const json* b) {
            return a->value("id", std::string()) < b->value("id", std::string());
        });
        for (const json* jp : junctions) {
            const json& j = *jp;
            std::string sheet = j.value("sheet", std::string());
            Point jpt = snap(j.contains("pos") ? j["pos"] : json::array({0, 0}));
            uf.add(jpt);
            // iterate wires in sorted(wire_pts) order (map is already sorted by id)
            for (auto& kv : wire_pts) {
                const std::string& wid = kv.first;
                if (wire_sheet[wid] != sheet) continue;
                const std::vector<Point>& pts = kv.second;
                for (size_t i = 0; i + 1 < pts.size(); ++i) {
                    if (on_segment(jpt, pts[i], pts[i + 1])) {
                        uf.unite(jpt, pts[i]);
                        uf.unite(jpt, pts[i + 1]);
                    }
                }
            }
        }
    }

    // ── §2.3 Pins ───────────────────────────────────────────────────────────────
    struct PinRec {
        std::string sym, ref, pin, name, type, sheet, libid;
        Point pt;
    };
    std::vector<PinRec> pin_records;
    // iter_placed_pins, inlined (doc symbol order, then symbol-def pin order; matches
    // engine/ecad/schema.py::iter_placed_pins). No dangling refs — values captured here.
    if (sch.contains("symbols") && sch["symbols"].is_array() && sch.contains("lib_cache") &&
        sch["lib_cache"].is_object()) {
        const json& cache = sch["lib_cache"];
        for (const json& sym : sch["symbols"]) {
            std::string libid = sym.value("libid", std::string());
            if (!cache.contains(libid) || !cache[libid].is_object()) continue;
            const json& sdef = cache[libid];
            int unit = sym.contains("unit") && sym["unit"].is_number_integer() ? sym["unit"].get<int>()
                                                                               : 1;
            if (!sdef.contains("pins") || !sdef["pins"].is_array()) continue;
            for (const json& pin : sdef["pins"]) {
                int punit =
                    pin.contains("unit") && pin["unit"].is_number_integer() ? pin["unit"].get<int>()
                                                                            : 1;
                if (punit != unit && punit != 0) continue;
                auto [wx, wy] = schema::pin_world(sym, pin);
                Point p = snap(json::array({wx, wy}));
                uf.add(p);
                PinRec rec;
                rec.sym = sym.value("id", std::string());
                rec.ref = sym.value("ref", std::string());
                rec.pin = jstr(pin.contains("number") ? pin["number"] : json(""));
                rec.name = jstr(pin.contains("name") ? pin["name"] : json(""));
                rec.type = pin.contains("type") ? jstr(pin["type"], "passive") : "passive";
                rec.sheet = sym.value("sheet", std::string());
                rec.pt = p;
                rec.libid = libid;
                pin_records.push_back(rec);
            }
        }
    }

    // attach an attach-point (label/power/sheet-pin) to any wire it lies on (same sheet)
    auto attach_to_wires = [&](const Point& pt, const std::string& sheet) {
        for (auto& kv : wire_pts) {
            const std::string& wid = kv.first;
            if (wire_sheet[wid] != sheet) continue;
            const std::vector<Point>& pts = kv.second;
            for (size_t i = 0; i + 1 < pts.size(); ++i) {
                if (on_segment(pt, pts[i], pts[i + 1])) {
                    uf.unite(pt, pts[i]);
                    uf.unite(pt, pts[i + 1]);
                }
            }
        }
    };

    // ── §2.4 Labels (sorted by id) ────────────────────────────────────────────
    struct LabelPt {
        std::string name, scope, sheet;
        Point pt;
    };
    std::vector<LabelPt> label_points;
    // group key -> anchor points; gkey = (name, "*global*") or (name, "sheet:<sheet>")
    std::map<std::string, std::vector<Point>> label_groups;
    // hier_label_pts[(sheet, name)] -> first anchor (labels sorted by id)
    std::map<std::pair<std::string, std::string>, Point> hier_label_pts;
    {
        std::vector<const json*> labels;
        if (sch.contains("labels") && sch["labels"].is_array())
            for (const json& lb : sch["labels"]) labels.push_back(&lb);
        std::sort(labels.begin(), labels.end(), [](const json* a, const json* b) {
            return a->value("id", std::string()) < b->value("id", std::string());
        });
        for (const json* lp : labels) {
            const json& lb = *lp;
            std::string name = jstr(lb.contains("text") ? lb["text"] : json(""));
            if (name.empty()) continue;
            std::string scope = lb.contains("scope") ? jstr(lb["scope"], "local") : "local";
            std::string sheet = lb.value("sheet", std::string());
            Point lpt = snap(lb.contains("pos") ? lb["pos"] : json::array({0, 0}));
            uf.add(lpt);
            attach_to_wires(lpt, sheet);
            std::string gkey;
            if (scope == "global")
                gkey = name + std::string(1, '\0') + "*global*";
            else
                gkey = name + std::string(1, '\0') + "sheet:" + sheet;
            label_groups[gkey].push_back(lpt);
            label_points.push_back(LabelPt{name, scope, sheet, lpt});
            if (scope == "hier") {
                auto key = std::make_pair(sheet, name);
                if (hier_label_pts.find(key) == hier_label_pts.end()) hier_label_pts[key] = lpt;
            }
        }
        // union within each group (sorted group keys; anchors sorted)
        for (auto& kv : label_groups) {
            std::vector<Point> anchors = kv.second;
            std::sort(anchors.begin(), anchors.end());
            for (size_t i = 1; i < anchors.size(); ++i) uf.unite(anchors[0], anchors[i]);
        }
    }

    // ── §2.4(b) Hierarchy bridge: sheet pins (sorted by id, pins by name) ──────
    struct SheetPinName {
        std::string name, sheet;
        Point pt;
    };
    std::vector<SheetPinName> sheet_pin_names;
    {
        std::vector<const json*> sheet_symbols;
        if (sch.contains("sheet_symbols") && sch["sheet_symbols"].is_array())
            for (const json& hs : sch["sheet_symbols"]) sheet_symbols.push_back(&hs);
        std::sort(sheet_symbols.begin(), sheet_symbols.end(), [](const json* a, const json* b) {
            return a->value("id", std::string()) < b->value("id", std::string());
        });
        for (const json* hp : sheet_symbols) {
            const json& hs = *hp;
            std::string parent_sheet = hs.value("sheet", std::string());
            std::string child = hs.value("child", std::string());
            std::vector<const json*> pins;
            if (hs.contains("pins") && hs["pins"].is_array())
                for (const json& pn : hs["pins"]) pins.push_back(&pn);
            std::stable_sort(pins.begin(), pins.end(), [](const json* a, const json* b) {
                return jstr(a->contains("name") ? (*a)["name"] : json("")) <
                       jstr(b->contains("name") ? (*b)["name"] : json(""));
            });
            for (const json* pnp : pins) {
                const json& pn = *pnp;
                std::string name = jstr(pn.contains("name") ? pn["name"] : json(""));
                if (name.empty()) continue;
                Point ppt = snap(pn.contains("pos") ? pn["pos"] : json::array({0, 0}));
                uf.add(ppt);
                attach_to_wires(ppt, parent_sheet);
                sheet_pin_names.push_back(SheetPinName{name, parent_sheet, ppt});
                auto key = std::make_pair(child, name);
                auto it = hier_label_pts.find(key);
                if (it != hier_label_pts.end()) uf.unite(ppt, it->second);
            }
        }
    }

    // ── §2.5 Power ports (sorted by id) ────────────────────────────────────────
    struct PowerPt {
        std::string net;
        Point pt;
    };
    std::vector<PowerPt> power_points;
    std::map<std::string, std::vector<Point>> power_groups;
    {
        std::vector<const json*> power;
        if (sch.contains("power") && sch["power"].is_array())
            for (const json& pw : sch["power"]) power.push_back(&pw);
        std::sort(power.begin(), power.end(), [](const json* a, const json* b) {
            return a->value("id", std::string()) < b->value("id", std::string());
        });
        for (const json* pwp : power) {
            const json& pw = *pwp;
            std::string net = jstr(pw.contains("net") ? pw["net"] : json(""));
            if (net.empty()) continue;
            Point ppt = snap(pw.contains("pos") ? pw["pos"] : json::array({0, 0}));
            uf.add(ppt);
            attach_to_wires(ppt, pw.value("sheet", std::string()));
            power_groups[net].push_back(ppt);
            power_points.push_back(PowerPt{net, ppt});
        }
        for (auto& kv : power_groups) {
            std::vector<Point> anchors = kv.second;
            std::sort(anchors.begin(), anchors.end());
            for (size_t i = 1; i < anchors.size(); ++i) uf.unite(anchors[0], anchors[i]);
        }
    }

    // ── §2.6 Buses (parse + expand only) ───────────────────────────────────────
    {
        std::vector<const json*> buses;
        if (sch.contains("buses") && sch["buses"].is_array())
            for (const json& b : sch["buses"]) buses.push_back(&b);
        std::sort(buses.begin(), buses.end(), [](const json* a, const json* b) {
            return a->value("id", std::string()) < b->value("id", std::string());
        });
        for (const json* bp : buses) {
            const json& bus = *bp;
            // expand_bus_members
            std::vector<std::string> members;
            const json& spec = bus.contains("members") ? bus["members"] : json::array();
            if (spec.is_array()) {
                for (const json& m : spec) members.push_back(jstr(m));
            } else if (spec.is_string()) {
                std::string s = spec.get<std::string>();
                // match ^(.*?)\[(\d+)\.\.(\d+)\]$
                size_t lb = s.rfind('[');
                bool matched = false;
                if (lb != std::string::npos && s.size() > lb + 1 && s.back() == ']') {
                    std::string inner = s.substr(lb + 1, s.size() - lb - 2);
                    size_t dd = inner.find("..");
                    if (dd != std::string::npos) {
                        std::string los = inner.substr(0, dd), his = inner.substr(dd + 2);
                        auto all_digits = [](const std::string& t) {
                            if (t.empty()) return false;
                            for (char c : t)
                                if (c < '0' || c > '9') return false;
                            return true;
                        };
                        if (all_digits(los) && all_digits(his)) {
                            std::string base = s.substr(0, lb);
                            int lo = std::stoi(los), hi = std::stoi(his);
                            int step = (hi >= lo) ? 1 : -1;
                            for (int i = lo; i != hi + step; i += step)
                                members.push_back(base + std::to_string(i));
                            matched = true;
                        }
                    }
                }
                if (!matched) members.push_back(s);
            } else {
                members.push_back(jstr(spec));
            }
            if (!members.empty()) {
                std::string joined;
                for (size_t i = 0; i < members.size(); ++i) {
                    if (i) joined += ", ";
                    joined += members[i];
                }
                std::string bid = bus.contains("id") ? bus.value("id", std::string("?")) : "?";
                warnings.push_back("bus " + bid + ": expanded " + std::to_string(members.size()) +
                                   " member(s) [" + joined +
                                   "]; bus routing is a v1 follow-up (parse-only)");
            }
        }
    }

    // ── §2.7 Name each class + emit ─────────────────────────────────────────────
    auto classes = uf.classes();  // sorted root -> sorted members

    // point -> class root
    std::map<Point, Point> root_of;
    for (auto& kv : classes)
        for (const Point& m : kv.second) root_of[m] = kv.first;

    auto root_of_get = [&](const Point& p, Point& out) -> bool {
        auto it = root_of.find(p);
        if (it == root_of.end()) return false;
        out = it->second;
        return true;
    };

    // power name: sorted power_points by (pt, net); first per root wins
    std::map<Point, std::string> power_name_for;
    {
        std::vector<PowerPt> sorted_pp = power_points;
        std::sort(sorted_pp.begin(), sorted_pp.end(), [](const PowerPt& a, const PowerPt& b) {
            if (a.pt < b.pt) return true;
            if (b.pt < a.pt) return false;
            return a.net < b.net;
        });
        for (const PowerPt& rec : sorted_pp) {
            Point root;
            if (!root_of_get(rec.pt, root)) continue;
            if (power_name_for.find(root) == power_name_for.end()) power_name_for[root] = rec.net;
        }
    }

    // label names: sorted label_points by (pt, name); global vs local
    std::map<Point, std::string> global_label_for;
    std::map<Point, std::string> local_label_for;
    {
        std::vector<LabelPt> sorted_lp = label_points;
        std::sort(sorted_lp.begin(), sorted_lp.end(), [](const LabelPt& a, const LabelPt& b) {
            if (a.pt < b.pt) return true;
            if (b.pt < a.pt) return false;
            return a.name < b.name;
        });
        for (const LabelPt& rec : sorted_lp) {
            Point root;
            if (!root_of_get(rec.pt, root)) continue;
            if (rec.scope == "global") {
                if (global_label_for.find(root) == global_label_for.end())
                    global_label_for[root] = rec.name;
            } else {
                if (local_label_for.find(root) == local_label_for.end())
                    local_label_for[root] = rec.name;
            }
        }
    }

    // sheet-pin names: sorted by (pt, name); setdefault into local_label_for
    {
        std::vector<SheetPinName> sorted_sp = sheet_pin_names;
        std::sort(sorted_sp.begin(), sorted_sp.end(),
                  [](const SheetPinName& a, const SheetPinName& b) {
                      if (a.pt < b.pt) return true;
                      if (b.pt < a.pt) return false;
                      return a.name < b.name;
                  });
        for (const SheetPinName& rec : sorted_sp) {
            Point root;
            if (!root_of_get(rec.pt, root)) continue;
            if (local_label_for.find(root) == local_label_for.end())
                local_label_for[root] = rec.name;
        }
    }

    // assign final names; auto N$<n> over unnamed roots in sorted order
    std::map<Point, std::string> name_of_root;
    std::vector<Point> auto_roots;
    for (auto& kv : classes) {
        const Point& root = kv.first;
        if (power_name_for.count(root))
            name_of_root[root] = power_name_for[root];
        else if (global_label_for.count(root))
            name_of_root[root] = global_label_for[root];
        else if (local_label_for.count(root))
            name_of_root[root] = local_label_for[root];
        else
            auto_roots.push_back(root);
    }
    for (size_t n = 0; n < auto_roots.size(); ++n)
        name_of_root[auto_roots[n]] = "N$" + std::to_string(n + 1);

    // duplicate-name warning (sorted by name)
    {
        std::map<std::string, std::set<Point>> name_to_roots;
        for (auto& kv : name_of_root) name_to_roots[kv.second].insert(kv.first);
        for (auto& kv : name_to_roots) {
            if (kv.second.size() > 1) {
                warnings.push_back("net name '" + kv.first + "' resolves to " +
                                   std::to_string(kv.second.size()) +
                                   " distinct nets (shorted-by-name without a wire); see ERC "
                                   "multiple_nets_label");
            }
        }
    }

    // ── build nets + by_pin from pins ──────────────────────────────────────────
    // pin_records sorted by (ref or "~", sym, pin, pt)
    std::sort(pin_records.begin(), pin_records.end(), [](const PinRec& a, const PinRec& b) {
        std::string ra = a.ref.empty() ? "~" : a.ref;
        std::string rb = b.ref.empty() ? "~" : b.ref;
        if (ra != rb) return ra < rb;
        if (a.sym != b.sym) return a.sym < b.sym;
        if (a.pin != b.pin) return a.pin < b.pin;
        return a.pt < b.pt;
    });

    // by_pin (sorted), nets (member dicts), pins_by_sym
    std::map<std::string, std::string> by_pin;
    std::map<std::string, std::vector<json>> nets_raw;  // net -> member dicts (with sort key fields)
    std::map<std::string, std::map<std::string, std::string>> pins_by_sym;

    struct NetMember {
        std::string sym, pin, ref, type;
        double x, y;
    };
    std::map<std::string, std::vector<NetMember>> nets_members;

    for (const PinRec& rec : pin_records) {
        std::string pinref = rec.sym + "." + rec.pin;
        std::string net;
        Point root;
        if (root_of_get(rec.pt, root)) {
            auto it = name_of_root.find(root);
            if (it != name_of_root.end()) net = it->second;
        }
        by_pin[pinref] = net;
        pins_by_sym[rec.sym][rec.pin] = net;
        if (!net.empty())
            nets_members[net].push_back(
                NetMember{rec.sym, rec.pin, rec.ref, rec.type, rec.pt.x, rec.pt.y});
    }

    // emit nets (sorted net names; members sorted by (ref or "~", sym, pin))
    json nets_out = json::object();
    for (auto& kv : nets_members) {
        std::vector<NetMember> members = kv.second;
        std::sort(members.begin(), members.end(), [](const NetMember& a, const NetMember& b) {
            std::string ra = a.ref.empty() ? "~" : a.ref;
            std::string rb = b.ref.empty() ? "~" : b.ref;
            if (ra != rb) return ra < rb;
            if (a.sym != b.sym) return a.sym < b.sym;
            return a.pin < b.pin;
        });
        json arr = json::array();
        for (const NetMember& m : members)
            arr.push_back(json{{"sym", m.sym},
                               {"pin", m.pin},
                               {"ref", m.ref},
                               {"type", m.type},
                               {"xy", json::array({m.x, m.y})}});
        nets_out[kv.first] = arr;
    }

    // components (one per symbol, sorted by (ref or "~", id))
    json components = json::array();
    {
        std::vector<const json*> syms;
        if (sch.contains("symbols") && sch["symbols"].is_array())
            for (const json& s : sch["symbols"]) syms.push_back(&s);
        // Python sort key: (s.get("ref", "~"), s.get("id", "")). absent ref => "~",
        // present-but-empty ref => "".
        auto refkey = [](const json* s) -> std::string {
            if (s->contains("ref")) return s->value("ref", std::string());
            return "~";
        };
        std::sort(syms.begin(), syms.end(), [&](const json* a, const json* b) {
            std::string ra = refkey(a), rb = refkey(b);
            if (ra != rb) return ra < rb;
            return a->value("id", std::string()) < b->value("id", std::string());
        });
        for (const json* sp : syms) {
            const json& sym = *sp;
            std::string sid = sym.value("id", std::string());
            json pins = json::object();  // sorted by key (std::map ordering)
            auto pit = pins_by_sym.find(sid);
            if (pit != pins_by_sym.end())
                for (auto& kv : pit->second) pins[kv.first] = kv.second;
            json fields = json::object();
            if (sym.contains("fields") && sym["fields"].is_object()) {
                std::map<std::string, json> sorted_fields;
                for (auto it = sym["fields"].begin(); it != sym["fields"].end(); ++it)
                    sorted_fields[it.key()] = it.value();
                for (auto& kv : sorted_fields) fields[kv.first] = kv.second;
            }
            components.push_back(json{
                {"ref", sym.value("ref", std::string())},
                {"libid", sym.value("libid", std::string())},
                {"value", sym.value("value", std::string())},
                {"footprint", sym.value("footprint", std::string())},
                {"sym", sid},
                {"pins", pins},
                {"fields", fields},
                {"dnp", sym.contains("dnp") ? sym["dnp"].get<bool>() : false}});
        }
    }

    // by_pin sorted (std::map already sorted)
    json by_pin_out = json::object();
    for (auto& kv : by_pin) by_pin_out[kv.first] = kv.second;

    std::sort(warnings.begin(), warnings.end());
    json warn_out = json::array();
    for (const std::string& w : warnings) warn_out.push_back(w);

    return json{{"nets", nets_out},
                {"by_pin", by_pin_out},
                {"components", components},
                {"warnings", warn_out}};
}

}  // namespace aicad::ecad
