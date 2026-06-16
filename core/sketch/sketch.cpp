// C++ port of engine/doc/sketch_solver.py (core subset). The residual/Jacobian
// formulas are copied from the Python helpers so the two stay at parity under the
// differential harness; see sketch.h for the coverage scope.
#include "sketch/sketch.h"

#include "solver/nsolve.h"
#include "util/expr.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <functional>
#include <initializer_list>
#include <limits>
#include <set>
#include <stdexcept>

namespace aicad::sketch {
namespace {

using json = nlohmann::json;
using Eigen::MatrixXd;
using Eigen::Vector2d;
using Eigen::VectorXd;
using aicad::solver::System;

constexpr double kDeg = 3.14159265358979323846 / 180.0;

using ValFn = std::function<Vector2d(const VectorXd&)>;
using JacFn = std::function<MatrixXd(const VectorXd&)>;

// A point handle: the variables it depends on, its world (x,y), and the (2,k)
// chain-rule Jacobian of that point w.r.t. those variables.
struct Handle {
    std::vector<int> idx;
    ValFn val;
    JacFn jac;
};

using EntMap = std::map<std::string, json>;
using VarMap = std::map<std::string, std::vector<int>>;

double num(const json& j) { return j.get<double>(); }

std::vector<int> concat(const std::vector<int>& a, const std::vector<int>& b) {
    std::vector<int> r = a;
    r.insert(r.end(), b.begin(), b.end());
    return r;
}
std::vector<int> concat(const std::vector<int>& a, const std::vector<int>& b,
                        const std::vector<int>& c) {
    return concat(concat(a, b), c);
}

// Pick a subset of an index vector by local positions.
std::vector<int> pick(const std::vector<int>& v, std::initializer_list<int> pos) {
    std::vector<int> r;
    for (int p : pos) r.push_back(v[p]);
    return r;
}

VectorXd vhead(const VectorXd& x, int n) { return x.head(n); }
VectorXd vseg(const VectorXd& x, int s, int n) { return x.segment(s, n); }

VectorXd r1(double v) {
    VectorXd r(1);
    r(0) = v;
    return r;
}
// Alias used where a local variable named `r1` shadows the helper.
VectorXd r1Helper(double v) { return r1(v); }

// Wrap a fixed 2-vector residual into a dynamic VectorXd.
VectorXd r1Vec2(const Vector2d& v) {
    VectorXd r(2);
    r << v(0), v(1);
    return r;
}

// Initial variable values for an entity in allocation order (for _add_fix's
// conic-family branch). Mirrors _entity_init_slice; only the generically-fixed
// types need it.
std::vector<double> entity_init_slice(const json& ent) {
    const std::string et = ent.at("type");
    if (et == "ellipse_arc") {
        double rot = ent.contains("rot") ? num(ent["rot"]) : 0.0;
        return {num(ent["center"][0]), num(ent["center"][1]), num(ent["rx"]),
                num(ent["ry"]), rot, num(ent["a0"]), num(ent["a1"])};
    }
    if (et == "parabola") {
        return {num(ent["vertex"][0]), num(ent["vertex"][1]), num(ent["focus"][0]),
                num(ent["focus"][1]), num(ent["t0"]), num(ent["t1"])};
    }
    if (et == "conic") {
        double rho = ent.contains("rho") ? num(ent["rho"]) : 0.5;
        return {num(ent["p0"][0]), num(ent["p0"][1]), num(ent["p1"][0]),
                num(ent["p1"][1]), num(ent["apex"][0]), num(ent["apex"][1]), rho};
    }
    if (et == "arc_slot") {
        return {num(ent["center"][0]), num(ent["center"][1]), num(ent["radius"]),
                num(ent["a0"]), num(ent["a1"]), num(ent["width"])};
    }
    return {};
}

// ---- entity parameterization ------------------------------------------------

std::vector<int> allocate_entity(System& sys, const json& e) {
    const std::string t = e["type"];
    if (t == "point") {
        Vector2d p(num(e["p"][0]), num(e["p"][1]));
        return sys.add_vars(2, {p(0), p(1)});
    }
    if (t == "line") {
        return sys.add_vars(4, {num(e["p1"][0]), num(e["p1"][1]),
                                num(e["p2"][0]), num(e["p2"][1])});
    }
    if (t == "circle") {
        return sys.add_vars(3, {num(e["center"][0]), num(e["center"][1]),
                                num(e["radius"])});
    }
    if (t == "arc") {
        return sys.add_vars(5, {num(e["center"][0]), num(e["center"][1]),
                                num(e["radius"]), num(e["start"]), num(e["end"])});
    }
    if (t == "ellipse") {
        return sys.add_vars(5, {num(e["center"][0]), num(e["center"][1]),
                                num(e["rx"]), num(e["ry"]), num(e["rot"])});
    }
    if (t == "ellipse_arc") {
        double rot = e.contains("rot") ? num(e["rot"]) : 0.0;
        return sys.add_vars(7, {num(e["center"][0]), num(e["center"][1]),
                                num(e["rx"]), num(e["ry"]), rot,
                                num(e["a0"]), num(e["a1"])});
    }
    if (t == "parabola") {
        return sys.add_vars(6, {num(e["vertex"][0]), num(e["vertex"][1]),
                                num(e["focus"][0]), num(e["focus"][1]),
                                num(e["t0"]), num(e["t1"])});
    }
    if (t == "conic") {
        double rho = e.contains("rho") ? num(e["rho"]) : 0.5;
        return sys.add_vars(7, {num(e["p0"][0]), num(e["p0"][1]),
                                num(e["p1"][0]), num(e["p1"][1]),
                                num(e["apex"][0]), num(e["apex"][1]), rho});
    }
    if (t == "slot") {
        return sys.add_vars(5, {num(e["p1"][0]), num(e["p1"][1]),
                                num(e["p2"][0]), num(e["p2"][1]), num(e["width"])});
    }
    if (t == "arc_slot") {
        return sys.add_vars(6, {num(e["center"][0]), num(e["center"][1]),
                                num(e["radius"]), num(e["a0"]), num(e["a1"]),
                                num(e["width"])});
    }
    if (t == "spline" || t == "style_spline") {
        std::vector<double> init;
        for (const auto& p : e["points"]) {
            init.push_back(num(p[0]));
            init.push_back(num(p[1]));
        }
        return sys.add_vars(static_cast<int>(init.size()), init);
    }
    if (t == "text") {
        return sys.add_vars(2, {num(e["p"][0]), num(e["p"][1])});
    }
    if (t == "eq_curve") {
        return sys.add_vars(0, {});
    }
    // Unknown / deferred type: no variables (treated as fixed), forward-tolerant.
    return sys.add_vars(0, {});
}

// ---- conic-family derived-point helpers (C28-A; port of sketch_solver.py) ----

// xs = [cx, cy, rx, ry, rot, a] (rot, a in degrees). Port of _ellipse_arc_pt_val.
Vector2d ellipse_arc_pt_val(const VectorXd& xs) {
    double cx = xs(0), cy = xs(1), rx = xs(2), ry = xs(3), rot = xs(4), a = xs(5);
    double rr = rot * kDeg, ar = a * kDeg;
    double lx = rx * std::cos(ar), ly = ry * std::sin(ar);
    double cr = std::cos(rr), sr = std::sin(rr);
    return Vector2d(cx + cr * lx - sr * ly, cy + sr * lx + cr * ly);
}
MatrixXd ellipse_arc_pt_jac(const VectorXd& xs) {
    double rx = xs(2), ry = xs(3), rot = xs(4), a = xs(5);
    double rr = rot * kDeg, ar = a * kDeg;
    double ca = std::cos(ar), sa = std::sin(ar);
    double cr = std::cos(rr), sr = std::sin(rr);
    double lx = rx * ca, ly = ry * sa;
    double dlx_drx = ca, dly_dry = sa;
    double dlx_da = -rx * sa * kDeg, dly_da = ry * ca * kDeg;
    double dwx_drot = (-sr * lx - cr * ly) * kDeg;
    double dwy_drot = (cr * lx - sr * ly) * kDeg;
    MatrixXd J(2, 6);
    J << 1.0, 0.0, cr * dlx_drx, -sr * dly_dry, dwx_drot, cr * dlx_da - sr * dly_da,
         0.0, 1.0, sr * dlx_drx, cr * dly_dry, dwy_drot, sr * dlx_da + cr * dly_da;
    return J;
}

// xs = [vx, vy, fx, fy, t]. Port of _parabola_pt_val.
Vector2d parabola_pt_val(const VectorXd& xs) {
    double vx = xs(0), vy = xs(1), fx = xs(2), fy = xs(3), t = xs(4);
    double dx = fx - vx, dy = fy - vy;
    double f = std::hypot(dx, dy);
    if (f < 1e-12) return Vector2d(vx, vy);
    double ux = dx / f, uy = dy / f;
    double wx = -uy, wy = ux;
    double s = (t * t) / (4.0 * f);
    return Vector2d(vx + s * ux + t * wx, vy + s * uy + t * wy);
}
// Numeric (central-difference) Jacobian. Port of _parabola_pt_jac.
MatrixXd parabola_pt_jac(const VectorXd& xs) {
    int n = static_cast<int>(xs.size());
    MatrixXd J(2, n);
    for (int i = 0; i < n; ++i) {
        double h = 1e-6 * std::max(1.0, std::abs(xs(i)));
        VectorXd xp = xs, xm = xs;
        xp(i) += h;
        xm(i) -= h;
        J.col(i) = (parabola_pt_val(xp) - parabola_pt_val(xm)) / (2.0 * h);
    }
    return J;
}

// arc_slot spine endpoint at trim angle a (deg): center + r*(cos a, sin a).
Vector2d arc_slot_pt_val(const VectorXd& xs) {
    double cx = xs(0), cy = xs(1), r = xs(2), a = xs(3);
    double ar = a * kDeg;
    return Vector2d(cx + r * std::cos(ar), cy + r * std::sin(ar));
}
MatrixXd arc_slot_pt_jac(const VectorXd& xs) {
    double r = xs(2), a = xs(3);
    double ar = a * kDeg;
    MatrixXd J(2, 4);
    J << 1.0, 0.0, std::cos(ar), -r * std::sin(ar) * kDeg,
         0.0, 1.0, std::sin(ar), r * std::cos(ar) * kDeg;
    return J;
}

// ---- handle resolution ------------------------------------------------------

Handle ident_point(const std::vector<int>& idx) {
    Handle h;
    h.idx = idx;
    h.val = [](const VectorXd& xs) { return Vector2d(xs(0), xs(1)); };
    h.jac = [](const VectorXd&) { return MatrixXd::Identity(2, 2); };
    return h;
}

// A derived-point handle with custom value/jacobian functions over local vars idx.
Handle derived_point(const std::vector<int>& idx, ValFn val, JacFn jac) {
    Handle h;
    h.idx = idx;
    h.val = std::move(val);
    h.jac = std::move(jac);
    return h;
}

Handle resolve_handle(const std::string& handle, const EntMap& entities,
                      const VarMap& var_map) {
    std::string eid = handle, sub;
    auto dot = handle.find('.');
    if (dot != std::string::npos) {
        eid = handle.substr(0, dot);
        sub = handle.substr(dot + 1);
    }

    if (eid == "origin" && !entities.count(eid)) {
        Handle h;
        h.val = [](const VectorXd&) { return Vector2d(0.0, 0.0); };
        h.jac = [](const VectorXd&) { return MatrixXd(2, 0); };
        return h;
    }

    const json& ent = entities.at(eid);
    const std::string t = ent.at("type");
    const std::vector<int>& vidx = var_map.at(eid);

    if (t == "point") return ident_point(vidx);
    if (t == "line") {
        if (sub.empty() || sub == "p1") return ident_point(pick(vidx, {0, 1}));
        if (sub == "p2") return ident_point(pick(vidx, {2, 3}));
        if (sub == "mid") {
            Handle h;
            h.idx = vidx;
            h.val = [](const VectorXd& xs) {
                return Vector2d((xs(0) + xs(2)) * 0.5, (xs(1) + xs(3)) * 0.5);
            };
            h.jac = [](const VectorXd&) {
                MatrixXd j(2, 4);
                j << 0.5, 0.0, 0.5, 0.0, 0.0, 0.5, 0.0, 0.5;
                return j;
            };
            return h;
        }
    }
    if (t == "circle") {
        if (sub.empty() || sub == "center") return ident_point(pick(vidx, {0, 1}));
    }
    if (t == "arc") {
        if (sub.empty() || sub == "center") return ident_point(pick(vidx, {0, 1}));
        if (sub == "start" || sub == "end") {
            // start point uses a0 (local idx 3), end uses a1 (local idx 4).
            int ai = (sub == "start") ? 3 : 4;
            Handle h;
            h.idx = pick(vidx, {0, 1, 2, ai});
            h.val = [](const VectorXd& xs) {
                double cx = xs(0), cy = xs(1), r = xs(2), a = xs(3) * kDeg;
                return Vector2d(cx + r * std::cos(a), cy + r * std::sin(a));
            };
            h.jac = [](const VectorXd& xs) {
                double r = xs(2), a = xs(3) * kDeg;
                MatrixXd j(2, 4);
                j << 1.0, 0.0, std::cos(a), -r * std::sin(a) * kDeg,
                     0.0, 1.0, std::sin(a), r * std::cos(a) * kDeg;
                return j;
            };
            return h;
        }
    }
    if (t == "ellipse") {
        if (sub.empty() || sub == "center") return ident_point(pick(vidx, {0, 1}));
    }
    if (t == "ellipse_arc") {
        if (sub.empty() || sub == "center") return ident_point(pick(vidx, {0, 1}));
        if (sub == "start" || sub == "end") {
            int ai = (sub == "start") ? 5 : 6;
            return derived_point(pick(vidx, {0, 1, 2, 3, 4, ai}),
                                 ellipse_arc_pt_val, ellipse_arc_pt_jac);
        }
    }
    if (t == "parabola") {
        if (sub.empty() || sub == "vertex") return ident_point(pick(vidx, {0, 1}));
        if (sub == "start" || sub == "end") {
            int ti = (sub == "start") ? 4 : 5;
            return derived_point(pick(vidx, {0, 1, 2, 3, ti}),
                                 parabola_pt_val, parabola_pt_jac);
        }
    }
    if (t == "conic") {
        if (sub.empty() || sub == "start" || sub == "p0")
            return ident_point(pick(vidx, {0, 1}));
        if (sub == "end" || sub == "p1") return ident_point(pick(vidx, {2, 3}));
        if (sub == "apex") return ident_point(pick(vidx, {4, 5}));
    }
    if (t == "slot") {
        if (sub.empty() || sub == "p1") return ident_point(pick(vidx, {0, 1}));
        if (sub == "p2") return ident_point(pick(vidx, {2, 3}));
    }
    if (t == "arc_slot") {
        if (sub.empty() || sub == "center") return ident_point(pick(vidx, {0, 1}));
        if (sub == "start" || sub == "end") {
            int ai = (sub == "start") ? 3 : 4;
            return derived_point(pick(vidx, {0, 1, 2, ai}),
                                 arc_slot_pt_val, arc_slot_pt_jac);
        }
    }
    if (t == "spline" || t == "style_spline") {
        if (sub.empty()) return ident_point(pick(vidx, {0, 1}));
        if (!sub.empty() && sub[0] == 'p') {
            int k = std::stoi(sub.substr(1));
            return ident_point(pick(vidx, {2 * k, 2 * k + 1}));
        }
    }
    if (t == "text") {
        return ident_point(pick(vidx, {0, 1}));
    }
    // Unknown sub-handle: resolve to a zero-variable origin-like point so the
    // constraint degrades gracefully (Python raises and the constraint is skipped;
    // here we let the caller's try/catch path mirror that — we throw).
    throw std::runtime_error("unresolved handle: " + handle);
}

// Build the std::map<string,double> the expr evaluator wants from the JSON env.
// Non-numeric env entries are skipped (the evaluator just won't find that name).
std::map<std::string, double> env_to_map(const json& env) {
    std::map<std::string, double> m;
    if (env.is_object()) {
        for (auto it = env.begin(); it != env.end(); ++it) {
            if (it.value().is_number()) m[it.key()] = it.value().get<double>();
        }
    }
    return m;
}

// Resolve a dimension constraint's float value. Mirrors _resolve_dim in
// sketch_solver.py EXACTLY: when `expr` is a non-null string we (1) prefer a bare
// env lookup if the whole expr string is itself an env key, else (2) evaluate it
// as arithmetic through the equations evaluator. Only when there is no expr do we
// fall back to the literal `value`. The evaluate() path matches the Python
// equations.evaluate within 1e-9 (the differential-harness coord tolerance).
double resolve_dim(const json& c, const json& env) {
    if (!c.contains("value")) return 0.0;
    const json& v = c["value"];
    if (v.is_number()) return v.get<double>();
    if (v.is_object()) {
        if (v.contains("expr") && v["expr"].is_string()) {
            std::string e = v["expr"];
            // (1) bare-variable lookup wins (matches `expr in equations_env`).
            if (env.is_object() && env.contains(e) && env[e].is_number()) {
                return env[e].get<double>();
            }
            // (2) arithmetic expression -> evaluate. On any evaluator error we
            // fall through to the literal `value` (forward-tolerant; the Python
            // would raise, but the host's solve path catches that the same way).
            try {
                return aicad::util::evaluate(e, env_to_map(env));
            } catch (const aicad::util::ExprError&) {
                // fall through to literal value below
            }
        }
        if (v.contains("value") && v["value"].is_number()) return v["value"].get<double>();
    }
    return 0.0;
}

bool is_line_ref(const std::string& h, const EntMap& entities) {
    std::string eid = h.substr(0, h.find('.'));
    auto it = entities.find(eid);
    return it != entities.end() && it->second.at("type") == "line" &&
           h.find('.') == std::string::npos;
}

std::string eid_of(const std::string& h) { return h.substr(0, h.find('.')); }

// Row-vector * (2,k) helper: returns 1xk.
Eigen::RowVectorXd rowdot(const Vector2d& d, const MatrixXd& J) {
    return d.transpose() * J;
}

// ---- constraint builders ----------------------------------------------------

// Per-ref arc-condition sign for a distance constraint (C50, SolidWorks min/max).
// -1 for "min" (near rim), +1 for "max" (far rim), 0 otherwise. Honored ONLY when
// the ref is a BARE circle/arc entity id (no ".sub"). Port of _distance_cond_sign.
double distance_cond_sign(const std::string& h, const std::string& c,
                          const EntMap& entities) {
    if (c != "min" && c != "max") return 0.0;
    if (h.find('.') != std::string::npos) return 0.0;
    auto it = entities.find(h);
    if (it == entities.end()) return 0.0;
    const std::string t = it->second.at("type");
    if (t != "circle" && t != "arc") return 0.0;
    return c == "min" ? -1.0 : 1.0;
}

// Distance constraint: point-point or point-line, with optional C50 cond (min/max
// rim, radius as a solver variable) and axis (h/v directional). Port of _add_distance.
void add_distance(System& sys, const std::string& cid, const std::string& h0,
                  const std::string& h1, double d, const EntMap& entities,
                  const VarMap& var_map, const std::string& axis,
                  const std::string& cond0, const std::string& cond1) {
    double s0 = distance_cond_sign(h0, cond0, entities);
    double s1 = distance_cond_sign(h1, cond1, entities);

    if (is_line_ref(h0, entities) || is_line_ref(h1, entities)) {
        bool h0_is_line = is_line_ref(h0, entities);
        std::string line_h = h0_is_line ? h0 : h1;
        std::string pt_h = h0_is_line ? h1 : h0;
        double s_pt = h0_is_line ? s1 : s0;
        std::vector<int> lidx = pick(var_map.at(eid_of(line_h)), {0, 1, 2, 3});
        Handle p = resolve_handle(pt_h, entities, var_map);
        int kp = static_cast<int>(p.idx.size());
        std::vector<int> combined = concat(p.idx, lidx);
        if (s_pt != 0.0) {
            std::vector<int> rpart = pick(var_map.at(pt_h), {2});
            combined = concat(combined, rpart);
        }
        ValFn pv = p.val;
        JacFn pj = p.jac;
        bool has_r = s_pt != 0.0;
        sys.add_residual(
            [pv, kp, d, s_pt, has_r](const VectorXd& xs) {
                Vector2d P = pv(vhead(xs, kp));
                double ax = xs(kp), ay = xs(kp + 1), bx = xs(kp + 2), by = xs(kp + 3);
                double adj = has_r ? s_pt * xs(kp + 4) : 0.0;
                double dxab = bx - ax, dyab = by - ay;
                double L = std::hypot(dxab, dyab);
                if (L < 1e-14) return r1(std::hypot(P(0) - ax, P(1) - ay) + adj - d);
                double cross = dxab * (P(1) - ay) - dyab * (P(0) - ax);
                return r1(std::abs(cross) / L + adj - d);
            },
            combined,
            [pv, pj, kp, s_pt, has_r](const VectorXd& xs) {
                Vector2d P = pv(vhead(xs, kp));
                MatrixXd Jp = pj(vhead(xs, kp));
                double ax = xs(kp), ay = xs(kp + 1), bx = xs(kp + 2), by = xs(kp + 3);
                double dxab = bx - ax, dyab = by - ay;
                double L2 = dxab * dxab + dyab * dyab;
                double L = L2 > 1e-28 ? std::sqrt(L2) : 1e-14;
                double cross = dxab * (P(1) - ay) - dyab * (P(0) - ax);
                double sgn = cross >= 0 ? 1.0 : -1.0;
                double Px = P(0), Py = P(1);
                Vector2d dc_dP(-dyab, dxab);
                MatrixXd J = MatrixXd::Zero(1, kp + 4 + (has_r ? 1 : 0));
                J.block(0, 0, 1, kp) = sgn * rowdot(dc_dP, Jp) / L;
                double dc_ax = by - Py, dc_ay = Px - bx, dc_bx = Py - ay, dc_by = ax - Px;
                double dL_ax = -dxab / L, dL_ay = -dyab / L, dL_bx = dxab / L, dL_by = dyab / L;
                auto dd = [&](double dc, double dl) {
                    return (sgn * dc * L - std::abs(cross) * dl) / L2;
                };
                J(0, kp) = dd(dc_ax, dL_ax);
                J(0, kp + 1) = dd(dc_ay, dL_ay);
                J(0, kp + 2) = dd(dc_bx, dL_bx);
                J(0, kp + 3) = dd(dc_by, dL_by);
                if (has_r) J(0, kp + 4) = s_pt;
                return J;
            },
            cid);
        return;
    }

    // point-point distance (a bare circle/arc ref resolves to its center).
    Handle a = resolve_handle(h0, entities, var_map);
    Handle b = resolve_handle(h1, entities, var_map);
    int k0 = static_cast<int>(a.idx.size()), k1 = static_cast<int>(b.idx.size());
    int kb = k0 + k1;
    std::vector<int> combined = concat(a.idx, b.idx);
    int r0col = -1, r1col = -1;
    int nextra = 0;
    if (s0 != 0.0) {
        r0col = kb + nextra;
        combined = concat(combined, pick(var_map.at(h0), {2}));
        ++nextra;
    }
    if (s1 != 0.0) {
        r1col = kb + nextra;
        combined = concat(combined, pick(var_map.at(h1), {2}));
        ++nextra;
    }
    int ncols = kb + nextra;
    ValFn av = a.val, bv = b.val;
    JacFn aj = a.jac, bj = b.jac;

    std::string ax = axis;
    std::transform(ax.begin(), ax.end(), ax.begin(),
                   [](unsigned char ch) { return std::tolower(ch); });

    if (ax == "h" || ax == "v") {
        int comp = (ax == "h") ? 0 : 1;
        // Axis-aligned gap: residual = |Delta_comp| + Sum(s*r) - d. The rim
        // adjustment is applied OUTSIDE the abs (a scalar offset on the gap),
        // matching the aligned (no-axis) case and the Python oracle
        // (_add_distance). Applying it per-point inside the abs would give a
        // different residual whenever a radius variable is free.
        sys.add_residual(
            [av, bv, k0, k1, comp, r0col, r1col, s0, s1, d](const VectorXd& xs) {
                Vector2d P0 = av(vhead(xs, k0));
                Vector2d P1 = bv(vseg(xs, k0, k1));
                double adj = 0.0;
                if (r0col >= 0) adj += s0 * xs(r0col);
                if (r1col >= 0) adj += s1 * xs(r1col);
                return r1(std::abs(P0(comp) - P1(comp)) + adj - d);
            },
            combined,
            [av, bv, aj, bj, k0, k1, kb, comp, r0col, r1col, s0, s1, ncols](
                const VectorXd& xs) {
                Vector2d P0 = av(vhead(xs, k0));
                Vector2d P1 = bv(vseg(xs, k0, k1));
                MatrixXd J0 = aj(vhead(xs, k0));
                MatrixXd J1 = bj(vseg(xs, k0, k1));
                double delta = P0(comp) - P1(comp);
                double sgn = delta >= 0 ? 1.0 : -1.0;
                MatrixXd J = MatrixXd::Zero(1, ncols);
                if (k0) J.block(0, 0, 1, k0) = sgn * J0.row(comp);
                if (k1) J.block(0, k0, 1, k1) = -sgn * J1.row(comp);
                if (r0col >= 0) J(0, r0col) = s0;
                if (r1col >= 0) J(0, r1col) = s1;
                return J;
            },
            cid);
        return;
    }

    // Aligned (no axis): centre-line distance; each rim adds to the scalar gap.
    sys.add_residual(
        [av, bv, k0, k1, r0col, r1col, s0, s1, d](const VectorXd& xs) {
            Vector2d P0 = av(vhead(xs, k0));
            Vector2d P1 = bv(vseg(xs, k0, k1));
            double adj = 0.0;
            if (r0col >= 0) adj += s0 * xs(r0col);
            if (r1col >= 0) adj += s1 * xs(r1col);
            return r1(std::hypot(P0(0) - P1(0), P0(1) - P1(1)) + adj - d);
        },
        combined,
        [av, bv, aj, bj, k0, k1, kb, r0col, r1col, s0, s1, ncols](const VectorXd& xs) {
            Vector2d P0 = av(vhead(xs, k0));
            Vector2d P1 = bv(vseg(xs, k0, k1));
            MatrixXd J0 = aj(vhead(xs, k0));
            MatrixXd J1 = bj(vseg(xs, k0, k1));
            double dx = P0(0) - P1(0), dy = P0(1) - P1(1);
            double dd = std::hypot(dx, dy);
            MatrixXd J = MatrixXd::Zero(1, ncols);
            if (dd < 1e-12) {
                if (r0col >= 0) J(0, r0col) = s0;
                if (r1col >= 0) J(0, r1col) = s1;
                return J;
            }
            Vector2d dir(dx / dd, dy / dd);
            if (k0) J.block(0, 0, 1, k0) = rowdot(dir, J0);
            if (k1) J.block(0, k0, 1, k1) = -rowdot(dir, J1);
            if (r0col >= 0) J(0, r0col) = s0;
            if (r1col >= 0) J(0, r1col) = s1;
            return J;
        },
        cid);
}

void add_tangent_line_circle(System& sys, const std::string& cid,
                             const std::string& line_eid, const std::string& circ_eid,
                             const VarMap& var_map) {
    std::vector<int> lidx = pick(var_map.at(line_eid), {0, 1, 2, 3});
    std::vector<int> cidx = pick(var_map.at(circ_eid), {0, 1, 2});
    std::vector<int> combined = concat(lidx, cidx);
    sys.add_residual(
        [](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double cx = xs(4), cy = xs(5), r = xs(6);
            double dxab = bx - ax, dyab = by - ay;
            double L = std::hypot(dxab, dyab);
            if (L < 1e-14) return r1(r);
            double cross = dxab * (ay - cy) - dyab * (ax - cx);
            return r1(std::abs(cross) / L - r);
        },
        combined,
        [](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double cx = xs(4), cy = xs(5);
            double dxab = bx - ax, dyab = by - ay;
            double L2 = dxab * dxab + dyab * dyab;
            double L = L2 > 1e-28 ? std::sqrt(L2) : 1e-14;
            double cross = dxab * (ay - cy) - dyab * (ax - cx);
            double sgn = cross >= 0 ? 1.0 : -1.0;
            // d(cross)/d[ax,ay,bx,by,cx,cy] (expanded form from the Python).
            double dc_ax = cy - by, dc_ay = bx - cx, dc_bx = ay - cy, dc_by = cx - ax;
            double dc_cx = by - ay, dc_cy = ax - bx;
            double dL_ax = -dxab / L, dL_ay = -dyab / L, dL_bx = dxab / L, dL_by = dyab / L;
            auto dd = [&](double dc, double dl) {
                return (sgn * dc * L - std::abs(cross) * dl) / L2;
            };
            MatrixXd J(1, 7);
            J(0, 0) = dd(dc_ax, dL_ax);
            J(0, 1) = dd(dc_ay, dL_ay);
            J(0, 2) = dd(dc_bx, dL_bx);
            J(0, 3) = dd(dc_by, dL_by);
            J(0, 4) = sgn * dc_cx / L;
            J(0, 5) = sgn * dc_cy / L;
            J(0, 6) = -1.0;
            return J;
        },
        cid);
}

void add_point_on(System& sys, const std::string& cid, const std::string& point_h,
                  const std::string& entity_h, const EntMap& entities,
                  const VarMap& var_map) {
    Handle p = resolve_handle(point_h, entities, var_map);
    std::string eid = eid_of(entity_h);
    const std::string t = entities.at(eid).at("type");
    int kp = static_cast<int>(p.idx.size());
    ValFn pv = p.val;
    JacFn pj = p.jac;
    if (t == "line") {
        std::vector<int> lidx = pick(var_map.at(eid), {0, 1, 2, 3});
        std::vector<int> combined = concat(p.idx, lidx);
        sys.add_residual(
            [pv, kp](const VectorXd& xs) {
                Vector2d P = pv(vhead(xs, kp));
                double ax = xs(kp), ay = xs(kp + 1), bx = xs(kp + 2), by = xs(kp + 3);
                return r1((bx - ax) * (P(1) - ay) - (by - ay) * (P(0) - ax));
            },
            combined,
            [pv, pj, kp](const VectorXd& xs) {
                Vector2d P = pv(vhead(xs, kp));
                MatrixXd Jp = pj(vhead(xs, kp));
                double ax = xs(kp), ay = xs(kp + 1), bx = xs(kp + 2), by = xs(kp + 3);
                double dxab = bx - ax, dyab = by - ay, Px = P(0), Py = P(1);
                Vector2d dc_dP(-dyab, dxab);
                MatrixXd J = MatrixXd::Zero(1, kp + 4);
                J.block(0, 0, 1, kp) = rowdot(dc_dP, Jp);
                J(0, kp) = -(Py - ay) + dyab;
                J(0, kp + 1) = (Px - ax) - dxab;
                J(0, kp + 2) = Py - ay;
                J(0, kp + 3) = -(Px - ax);
                return J;
            },
            cid);
        return;
    }
    if (t == "circle" || t == "arc") {
        std::vector<int> cidx = pick(var_map.at(eid), {0, 1, 2});
        std::vector<int> combined = concat(p.idx, cidx);
        sys.add_residual(
            [pv, kp](const VectorXd& xs) {
                Vector2d P = pv(vhead(xs, kp));
                double cx = xs(kp), cy = xs(kp + 1), r = xs(kp + 2);
                return r1(std::hypot(P(0) - cx, P(1) - cy) - r);
            },
            combined,
            [pv, pj, kp](const VectorXd& xs) {
                Vector2d P = pv(vhead(xs, kp));
                MatrixXd Jp = pj(vhead(xs, kp));
                double cx = xs(kp), cy = xs(kp + 1);
                double dx = P(0) - cx, dy = P(1) - cy, dd = std::hypot(dx, dy);
                MatrixXd J = MatrixXd::Zero(1, kp + 3);
                if (dd < 1e-12) return J;
                Vector2d dir(dx / dd, dy / dd);
                J.block(0, 0, 1, kp) = rowdot(dir, Jp);
                J(0, kp) = -dx / dd;
                J(0, kp + 1) = -dy / dd;
                J(0, kp + 2) = -1.0;
                return J;
            },
            cid);
    }
}

void add_fix(System& sys, const std::string& cid, const std::string& handle,
             const EntMap& entities, const VarMap& var_map) {
    std::string eid = eid_of(handle);
    if (eid == "origin" && !entities.count(eid)) return;
    const json& ent = entities.at(eid);
    const std::string t = ent.at("type");
    std::string sub;
    auto dot = handle.find('.');
    if (dot != std::string::npos) sub = handle.substr(dot + 1);

    auto pin = [&](const std::vector<int>& idx, const VectorXd& tgt) {
        int k = static_cast<int>(idx.size());
        VectorXd t_copy = tgt;
        sys.add_residual(
            [t_copy, k](const VectorXd& xs) { return VectorXd(xs - t_copy); }, idx,
            [k](const VectorXd&) { return MatrixXd(MatrixXd::Identity(k, k)); }, cid);
    };

    if (t == "point") {
        VectorXd tgt(2);
        tgt << num(ent["p"][0]), num(ent["p"][1]);
        pin(var_map.at(eid), tgt);
    } else if (t == "line") {
        if (sub.empty()) {
            VectorXd tgt(4);
            tgt << num(ent["p1"][0]), num(ent["p1"][1]), num(ent["p2"][0]), num(ent["p2"][1]);
            pin(pick(var_map.at(eid), {0, 1, 2, 3}), tgt);
        } else if (sub == "p1") {
            VectorXd tgt(2);
            tgt << num(ent["p1"][0]), num(ent["p1"][1]);
            pin(pick(var_map.at(eid), {0, 1}), tgt);
        } else if (sub == "p2") {
            VectorXd tgt(2);
            tgt << num(ent["p2"][0]), num(ent["p2"][1]);
            pin(pick(var_map.at(eid), {2, 3}), tgt);
        }
    } else if (t == "circle") {
        if (sub.empty()) {
            VectorXd tgt(3);
            tgt << num(ent["center"][0]), num(ent["center"][1]), num(ent["radius"]);
            pin(pick(var_map.at(eid), {0, 1, 2}), tgt);
        } else if (sub == "center") {
            VectorXd tgt(2);
            tgt << num(ent["center"][0]), num(ent["center"][1]);
            pin(pick(var_map.at(eid), {0, 1}), tgt);
        }
    } else if (t == "arc") {
        if (sub.empty()) {
            VectorXd tgt(5);
            tgt << num(ent["center"][0]), num(ent["center"][1]), num(ent["radius"]),
                num(ent["start"]), num(ent["end"]);
            pin(pick(var_map.at(eid), {0, 1, 2, 3, 4}), tgt);
        } else if (sub == "center") {
            VectorXd tgt(2);
            tgt << num(ent["center"][0]), num(ent["center"][1]);
            pin(pick(var_map.at(eid), {0, 1}), tgt);
        }
    } else if (t == "ellipse") {
        if (sub.empty() || sub == "center") {
            VectorXd tgt(2);
            tgt << num(ent["center"][0]), num(ent["center"][1]);
            pin(pick(var_map.at(eid), {0, 1}), tgt);
        }
    } else if (t == "slot") {
        if (sub.empty() || sub == "p1") {
            VectorXd tgt(2);
            tgt << num(ent["p1"][0]), num(ent["p1"][1]);
            pin(pick(var_map.at(eid), {0, 1}), tgt);
        } else if (sub == "p2") {
            VectorXd tgt(2);
            tgt << num(ent["p2"][0]), num(ent["p2"][1]);
            pin(pick(var_map.at(eid), {2, 3}), tgt);
        }
    } else if (t == "spline" || t == "style_spline") {
        const json& pts = ent["points"];
        if (sub.empty()) {
            int n = static_cast<int>(pts.size()) * 2;
            VectorXd tgt(n);
            for (int i = 0; i < static_cast<int>(pts.size()); ++i) {
                tgt(2 * i) = num(pts[i][0]);
                tgt(2 * i + 1) = num(pts[i][1]);
            }
            pin(var_map.at(eid), tgt);
        } else if (!sub.empty() && sub[0] == 'p') {
            int k = std::stoi(sub.substr(1));
            if (k >= 0 && k < static_cast<int>(pts.size())) {
                VectorXd tgt(2);
                tgt << num(pts[k][0]), num(pts[k][1]);
                pin(pick(var_map.at(eid), {2 * k, 2 * k + 1}), tgt);
            }
        }
    } else if (t == "ellipse_arc" || t == "parabola" || t == "conic" || t == "arc_slot") {
        // Generic fix on any resolvable handle: pin the derived point to its
        // current world position (computed from the entity's init slice). Port of
        // _add_fix's conic-family branch.
        Handle h = resolve_handle(handle, entities, var_map);
        int base = var_map.at(eid)[0];
        std::vector<double> cur = entity_init_slice(ent);
        VectorXd seed(static_cast<int>(h.idx.size()));
        for (int i = 0; i < static_cast<int>(h.idx.size()); ++i)
            seed(i) = cur[h.idx[i] - base];
        Vector2d tgt = h.val(seed);
        ValFn hv = h.val;
        JacFn hj = h.jac;
        sys.add_residual(
            [hv, tgt](const VectorXd& xs) {
                Vector2d P = hv(xs);
                return r1Vec2(P - tgt);
            },
            h.idx, [hj](const VectorXd& xs) { return hj(xs); }, cid);
    }
}

// Two lines' direction vars [ax,ay,bx,by] for parallel/perpendicular/collinear.
void add_parallel(System& sys, const std::string& cid, const std::string& h0,
                  const std::string& h1, const VarMap& var_map) {
    std::vector<int> combined =
        concat(pick(var_map.at(eid_of(h0)), {0, 1, 2, 3}),
               pick(var_map.at(eid_of(h1)), {0, 1, 2, 3}));
    sys.add_residual(
        [](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double cx = xs(4), cy = xs(5), dx = xs(6), dy = xs(7);
            return r1((bx - ax) * (dy - cy) - (by - ay) * (dx - cx));
        },
        combined,
        [](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double cx = xs(4), cy = xs(5), dx = xs(6), dy = xs(7);
            MatrixXd J(1, 8);
            J << -(dy - cy), (dx - cx), (dy - cy), -(dx - cx), (by - ay), -(bx - ax),
                -(by - ay), (bx - ax);
            return J;
        },
        cid);
}

void add_perpendicular(System& sys, const std::string& cid, const std::string& h0,
                       const std::string& h1, const VarMap& var_map) {
    std::vector<int> combined =
        concat(pick(var_map.at(eid_of(h0)), {0, 1, 2, 3}),
               pick(var_map.at(eid_of(h1)), {0, 1, 2, 3}));
    sys.add_residual(
        [](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double cx = xs(4), cy = xs(5), dx = xs(6), dy = xs(7);
            return r1((bx - ax) * (dx - cx) + (by - ay) * (dy - cy));
        },
        combined,
        [](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double cx = xs(4), cy = xs(5), dx = xs(6), dy = xs(7);
            MatrixXd J(1, 8);
            J << -(dx - cx), -(dy - cy), (dx - cx), (dy - cy), -(bx - ax), -(by - ay),
                (bx - ax), (by - ay);
            return J;
        },
        cid);
}

void add_equal(System& sys, const std::string& cid, const std::string& h0,
               const std::string& h1, const EntMap& entities, const VarMap& var_map) {
    std::string e0 = eid_of(h0), e1 = eid_of(h1);
    std::string t0 = entities.at(e0).at("type"), t1 = entities.at(e1).at("type");
    // line-line OR slot-slot: equal SPINE length |p2-p1| (both store the spine
    // endpoints in vars [0..3]). Port of _add_equal / _add_equal_length_4var.
    if ((t0 == "line" && t1 == "line") || (t0 == "slot" && t1 == "slot")) {
        std::vector<int> combined = concat(pick(var_map.at(e0), {0, 1, 2, 3}),
                                           pick(var_map.at(e1), {0, 1, 2, 3}));
        sys.add_residual(
            [](const VectorXd& xs) {
                double L0 = std::hypot(xs(2) - xs(0), xs(3) - xs(1));
                double L1 = std::hypot(xs(6) - xs(4), xs(7) - xs(5));
                return r1(L0 - L1);
            },
            combined,
            [](const VectorXd& xs) {
                double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
                double cx = xs(4), cy = xs(5), dx = xs(6), dy = xs(7);
                double L0 = std::max(std::hypot(bx - ax, by - ay), 1e-12);
                double L1 = std::max(std::hypot(dx - cx, dy - cy), 1e-12);
                MatrixXd J(1, 8);
                J << -(bx - ax) / L0, -(by - ay) / L0, (bx - ax) / L0, (by - ay) / L0,
                    (dx - cx) / L1, (dy - cy) / L1, -(dx - cx) / L1, -(dy - cy) / L1;
                return J;
            },
            cid);
    } else if ((t0 == "circle" || t0 == "arc") && (t1 == "circle" || t1 == "arc")) {
        std::vector<int> combined = concat(pick(var_map.at(e0), {2}), pick(var_map.at(e1), {2}));
        sys.add_residual([](const VectorXd& xs) { return r1(xs(0) - xs(1)); }, combined,
                         [](const VectorXd&) {
                             MatrixXd j(1, 2);
                             j << 1.0, -1.0;
                             return j;
                         },
                         cid);
    }
}

void add_midpoint(System& sys, const std::string& cid, const std::string& point_h,
                  const std::string& line_h, const EntMap& entities,
                  const VarMap& var_map) {
    Handle p = resolve_handle(point_h, entities, var_map);
    std::vector<int> lidx = pick(var_map.at(eid_of(line_h)), {0, 1, 2, 3});
    int kp = static_cast<int>(p.idx.size());
    std::vector<int> combined = concat(p.idx, lidx);
    ValFn pv = p.val;
    JacFn pj = p.jac;
    sys.add_residual(
        [pv, kp](const VectorXd& xs) {
            Vector2d P = pv(vhead(xs, kp));
            double ax = xs(kp), ay = xs(kp + 1), bx = xs(kp + 2), by = xs(kp + 3);
            VectorXd r(2);
            r << P(0) - (ax + bx) * 0.5, P(1) - (ay + by) * 0.5;
            return r;
        },
        combined,
        [pj, kp](const VectorXd& xs) {
            MatrixXd Jp = pj(vhead(xs, kp));
            MatrixXd J(2, kp + 4);
            MatrixXd Jl(2, 4);
            Jl << -0.5, 0.0, -0.5, 0.0, 0.0, -0.5, 0.0, -0.5;
            J << Jp, Jl;
            return J;
        },
        cid);
}

// Add a coincident-style 2-point residual: f(P0,P1) selecting components.
// kind: 0 = both (coincident), 1 = y only (horizontal), 2 = x only (vertical).
void add_two_point(System& sys, const std::string& cid, const std::string& h0,
                   const std::string& h1, int kind, const EntMap& entities,
                   const VarMap& var_map) {
    Handle a = resolve_handle(h0, entities, var_map);
    Handle b = resolve_handle(h1, entities, var_map);
    int k0 = static_cast<int>(a.idx.size()), k1 = static_cast<int>(b.idx.size());
    std::vector<int> combined = concat(a.idx, b.idx);
    ValFn av = a.val, bv = b.val;
    JacFn aj = a.jac, bj = b.jac;
    sys.add_residual(
        [av, bv, k0, k1, kind](const VectorXd& xs) {
            Vector2d P0 = av(vhead(xs, k0));
            Vector2d P1 = bv(vseg(xs, k0, k1));
            if (kind == 0) {
                VectorXd r(2);
                r << P0(0) - P1(0), P0(1) - P1(1);
                return r;
            }
            int c = (kind == 1) ? 1 : 0;  // horizontal -> y, vertical -> x
            return r1(P0(c) - P1(c));
        },
        combined,
        [aj, bj, k0, k1, kind](const VectorXd& xs) {
            MatrixXd J0 = aj(vhead(xs, k0));
            MatrixXd J1 = bj(vseg(xs, k0, k1));
            if (kind == 0) {
                MatrixXd J = MatrixXd::Zero(2, k0 + k1);
                if (k0) J.block(0, 0, 2, k0) = J0;
                J.block(0, k0, 2, k1) = -J1;
                return J;
            }
            int c = (kind == 1) ? 1 : 0;
            MatrixXd J = MatrixXd::Zero(1, k0 + k1);
            if (k0) J.block(0, 0, 1, k0) = J0.row(c);
            J.block(0, k0, 1, k1) = -J1.row(c);
            return J;
        },
        cid);
}

void add_radius_like(System& sys, const std::string& cid, const std::string& ref,
                     double r, const EntMap& entities, const VarMap& var_map) {
    std::string eid = eid_of(ref);
    const std::string t = entities.at(eid).at("type");
    if (t == "circle" || t == "arc") {
        std::vector<int> idx = pick(var_map.at(eid), {2});
        sys.add_residual([r](const VectorXd& xs) { return r1(xs(0) - r); }, idx,
                         [](const VectorXd&) {
                             MatrixXd j(1, 1);
                             j << 1.0;
                             return j;
                         },
                         cid);
    }
}

void add_whole_line_hv(System& sys, const std::string& cid, const std::string& eid,
                       bool horizontal, const VarMap& var_map) {
    // horizontal: p1y==p2y (vars [1,3]); vertical: p1x==p2x (vars [0,2]).
    std::vector<int> idx = horizontal ? pick(var_map.at(eid), {1, 3})
                                      : pick(var_map.at(eid), {0, 2});
    sys.add_residual([](const VectorXd& xs) { return r1(xs(0) - xs(1)); }, idx,
                     [](const VectorXd&) {
                         MatrixXd j(1, 2);
                         j << 1.0, -1.0;
                         return j;
                     },
                     cid);
}

void add_concentric(System& sys, const std::string& cid, const std::string& h0,
                    const std::string& h1, const VarMap& var_map) {
    std::vector<int> combined = concat(pick(var_map.at(eid_of(h0)), {0, 1}),
                                       pick(var_map.at(eid_of(h1)), {0, 1}));
    sys.add_residual(
        [](const VectorXd& xs) {
            VectorXd r(2);
            r << xs(0) - xs(2), xs(1) - xs(3);
            return r;
        },
        combined,
        [](const VectorXd&) {
            MatrixXd j(2, 4);
            j << 1, 0, -1, 0, 0, 1, 0, -1;
            return j;
        },
        cid);
}

// Angle between two lines (unsigned, atan2(|cross|, dot)). Port of _add_angle.
void add_angle(System& sys, const std::string& cid, const std::string& h0,
               const std::string& h1, double ang_deg, const VarMap& var_map) {
    std::vector<int> combined = concat(pick(var_map.at(eid_of(h0)), {0, 1, 2, 3}),
                                       pick(var_map.at(eid_of(h1)), {0, 1, 2, 3}));
    double ang_rad = ang_deg * kDeg;
    sys.add_residual(
        [ang_rad](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double cx = xs(4), cy = xs(5), dx = xs(6), dy = xs(7);
            double ux = bx - ax, uy = by - ay;
            double vx = dx - cx, vy = dy - cy;
            double cross = ux * vy - uy * vx;
            double dot = ux * vx + uy * vy;
            double theta = std::atan2(std::abs(cross), dot);
            return r1(theta - ang_rad);
        },
        combined,
        [](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double cx = xs(4), cy = xs(5), dx = xs(6), dy = xs(7);
            double ux = bx - ax, uy = by - ay;
            double vx = dx - cx, vy = dy - cy;
            double cross = ux * vy - uy * vx;
            double dot = ux * vx + uy * vy;
            double abs_cross = std::abs(cross);
            double sgn_cross = cross >= 0 ? 1.0 : -1.0;
            double denom = dot * dot + abs_cross * abs_cross;
            if (denom < 1e-24) return MatrixXd(MatrixXd::Zero(1, 8));
            double dc_dux = vy, dc_duy = -vx, dc_dvx = -uy, dc_dvy = ux;
            double dd_dux = vx, dd_duy = vy, dd_dvx = ux, dd_dvy = uy;
            auto dtheta = [&](double dc_d_var, double dd_d_var) {
                return (dot * sgn_cross * dc_d_var - abs_cross * dd_d_var) / denom;
            };
            double dt_dux = dtheta(dc_dux, dd_dux);
            double dt_duy = dtheta(dc_duy, dd_duy);
            double dt_dvx = dtheta(dc_dvx, dd_dvx);
            double dt_dvy = dtheta(dc_dvy, dd_dvy);
            MatrixXd J(1, 8);
            J << -dt_dux, -dt_duy, dt_dux, dt_duy, -dt_dvx, -dt_dvy, dt_dvx, dt_dvy;
            return J;
        },
        cid);
}

// Two lines collinear: parallel + endpoint C of line B on line A (2 residuals).
// Port of _add_collinear.
void add_collinear(System& sys, const std::string& cid, const std::string& h0,
                   const std::string& h1, const VarMap& var_map) {
    std::vector<int> combined = concat(pick(var_map.at(eid_of(h0)), {0, 1, 2, 3}),
                                       pick(var_map.at(eid_of(h1)), {0, 1, 2, 3}));
    sys.add_residual(
        [](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double cx = xs(4), cy = xs(5), dx = xs(6), dy = xs(7);
            VectorXd r(2);
            r << (bx - ax) * (dy - cy) - (by - ay) * (dx - cx),
                 (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
            return r;
        },
        combined,
        [](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double cx = xs(4), cy = xs(5), dx = xs(6), dy = xs(7);
            MatrixXd J(2, 8);
            // r1 = (bx-ax)(dy-cy) - (by-ay)(dx-cx)
            J(0, 0) = -(dy - cy);
            J(0, 1) = (dx - cx);
            J(0, 2) = (dy - cy);
            J(0, 3) = -(dx - cx);
            J(0, 4) = (by - ay);
            J(0, 5) = -(bx - ax);
            J(0, 6) = -(by - ay);
            J(0, 7) = (bx - ax);
            // r2 = (bx-ax)(cy-ay) - (by-ay)(cx-ax)
            J(1, 0) = -(cy - ay) + (by - ay);
            J(1, 1) = -(bx - ax) + (cx - ax);
            J(1, 2) = (cy - ay);
            J(1, 3) = -(cx - ax);
            J(1, 4) = -(by - ay);
            J(1, 5) = (bx - ax);
            J(1, 6) = 0.0;
            J(1, 7) = 0.0;
            return J;
        },
        cid);
}

// Two circles/arcs share center AND radius (3 linear residuals). Port of _add_coradial.
void add_coradial(System& sys, const std::string& cid, const std::string& h0,
                  const std::string& h1, const VarMap& var_map) {
    std::vector<int> combined = concat(pick(var_map.at(eid_of(h0)), {0, 1, 2}),
                                       pick(var_map.at(eid_of(h1)), {0, 1, 2}));
    sys.add_residual(
        [](const VectorXd& xs) {
            VectorXd r(3);
            r << xs(0) - xs(3), xs(1) - xs(4), xs(2) - xs(5);
            return r;
        },
        combined,
        [](const VectorXd&) {
            MatrixXd j(3, 6);
            j << 1.0, 0.0, 0.0, -1.0, 0.0, 0.0,
                 0.0, 1.0, 0.0, 0.0, -1.0, 0.0,
                 0.0, 0.0, 1.0, 0.0, 0.0, -1.0;
            return j;
        },
        cid);
}

// circle/arc <-> circle/arc tangency: |C0-C1| = r0+r1 (external) or |r0-r1|
// (internal). Branch frozen at build time from the as-drawn geometry. Port of
// _add_tangent_circle_circle.
void add_tangent_circle_circle(System& sys, const std::string& cid,
                               const std::string& eid0, const std::string& eid1,
                               const EntMap& entities, const VarMap& var_map) {
    std::vector<int> combined = concat(pick(var_map.at(eid0), {0, 1, 2}),
                                       pick(var_map.at(eid1), {0, 1, 2}));
    const json& e0 = entities.at(eid0);
    const json& e1 = entities.at(eid1);
    double c0x = num(e0["center"][0]), c0y = num(e0["center"][1]);
    double c1x = num(e1["center"][0]), c1y = num(e1["center"][1]);
    double r0_0 = num(e0["radius"]), r1_0 = num(e1["radius"]);
    double d0 = std::hypot(c0x - c1x, c0y - c1y);
    bool internal = std::abs(d0 - std::abs(r0_0 - r1_0)) < std::abs(d0 - (r0_0 + r1_0));
    sys.add_residual(
        [internal](const VectorXd& xs) {
            double cx0 = xs(0), cy0 = xs(1), r0 = xs(2);
            double cx1 = xs(3), cy1 = xs(4), r1 = xs(5);
            double d = std::hypot(cx0 - cx1, cy0 - cy1);
            double target = internal ? std::abs(r0 - r1) : (r0 + r1);
            return r1Helper(d - target);
        },
        combined,
        [internal](const VectorXd& xs) {
            double cx0 = xs(0), cy0 = xs(1), r0 = xs(2);
            double cx1 = xs(3), cy1 = xs(4), r1 = xs(5);
            double dx = cx0 - cx1, dy = cy0 - cy1;
            double d = std::hypot(dx, dy);
            // BUG-1: at coincident centers d(|C0-C1|)/dC is 0/0. A zero position
            // gradient strands Newton (it cannot separate the centers and
            // corrupts the radii instead). Fall back to a DETERMINISTIC unit
            // direction (+x) so the Jacobian is well-defined and the centers get
            // pushed apart to the tangent distance. Matches the Python fallback
            // (1,0) verbatim -> bit-identical under the differential harness.
            double ux = 1.0, uy = 0.0;
            if (d >= 1e-12) { ux = dx / d; uy = dy / d; }
            double dr0, dr1;
            if (internal) {
                double s = (r0 - r1) >= 0 ? 1.0 : -1.0;
                dr0 = -s; dr1 = s;
            } else {
                dr0 = -1.0; dr1 = -1.0;
            }
            MatrixXd J(1, 6);
            J << ux, uy, dr0, -ux, -uy, dr1;
            return J;
        },
        cid);
}

// Points h0 and h1 symmetric about an axis line (2 residuals: midpoint on axis +
// (P-Q) perpendicular to axis). Port of _add_symmetric.
void add_symmetric(System& sys, const std::string& cid, const std::string& h0,
                   const std::string& h1, const std::string& axis_h,
                   const EntMap& entities, const VarMap& var_map) {
    Handle a = resolve_handle(h0, entities, var_map);
    Handle b = resolve_handle(h1, entities, var_map);
    std::vector<int> aidx = pick(var_map.at(eid_of(axis_h)), {0, 1, 2, 3});
    int k0 = static_cast<int>(a.idx.size()), k1 = static_cast<int>(b.idx.size());
    std::vector<int> combined = concat(concat(a.idx, b.idx), aidx);
    ValFn av = a.val, bv = b.val;
    JacFn aj = a.jac, bj = b.jac;
    sys.add_residual(
        [av, bv, k0, k1](const VectorXd& xs) {
            Vector2d P = av(vhead(xs, k0));
            Vector2d Q = bv(vseg(xs, k0, k1));
            double ax = xs(k0 + k1), ay = xs(k0 + k1 + 1);
            double bx = xs(k0 + k1 + 2), by = xs(k0 + k1 + 3);
            double Mx = (P(0) + Q(0)) * 0.5, My = (P(1) + Q(1)) * 0.5;
            double dxab = bx - ax, dyab = by - ay;
            double c1 = dxab * (My - ay) - dyab * (Mx - ax);
            double c2 = (P(0) - Q(0)) * dxab + (P(1) - Q(1)) * dyab;
            VectorXd r(2);
            r << c1, c2;
            return r;
        },
        combined,
        [av, bv, aj, bj, k0, k1](const VectorXd& xs) {
            Vector2d P = av(vhead(xs, k0));
            Vector2d Q = bv(vseg(xs, k0, k1));
            MatrixXd Jp0 = aj(vhead(xs, k0));
            MatrixXd Jq0 = bj(vseg(xs, k0, k1));
            double ax = xs(k0 + k1), ay = xs(k0 + k1 + 1);
            double bx = xs(k0 + k1 + 2), by = xs(k0 + k1 + 3);
            double dxab = bx - ax, dyab = by - ay;
            double Mx = (P(0) + Q(0)) * 0.5, My = (P(1) + Q(1)) * 0.5;
            Vector2d dc1_dP(-dyab * 0.5, dxab * 0.5);
            Vector2d dc1_dQ(-dyab * 0.5, dxab * 0.5);
            Vector2d dc2_dP(dxab, dyab);
            Vector2d dc2_dQ(-dxab, -dyab);
            Eigen::RowVector4d dc1_da(-(My - ay) + (by - ay), -(bx - ax) + (Mx - ax),
                                      My - ay, -(Mx - ax));
            Eigen::RowVector4d dc2_da(-(P(0) - Q(0)), -(P(1) - Q(1)), P(0) - Q(0),
                                      P(1) - Q(1));
            MatrixXd J = MatrixXd::Zero(2, k0 + k1 + 4);
            if (k0) J.block(0, 0, 1, k0) = rowdot(dc1_dP, Jp0);
            if (k1) J.block(0, k0, 1, k1) = rowdot(dc1_dQ, Jq0);
            J.block(0, k0 + k1, 1, 4) = dc1_da;
            if (k0) J.block(1, 0, 1, k0) = rowdot(dc2_dP, Jp0);
            if (k1) J.block(1, k0, 1, k1) = rowdot(dc2_dQ, Jq0);
            J.block(1, k0 + k1, 1, 4) = dc2_da;
            return J;
        },
        cid);
}

void add_constraint(System& sys, const json& c, const EntMap& entities,
                    const VarMap& var_map, const json& env) {
    const std::string ctype = c.at("type");
    std::vector<std::string> refs;
    if (c.contains("refs"))
        for (const auto& r : c["refs"]) refs.push_back(r.get<std::string>());
    const std::string cid = c.value("id", std::string());
    if (c.value("driven", false)) return;  // reference dims measured after solve

    if (ctype == "coincident") {
        add_two_point(sys, cid, refs[0], refs[1], 0, entities, var_map);
    } else if (ctype == "horizontal" || ctype == "vertical") {
        bool horiz = ctype == "horizontal";
        if (refs.size() >= 2) {
            add_two_point(sys, cid, refs[0], refs[1], horiz ? 1 : 2, entities, var_map);
        } else if (refs[0].find('.') == std::string::npos) {
            std::string eid = refs[0];
            std::string et = entities.at(eid).at("type");
            // line and slot both store [p1x,p1y,p2x,p2y,...]; whole-entity h/v
            // enforces p1y==p2y (h) or p1x==p2x (v).
            if (et == "line" || et == "slot")
                add_whole_line_hv(sys, cid, eid, horiz, var_map);
        }
    } else if (ctype == "parallel") {
        add_parallel(sys, cid, refs[0], refs[1], var_map);
    } else if (ctype == "perpendicular") {
        add_perpendicular(sys, cid, refs[0], refs[1], var_map);
    } else if (ctype == "concentric") {
        add_concentric(sys, cid, refs[0], refs[1], var_map);
    } else if (ctype == "collinear") {
        add_collinear(sys, cid, refs[0], refs[1], var_map);
    } else if (ctype == "coradial") {
        add_coradial(sys, cid, refs[0], refs[1], var_map);
    } else if (ctype == "equal") {
        add_equal(sys, cid, refs[0], refs[1], entities, var_map);
    } else if (ctype == "midpoint") {
        add_midpoint(sys, cid, refs[0], refs[1], entities, var_map);
    } else if (ctype == "symmetric") {
        add_symmetric(sys, cid, refs[0], refs[1], refs[2], entities, var_map);
    } else if (ctype == "angle") {
        add_angle(sys, cid, refs[0], refs[1], resolve_dim(c, env), var_map);
    } else if (ctype == "fix") {
        add_fix(sys, cid, refs[0], entities, var_map);
    } else if (ctype == "point_on") {
        add_point_on(sys, cid, refs[0], refs[1], entities, var_map);
    } else if (ctype == "tangent") {
        std::string e0 = eid_of(refs[0]), e1 = eid_of(refs[1]);
        std::string t0 = entities.at(e0).at("type"), t1 = entities.at(e1).at("type");
        bool curved0 = (t0 == "circle" || t0 == "arc");
        bool curved1 = (t1 == "circle" || t1 == "arc");
        if (t0 == "line" && curved1)
            add_tangent_line_circle(sys, cid, e0, e1, var_map);
        else if (curved0 && t1 == "line")
            add_tangent_line_circle(sys, cid, e1, e0, var_map);
        else if (curved0 && curved1)
            add_tangent_circle_circle(sys, cid, e0, e1, entities, var_map);
        // line-conic / curve-curve tangency deferred (no conic/spline entities here)
    } else if (ctype == "distance") {
        std::string axis;
        if (c.contains("axis") && c["axis"].is_string()) axis = c["axis"].get<std::string>();
        std::string cond0, cond1;
        if (c.contains("cond") && c["cond"].is_array()) {
            const json& cd = c["cond"];
            if (cd.size() > 0 && cd[0].is_string()) cond0 = cd[0].get<std::string>();
            if (cd.size() > 1 && cd[1].is_string()) cond1 = cd[1].get<std::string>();
        }
        add_distance(sys, cid, refs[0], refs[1], resolve_dim(c, env), entities, var_map,
                     axis, cond0, cond1);
    } else if (ctype == "radius" || ctype == "diameter") {
        std::string eid = eid_of(refs[0]);
        std::string et = entities.at(eid).at("type");
        double val = resolve_dim(c, env);
        if (et == "circle" || et == "arc") {
            // radius: r == val; diameter: r == val/2.
            double r = (ctype == "radius") ? val : val / 2.0;
            add_radius_like(sys, cid, refs[0], r, entities, var_map);
        } else if (et == "slot" || et == "arc_slot") {
            // width var: slot index 4, arc_slot index 5.
            int wpos = (et == "slot") ? 4 : 5;
            std::vector<int> widx = pick(var_map.at(eid), {wpos});
            // radius: cap radius = w/2 -> w == 2*val; diameter: width == val.
            double target = (ctype == "radius") ? 2.0 * val : val;
            sys.add_residual(
                [target](const VectorXd& xs) { return r1(xs(0) - target); }, widx,
                [](const VectorXd&) {
                    MatrixXd j(1, 1);
                    j << 1.0;
                    return j;
                },
                cid);
        }
    }
    // unknown -> skip (forward-tolerant)
}

// ---- result emission --------------------------------------------------------

json extract_entities(const VectorXd& x, const json& entities_list,
                      const VarMap& var_map) {
    json out = json::object();
    for (const auto& e : entities_list) {
        std::string id = e["id"];
        const std::string t = e["type"];
        const std::vector<int>& v = var_map.at(id);
        json o;
        o["type"] = t;
        if (t == "point") {
            o["p"] = {x(v[0]), x(v[1])};
        } else if (t == "line") {
            o["p1"] = {x(v[0]), x(v[1])};
            o["p2"] = {x(v[2]), x(v[3])};
        } else if (t == "circle") {
            o["center"] = {x(v[0]), x(v[1])};
            o["radius"] = x(v[2]);
        } else if (t == "arc") {
            o["center"] = {x(v[0]), x(v[1])};
            o["radius"] = x(v[2]);
            o["start"] = x(v[3]);
            o["end"] = x(v[4]);
        } else if (t == "ellipse") {
            o["center"] = {x(v[0]), x(v[1])};
            o["rx"] = x(v[2]);
            o["ry"] = x(v[3]);
            o["rot"] = x(v[4]);
        } else if (t == "ellipse_arc") {
            o["center"] = {x(v[0]), x(v[1])};
            o["rx"] = x(v[2]);
            o["ry"] = x(v[3]);
            o["rot"] = x(v[4]);
            o["a0"] = x(v[5]);
            o["a1"] = x(v[6]);
        } else if (t == "parabola") {
            o["vertex"] = {x(v[0]), x(v[1])};
            o["focus"] = {x(v[2]), x(v[3])};
            o["t0"] = x(v[4]);
            o["t1"] = x(v[5]);
        } else if (t == "conic") {
            o["p0"] = {x(v[0]), x(v[1])};
            o["p1"] = {x(v[2]), x(v[3])};
            o["apex"] = {x(v[4]), x(v[5])};
            o["rho"] = x(v[6]);
        } else if (t == "slot") {
            o["p1"] = {x(v[0]), x(v[1])};
            o["p2"] = {x(v[2]), x(v[3])};
            o["width"] = x(v[4]);
        } else if (t == "arc_slot") {
            o["center"] = {x(v[0]), x(v[1])};
            o["radius"] = x(v[2]);
            o["a0"] = x(v[3]);
            o["a1"] = x(v[4]);
            o["width"] = x(v[5]);
        } else if (t == "spline" || t == "style_spline") {
            int n = static_cast<int>(v.size()) / 2;
            json pts = json::array();
            for (int i = 0; i < n; ++i) pts.push_back({x(v[2 * i]), x(v[2 * i + 1])});
            o["points"] = pts;
        } else if (t == "text") {
            o["p"] = {x(v[0]), x(v[1])};
        } else {
            o = e;  // pass through unknown types unchanged
        }
        if (e.contains("construction")) o["construction"] = e["construction"];
        out[id] = o;
    }
    return out;
}

// SVD-nullspace per-entity status coloring (port of _compute_entity_statuses).
std::map<std::string, std::string> compute_statuses(
    const VectorXd& x, const json& entities_list, const VarMap& var_map,
    int n_vars, const MatrixXd& J, const std::vector<std::string>& conflicting,
    const std::map<std::string, std::vector<int>>& tag_vars) {
    std::map<std::string, std::string> statuses;
    if (J.rows() == 0 || n_vars == 0) {
        for (const auto& e : entities_list) statuses[e["id"]] = "under";
        return statuses;
    }
    Eigen::JacobiSVD<MatrixXd> svd(J, Eigen::ComputeFullV);
    VectorXd s = svd.singularValues();
    double s0 = s.size() ? s(0) : 1.0;
    double thr = std::max(1e-7, 1e3 * std::numeric_limits<double>::epsilon() * s0);
    int rank = 0;
    for (int i = 0; i < s.size(); ++i)
        if (s(i) > thr) ++rank;
    const MatrixXd& V = svd.matrixV();  // n x n
    int n_null = n_vars - rank;
    VectorXd null_proj = VectorXd::Zero(n_vars);
    if (n_null > 0) {
        MatrixXd Vn = V.block(0, rank, n_vars, n_null);
        for (int i = 0; i < n_vars; ++i) null_proj(i) = Vn.row(i).norm();
    }
    const double tol_null = 1e-6;

    std::set<int> conflicting_vars;
    for (const auto& tag : conflicting) {
        auto it = tag_vars.find(tag);
        if (it != tag_vars.end())
            for (int v : it->second) conflicting_vars.insert(v);
    }

    for (const auto& e : entities_list) {
        std::string id = e["id"];
        const std::vector<int>& v = var_map.at(id);
        bool over = false;
        for (int vi : v)
            if (conflicting_vars.count(vi)) over = true;
        if (!conflicting_vars.empty() && over) {
            statuses[id] = "over";
            continue;
        }
        bool full = true;
        for (int vi : v)
            if (null_proj(vi) >= tol_null) full = false;
        statuses[id] = full ? "full" : "under";
    }
    return statuses;
}

}  // namespace

SketchSolution solve_sketch(const json& sketch, const json& env) {
    json entities_list = sketch.value("entities", json::array());
    json constraints_list = sketch.value("constraints", json::array());

    EntMap entities;
    for (const auto& e : entities_list) entities[e["id"]] = e;

    System sys;
    VarMap var_map;
    for (const auto& e : entities_list) {
        try {
            var_map[e["id"]] = allocate_entity(sys, e);
        } catch (...) {
            var_map[e["id"]] = sys.add_vars(0, {});
        }
    }

    // Track which variables each constraint tag touches (for over-defined coloring)
    // and register residual blocks.
    std::map<std::string, std::vector<int>> tag_vars;
    for (const auto& c : constraints_list) {
        int before = sys.n_vars();  // (unused, kept for clarity)
        (void)before;
        try {
            add_constraint(sys, c, entities, var_map, env);
        } catch (...) {
            // forward-tolerant: skip malformed/unsupported constraints
        }
    }

    SketchSolution sol;
    auto res = sys.solve(std::nullopt, 1e-10, 50);
    sol.x = res.x;
    sol.converged = res.converged;
    sol.var_map = var_map;

    auto analysis = sys.analyze(res.x);
    sol.dof = analysis.dof;
    sol.redundant = analysis.redundant_tags;
    sol.conflicting = analysis.conflicting_tags;

    sol.entities = extract_entities(res.x, entities_list, var_map);

    // Build the dense real-block Jacobian for status coloring, plus the
    // tag->variables map needed to flag over-defined entities.
    int n = sys.n_vars();
    MatrixXd J = sys.real_jacobian(res.x);
    for (const auto& c : constraints_list) {
        // reconstruct which variables a conflicting tag touches from refs
        // (cheap: union of referenced entity var blocks).
        if (!c.contains("id")) continue;
        std::string cid = c["id"];
        std::vector<int> vars;
        if (c.contains("refs"))
            for (const auto& r : c["refs"]) {
                std::string h = r.get<std::string>();
                std::string eid = h.substr(0, h.find('.'));
                auto it = var_map.find(eid);
                if (it != var_map.end())
                    vars.insert(vars.end(), it->second.begin(), it->second.end());
            }
        tag_vars[cid] = vars;
    }
    sol.statuses = compute_statuses(res.x, entities_list, var_map, n, J,
                                    sol.conflicting, tag_vars);
    return sol;
}

}  // namespace aicad::sketch
