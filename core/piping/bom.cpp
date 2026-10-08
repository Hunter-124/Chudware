// core/piping/bom.cpp — piping_bom: line list, aggregated items with masses,
// spools (split at flanged joints and max_spool_length_mm), weld counts.
#include "piping/bom.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>
#include <map>
#include <numeric>
#include <set>
#include <string>
#include <vector>
#include <utility>

#include "piping/catalog.h"
#include "piping/schema.h"

namespace chudware::piping {
namespace {

using pyutil::jerr;
using pyutil::pyround;
using V3 = std::array<double, 3>;

constexpr double kPi = 3.14159265358979323846;
constexpr double kSnap = 1.0;  // mm; same snapping tolerance as the network solver

double material_density(const std::string& m) {
    static const std::map<std::string, double> rho = {
        {"A106-B", 7850.0},     {"A53-B", 7850.0},      {"A333-6", 7850.0},
        {"A312-304", 8000.0},   {"A312-316", 8000.0},   {"A312-TP304", 8000.0},
        {"A312-TP316", 8000.0}, {"PVC", 1400.0},        {"CPVC", 1550.0},
        {"copper", 8940.0},
    };
    const auto it = rho.find(m);
    return it == rho.end() ? -1.0 : it->second;
}

V3 to_v3(const ojson& p) { return {p[0].get<double>(), p[1].get<double>(), p[2].get<double>()}; }
double dist(const V3& a, const V3& b) {
    return std::sqrt((a[0] - b[0]) * (a[0] - b[0]) + (a[1] - b[1]) * (a[1] - b[1]) +
                     (a[2] - b[2]) * (a[2] - b[2]));
}

struct RunInfo {
    std::string id;
    std::string name;
    ojson nps;
    std::string schedule;
    std::string material;
    std::string end_type;
    std::vector<V3> pts;
    std::vector<double> cum;  // arc length at each vertex
    double length = 0.0;
    double od = 0.0;
    double idia = 0.0;
    double takeout = 0.0;
    double rho = 0.0;
};

// Closest point of p on the run centreline: (distance, arc-length s).
std::pair<double, double> project(const RunInfo& r, const V3& p) {
    double best = std::numeric_limits<double>::infinity();
    double best_s = 0.0;
    for (size_t i = 0; i + 1 < r.pts.size(); ++i) {
        const V3& a = r.pts[i];
        const V3& b = r.pts[i + 1];
        const V3 d = {b[0] - a[0], b[1] - a[1], b[2] - a[2]};
        const double L2 = d[0] * d[0] + d[1] * d[1] + d[2] * d[2];
        double t = L2 > 0 ? ((p[0] - a[0]) * d[0] + (p[1] - a[1]) * d[1] + (p[2] - a[2]) * d[2]) / L2
                          : 0.0;
        t = std::clamp(t, 0.0, 1.0);
        const V3 q = {a[0] + t * d[0], a[1] + t * d[1], a[2] + t * d[2]};
        const double dd = dist(p, q);
        if (dd < best) {
            best = dd;
            best_s = r.cum[i] + t * std::sqrt(L2);
        }
    }
    return {best, best_s};
}

struct Touch {
    size_t run;
    double s;
    bool at_end;
};

std::vector<Touch> touches_at(const std::vector<RunInfo>& runs, const V3& p) {
    std::vector<Touch> out;
    for (size_t i = 0; i < runs.size(); ++i) {
        const auto [d, s] = project(runs[i], p);
        if (d > kSnap) continue;
        out.push_back({i, s, s <= kSnap || s >= runs[i].length - kSnap});
    }
    return out;
}

double wall_area_mm2(double od, double idia) { return kPi / 4.0 * (od * od - idia * idia); }

struct FitInfo {
    std::string id;
    std::string type;
    V3 origin{};
    std::vector<Touch> touches;
    double leg = 0.0;       // take-out per connected run side, mm
    double mass = -1.0;     // kg; < 0 = unknown
    int welds = 0;
    std::string key;        // item aggregation key
    ojson item;             // item descriptor (without qty/mass)
};

struct Piece {
    size_t run;
    double s0;
    double s1;
};

struct Spool {
    std::vector<Piece> pieces;
    double length = 0.0;
    int shop_welds = 0;
    std::vector<std::string> fittings;
};

struct DSU {
    std::vector<size_t> p;
    explicit DSU(size_t n) : p(n) { std::iota(p.begin(), p.end(), 0); }
    size_t find(size_t x) { return p[x] == x ? x : p[x] = find(p[x]); }
    void unite(size_t a, size_t b) {
        a = find(a);
        b = find(b);
        if (a != b) p[std::max(a, b)] = std::min(a, b);
    }
};

}  // namespace

ojson piping_bom(const ojson& params) {
    if (!params.is_object()) return jerr("invalid_params", "pass {piping, max_spool_length_mm}");
    const ojson doc = params.value("piping", ojson(nullptr));
    if (doc.is_null()) {
        return jerr("invalid_params", "piping (an aicad.piping/1 document) is required");
    }
    const std::vector<std::string> verrs = validate_piping(doc);
    if (!verrs.empty()) {
        ojson out = jerr("invalid_piping", "fix the listed errors (see contracts/piping_schema.md)");
        out["errors"] = verrs;
        return out;
    }
    double max_spool = 12000.0;
    if (params.contains("max_spool_length_mm") && !params["max_spool_length_mm"].is_null()) {
        const ojson& v = params["max_spool_length_mm"];
        if (!v.is_number() || v.is_boolean() || !(v.get<double>() > 0)) {
            return jerr("invalid_params", "max_spool_length_mm must be a positive number");
        }
        max_spool = v.get<double>();
    }
    const std::string doc_mat =
        doc.value("material", ojson(nullptr)).is_string() ? doc["material"].get<std::string>() : "A106-B";
    std::vector<std::string> warnings;

    // ── runs ────────────────────────────────────────────────────────────────
    std::vector<RunInfo> runs;
    for (const auto& run : doc["runs"]) {
        if (run.value("suppressed", false)) continue;
        RunInfo r;
        r.id = pyutil::py_str(run["id"]);
        r.name = run.value("name", ojson(nullptr)).is_string() ? run["name"].get<std::string>() : r.id;
        r.nps = run["nps"];
        r.schedule = pyutil::py_str(run.value("schedule", ojson("40")));
        r.material = run.value("material", ojson(nullptr)).is_string()
                         ? run["material"].get<std::string>()
                         : doc_mat;
        r.end_type = run.value("end_type", ojson(nullptr)).is_string()
                         ? run["end_type"].get<std::string>()
                         : "BW";
        for (const auto& p : run["path"]) r.pts.push_back(to_v3(p));
        r.cum.push_back(0.0);
        for (size_t i = 0; i + 1 < r.pts.size(); ++i) r.cum.push_back(r.cum.back() + dist(r.pts[i], r.pts[i + 1]));
        r.length = r.cum.back();
        const ojson pipe = nearest_pipe(r.nps, r.schedule);
        if (!pipe.value("ok", false)) {
            return jerr("unknown_pipe_size", "run " + r.id + ": " + pipe.value("reason", std::string()));
        }
        r.od = pipe["outer_dia"].get<double>();
        r.idia = pipe["inner_dia"].get<double>();
        r.rho = material_density(r.material);
        if (r.rho < 0) {
            return jerr("unknown_material", "run " + r.id + ": no density for " +
                                                pyutil::py_repr_str(r.material));
        }
        runs.push_back(std::move(r));
    }

    // ── fittings: take-out leg, mass, welds ─────────────────────────────────
    std::vector<FitInfo> fits;
    std::vector<std::pair<size_t, double>> flange_claims;  // (run, s) already flanged
    for (const auto& ft : doc["fittings"]) {
        if (ft.value("suppressed", false)) continue;
        FitInfo f;
        f.id = pyutil::py_str(ft["id"]);
        f.type = ft["type"].get<std::string>();
        f.origin = to_v3(ft["origin"]);
        f.touches = touches_at(runs, f.origin);
        if (f.type == "flange" && !f.touches.empty()) {
            // A flange is welded to one pipe end; at a bolted joint of two runs
            // each flange of the pair takes a different run.
            Touch pick = f.touches[0];
            for (const Touch& t : f.touches) {
                bool claimed = false;
                for (const auto& [cr, cs] : flange_claims)
                    claimed = claimed || (cr == t.run && std::abs(cs - t.s) <= kSnap);
                if (!claimed) {
                    pick = t;
                    break;
                }
            }
            f.touches = {pick};
            flange_claims.emplace_back(pick.run, pick.s);
        }
        const ojson prm = ft.value("params", ojson::object());
        const ojson nps = ft["nps"];
        const double nps_d = nps.get<double>();
        const std::string sched = pyutil::py_str(ft.value("schedule", ojson("40")));
        std::string mat = ft.value("material", ojson(nullptr)).is_string()
                              ? ft["material"].get<std::string>()
                              : (f.touches.empty() ? doc_mat : runs[f.touches[0].run].material);
        const double rho = material_density(mat);
        if (rho < 0) {
            return jerr("unknown_material", "fitting " + f.id + ": no density for " +
                                                pyutil::py_repr_str(mat));
        }
        if (f.touches.empty()) {
            warnings.push_back("fitting " + f.id + " is not on any run: counted in items but not "
                               "in take-outs or spools");
        }
        const bool threaded = !f.touches.empty() && runs[f.touches[0].run].end_type == "THD";
        const ojson pipe = nearest_pipe(nps, sched);
        const double aw = pipe.value("ok", false)
                              ? wall_area_mm2(pipe["outer_dia"].get<double>(), pipe["inner_dia"].get<double>())
                              : -1.0;
        const double od = pipe.value("ok", false) ? pipe["outer_dia"].get<double>() : 0.0;
        if (aw < 0) {
            warnings.push_back("fitting " + f.id + ": no catalog pipe for its nps/schedule; mass unknown");
        }
        f.item = ojson::object();
        f.item["kind"] = f.type;
        f.item["nps"] = nps;

        if (f.type == "elbow") {
            const double angle =
                prm.value("angle", ojson(nullptr)).is_number() ? prm["angle"].get<double>() : 90.0;
            const std::string radius =
                prm.value("radius", ojson(nullptr)).is_string() ? prm["radius"].get<std::string>() : "LR";
            const ojson dims = fitting_dims("elbow", nps_d);
            // B16.9 LR 90° centre-to-end A equals the bend radius (1.5·NPS);
            // SR radius is 1.0·NPS.
            double r_lr = 1.5 * nps_d * 25.4;
            if (dims.value("center_to_end", ojson(nullptr)).is_number()) r_lr = dims["center_to_end"].get<double>();
            const double R = radius == "SR" ? r_lr * (2.0 / 3.0) : r_lr;
            const double theta = angle * kPi / 180.0;
            f.leg = R * std::tan(theta / 2.0);
            if (aw >= 0) f.mass = aw * 1e-6 * (R * theta * 1e-3) * rho;
            f.welds = 2;
            f.item["schedule"] = sched;
            f.item["material"] = mat;
            f.item["angle"] = angle;
            f.item["radius"] = radius;
            f.key = "elbow|" + nps.dump() + "|" + sched + "|" + mat + "|" + pyutil::py_float(angle) + "|" + radius;
        } else if (f.type == "tee") {
            const ojson dims = fitting_dims("tee", nps_d);
            if (dims.value("center_to_end", ojson(nullptr)).is_number()) {
                const double C = dims["center_to_end"].get<double>();
                f.leg = C;
                if (aw >= 0) f.mass = aw * 1e-6 * ((2.0 * C + std::max(C - od / 2.0, 0.0)) * 1e-3) * rho;
            } else {
                warnings.push_back("tee " + f.id + ": NPS " + nps.dump() +
                                   " not in the B16.9 catalog; take-out and mass unknown");
            }
            f.welds = 3;
            const ojson branch = prm.value("branch_nps", ojson(nullptr));
            f.item["schedule"] = sched;
            f.item["material"] = mat;
            f.item["branch_nps"] = branch.is_null() ? nps : branch;
            f.key = "tee|" + nps.dump() + "|" + f.item["branch_nps"].dump() + "|" + sched + "|" + mat;
        } else if (f.type == "reducer") {
            const ojson nps2 = ft["nps2"];
            const ojson dims = fitting_dims("reducer", nps_d, nps2);
            const ojson small = nearest_pipe(nps2, sched);
            if (dims.value("length", ojson(nullptr)).is_number()) {
                const double H = dims["length"].get<double>();
                f.leg = H / 2.0;
                if (aw >= 0 && small.value("ok", false)) {
                    const double aw2 = wall_area_mm2(small["outer_dia"].get<double>(),
                                                     small["inner_dia"].get<double>());
                    f.mass = 0.5 * (aw + aw2) * 1e-6 * (H * 1e-3) * rho;
                }
            } else {
                warnings.push_back("reducer " + f.id + ": " + nps.dump() + "x" + nps2.dump() +
                                   " not in the B16.9 catalog; take-out and mass unknown");
            }
            f.welds = 2;
            f.item["nps2"] = nps2;
            f.item["schedule"] = sched;
            f.item["material"] = mat;
            f.key = "reducer|" + nps.dump() + "|" + nps2.dump() + "|" + sched + "|" + mat;
        } else {  // flange
            const double cls =
                prm.value("flange_class", ojson(nullptr)).is_number() ? prm["flange_class"].get<double>() : 150.0;
            const std::string face =
                prm.value("flange_face", ojson(nullptr)).is_string() ? prm["flange_face"].get<std::string>() : "RF";
            ojson dims = fitting_dims("flange", nps_d, nullptr, cls);
            if (!dims.value("thickness", ojson(nullptr)).is_number() && std::lround(cls) != 150) {
                dims = fitting_dims("flange", nps_d, nullptr, 150);
                if (dims.value("thickness", ojson(nullptr)).is_number()) {
                    warnings.push_back("flange " + f.id + ": class " + std::to_string(std::lround(cls)) +
                                       " dims not catalogued; mass/take-out from class 150 (lower bound)");
                }
            }
            if (dims.value("thickness", ojson(nullptr)).is_number()) {
                const double thk = dims["thickness"].get<double>();
                const double fod = dims["od"].get<double>();
                const double bd = dims.value("bolt_dia", ojson(0.0)).is_number() ? dims["bolt_dia"].get<double>() : 0.0;
                const double nb = dims.value("bolt_holes", ojson(0.0)).is_number() ? dims["bolt_holes"].get<double>() : 0.0;
                const double bore = pipe.value("ok", false) ? pipe["inner_dia"].get<double>() : 0.0;
                f.leg = thk;
                const double area = kPi / 4.0 * (fod * fod - bore * bore) - nb * kPi / 4.0 * bd * bd;
                if (pipe.value("ok", false)) f.mass = area * 1e-6 * (thk * 1e-3) * rho;
            } else {
                warnings.push_back("flange " + f.id + ": NPS " + nps.dump() +
                                   " not in the B16.5 catalog; take-out and mass unknown");
            }
            f.welds = 1;  // weld-neck butt weld; the bolted face is not a weld
            f.item["flange_class"] = std::lround(cls);
            f.item["flange_face"] = face;
            f.item["material"] = mat;
            f.key = "flange|" + nps.dump() + "|" + std::to_string(std::lround(cls)) + "|" + face + "|" + mat;
        }
        if (threaded) f.welds = 0;
        for (const Touch& t : f.touches) runs[t.run].takeout += t.at_end ? f.leg : 2.0 * f.leg;
        fits.push_back(std::move(f));
    }

    // ── junctions between runs: connectivity, flange breaks, direct welds ───
    struct Junction {
        V3 p{};
        std::vector<Touch> touches;
        bool flanged = false;
        int welds = 0;  // butt welds not owned by a fitting
    };
    std::vector<Junction> junctions;
    for (size_t i = 0; i < runs.size(); ++i) {
        for (const V3& end : {runs[i].pts.front(), runs[i].pts.back()}) {
            bool seen = false;
            for (const Junction& j : junctions) seen = seen || dist(j.p, end) <= kSnap;
            if (seen) continue;
            Junction j;
            j.p = end;
            j.touches = touches_at(runs, end);
            if (j.touches.size() < 2) continue;
            int n_flanges = 0;
            bool any_fitting = false;
            for (const FitInfo& f : fits) {
                if (dist(f.origin, end) > kSnap) continue;
                any_fitting = true;
                if (f.type == "flange") ++n_flanges;
            }
            j.flanged = n_flanges > 0;
            if (j.flanged && n_flanges == 1) {
                warnings.push_back("flanged joint at " + ojson({pyround(end[0], 1), pyround(end[1], 1), pyround(end[2], 1)}).dump() +
                                   " has one flange fitting: the mating flange is not in the BOM");
            }
            if (!any_fitting) {
                bool threaded = false;
                for (const Touch& t : j.touches) threaded = threaded || runs[t.run].end_type == "THD";
                j.welds = threaded ? 0 : static_cast<int>(j.touches.size()) - 1;
                for (const Touch& t : j.touches) {
                    if (!t.at_end) {
                        warnings.push_back("run " + runs[t.run].id +
                                           " has a branch connection with no tee fitting (stub-in "
                                           "weld counted)");
                    }
                }
            }
            junctions.push_back(std::move(j));
        }
    }

    DSU dsu(runs.size());
    for (const Junction& j : junctions) {
        if (j.flanged) continue;
        for (size_t k = 1; k < j.touches.size(); ++k) dsu.unite(j.touches[0].run, j.touches[k].run);
    }

    // ── spools: components packed greedily up to max_spool_length_mm ───────
    std::vector<Spool> spools;
    int cut_welds = 0;
    std::map<size_t, std::vector<size_t>> comps;  // root -> runs (doc order)
    for (size_t i = 0; i < runs.size(); ++i) comps[dsu.find(i)].push_back(i);
    for (const auto& [root, members] : comps) {
        Spool cur;
        for (size_t ri : members) {
            const double L = runs[ri].length;
            double s = 0.0;
            while (L - s > (max_spool - cur.length) + 1e-6) {
                const double take = max_spool - cur.length;
                if (take > 1e-6) {
                    cur.pieces.push_back({ri, s, s + take});
                    cur.length += take;
                    s += take;
                    ++cut_welds;  // the pipe is cut here: one field weld
                }
                spools.push_back(std::move(cur));
                cur = Spool{};
            }
            if (L - s > 1e-6) {
                cur.pieces.push_back({ri, s, L});
                cur.length += L - s;
            }
        }
        if (!cur.pieces.empty()) spools.push_back(std::move(cur));
    }
    auto spool_of = [&](size_t run, double s) -> long {
        for (size_t k = 0; k < spools.size(); ++k)
            for (const Piece& p : spools[k].pieces)
                if (p.run == run && s >= p.s0 - 1e-6 && s <= p.s1 + 1e-6) return static_cast<long>(k);
        return -1;
    };

    int shop = 0;
    int field = cut_welds;
    auto place_welds = [&](const std::vector<Touch>& touches, int welds, const std::string* fid) {
        std::vector<long> ss;
        for (const Touch& t : touches) {
            const long k = spool_of(t.run, t.s);
            if (k >= 0 && std::find(ss.begin(), ss.end(), k) == ss.end()) ss.push_back(k);
        }
        const int f_welds = std::min(welds, std::max(0, static_cast<int>(ss.size()) - 1));
        field += f_welds;
        shop += welds - f_welds;
        if (!ss.empty()) {
            spools[static_cast<size_t>(ss[0])].shop_welds += welds - f_welds;
            if (fid != nullptr) spools[static_cast<size_t>(ss[0])].fittings.push_back(*fid);
        }
    };
    for (const FitInfo& f : fits) place_welds(f.touches, f.welds, &f.id);
    int flanged_joints = 0;
    for (const Junction& j : junctions) {
        if (j.flanged) ++flanged_joints;
        if (j.welds > 0) place_welds(j.touches, j.welds, nullptr);
    }

    // ── line list + items ───────────────────────────────────────────────────
    ojson line_list = ojson::array();
    std::vector<std::string> order;
    std::map<std::string, ojson> items;
    double pipe_net_m = 0.0;
    double centre_m = 0.0;
    double mass_total = 0.0;
    bool mass_partial = false;
    for (const RunInfo& r : runs) {
        double net = r.length - r.takeout;
        if (net < 0) {
            warnings.push_back("run " + r.id + ": fitting take-outs exceed its centreline length");
            net = 0;
        }
        ojson ll = ojson::object();
        ll["run_id"] = r.id;
        ll["name"] = r.name;
        ll["nps"] = r.nps;
        ll["schedule"] = r.schedule;
        ll["material"] = r.material;
        ll["end_type"] = r.end_type;
        ll["length_mm"] = pyround(r.length, 1);
        ll["cut_length_mm"] = pyround(net, 1);
        line_list.push_back(std::move(ll));

        const double kgm = wall_area_mm2(r.od, r.idia) * 1e-6 * r.rho;
        const std::string key = "pipe|" + r.nps.dump() + "|" + r.schedule + "|" + r.material;
        if (!items.count(key)) {
            ojson it = ojson::object();
            it["kind"] = "pipe";
            it["nps"] = r.nps;
            it["schedule"] = r.schedule;
            it["material"] = r.material;
            it["unit"] = "m";
            it["qty"] = 0;
            it["length_mm"] = 0.0;
            it["unit_mass_kg"] = pyround(kgm, 3);
            it["mass_kg"] = 0.0;
            items[key] = std::move(it);
            order.push_back(key);
        }
        ojson& it = items[key];
        it["qty"] = it["qty"].get<int>() + 1;
        it["length_mm"] = it["length_mm"].get<double>() + net;
        it["mass_kg"] = it["mass_kg"].get<double>() + kgm * net * 1e-3;
        pipe_net_m += net * 1e-3;
        centre_m += r.length * 1e-3;
        mass_total += kgm * net * 1e-3;
    }
    for (const FitInfo& f : fits) {
        if (!items.count(f.key)) {
            ojson it = f.item;
            it["unit"] = "ea";
            it["qty"] = 0;
            it["unit_mass_kg"] = f.mass >= 0 ? ojson(pyround(f.mass, 3)) : ojson(nullptr);
            it["mass_kg"] = f.mass >= 0 ? ojson(0.0) : ojson(nullptr);
            items[f.key] = std::move(it);
            order.push_back(f.key);
        }
        ojson& it = items[f.key];
        it["qty"] = it["qty"].get<int>() + 1;
        if (f.mass >= 0 && it["mass_kg"].is_number()) {
            it["mass_kg"] = it["mass_kg"].get<double>() + f.mass;
            mass_total += f.mass;
        } else {
            mass_partial = true;
        }
    }
    ojson items_out = ojson::array();
    for (const auto& k : order) {
        ojson it = items[k];
        if (it.contains("length_mm")) it["length_mm"] = pyround(it["length_mm"].get<double>(), 1);
        if (it["mass_kg"].is_number()) it["mass_kg"] = pyround(it["mass_kg"].get<double>(), 2);
        items_out.push_back(std::move(it));
    }
    if (mass_partial) warnings.push_back("total mass excludes fittings whose mass is unknown");

    ojson spools_out = ojson::array();
    for (size_t k = 0; k < spools.size(); ++k) {
        const Spool& sp = spools[k];
        ojson s = ojson::object();
        s["id"] = "SP" + std::to_string(k + 1);
        ojson rids = ojson::array();
        ojson pieces = ojson::array();
        for (const Piece& p : sp.pieces) {
            if (std::find(rids.begin(), rids.end(), ojson(runs[p.run].id)) == rids.end())
                rids.push_back(runs[p.run].id);
            pieces.push_back({{"run_id", runs[p.run].id},
                              {"from_mm", pyround(p.s0, 1)},
                              {"to_mm", pyround(p.s1, 1)}});
        }
        s["run_ids"] = std::move(rids);
        s["fitting_ids"] = sp.fittings;
        s["length_mm"] = pyround(sp.length, 1);
        s["shop_welds"] = sp.shop_welds;
        s["pieces"] = std::move(pieces);
        spools_out.push_back(std::move(s));
    }

    ojson totals = ojson::object();
    totals["pipe_length_m"] = pyround(pipe_net_m, 3);
    totals["centreline_length_m"] = pyround(centre_m, 3);
    totals["mass_kg"] = pyround(mass_total, 2);
    totals["shop_welds"] = shop;
    totals["field_welds"] = field;
    totals["flanged_joints"] = flanged_joints;
    totals["spools"] = spools.size();

    ojson out = ojson::object();
    out["ok"] = true;
    out["line_list"] = std::move(line_list);
    out["items"] = std::move(items_out);
    out["spools"] = std::move(spools_out);
    out["totals"] = std::move(totals);
    out["max_spool_length_mm"] = max_spool;
    out["warnings"] = warnings;
    out["hint"] = std::to_string(runs.size()) + " line(s), " + pyutil::fmt_f(pipe_net_m, 2) +
                  " m cut pipe, " + std::to_string(spools.size()) + " spool(s), " +
                  std::to_string(shop) + " shop / " + std::to_string(field) + " field welds";
    out["next_actions"] = ojson::array({"piping_spec_check", "piping_solve"});
    return out;
}

}  // namespace chudware::piping
