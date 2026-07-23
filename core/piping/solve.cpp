// core/piping/solve.cpp — port of engine_oracle/piping/solve.py.
// The solve is the hydraulic analogue of nodal analysis: nodal heads play the
// role of node voltages, the pipe dQ/dH conductance is the MNA conductance
// stamp, a fixed-head node is ground. Newton–Raphson with Reynolds-refreshed
// friction + backtracking line search, mirroring the oracle iteration-for-
// iteration (convergence behaviour is part of the parity surface).
#define _USE_MATH_DEFINES  // MSVC needs this before <cmath> for M_PI (mirrors cam/feeds.cpp)
#include "piping/solve.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstring>
#include <fstream>
#include <map>
#include <sstream>

#include "piping/catalog.h"
#include "piping/schema.h"

namespace chudware::piping {
namespace {

using pyutil::fmt_f;
using pyutil::fmt_g;
using pyutil::jerr;
using pyutil::py_float;
using pyutil::py_repr;
using pyutil::py_truthy;
using pyutil::pyround;

constexpr double kMinAbsQ = 1e-9;    // m³/s flow floor for friction Reynolds
constexpr double kQJacFloor = 1e-5;  // m³/s |Q| floor in the dQ/dH Jacobian only

// Absolute roughness ε (m) by material family (advisory; drives friction only).
double roughness_for(const std::string& mat) {
    if (mat == "A106-B" || mat == "A53-B" || mat == "A333-6") return 4.6e-5;
    if (mat == "A312-304" || mat == "A312-316") return 1.5e-5;
    if (mat == "copper" || mat == "PVC" || mat == "CPVC") return 1.5e-6;
    return 4.6e-5;  // commercial steel
}

// ── geometry helpers ─────────────────────────────────────────────────────────

using Vec3 = std::array<double, 3>;

double dist3(const Vec3& a, const Vec3& b) {
    double s = 0.0;
    for (int i = 0; i < 3; ++i) s += (a[i] - b[i]) * (a[i] - b[i]);
    return std::sqrt(s);
}

Vec3 to_vec3(const ojson& p) {
    Vec3 out{0.0, 0.0, 0.0};
    for (int i = 0; i < 3 && i < static_cast<int>(p.size()); ++i)
        out[i] = pyutil::to_double(p[i]);
    return out;
}

double polyline_length_mm(const ojson& path) {
    double L = 0.0;
    for (size_t i = 0; i + 1 < path.size(); ++i) L += dist3(to_vec3(path[i]), to_vec3(path[i + 1]));
    return L;
}

double seg_dist(const Vec3& p, const Vec3& a, const Vec3& b) {
    Vec3 ab{b[0] - a[0], b[1] - a[1], b[2] - a[2]};
    Vec3 ap{p[0] - a[0], p[1] - a[1], p[2] - a[2]};
    double denom = 0.0, dot = 0.0;
    for (int i = 0; i < 3; ++i) {
        denom += ab[i] * ab[i];
        dot += ap[i] * ab[i];
    }
    const double t = denom == 0.0 ? 0.0 : std::max(0.0, std::min(1.0, dot / denom));
    Vec3 proj{a[0] + t * ab[0], a[1] + t * ab[1], a[2] + t * ab[2]};
    return dist3(p, proj);
}

bool point_on_polyline(const Vec3& pt, const ojson& path, double tol) {
    for (size_t i = 0; i + 1 < path.size(); ++i)
        if (seg_dist(pt, to_vec3(path[i]), to_vec3(path[i + 1])) <= tol) return true;
    return false;
}

// ── network assembly ─────────────────────────────────────────────────────────

struct Node {
    std::string id;
    Vec3 xyz{0, 0, 0};  // mm
    int idx = 0;
    bool fixed = false;
    double head = 0.0;    // m (liquid) / P² (gas), set for fixed nodes
    double demand = 0.0;  // m³/s (liquid) / kg/s (gas) leaving the system
};

struct Link {
    std::string id;
    int a = 0, b = 0;
    double length_m = 0.0;
    double d_m = 0.0;  // inner diameter (m)
    double area = 0.0;
    double eps = 4.6e-5;
    double kfit = 0.0;  // summed fitting K
    std::vector<ojson> kbreak;
    ojson pump = nullptr;  // optional pump dict (head-vs-flow)
    double flow = 1e-4;    // m³/s (liquid) / kg/s (gas) current iterate
    double fd = 0.02;
    double mu = 0.0;   // fluid viscosity (0 = unknown → laminar closed form off)
    double rho = 0.0;
};

struct NetCtx {
    bool ok = false;
    ojson err;
    std::vector<Node> nodes;
    std::vector<Link> links;
    std::vector<std::string> warnings;
};

void snap_nodes(const ojson& runs, double tol, NetCtx& ctx,
                std::map<int, std::pair<int, int>>& run_ep) {
    auto get = [&](const Vec3& xyz) -> int {
        for (const auto& nd : ctx.nodes)
            if (dist3(nd.xyz, xyz) <= tol) return nd.idx;
        const int idx = static_cast<int>(ctx.nodes.size());
        Node nd;
        nd.id = "n" + std::to_string(idx);
        nd.xyz = xyz;
        nd.idx = idx;
        ctx.nodes.push_back(nd);
        return idx;
    };
    for (int ri = 0; ri < static_cast<int>(runs.size()); ++ri) {
        const ojson& path = runs[ri]["path"];
        run_ep[ri] = {get(to_vec3(path[0])), get(to_vec3(path[path.size() - 1]))};
    }
}

// Default fitting K-factors (Crane TP-410 order of magnitude); the report
// always records which method produced each K.
constexpr double kKTeeRun = 0.6, kKTeeBranch = 1.3, kKFlange = 0.0, kKReducer = 0.10;

// fluids.fittings.bend_rounded, Rennels method (fd given).
double bend_rounded_rennels(double Di, double angle_deg, double fd, double bend_diameters) {
    constexpr double kPi = 3.14159265358979323846;
    const double angle = angle_deg * kPi / 180.0;  // radians(angle)
    const double rc = Di * bend_diameters;
    const double sin_term = std::sin(0.5 * angle);
    return fd * angle * rc / Di + (0.10 + 2.4 * fd) * sin_term +
           6.6 * fd * (std::sqrt(sin_term) + sin_term) /
               std::pow(rc / Di, 4.0 * angle / kPi);
}

// fluids.fittings.contraction_conical, Rennels method (fd + cone length given).
double contraction_conical_rennels(double Di1, double Di2, double l, double fd) {
    constexpr double kPi = 3.14159265358979323846;
    const double beta = Di2 / Di1;
    const double angle_rad = l != 0.0 ? 2.0 * std::atan((Di1 - Di2) / (2.0 * l)) : kPi;
    const double beta2 = beta * beta;
    const double beta4 = beta2 * beta2;
    const double beta5 = beta4 * beta;
    const double lbd = 1.0 + 0.622 * std::pow(angle_rad / kPi, 0.8) *
                                  (1.0 - 0.215 * beta2 - 0.785 * beta5);
    const double sin_half_angle = std::sin(0.5 * angle_rad);
    const double K_fr2 = fd * (1.0 - beta4) / (8.0 * sin_half_angle);
    const double K_conv2 =
        0.0696 * sin_half_angle * (1.0 - beta5) * lbd * lbd + (lbd - 1.0) * (lbd - 1.0);
    return K_fr2 + K_conv2;
}

// K-factor for a fitting on a pipe of inner-dia d_m (m) at friction fd.
std::pair<double, std::string> fitting_K(const ojson& ft, double d_m, double fd) {
    const std::string ftype = ft.value("type", std::string());
    const ojson params = ft.value("params", ojson::object());
    const auto getp = [&](const char* k) -> ojson {
        return params.is_object() ? params.value(k, ojson(nullptr)) : ojson(nullptr);
    };

    if (ftype == "elbow") {
        const ojson angj = getp("angle");
        // params.get("angle", 90) or 90 — the resolved value feeds both the
        // K formula and the method string.
        const ojson angle_v = py_truthy(angj) ? angj : ojson(90);
        const double angle = pyutil::to_double(angle_v);
        const ojson radj = getp("radius");
        const std::string radius =
            py_truthy(radj) ? radj.get<std::string>() : std::string("LR");
        std::string up = radius;
        std::transform(up.begin(), up.end(), up.begin(),
                       [](unsigned char c) { return std::toupper(c); });
        const double bd = up == "LR" ? 1.5 : 1.0;
        try {
            return {bend_rounded_rennels(d_m, angle, fd, bd),
                    "bend_rounded(" + radius + "," + pyutil::py_str(angle_v) + "deg)"};
        } catch (...) {
        }
    }
    if (ftype == "reducer") {
        try {
            const ojson nps2 = ft.value("nps2", ojson(nullptr));
            const ojson d2 = nearest_pipe(nps2, ft.value("schedule", std::string("40")));
            if (d2.value("ok", false)) {
                const double d_small = d2["inner_dia"].get<double>() / 1000.0;
                const double d1 = std::max(d_m, d_small), d0 = std::min(d_m, d_small);
                // contraction_conical needs a cone length; use the catalog reducer length.
                const ojson dims = fitting_dims("reducer", ft.value("nps", ojson(nullptr)).get<double>(),
                                                nps2);
                const double L = (dims.value("length", ojson(nullptr)).is_number()
                                      ? dims["length"].get<double>()
                                      : 0.0) /
                                 1000.0;
                if (L > 0) {
                    return {contraction_conical_rennels(d1, d0, L, fd), "contraction_conical"};
                }
            }
        } catch (...) {
        }
        return {kKReducer, "default(reducer)"};
    }
    if (ftype == "tee") {
        const ojson branch = getp("branch_nps");
        const ojson nps = ft.value("nps", ojson(nullptr));
        const double nps_v = py_truthy(nps) ? pyutil::to_double(nps) : 0.0;
        if (!branch.is_null() && pyutil::to_double(branch) < nps_v) {
            return {kKTeeBranch, "default(tee_branch)"};
        }
        return {kKTeeRun, "default(tee_run)"};
    }
    if (ftype == "flange") return {kKFlange, "default(flange)"};
    if (ftype == "elbow") return {0.3, "default(elbow)"};  // fluids-absent fallback
    return {0.0, "default(none)"};
}

NetCtx build_network(const ojson& doc, const ojson& fluid, double tol) {
    NetCtx ctx;
    ojson runs_all = doc.value("runs", ojson::array());
    ojson runs = ojson::array();
    for (const auto& r : runs_all)
        if (!r.value("suppressed", false)) runs.push_back(r);
    if (runs.empty()) {
        ctx.err = jerr("no (unsuppressed) pipe runs to solve",
                       "add at least one run to the piping document");
        return ctx;
    }

    std::map<int, std::pair<int, int>> run_ep;
    snap_nodes(runs, tol, ctx, run_ep);
    const ojson doc_material = doc.value("material", ojson(nullptr));

    for (int ri = 0; ri < static_cast<int>(runs.size()); ++ri) {
        const ojson& run = runs[ri];
        Link lk;
        lk.id = run.value("id", "r" + std::to_string(ri));
        lk.a = run_ep[ri].first;
        lk.b = run_ep[ri].second;
        lk.length_m = polyline_length_mm(run["path"]) / 1000.0;
        const ojson bore_override = run.value("bore_mm", ojson(nullptr));
        if (py_truthy(bore_override)) {
            // Explicit inner diameter (mm) — externally-supplied networks (e.g.
            // the hydraulic→piping handoff) bypass the schedule lookup.
            lk.d_m = pyutil::to_double(bore_override) / 1000.0;
        } else {
            const ojson spec = nearest_pipe(run.value("nps", ojson(nullptr)),
                                            run.value("schedule", std::string("40")));
            if (!spec.value("ok", false)) {
                ctx.warnings.push_back(lk.id + ": " + spec.value("reason", std::string()));
                // fall back to nps inches as bore so the network stays solvable
                const ojson nps = run.value("nps", ojson(nullptr));
                const double bore_mm =
                    (py_truthy(nps) ? pyutil::to_double(nps) : 1.0) * 25.4;
                lk.d_m = bore_mm / 1000.0;
            } else {
                lk.d_m = spec["inner_dia"].get<double>() / 1000.0;
            }
        }
        lk.area = M_PI * lk.d_m * lk.d_m / 4.0;
        const ojson mat = run.value("material", ojson(nullptr));
        const ojson mat_eff = py_truthy(mat) ? mat : doc_material;
        lk.eps = roughness_for(mat_eff.is_string() ? mat_eff.get<std::string>() : "");
        // fluid props on the link so link_flow can pick the regime-consistent
        // (laminar vs square-law) local head-flow relation
        lk.mu = fluid.is_object() && py_truthy(fluid.value("mu", ojson(nullptr)))
                    ? pyutil::to_double(fluid["mu"])
                    : 0.0;
        lk.rho = fluid.is_object() && py_truthy(fluid.value("rho", ojson(nullptr)))
                     ? pyutil::to_double(fluid["rho"])
                     : 0.0;
        if (lk.length_m <= 0) {
            ctx.warnings.push_back(lk.id + ": zero-length run skipped");
            continue;
        }
        ctx.links.push_back(std::move(lk));
    }

    if (ctx.links.empty()) {
        ctx.err = jerr("no solvable links (all runs zero-length or unresolved)",
                       "check run paths and nps/schedule");
        return ctx;
    }

    // Attach fittings to the run(s) they lie on (K-factor loss elements).
    std::vector<const ojson*> nonzero_runs;
    for (const auto& r : runs)
        if (polyline_length_mm(r["path"]) > 0) nonzero_runs.push_back(&r);
    for (const auto& ft : doc.value("fittings", ojson::array())) {
        if (ft.value("suppressed", false)) continue;
        const Vec3 origin = to_vec3(ft.value("origin", ojson::array({0.0, 0.0, 0.0})));
        bool placed = false;
        for (size_t i = 0; i < ctx.links.size() && i < nonzero_runs.size(); ++i) {
            if (point_on_polyline(origin, (*nonzero_runs[i])["path"], tol)) {
                auto [K, method] = fitting_K(ft, ctx.links[i].d_m, ctx.links[i].fd);
                ctx.links[i].kfit += K;
                ojson kb = ojson::object();
                kb["fitting"] = ft.value("id", ojson(nullptr));
                kb["type"] = ft.value("type", ojson(nullptr));
                kb["K"] = pyround(K, 4);
                kb["method"] = method;
                ctx.links[i].kbreak.push_back(std::move(kb));
                placed = true;
                break;
            }
        }
        if (!placed) {
            ctx.warnings.push_back("fitting " + pyutil::py_str(ft.value("id", ojson(nullptr))) +
                                   " at " + py_repr(ft.value("origin", ojson(nullptr))) +
                                   " not on any run — K ignored");
        }
    }

    ctx.ok = true;
    return ctx;
}

// ── boundary-condition resolution ────────────────────────────────────────────

// Resolve a BC node reference: {"node": "n0"} / {"at": [x,y,z]} (nearest
// within tol) / {"run": "r1", "end": "start"|"end"}. -1 = unresolved.
int resolve_node(const ojson& ref, const NetCtx& ctx, double tol) {
    if (ref.is_object() && ref.contains("node")) {
        const std::string want = ref["node"].is_string() ? ref["node"].get<std::string>() : "";
        for (const auto& nd : ctx.nodes)
            if (nd.id == want) return nd.idx;
        return -1;
    }
    if (ref.is_object() && ref.contains("at")) {
        const Vec3 at = to_vec3(ref["at"]);
        int best = -1;
        double bd = tol;
        for (const auto& nd : ctx.nodes) {
            const double d = dist3(nd.xyz, at);
            if (d <= bd) {
                best = nd.idx;
                bd = d;
            }
        }
        return best;
    }
    if (ref.is_object() && ref.contains("run")) {
        const std::string want = ref["run"].is_string() ? ref["run"].get<std::string>() : "";
        for (const auto& lk : ctx.links)
            if (lk.id == want)
                return ref.value("end", std::string("start")) == "start" ? lk.a : lk.b;
        return -1;
    }
    return -1;
}

ojson apply_boundary(NetCtx& ctx, const ojson& boundary, double tol, double rho) {
    const ojson bcs = boundary.is_array() ? boundary : boundary.value("nodes", ojson::array());
    int n_fixed = 0;
    for (const auto& bc : bcs) {
        const int idx = resolve_node(bc, ctx, tol);
        if (idx < 0) {
            return jerr("boundary reference " + py_repr(bc) + " did not resolve to a node",
                        "use {'node':'n0'} / {'at':[x,y,z]} / {'run':'r1','end':'start'}");
        }
        Node& nd = ctx.nodes[idx];
        const double z = nd.xyz[2] / 1000.0;
        if (bc.is_object() && bc.contains("head")) {
            nd.fixed = true;
            nd.head = pyutil::to_double(bc["head"]);
            ++n_fixed;
        } else if (bc.is_object() && bc.contains("pressure")) {  // kPa gauge -> head
            nd.fixed = true;
            nd.head = z + pyutil::to_double(bc["pressure"]) * 1000.0 / (rho * kG);
            ++n_fixed;
        } else if (bc.is_object() && bc.contains("demand")) {  // m³/h leaving (+)
            nd.demand += pyutil::to_double(bc["demand"]) / 3600.0;
        } else if (bc.is_object() && bc.contains("demand_lps")) {
            nd.demand += pyutil::to_double(bc["demand_lps"]) / 1000.0;
        }
    }
    if (n_fixed == 0) {
        return jerr("network under-constrained: no fixed-head/pressure node",
                    "give at least one boundary with 'pressure' (kPa) or 'head' (m)");
    }
    return nullptr;
}

// ── pump curves ───────────────────────────────────────────────────────────────

std::string trim(const std::string& s) {
    const size_t a = s.find_first_not_of(" \t\r\n");
    if (a == std::string::npos) return "";
    const size_t b = s.find_last_not_of(" \t\r\n");
    return s.substr(a, b - a + 1);
}

std::vector<std::string> split_csv_line(const std::string& line) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : line) {
        if (c == ',') {
            out.push_back(cur);
            cur.clear();
        } else {
            cur += c;
        }
    }
    out.push_back(cur);
    return out;
}

std::string lower_str(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    return s;
}

// Python float(): trims whitespace; accepts inf/nan like strtod.
bool py_float_parse(const std::string& s, double& out) {
    const std::string t = trim(s);
    if (t.empty()) return false;
    char* end = nullptr;
    out = std::strtod(t.c_str(), &end);
    return end != t.c_str() && *end == '\0';
}

}  // namespace

// os.path.isfile(src) raises TypeError for a non-path input; the oracle wraps
// that as "cannot read pump curve: <exc>". Mirror it for non-string sources.
ojson load_pump_curve_json(const ojson& src, double speed_ratio,
                           const std::string& flow_col, const std::string& head_col) {
    if (src.is_string()) return load_pump_curve(src.get<std::string>(), speed_ratio,
                                                flow_col, head_col);
    const char* kind = src.is_object()   ? "dict"
                       : src.is_array()  ? "list"
                       : src.is_number() ? "float"
                                         : "NoneType";
    return pyutil::jerr("cannot read pump curve: stat: path should be string, bytes, "
                        "os.PathLike or integer, not " +
                        std::string(kind));
}

ojson load_pump_curve(const std::string& csv_source, double speed_ratio,
                      const std::string& flow_col, const std::string& head_col) {
    std::string text;
    {
        std::ifstream fh(csv_source);
        if (fh.good()) {
            std::ostringstream ss;
            ss << fh.rdbuf();
            text = ss.str();
        } else {
            text = csv_source;
        }
    }

    // csv.DictReader semantics: first row is the header (an empty first row
    // yields NO fieldnames -> "empty" error, exactly like the oracle).
    std::vector<std::string> lines;
    {
        std::istringstream in(text);
        std::string line;
        while (std::getline(in, line)) {
            if (!line.empty() && line.back() == '\r') line.pop_back();
            lines.push_back(line);
        }
    }
    if (lines.empty() || lines[0].empty()) {
        return jerr("pump curve CSV is empty",
                    "need a header row with flow,head columns");
    }
    const std::vector<std::string> fieldnames = split_csv_line(lines[0]);
    std::map<std::string, std::string> fmap;  // strip().lower() -> original
    for (const auto& c : fieldnames) fmap[lower_str(trim(c))] = c;
    auto find_col = [&](const std::string& want) -> std::string {
        auto it = fmap.find(lower_str(want));
        return it != fmap.end() ? it->second : "";
    };
    std::string fk = find_col(flow_col);
    if (fk.empty()) fk = find_col("q");
    if (fk.empty()) fk = find_col("flow_m3h");
    std::string hk = find_col(head_col);
    if (hk.empty()) hk = find_col("h");
    if (hk.empty()) hk = find_col("head_m");
    if (fk.empty() || hk.empty()) {
        ojson cols = ojson::array();
        for (const auto& c : fieldnames) cols.push_back(c);
        return jerr("pump curve needs '" + flow_col + "' and '" + head_col + "' columns",
                    "found columns: " + py_repr(cols));
    }
    const double r = speed_ratio;
    std::vector<std::array<double, 2>> pts;
    for (size_t li = 1; li < lines.size(); ++li) {
        const auto cells = split_csv_line(lines[li]);
        std::string qstr, hstr;
        bool have_q = false, have_h = false;
        for (size_t i = 0; i < fieldnames.size() && i < cells.size(); ++i) {
            if (fieldnames[i] == fk) {
                qstr = cells[i];
                have_q = true;
            }
            if (fieldnames[i] == hk) {
                hstr = cells[i];
                have_h = true;
            }
        }
        double q, h;
        if (!have_q || !have_h || !py_float_parse(qstr, q) || !py_float_parse(hstr, h))
            continue;
        pts.push_back({q * r, h * r * r});
    }
    if (pts.size() < 2) {
        return jerr("pump curve needs >= 2 valid points", "check numeric flow/head cells");
    }
    std::stable_sort(pts.begin(), pts.end(),
                     [](const auto& a, const auto& b) { return a[0] < b[0]; });
    ojson points = ojson::array();
    for (const auto& p : pts) points.push_back(ojson::array({p[0], p[1]}));
    ojson out = ojson::object();
    out["ok"] = true;
    out["points"] = std::move(points);
    out["shutoff_head"] = pts[0][1];
    out["max_flow"] = pts[pts.size() - 1][0];
    out["speed_ratio"] = r;
    return out;
}

namespace {

// Pump head (m) and slope d(head)/dQ (m per m³/s) at a flow magnitude (m³/s):
// piecewise-linear over the curve, flat extrapolation past both ends.
std::pair<double, double> pump_head_and_slope(const ojson& pump, double flow_m3s) {
    const double q = std::max(0.0, flow_m3s) * 3600.0;  // m³/h
    const ojson& pts = pump["points"];
    const auto q_at = [&](size_t i) { return pts[i][0].get<double>(); };
    const auto h_at = [&](size_t i) { return pts[i][1].get<double>(); };
    const size_t n = pts.size();
    if (q <= q_at(0)) return {h_at(0), 0.0};
    if (q >= q_at(n - 1)) return {h_at(n - 1), 0.0};
    for (size_t i = 0; i + 1 < n; ++i) {
        const double q0 = q_at(i), h0 = h_at(i);
        const double q1 = q_at(i + 1), h1 = h_at(i + 1);
        if (q0 <= q && q <= q1 && q1 > q0) {
            const double slope_per_m3h = (h1 - h0) / (q1 - q0);
            return {h0 + (q - q0) * slope_per_m3h, slope_per_m3h * 3600.0};
        }
    }
    return {h_at(n - 1), 0.0};
}

double pump_head(const ojson& pump, double flow_m3s) {
    return pump_head_and_slope(pump, flow_m3s).first;
}

ojson attach_pumps(NetCtx& ctx, const ojson& pumps) {
    for (const auto& spec : pumps.is_array() ? pumps : ojson::array()) {
        const std::string run_id = spec.value("run", std::string());
        Link* lk = nullptr;
        for (auto& l : ctx.links)
            if (l.id == run_id) lk = &l;
        if (lk == nullptr) {
            ojson ids = ojson::array();
            for (const auto& l : ctx.links) ids.push_back(l.id);
            return jerr("pump references unknown run " + py_repr(run_id),
                        "runs available: " + py_repr(ids));
        }
        const ojson curve = spec.value("curve", ojson(nullptr));
        ojson pump = nullptr;
        if (curve.is_object() && curve.value("ok", false)) {
            pump = curve;
        } else if (py_truthy(curve)) {
            pump = load_pump_curve_json(curve, spec.value("speed_ratio", 1.0), "flow",
                                        "head");
        }
        if (pump.is_null() || !pump.value("ok", false)) {
            return pump.is_object() ? pump : jerr("pump missing 'curve'");
        }
        pump["sign"] = spec.value("direction", std::string()) == "b_to_a" ? -1.0 : 1.0;
        lk->pump = std::move(pump);
    }
    return nullptr;
}

// ── friction + solve ──────────────────────────────────────────────────────────

// fluids.friction.Clamond (fast=False) — the exact Colebrook solution the
// oracle's friction_factor() default evaluates.
double clamond(double Re, double eD) {
    const double X1 = eD * Re * 0.1239681863354175460160858261654858382699;
    const double X2 = std::log(Re) - 0.7793974884556819406441139701653776731705;
    double F = X2 - 0.2;
    double X1F = X1 + F;
    double X1F1 = 1.0 + X1F;
    double E = (std::log(X1F) - 0.2) / X1F1;
    F = F - (X1F1 + 0.5 * E) * E * X1F / (X1F1 + E * (1.0 + (1.0 / 3.0) * E));
    X1F = X1 + F;
    X1F1 = 1.0 + X1F;
    E = (std::log(X1F) + F - X2) / X1F1;
    const double b = X1F1 + E * (1.0 + (1.0 / 3.0) * E);
    F = b / (b * F - ((X1F1 + 0.5 * E) * E * X1F));
    return 1.325474527619599502640416597148504422899 * (F * F);
}

// fluids.Reynolds(V, D, rho, mu): nu = mu/rho; V*D/nu (same op order as the
// oracle for bit-parity).
double reynolds(double V, double D, double rho, double mu) {
    const double nu = mu / rho;
    return V * D / nu;
}

double friction_factor_lk(const Link& lk, double q_abs, double mu, double rho) {
    const double v = q_abs / lk.area;
    const double Re = reynolds(std::max(v, 1e-9), lk.d_m, rho, mu);
    if (Re < 2100) return std::max(64.0 / std::max(Re, 1e-6), 1e-4);
    return clamond(Re, lk.eps / lk.d_m);
}

// r such that friction+fitting headloss h = r·Q² (m), at the current fd + K.
double resistance(const Link& lk) {
    const double c_pipe = lk.fd * lk.length_m / (2.0 * kG * lk.d_m * lk.area * lk.area);
    const double c_fit = lk.kfit / (2.0 * kG * lk.area * lk.area);
    return c_pipe + c_fit;
}

// Flow q (m³/s, a→b positive) and conductance p = dq/d(ΔH) for a head
// difference — see the oracle's _link_flow for the laminar closed form and
// the pump inner-Newton.
std::pair<double, double> link_flow(Link& lk, double dH) {
    const double r = resistance(lk);
    if (r <= 0) return {0.0, 0.0};
    if (lk.pump.is_null()) {
        // Regime-consistent local solve (Hagen-Poiseuille is LINEAR in Q; the
        // square law with a stale fd oscillates the outer Newton on laminar
        // links — see the oracle's comment).
        if (lk.mu > 0.0 && lk.rho > 0.0 && lk.d_m > 0.0) {
            const double c_lam =
                128.0 * lk.mu * lk.length_m / (lk.rho * kG * M_PI * std::pow(lk.d_m, 4));
            const double r_fit = lk.kfit / (2.0 * kG * lk.area * lk.area);
            double q_mag;
            if (r_fit > 0.0) {
                q_mag = (-c_lam + std::sqrt(c_lam * c_lam + 4.0 * r_fit * std::abs(dH))) /
                        (2.0 * r_fit);
            } else {
                q_mag = c_lam > 0.0 ? std::abs(dH) / c_lam : 0.0;
            }
            const double re =
                lk.area ? lk.rho * (q_mag / lk.area) * lk.d_m / lk.mu : 0.0;
            if (re < 2100.0) {
                lk.fd = std::max(64.0 / std::max(re, 1e-6), 1e-4);
                const double q = std::copysign(q_mag, dH);
                const double denom = c_lam + 2.0 * r_fit * q_mag;
                const double p = denom > 0 ? 1.0 / denom : 0.0;
                return {q, p};
            }
        }
        const double q = std::copysign(std::sqrt(std::abs(dH) / r), dH);
        return {q, 1.0 / (2.0 * r * std::max(std::abs(q), kQJacFloor))};
    }
    const double sign = lk.pump["sign"].get<double>();
    double q = lk.flow != 0.0 ? lk.flow : 1e-4;
    for (int i = 0; i < 40; ++i) {
        auto [hp, dhpdq] = pump_head_and_slope(lk.pump, std::abs(q));
        const double sgn_q = q >= 0 ? 1.0 : -1.0;
        const double f = r * q * std::abs(q) - sign * hp - dH;
        const double dfdq = 2.0 * r * std::abs(q) - sign * dhpdq * sgn_q;
        if (dfdq == 0) break;
        const double step = f / dfdq;
        q -= step;
        if (std::abs(step) < 1e-12) break;
    }
    const double dhpdq = pump_head_and_slope(lk.pump, std::abs(q)).second;
    const double dfdq = 2.0 * r * std::max(std::abs(q), kQJacFloor) -
                        sign * dhpdq * (q >= 0 ? 1.0 : -1.0);
    return {q, 1.0 / std::max(dfdq, 1e-9)};
}

// Dense LU solve with partial pivoting (the LAPACK dgesv analogue numpy uses).
// Returns false on a singular system.
bool lu_solve(std::vector<std::vector<double>>& A, std::vector<double>& b,
              std::vector<double>& x) {
    const int n = static_cast<int>(b.size());
    x.assign(n, 0.0);
    for (int col = 0; col < n; ++col) {
        int piv = col;
        for (int r = col + 1; r < n; ++r)
            if (std::abs(A[r][col]) > std::abs(A[piv][col])) piv = r;
        if (A[piv][col] == 0.0) return false;
        if (piv != col) {
            std::swap(A[piv], A[col]);
            std::swap(b[piv], b[col]);
        }
        for (int r = col + 1; r < n; ++r) {
            const double f = A[r][col] / A[col][col];
            if (f == 0.0) continue;
            for (int c = col; c < n; ++c) A[r][c] -= f * A[col][c];
            b[r] -= f * b[col];
        }
    }
    for (int r = n - 1; r >= 0; --r) {
        double s = b[r];
        for (int c = r + 1; c < n; ++c) s -= A[r][c] * x[c];
        x[r] = s / A[r][r];
    }
    return true;
}

double max_abs(const std::vector<double>& v) {
    double m = 0.0;
    for (double x : v) m = std::max(m, std::abs(x));
    return m;
}

// Iterate the flow<->friction fixed point at converged heads; returns the max
// free-node mass imbalance (m³/s).
double finalize_flows(std::vector<Node>& nodes, std::vector<Link>& links,
                      const std::vector<double>& H, double mu, double rho) {
    for (int i = 0; i < 60; ++i) {
        double dmax = 0.0;
        for (auto& lk : links) {
            lk.fd = friction_factor_lk(lk, std::max(std::abs(lk.flow), kMinAbsQ), mu, rho);
            const double q = link_flow(lk, H[lk.a] - H[lk.b]).first;
            dmax = std::max(dmax, std::abs(q - lk.flow));
            lk.flow = q;
        }
        if (dmax < 1e-10) break;
    }
    std::map<int, double> imbalance;
    for (const auto& nd : nodes)
        imbalance[nd.idx] = nd.fixed ? 0.0 : -nd.demand;
    for (const auto& lk : links) {
        imbalance[lk.b] += lk.flow;
        imbalance[lk.a] -= lk.flow;
    }
    double m = 0.0;
    for (const auto& nd : nodes)
        if (!nd.fixed) m = std::max(m, std::abs(imbalance[nd.idx]));
    return m;
}

ojson assemble_result(const NetCtx& ctx, const std::vector<Node>& nodes,
                      const std::vector<Link>& links, const std::vector<double>& H,
                      const ojson& fl, bool converged, int it, double max_imbalance) {
    const double rho = fl["rho"].get<double>();
    ojson node_out = ojson::array();
    for (const auto& nd : nodes) {
        const double z = nd.xyz[2] / 1000.0;
        const double head = H[nd.idx];
        ojson row = ojson::object();
        row["id"] = nd.id;
        row["xyz"] = ojson::array({nd.xyz[0], nd.xyz[1], nd.xyz[2]});
        row["fixed"] = nd.fixed;
        row["head_m"] = pyround(head, 4);
        row["pressure_kpa"] = pyround(rho * kG * (head - z) / 1000.0, 3);
        row["demand_m3h"] = pyround(nd.demand * 3600.0, 4);
        node_out.push_back(std::move(row));
    }
    ojson link_out = ojson::array();
    for (const auto& lk : links) {
        const double q = lk.flow;
        const double v = lk.area ? q / lk.area : 0.0;
        const double r = resistance(lk);
        const double headloss = r * q * std::abs(q);
        const double Re =
            lk.area ? reynolds(std::abs(v), lk.d_m, rho, fl["mu"].get<double>()) : 0.0;
        ojson row = ojson::object();
        row["id"] = lk.id;
        row["from"] = nodes[lk.a].id;
        row["to"] = nodes[lk.b].id;
        row["length_mm"] = pyround(lk.length_m * 1000.0, 2);
        row["id_mm"] = pyround(lk.d_m * 1000.0, 3);
        row["flow_m3h"] = pyround(q * 3600.0, 4);
        row["velocity_ms"] = pyround(v, 4);
        row["reynolds"] = pyround(Re, 1);
        row["friction_factor"] = pyround(lk.fd, 5);
        row["headloss_m"] = pyround(headloss, 4);
        row["dp_kpa"] = pyround(rho * kG * headloss / 1000.0, 3);
        row["k_total"] = pyround(lk.kfit, 4);
        row["pump_head_m"] = !lk.pump.is_null()
                                 ? ojson(pyround(lk.pump["sign"].get<double>() *
                                                     pump_head(lk.pump, std::abs(q)),
                                                 3))
                                 : ojson(0.0);
        link_out.push_back(std::move(row));
    }
    ojson hint = nullptr;
    ojson next_actions = ojson::array();
    if (!converged) {
        hint = "solver did not converge in " + std::to_string(it) +
               " iterations (max node imbalance " + fmt_g(max_imbalance * 3600.0, 3) +
               " m³/h) — check connectivity / boundary conditions";
        next_actions.push_back("verify every run endpoint meets another run or a boundary node");
    } else {
        next_actions.push_back("call piping_report for the per-segment K-factor pressure-drop table");
    }
    ojson fluid_out = ojson::object();
    fluid_out["name"] = fl.value("name", std::string("custom"));
    fluid_out["rho"] = fl["rho"];
    fluid_out["mu"] = fl["mu"];
    fluid_out["source"] = fl.value("source", ojson(nullptr));
    fluid_out["phase"] = fl.value("phase", std::string("liquid"));

    ojson out = ojson::object();
    out["ok"] = true;
    out["converged"] = converged;
    out["iterations"] = it;
    out["max_imbalance_m3h"] = pyround(max_imbalance * 3600.0, 6);
    out["fluid"] = std::move(fluid_out);
    out["nodes"] = std::move(node_out);
    out["links"] = std::move(link_out);
    out["warnings"] = ctx.warnings;
    out["hint"] = hint;
    out["next_actions"] = next_actions;
    return out;
}

}  // namespace

ojson solve(const ojson& doc, const ojson& boundary, const ojson& fluid,
            const ojson& pumps, const std::string& network_type, double tol_mm,
            int max_iter, double tol_head) {
    std::string nt = network_type;
    std::transform(nt.begin(), nt.end(), nt.begin(),
                   [](unsigned char c) { return std::tolower(c); });
    if (nt == "gas" || nt == "compressible") {
        return solve_gas(doc, boundary, fluid, tol_mm, max_iter);
    }
    const auto verrs = validate_piping(doc);
    if (!verrs.empty()) {
        ojson errs = ojson::array();
        for (const auto& e : verrs) errs.push_back(e);
        ojson out = jerr("piping document failed schema validation",
                         "run engine.piping.schema.validate(doc) and fix the errors");
        out["errors"] = std::move(errs);
        return out;
    }

    const ojson fl = resolve_solver_fluid(fluid);
    if (!fl.value("ok", true) && fl.contains("reason")) return fl;
    const double rho = fl["rho"].get<double>();
    const double mu = fl["mu"].get<double>();

    NetCtx ctx = build_network(doc, fl, tol_mm);
    if (!ctx.ok) return ctx.err;
    if (ojson err = apply_boundary(ctx, boundary, tol_mm, rho); !err.is_null()) return err;
    if (ojson err = attach_pumps(ctx, pumps); !err.is_null()) return err;

    std::vector<Node>& nodes = ctx.nodes;
    std::vector<Link>& links = ctx.links;
    std::vector<int> free_idx;
    for (const auto& nd : nodes)
        if (!nd.fixed) free_idx.push_back(nd.idx);
    std::map<int, int> pos;  // node idx -> reduced row
    for (int k = 0; k < static_cast<int>(free_idx.size()); ++k) pos[free_idx[k]] = k;
    const int nfree = static_cast<int>(free_idx.size());

    // initial heads: fixed at BC, free at mean fixed head
    double h0 = 0.0;
    int nfixed = 0;
    for (const auto& nd : nodes)
        if (nd.fixed) {
            h0 += nd.head;
            ++nfixed;
        }
    h0 /= nfixed;
    std::vector<double> H(nodes.size());
    for (const auto& nd : nodes) H[nd.idx] = nd.fixed ? nd.head : h0;

    // Newton–Raphson on the nodal mass-balance residual e(H) = inflow − outflow
    // − demand; the Jacobian is the (negated) weighted graph Laplacian with
    // edge weight p = dQ/dH. Friction refreshes each iteration; a backtracking
    // line search on ‖e‖∞ keeps the square-law solve globally convergent.
    auto residual = [&](const std::vector<double>& Hv, std::vector<double>& e) {
        std::fill(e.begin(), e.end(), 0.0);
        for (auto& lk : links) {
            const double q = link_flow(lk, Hv[lk.a] - Hv[lk.b]).first;
            lk.flow = q;
            auto ia = pos.find(lk.a), ib = pos.find(lk.b);
            if (ia != pos.end()) e[ia->second] -= q;
            if (ib != pos.end()) e[ib->second] += q;
        }
        for (const auto& nd : nodes) {
            auto it = pos.find(nd.idx);
            if (it != pos.end()) e[it->second] -= nd.demand;
        }
    };

    bool converged = nfree == 0;
    int it = 0;
    // Mass-balance tolerance (m³/s), scaled to the network's own flow scale —
    // never looser than tol_head, never tighter than 1e-12 (see the oracle).
    double q_scale = 0.0;
    for (const auto& nd : nodes) q_scale = std::max(q_scale, std::abs(nd.demand));
    const double tol_q =
        q_scale > 0 ? std::min(tol_head, std::max(1e-4 * q_scale, 1e-12)) : tol_head;

    if (nfree) {
        for (it = 1; it <= max_iter; ++it) {
            for (auto& lk : links)
                lk.fd = friction_factor_lk(lk, std::max(std::abs(lk.flow), kMinAbsQ), mu, rho);

            std::vector<std::vector<double>> J(nfree, std::vector<double>(nfree, 0.0));
            std::vector<double> e(nfree, 0.0);
            for (auto& lk : links) {
                auto [q, p] = link_flow(lk, H[lk.a] - H[lk.b]);
                lk.flow = q;
                const int a = lk.a, b = lk.b;
                auto ia = pos.find(a), ib = pos.find(b);
                if (ia != pos.end()) {
                    e[ia->second] -= q;
                    J[ia->second][ia->second] -= p;
                    if (ib != pos.end()) J[ia->second][ib->second] += p;
                }
                if (ib != pos.end()) {
                    e[ib->second] += q;
                    J[ib->second][ib->second] -= p;
                    if (ia != pos.end()) J[ib->second][ia->second] += p;
                }
            }
            for (const auto& nd : nodes) {
                auto itp = pos.find(nd.idx);
                if (itp != pos.end()) e[itp->second] -= nd.demand;
            }

            const double enorm = max_abs(e);
            if (enorm < tol_q) {
                converged = true;
                break;
            }
            std::vector<double> neg_e(e.begin(), e.end());
            for (double& v : neg_e) v = -v;
            std::vector<double> dH;
            if (!lu_solve(J, neg_e, dH)) {
                ojson out = jerr("singular hydraulic system (disconnected network?)",
                                 "check that every node connects to a fixed-head node");
                out["iterations"] = it;
                return out;
            }

            // backtracking line search on the mass-imbalance residual
            const std::vector<double> base = H;
            double factor = 1.0;
            std::vector<double> etmp(nfree, 0.0);
            for (int ls = 0; ls < 30; ++ls) {
                for (int idx : free_idx) H[idx] = base[idx] + factor * dH[pos[idx]];
                residual(H, etmp);
                if (max_abs(etmp) < enorm || factor < 1e-6) break;
                factor *= 0.5;
            }
            // convergence is decided solely by the residual at the top of the
            // loop — never by a small step (a stalled line search must not
            // masquerade as converged).
        }
    }

    const double max_imbalance = finalize_flows(nodes, links, H, mu, rho);
    return assemble_result(ctx, nodes, links, H, fl, converged, it, max_imbalance);
}

ojson pressure_drop_report(const ojson& doc, const ojson& result_in,
                           const ojson& boundary, const ojson& fluid,
                           const ojson& pumps, double tol_mm) {
    ojson result = result_in;
    if (result.is_null()) {
        if (boundary.is_null()) {
            return jerr(
                "pressure_drop_report needs a solved 'result' or a 'boundary' to solve",
                "pass boundary=[{'node':'n0','pressure':<kPa>}, ...]");
        }
        result = solve(doc, boundary, fluid, pumps, "liquid", tol_mm);
    }
    if (!result.value("ok", false)) return result;

    // dict(WATER); fl.update(fluid or {}) — NOT the sourced resolver.
    ojson fl = ojson::object();
    fl["name"] = "water";
    fl["rho"] = 998.2;
    fl["mu"] = 1.002e-3;
    if (py_truthy(fluid)) pyutil::update(fl, fluid);
    const double rho = fl["rho"].get<double>();
    const double mu = fl["mu"].get<double>();
    NetCtx ctx = build_network(doc, fl, tol_mm);
    if (!ctx.ok) return ctx.err;

    std::map<std::string, ojson> link_res;
    for (const auto& lk : result["links"]) link_res[lk["id"].get<std::string>()] = lk;

    ojson segments = ojson::array();
    double total_dp = 0.0;
    for (auto& lk : ctx.links) {
        auto it = link_res.find(lk.id);
        if (it == link_res.end()) continue;
        const ojson& lr = it->second;
        const double q = lr["flow_m3h"].get<double>() / 3600.0;
        const double v = lk.area ? q / lk.area : 0.0;
        lk.fd = friction_factor_lk(lk, std::max(std::abs(q), kMinAbsQ), mu, rho);
        // friction component (fluids K_from_f / dP_from_K)
        const double k_fric = lk.d_m ? lk.fd * lk.length_m / lk.d_m : 0.0;
        const double dp_fric = k_fric * 0.5 * rho * std::abs(v) * std::abs(v);
        ojson breakdown = ojson::array();
        {
            ojson b = ojson::object();
            b["item"] = "pipe friction";
            b["K"] = pyround(k_fric, 4);
            b["dp_kpa"] = pyround(dp_fric / 1000.0, 4);
            b["method"] = "Colebrook fd=" + fmt_f(lk.fd, 5) +
                          ", L/D=" + fmt_f(lk.length_m / lk.d_m, 1);
            breakdown.push_back(std::move(b));
        }
        double seg_dp = dp_fric;
        // per-fitting K at the segment's fd, then its dP
        for (const auto& fb : lk.kbreak) {
            const double dp_k = fb["K"].get<double>() * 0.5 * rho * std::abs(v) * std::abs(v);
            ojson b = ojson::object();
            b["item"] = fb["type"];
            b["fitting"] = fb["fitting"];
            b["K"] = fb["K"];
            b["dp_kpa"] = pyround(dp_k / 1000.0, 4);
            b["method"] = fb["method"];
            breakdown.push_back(std::move(b));
            seg_dp += dp_k;
        }
        total_dp += seg_dp;
        ojson seg = ojson::object();
        seg["id"] = lk.id;
        seg["from"] = lr["from"];
        seg["to"] = lr["to"];
        seg["length_mm"] = lr["length_mm"];
        seg["id_mm"] = lr["id_mm"];
        seg["flow_m3h"] = lr["flow_m3h"];
        seg["velocity_ms"] = lr["velocity_ms"];
        seg["k_friction"] = pyround(k_fric, 4);
        seg["k_fittings"] = pyround(lk.kfit, 4);
        seg["dp_kpa"] = pyround(seg_dp / 1000.0, 4);
        seg["breakdown"] = std::move(breakdown);
        segments.push_back(std::move(seg));
    }

    ojson fluid_out = ojson::object();
    fluid_out["name"] = fl.value("name", std::string("custom"));
    fluid_out["rho"] = fl["rho"];
    fluid_out["mu"] = fl["mu"];

    ojson out = ojson::object();
    out["ok"] = true;
    out["fluid"] = std::move(fluid_out);
    out["segments"] = std::move(segments);
    out["total_dp_kpa"] = pyround(total_dp / 1000.0, 4);
    out["converged"] = result.value("converged", ojson(nullptr));
    out["warnings"] = ctx.warnings;
    out["hint"] = result.value("converged", false)
                      ? ojson(nullptr)
                      : ojson("underlying solve did not converge — pressures/flows are "
                              "approximate");
    out["next_actions"] =
        ojson::array({"deliver the pressure-drop table or size a pump for total_dp_kpa"});
    return out;
}

// ── compressible (gas) network solve ──────────────────────────────────────────
// Isothermal-compressible sibling of the head solve: the nodal potential is
// φ = P_abs² (Pa²) — per link, ṁ²·(f·L/D + ΣK) = (A²·M/(R·T·Z))·(P_a² − P_b²)
// (Weymouth form; the 2·ln(P1/P2) momentum term is neglected, Z = 1).

namespace {

constexpr double kPhiJacFloorFrac = 1e-9;  // |Δφ| floor fraction in the Jacobian only

double gas_friction_factor(const Link& lk, double mdot_abs, double mu) {
    if (lk.area <= 0) return 0.02;
    const double Re = mdot_abs * lk.d_m / (lk.area * mu);
    if (Re < 2100) return std::max(64.0 / std::max(Re, 1e-6), 1e-4);
    return clamond(std::max(Re, 1.0), lk.eps / lk.d_m);
}

// Mass flow ṁ (kg/s, a→b positive) and conductance dṁ/dφ for Δφ = φ_a−φ_b.
std::pair<double, double> gas_link_flow(const Link& lk, double dphi, double coef_geom,
                                        double phi_floor) {
    const double ktot = lk.fd * lk.length_m / lk.d_m + lk.kfit;
    if (ktot <= 0 || coef_geom <= 0) return {0.0, 0.0};
    const double coef = coef_geom / ktot;  // ṁ² = coef·|Δφ|
    const double mdot = std::copysign(std::sqrt(coef * std::abs(dphi)), dphi);
    const double p = 0.5 * std::sqrt(coef / std::max(std::abs(dphi), phi_floor));
    return {mdot, p};
}

// Fixed absolute-pressure² nodes + mass demands; reuses Node.head for φ and
// Node.demand for the mass demand (kg/s leaving).
ojson apply_gas_boundary(NetCtx& ctx, const ojson& boundary, double tol, double rho_std) {
    const ojson bcs = boundary.is_array() ? boundary : boundary.value("nodes", ojson::array());
    int n_fixed = 0;
    for (const auto& bc : bcs) {
        const int idx = resolve_node(bc, ctx, tol);
        if (idx < 0) {
            return jerr("boundary reference " + py_repr(bc) + " did not resolve to a node",
                        "use {'node':'n0'} / {'at':[x,y,z]} / {'run':'r1','end':'start'}");
        }
        Node& nd = ctx.nodes[idx];
        if (bc.is_object() && bc.contains("pressure_abs")) {  // kPa absolute
            const double p_abs = pyutil::to_double(bc["pressure_abs"]) * 1000.0;
            nd.fixed = true;
            nd.head = p_abs * p_abs;
            ++n_fixed;
        } else if (bc.is_object() && bc.contains("pressure")) {  // kPa gauge -> absolute
            const double p_abs = pyutil::to_double(bc["pressure"]) * 1000.0 + P_ATM;
            nd.fixed = true;
            nd.head = p_abs * p_abs;
            ++n_fixed;
        } else if (bc.is_object() && bc.contains("mass_demand")) {  // kg/h leaving (+)
            nd.demand += pyutil::to_double(bc["mass_demand"]) / 3600.0;
        } else if (bc.is_object() &&
                   (bc.contains("std_demand") || bc.contains("demand"))) {  // Nm³/h
            const double q_std = pyutil::to_double(
                bc.contains("std_demand") ? bc["std_demand"] : bc["demand"]);
            nd.demand += q_std * rho_std / 3600.0;
        }
    }
    if (n_fixed == 0) {
        return jerr("gas network under-constrained: no fixed-pressure node",
                    "give at least one boundary with 'pressure' (kPa gauge) or "
                    "'pressure_abs'");
    }
    return nullptr;
}

ojson assemble_gas_result(const NetCtx& ctx, const std::vector<Node>& nodes,
                          const std::vector<Link>& links, const std::vector<double>& phi,
                          const ojson& fl, double M, double T, double mu, double rho_std,
                          bool converged, int it, double max_imbalance) {
    ojson node_out = ojson::array();
    for (const auto& nd : nodes) {
        const double p_abs = std::sqrt(std::max(phi[nd.idx], 0.0));
        ojson row = ojson::object();
        row["id"] = nd.id;
        row["xyz"] = ojson::array({nd.xyz[0], nd.xyz[1], nd.xyz[2]});
        row["fixed"] = nd.fixed;
        row["pressure_abs_kpa"] = pyround(p_abs / 1000.0, 3);
        row["pressure_kpa"] = pyround((p_abs - P_ATM) / 1000.0, 3);
        row["mass_demand_kgh"] = pyround(nd.demand * 3600.0, 5);
        node_out.push_back(std::move(row));
    }
    ojson link_out = ojson::array();
    for (const auto& lk : links) {
        const double m = lk.flow;
        const double p_a = std::sqrt(std::max(phi[lk.a], 0.0));
        const double p_b = std::sqrt(std::max(phi[lk.b], 0.0));
        const double p_mean = 0.5 * (p_a + p_b);
        const double rho_mean = p_mean * M / (R_UNIVERSAL * T);
        const double v = lk.area && rho_mean ? m / (lk.area * rho_mean) : 0.0;
        const double Re = lk.area ? std::abs(m) * lk.d_m / (lk.area * mu) : 0.0;
        ojson row = ojson::object();
        row["id"] = lk.id;
        row["from"] = nodes[lk.a].id;
        row["to"] = nodes[lk.b].id;
        row["length_mm"] = pyround(lk.length_m * 1000.0, 2);
        row["id_mm"] = pyround(lk.d_m * 1000.0, 3);
        row["mass_flow_kgh"] = pyround(m * 3600.0, 5);
        row["std_flow_nm3h"] = rho_std ? ojson(pyround(m / rho_std * 3600.0, 4)) : ojson(0.0);
        row["velocity_ms"] = pyround(v, 4);
        row["reynolds"] = pyround(Re, 1);
        row["friction_factor"] = pyround(lk.fd, 5);
        row["dp_kpa"] = pyround((p_a - p_b) / 1000.0, 4);
        row["k_total"] = pyround(lk.kfit, 4);
        link_out.push_back(std::move(row));
    }
    ojson next_actions =
        converged
            ? ojson::array({"deliver node pressures + mass/standard flows"})
            : ojson::array(
                  {"verify every run endpoint meets another run or a fixed-pressure "
                   "boundary node"});
    ojson fluid_out = ojson::object();
    fluid_out["name"] = fl.value("name", std::string("custom"));
    fluid_out["phase"] = "gas";
    fluid_out["molar_mass"] = M;
    fluid_out["T_K"] = pyround(T, 3);
    fluid_out["mu"] = mu;
    fluid_out["rho_std_kgm3"] = pyround(rho_std, 5);
    fluid_out["source"] = fl.value("source", ojson(nullptr));

    ojson out = ojson::object();
    out["ok"] = true;
    out["network_type"] = "gas";
    out["converged"] = converged;
    out["iterations"] = it;
    out["max_imbalance_kgh"] = pyround(max_imbalance * 3600.0, 8);
    out["fluid"] = std::move(fluid_out);
    out["assumptions"] = ojson::array(
        {"isothermal", "ideal gas (Z=1)",
         "Weymouth form (2·ln(P1/P2) momentum term neglected)"});
    out["nodes"] = std::move(node_out);
    out["links"] = std::move(link_out);
    out["warnings"] = ctx.warnings;
    out["hint"] = converged
                      ? ojson(nullptr)
                      : ojson("gas solve did not converge in " + std::to_string(it) +
                              " iterations (max node imbalance " +
                              fmt_g(max_imbalance * 3600.0, 3) + " kg/h)");
    out["next_actions"] = next_actions;
    return out;
}

}  // namespace

ojson solve_gas(const ojson& doc, const ojson& boundary, const ojson& fluid,
                double tol_mm, int max_iter, double tol_mass) {
    const auto verrs = validate_piping(doc);
    if (!verrs.empty()) {
        ojson errs = ojson::array();
        for (const auto& e : verrs) errs.push_back(e);
        ojson out = jerr("piping document failed schema validation",
                         "run engine.piping.schema.validate(doc) and fix the errors");
        out["errors"] = std::move(errs);
        return out;
    }

    const ojson fl = resolve_solver_fluid(fluid);
    if (!fl.value("ok", true) && fl.contains("reason")) return fl;
    if (fl.value("phase", std::string()) != "gas" ||
        !py_truthy(fl.value("molar_mass", ojson(nullptr)))) {
        return jerr("gas network solve needs a named compressible fluid with a molar mass",
                    "pass fluid={'name':'natural_gas','T':288.15,'P':<line Pa>} "
                    "(also air/methane/nitrogen/carbon_dioxide)");
    }
    const double M = fl["molar_mass"].get<double>();
    const double T =
        py_truthy(fl.value("T_K", ojson(nullptr))) ? fl["T_K"].get<double>() : STD_T;
    const double mu = fl["mu"].get<double>();
    const double rho_std = STD_P * M / (R_UNIVERSAL * STD_T);

    NetCtx ctx = build_network(doc, fl, tol_mm);
    if (!ctx.ok) return ctx.err;
    if (ojson err = apply_gas_boundary(ctx, boundary, tol_mm, rho_std); !err.is_null())
        return err;

    std::vector<Node>& nodes = ctx.nodes;
    std::vector<Link>& links = ctx.links;
    for (auto& lk : links) lk.flow = 1e-3;  // kg/s seed

    std::vector<int> free_idx;
    for (const auto& nd : nodes)
        if (!nd.fixed) free_idx.push_back(nd.idx);
    std::map<int, int> pos;
    for (int k = 0; k < static_cast<int>(free_idx.size()); ++k) pos[free_idx[k]] = k;
    const int nfree = static_cast<int>(free_idx.size());

    double phi0 = 0.0;
    int nfixed = 0;
    for (const auto& nd : nodes)
        if (nd.fixed) {
            phi0 += nd.head;
            ++nfixed;
        }
    phi0 /= nfixed;
    std::vector<double> phi(nodes.size());
    for (const auto& nd : nodes) phi[nd.idx] = nd.fixed ? nd.head : phi0;
    const double phi_floor = std::max(kPhiJacFloorFrac * phi0, 1.0);

    std::map<std::string, double> coef;
    for (const auto& lk : links)
        coef[lk.id] = lk.area * lk.area * M / (R_UNIVERSAL * T);

    auto residual = [&](const std::vector<double>& phiv, std::vector<double>& e) {
        std::fill(e.begin(), e.end(), 0.0);
        for (auto& lk : links) {
            const double m =
                gas_link_flow(lk, phiv[lk.a] - phiv[lk.b], coef[lk.id], phi_floor).first;
            lk.flow = m;
            auto ia = pos.find(lk.a), ib = pos.find(lk.b);
            if (ia != pos.end()) e[ia->second] -= m;
            if (ib != pos.end()) e[ib->second] += m;
        }
        for (const auto& nd : nodes) {
            auto it = pos.find(nd.idx);
            if (it != pos.end()) e[it->second] -= nd.demand;
        }
    };

    bool converged = nfree == 0;
    int it = 0;
    if (nfree) {
        for (it = 1; it <= max_iter; ++it) {
            for (auto& lk : links)
                lk.fd = gas_friction_factor(lk, std::max(std::abs(lk.flow), 1e-9), mu);

            std::vector<std::vector<double>> J(nfree, std::vector<double>(nfree, 0.0));
            std::vector<double> e(nfree, 0.0);
            for (auto& lk : links) {
                auto [m, p] =
                    gas_link_flow(lk, phi[lk.a] - phi[lk.b], coef[lk.id], phi_floor);
                lk.flow = m;
                const int a = lk.a, b = lk.b;
                auto ia = pos.find(a), ib = pos.find(b);
                if (ia != pos.end()) {
                    e[ia->second] -= m;
                    J[ia->second][ia->second] -= p;
                    if (ib != pos.end()) J[ia->second][ib->second] += p;
                }
                if (ib != pos.end()) {
                    e[ib->second] += m;
                    J[ib->second][ib->second] -= p;
                    if (ia != pos.end()) J[ib->second][ia->second] += p;
                }
            }
            for (const auto& nd : nodes) {
                auto itp = pos.find(nd.idx);
                if (itp != pos.end()) e[itp->second] -= nd.demand;
            }

            const double enorm = max_abs(e);
            if (enorm < tol_mass) {
                converged = true;
                break;
            }
            std::vector<double> neg_e(e.begin(), e.end());
            for (double& v : neg_e) v = -v;
            std::vector<double> dphi;
            if (!lu_solve(J, neg_e, dphi)) {
                ojson out = jerr("singular gas system (disconnected network?)",
                                 "check that every node connects to a fixed-pressure node");
                out["iterations"] = it;
                return out;
            }

            const std::vector<double> base = phi;
            double factor = 1.0;
            std::vector<double> etmp(nfree, 0.0);
            for (int ls = 0; ls < 40; ++ls) {
                for (int idx : free_idx) {
                    const double cand = base[idx] + factor * dphi[pos[idx]];
                    phi[idx] = std::max(cand, 1.0);  // keep P² > 0 (physical)
                }
                residual(phi, etmp);
                if (max_abs(etmp) < enorm || factor < 1e-6) break;
                factor *= 0.5;
            }
        }
    }

    // settle mass flows against Reynolds-dependent friction at the converged
    // potentials
    for (int i = 0; i < 60; ++i) {
        double dmax = 0.0;
        for (auto& lk : links) {
            lk.fd = gas_friction_factor(lk, std::max(std::abs(lk.flow), 1e-9), mu);
            const double m =
                gas_link_flow(lk, phi[lk.a] - phi[lk.b], coef[lk.id], phi_floor).first;
            dmax = std::max(dmax, std::abs(m - lk.flow));
            lk.flow = m;
        }
        if (dmax < 1e-12) break;
    }
    std::map<int, double> imbalance;
    for (const auto& nd : nodes)
        imbalance[nd.idx] = nd.fixed ? 0.0 : -nd.demand;
    for (const auto& lk : links) {
        imbalance[lk.b] += lk.flow;
        imbalance[lk.a] -= lk.flow;
    }
    double max_imbalance = 0.0;
    for (const auto& nd : nodes)
        if (!nd.fixed) max_imbalance = std::max(max_imbalance, std::abs(imbalance[nd.idx]));

    return assemble_gas_result(ctx, nodes, links, phi, fl, M, T, mu, rho_std, converged,
                               it, max_imbalance);
}

// ── pump/system operating point + NPSH margin ─────────────────────────────────

namespace {

double system_head(const ojson& system, double q_m3h) {
    const double h0 = system.value("static_head_m", ojson(0.0)).is_number()
                          ? system["static_head_m"].get<double>()
                          : 0.0;
    const double k =
        system.value("k", ojson(0.0)).is_number() ? system["k"].get<double>() : 0.0;
    return h0 + k * q_m3h * q_m3h;
}

// Piecewise-linear interpolation of [[x,y],...] (sorted by x); NaN outside.
constexpr double kNoInterp = std::numeric_limits<double>::quiet_NaN();
double interp_curve(const ojson& pts, double x) {
    if (!pts.is_array() || pts.empty()) return kNoInterp;
    const auto x_at = [&](size_t i) { return pts[i][0].get<double>(); };
    const auto y_at = [&](size_t i) { return pts[i][1].get<double>(); };
    if (x < x_at(0) || x > x_at(pts.size() - 1)) return kNoInterp;
    for (size_t i = 0; i + 1 < pts.size(); ++i) {
        const double x0 = x_at(i), y0 = y_at(i);
        const double x1 = x_at(i + 1), y1 = y_at(i + 1);
        if (x0 <= x && x <= x1 && x1 > x0) return y0 + (x - x0) * (y1 - y0) / (x1 - x0);
    }
    return y_at(pts.size() - 1);
}

// Fit static + k·Q² from a network solve at a reference demand. Returns
// (system, derived_meta) or (null, contract-shaped err).
std::pair<ojson, ojson> derive_system_curve(const ojson& doc, const ojson& boundary,
                                            const ojson& fluid, const ojson& q_ref_m3h) {
    const double q_ref = py_truthy(q_ref_m3h) ? pyutil::to_double(q_ref_m3h) : 50.0;
    ojson demand_bc = nullptr, fixed_bc = nullptr;
    const ojson bcs = boundary.is_array() ? boundary : boundary.value("nodes", ojson::array());
    ojson probe = ojson::array();
    for (const auto& bc : bcs) {
        ojson b = bc;
        if (bc.is_object() && (bc.contains("demand") || bc.contains("demand_lps"))) {
            b = ojson::object();
            for (auto it = bc.begin(); it != bc.end(); ++it)
                if (it.key() != "demand" && it.key() != "demand_lps") b[it.key()] = it.value();
            b["demand"] = q_ref;
            demand_bc = b;
        } else if (bc.is_object() && (bc.contains("pressure") || bc.contains("head"))) {
            fixed_bc = bc;
        }
        probe.push_back(b);
    }
    if (demand_bc.is_null() || fixed_bc.is_null()) {
        return {nullptr,
                jerr("cannot derive a system curve",
                     "boundary needs one fixed pressure/head node and one demand node "
                     "(the operating flow), or pass system={'static_head_m','k'}")};
    }
    const ojson res = solve(doc, probe, fluid);
    if (!res.value("ok", false)) return {nullptr, res};
    double total_loss_m = 0.0;
    for (const auto& l : res["links"]) total_loss_m += l["headloss_m"].get<double>();
    const double k = q_ref ? total_loss_m / (q_ref * q_ref) : 0.0;
    double fixed_head = 0.0;
    for (const auto& n : res["nodes"])
        if (n.value("fixed", false)) {
            fixed_head = n["head_m"].get<double>();
            break;
        }
    double z_demand = 0.0;
    for (const auto& n : res["nodes"]) {
        if (std::abs(n["demand_m3h"].get<double>() - q_ref) < 1e-3) {
            z_demand = n["xyz"][2].get<double>() / 1000.0;
            break;
        }
    }
    const double static_head = fixed_head - z_demand;
    ojson system = ojson::object();
    system["static_head_m"] = static_head;
    system["k"] = k;
    ojson meta = ojson::object();
    meta["q_ref_m3h"] = q_ref;
    meta["total_loss_m"] = pyround(total_loss_m, 4);
    meta["static_head_m"] = pyround(static_head, 4);
    meta["k"] = k;
    return {system, meta};
}

ojson npsh_margin(double q_m3h, const ojson& suction, const ojson& fluid,
                  const ojson& npshr_curve) {
    auto nulls = [] {
        ojson out = ojson::object();
        out["npsha_m"] = nullptr;
        out["npshr_m"] = nullptr;
        out["margin_m"] = nullptr;
        return out;
    };
    if (suction.is_null()) {
        ojson out = nulls();
        out["hint"] = "pass suction={'pressure_kpa','elevation_m','friction_m'} for NPSHa";
        return out;
    }
    const ojson fl = resolve_solver_fluid(fluid);
    if (!fl.value("ok", true) && fl.contains("reason")) {
        ojson out = nulls();
        out["reason"] = fl["reason"];
        return out;
    }
    const double rho = fl["rho"].get<double>();
    const double p_atm =
        (suction.value("atm_kpa", ojson(P_ATM / 1000.0))).get<double>() * 1000.0;
    const double p_suct_g = suction.value("pressure_kpa", 0.0) * 1000.0;
    double p_vap;
    ojson pvap_src;
    if (suction.contains("pvap_kpa")) {
        p_vap = suction["pvap_kpa"].get<double>() * 1000.0;
        pvap_src = "caller-supplied";
    } else {
        if (fl.value("pvap", ojson(nullptr)).is_null()) {
            ojson out = nulls();
            out["hint"] =
                "no vapour pressure for this fluid — pass suction={'pvap_kpa':...}";
            return out;
        }
        p_vap = fl["pvap"].get<double>();
        pvap_src = fl.value("source", ojson(nullptr));
    }
    const double z = suction.value("elevation_m", 0.0);
    const double h_fric = suction.value("friction_m", 0.0);
    const double npsha = (p_atm + p_suct_g - p_vap) / (rho * kG) + z - h_fric;

    double npshr = kNoInterp;
    if (!npshr_curve.is_null()) {
        const ojson curve = !npshr_curve.is_object()
                                ? load_pump_curve_json(npshr_curve, 1.0, "flow", "npshr")
                                : npshr_curve;
        if (curve.value("ok", false)) npshr = interp_curve(curve["points"], q_m3h);
    }
    const bool have_r = !std::isnan(npshr);
    ojson out = ojson::object();
    out["npsha_m"] = pyround(npsha, 4);
    out["npshr_m"] = have_r ? ojson(pyround(npshr, 4)) : ojson(nullptr);
    out["margin_m"] = have_r ? ojson(pyround(npsha - npshr, 4)) : ojson(nullptr);
    out["rho_kgm3"] = rho;
    out["pvap_pa"] = pyround(p_vap, 3);
    out["pvap_source"] = pvap_src;
    if (!have_r) out["hint"] = "pass npshr_curve (flow,npshr CSV) to get the NPSH margin";
    return out;
}

}  // namespace

ojson operating_point(const ojson& pump_curve, const ojson& system_in, const ojson& doc,
                      const ojson& boundary, const ojson& fluid, const ojson& q_ref_m3h,
                      const ojson& npshr_curve, const ojson& suction, double speed_ratio) {
    ojson pump = nullptr;
    if (pump_curve.is_object() && pump_curve.value("ok", false)) {
        pump = pump_curve;
    } else {
        pump = load_pump_curve_json(pump_curve, speed_ratio, "flow", "head");
    }
    if (!pump.value("ok", false)) return pump;

    ojson system = system_in;
    ojson derived = nullptr;
    if (system.is_null()) {
        if (doc.is_null() || boundary.is_null()) {
            return jerr(
                "operating_point needs a 'system' curve or a doc+boundary to derive one",
                "pass system={'static_head_m':h0,'k':k} or doc=<piping doc>, "
                "boundary=[...]");
        }
        auto [sys, meta] = derive_system_curve(doc, boundary, fluid, q_ref_m3h);
        if (sys.is_null()) return meta;  // contract-shaped failure
        system = sys;
        derived = meta;
    }

    // Intersect: g(Q) = H_pump(Q) − H_sys(Q). Bisect over the pump's flow range.
    const ojson& pts = pump["points"];
    const double q_lo0 = pts[0][0].get<double>(), q_hi0 = pts[pts.size() - 1][0].get<double>();
    auto g = [&](double q) { return pump_head(pump, q / 3600.0) - system_head(system, q); };
    double q_lo = q_lo0, q_hi = q_hi0;
    const double g_lo = g(q_lo), g_hi = g(q_hi);
    if (g_lo < 0) {
        ojson out = jerr(
            "no operating point: system head exceeds pump shutoff head at zero flow",
            "the pump cannot meet the static lift — raise pump speed or lower static head");
        out["shutoff_head_m"] = pump["shutoff_head"];
        out["static_head_m"] = system.value("static_head_m", ojson(nullptr));
        return out;
    }
    if (g_hi > 0) {
        ojson out = jerr(
            "no operating point within the pump curve: pump head still exceeds system "
            "head at max curve flow",
            "extend the pump curve to higher flow, or the system is over-pumped");
        out["max_curve_flow_m3h"] = q_hi;
        return out;
    }
    for (int i = 0; i < 200; ++i) {
        const double q_mid = 0.5 * (q_lo + q_hi);
        if (g(q_mid) > 0)
            q_lo = q_mid;
        else
            q_hi = q_mid;
        if (q_hi - q_lo < 1e-9) break;
    }
    const double q_star = 0.5 * (q_lo + q_hi);
    const double h_star = system_head(system, q_star);

    ojson duty = ojson::object();
    duty["flow_m3h"] = pyround(q_star, 4);
    duty["head_m"] = pyround(h_star, 4);
    duty["flow_m3s"] = pyround(q_star / 3600.0, 8);
    ojson pump_out = ojson::object();
    pump_out["shutoff_head_m"] = pump["shutoff_head"];
    pump_out["max_flow_m3h"] = pump["max_flow"];
    pump_out["speed_ratio"] = pump.value("speed_ratio", speed_ratio);
    ojson sys_out = ojson::object();
    sys_out["static_head_m"] = system.value("static_head_m", 0.0);
    sys_out["k"] = system.value("k", 0.0);

    ojson out = ojson::object();
    out["ok"] = true;
    out["duty_point"] = std::move(duty);
    out["pump"] = std::move(pump_out);
    out["system_curve"] = std::move(sys_out);
    out["warnings"] = ojson::array();
    out["next_actions"] = ojson::array({"confirm the duty point sits near the pump BEP"});
    if (!derived.is_null()) out["system_curve"]["derived_from"] = derived;

    const ojson npsh = npsh_margin(q_star, suction, fluid, npshr_curve);
    out["npsh"] = npsh;
    if (!npsh.value("margin_m", ojson(nullptr)).is_null() &&
        npsh["margin_m"].get<double>() < 0) {
        out["warnings"].push_back("NPSH margin negative — cavitation risk at the duty point");
        ojson na = ojson::array({"raise suction pressure / lower NPSHr — cavitation risk"});
        for (const auto& a : out["next_actions"]) na.push_back(a);
        out["next_actions"] = std::move(na);
    }
    return out;
}

}  // namespace chudware::piping
