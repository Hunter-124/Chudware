// core/agent/iface_plan.cpp — see iface_plan.h.
#include "agent/iface_plan.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <map>
#include <set>
#include <sstream>

namespace chudware {
namespace agent {

namespace {

std::string lower_copy(std::string s) {
    for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    return s;
}

double as_number(const json& v, double d = 0.0) {
    if (v.is_number()) return v.get<double>();
    if (v.is_object() && v.contains("value") && v["value"].is_number())
        return v["value"].get<double>();
    if (v.is_string()) {
        try {
            return std::stod(v.get<std::string>());
        } catch (...) {
        }
    }
    return d;
}

int as_int(const json& v, int d = 0) {
    if (v.is_number_integer()) return v.get<int>();
    if (v.is_number()) return static_cast<int>(std::lround(v.get<double>()));
    if (v.is_object() && v.contains("value")) return as_int(v["value"], d);
    if (v.is_string()) {
        try {
            return std::stoi(v.get<std::string>());
        } catch (...) {
        }
    }
    return d;
}

std::string jstr(const json& o, const char* k, const std::string& d = {}) {
    if (!o.is_object() || !o.contains(k) || !o[k].is_string()) return d;
    return o[k].get<std::string>();
}

// Pull first non-empty string among keys.
std::string first_str(const json& o, std::initializer_list<const char*> keys) {
    for (const char* k : keys) {
        std::string s = jstr(o, k);
        if (!s.empty()) return s;
    }
    return {};
}

std::vector<std::string> read_between(const json& o) {
    std::vector<std::string> out;
    const json* arr = nullptr;
    if (o.contains("between") && o["between"].is_array()) arr = &o["between"];
    else if (o.contains("parts") && o["parts"].is_array()) arr = &o["parts"];
    else if (o.contains("mates") && o["mates"].is_array()) arr = &o["mates"];
    if (!arr) return out;
    for (const auto& x : *arr) {
        if (x.is_string()) {
            std::string s = x.get<std::string>();
            if (!s.empty()) out.push_back(s);
        }
    }
    return out;
}

std::string compact_params(const json& params) {
    if (!params.is_object() || params.empty()) return {};
    std::ostringstream oss;
    bool first = true;
    auto emit = [&](const std::string& k, const json& v) {
        if (!first) oss << ", ";
        first = false;
        oss << k << "=";
        if (v.is_string()) oss << v.get<std::string>();
        else if (v.is_number() || v.is_boolean()) oss << v.dump();
        else if (v.is_object() || v.is_array()) oss << v.dump();
        else oss << v.dump();
    };
    // Prefer known keys first for stable readable order.
    static const char* prefer[] = {"holes", "fit", "count", "dia", "diameter", "pattern",
                                   "spacing", "pcd", "clearance", "bolt", "hardware"};
    std::set<std::string> seen;
    for (const char* k : prefer) {
        if (params.contains(k)) {
            emit(k, params[k]);
            seen.insert(k);
        }
    }
    for (auto it = params.begin(); it != params.end(); ++it) {
        if (seen.count(it.key())) continue;
        emit(it.key(), it.value());
    }
    return oss.str();
}

json extract_features_array(const json& tree_or_doc) {
    if (!tree_or_doc.is_object()) return json::array();
    if (tree_or_doc.contains("features") && tree_or_doc["features"].is_array())
        return tree_or_doc["features"];
    if (tree_or_doc.contains("tree") && tree_or_doc["tree"].is_object() &&
        tree_or_doc["tree"].contains("features") && tree_or_doc["tree"]["features"].is_array())
        return tree_or_doc["tree"]["features"];
    return json::array();
}

double feature_diameter(const json& f) {
    // params.diameter / diameter / dia / standard M-size on feature or params.
    auto from = [](const json& o) -> double {
        if (!o.is_object()) return 0.0;
        for (const char* k : {"diameter", "dia", "d", "bore_diameter", "hole_diameter"}) {
            if (o.contains(k)) {
                double v = as_number(o[k], 0.0);
                if (v > 0.0) return v;
            }
        }
        if (o.contains("radius")) {
            double r = as_number(o["radius"], 0.0);
            if (r > 0.0) return r * 2.0;
        }
        return 0.0;
    };
    double d = from(f);
    if (d > 0.0) return d;
    if (f.contains("params") && f["params"].is_object()) {
        d = from(f["params"]);
        if (d > 0.0) return d;
    }
    // M-standard string → nominal clearance (medium): M5 → 5.5 rough; use nominal+0.5
    auto msize = [&](const json& o) -> double {
        if (!o.is_object()) return 0.0;
        for (const char* k : {"standard", "thread", "size", "screw", "bolt"}) {
            if (!o.contains(k) || !o[k].is_string()) continue;
            std::string s = o[k].get<std::string>();
            if (s.empty()) continue;
            size_t i = 0;
            while (i < s.size() && (s[i] == 'M' || s[i] == 'm' || std::isspace((unsigned char)s[i])))
                ++i;
            if (i >= s.size()) continue;
            try {
                double nom = std::stod(s.substr(i));
                if (nom > 0.0) return nom + 0.5;  // medium clearance approx for compare
            } catch (...) {
            }
        }
        return 0.0;
    };
    d = msize(f);
    if (d > 0.0) return d;
    if (f.contains("params")) d = msize(f["params"]);
    return d;
}

int feature_count(const json& f) {
    if (f.contains("count")) return as_int(f["count"], 0);
    if (f.contains("params") && f["params"].is_object() && f["params"].contains("count"))
        return as_int(f["params"]["count"], 0);
    // hole: face-local `positions` or `world_points` — one bore per entry either way.
    for (const char* k : {"positions", "world_points"}) {
        if (f.contains(k) && f[k].is_array()) return static_cast<int>(f[k].size());
        if (f.contains("params") && f["params"].is_object() && f["params"].contains(k) &&
            f["params"][k].is_array())
            return static_cast<int>(f["params"][k].size());
    }
    return 0;
}

double feature_pcd(const json& f) {
    auto get = [](const json& o) -> double {
        if (!o.is_object()) return 0.0;
        for (const char* k : {"pcd", "pitch_circle", "bolt_circle", "bc_dia"}) {
            if (o.contains(k)) {
                double v = as_number(o[k], 0.0);
                if (v > 0.0) return v;
            }
        }
        return 0.0;
    };
    double v = get(f);
    if (v > 0.0) return v;
    if (f.contains("params")) return get(f["params"]);
    return 0.0;
}

// Extract hole-like dims from interface params (plan-side, not measured).
struct PlanHoleDims {
    double dia = 0.0;
    int count = 0;
    double spacing = 0.0;
    double pcd = 0.0;
    std::string pattern;
    std::string fit;
};

PlanHoleDims plan_hole_dims(const InterfaceSpec& iface) {
    PlanHoleDims h;
    h.fit = jstr(iface.params, "fit");
    const json* holes = nullptr;
    if (iface.params.contains("holes") && iface.params["holes"].is_object())
        holes = &iface.params["holes"];
    auto read = [&](const json& o) {
        if (o.contains("dia")) h.dia = as_number(o["dia"], h.dia);
        if (o.contains("diameter")) h.dia = as_number(o["diameter"], h.dia);
        if (o.contains("count")) h.count = as_int(o["count"], h.count);
        if (o.contains("spacing")) h.spacing = as_number(o["spacing"], h.spacing);
        if (o.contains("pcd")) h.pcd = as_number(o["pcd"], h.pcd);
        if (o.contains("pattern") && o["pattern"].is_string())
            h.pattern = o["pattern"].get<std::string>();
        if (o.contains("fit") && o["fit"].is_string() && h.fit.empty())
            h.fit = o["fit"].get<std::string>();
    };
    if (holes) read(*holes);
    read(iface.params);
    // bolt: "M5" → nominal clearance ~5.5
    if (h.dia <= 0.0) {
        for (const char* k : {"bolt", "hardware", "screw", "fastener"}) {
            if (!iface.params.contains(k)) continue;
            if (iface.params[k].is_string()) {
                std::string s = iface.params[k].get<std::string>();
                size_t i = 0;
                while (i < s.size() &&
                       (s[i] == 'M' || s[i] == 'm' || std::isspace((unsigned char)s[i])))
                    ++i;
                if (i < s.size()) {
                    try {
                        double nom = std::stod(s.substr(i));
                        if (nom > 0.0) h.dia = nom + 0.5;
                    } catch (...) {
                    }
                }
            } else if (iface.params[k].is_object()) {
                read(iface.params[k]);
            }
        }
    }
    return h;
}

std::string seed_of(const InterfaceSpec& iface) {
    if (!iface.seed.empty()) return iface.seed;
    if (!iface.between.empty()) return iface.between.front();
    return {};
}

std::string other_of(const InterfaceSpec& iface, const std::string& name) {
    const std::string n = iface_norm_name(name);
    for (const auto& p : iface.between) {
        if (iface_norm_name(p) != n) return p;
    }
    return {};
}

// Match part name against task id/title/request/part field.
bool task_matches_part(const json& t, const std::string& part) {
    if (!t.is_object() || part.empty()) return false;
    const std::string pn = iface_norm_name(part);
    auto hit = [&](const std::string& s) {
        if (s.empty()) return false;
        if (iface_norm_name(s) == pn) return true;
        // token containment: "bracket_a L-bracket" contains bracket_a
        std::string low = lower_copy(s);
        std::string needle = lower_copy(part);
        if (low.find(needle) != std::string::npos) return true;
        // normalized needle as substring of normalized hay
        std::string hn = iface_norm_name(s);
        return hn.find(pn) != std::string::npos && pn.size() >= 3;
    };
    if (t.contains("part") && t["part"].is_string() && hit(t["part"].get<std::string>()))
        return true;
    if (t.contains("id") && t["id"].is_string() && hit(t["id"].get<std::string>())) return true;
    if (t.contains("title") && t["title"].is_string() && hit(t["title"].get<std::string>()))
        return true;
    if (t.contains("request") && t["request"].is_string() && hit(t["request"].get<std::string>()))
        return true;
    if (t.contains("name") && t["name"].is_string() && hit(t["name"].get<std::string>()))
        return true;
    return false;
}

// Aggregate measured hole dia/count from MeasuredInterface.
struct AggHoles {
    double primary_dia = 0.0;
    int total_count = 0;
    double pcd = 0.0;
};

AggHoles aggregate_holes(const MeasuredInterface& m) {
    AggHoles a;
    for (const auto& h : m.holes) {
        if (!h.is_object()) continue;
        double d = as_number(h.value("diameter", 0.0), 0.0);
        int c = as_int(h.value("count", 0), 0);
        if (c <= 0) c = 1;
        if (d > 0.0) {
            if (a.primary_dia <= 0.0) a.primary_dia = d;
            // Prefer the most common / first positive; if multiple distinct, keep first.
        }
        a.total_count += c;
        double p = as_number(h.value("pcd", 0.0), 0.0);
        if (p > 0.0 && a.pcd <= 0.0) a.pcd = p;
    }
    return a;
}

}  // namespace

std::string iface_norm_name(const std::string& s) {
    std::string out;
    out.reserve(s.size());
    bool prev_us = false;
    for (unsigned char c : s) {
        if (std::isalnum(c)) {
            out.push_back(static_cast<char>(std::tolower(c)));
            prev_us = false;
        } else if (!prev_us && !out.empty()) {
            out.push_back('_');
            prev_us = true;
        }
    }
    while (!out.empty() && out.back() == '_') out.pop_back();
    return out;
}

bool interface_seed_is(const InterfaceSpec& iface, const std::string& name) {
    const std::string seed = seed_of(iface);
    if (seed.empty() || name.empty()) return false;
    return iface_norm_name(seed) == iface_norm_name(name);
}

std::string interface_mate_of(const InterfaceSpec& iface, const std::string& name) {
    return other_of(iface, name);
}

InterfaceLedger parse_interface_ledger(const json& plan) {
    InterfaceLedger led;
    if (!plan.is_object()) return led;

    json arr = json::array();
    if (plan.contains("interfaces") && plan["interfaces"].is_array())
        arr = plan["interfaces"];
    else if (plan.contains("mates") && plan["mates"].is_array())
        arr = plan["mates"];

    int auto_i = 0;
    for (const auto& raw : arr) {
        if (!raw.is_object()) continue;
        InterfaceSpec spec;
        spec.between = read_between(raw);
        if (spec.between.size() < 2) continue;
        // Keep only first two unique for binary mate (planner may list more; take pair).
        if (spec.between.size() > 2) spec.between.resize(2);

        spec.kind = first_str(raw, {"kind", "type", "mate_kind"});
        if (spec.kind.empty()) spec.kind = "mate";
        spec.seed = first_str(raw, {"seed", "seed_part", "fixes", "standard_bearer"});
        if (spec.seed.empty()) {
            // Heuristic: part whose name suggests hardware/catalog carrier, else first.
            for (const auto& p : spec.between) {
                std::string n = lower_copy(p);
                if (n.find("screw") != std::string::npos || n.find("bolt") != std::string::npos ||
                    n.find("pin") != std::string::npos || n.find("hardware") != std::string::npos ||
                    n.find("fastener") != std::string::npos) {
                    spec.seed = p;
                    break;
                }
            }
        }
        if (spec.seed.empty()) spec.seed = spec.between.front();

        spec.id = first_str(raw, {"id", "name"});
        if (spec.id.empty()) spec.id = "iface" + std::to_string(++auto_i);

        // Collect free-form params: prefer nested object, else remaining keys.
        if (raw.contains("params") && raw["params"].is_object())
            spec.params = raw["params"];
        else {
            spec.params = json::object();
            for (auto it = raw.begin(); it != raw.end(); ++it) {
                const std::string& k = it.key();
                if (k == "between" || k == "parts" || k == "mates" || k == "kind" || k == "type" ||
                    k == "mate_kind" || k == "seed" || k == "seed_part" || k == "fixes" ||
                    k == "standard_bearer" || k == "id" || k == "name" || k == "params")
                    continue;
                spec.params[k] = it.value();
            }
        }

        std::ostringstream sum;
        sum << spec.kind << " between ";
        for (size_t i = 0; i < spec.between.size(); ++i) {
            if (i) sum << " + ";
            sum << spec.between[i];
        }
        sum << " (seed=" << spec.seed << ")";
        std::string cp = compact_params(spec.params);
        if (!cp.empty()) sum << " [" << cp << "]";
        spec.raw_summary = sum.str();

        // Record deps: every non-seed participant depends on seed.
        const std::string sn = iface_norm_name(spec.seed);
        led.is_seed[spec.seed] = true;
        for (const auto& p : spec.between) {
            if (iface_norm_name(p) == sn) continue;
            led.part_deps[p].push_back(spec.seed);
            // Ensure seed key exists.
            if (!led.is_seed.count(p)) led.is_seed[p] = false;
        }
        led.interfaces.push_back(std::move(spec));
    }
    return led;
}

std::vector<size_t> order_parts_by_interfaces(const std::vector<std::string>& part_names,
                                              const InterfaceLedger& ledger) {
    const size_t n = part_names.size();
    std::vector<size_t> order;
    if (n == 0) return order;

    // Map normalized name → indices (first wins for multi).
    std::map<std::string, size_t> idx;
    for (size_t i = 0; i < n; ++i) idx[iface_norm_name(part_names[i])] = i;

    std::vector<int> indeg(n, 0);
    std::vector<std::vector<size_t>> adj(n);
    auto add_edge = [&](size_t from, size_t to) {
        // from must precede to
        if (from == to) return;
        adj[from].push_back(to);
        indeg[to] += 1;
    };

    for (const auto& kv : ledger.part_deps) {
        auto it_dep = idx.find(iface_norm_name(kv.first));
        if (it_dep == idx.end()) continue;
        for (const auto& seed_name : kv.second) {
            auto it_seed = idx.find(iface_norm_name(seed_name));
            if (it_seed == idx.end()) continue;
            add_edge(it_seed->second, it_dep->second);
        }
    }

    // Stable Kahn: among ready nodes, pick lowest original index.
    std::vector<bool> placed(n, false);
    while (order.size() < n) {
        int pick = -1;
        for (size_t i = 0; i < n; ++i) {
            if (placed[i] || indeg[i] > 0) continue;
            if (pick < 0 || static_cast<int>(i) < pick) pick = static_cast<int>(i);
        }
        if (pick < 0) {
            // Cycle or broken: append remaining in original order.
            for (size_t i = 0; i < n; ++i)
                if (!placed[i]) order.push_back(i);
            break;
        }
        size_t u = static_cast<size_t>(pick);
        placed[u] = true;
        order.push_back(u);
        for (size_t v : adj[u]) {
            if (indeg[v] > 0) --indeg[v];
        }
    }
    return order;
}

json fold_interface_deps_into_tasks(const json& tasks, const InterfaceLedger& ledger) {
    if (!tasks.is_array() || ledger.interfaces.empty()) return tasks;
    json out = tasks;
    // For each interface edge seed → dependent, find matching tasks and add dep.
    for (const auto& iface : ledger.interfaces) {
        const std::string seed = seed_of(iface);
        if (seed.empty()) continue;
        for (const auto& dep_part : iface.between) {
            if (iface_norm_name(dep_part) == iface_norm_name(seed)) continue;
            // Find seed task and dep task indices.
            int seed_ti = -1, dep_ti = -1;
            for (size_t i = 0; i < out.size(); ++i) {
                if (!out[i].is_object()) continue;
                if (seed_ti < 0 && task_matches_part(out[i], seed)) seed_ti = static_cast<int>(i);
                if (dep_ti < 0 && task_matches_part(out[i], dep_part)) dep_ti = static_cast<int>(i);
            }
            if (seed_ti < 0 || dep_ti < 0 || seed_ti == dep_ti) continue;
            std::string seed_id = out[seed_ti].value("id", std::string());
            if (seed_id.empty()) continue;
            if (!out[dep_ti].contains("deps") || !out[dep_ti]["deps"].is_array())
                out[dep_ti]["deps"] = json::array();
            bool has = false;
            for (const auto& d : out[dep_ti]["deps"]) {
                if (d.is_string() && d.get<std::string>() == seed_id) {
                    has = true;
                    break;
                }
            }
            if (!has) out[dep_ti]["deps"].push_back(seed_id);
        }
    }
    return out;
}

std::vector<std::vector<size_t>> waves_from_task_deps(const json& tasks) {
    std::vector<std::vector<size_t>> waves;
    if (!tasks.is_array() || tasks.empty()) return waves;
    const size_t n = tasks.size();
    std::map<std::string, size_t> id_to_idx;
    for (size_t i = 0; i < n; ++i) {
        if (!tasks[i].is_object()) continue;
        std::string id = tasks[i].value("id", std::string());
        if (id.empty()) id = "u" + std::to_string(i + 1);
        id_to_idx[id] = i;
    }
    std::vector<int> indeg(n, 0);
    std::vector<std::vector<size_t>> adj(n);
    for (size_t i = 0; i < n; ++i) {
        if (!tasks[i].is_object()) continue;
        if (!tasks[i].contains("deps") || !tasks[i]["deps"].is_array()) continue;
        for (const auto& d : tasks[i]["deps"]) {
            if (!d.is_string()) continue;
            auto it = id_to_idx.find(d.get<std::string>());
            if (it == id_to_idx.end()) continue;
            adj[it->second].push_back(i);
            indeg[i] += 1;
        }
    }
    std::vector<size_t> ready;
    for (size_t i = 0; i < n; ++i)
        if (indeg[i] == 0) ready.push_back(i);
    std::vector<bool> placed(n, false);
    while (!ready.empty()) {
        waves.push_back(ready);
        std::vector<size_t> next;
        for (size_t u : ready) {
            placed[u] = true;
            for (size_t v : adj[u]) {
                if (--indeg[v] == 0) next.push_back(v);
            }
        }
        ready.swap(next);
    }
    std::vector<size_t> rest;
    for (size_t i = 0; i < n; ++i)
        if (!placed[i]) rest.push_back(i);
    if (!rest.empty()) waves.push_back(rest);
    return waves;
}

MeasuredInterface measure_interface_from_tree(const std::string& part_name, const json& tree_or_doc,
                                              const json& bbox) {
    MeasuredInterface m;
    m.part_name = part_name;
    m.bbox = bbox.is_object() ? bbox : json::object();
    m.holes = json::array();
    json feats = extract_features_array(tree_or_doc);
    std::ostringstream digest;
    int lines = 0;
    for (const auto& f : feats) {
        if (!f.is_object()) continue;
        std::string type = jstr(f, "type");
        if (type.empty() && f.contains("params") && f["params"].is_object())
            type = jstr(f["params"], "type");
        std::string id = jstr(f, "id", "?");
        std::string name = jstr(f, "name");
        const bool holeish =
            type == "hole" || type == "bolt_circle" || type == "counterbore" ||
            type == "countersink" || type.find("hole") != std::string::npos;
        if (holeish) {
            json h = json::object();
            h["type"] = type.empty() ? "hole" : type;
            h["id"] = id;
            double dia = feature_diameter(f);
            if (dia > 0.0) h["diameter"] = dia;
            int cnt = feature_count(f);
            if (cnt > 0) h["count"] = cnt;
            double pcd = feature_pcd(f);
            if (pcd > 0.0) h["pcd"] = pcd;
            // placement passthrough if present (face-local positions or world points)
            for (const char* k : {"positions", "world_points"}) {
                if (f.contains(k)) h[k] = f[k];
                else if (f.contains("params") && f["params"].is_object() &&
                         f["params"].contains(k))
                    h[k] = f["params"][k];
            }
            m.holes.push_back(h);
        }
        if (lines < 24) {
            if (lines) digest << "\n";
            digest << id << " " << type;
            if (!name.empty()) digest << " " << name;
            double dia = feature_diameter(f);
            if (dia > 0.0) digest << " dia=" << dia;
            int cnt = feature_count(f);
            if (cnt > 0) digest << " count=" << cnt;
            ++lines;
        }
    }
    m.tree_digest = digest.str();
    std::ostringstream sum;
    sum << part_name;
    if (m.bbox.contains("size") && m.bbox["size"].is_array() && m.bbox["size"].size() >= 3) {
        sum << " bbox≈[" << as_number(m.bbox["size"][0]) << " x " << as_number(m.bbox["size"][1])
            << " x " << as_number(m.bbox["size"][2]) << "] mm";
    }
    if (!m.holes.empty()) {
        sum << "; holes:";
        for (size_t i = 0; i < m.holes.size(); ++i) {
            const auto& h = m.holes[i];
            sum << " " << h.value("type", "hole");
            if (h.contains("count")) sum << "×" << h["count"].get<int>();
            if (h.contains("diameter")) sum << " ⌀" << as_number(h["diameter"]);
            if (h.contains("pcd")) sum << " pcd=" << as_number(h["pcd"]);
            if (i + 1 < m.holes.size()) sum << ",";
        }
    } else if (!m.tree_digest.empty()) {
        sum << "; features present";
    } else {
        sum << "; (no hole features measured)";
    }
    m.summary = sum.str();
    return m;
}

std::string build_interface_advisory(const std::string& part_name, const InterfaceLedger& ledger,
                                     const std::map<std::string, MeasuredInterface>& built) {
    if (part_name.empty() || ledger.interfaces.empty()) return {};
    const std::string pn = iface_norm_name(part_name);

    // Index built by normalized name.
    std::map<std::string, const MeasuredInterface*> built_n;
    for (const auto& kv : built) built_n[iface_norm_name(kv.first)] = &kv.second;

    std::ostringstream out;
    out << "INTERFACE CONSTRAINTS for part '" << part_name << "':\n";
    int n_lines = 0;
    for (const auto& iface : ledger.interfaces) {
        bool participates = false;
        for (const auto& p : iface.between) {
            if (iface_norm_name(p) == pn) {
                participates = true;
                break;
            }
        }
        if (!participates) continue;

        const bool am_seed = interface_seed_is(iface, part_name);
        const std::string mate = interface_mate_of(iface, part_name);
        const MeasuredInterface* mate_m = nullptr;
        if (!mate.empty()) {
            auto it = built_n.find(iface_norm_name(mate));
            if (it != built_n.end()) mate_m = it->second;
        }
        PlanHoleDims phd = plan_hole_dims(iface);

        ++n_lines;
        out << "\n- Interface '" << iface.id << "' (" << iface.kind << ")";
        if (!mate.empty()) out << " with '" << mate << "'";
        out << ":\n";

        if (mate_m) {
            // (a) mating part already built — measured dims are binding for dependent
            // (and informative for seed if somehow rebuilt).
            out << "  Your mating part '" << mate << "' ALREADY EXISTS with these interface "
                   "dimensions — match them EXACTLY:\n";
            out << "    " << mate_m->summary << "\n";
            if (!mate_m->tree_digest.empty()) {
                out << "    Feature digest:\n";
                std::istringstream iss(mate_m->tree_digest);
                std::string line;
                int lc = 0;
                while (std::getline(iss, line) && lc < 12) {
                    out << "      " << line << "\n";
                    ++lc;
                }
            }
            AggHoles ah = aggregate_holes(*mate_m);
            if (ah.primary_dia > 0.0)
                out << "    Hole diameter to match: " << ah.primary_dia << " mm\n";
            if (ah.total_count > 0) out << "    Hole count to match: " << ah.total_count << "\n";
            if (ah.pcd > 0.0) out << "    PCD to match: " << ah.pcd << " mm\n";
            if (!am_seed)
                out << "  You are the DEPENDENT — do NOT invent new hole sizes/patterns; "
                       "copy the mate.\n";
        } else if (am_seed) {
            // (b) seed, mate not built — plan params are BINDING standard
            out << "  Interface standard for '" << part_name
                << "' (you are the SEED — these dimensions are BINDING, the mating part will "
                   "be designed to them):\n";
            out << "    " << iface.raw_summary << "\n";
            if (phd.dia > 0.0) out << "    Binding hole dia: " << phd.dia << " mm\n";
            if (phd.count > 0) out << "    Binding hole count: " << phd.count << "\n";
            if (phd.spacing > 0.0) out << "    Binding spacing: " << phd.spacing << " mm\n";
            if (phd.pcd > 0.0) out << "    Binding PCD: " << phd.pcd << " mm\n";
            if (!phd.pattern.empty()) out << "    Pattern: " << phd.pattern << "\n";
            if (!phd.fit.empty()) out << "    Fit: " << phd.fit << "\n";
            if (!iface.params.empty())
                out << "    Spec JSON: " << iface.params.dump() << "\n";
        } else {
            // (c) dependent, mate not yet built — still bind to plan standard
            out << "  Mating part '" << mate
                << "' is not built yet. Interface standard from the plan (seed='"
                << seed_of(iface) << "' will set these; match them):\n";
            out << "    " << iface.raw_summary << "\n";
            if (phd.dia > 0.0) out << "    Required hole dia: " << phd.dia << " mm\n";
            if (phd.count > 0) out << "    Required hole count: " << phd.count << "\n";
            if (phd.spacing > 0.0) out << "    Required spacing: " << phd.spacing << " mm\n";
            if (phd.pcd > 0.0) out << "    Required PCD: " << phd.pcd << " mm\n";
            if (!phd.pattern.empty()) out << "    Pattern: " << phd.pattern << "\n";
            if (!phd.fit.empty()) out << "    Fit: " << phd.fit << "\n";
        }
    }
    if (n_lines == 0) return {};
    out << "\nHonor every interface dimension above. Mating geometry that disagrees will fail "
           "assembly verification.\n";
    return out.str();
}

std::vector<InterfaceMismatch> verify_interfaces(
    const InterfaceLedger& ledger, const std::map<std::string, MeasuredInterface>& built,
    double dia_tol_mm) {
    std::vector<InterfaceMismatch> out;
    std::map<std::string, const MeasuredInterface*> bn;
    for (const auto& kv : built) bn[iface_norm_name(kv.first)] = &kv.second;

    for (const auto& iface : ledger.interfaces) {
        if (iface.between.size() < 2) continue;
        const std::string seed = seed_of(iface);
        const std::string dep = other_of(iface, seed);
        if (seed.empty() || dep.empty()) continue;
        auto it_s = bn.find(iface_norm_name(seed));
        auto it_d = bn.find(iface_norm_name(dep));
        if (it_s == bn.end() || it_d == bn.end()) continue;

        const MeasuredInterface& ms = *it_s->second;
        const MeasuredInterface& md = *it_d->second;
        AggHoles hs = aggregate_holes(ms);
        AggHoles hd = aggregate_holes(md);
        PlanHoleDims phd = plan_hole_dims(iface);

        // Prefer seed measured dia as the standard; fall back to plan.
        double expect_dia = hs.primary_dia > 0.0 ? hs.primary_dia : phd.dia;
        int expect_count = hs.total_count > 0 ? hs.total_count : phd.count;
        double expect_pcd = hs.pcd > 0.0 ? hs.pcd : phd.pcd;

        auto push = [&](const std::string& detail, json expected, json actual) {
            InterfaceMismatch mm;
            mm.interface_id = iface.id;
            mm.kind = iface.kind;
            mm.seed_part = seed;
            mm.dependent_part = dep;  // SEED wins — fix dependent
            mm.detail = detail;
            mm.expected = std::move(expected);
            mm.actual = std::move(actual);
            out.push_back(std::move(mm));
        };

        if (expect_dia > 0.0 && hd.primary_dia > 0.0) {
            if (std::fabs(expect_dia - hd.primary_dia) > dia_tol_mm) {
                push("hole diameter mismatch: seed/standard " + std::to_string(expect_dia) +
                         " mm vs dependent " + std::to_string(hd.primary_dia) + " mm",
                     json{{"diameter", expect_dia}}, json{{"diameter", hd.primary_dia}});
            }
        } else if (expect_dia > 0.0 && hd.primary_dia <= 0.0 && md.holes.empty() &&
                   expect_count > 0) {
            // Dependent has no measurable holes but plan/seed expects them.
            push("dependent missing measured holes; expected dia " + std::to_string(expect_dia) +
                     " mm count " + std::to_string(expect_count),
                 json{{"diameter", expect_dia}, {"count", expect_count}},
                 json{{"holes", md.holes}});
        }

        if (expect_count > 0 && hd.total_count > 0 && expect_count != hd.total_count) {
            push("hole count mismatch: expected " + std::to_string(expect_count) + " got " +
                     std::to_string(hd.total_count),
                 json{{"count", expect_count}}, json{{"count", hd.total_count}});
        }

        if (expect_pcd > 0.0 && hd.pcd > 0.0 && std::fabs(expect_pcd - hd.pcd) > dia_tol_mm * 4) {
            push("hole pattern PCD mismatch: expected " + std::to_string(expect_pcd) + " vs " +
                     std::to_string(hd.pcd),
                 json{{"pcd", expect_pcd}}, json{{"pcd", hd.pcd}});
        }

        // Bbox face thickness sanity for bolted joints: if both have size[2], warn on huge
        // disparity only when kind suggests face mate (soft — skip if either missing).
        if ((iface.kind.find("face") != std::string::npos ||
             iface.kind.find("bolted") != std::string::npos) &&
            ms.bbox.contains("size") && md.bbox.contains("size") && ms.bbox["size"].is_array() &&
            md.bbox["size"].is_array() && ms.bbox["size"].size() >= 2 &&
            md.bbox["size"].size() >= 2) {
            // Compare in-plane extents roughly: if one part's min planar dim is << other
            // by factor of 5+, flag — rare, keep soft.
            auto planar_min = [](const json& size) {
                double a = as_number(size[0]), b = as_number(size[1]);
                return std::min(a, b);
            };
            double ps = planar_min(ms.bbox["size"]);
            double pd = planar_min(md.bbox["size"]);
            if (ps > 1.0 && pd > 1.0) {
                double ratio = ps > pd ? ps / pd : pd / ps;
                if (ratio > 8.0) {
                    push("mating face size disparity (planar min " + std::to_string(ps) + " vs " +
                             std::to_string(pd) + " mm) — check interface alignment",
                         json{{"seed_planar_min", ps}}, json{{"dep_planar_min", pd}});
                }
            }
        }
    }
    return out;
}

std::string build_mismatch_fix_advisory(const std::vector<InterfaceMismatch>& mismatches,
                                        const std::string& dependent_part) {
    if (mismatches.empty() || dependent_part.empty()) return {};
    const std::string dn = iface_norm_name(dependent_part);
    std::ostringstream out;
    out << "INTERFACE MISMATCH FIX for dependent part '" << dependent_part
        << "'. The SEED part set the standard — change THIS part only to match:\n";
    int n = 0;
    for (const auto& mm : mismatches) {
        if (iface_norm_name(mm.dependent_part) != dn) continue;
        ++n;
        out << "\n- [" << mm.interface_id << " / " << mm.kind << "] " << mm.detail << "\n";
        if (!mm.expected.empty()) out << "  expected: " << mm.expected.dump() << "\n";
        if (!mm.actual.empty()) out << "  actual:   " << mm.actual.dump() << "\n";
        out << "  seed (do NOT modify): " << mm.seed_part << "\n";
    }
    if (n == 0) return {};
    out << "\nEdit hole diameters, counts, and positions on '" << dependent_part
        << "' so they match the seed. Do not redesign the seed part.\n";
    return out.str();
}

}  // namespace agent
}  // namespace chudware
