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
#include <functional>
#include <map>
#include <optional>
#include <sstream>

#include "doc/native_policy.h"
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
    std::vector<ojson> fittings;
    ojson pump = nullptr;  // optional pump dict (head-vs-flow)
    double flow = 1e-4;    // m³/s (liquid) / kg/s (gas) current iterate
    double fd = 0.02;
    double mu = 0.0;   // fluid viscosity (0 = unknown → laminar closed form off)
    double rho = 0.0;
    bool mass_flow = false;
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
constexpr int kTeeAuto = -1, kTeeRun = 0, kTeeBranch = 1;  // fitting_K tee_path

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

// Evaluate one k_model override object; nullopt for an unknown kind (validated upstream).
// `key` names the params entry it came from: "k_model" keeps the historical method
// strings, the tee path keys (k_model_run / k_model_branch) label their method.
std::optional<std::pair<double, std::string>> k_model_K(const ojson& km, double d_in, double Re,
                                                         const std::string& key) {
    const std::string kind = km.value("kind", "");
    const std::string tag = key == "k_model" ? "" : " [" + key + "]";
    if (kind == "constant") return std::pair{km["K"].get<double>(), key + "(constant)"};
    if (kind == "two_k")
        return std::pair{km["K1"].get<double>() / std::max(Re, 1e-6) +
                             km["Kinf"].get<double>() * (1 + 1 / d_in),
                         "Hooper(two_k)" + tag};
    if (kind == "three_k")
        return std::pair{km["K1"].get<double>() / std::max(Re, 1e-6) +
                             km["Ki"].get<double>() * (1 + km["Kd"].get<double>() / std::pow(d_in, 0.3)),
                         "Darby(three_k)" + tag};
    if (kind == "cv")
        return std::pair{890.3 * std::pow(d_in, 4) / std::pow(km["Cv"].get<double>(), 2),
                         "Cv(US)" + tag};
    if (kind == "table") {
        const auto& points = km["points"];
        double K = points.front()["K"].get<double>();
        for (size_t i = 1; i < points.size(); ++i) {
            const double lo = points[i-1]["re"].get<double>();
            const double hi = points[i]["re"].get<double>();
            if (Re <= lo) break;
            const double t = std::clamp(std::log(Re / lo) / std::log(hi / lo), 0.0, 1.0);
            K = points[i-1]["K"].get<double>() +
                t * (points[i]["K"].get<double>() - points[i-1]["K"].get<double>());
            if (Re <= hi) break;
        }
        return std::pair{K, "table(log-Re," + km.value("source", "user") + ")" + tag};
    }
    return std::nullopt;
}

// K-factor for a fitting on a pipe of inner-dia d_m (m) at friction fd.
// Tees: `tee_path` forces the straight-through (kTeeRun) or branch (kTeeBranch) path;
// kTeeAuto derives it from attribution (branch_run == run_id) or, for legacy
// unattributed tees, from a reducing branch_nps. A tee's params.k_model_run /
// params.k_model_branch override that path; params.k_model overrides both.
std::pair<double, std::string> fitting_K(const ojson& ft, double d_m, double fd,
                                         double Re, const std::string& run_id = "",
                                         int tee_path = kTeeAuto) {
    const std::string ftype = ft.value("type", std::string());
    const ojson params = ft.value("params", ojson::object());
    const auto getp = [&](const char* k) -> ojson {
        return params.is_object() ? params.value(k, ojson(nullptr)) : ojson(nullptr);
    };

    const double d_in = d_m / 0.0254;
    const double fT = 1.0 / std::pow(-2.0 * std::log10(0.0000457 / (3.7 * d_m)), 2);
    const auto km = getp("k_model");
    if (ftype == "tee") {
        const bool attributed = ft.value("run_id", ojson(nullptr)).is_string() &&
                                getp("branch_run").is_string() && getp("run_ids").is_array();
        bool branch = tee_path == kTeeBranch;
        if (tee_path == kTeeAuto) {
            if (attributed) {
                branch = params["branch_run"] == run_id;
            } else {
                const ojson branch_nps = getp("branch_nps");
                const ojson nps = ft.value("nps", ojson(nullptr));
                const double nps_v = py_truthy(nps) ? pyutil::to_double(nps) : 0.0;
                branch = !branch_nps.is_null() && pyutil::to_double(branch_nps) < nps_v;
            }
        }
        const char* path_key = branch ? "k_model_branch" : "k_model_run";
        const auto path_model = getp(path_key);
        if (path_model.is_object())
            if (auto k = k_model_K(path_model, d_in, Re, path_key)) return *k;
        if (km.is_object())
            if (auto k = k_model_K(km, d_in, Re, "k_model")) return *k;
        if (attributed || tee_path != kTeeAuto)
            return {(branch ? 60 : 20) * fT,
                    branch ? "Crane TP-410(tee_branch)" : "Crane TP-410(tee_run)"};
        return branch ? std::pair{kKTeeBranch, std::string("default(tee_branch)")}
                      : std::pair{kKTeeRun, std::string("default(tee_run)")};
    }
    if (km.is_object())
        if (auto k = k_model_K(km, d_in, Re, "k_model")) return *k;
    if (ftype == "valve") {
        const std::string type = params.value("valve_type", "gate");
        double factor = 8;
        if (type == "globe") factor = 340;
        else if (type == "angle") factor = 150;
        else if (type == "ball") factor = 3;
        else if (type == "plug") factor = 18;
        else if (type == "butterfly") {
            const double nps = ft.value("nps", 2.0);
            factor = nps <= 8 ? 45 : nps < 16 ? 35 : 25;
        } else if (type == "check_swing") factor = 100;
        else if (type == "check_lift") factor = 600;
        return {factor * fT, "Crane TP-410(" + type + ")"};
    }
    if (ftype == "orifice") {
        const auto beta_value = getp("beta");
        const double beta = beta_value.is_number() ? beta_value.get<double>() :
                            params["bore_mm"].get<double>() / (d_m * 1000);
        constexpr double C = 0.61;
        const double ratio = std::sqrt(1 - std::pow(beta, 4) * (1 - C*C)) / (C * beta * beta);
        return {std::pow(ratio - 1, 2), "sharp-edged orifice (Rennels, C=0.61)"};
    }
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
        const auto radius_mm = getp("radius_mm");
        const double bd = radius_mm.is_number() ? radius_mm.get<double>() / (d_m * 1000) :
                                                up == "LR" ? 1.5 : 1.0;
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
    if (ftype == "flange") return {kKFlange, "default(flange)"};
    if (ftype == "elbow") return {0.3, "default(elbow)"};  // fluids-absent fallback
    return {0.0, "default(none)"};
}
void refresh_link_fittings(Link& lk) {
    lk.kfit = 0.0;
    lk.kbreak.clear();
    const double Re = lk.mu > 0 && lk.area > 0 ?
        std::abs(lk.flow) * lk.d_m * (lk.mass_flow ? 1.0 : lk.rho) / (lk.area * lk.mu) : 1e5;
    for (const auto& ft : lk.fittings) {
        auto [K, method] = fitting_K(ft, lk.d_m, lk.fd, Re, lk.id);
        lk.kfit += K;
        ojson kb = ojson::object();
        kb["fitting"] = ft.value("id", ojson(nullptr));
        kb["type"] = ft.value("type", ojson(nullptr));
        kb["K"] = pyround(K, 4);
        kb["method"] = method;
        lk.kbreak.push_back(std::move(kb));
    }
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
        if (!std::isfinite(lk.length_m)) {
            ctx.err = jerr(lk.id + ": run length calculation overflowed",
                           "use finite coordinates with physically meaningful magnitudes");
            return ctx;
        }
        const ojson bore_override = run.value("bore_mm", ojson(nullptr));
        if (py_truthy(bore_override)) {
            // Explicit inner diameter (mm) — externally-supplied networks (e.g.
            // the hydraulic→piping handoff) bypass the schedule lookup.
            lk.d_m = pyutil::to_double(bore_override) / 1000.0;
        } else {
            const ojson spec = nearest_pipe(run.value("nps", ojson(nullptr)),
                                            run.value("schedule", std::string("40")));
            if (!spec.value("ok", false)) {
                ctx.err = jerr(
                    lk.id + ": pipe size/schedule has no catalog dimensions",
                    spec.value("reason", std::string()) +
                        "; supply a valid catalog NPS/schedule or explicit bore_mm");
                return ctx;
            } else {
                lk.d_m = spec["inner_dia"].get<double>() / 1000.0;
            }
        }
        lk.area = M_PI * lk.d_m * lk.d_m / 4.0;
        if (!std::isfinite(lk.d_m) || lk.d_m <= 0 ||
            !std::isfinite(lk.area) || lk.area <= 0) {
            ctx.err = jerr(lk.id + ": pipe bore/area calculation is invalid",
                           "use a finite positive bore_mm or supported catalog size");
            return ctx;
        }
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
        lk.mass_flow = fluid.value("phase", std::string()) == "gas";
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
        const auto owner = ft.value("run_id", ojson(nullptr));
        const auto params = ft.value("params", ojson::object());
        const bool attributed_tee = owner.is_string() && ft.value("type", "") == "tee" &&
            params.value("run_ids", ojson(nullptr)).is_array() &&
            params.value("branch_run", ojson(nullptr)).is_string();
        for (size_t i = 0; i < ctx.links.size() && i < nonzero_runs.size(); ++i) {
            const auto& id = ctx.links[i].id;
            const bool owns = attributed_tee ?
                (params["branch_run"] == id || (params["run_ids"].size() == 2 &&
                                               params["run_ids"][1] == id)) :
                owner.is_string() ? owner == id :
                point_on_polyline(origin, (*nonzero_runs[i])["path"], tol);
            if (owns) {
                ctx.links[i].fittings.push_back(ft);
                placed = true;
                if (!attributed_tee) break;
            }
        }
        if (!placed) {
            ctx.warnings.push_back("fitting " + pyutil::py_str(ft.value("id", ojson(nullptr))) +
                                   " at " + py_repr(ft.value("origin", ojson(nullptr))) +
                                   " not on any run — K ignored");
        }
    }
    for (auto& lk : ctx.links) refresh_link_fittings(lk);

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

bool finite_number(const ojson& value) {
    return value.is_number() && !value.is_boolean() &&
           std::isfinite(value.get<double>());
}

ojson boundary_list(const ojson& boundary) {
    if (boundary.is_array()) return boundary;
    if (boundary.is_object() && boundary.value("nodes", ojson(nullptr)).is_array())
        return boundary["nodes"];
    return nullptr;
}

ojson validate_boundary_ref(const ojson& bc) {
    if (!bc.is_object()) return jerr("each boundary condition must be an object");
    const int refs = static_cast<int>(bc.contains("node")) +
                     static_cast<int>(bc.contains("at")) +
                     static_cast<int>(bc.contains("run"));
    if (refs != 1) {
        return jerr("each boundary condition must use exactly one node reference",
                    "use one of 'node', 'at', or 'run'+'end'");
    }
    if (bc.contains("node") &&
        (!bc["node"].is_string() || bc["node"].get<std::string>().empty()))
        return jerr("boundary node must be a non-empty string");
    if (bc.contains("at")) {
        const ojson at = bc["at"];
        if (!at.is_array() || at.size() != 3)
            return jerr("boundary at must be a finite [x,y,z] point in mm");
        for (const auto& coordinate : at)
            if (!finite_number(coordinate))
                return jerr("boundary at must be a finite [x,y,z] point in mm");
    }
    if (bc.contains("run")) {
        if (!bc["run"].is_string() || bc["run"].get<std::string>().empty())
            return jerr("boundary run must be a non-empty string");
        const std::string end = bc.value("end", std::string("start"));
        if (end != "start" && end != "end")
            return jerr("boundary run end must be 'start' or 'end'");
    }
    return nullptr;
}

ojson apply_boundary(NetCtx& ctx, const ojson& boundary, double tol, double rho) {
    const ojson bcs = boundary_list(boundary);
    if (!bcs.is_array() || bcs.empty())
        return jerr("boundary conditions must be a non-empty list");
    int n_fixed = 0;
    std::map<int, std::string> assigned;
    for (const auto& bc : bcs) {
        if (ojson err = validate_boundary_ref(bc); !err.is_null()) return err;
        const int conditions = static_cast<int>(bc.contains("head")) +
                               static_cast<int>(bc.contains("pressure")) +
                               static_cast<int>(bc.contains("demand")) +
                               static_cast<int>(bc.contains("demand_lps"));
        if (conditions != 1) {
            return jerr(
                "each liquid boundary condition must specify exactly one value",
                "use one of head, pressure, demand, or demand_lps");
        }
        const char* key = bc.contains("head")       ? "head"
                          : bc.contains("pressure") ? "pressure"
                          : bc.contains("demand")   ? "demand"
                                                   : "demand_lps";
        if (!finite_number(bc[key]))
            return jerr(std::string("boundary ") + key + " must be finite");
        if (std::string(key) == "pressure" &&
            bc[key].get<double>() <= -P_ATM / 1000.0) {
            return jerr("boundary gauge pressure implies non-positive absolute pressure");
        }

        const int idx = resolve_node(bc, ctx, tol);
        if (idx < 0) {
            return jerr("boundary reference " + py_repr(bc) + " did not resolve to a node",
                        "use {'node':'n0'} / {'at':[x,y,z]} / {'run':'r1','end':'start'}");
        }
        Node& nd = ctx.nodes[idx];
        const bool fixed = std::string(key) == "head" || std::string(key) == "pressure";
        auto prior = assigned.find(idx);
        if (prior != assigned.end() &&
            (fixed || prior->second == "fixed")) {
            return jerr("node " + nd.id +
                        " has conflicting or duplicate fixed/demand boundary conditions");
        }
        if (fixed) {
            assigned[idx] = "fixed";
            const double z = nd.xyz[2] / 1000.0;
            nd.fixed = true;
            nd.head = std::string(key) == "head"
                          ? bc[key].get<double>()
                          : z + bc[key].get<double>() * 1000.0 / (rho * kG);
            const double pressure_pa = rho * kG * (nd.head - z);
            if (!std::isfinite(nd.head) || !std::isfinite(pressure_pa)) {
                return jerr("liquid boundary pressure/head conversion overflowed");
            }
            ++n_fixed;
        } else {
            assigned[idx] = "demand";
            nd.demand += std::string(key) == "demand"
                             ? bc[key].get<double>() / 3600.0
                             : bc[key].get<double>() / 1000.0;
        }
    }
    if (n_fixed == 0) {
        return jerr("network under-constrained: no fixed-head/pressure node",
                    "give at least one boundary with 'pressure' (kPa) or 'head' (m)");
    }
    return nullptr;
}

ojson validate_anchored_components(const NetCtx& ctx,
                                   const std::string& pressure_kind) {
    std::vector<std::vector<int>> adjacent(ctx.nodes.size());
    for (const auto& lk : ctx.links) {
        adjacent[lk.a].push_back(lk.b);
        adjacent[lk.b].push_back(lk.a);
    }
    std::vector<bool> seen(ctx.nodes.size(), false);
    for (const auto& start : ctx.nodes) {
        if (seen[start.idx]) continue;
        std::vector<int> stack{start.idx};
        seen[start.idx] = true;
        bool anchored = false;
        for (size_t cursor = 0; cursor < stack.size(); ++cursor) {
            const int node = stack[cursor];
            anchored = anchored || ctx.nodes[node].fixed;
            for (int next : adjacent[node]) {
                if (!seen[next]) {
                    seen[next] = true;
                    stack.push_back(next);
                }
            }
        }
        if (!anchored) {
            return jerr("disconnected network component at " + start.id +
                            " has no fixed-" + pressure_kind + " boundary",
                        "each connected component must contain a fixed pressure/head node");
        }
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
    if (!std::isfinite(speed_ratio) || speed_ratio <= 0) {
        return jerr("pump curve speed_ratio must be finite and > 0");
    }
    // Inline CSV must be classified before any path lookup, including in cells.
    std::string text;
    if (csv_source.find_first_of(",\r\n") == std::string::npos) {
        try {
            const std::string path = doc::native_policy::resolve_reference(csv_source);
            std::ifstream fh(path);
            if (fh.good()) {
                std::ostringstream ss;
                ss << fh.rdbuf();
                text = ss.str();
            } else {
                text = csv_source;
            }
        } catch (const std::exception& exc) {
            return jerr("cannot read pump curve: " + std::string(exc.what()));
        }
    } else {
        text = csv_source;
    }

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
    std::map<std::string, std::string> fmap;
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
        if (trim(lines[li]).empty()) continue;
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
        double q = 0.0, h = 0.0;
        if (!have_q || !have_h || !py_float_parse(qstr, q) ||
            !py_float_parse(hstr, h) || !std::isfinite(q) ||
            !std::isfinite(h)) {
            return jerr("pump curve row " + std::to_string(li + 1) +
                            " has missing, non-numeric, or non-finite data",
                        "every non-empty row must provide finite flow/head values");
        }
        if (q < 0 || h < 0) {
            return jerr("pump curve flow/head values must be >= 0");
        }
        const double scaled_q = q * r;
        const double scaled_h = h * r * r;
        if (!std::isfinite(scaled_q) || !std::isfinite(scaled_h))
            return jerr("pump curve affinity scaling overflowed");
        pts.push_back({scaled_q, scaled_h});
    }
    if (pts.size() < 2) {
        return jerr("pump curve needs >= 2 valid points", "check numeric flow/head cells");
    }
    std::stable_sort(pts.begin(), pts.end(),
                     [](const auto& a, const auto& b) { return a[0] < b[0]; });
    const bool npshr = lower_str(head_col).find("npshr") != std::string::npos;
    for (size_t i = 1; i < pts.size(); ++i) {
        if (!(pts[i][0] > pts[i - 1][0])) {
            return jerr("pump curve flow values must be strictly increasing and unique");
        }
        if ((!npshr && pts[i][1] > pts[i - 1][1]) ||
            (npshr && pts[i][1] < pts[i - 1][1])) {
            return jerr(npshr
                            ? "NPSHr curve must be non-decreasing with flow"
                            : "pump head curve must be non-increasing with flow");
        }
    }
    ojson points = ojson::array();
    for (const auto& p : pts) points.push_back(ojson::array({p[0], p[1]}));
    ojson out = ojson::object();
    out["ok"] = true;
    out["points"] = std::move(points);
    out["shutoff_head"] = pts[0][1];
    out["max_flow"] = pts.back()[0];
    out["speed_ratio"] = r;
    return out;
}

namespace {
ojson validate_curve_object(const ojson& curve, bool npshr = false) {
    const ojson pts = curve.value("points", ojson(nullptr));
    if (!curve.is_object() || !curve.value("ok", false) ||
        !pts.is_array() || pts.size() < 2) {
        return jerr("pump curve object must contain >= 2 points");
    }
    double prior_q = -1.0;
    double prior_y = 0.0;
    for (size_t i = 0; i < pts.size(); ++i) {
        const ojson point = pts[i];
        if (!point.is_array() || point.size() != 2 ||
            !finite_number(point[0]) || !finite_number(point[1]) ||
            point[0].get<double>() < 0 || point[1].get<double>() < 0) {
            return jerr("pump curve points must be finite non-negative [flow, value] pairs");
        }
        const double q = point[0].get<double>();
        const double y = point[1].get<double>();
        if (i > 0 && (!(q > prior_q) ||
                      (!npshr && y > prior_y) ||
                      (npshr && y < prior_y))) {
            return jerr(npshr
                            ? "NPSHr curve needs strictly increasing flow and "
                              "non-decreasing NPSHr"
                            : "pump curve needs strictly increasing flow and "
                              "non-increasing head");
        }
        prior_q = q;
        prior_y = y;
    }
    const ojson speed = curve.value("speed_ratio", ojson(1.0));
    if (!finite_number(speed) || speed.get<double>() <= 0)
        return jerr("pump curve speed_ratio must be finite and > 0");
    return nullptr;
}


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
    if (pumps.is_null()) return nullptr;
    if (!pumps.is_array()) return jerr("pumps must be a list");
    std::map<std::string, bool> attached;
    for (const auto& spec : pumps) {
        if (!spec.is_object()) return jerr("each pump specification must be an object");
        const ojson run = spec.value("run", ojson(nullptr));
        if (!run.is_string() || run.get<std::string>().empty())
            return jerr("pump run must be a non-empty string");
        const std::string run_id = run.get<std::string>();
        if (attached[run_id])
            return jerr("multiple pumps on run " + py_repr(run_id) + " are ambiguous");
        Link* lk = nullptr;
        for (auto& l : ctx.links)
            if (l.id == run_id) lk = &l;
        if (lk == nullptr) {
            ojson ids = ojson::array();
            for (const auto& l : ctx.links) ids.push_back(l.id);
            return jerr("pump references unknown run " + py_repr(run_id),
                        "runs available: " + py_repr(ids));
        }
        const std::string direction = spec.value("direction", std::string("a_to_b"));
        if (direction != "a_to_b" && direction != "b_to_a")
            return jerr("pump direction must be 'a_to_b' or 'b_to_a'");
        const ojson speed = spec.value("speed_ratio", ojson(1.0));
        if (!finite_number(speed) || speed.get<double>() <= 0)
            return jerr("pump speed_ratio must be finite and > 0");

        const ojson curve = spec.value("curve", ojson(nullptr));
        ojson pump = nullptr;
        if (curve.is_object() && curve.value("ok", false)) {
            if (ojson err = validate_curve_object(curve); !err.is_null()) return err;
            pump = curve;
        } else if (py_truthy(curve)) {
            pump = load_pump_curve_json(curve, speed.get<double>(), "flow", "head");
        }
        if (pump.is_null() || !pump.value("ok", false)) {
            return pump.is_object() ? pump : jerr("pump missing 'curve'");
        }
        pump["sign"] = direction == "b_to_a" ? -1.0 : 1.0;
        lk->pump = std::move(pump);
        attached[run_id] = true;
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
            refresh_link_fittings(lk);
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
    ojson warnings = ctx.warnings;
    for (const auto& row : link_out) {
        const double velocity = std::abs(row["velocity_ms"].get<double>());
        if (velocity > 3.0) {
            warnings.push_back(
                row["id"].get<std::string>() + ": liquid velocity " +
                fmt_f(velocity, 2) +
                " m/s exceeds the 3 m/s general erosion/noise advisory limit");
        }
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
    out["ok"] = converged;
    if (!converged) out["reason"] = "piping solver did not converge";
    out["converged"] = converged;
    out["iterations"] = it;
    out["max_imbalance_m3h"] = pyround(max_imbalance * 3600.0, 6);
    out["fluid"] = std::move(fluid_out);
    out["nodes"] = std::move(node_out);
    out["links"] = std::move(link_out);
    out["warnings"] = std::move(warnings);
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
    if (nt != "liquid" && nt != "incompressible" &&
        nt != "gas" && nt != "compressible") {
        return jerr("network_type must be 'liquid' or 'gas'");
    }
    if (!std::isfinite(tol_mm) || tol_mm <= 0 ||
        max_iter <= 0 || !std::isfinite(tol_head) || tol_head <= 0) {
        return jerr("tol_mm, max_iter, and solver tolerance must be finite and > 0");
    }
    if (nt == "gas" || nt == "compressible") {
        if (py_truthy(pumps))
            return jerr("pumps are not supported by the gas-network solver");
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
    if (fl.value("phase", std::string("liquid")) == "gas") {
        return jerr("named gas fluid requires network_type='gas'",
                    "the liquid solver assumes incompressible density");
    }
    const double rho = fl["rho"].get<double>();
    const double mu = fl["mu"].get<double>();

    NetCtx ctx = build_network(doc, fl, tol_mm);
    if (!ctx.ok) return ctx.err;
    if (ojson err = apply_boundary(ctx, boundary, tol_mm, rho); !err.is_null()) return err;
    if (ojson err = validate_anchored_components(ctx, "head/pressure"); !err.is_null())
        return err;
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
            for (auto& lk : links) {
                lk.fd =
                    friction_factor_lk(lk, std::max(std::abs(lk.flow), kMinAbsQ), mu, rho);
                refresh_link_fittings(lk);
            }

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
    converged = converged && std::isfinite(max_imbalance) &&
                max_imbalance <= tol_q;
    const int iterations_run = nfree ? std::min(it, max_iter) : 0;
    return assemble_result(ctx, nodes, links, H, fl, converged,
                           iterations_run, max_imbalance);
}

nlohmann::json run_loss_1d(const nlohmann::json& doc_in, const std::string& run_id, double rho,
                           double mu, double flow_m3s, double roughness_mm) {
    auto fail = [](const std::string& reason, const std::string& hint) {
        return nlohmann::json{{"ok", false}, {"reason", reason}, {"hint", hint}};
    };
    if (!doc_in.is_object() || !doc_in.value("runs", nlohmann::json()).is_array())
        return fail("invalid_piping", "piping must be an aicad.piping/1 document with runs");
    if (!(std::isfinite(rho) && rho > 0 && std::isfinite(mu) && mu > 0 &&
          std::isfinite(flow_m3s) && flow_m3s > 0))
        return fail("invalid_params", "the 1-D estimate needs positive rho, mu and flow");
    const ojson doc(doc_in);
    const ojson* run = nullptr;
    ojson ids = ojson::array();
    for (const auto& r : doc["runs"]) {
        if (!r.is_object() || r.value("suppressed", false)) continue;
        ids.push_back(r.value("id", ojson(nullptr)));
        if (r.value("id", ojson(nullptr)) == ojson(run_id)) run = &r;
    }
    if (!run)
        return fail("unknown_run", "no unsuppressed run '" + run_id + "' in the piping document; "
                                   "runs: " + ids.dump());
    ojson fluid = ojson::object();
    fluid["rho"] = rho;
    fluid["mu"] = mu;
    NetCtx ctx = build_network(doc, fluid, kSnapTolMm);
    if (!ctx.ok)
        return fail("invalid_piping", ctx.err.value("reason", std::string("piping network build "
                                                                          "failed")) +
                                          "; " + ctx.err.value("hint", std::string()));
    auto lk_it = std::find_if(ctx.links.begin(), ctx.links.end(),
                              [&](const Link& l) { return l.id == run_id; });
    if (lk_it == ctx.links.end())
        return fail("degenerate_run", "run '" + run_id + "' has zero length");
    const Link& lk = *lk_it;

    // Same friction law and fitting K-factors piping_solve applies to this link, evaluated at
    // the given flow. roughness_mm < 0 keeps the run's material roughness.
    const double v = flow_m3s / lk.area;
    const double Re = reynolds(v, lk.d_m, rho, mu);
    const double eps_m = roughness_mm >= 0.0 ? roughness_mm * 1e-3 : lk.eps;
    const double fd = Re < 2100 ? std::max(64.0 / std::max(Re, 1e-6), 1e-4)
                                : clamond(Re, eps_m / lk.d_m);
    const double q = 0.5 * rho * v * v;

    // Friction over the run's centreline measured corner to corner — the length piping_solve
    // (and Crane practice) uses — plus the K of every fitting on the run. A path corner with no
    // elbow fitting is still a swept bend in the bore (run.bend_radius, null = 1.5·OD, as
    // services/piping/threed.py and cfd_handoff.py sweep it), so it adds a Rennels bend K.
    double od_mm = lk.d_m * 1000.0;
    if (!py_truthy(run->value("bore_mm", ojson(nullptr)))) {
        const ojson spec = nearest_pipe(run->value("nps", ojson(nullptr)),
                                        run->value("schedule", std::string("40")));
        if (spec.value("ok", false) && spec["outer_dia"].is_number())
            od_mm = spec["outer_dia"].get<double>();
    }
    const ojson br = run->value("bend_radius", ojson(nullptr));
    const double bend_r_mm = br.is_number() && br.get<double>() > 0 ? br.get<double>() : 1.5 * od_mm;
    const ojson& path = (*run)["path"];
    const double poly_mm = polyline_length_mm(path);
    int bends = 0;
    nlohmann::json fittings = nlohmann::json::array();
    double k_sum = 0.0;
    auto add_fitting = [&](const nlohmann::json& id, const std::string& type, double K,
                           const std::string& method) {
        k_sum += K;
        fittings.push_back({{"id", id}, {"type", type}, {"K", K}, {"method", method},
                            {"dp_pa", K * q}});
    };
    for (const auto& ft : lk.fittings) {
        const auto [K, method] = fitting_K(ft, lk.d_m, fd, std::abs(Re), lk.id);
        add_fitting(nlohmann::json(ft.value("id", ojson(nullptr))),
                    ft.value("type", std::string()), K, method);
    }
    // Swept-arc take-out R·tan(θ/2) at each corner: what is left of the straight end legs.
    std::vector<double> takeout(path.size(), 0.0);
    for (size_t i = 1; i + 1 < path.size(); ++i) {
        const Vec3 a = to_vec3(path[i - 1]), b = to_vec3(path[i]), c = to_vec3(path[i + 1]);
        const double lab = dist3(a, b), lbc = dist3(b, c);
        if (lab <= 0.0 || lbc <= 0.0) continue;
        double cosang = 0.0;
        for (int k = 0; k < 3; ++k) cosang += (b[k] - a[k]) * (c[k] - b[k]);
        cosang = std::clamp(cosang / (lab * lbc), -1.0, 1.0);
        const double theta_deg = std::acos(cosang) * 180.0 / M_PI;
        if (theta_deg < 0.5) continue;
        ++bends;
        takeout[i] = bend_r_mm * std::tan(0.5 * theta_deg * M_PI / 180.0);
        bool has_elbow = false;
        for (const auto& ft : lk.fittings)
            if (ft.value("type", std::string()) == "elbow" &&
                dist3(to_vec3(ft.value("origin", ojson::array({0.0, 0.0, 0.0}))), b) <= kSnapTolMm)
                has_elbow = true;
        if (!has_elbow) {  // a swept corner with no elbow fitting still turns the flow
            const double rd = bend_r_mm / (lk.d_m * 1000.0);
            add_fitting(nullptr, "bend", bend_rounded_rennels(lk.d_m, theta_deg, fd, rd),
                        "bend_rounded(path corner," + fmt_g(theta_deg, 4) + "deg,r/D=" +
                            fmt_g(rd, 3) + ")");
        }
    }
    // Straight run from each end to the first bend's tangent point (the whole path if none).
    auto end_straight = [&](bool from_start) {
        double s = 0.0;
        const size_t n = path.size();
        for (size_t k = 1; k < n; ++k) {
            const size_t i = from_start ? k : n - 1 - k;  // vertex reached
            const size_t j = from_start ? k - 1 : n - k;  // vertex left
            s += dist3(to_vec3(path[j]), to_vec3(path[i]));
            if (takeout[i] > 0.0) return std::max(0.0, s - takeout[i]);
        }
        return s;
    };
    const double friction_pa = fd * (poly_mm * 1e-3) / lk.d_m * q;
    return {{"ok", true},
            {"run_id", run_id},
            {"inner_dia_mm", lk.d_m * 1000.0},
            {"velocity_ms", v},
            {"reynolds", Re},
            {"friction_factor", fd},
            {"roughness_mm", eps_m * 1000.0},
            {"material_roughness_mm", lk.eps * 1000.0},
            {"dynamic_pressure_pa", q},
            {"centreline_length_mm", poly_mm},
            {"bend_radius_mm", bend_r_mm},
            {"bends", bends},
            {"inlet_straight_mm", end_straight(true)},
            {"outlet_straight_mm", end_straight(false)},
            {"friction_pa", friction_pa},
            {"fittings", fittings},
            {"fittings_K", k_sum},
            {"fittings_pa", k_sum * q},
            {"total_pa", friction_pa + k_sum * q}};
}

ojson pressure_drop_report(const ojson& doc, const ojson& result_in,
                           const ojson& boundary, const ojson& fluid,
                           const ojson& pumps, double tol_mm) {
    if (!std::isfinite(tol_mm) || tol_mm <= 0)
        return jerr("tol_mm must be finite and > 0");
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
    if (!result.value("converged", false))
        return jerr("cannot report a non-converged piping solve",
                    "rerun piping_solve with valid connected boundaries");

    const ojson report_fluid =
        py_truthy(fluid) ? fluid : result.value("fluid", ojson(nullptr));
    const ojson fl = resolve_solver_fluid(report_fluid);
    if (!fl.value("ok", true) && fl.contains("reason")) return fl;
    const double rho = fl["rho"].get<double>();
    const double mu = fl["mu"].get<double>();
    NetCtx ctx = build_network(doc, fl, tol_mm);
    if (!ctx.ok) return ctx.err;

    std::map<std::string, ojson> link_res;
    for (const auto& lk : result["links"]) link_res[lk["id"].get<std::string>()] = lk;

    ojson segments = ojson::array();
    std::map<std::string, double> segment_loss_kpa;
    for (auto& lk : ctx.links) {
        auto it = link_res.find(lk.id);
        if (it == link_res.end()) {
            return jerr("solve result is missing link " + py_repr(lk.id),
                        "report doc and solve result must describe the same network");
        }
        const ojson& lr = it->second;
        const double q = lr["flow_m3h"].get<double>() / 3600.0;
        const double v = lk.area ? q / lk.area : 0.0;
        lk.fd = friction_factor_lk(lk, std::max(std::abs(q), kMinAbsQ), mu, rho);
        refresh_link_fittings(lk);
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
        const double seg_dp_kpa = seg_dp / 1000.0;
        segment_loss_kpa[lk.id] = seg_dp_kpa;
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
        seg["dp_kpa"] = pyround(seg_dp_kpa, 4);
        seg["breakdown"] = std::move(breakdown);
        segments.push_back(std::move(seg));
    }

    // Resolve the actual source-to-terminal topology from signed solved flow.
    // A network total is a path sum, never the sum of every link in the model.
    struct ReportArc {
        std::string link_id;
        std::string to;
        double loss_kpa = 0.0;
    };
    constexpr double kReportFlowEpsM3h = 1e-9;
    std::map<std::string, std::vector<ReportArc>> downstream;
    std::map<std::string, double> net_outflow_m3h;
    ojson diagnostics = ojson::array();
    for (const auto& lr : result["links"]) {
        const std::string id = lr["id"].get<std::string>();
        const std::string from = lr["from"].get<std::string>();
        const std::string to = lr["to"].get<std::string>();
        const double q_m3h = lr["flow_m3h"].get<double>();
        if (!std::isfinite(q_m3h)) {
            diagnostics.push_back("link " + id +
                                  " has non-finite solved flow; path direction is undefined");
            continue;
        }
        if (std::abs(q_m3h) <= kReportFlowEpsM3h) {
            diagnostics.push_back("link " + id +
                                  " has zero solved flow and was excluded from path totals");
            continue;
        }
        const bool forward = q_m3h > 0.0;
        const std::string& upstream = forward ? from : to;
        const std::string& downstream_node = forward ? to : from;
        auto loss_it = segment_loss_kpa.find(id);
        if (loss_it == segment_loss_kpa.end()) {
            return jerr("solve result contains unknown link " + py_repr(id),
                        "report doc and solve result must describe the same network");
        }
        downstream[upstream].push_back({id, downstream_node, loss_it->second});
        net_outflow_m3h[upstream] += std::abs(q_m3h);
        net_outflow_m3h[downstream_node] -= std::abs(q_m3h);
    }

    std::vector<std::string> sources;
    std::vector<std::string> terminals;
    std::map<std::string, std::string> terminal_kind;
    for (const auto& node : result.value("nodes", ojson::array())) {
        const std::string id = node["id"].get<std::string>();
        const double demand_m3h = node.value("demand_m3h", 0.0);
        const bool fixed = node.value("fixed", false);
        const double net_out = net_outflow_m3h[id];
        if ((fixed && net_out > kReportFlowEpsM3h) ||
            demand_m3h < -kReportFlowEpsM3h) {
            sources.push_back(id);
        }
        if (demand_m3h > kReportFlowEpsM3h) {
            terminals.push_back(id);
            terminal_kind[id] = "demand";
        } else if (fixed && net_out < -kReportFlowEpsM3h) {
            terminals.push_back(id);
            terminal_kind[id] = "fixed_pressure_sink";
        }
    }
    if (sources.empty()) {
        diagnostics.push_back(
            "no solved-flow source could be identified (expected a fixed node with net "
            "outflow or a negative-demand injection)");
    }
    if (terminals.empty()) {
        diagnostics.push_back(
            "no demand terminal or fixed-pressure sink could be identified");
    }

    constexpr size_t kMaxReportPaths = 4096;
    ojson paths = ojson::array();
    std::map<std::string, bool> reached_terminal;
    std::vector<std::string> link_path;
    std::vector<std::string> node_path;
    std::map<std::string, bool> visited;
    bool truncated = false;
    double governing_total = -1.0;
    ojson governing_path = nullptr;
    for (const std::string& source : sources) {
        for (const std::string& terminal : terminals) {
            link_path.clear();
            node_path = {source};
            visited.clear();
            visited[source] = true;
            std::function<void(const std::string&, double)> walk =
                [&](const std::string& node, double total_kpa) {
                    if (paths.size() >= kMaxReportPaths) {
                        truncated = true;
                        return;
                    }
                    if (node == terminal) {
                        ojson path = ojson::object();
                        path["source_node"] = source;
                        path["terminal_node"] = terminal;
                        path["terminal_kind"] = terminal_kind[terminal];
                        path["link_ids"] = link_path;
                        path["node_ids"] = node_path;
                        path["total_dp_kpa"] = pyround(total_kpa, 4);
                        reached_terminal[terminal] = true;
                        if (total_kpa > governing_total) {
                            governing_total = total_kpa;
                            governing_path = path;
                        }
                        paths.push_back(std::move(path));
                        return;
                    }
                    auto it = downstream.find(node);
                    if (it == downstream.end()) return;
                    for (const ReportArc& arc : it->second) {
                        if (visited[arc.to]) continue;
                        visited[arc.to] = true;
                        link_path.push_back(arc.link_id);
                        node_path.push_back(arc.to);
                        walk(arc.to, total_kpa + arc.loss_kpa);
                        node_path.pop_back();
                        link_path.pop_back();
                        visited[arc.to] = false;
                        if (truncated) return;
                    }
                };
            walk(source, 0.0);
            if (truncated) break;
        }
        if (truncated) break;
    }
    if (truncated) {
        diagnostics.push_back(
            "source-to-terminal path enumeration exceeded 4096 simple paths; "
            "the governing path is not proven");
    }
    for (const std::string& terminal : terminals) {
        if (!reached_terminal[terminal]) {
            diagnostics.push_back("terminal " + terminal +
                                  " has no directed solved-flow path from an identified source");
        }
    }
    bool every_terminal_reached = !terminals.empty();
    for (const std::string& terminal : terminals)
        every_terminal_reached = every_terminal_reached && reached_terminal[terminal];
    const bool path_complete = !paths.empty() && !truncated && every_terminal_reached;

    ojson fluid_out = ojson::object();
    fluid_out["name"] = fl.value("name", std::string("custom"));
    fluid_out["rho"] = fl["rho"];
    fluid_out["mu"] = fl["mu"];
    fluid_out["source"] = fl.value("source", ojson(nullptr));
    fluid_out["phase"] = fl.value("phase", std::string("liquid"));

    ojson warnings = ctx.warnings;
    for (const auto& diagnostic : diagnostics) warnings.push_back(diagnostic);
    ojson path_analysis = ojson::object();
    path_analysis["complete"] = path_complete;
    path_analysis["sources"] = sources;
    path_analysis["terminals"] = terminals;
    path_analysis["path_count"] = paths.size();
    path_analysis["diagnostics"] = diagnostics;

    ojson out = ojson::object();
    out["ok"] = path_complete;
    out["fluid"] = std::move(fluid_out);
    out["segments"] = std::move(segments);
    out["path_totals"] = std::move(paths);
    out["governing_path"] = governing_path;
    out["total_dp_kpa"] =
        path_complete ? ojson(pyround(governing_total, 4)) : ojson(nullptr);
    out["path_analysis"] = std::move(path_analysis);
    out["converged"] = true;
    out["warnings"] = std::move(warnings);
    out["hint"] =
        path_complete
            ? ojson(nullptr)
            : ojson("select or correct a source-to-demand flow path before sizing a pump");
    if (!path_complete) {
        out["reason"] =
            "pressure-drop path total is ambiguous or incomplete; per-segment details remain "
            "available";
    }
    out["next_actions"] = ojson::array(
        {"use governing_path.total_dp_kpa for the worst resolved source-to-demand path"});
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
ojson apply_gas_boundary(NetCtx& ctx, const ojson& boundary, double tol,
                         double rho_std) {
    const ojson bcs = boundary_list(boundary);
    if (!bcs.is_array() || bcs.empty())
        return jerr("gas boundary conditions must be a non-empty list");
    int n_fixed = 0;
    std::map<int, std::string> assigned;
    for (const auto& bc : bcs) {
        if (ojson err = validate_boundary_ref(bc); !err.is_null()) return err;
        const int conditions = static_cast<int>(bc.contains("pressure_abs")) +
                               static_cast<int>(bc.contains("pressure")) +
                               static_cast<int>(bc.contains("mass_demand")) +
                               static_cast<int>(bc.contains("std_demand")) +
                               static_cast<int>(bc.contains("demand"));
        if (conditions != 1) {
            return jerr(
                "each gas boundary condition must specify exactly one value",
                "use pressure_abs, pressure, mass_demand, std_demand, or demand");
        }
        const char* key = bc.contains("pressure_abs") ? "pressure_abs"
                          : bc.contains("pressure")   ? "pressure"
                          : bc.contains("mass_demand") ? "mass_demand"
                          : bc.contains("std_demand")  ? "std_demand"
                                                       : "demand";
        if (!finite_number(bc[key]))
            return jerr(std::string("gas boundary ") + key + " must be finite");
        double p_abs = 0.0;
        const bool fixed = std::string(key) == "pressure_abs" ||
                           std::string(key) == "pressure";
        if (fixed) {
            p_abs = std::string(key) == "pressure_abs"
                        ? bc[key].get<double>() * 1000.0
                        : bc[key].get<double>() * 1000.0 + P_ATM;
            if (!std::isfinite(p_abs) || p_abs <= 0)
                return jerr("gas boundary absolute pressure must be > 0 Pa");
        }

        const int idx = resolve_node(bc, ctx, tol);
        if (idx < 0) {
            return jerr("boundary reference " + py_repr(bc) + " did not resolve to a node",
                        "use {'node':'n0'} / {'at':[x,y,z]} / {'run':'r1','end':'start'}");
        }
        Node& nd = ctx.nodes[idx];
        auto prior = assigned.find(idx);
        if (prior != assigned.end() &&
            (fixed || prior->second == "fixed")) {
            return jerr("node " + nd.id +
                        " has conflicting or duplicate gas boundary conditions");
        }
        if (fixed) {
            assigned[idx] = "fixed";
            nd.fixed = true;
            nd.head = p_abs * p_abs;
            if (!std::isfinite(nd.head)) {
                return jerr("gas boundary pressure-squared conversion overflowed");
            }
            ++n_fixed;
        } else {
            assigned[idx] = "demand";
            if (std::string(key) == "mass_demand")
                nd.demand += bc[key].get<double>() / 3600.0;
            else
                nd.demand += bc[key].get<double>() * rho_std / 3600.0;
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
    out["ok"] = converged;
    if (!converged) out["reason"] = "gas piping solver did not converge";
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
    if (!std::isfinite(tol_mm) || tol_mm <= 0 || max_iter <= 0 ||
        !std::isfinite(tol_mass) || tol_mass <= 0) {
        return jerr("tol_mm, max_iter, and tol_mass must be finite and > 0");
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
    if (ojson err = validate_anchored_components(ctx, "pressure"); !err.is_null())
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
            for (auto& lk : links) {
                lk.fd =
                    gas_friction_factor(lk, std::max(std::abs(lk.flow), 1e-9), mu);
                refresh_link_fittings(lk);
            }

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
            refresh_link_fittings(lk);
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
    for (double value : phi)
        converged = converged && std::isfinite(value) && value > 0;
    converged = converged && std::isfinite(max_imbalance) &&
                max_imbalance <= tol_mass;
    const int iterations_run = nfree ? std::min(it, max_iter) : 0;

    return assemble_gas_result(ctx, nodes, links, phi, fl, M, T, mu, rho_std,
                               converged, iterations_run, max_imbalance);
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
                                            const ojson& fluid,
                                            const ojson& q_ref_m3h) {
    const double q_ref =
        q_ref_m3h.is_null() ? 50.0
                            : (finite_number(q_ref_m3h)
                                   ? q_ref_m3h.get<double>()
                                   : std::numeric_limits<double>::quiet_NaN());
    if (!std::isfinite(q_ref) || q_ref <= 0) {
        return {nullptr, jerr("q_ref_m3h must be finite and > 0")};
    }
    const ojson bcs = boundary_list(boundary);
    if (!bcs.is_array()) {
        return {nullptr, jerr("cannot derive a system curve",
                              "boundary must be a list")};
    }
    int demand_count = 0;
    int fixed_count = 0;
    ojson terminal_pressure_kpa = nullptr;
    ojson probe = ojson::array();
    for (const auto& bc : bcs) {
        ojson b = bc;
        if (bc.is_object() &&
            (bc.contains("demand") || bc.contains("demand_lps"))) {
            ++demand_count;
            b = ojson::object();
            for (auto it = bc.begin(); it != bc.end(); ++it)
                if (it.key() != "demand" && it.key() != "demand_lps")
                    b[it.key()] = it.value();
            b["demand"] = q_ref;
            if (bc.contains("terminal_pressure_kpa")) {
                if (!finite_number(bc["terminal_pressure_kpa"]) ||
                    bc["terminal_pressure_kpa"].get<double>() <=
                        -P_ATM / 1000.0) {
                    return {nullptr,
                            jerr("terminal_pressure_kpa must be finite and imply "
                                 "positive absolute pressure")};
                }
                terminal_pressure_kpa = bc["terminal_pressure_kpa"];
            }
        } else if (bc.is_object() &&
                   (bc.contains("pressure") || bc.contains("head"))) {
            ++fixed_count;
        }
        probe.push_back(b);
    }
    if (demand_count != 1 || fixed_count != 1) {
        return {nullptr,
                jerr("cannot derive a system curve",
                     "boundary needs exactly one fixed pressure/head node and exactly "
                     "one demand node, or pass system={'static_head_m','k'}")};
    }
    if (terminal_pressure_kpa.is_null()) {
        return {
            nullptr,
            jerr("cannot derive static system head without a terminal-pressure assumption",
                 "add terminal_pressure_kpa to the demand boundary (use 0 for an "
                 "atmospheric discharge), or pass system={'static_head_m','k'}")};
    }
    const ojson res = solve(doc, probe, fluid);
    if (!res.value("ok", false)) return {nullptr, res};

    double fixed_head = 0.0;
    double demand_head = 0.0;
    double z_demand = 0.0;
    bool found_fixed = false;
    bool found_demand = false;
    for (const auto& n : res["nodes"]) {
        if (n.value("fixed", false)) {
            fixed_head = n["head_m"].get<double>();
            found_fixed = true;
        }
        if (std::abs(n["demand_m3h"].get<double>() - q_ref) < 1e-3) {
            demand_head = n["head_m"].get<double>();
            z_demand = n["xyz"][2].get<double>() / 1000.0;
            found_demand = true;
        }
    }
    if (!found_fixed || !found_demand) {
        return {nullptr, jerr("cannot identify fixed and demand nodes in system-curve probe")};
    }
    // The solved source-to-demand total-head difference is the friction loss at
    // q_ref.  The zero-flow static requirement is instead the terminal total
    // head required by the caller minus the total head already available at the
    // source.  Keeping these terms separate gives a +10 m requirement for an
    // atmospheric outlet 10 m above an atmospheric source, and subtracts source
    // pressure assistance with the correct sign.
    const double total_loss_m = std::abs(fixed_head - demand_head);
    const double k = total_loss_m / (q_ref * q_ref);
    const double rho = res["fluid"]["rho"].get<double>();
    const double terminal_pressure = terminal_pressure_kpa.get<double>();
    const double terminal_total_head =
        z_demand + terminal_pressure * 1000.0 / (rho * kG);
    const double static_head = terminal_total_head - fixed_head;
    ojson system = ojson::object();
    system["static_head_m"] = static_head;
    system["k"] = k;
    ojson meta = ojson::object();
    meta["q_ref_m3h"] = q_ref;
    meta["total_loss_m"] = pyround(total_loss_m, 4);
    meta["static_head_m"] = pyround(static_head, 4);
    meta["source_total_head_m"] = pyround(fixed_head, 4);
    meta["destination_elevation_m"] = pyround(z_demand, 4);
    meta["terminal_pressure_kpa"] = terminal_pressure;
    meta["terminal_total_head_m"] = pyround(terminal_total_head, 4);
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
    if (!suction.is_object()) {
        ojson out = nulls();
        out["reason"] = "suction must be an object";
        return out;
    }
    const ojson fl = resolve_solver_fluid(fluid);
    if (!fl.value("ok", true) && fl.contains("reason")) {
        ojson out = nulls();
        out["reason"] = fl["reason"];
        return out;
    }
    const double rho = fl["rho"].get<double>();
    auto finite_suction = [&](const char* key, double def) {
        const ojson value = suction.value(key, ojson(def));
        return finite_number(value) ? value.get<double>()
                                    : std::numeric_limits<double>::quiet_NaN();
    };
    const double atm_kpa = finite_suction("atm_kpa", P_ATM / 1000.0);
    const double pressure_kpa = finite_suction("pressure_kpa", 0.0);
    const double z = finite_suction("elevation_m", 0.0);
    const double h_fric = finite_suction("friction_m", 0.0);
    if (!std::isfinite(atm_kpa) || atm_kpa <= 0 ||
        !std::isfinite(pressure_kpa) || !std::isfinite(z) ||
        !std::isfinite(h_fric) || h_fric < 0) {
        ojson out = nulls();
        out["reason"] =
            "suction pressures/elevation must be finite, atm_kpa > 0, and friction_m >= 0";
        return out;
    }
    const double p_atm = atm_kpa * 1000.0;
    const double p_suct_g = pressure_kpa * 1000.0;
    if (p_atm + p_suct_g <= 0) {
        ojson out = nulls();
        out["reason"] = "suction gauge pressure implies non-positive absolute pressure";
        return out;
    }
    double p_vap;
    ojson pvap_src;
    if (suction.contains("pvap_kpa")) {
        if (!finite_number(suction["pvap_kpa"]) ||
            suction["pvap_kpa"].get<double>() < 0) {
            ojson out = nulls();
            out["reason"] = "suction pvap_kpa must be finite and >= 0";
            return out;
        }
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
    const double npsha =
        (p_atm + p_suct_g - p_vap) / (rho * kG) + z - h_fric;
    if (!std::isfinite(npsha)) {
        ojson out = nulls();
        out["reason"] = "NPSHa calculation produced a non-finite value";
        return out;
    }

    double npshr = kNoInterp;
    if (!npshr_curve.is_null()) {
        ojson curve = npshr_curve;
        if (!curve.is_object())
            curve = load_pump_curve_json(curve, 1.0, "flow", "npshr");
        if (!curve.value("ok", false)) {
            ojson out = nulls();
            out["reason"] = curve.value("reason", std::string("invalid NPSHr curve"));
            return out;
        }
        if (ojson err = validate_curve_object(curve, true); !err.is_null()) {
            ojson out = nulls();
            out["reason"] = err["reason"];
            return out;
        }
        npshr = interp_curve(curve["points"], q_m3h);
        if (std::isnan(npshr)) {
            ojson out = nulls();
            out["reason"] = "NPSHr curve does not cover the duty-point flow";
            return out;
        }
    }
    const bool have_r = !std::isnan(npshr);
    ojson out = ojson::object();
    out["ok"] = true;
    out["npsha_m"] = pyround(npsha, 4);
    out["npshr_m"] = have_r ? ojson(pyround(npshr, 4)) : ojson(nullptr);
    out["margin_m"] = have_r ? ojson(pyround(npsha - npshr, 4)) : ojson(nullptr);
    out["cavitation_risk"] = npsha <= 0 || (have_r && npsha <= npshr);
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
    if (!std::isfinite(speed_ratio) || speed_ratio <= 0)
        return jerr("speed_ratio must be finite and > 0");
    ojson pump = nullptr;
    if (pump_curve.is_object() && pump_curve.value("ok", false)) {
        if (ojson err = validate_curve_object(pump_curve); !err.is_null()) return err;
        pump = pump_curve;
        if (!pump.contains("shutoff_head")) pump["shutoff_head"] = pump["points"][0][1];
        if (!pump.contains("max_flow")) pump["max_flow"] = pump["points"].back()[0];
        if (!pump.contains("speed_ratio")) pump["speed_ratio"] = speed_ratio;
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
    if (!system.is_object()) return jerr("system curve must be an object");
    const ojson static_head_j = system.value("static_head_m", ojson(0.0));
    const ojson k_j = system.value("k", ojson(0.0));
    if (!finite_number(static_head_j) || !finite_number(k_j) ||
        k_j.get<double>() < 0) {
        return jerr("system static_head_m must be finite and k must be finite and >= 0");
    }

    // Intersect: g(Q) = H_pump(Q) − H_sys(Q). Bisect over the pump's flow range.
    const ojson& pts = pump["points"];
    const double q_lo0 = pts[0][0].get<double>(), q_hi0 = pts[pts.size() - 1][0].get<double>();
    auto g = [&](double q) { return pump_head(pump, q / 3600.0) - system_head(system, q); };
    double q_lo = q_lo0, q_hi = q_hi0;
    const double g_lo = g(q_lo), g_hi = g(q_hi);
    if (!std::isfinite(g_lo) || !std::isfinite(g_hi))
        return jerr("pump/system curve evaluation produced a non-finite value");
    if (g_lo < 0) {
        ojson out = jerr(
            "no operating point: system head exceeds pump shutoff head at minimum curve flow",
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
    if (!suction.is_null() && npsh.contains("reason")) {
        ojson err = jerr("invalid suction/NPSH setup: " +
                         npsh["reason"].get<std::string>());
        err["npsh"] = npsh;
        return err;
    }
    out["npsh"] = npsh;
    if (npsh.value("cavitation_risk", false)) {
        out["warnings"].push_back(
            "NPSHa is non-positive or does not exceed NPSHr — cavitation risk at the duty point");
        ojson na = ojson::array(
            {"raise suction pressure / reduce suction losses / lower NPSHr — cavitation risk"});
        for (const auto& a : out["next_actions"]) na.push_back(a);
        out["next_actions"] = std::move(na);
    }
    return out;
}

}  // namespace chudware::piping

namespace chudware::piping {
ojson piping_fitting_k(const ojson& args) {
    auto fail = [](const std::string& reason, const std::string& hint) {
        return ojson{{"ok", false}, {"reason", reason}, {"hint", hint}};
    };
    try {
        if (!args.is_object() || !args.value("fitting", ojson(nullptr)).is_object())
            return fail("invalid_params", "pass a fitting object");
        const auto input = args["fitting"];
        if (input.contains("params") && !input["params"].is_object())
            return fail("invalid_params", "fitting params must be an object");
        const auto nps = args.value("nps", input.value("nps", ojson(2)));
        const auto schedule = args.value("schedule", std::string("40"));
        const auto spec = nearest_pipe(nps, schedule);
        if (!spec.value("ok", false)) return fail("invalid_params", "use a catalog NPS/schedule");
        auto ft = new_fitting("ft1", input.value("type", std::string()), nps, nullptr,
                              schedule, "Preview", input.value("params", ojson::object()));
        ft["nps2"] = input.value("nps2", ojson(nullptr));
        if (input.contains("run_id")) ft["run_id"] = input["run_id"];
        const auto errors = validate_fitting(ft);
        if (!errors.empty()) {
            const bool invalid_model = std::any_of(errors.begin(), errors.end(), [](const auto& e) {
                return e.find(".params.k_model") != std::string::npos;
            });
            return fail(invalid_model ? "invalid_k_model" : "invalid_params", errors.front());
        }
        const double d = spec["inner_dia"].get<double>() / 1000;
        if (ft["type"] == "orifice" && ft["params"].value("beta", ojson(nullptr)).is_null() &&
            ft["params"]["bore_mm"].get<double>() >= d * 1000)
            return fail("invalid_params", "orifice bore must be smaller than pipe ID");
        double Re = 1e5;
        if (args.contains("reynolds")) {
            if (!finite_number(args["reynolds"]) || args["reynolds"].get<double>() <= 0)
                return fail("invalid_params", "reynolds must be finite and positive");
            Re = args["reynolds"].get<double>();
        } else if (args.contains("velocity_ms")) {
            if (!finite_number(args["velocity_ms"]))
                return fail("invalid_params", "velocity_ms must be finite");
            const auto fl = resolve_solver_fluid(args.value("fluid", ojson(nullptr)));
            if (!fl.value("ok", true)) return fl;
            Re = std::max(std::abs(args["velocity_ms"].get<double>()) *
                          d * fl["rho"].get<double>() / fl["mu"].get<double>(), 1e-6);
        }
        const double fT = 1 / std::pow(-2 * std::log10(0.0000457 / (3.7*d)), 2);
        const bool tee = ft["type"] == "tee";
        int tee_path = kTeeAuto;
        if (args.contains("path")) {
            const auto path = args["path"];
            if (!tee || (path != "run" && path != "branch"))
                return fail("invalid_params", "path is \"run\" or \"branch\" and applies only to tees");
            tee_path = path == "branch" ? kTeeBranch : kTeeRun;
        }
        auto evaluate = [&](double re, int which) {
            return fitting_K(ft, d, re < 2100 ? 64/re : clamond(re, 0.0000457/d), re,
                             ft.value("run_id", std::string()), which);
        };
        const auto [K, method] = evaluate(Re, tee_path);
        ojson curve = ojson::array();
        for (int i = 0; i < 24; ++i) {
            const double re = std::pow(10.0, 3.0 + 4.0 * i / 23.0);
            curve.push_back({{"re", re}, {"K", evaluate(re, tee_path).first}});
        }
        ojson out = {{"ok", true}, {"K", K}, {"method", method}, {"fT", fT},
                     {"inner_dia_mm", d*1000}, {"reynolds", Re}, {"curve", std::move(curve)}};
        if (tee) {
            // Both tee loss paths, each on the velocity of the run that carries it.
            const auto [K_run, method_run] = evaluate(Re, kTeeRun);
            const auto [K_branch, method_branch] = evaluate(Re, kTeeBranch);
            out["paths"] = {{"run", {{"K", K_run}, {"method", method_run}}},
                            {"branch", {{"K", K_branch}, {"method", method_branch}}}};
        }
        return out;
    } catch (const nlohmann::json::exception&) {
        return fail("invalid_params", "check fitting and numeric parameter types");
    }
}
}  // namespace chudware::piping
