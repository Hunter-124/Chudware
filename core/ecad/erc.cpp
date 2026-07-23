// ERC implementation — see erc.h. Mirrors engine/ecad/erc.py exactly: the pin-type
// pair matrix, the per-pin / per-net / per-symbol / hierarchy rules, the violation
// record shape, and the final deterministic sort. OCCT-free; pure JSON.

#include "ecad/erc.h"

#include <algorithm>
#include <cmath>
#include <map>
#include <set>
#include <string>
#include <utility>
#include <vector>

#include "ecad/schema.h"

namespace chudware::ecad {

using nlohmann::json;

namespace {

// pin classes for the net-level aggregate rules
const std::set<std::string>& drivers_set() {
    static const std::set<std::string> s{"output",         "power_out",  "bidirectional", "tri_state",
                                         "open_collector", "open_emitter", "passive"};
    return s;
}
const std::set<std::string>& strong_drivers() {
    static const std::set<std::string> s{"output", "power_out"};
    return s;
}
const std::set<std::string>& sinks_set() {
    static const std::set<std::string> s{"input", "power_in"};
    return s;
}
const std::set<std::string>& power_sources() {
    static const std::set<std::string> s{"power_out"};
    return s;
}

// Pairwise severity matrix. Symmetric; unspecified/free always compatible.
std::string pair_severity(const std::string& a, const std::string& b) {
    if (a == "unspecified" || a == "free" || b == "unspecified" || b == "free") return "";
    std::string lo = (a <= b) ? a : b;
    std::string hi = (a <= b) ? b : a;
    // _PAIR entries (canonicalized sorted):
    //   output,output -> error ; output,power_out -> error ; power_out,power_out -> error
    //   output,bidirectional -> warning
    if (lo == "output" && hi == "output") return "error";
    if (lo == "output" && hi == "power_out") return "error";
    if (lo == "power_out" && hi == "power_out") return "error";
    if (lo == "bidirectional" && hi == "output") return "warning";
    return "";
}

std::string jstr(const json& v, const std::string& dflt = "") {
    if (v.is_string()) return v.get<std::string>();
    if (v.is_number_integer()) return std::to_string(v.get<long long>());
    if (v.is_number()) return v.dump();
    return dflt;
}

double get_num(const json& v, double dflt = 0.0) { return v.is_number() ? v.get<double>() : dflt; }

double py_round(double v) {
    double fl = std::floor(v);
    double diff = v - fl;
    if (diff < 0.5) return fl;
    if (diff > 0.5) return fl + 1.0;
    double ev = std::fmod(fl, 2.0);
    return (ev == 0.0) ? fl : fl + 1.0;
}

std::pair<double, double> snap_xy(double x, double y) {
    return {py_round(x * 1e6) / 1e6 + 0.0, py_round(y * 1e6) / 1e6 + 0.0};
}

// A violation record + its sort key.
struct Viol {
    json v;  // the emitted dict
};

json make_violation(const std::string& rule, const std::string& severity, const json& net,
                    std::vector<std::string> pins, const json& xy, const std::string& message) {
    std::sort(pins.begin(), pins.end());
    json pins_arr = json::array();
    for (const std::string& p : pins) pins_arr.push_back(p);
    json xy_out;
    if (xy.is_array())
        xy_out = xy;
    else
        xy_out = nullptr;
    return json{{"rule", rule},     {"severity", severity}, {"net", net},
                {"pins", pins_arr}, {"xy", xy_out},          {"message", message}};
}

bool net_has_power_port(const json& sch, const std::string& net) {
    if (!sch.contains("power") || !sch["power"].is_array()) return false;
    for (const json& pw : sch["power"])
        if (jstr(pw.contains("net") ? pw["net"] : json("")) == net) return true;
    return false;
}

// iter_placed_pins parallel (symbol order, pin order). Returns pinref + type/xy/ref/name.
struct PinInfo {
    std::string type, ref, name;
    double x, y;
    bool has_xy = false;
};

void collect_pin_info(const json& doc, std::map<std::string, PinInfo>& pin_info,
                      std::map<std::string, bool>& pin_is_nc,
                      const std::set<std::pair<double, double>>& nc_set) {
    const json& cache = doc.contains("lib_cache") ? doc["lib_cache"] : json::object();
    if (!doc.contains("symbols") || !doc["symbols"].is_array()) return;
    for (const json& sym : doc["symbols"]) {
        std::string libid = sym.value("libid", std::string());
        if (!cache.contains(libid) || !cache[libid].is_object()) continue;
        const json& sdef = cache[libid];
        int unit = sym.contains("unit") && sym["unit"].is_number_integer() ? sym["unit"].get<int>() : 1;
        if (!sdef.contains("pins") || !sdef["pins"].is_array()) continue;
        for (const json& pin : sdef["pins"]) {
            int punit = pin.contains("unit") && pin["unit"].is_number_integer() ? pin["unit"].get<int>()
                                                                                : 1;
            if (punit != unit && punit != 0) continue;
            auto [wx, wy] = schema::pin_world(sym, pin);
            auto [kx, ky] = snap_xy(wx, wy);
            std::string num = jstr(pin.contains("number") ? pin["number"] : json(""));
            std::string pinref = sym.value("id", std::string()) + "." + num;
            PinInfo info;
            info.type = pin.contains("type") ? jstr(pin["type"], "passive") : "passive";
            info.ref = sym.value("ref", std::string());
            info.name = jstr(pin.contains("name") ? pin["name"] : json(""));
            info.x = kx;
            info.y = ky;
            info.has_xy = true;
            pin_info[pinref] = info;  // last write wins (matches dict assignment)
            pin_is_nc[pinref] = nc_set.count({kx, ky}) > 0;
        }
    }
}

}  // namespace

json run_erc(const json& sch, const json& netlist) {
    json out = json::array();
    if (!schema::is_schematic(sch)) return out;

    const json nets = netlist.contains("nets") ? netlist["nets"] : json::object();
    const json by_pin = netlist.contains("by_pin") ? netlist["by_pin"] : json::object();

    // ── no-connect points ──
    std::set<std::pair<double, double>> nc_set;
    if (sch.contains("no_connect") && sch["no_connect"].is_array()) {
        for (const json& nc : sch["no_connect"]) {
            if (nc.contains("pos") && nc["pos"].is_array() && nc["pos"].size() == 2) {
                auto [x, y] = snap_xy(get_num(nc["pos"][0]), get_num(nc["pos"][1]));
                nc_set.insert({x, y});
            }
        }
    }

    std::map<std::string, PinInfo> pin_info;
    std::map<std::string, bool> pin_is_nc;
    collect_pin_info(sch, pin_info, pin_is_nc, nc_set);

    auto net_size = [&](const std::string& net) -> int {
        if (net.empty() || !nets.contains(net) || !nets[net].is_array()) return 0;
        return (int)nets[net].size();
    };

    // ── per-pin rules (sorted by pinref) ──
    {
        std::vector<std::string> keys;
        for (auto it = by_pin.begin(); it != by_pin.end(); ++it) keys.push_back(it.key());
        std::sort(keys.begin(), keys.end());
        for (const std::string& pinref : keys) {
            std::string net = by_pin[pinref].is_string() ? by_pin[pinref].get<std::string>() : "";
            auto pit = pin_info.find(pinref);
            std::string ptype = (pit != pin_info.end()) ? pit->second.type : "passive";
            json xy = nullptr;
            if (pit != pin_info.end() && pit->second.has_xy)
                xy = json::array({pit->second.x, pit->second.y});
            int ns = net.empty() ? 0 : net_size(net);
            bool is_nc = pin_is_nc.count(pinref) ? pin_is_nc[pinref] : false;

            if ((net.empty() || ns <= 1) && ptype != "no_connect") {
                if (!is_nc) {
                    json netj = net.empty() ? json(nullptr) : json(net);
                    out.push_back(make_violation(
                        "pin_not_connected", "warning", netj, {pinref}, xy,
                        "pin " + pinref + " (" + ptype + ") is not connected to any net"));
                    if (!net.empty() && ns == 1) {
                        out.push_back(make_violation(
                            "floating_net_single_pin", "warning", json(net), {pinref}, xy,
                            "net " + net + " has a single pin (" + pinref +
                                ") — likely a wiring slip"));
                    }
                }
            }
            if (is_nc && !net.empty() && ns > 1) {
                out.push_back(make_violation(
                    "no_connect_connected", "error", json(net), {pinref}, xy,
                    "pin " + pinref + " is flagged no-connect but is wired into net " + net +
                        " (size " + std::to_string(ns) + ")"));
            }
        }
    }

    // ── per-net aggregate rules (sorted by net name) ──
    {
        std::vector<std::string> net_keys;
        for (auto it = nets.begin(); it != nets.end(); ++it) net_keys.push_back(it.key());
        std::sort(net_keys.begin(), net_keys.end());
        for (const std::string& net : net_keys) {
            const json& members = nets[net];
            std::vector<std::string> types, pinrefs;
            for (const json& m : members) {
                types.push_back(m.contains("type") ? jstr(m["type"], "passive") : "passive");
                pinrefs.push_back(jstr(m.contains("sym") ? m["sym"] : json("")) + "." +
                                  jstr(m.contains("pin") ? m["pin"] : json("")));
            }
            json net_xy = nullptr;
            if (!members.empty() && members[0].contains("xy")) net_xy = members[0]["xy"];

            std::vector<std::string> strong;
            int n_drivers = 0;
            std::vector<std::string> sinks_list;
            bool has_power_source = false;
            for (size_t i = 0; i < types.size(); ++i) {
                if (strong_drivers().count(types[i])) strong.push_back(pinrefs[i]);
                if (drivers_set().count(types[i])) ++n_drivers;
                if (sinks_set().count(types[i])) sinks_list.push_back(types[i]);
                if (power_sources().count(types[i])) has_power_source = true;
            }
            bool port_powered = net_has_power_port(sch, net);

            // conflicting_drivers
            if (strong.size() >= 2) {
                std::vector<std::string> ss = strong;
                std::sort(ss.begin(), ss.end());
                std::string joined;
                for (size_t i = 0; i < ss.size(); ++i) {
                    if (i) joined += ", ";
                    joined += ss[i];
                }
                out.push_back(make_violation(
                    "conflicting_drivers", "error", json(net), strong, net_xy,
                    "net " + net + " has " + std::to_string(strong.size()) + " strong drivers (" +
                        joined + ") — output-output short"));
            }

            // pairwise matrix scan (only warnings emitted as pin_type_conflict)
            for (size_t i = 0; i < types.size(); ++i) {
                for (size_t k = i + 1; k < types.size(); ++k) {
                    std::string sev = pair_severity(types[i], types[k]);
                    if (sev == "warning") {
                        out.push_back(make_violation(
                            "pin_type_conflict", "warning", json(net), {pinrefs[i], pinrefs[k]},
                            net_xy,
                            "pins " + pinrefs[i] + " (" + types[i] + ") and " + pinrefs[k] + " (" +
                                types[k] + ") are a questionable combination on net " + net));
                    }
                }
            }

            // power_not_driven
            bool any_power_in = false;
            for (const std::string& t : types)
                if (t == "power_in") any_power_in = true;
            if (any_power_in && !has_power_source && !port_powered) {
                std::vector<std::string> pin_in;
                for (size_t i = 0; i < types.size(); ++i)
                    if (types[i] == "power_in") pin_in.push_back(pinrefs[i]);
                out.push_back(make_violation(
                    "power_not_driven", "error", json(net), pin_in, net_xy,
                    "net " + net + " has power_in pin(s) but no power_out or power port to drive it"));
            }

            // no_driver
            if (!members.empty() && n_drivers == 0 && !port_powered &&
                sinks_list.size() == types.size() && !sinks_list.empty()) {
                std::set<std::string> uniq(types.begin(), types.end());
                std::string joined;
                bool first = true;
                for (const std::string& t : uniq) {
                    if (!first) joined += ", ";
                    joined += t;
                    first = false;
                }
                out.push_back(make_violation("no_driver", "warning", json(net), pinrefs, net_xy,
                                             "net " + net + " has only sink pins (" + joined +
                                                 ") — no driver"));
            }
        }
    }

    // ── symbol-level rules (sorted by (ref or "~", id)) ──
    {
        std::vector<const json*> syms;
        if (sch.contains("symbols") && sch["symbols"].is_array())
            for (const json& s : sch["symbols"]) syms.push_back(&s);
        auto refkey = [](const json* s) -> std::string {
            if (s->contains("ref")) return s->value("ref", std::string());
            return "~";
        };
        std::sort(syms.begin(), syms.end(), [&](const json* a, const json* b) {
            std::string ra = refkey(a), rb = refkey(b);
            if (ra != rb) return ra < rb;
            return a->value("id", std::string()) < b->value("id", std::string());
        });
        std::map<std::string, std::string> refs_seen;
        const json& cache = sch.contains("lib_cache") ? sch["lib_cache"] : json::object();
        for (const json* sp : syms) {
            const json& sym = *sp;
            std::string ref = jstr(sym.contains("ref") ? sym["ref"] : json(""));
            std::string sid = sym.value("id", std::string());
            json xy = (sym.contains("pos") && sym["pos"].is_array()) ? sym["pos"] : json(nullptr);
            bool unannotated = (!ref.empty() && ref.back() == '?') || ref.empty();
            if (unannotated) {
                out.push_back(make_violation("unannotated_ref", "warning", json(nullptr), {sid}, xy,
                                             "symbol " + sid + " has an unannotated ref '" + ref +
                                                 "'"));
            } else {
                auto it = refs_seen.find(ref);
                if (it != refs_seen.end()) {
                    out.push_back(make_violation(
                        "duplicate_ref", "error", json(nullptr), {sid, it->second}, xy,
                        "ref '" + ref + "' is used by two symbols (" + it->second + " and " + sid +
                            ")"));
                } else {
                    refs_seen[ref] = sid;
                }
            }
            // missing_footprint
            std::string libid = sym.value("libid", std::string());
            bool is_power_sym = false;
            if (cache.contains(libid) && cache[libid].is_object())
                is_power_sym = cache[libid].value("power", false);
            bool on_board = sym.contains("on_board") ? sym["on_board"].get<bool>() : true;
            std::string footprint = sym.value("footprint", std::string());
            if (on_board && footprint.empty() && !is_power_sym) {
                out.push_back(make_violation("missing_footprint", "warning", json(nullptr), {sid}, xy,
                                             "on-board symbol " + sid + " (" + ref +
                                                 ") has no footprint assigned"));
            }
        }
    }

    // ── multiple_nets_label ──
    {
        // group label texts (insertion order, but we iterate sorted names)
        std::map<std::string, std::vector<const json*>> label_names;
        if (sch.contains("labels") && sch["labels"].is_array())
            for (const json& lb : sch["labels"]) {
                std::string name = jstr(lb.contains("text") ? lb["text"] : json(""));
                if (!name.empty()) label_names[name].push_back(&lb);
            }
        for (auto& kv : label_names) {
            const std::string& name = kv.first;
            const std::vector<const json*>& lbs = kv.second;
            bool in_nets = nets.contains(name);
            if (!in_nets && lbs.size() >= 2) {
                std::set<std::pair<double, double>> pts;
                for (const json* l : lbs) {
                    double px = 0, py = 0;
                    if (l->contains("pos") && (*l)["pos"].is_array() && (*l)["pos"].size() >= 2) {
                        px = get_num((*l)["pos"][0]);
                        py = get_num((*l)["pos"][1]);
                    }
                    pts.insert({py_round(px * 1e6) / 1e6, py_round(py * 1e6) / 1e6});
                }
                if (pts.size() >= 2) {
                    json xy = (lbs[0]->contains("pos")) ? (*lbs[0])["pos"] : json(nullptr);
                    out.push_back(make_violation(
                        "multiple_nets_label", "error", json(name), {}, xy,
                        "label '" + name + "' is placed on " + std::to_string(pts.size()) +
                            " disconnected points but no net carries it — coincident with multiple "
                            "union classes"));
                }
            }
        }
    }

    // ── hierarchy mismatch ──
    {
        if (sch.contains("sheet_symbols") && sch["sheet_symbols"].is_array() &&
            !sch["sheet_symbols"].empty()) {
            const json& symbols = sch["sheet_symbols"];
            // hier labels per sheet
            std::map<std::string, std::set<std::string>> hier_by_sheet;
            if (sch.contains("labels") && sch["labels"].is_array())
                for (const json& lb : sch["labels"]) {
                    if (jstr(lb.contains("scope") ? lb["scope"] : json("")) == "hier") {
                        std::string nm = jstr(lb.contains("text") ? lb["text"] : json(""));
                        if (!nm.empty())
                            hier_by_sheet[lb.value("sheet", std::string())].insert(nm);
                    }
                }
            // pin names exposed per child
            std::map<std::string, std::set<std::string>> pins_for_child;
            for (const json& hs : symbols) {
                std::string child = hs.value("child", std::string());
                if (hs.contains("pins") && hs["pins"].is_array())
                    for (const json& pn : hs["pins"])
                        if (pn.is_object()) {
                            std::string nm = jstr(pn.contains("name") ? pn["name"] : json(""));
                            if (!nm.empty()) pins_for_child[child].insert(nm);
                        }
            }
            // sheet pins with no child hier label (sheet_symbols sorted by id, pins by name)
            std::vector<const json*> ss;
            for (const json& hs : symbols) ss.push_back(&hs);
            std::sort(ss.begin(), ss.end(), [](const json* a, const json* b) {
                return a->value("id", std::string()) < b->value("id", std::string());
            });
            for (const json* hp : ss) {
                const json& hs = *hp;
                std::string hid = hs.value("id", std::string());
                std::string child = hs.value("child", std::string());
                const std::set<std::string>& child_labels =
                    hier_by_sheet.count(child) ? hier_by_sheet[child] : std::set<std::string>();
                json pos = hs.contains("pos") ? hs["pos"] : json(nullptr);
                std::vector<const json*> pins;
                if (hs.contains("pins") && hs["pins"].is_array())
                    for (const json& pn : hs["pins"]) pins.push_back(&pn);
                std::stable_sort(pins.begin(), pins.end(), [](const json* a, const json* b) {
                    return jstr(a->contains("name") ? (*a)["name"] : json("")) <
                           jstr(b->contains("name") ? (*b)["name"] : json(""));
                });
                for (const json* pnp : pins) {
                    const json& pn = *pnp;
                    if (!pn.is_object()) continue;
                    std::string nm = jstr(pn.contains("name") ? pn["name"] : json(""));
                    if (!nm.empty() && !child_labels.count(nm)) {
                        json xy = (pn.contains("pos") && !pn["pos"].is_null()) ? pn["pos"] : pos;
                        out.push_back(make_violation(
                            "hier_pin_no_label", "warning", json(nullptr), {hid + "." + nm}, xy,
                            "sheet pin '" + nm + "' on " + hid +
                                " has no matching hier label in child sheet '" + child + "'"));
                    }
                }
            }
            // hier labels with no exposing sheet pin (sorted children, sorted names)
            for (auto& kv : hier_by_sheet) {
                const std::string& child = kv.first;
                if (!pins_for_child.count(child)) continue;
                const std::set<std::string>& exposed = pins_for_child[child];
                for (const std::string& nm : kv.second) {  // std::set is sorted
                    if (!exposed.count(nm)) {
                        out.push_back(make_violation(
                            "hier_label_no_pin", "warning", json(nullptr), {child + ":" + nm},
                            json(nullptr),
                            "hier label '" + nm + "' in sheet '" + child +
                                "' is not exposed as a sheet pin on any symbol instantiating it"));
                    }
                }
            }
        }
    }

    // ── final deterministic sort ──
    // key = (rule, net or "", tuple(pins), tuple(xy) if xy else ())
    std::stable_sort(out.begin(), out.end(), [](const json& a, const json& b) {
        std::string ra = a["rule"].get<std::string>(), rb = b["rule"].get<std::string>();
        if (ra != rb) return ra < rb;
        std::string na = a["net"].is_string() ? a["net"].get<std::string>() : "";
        std::string nb = b["net"].is_string() ? b["net"].get<std::string>() : "";
        if (na != nb) return na < nb;
        // pins tuple comparison
        const json& pa = a["pins"];
        const json& pb = b["pins"];
        size_t n = std::min(pa.size(), pb.size());
        for (size_t i = 0; i < n; ++i) {
            std::string s1 = pa[i].get<std::string>(), s2 = pb[i].get<std::string>();
            if (s1 != s2) return s1 < s2;
        }
        if (pa.size() != pb.size()) return pa.size() < pb.size();
        // xy tuple: empty () if null, else (x, y)
        bool xa_empty = !a["xy"].is_array();
        bool xb_empty = !b["xy"].is_array();
        if (xa_empty || xb_empty) {
            if (xa_empty != xb_empty) return xa_empty;  // () < non-empty
            return false;                               // both empty -> equal
        }
        size_t m = std::min(a["xy"].size(), b["xy"].size());
        for (size_t i = 0; i < m; ++i) {
            double d1 = a["xy"][i].get<double>(), d2 = b["xy"][i].get<double>();
            if (d1 != d2) return d1 < d2;
        }
        return a["xy"].size() < b["xy"].size();
    });

    return out;
}

}  // namespace chudware::ecad
