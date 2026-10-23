// C++ port of engine/doc/sketch_solver.py (core subset). The residual/Jacobian
// formulas are copied from the Python helpers so the two stay at parity under the
// differential harness; see sketch.h for the coverage scope.
#include "sketch/sketch.h"

#include "solver/nsolve.h"
#include "util/expr.h"

#include <algorithm>
#include <cctype>
#include <charconv>
#include <chrono>
#include <cmath>
#include <functional>
#include <initializer_list>
#include <limits>
#include <memory>
#include <optional>
#include <cstdio>
#include <set>
#include <stdexcept>
#include <unordered_set>
#include <utility>

namespace chudware::sketch {
namespace {

using json = nlohmann::json;
using Eigen::MatrixXd;
using Eigen::Vector2d;
using Eigen::VectorXd;
using chudware::solver::System;

constexpr double kPi = 3.14159265358979323846;
constexpr double kDeg = kPi / 180.0;

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

// nlohmann's CONST operator[] is UNDEFINED BEHAVIOUR on a missing object key or an
// out-of-range array index: it dereferences end() and hands back a garbage json&,
// which num() then reads as a double — a wild pointer inside
// get_arithmetic_value(). A model that writes an entity in the shorthand dialect
// ({"type":"circle","cx":-23,"cy":0,"r":2.5} — no "center"/"radius"/"id") therefore
// SIGSEGV'd the whole process from inside allocate_entity, killing the python admin
// worker mid-turn and losing every modeling result in the session. The surrounding
// try/catch could not help: UB does not throw.
//
// jat() turns every miss into a reference to a shared null json, so num() raises
// nlohmann::type_error and the forward-tolerant catch skips exactly one entity or
// constraint — the same degradation an unresolved handle already gets.
const json& jnull() {
    static const json kNull;
    return kNull;
}
const json& jat(const json& j, const char* key) {
    if (!j.is_object()) return jnull();
    const auto it = j.find(key);
    return it == j.end() ? jnull() : *it;
}
const json& jat(const json& j, int i) {
    if (!j.is_array() || i < 0 || static_cast<std::size_t>(i) >= j.size()) return jnull();
    return j[static_cast<std::size_t>(i)];
}
// Entity/constraint id with the same missing-key tolerance (mirrors sketch3d.cpp,
// which already reads ids via value()). An id-less entity cannot be referenced by
// any constraint, so collapsing them onto "" is harmless — unlike reading end().
std::string jid(const json& j) {
    if (!j.is_object()) return std::string();
    const auto it = j.find("id");
    return (it != j.end() && it->is_string()) ? it->get<std::string>() : std::string();
}

std::vector<int> concat(const std::vector<int>& a, const std::vector<int>& b) {
    std::vector<int> r = a;
    r.insert(r.end(), b.begin(), b.end());
    return r;
}
std::vector<int> concat(const std::vector<int>& a, const std::vector<int>& b,
                        const std::vector<int>& c) {
    return concat(concat(a, b), c);
}

// Pick a subset of an index vector by local positions. Bounds-checked: an entity
// whose allocation failed registers ZERO solver vars (solve loop fallback), and the
// hardcoded layouts below ({0,1} on a line, {2,3} on an arc, p9 on a short spline, …)
// used to turn that into an out-of-bounds read that SIGSEGV'd hostd mid-agent-run.
// Throwing mirrors the unresolved-handle path: the caller skips the constraint.
std::vector<int> pick(const std::vector<int>& v, std::initializer_list<int> pos) {
    std::vector<int> r;
    r.reserve(pos.size());
    for (int p : pos) {
        if (p < 0 || static_cast<std::size_t>(p) >= v.size())
            throw std::out_of_range("pick: var index " + std::to_string(p) +
                                    " out of range (" + std::to_string(v.size()) +
                                    " solver vars)");
        r.push_back(v[p]);
    }
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
    const std::string et = ent.value("type", std::string());
    if (et == "ellipse_arc") {
        double rot = ent.contains("rot") ? num(jat(ent, "rot")) : 0.0;
        return {num(jat(jat(ent, "center"), 0)), num(jat(jat(ent, "center"), 1)),
                num(jat(ent, "rx")), num(jat(ent, "ry")), rot,
                num(jat(ent, "a0")), num(jat(ent, "a1"))};
    }
    if (et == "parabola") {
        return {num(jat(jat(ent, "vertex"), 0)), num(jat(jat(ent, "vertex"), 1)),
                num(jat(jat(ent, "focus"), 0)), num(jat(jat(ent, "focus"), 1)),
                num(jat(ent, "t0")), num(jat(ent, "t1"))};
    }
    if (et == "conic") {
        double rho = ent.contains("rho") ? num(jat(ent, "rho")) : 0.5;
        return {num(jat(jat(ent, "p0"), 0)), num(jat(jat(ent, "p0"), 1)),
                num(jat(jat(ent, "p1"), 0)), num(jat(jat(ent, "p1"), 1)),
                num(jat(jat(ent, "apex"), 0)), num(jat(jat(ent, "apex"), 1)), rho};
    }
    if (et == "arc_slot") {
        return {num(jat(jat(ent, "center"), 0)), num(jat(jat(ent, "center"), 1)),
                num(jat(ent, "radius")), num(jat(ent, "a0")), num(jat(ent, "a1")),
                num(jat(ent, "width"))};
    }
    return {};
}

// ---- entity parameterization ------------------------------------------------

std::vector<int> allocate_entity(System& sys, const json& e) {
    const std::string t = e.value("type", std::string());
    if (t == "point") {
        Vector2d p(num(jat(jat(e, "p"), 0)), num(jat(jat(e, "p"), 1)));
        return sys.add_vars(2, {p(0), p(1)});
    }
    if (t == "line") {
        return sys.add_vars(4, {num(jat(jat(e, "p1"), 0)), num(jat(jat(e, "p1"), 1)),
                                num(jat(jat(e, "p2"), 0)), num(jat(jat(e, "p2"), 1))});
    }
    if (t == "circle") {
        return sys.add_vars(3, {num(jat(jat(e, "center"), 0)), num(jat(jat(e, "center"), 1)),
                                num(jat(e, "radius"))});
    }
    if (t == "arc") {
        return sys.add_vars(5, {num(jat(jat(e, "center"), 0)), num(jat(jat(e, "center"), 1)),
                                num(jat(e, "radius")), num(jat(e, "start")),
                                num(jat(e, "end"))});
    }
    if (t == "ellipse") {
        return sys.add_vars(5, {num(jat(jat(e, "center"), 0)), num(jat(jat(e, "center"), 1)),
                                num(jat(e, "rx")), num(jat(e, "ry")), num(jat(e, "rot"))});
    }
    if (t == "ellipse_arc") {
        double rot = e.contains("rot") ? num(jat(e, "rot")) : 0.0;
        return sys.add_vars(7, {num(jat(jat(e, "center"), 0)), num(jat(jat(e, "center"), 1)),
                                num(jat(e, "rx")), num(jat(e, "ry")), rot,
                                num(jat(e, "a0")), num(jat(e, "a1"))});
    }
    if (t == "parabola") {
        return sys.add_vars(6, {num(jat(jat(e, "vertex"), 0)), num(jat(jat(e, "vertex"), 1)),
                                num(jat(jat(e, "focus"), 0)), num(jat(jat(e, "focus"), 1)),
                                num(jat(e, "t0")), num(jat(e, "t1"))});
    }
    if (t == "conic") {
        double rho = e.contains("rho") ? num(jat(e, "rho")) : 0.5;
        return sys.add_vars(7, {num(jat(jat(e, "p0"), 0)), num(jat(jat(e, "p0"), 1)),
                                num(jat(jat(e, "p1"), 0)), num(jat(jat(e, "p1"), 1)),
                                num(jat(jat(e, "apex"), 0)), num(jat(jat(e, "apex"), 1)),
                                rho});
    }
    if (t == "slot") {
        return sys.add_vars(5, {num(jat(jat(e, "p1"), 0)), num(jat(jat(e, "p1"), 1)),
                                num(jat(jat(e, "p2"), 0)), num(jat(jat(e, "p2"), 1)),
                                num(jat(e, "width"))});
    }
    if (t == "arc_slot") {
        return sys.add_vars(6, {num(jat(jat(e, "center"), 0)), num(jat(jat(e, "center"), 1)),
                                num(jat(e, "radius")), num(jat(e, "a0")),
                                num(jat(e, "a1")), num(jat(e, "width"))});
    }
    if (t == "spline" || t == "style_spline") {
        const json& pts = jat(e, "points");
        if (!pts.is_array()) throw std::runtime_error("spline: missing points array");
        std::vector<double> init;
        for (const auto& p : pts) {
            init.push_back(num(jat(p, 0)));
            init.push_back(num(jat(p, 1)));
        }
        return sys.add_vars(static_cast<int>(init.size()), init);
    }
    if (t == "text") {
        return sys.add_vars(2, {num(jat(jat(e, "p"), 0)), num(jat(jat(e, "p"), 1))});
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

// ---- curve evaluators + numeric closest-point infrastructure ----------------
//
// Verbatim port of sketch_solver.py's conic/spline curve evaluators and the
// numeric projection helpers used by point_on (conics) and the line-conic /
// line-spline / curve-curve tangency residuals. Each evaluator maps an entity's
// variable slice `cv` + a curve parameter to (P, dP/dparam). Range-free: the
// trim vars (a0/a1, t0/t1) are ignored, mirroring how arc/circle point_on uses
// the full circle today.

using CurvePair = std::pair<Vector2d, Vector2d>;
using CurveFn = std::function<CurvePair(const VectorXd&, double)>;
using ScanFn = std::function<VectorXd(const VectorXd&, int)>;

// numpy linspace(lo, hi, n) (endpoints inclusive).
VectorXd linspace(double lo, double hi, int n) {
    VectorXd v(n);
    if (n == 1) {
        v(0) = lo;
        return v;
    }
    for (int i = 0; i < n; ++i) v(i) = lo + (hi - lo) * (static_cast<double>(i) / (n - 1));
    return v;
}

// Central-difference Jacobian of a scalar residual rfn(x)->double over n vars.
// Port of _numeric_jac_1: h = 1e-6 * max(1, |xs[i]|); J = (r(x+h)-r(x-h))/(2h).
MatrixXd numeric_jac_1(const std::function<double(const VectorXd&)>& rfn,
                       const VectorXd& xs) {
    int n = static_cast<int>(xs.size());
    MatrixXd J(1, n);
    for (int i = 0; i < n; ++i) {
        double h = 1e-6 * std::max(1.0, std::abs(xs(i)));
        VectorXd xp = xs, xm = xs;
        xp(i) += h;
        xm(i) -= h;
        J(0, i) = (rfn(xp) - rfn(xm)) / (2.0 * h);
    }
    return J;
}

// xs = [vx, vy, fx, fy]; P(t) = vertex + (t^2/4f)*u + t*w; tangent dP/dt.
// Port of _parabola_curve.
CurvePair parabola_curve(const VectorXd& xs, double t) {
    double vx = xs(0), vy = xs(1), fx = xs(2), fy = xs(3);
    double dx = fx - vx, dy = fy - vy;
    double f = std::hypot(dx, dy);
    if (f < 1e-12) return {Vector2d(vx, vy), Vector2d(0.0, 1.0)};
    double ux = dx / f, uy = dy / f;
    double wx = -uy, wy = ux;
    double s = (t * t) / (4.0 * f);
    Vector2d P(vx + s * ux + t * wx, vy + s * uy + t * wy);
    double dsd = (2.0 * t) / (4.0 * f);
    Vector2d dP(dsd * ux + wx, dsd * uy + wy);
    return {P, dP};
}

// xs = [p0x,p0y,p1x,p1y,apx,apy,rho]; rational quadratic Bezier on u in [0,1].
// Port of _conic_curve.
CurvePair conic_curve(const VectorXd& xs, double u) {
    Vector2d p0(xs(0), xs(1));
    Vector2d p1(xs(2), xs(3));
    Vector2d ap(xs(4), xs(5));
    double rho = xs(6);
    rho = std::min(std::max(rho, 1e-6), 1.0 - 1e-6);
    double w = rho / (1.0 - rho);
    double b0 = (1.0 - u) * (1.0 - u);
    double b1 = 2.0 * (1.0 - u) * u * w;
    double b2 = u * u;
    double den = b0 + b1 + b2;
    Vector2d num = b0 * p0 + b1 * ap + b2 * p1;
    Vector2d P = num / den;
    double db0 = -2.0 * (1.0 - u);
    double db1 = 2.0 * w * (1.0 - 2.0 * u);
    double db2 = 2.0 * u;
    Vector2d dnum = db0 * p0 + db1 * ap + db2 * p1;
    double dden = db0 + db1 + db2;
    Vector2d dP = (dnum * den - num * dden) / (den * den);
    return {P, dP};
}

// xs = [cx, cy, r]; a = angle in RADIANS. Port of _circle_curve (== _arc_curve).
CurvePair circle_curve(const VectorXd& xs, double a) {
    double cx = xs(0), cy = xs(1), r = xs(2);
    double ca = std::cos(a), sa = std::sin(a);
    return {Vector2d(cx + r * ca, cy + r * sa), Vector2d(-r * sa, r * ca)};
}

// xs = [cx, cy, rx, ry, rot]; a = angle in RADIANS (un-rotated frame).
// Port of _ellipse_arc_curve. rot is in DEGREES (kDeg == _DEG).
CurvePair ellipse_arc_curve(const VectorXd& cv, double a) {
    double cx = cv(0), cy = cv(1), rx = cv(2), ry = cv(3), rot = cv(4);
    double rr = rot * kDeg;
    double cr = std::cos(rr), sr = std::sin(rr);
    double ca = std::cos(a), sa = std::sin(a);
    double lx = rx * ca, ly = ry * sa;
    Vector2d P(cx + cr * lx - sr * ly, cy + sr * lx + cr * ly);
    double dlx = -rx * sa, dly = ry * ca;
    Vector2d dP(cr * dlx - sr * dly, sr * dlx + cr * dly);
    return {P, dP};
}

// Return the list of (x,y) control/through points from a flat var slice.
// Port of _spline_segments.
std::vector<Vector2d> spline_segments(const VectorXd& cv) {
    int n = static_cast<int>(cv.size()) / 2;
    std::vector<Vector2d> pts;
    pts.reserve(n);
    for (int i = 0; i < n; ++i) pts.emplace_back(cv(2 * i), cv(2 * i + 1));
    return pts;
}

// Catmull-Rom interpolating spline through `pts`. u in [0, n-1]. Port of
// _catmull_rom.
CurvePair catmull_rom(const std::vector<Vector2d>& pts, double u) {
    int n = static_cast<int>(pts.size());
    if (n == 1) return {pts[0], Vector2d(0.0, 0.0)};
    if (n == 2) {
        double t = std::min(std::max(u, 0.0), 1.0);
        Vector2d P(pts[0](0) + (pts[1](0) - pts[0](0)) * t,
                   pts[0](1) + (pts[1](1) - pts[0](1)) * t);
        Vector2d dP(pts[1](0) - pts[0](0), pts[1](1) - pts[0](1));
        return {P, dP};
    }
    u = std::min(std::max(u, 0.0), static_cast<double>(n - 1));
    int i = static_cast<int>(std::floor(u));
    if (i >= n - 1) i = n - 2;
    double t = u - i;
    Vector2d p0 = (i - 1 >= 0) ? pts[i - 1] : (2.0 * pts[0] - pts[1]);
    Vector2d p1 = pts[i];
    Vector2d p2 = pts[i + 1];
    Vector2d p3 = (i + 2 <= n - 1) ? pts[i + 2] : (2.0 * pts[n - 1] - pts[n - 2]);
    double t2 = t * t, t3 = t2 * t;
    Vector2d P = 0.5 * ((2.0 * p1) + (-p0 + p2) * t +
                        (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * t2 +
                        (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * t3);
    Vector2d dP = 0.5 * ((-p0 + p2) +
                         (2.0 * p0 - 5.0 * p1 + 4.0 * p2 - p3) * (2.0 * t) +
                         (-p0 + 3.0 * p1 - 3.0 * p2 + p3) * (3.0 * t2));
    return {P, dP};
}

// Clamped cubic B-spline by control poles. u in [0, 1]. Port of _bspline_poles.
CurvePair bspline_poles(const std::vector<Vector2d>& pts, double u) {
    int n = static_cast<int>(pts.size());
    if (n == 1) return {pts[0], Vector2d(0.0, 0.0)};
    if (n == 2) {
        double t = std::min(std::max(u, 0.0), 1.0);
        Vector2d P = pts[0] + (pts[1] - pts[0]) * t;
        return {P, Vector2d(pts[1] - pts[0])};
    }
    int degree = std::min(3, n - 1);
    int n_internal = n - degree - 1;
    std::vector<double> knots;
    for (int j = 0; j < degree + 1; ++j) knots.push_back(0.0);
    for (int j = 1; j < n_internal + 1; ++j)
        knots.push_back(static_cast<double>(j) / (n_internal + 1));
    for (int j = 0; j < degree + 1; ++j) knots.push_back(1.0);
    double uu = std::min(std::max(u, 0.0), 1.0 - 1e-12);

    auto deboor_value = [&](double uval) -> Vector2d {
        int k = degree;
        while (k < n - 1 && uu >= knots[k + 1]) ++k;
        std::vector<Vector2d> d;
        d.reserve(degree + 1);
        for (int j = 0; j < degree + 1; ++j) d.push_back(pts[j + k - degree]);
        for (int r = 1; r < degree + 1; ++r) {
            for (int j = degree; j >= r; --j) {
                double left = knots[j + k - degree];
                double right = knots[j + 1 + k - r];
                double denom = right - left;
                double alpha = (denom <= 1e-15) ? 0.0 : (uval - left) / denom;
                d[j] = (1.0 - alpha) * d[j - 1] + alpha * d[j];
            }
        }
        return d[degree];
    };

    double h = 1e-6;
    Vector2d P = deboor_value(uu);
    Vector2d Pp = deboor_value(std::min(uu + h, 1.0 - 1e-12));
    Vector2d Pm = deboor_value(std::max(uu - h, 0.0));
    Vector2d dP = (Pp - Pm) / (2.0 * h);
    return {P, dP};
}

// Through-point spline P(u), dP/du. u in [0, n-1]. Port of _spline_curve.
CurvePair spline_curve(const VectorXd& cv, double u) {
    return catmull_rom(spline_segments(cv), u);
}

// Control-pole spline P(u), dP/du. u in [0, 1]. Port of _style_spline_curve.
CurvePair style_spline_curve(const VectorXd& cv, double u) {
    return bspline_poles(spline_segments(cv), u);
}

// xs = [cx,cy,rx,ry,rot]; signed implicit value of the rotated ellipse at P,
// scaled to length units. Port of _ellipse_implicit.
double ellipse_implicit(const VectorXd& xs, const Vector2d& P) {
    double cx = xs(0), cy = xs(1), rx = xs(2), ry = xs(3), rot = xs(4);
    double rr = rot * kDeg;
    double cr = std::cos(rr), sr = std::sin(rr);
    double dxp = P(0) - cx, dyp = P(1) - cy;
    double qx = cr * dxp + sr * dyp;
    double qy = -sr * dxp + cr * dyp;
    rx = (std::abs(rx) > 1e-9) ? rx : 1e-9;
    ry = (std::abs(ry) > 1e-9) ? ry : 1e-9;
    double rad = std::hypot(qx / rx, qy / ry);
    double r_mean = 0.5 * (std::abs(rx) + std::abs(ry));
    return (rad - 1.0) * r_mean;
}


// Exact line<->ellipse tangency residual via the support function. Port of
// _ellipse_line_support. line = [ax,ay,bx,by]; ell = [cx,cy,rx,ry,rot].
double ellipse_line_support(const VectorXd& line, const VectorXd& ell) {
    double ax = line(0), ay = line(1), bx = line(2), by = line(3);
    double cx = ell(0), cy = ell(1), rx = ell(2), ry = ell(3), rot = ell(4);
    double dxab = bx - ax, dyab = by - ay;
    double L = std::hypot(dxab, dyab);
    if (L < 1e-12) return 0.0;
    double nx = -dyab / L, ny = dxab / L;
    double rr = rot * kDeg;
    double cr = std::cos(rr), sr = std::sin(rr);
    double nlx = cr * nx + sr * ny;
    double nly = -sr * nx + cr * ny;
    double support = std::hypot(rx * nlx, ry * nly);
    double dist = std::abs((cx - ax) * nx + (cy - ay) * ny);
    return dist - support;
}

// Signed perpendicular offset of P from curve point Q with tangent dQ. Port of
// _signed_offset.
double signed_offset(const Vector2d& P, const Vector2d& Q, const Vector2d& dQ) {
    double L = std::hypot(dQ(0), dQ(1));
    if (L < 1e-12) return std::hypot(P(0) - Q(0), P(1) - Q(1));
    double nx = -dQ(1) / L, ny = dQ(0) / L;
    return (P(0) - Q(0)) * nx + (P(1) - Q(1)) * ny;
}

// Refine the closest-point param: root of g(p)=(C(p)-P).C'(p)=0 via Newton.
// Port of _newton_project (12 iters; internal h=1e-4).
double newton_project(const std::function<CurvePair(double)>& curve_fn, double p0,
                      const Vector2d& P, int iters = 12) {
    double p = p0;
    for (int it = 0; it < iters; ++it) {
        CurvePair cur = curve_fn(p);
        Vector2d Q = cur.first, dQ = cur.second;
        Vector2d diff(Q(0) - P(0), Q(1) - P(1));
        double g = diff(0) * dQ(0) + diff(1) * dQ(1);
        double h = 1e-4;
        Vector2d dQp = curve_fn(p + h).second;
        Vector2d dQm = curve_fn(p - h).second;
        Vector2d ddQ = (dQp - dQm) / (2.0 * h);
        double gp = dQ(0) * dQ(0) + dQ(1) * dQ(1) + diff(0) * ddQ(0) + diff(1) * ddQ(1);
        if (std::abs(gp) < 1e-14) break;
        double step = g / gp;
        p -= step;
        if (std::abs(step) < 1e-12) break;
    }
    return p;
}

// Param t minimizing |P - parabola(t)|: 81-sample scan over [-200,200] + Newton.
// Port of _parabola_project.
double parabola_project(const VectorXd& cv, const Vector2d& P, double t0 = -200.0,
                        double t1 = 200.0) {
    double best_t = t0, best_d = std::numeric_limits<double>::infinity();
    for (int k = 0; k < 81; ++k) {
        double t = t0 + (t1 - t0) * k / 80.0;
        Vector2d Q = parabola_curve(cv, t).first;
        double d = (P(0) - Q(0)) * (P(0) - Q(0)) + (P(1) - Q(1)) * (P(1) - Q(1));
        if (d < best_d) {
            best_d = d;
            best_t = t;
        }
    }
    return newton_project([&](double t) { return parabola_curve(cv, t); }, best_t, P);
}

// Param u in [0,1] minimizing |P - conic(u)|: 41-sample scan + Newton, clamped.
// Port of _conic_project.
double conic_project(const VectorXd& cv, const Vector2d& P) {
    double best_u = 0.0, best_d = std::numeric_limits<double>::infinity();
    for (int k = 0; k < 41; ++k) {
        double u = k / 40.0;
        Vector2d Q = conic_curve(cv, u).first;
        double d = (P(0) - Q(0)) * (P(0) - Q(0)) + (P(1) - Q(1)) * (P(1) - Q(1));
        if (d < best_d) {
            best_d = d;
            best_u = u;
        }
    }
    double u = newton_project([&](double uu) { return conic_curve(cv, uu); }, best_u, P);
    return std::min(std::max(u, 0.0), 1.0);
}

// Param p where the curve tangent is parallel to a line (tangent.n = 0). Coarse
// 41-sample scan for the extreme point in +n, then 12 Newton iters (h=1e-4).
// Port of _extremum_param.
double extremum_param(const CurveFn& curve, const VectorXd& cv, double nx, double ny,
                      const ScanFn& scan) {
    VectorXd pts = scan(cv, 41);
    double best_p = pts(0), best_proj = -std::numeric_limits<double>::infinity();
    for (int i = 0; i < pts.size(); ++i) {
        Vector2d Q = curve(cv, pts(i)).first;
        double proj = Q(0) * nx + Q(1) * ny;
        if (proj > best_proj) {
            best_proj = proj;
            best_p = pts(i);
        }
    }
    double p = best_p;
    for (int it = 0; it < 12; ++it) {
        Vector2d dQ = curve(cv, p).second;
        double g = dQ(0) * nx + dQ(1) * ny;
        double h = 1e-4;
        Vector2d dQp = curve(cv, p + h).second;
        Vector2d dQm = curve(cv, p - h).second;
        double ddg = ((dQp(0) - dQm(0)) * nx + (dQp(1) - dQm(1)) * ny) / (2.0 * h);
        if (std::abs(ddg) < 1e-14) break;
        double step = g / ddg;
        p -= step;
        if (std::abs(step) < 1e-12) break;
    }
    return p;
}

// Params (p, q) at the mutual closest approach of two curves: 25x25 coarse scan
// then 8 alternating closest-point projections. Port of _mutual_closest.
// When `warm_pq` is non-null and holds a finite prior (p,q) from an earlier residual
// evaluation in the same solve (interactive path), the expensive 25×25
// scan is skipped and Newton continues from the warm seed — same attractor for a
// smoothly moving geometry, much cheaper under LM's many residual probes.
std::pair<double, double> mutual_closest(const CurveFn& curve0, const VectorXd& cv0,
                                         const ScanFn& scan0, const CurveFn& curve1,
                                         const VectorXd& cv1, const ScanFn& scan1,
                                         std::pair<double, double>* warm_pq = nullptr) {
    double bp, bq;
    const bool have_warm = warm_pq && std::isfinite(warm_pq->first) &&
                           std::isfinite(warm_pq->second);
    if (have_warm) {
        bp = warm_pq->first;
        bq = warm_pq->second;
    } else {
        VectorXd ps = scan0(cv0, 25);
        VectorXd qs = scan1(cv1, 25);
        std::vector<Vector2d> q_pts;
        q_pts.reserve(qs.size());
        for (int j = 0; j < qs.size(); ++j) q_pts.push_back(curve1(cv1, qs(j)).first);
        double best = std::numeric_limits<double>::infinity();
        bp = ps(0);
        bq = qs(0);
        for (int i = 0; i < ps.size(); ++i) {
            Vector2d P0 = curve0(cv0, ps(i)).first;
            for (int j = 0; j < qs.size(); ++j) {
                const Vector2d& Q1 = q_pts[j];
                double d = (P0(0) - Q1(0)) * (P0(0) - Q1(0)) +
                           (P0(1) - Q1(1)) * (P0(1) - Q1(1));
                if (d < best) {
                    best = d;
                    bp = ps(i);
                    bq = qs(j);
                }
            }
        }
    }
    double p = bp, q = bq;
    for (int it = 0; it < 8; ++it) {
        Vector2d P0 = curve0(cv0, p).first;
        q = newton_project([&](double u) { return curve1(cv1, u); }, q, P0);
        Vector2d Q1 = curve1(cv1, q).first;
        p = newton_project([&](double u) { return curve0(cv0, u); }, p, Q1);
    }
    if (warm_pq) *warm_pq = {p, q};
    return {p, q};
}

// Signed gap between the two curves at their mutual closest point. v = [cv0, cv1]
// (k0 + k1 vars). Returns a scalar. Port of _curve_gap_residual.
double curve_gap_residual(const VectorXd& v, int k0, int k1, const CurveFn& curve0,
                          const ScanFn& scan0, const CurveFn& curve1,
                          const ScanFn& scan1,
                          std::pair<double, double>* warm_pq = nullptr) {
    VectorXd cv0 = v.head(k0);
    VectorXd cv1 = v.segment(k0, k1);
    auto pq = mutual_closest(curve0, cv0, scan0, curve1, cv1, scan1, warm_pq);
    double p = pq.first, q = pq.second;
    CurvePair c0 = curve0(cv0, p);
    Vector2d P0 = c0.first, dP0 = c0.second;
    Vector2d Q1 = curve1(cv1, q).first;
    double L = std::hypot(dP0(0), dP0(1));
    if (L < 1e-12) return std::hypot(P0(0) - Q1(0), P0(1) - Q1(1));
    double nx = -dP0(1) / L, ny = dP0(0) / L;
    return (Q1(0) - P0(0)) * nx + (Q1(1) - P0(1)) * ny;
}

// Per-entity-type curve description for the generic curve<->curve tangent
// residual. Port of _curve_descriptor. Returns false (ok=false) for an
// unsupported entity type.
struct CurveDescriptor {
    bool ok = false;
    std::vector<int> idx;  // global variable indices the curve depends on
    CurveFn eval;
    ScanFn scan;
};

CurveDescriptor curve_descriptor(const std::string& etype,
                                 const std::vector<int>& vidx) {
    CurveDescriptor d;
    if (etype == "circle" || etype == "arc") {
        d.ok = true;
        d.idx = pick(vidx, {0, 1, 2});
        d.eval = circle_curve;
        d.scan = [](const VectorXd&, int n) { return linspace(0.0, 2.0 * kPi, n); };
    } else if (etype == "ellipse_arc" || etype == "ellipse") {
        d.ok = true;
        d.idx = pick(vidx, {0, 1, 2, 3, 4});
        d.eval = ellipse_arc_curve;
        d.scan = [](const VectorXd&, int n) { return linspace(0.0, 2.0 * kPi, n); };
    } else if (etype == "parabola") {
        d.ok = true;
        d.idx = pick(vidx, {0, 1, 2, 3});
        d.eval = parabola_curve;
        d.scan = [](const VectorXd&, int n) { return linspace(-200.0, 200.0, n); };
    } else if (etype == "conic") {
        d.ok = true;
        d.idx = pick(vidx, {0, 1, 2, 3, 4, 5, 6});
        d.eval = conic_curve;
        d.scan = [](const VectorXd&, int n) { return linspace(0.0, 1.0, n); };
    } else if (etype == "spline") {
        if (vidx.size() < 4) throw std::runtime_error("spline curve has fewer than two points");
        d.ok = true;
        d.idx = vidx;
        int npts = static_cast<int>(vidx.size()) / 2;
        d.eval = spline_curve;
        d.scan = [npts](const VectorXd&, int n) {
            return linspace(0.0, static_cast<double>(std::max(npts - 1, 1)), n);
        };
    } else if (etype == "style_spline") {
        if (vidx.size() < 4) throw std::runtime_error("style_spline curve has fewer than two points");
        d.ok = true;
        d.idx = vidx;
        d.eval = style_spline_curve;
        d.scan = [](const VectorXd&, int n) { return linspace(0.0, 1.0, n); };
    }
    return d;
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

// A scalar drag handle: a SINGLE solver variable (a circle/arc radius, or an arc
// endpoint angle in degrees). The host pre-computes the target scalar — radius =
// |cursor - centre| in mm, or the endpoint angle in degrees — into the drag op's
// xy[0] (see sketch_mode.cpp), so drag() soft-targets idx[0] toward that scalar and
// only the one DOF moves: the centre and radius never drift. val/jac embed the scalar
// as the x-component of a 1D point — valid for the Handle, unused by the scalar drag.
Handle scalar_handle(const std::vector<int>& idx) {
    Handle h;
    h.idx = idx;
    h.val = [](const VectorXd& xs) { return Vector2d(xs(0), 0.0); };
    h.jac = [](const VectorXd&) {
        MatrixXd j(2, 1);
        j << 1.0, 0.0;
        return j;
    };
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
    // Zero solver vars means the entity's allocation FAILED upstream (the solve loop
    // registers an empty vector to stay forward-tolerant). Every branch below indexes
    // this vector; an empty one is UB (and used to SIGSEGV hostd). Throw so the
    // caller skips the constraint, same as an unresolved handle.
    if (vidx.empty())
        throw std::runtime_error("handle '" + handle + "': entity '" + eid + "' (" + t +
                                 ") has no solver vars — its geometry failed to register");

    if (t == "point") return ident_point(vidx);
    if (t == "line") {
        // start/end are the modeler's common aliases for p1/p2 (session trim/extend
        // already treat them that way; without this, coincident on "l0.end" silently
        // failed via the unresolved-handle throw → skipped constraint).
        if (sub.empty() || sub == "p1" || sub == "start")
            return ident_point(pick(vidx, {0, 1}));
        if (sub == "p2" || sub == "end") return ident_point(pick(vidx, {2, 3}));
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
        // Rim drag handle: the host pre-computes the target RADIUS (mm) into xy[0], so
        // this exposes ONLY the radius variable; the centre never drifts. "<id>.rim".
        if (sub == "rim") return scalar_handle(pick(vidx, {2}));
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
        // Rim drag handle: host pre-computes the target RADIUS (mm) into xy[0]. "<id>.rim".
        if (sub == "rim") return scalar_handle(pick(vidx, {2}));
        // Endpoint-ANGLE drag handles: the host pre-computes the target angle (deg) into
        // xy[0], so these expose ONLY the angle variable (a0/a1); the centre and radius
        // never move. Contract drag-handles "<id>.start_angle" / "<id>.end_angle".
        if (sub == "start_angle") return scalar_handle(pick(vidx, {3}));
        if (sub == "end_angle") return scalar_handle(pick(vidx, {4}));
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
        if (sub.empty() || sub == "start")
            return ident_point(pick(vidx, {0, 1}));
        if (sub == "end") {
            const int n = static_cast<int>(vidx.size());
            return ident_point(pick(vidx, {n - 2, n - 1}));
        }
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
                return chudware::util::evaluate(e, env_to_map(env));
            } catch (const chudware::util::ExprError&) {
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
        // adjustment is applied OUTSIDE the abs (a scalar offset on the centre-gap),
        // matching the aligned (radial) branch and the Python oracle _add_distance.
        // For the realistic separated case this gives the true facing near/far-rim
        // gap (|Du| - r0 - r1 for [min,min]); a per-coordinate (inside-the-abs) form
        // would instead pick the same-side rim of both circles and report the wrong
        // gap. NOTE: the degenerate concentric+axis+cond case (|Du|=0) is an
        // unsatisfiable request with positive radii; see logs/agentB-blockers.md for
        // the SW-validation note on the axis rim convention.
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
                double length = std::max(std::hypot(bx - ax, by - ay), 1e-12);
                return r1(((bx - ax) * (P(1) - ay) - (by - ay) * (P(0) - ax)) / length);
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
                double length = std::max(std::hypot(dxab, dyab), 1e-12);
                double cross = dxab * (Py - ay) - dyab * (Px - ax);
                Eigen::RowVector4d dl(-dxab / length, -dyab / length, dxab / length, dyab / length);
                J /= length;
                J.rightCols(4) -= (cross / (length * length)) * dl;
                return J;
            },
            cid);
        return;
    }
    if (t == "circle" || t == "arc") {
        // point_on places the point on the FULL circle (any arc a0/a1 trim is
        // ignored), matching the Python oracle's _add_point_on convention.
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
        return;
    }
    if (t == "ellipse_arc" || t == "ellipse") {
        // Implicit rotated-ellipse residual on (cx, cy, rx, ry, rot). For ellipse_arc
        // the a0/a1 trim is ignored (places on the FULL ellipse), matching the
        // arc/circle point_on convention; a plain ellipse has no trim. _add_point_on.
        std::vector<int> eidx = pick(var_map.at(eid), {0, 1, 2, 3, 4});
        std::vector<int> combined = concat(p.idx, eidx);
        constexpr int ke = 5;
        sys.add_residual(
            [pv, kp, ke](const VectorXd& xs) {
                Vector2d P = pv(vhead(xs, kp));
                return r1(ellipse_implicit(vseg(xs, kp, ke), P));
            },
            combined,
            [pv, kp, ke](const VectorXd& xs) {
                return numeric_jac_1(
                    [pv, kp, ke](const VectorXd& v) {
                        return ellipse_implicit(vseg(v, kp, ke), pv(vhead(v, kp)));
                    },
                    xs);
            },
            cid);
        return;
    }
    if (t == "parabola") {
        // Closest-param projection: residual = signed offset of P from the curve
        // along the normal at the projected param t*. Port of _add_point_on.
        std::vector<int> eidx = pick(var_map.at(eid), {0, 1, 2, 3});
        std::vector<int> combined = concat(p.idx, eidx);
        constexpr int ke = 4;
        auto rfn = [pv, kp, ke](const VectorXd& v) {
            Vector2d P = pv(vhead(v, kp));
            VectorXd cv = vseg(v, kp, ke);
            double t_ = parabola_project(cv, P);
            CurvePair qd = parabola_curve(cv, t_);
            return signed_offset(P, qd.first, qd.second);
        };
        sys.add_residual(
            [rfn](const VectorXd& xs) { return r1(rfn(xs)); }, combined,
            [rfn](const VectorXd& xs) { return numeric_jac_1(rfn, xs); }, cid);
        return;
    }
    if (t == "conic") {
        std::vector<int> eidx = pick(var_map.at(eid), {0, 1, 2, 3, 4, 5, 6});
        std::vector<int> combined = concat(p.idx, eidx);
        constexpr int ke = 7;
        auto rfn = [pv, kp, ke](const VectorXd& v) {
            Vector2d P = pv(vhead(v, kp));
            VectorXd cv = vseg(v, kp, ke);
            double u = conic_project(cv, P);
            CurvePair qd = conic_curve(cv, u);
            return signed_offset(P, qd.first, qd.second);
        };
        sys.add_residual(
            [rfn](const VectorXd& xs) { return r1(rfn(xs)); }, combined,
            [rfn](const VectorXd& xs) { return numeric_jac_1(rfn, xs); }, cid);
        return;
    }
    if (t == "spline" || t == "style_spline") {
        CurveDescriptor desc = curve_descriptor(t, var_map.at(eid));
        const int ke = static_cast<int>(desc.idx.size());
        auto rfn = [pv, kp, ke, desc](const VectorXd& v) {
            Vector2d P = pv(v.head(kp));
            VectorXd cv = v.segment(kp, ke);
            VectorXd scan = desc.scan(cv, 81);
            double best = scan(0), dist = std::numeric_limits<double>::infinity();
            for (int i = 0; i < scan.size(); ++i) {
                double d = (desc.eval(cv, scan(i)).first - P).squaredNorm();
                if (d < dist) { dist = d; best = scan(i); }
            }
            double u = newton_project([&](double p) { return desc.eval(cv, p); }, best, P);
            u = std::clamp(u, scan(0), scan(scan.size() - 1));
            CurvePair q = desc.eval(cv, u);
            Vector2d delta = P - q.first;
            double offset = signed_offset(P, q.first, q.second);
            if ((u <= scan(0) + 1e-10 && delta.dot(q.second) < 0) ||
                (u >= scan(scan.size() - 1) - 1e-10 && delta.dot(q.second) > 0))
                return std::copysign(delta.norm(), offset == 0 ? 1.0 : offset);
            return offset;
        };
        sys.add_residual([rfn](const VectorXd& v) { return r1(rfn(v)); },
                         concat(p.idx, desc.idx),
                         [rfn](const VectorXd& v) { return numeric_jac_1(rfn, v); }, cid);
        return;
    }
    throw std::runtime_error("point_on: unsupported curve ref '" + entity_h + "' (" + t + ")");
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

    bool applied = false;
    auto pin = [&](const std::vector<int>& idx, const VectorXd& tgt) {
        int k = static_cast<int>(idx.size());
        VectorXd t_copy = tgt;
        sys.add_residual(
            [t_copy, k](const VectorXd& xs) { return VectorXd(xs - t_copy); }, idx,
            [k](const VectorXd&) { return MatrixXd(MatrixXd::Identity(k, k)); }, cid);
        applied = true;
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
        if (sub.empty()) {
            VectorXd tgt(5);
            tgt << num(ent["center"][0]), num(ent["center"][1]), num(ent["rx"]),
                num(ent["ry"]), num(ent["rot"]);
            pin(pick(var_map.at(eid), {0, 1, 2, 3, 4}), tgt);
        } else if (sub == "center") {
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
        } else {
            int k = -1;
            if (sub == "start")
                k = 0;
            else if (sub == "end")
                k = static_cast<int>(pts.size()) - 1;
            else if (sub[0] == 'p')
                k = std::stoi(sub.substr(1));
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
        applied = true;
    }
    if (!applied) throw std::runtime_error("fix: unsupported handle '" + handle + "'");
}


void add_equal(System& sys, const std::string& cid, const std::string& h0,
               const std::string& h1, const EntMap& entities, const VarMap& var_map) {
    std::string e0 = eid_of(h0), e1 = eid_of(h1);
    std::string t0 = entities.at(e0).at("type"), t1 = entities.at(e1).at("type");
    auto equal_vars = [&](std::initializer_list<int> positions) {
        auto a = pick(var_map.at(e0), positions), b = pick(var_map.at(e1), positions);
        const int n = static_cast<int>(a.size());
        sys.add_residual([n](const VectorXd& v) { return VectorXd(v.head(n) - v.tail(n)); },
                         concat(a, b), [n](const VectorXd&) {
            MatrixXd j = MatrixXd::Zero(n, 2 * n);
            j.leftCols(n).setIdentity(); j.rightCols(n) = -MatrixXd::Identity(n, n);
            return j;
        }, cid);
    };
    if (t0 == "slot" && t1 == "slot") equal_vars({4});
    if ((t0 == "ellipse" || t0 == "ellipse_arc") && (t1 == "ellipse" || t1 == "ellipse_arc")) {
        equal_vars({2, 3});
        return;
    }
    if (t0 == "arc_slot" && t1 == "arc_slot") {
        equal_vars({2, 5});
        return;
    }
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
    } else {
        throw std::runtime_error("equal: incompatible refs '" + h0 + "' (" + t0 +
                                 "), '" + h1 + "' (" + t1 + ")");
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

// Line (or slot spine) length residual: |p2 - p1| == d. Used by type "length"
// and by the common modeler shorthand distance{refs:[line]} (= line-length dim).
void add_line_length(System& sys, const std::string& cid, const std::string& line_h,
                     double d, const EntMap& entities, const VarMap& var_map) {
    const std::string eid = eid_of(line_h);
    auto it = entities.find(eid);
    if (it == entities.end())
        throw std::runtime_error("length constraint: unknown entity '" + eid + "'");
    const std::string t = it->second.value("type", std::string());
    if (t != "line" && t != "slot")
        throw std::runtime_error("length constraint needs a line/slot ref (got '" + t +
                                 "': '" + line_h + "')");
    auto vit = var_map.find(eid);
    if (vit == var_map.end() || vit->second.size() < 4)
        throw std::runtime_error("length constraint: entity '" + eid + "' has no line vars");
    std::vector<int> idx = pick(vit->second, {0, 1, 2, 3});
    sys.add_residual(
        [d](const VectorXd& xs) {
            return r1(std::hypot(xs(2) - xs(0), xs(3) - xs(1)) - d);
        },
        idx,
        [](const VectorXd& xs) {
            double ax = xs(0), ay = xs(1), bx = xs(2), by = xs(3);
            double L = std::max(std::hypot(bx - ax, by - ay), 1e-12);
            MatrixXd J(1, 4);
            J << -(bx - ax) / L, -(by - ay) / L, (bx - ax) / L, (by - ay) / L;
            return J;
        },
        cid);
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
            // Numerical guard: at coincident centers d(|C0-C1|)/dC is 0/0. A zero
            // position gradient strands Newton (it cannot separate the centers and
            // corrupts the radii instead). Fall back to a deterministic unit
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

// Tangency line <-> conic-family curve. For ellipse_arc this is EXACT via the
// support function; for parabola/conic it is the numeric closest-distance.
// Numeric Jacobian. Port of _add_tangent_line_conic.
void add_tangent_line_conic(System& sys, const std::string& cid,
                            const std::string& line_eid, const std::string& conic_eid,
                            const EntMap& entities, const VarMap& var_map) {
    const std::string etype = entities.at(conic_eid).at("type");
    std::vector<int> lidx = pick(var_map.at(line_eid), {0, 1, 2, 3});

    if (etype == "ellipse_arc" || etype == "ellipse") {
        std::vector<int> eidx = pick(var_map.at(conic_eid), {0, 1, 2, 3, 4});
        std::vector<int> combined = concat(lidx, eidx);
        auto rfn = [](const VectorXd& v) {
            return ellipse_line_support(v.head(4), v.segment(4, 5));
        };
        sys.add_residual(
            [rfn](const VectorXd& xs) { return r1(rfn(xs)); }, combined,
            [rfn](const VectorXd& xs) { return numeric_jac_1(rfn, xs); }, cid);
        return;
    }

    CurveFn curve;
    ScanFn scan;
    std::vector<int> eidx;
    if (etype == "parabola") {
        eidx = pick(var_map.at(conic_eid), {0, 1, 2, 3});
        curve = parabola_curve;
        scan = [](const VectorXd&, int n) { return linspace(-200.0, 200.0, n); };
    } else {  // conic
        eidx = pick(var_map.at(conic_eid), {0, 1, 2, 3, 4, 5, 6});
        curve = conic_curve;
        scan = [](const VectorXd&, int n) { return linspace(0.0, 1.0, n); };
    }
    int ke = static_cast<int>(eidx.size());
    std::vector<int> combined = concat(lidx, eidx);
    auto rfn = [curve, scan, ke](const VectorXd& v) -> double {
        double ax = v(0), ay = v(1), bx = v(2), by = v(3);
        VectorXd cv = v.segment(4, ke);
        double dxab = bx - ax, dyab = by - ay;
        double L = std::hypot(dxab, dyab);
        if (L < 1e-12) return 0.0;
        double nx = -dyab / L, ny = dxab / L;
        double p = extremum_param(curve, cv, nx, ny, scan);
        Vector2d Q = curve(cv, p).first;
        return (Q(0) - ax) * nx + (Q(1) - ay) * ny;
    };
    sys.add_residual(
        [rfn](const VectorXd& xs) { return r1(rfn(xs)); }, combined,
        [rfn](const VectorXd& xs) { return numeric_jac_1(rfn, xs); }, cid);
}

// Tangency line <-> spline (the spline families; the line<->conic path covers
// conics). Residual = signed perpendicular distance from the line to the curve's
// extreme point in the line-normal direction. Port of _add_tangent_line_curve.
void add_tangent_line_curve(System& sys, const std::string& cid,
                            const std::string& line_eid, const std::string& curve_eid,
                            const EntMap& entities, const VarMap& var_map) {
    CurveDescriptor desc =
        curve_descriptor(entities.at(curve_eid).at("type"), var_map.at(curve_eid));
    if (!desc.ok) return;
    CurveFn curve = desc.eval;
    ScanFn scan = desc.scan;
    std::vector<int> eidx = desc.idx;
    std::vector<int> lidx = pick(var_map.at(line_eid), {0, 1, 2, 3});
    int ke = static_cast<int>(eidx.size());
    std::vector<int> combined = concat(lidx, eidx);
    auto rfn = [curve, scan, ke](const VectorXd& v) -> double {
        double ax = v(0), ay = v(1), bx = v(2), by = v(3);
        VectorXd cv = v.segment(4, ke);
        double dxab = bx - ax, dyab = by - ay;
        double L = std::hypot(dxab, dyab);
        if (L < 1e-12) return 0.0;
        double nx = -dyab / L, ny = dxab / L;
        double p = extremum_param(curve, cv, nx, ny, scan);
        Vector2d Q = curve(cv, p).first;
        return (Q(0) - ax) * nx + (Q(1) - ay) * ny;
    };
    sys.add_residual(
        [rfn](const VectorXd& xs) { return r1(rfn(xs)); }, combined,
        [rfn](const VectorXd& xs) { return numeric_jac_1(rfn, xs); }, cid);
}

// Tangency between two general curves (any pair among circle/arc/conic/spline,
// excluding line-circle and circle-circle handled above). Single scalar residual
// = signed gap at the mutual closest point. Numeric Jacobian. Range-free for
// trimmed curves. Port of _add_tangent_curve_curve.
// `interactive`: warm-start the mutual-closest (p,q) across residual
// evaluations within one solve (shared across residual + numeric Jacobian probes).
void add_tangent_curve_curve(System& sys, const std::string& cid,
                             const std::string& eid0, const std::string& eid1,
                             const EntMap& entities, const VarMap& var_map,
                             bool interactive = false) {
    CurveDescriptor d0 = curve_descriptor(entities.at(eid0).at("type"), var_map.at(eid0));
    CurveDescriptor d1 = curve_descriptor(entities.at(eid1).at("type"), var_map.at(eid1));
    if (!d0.ok || !d1.ok) throw std::runtime_error("tangent: unsupported curve pair");
    CurveFn curve0 = d0.eval, curve1 = d1.eval;
    ScanFn scan0 = d0.scan, scan1 = d1.scan;
    int k0 = static_cast<int>(d0.idx.size()), k1 = static_cast<int>(d1.idx.size());
    std::vector<int> combined = concat(d0.idx, d1.idx);
    // Shared (p,q) seed for this residual block. NaN => cold scan on first eval.
    // Only attached when interactive so the exact path never skips the 25×25 scan.
    auto warm_pq = interactive
                       ? std::make_shared<std::pair<double, double>>(
                             std::numeric_limits<double>::quiet_NaN(),
                             std::numeric_limits<double>::quiet_NaN())
                       : std::shared_ptr<std::pair<double, double>>{};
    auto rfn = [k0, k1, curve0, scan0, curve1, scan1, warm_pq](const VectorXd& v) {
        return curve_gap_residual(v, k0, k1, curve0, scan0, curve1, scan1,
                                  warm_pq.get());
    };
    sys.add_residual(
        [rfn](const VectorXd& xs) { return r1(rfn(xs)); }, combined,
        [rfn](const VectorXd& xs) { return numeric_jac_1(rfn, xs); }, cid);
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
            double length = std::max(std::hypot(dxab, dyab), 1e-12);
            double c1 = dxab * (My - ay) - dyab * (Mx - ax);
            double c2 = (P(0) - Q(0)) * dxab + (P(1) - Q(1)) * dyab;
            VectorXd r(2);
            r << c1 / length, c2 / length;
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
            double length = std::max(std::hypot(dxab, dyab), 1e-12);
            double c1 = dxab * (My - ay) - dyab * (Mx - ax);
            double c2 = (P(0) - Q(0)) * dxab + (P(1) - Q(1)) * dyab;
            Eigen::RowVector4d dl(-dxab / length, -dyab / length, dxab / length, dyab / length);
            J /= length;
            J.block(0, k0 + k1, 1, 4) -= (c1 / (length * length)) * dl;
            J.block(1, k0 + k1, 1, 4) -= (c2 / (length * length)) * dl;
            return J;
        },
        cid);
}

bool is_axis(const std::string& h) { return h == "axis.h" || h == "axis.v"; }

bool is_direction_ref(const std::string& h, const EntMap& entities) {
    if (is_axis(h)) return true;
    auto it = entities.find(h);
    return it != entities.end() &&
           (it->second.value("type", "") == "line" || it->second.value("type", "") == "slot");
}

// Virtual axes have no variables, just like the virtual origin point.
struct LineRef {
    std::vector<int> idx;
    std::function<Eigen::Vector4d(const VectorXd&)> val;
};
LineRef line_ref(const std::string& h, const EntMap& entities, const VarMap& vars) {
    if (is_axis(h)) {
        const bool horizontal = h == "axis.h";
        return {{}, [horizontal](const VectorXd&) {
            return Eigen::Vector4d(0, 0, horizontal ? 1 : 0, horizontal ? 0 : 1);
        }};
    }
    if (!is_direction_ref(h, entities))
        throw std::runtime_error("ref '" + h + "' needs a line/slot/axis");
    return {pick(vars.at(h), {0, 1, 2, 3}),
            [](const VectorXd& v) { return Eigen::Vector4d(v.head(4)); }};
}

// Angular residuals are homogeneous in both line lengths. Unlike raw cross/dot
// products, shrinking a free line cannot improve parallel/perpendicular error.
void add_line_orientation(System& sys, const std::string& cid, const std::string& type,
                           const std::string& h0, const std::string& h1, double degrees,
                           const EntMap& entities, const VarMap& vars) {
    LineRef a = line_ref(h0, entities, vars), b = line_ref(h1, entities, vars);
    int ka = static_cast<int>(a.idx.size()), kb = static_cast<int>(b.idx.size());
    auto authored_direction = [&](const std::string& h) {
        if (is_axis(h)) return h == "axis.h" ? Vector2d(1, 0) : Vector2d(0, 1);
        const json& e = entities.at(h);
        return Vector2d(num(jat(jat(e, "p2"), 0)) - num(jat(jat(e, "p1"), 0)),
                        num(jat(jat(e, "p2"), 1)) - num(jat(jat(e, "p1"), 1)));
    };
    Vector2d u0 = authored_direction(h0), v0 = authored_direction(h1);
    if (u0.norm() < 1e-12 || v0.norm() < 1e-12)
        throw std::runtime_error(type + ": zero-length direction ref '" + (u0.norm() < 1e-12 ? h0 : h1) + "'");
    double theta0 = std::atan2(u0.x() * v0.y() - u0.y() * v0.x(), u0.dot(v0));
    double target = type == "perpendicular" ? .5 * kPi : 0;
    if (type != "angle") target += std::round((theta0 - target) / kPi) * kPi;
    else target = degrees * kDeg;
    sys.add_residual([a, b, ka, kb, type, target](const VectorXd& v) {
        Eigen::Vector4d av = a.val(v.head(ka)), bv = b.val(v.segment(ka, kb));
        Vector2d u = av.tail<2>() - av.head<2>(), w = bv.tail<2>() - bv.head<2>();
        double cross = u.x() * w.y() - u.y() * w.x();
        double theta = std::atan2(type == "angle" ? std::abs(cross) : cross, u.dot(w));
        return r1(type == "angle" ? theta - target : std::remainder(theta - target, 2 * kPi));
    }, concat(a.idx, b.idx), [a, b, ka, kb, type](const VectorXd& v) {
        Eigen::Vector4d av = a.val(v.head(ka)), bv = b.val(v.segment(ka, kb));
        Vector2d u = av.tail<2>() - av.head<2>(), w = bv.tail<2>() - bv.head<2>();
        double sign = type == "angle" && u.x() * w.y() - u.y() * w.x() < 0 ? -1 : 1;
        Vector2d du = sign * Vector2d(u.y(), -u.x()) / std::max(u.squaredNorm(), 1e-24);
        Vector2d dw = sign * Vector2d(-w.y(), w.x()) / std::max(w.squaredNorm(), 1e-24);
        MatrixXd j(1, ka + kb);
        if (ka) { j.block<1, 2>(0, 0) = -du.transpose(); j.block<1, 2>(0, 2) = du.transpose(); }
        if (kb) { j.block<1, 2>(0, ka) = -dw.transpose(); j.block<1, 2>(0, ka + 2) = dw.transpose(); }
        return j;
    }, cid);
}

// Both endpoints of the movable line lie on the reference's infinite line.
// Prefer fixed/virtual references in either ref order; normalization removes
// length-scaling incentives while the relation removes exactly two geometric DOFs.
void add_collinear(System& sys, const std::string& cid, const std::string& h0,
                     const std::string& h1, const EntMap& entities, const VarMap& vars,
                     const std::set<std::string>& fixed_lines) {
    bool fixed0 = is_axis(h0) || fixed_lines.count(h0), fixed1 = is_axis(h1) || fixed_lines.count(h1);
    const std::string& reference = fixed0 && !fixed1 ? h0 : h1;
    const std::string& moving = fixed0 && !fixed1 ? h1 : h0;
    LineRef a = line_ref(moving, entities, vars), b = line_ref(reference, entities, vars);
    int ka = static_cast<int>(a.idx.size()), kb = static_cast<int>(b.idx.size());
    sys.add_residual([a, b, ka, kb](const VectorXd& v) {
        Eigen::Vector4d av = a.val(v.head(ka)), bv = b.val(v.segment(ka, kb));
        Vector2d u = bv.tail<2>() - bv.head<2>();
        double length = std::max(u.norm(), 1e-12);
        Vector2d d0 = av.head<2>() - bv.head<2>(), d1 = av.tail<2>() - bv.head<2>();
        VectorXd r(2);
        r << (u.x() * d0.y() - u.y() * d0.x()) / length,
             (u.x() * d1.y() - u.y() * d1.x()) / length;
        return r;
    }, concat(a.idx, b.idx), [a, b, ka, kb](const VectorXd& v) {
        Eigen::Vector4d av = a.val(v.head(ka)), bv = b.val(v.segment(ka, kb));
        Vector2d u = bv.tail<2>() - bv.head<2>();
        double length = std::max(u.norm(), 1e-12);
        Vector2d normal(-u.y() / length, u.x() / length);
        MatrixXd j = MatrixXd::Zero(2, ka + kb);
        for (int i = 0; i < 2; ++i) {
            Vector2d d = av.segment<2>(2 * i) - bv.head<2>();
            double cross = u.x() * d.y() - u.y() * d.x();
            Vector2d gu = Vector2d(d.y(), -d.x()) / length - cross * u / (length * length * length);
            if (ka) j.block<1, 2>(i, 2 * i) = normal.transpose();
            if (kb) {
                j.block<1, 2>(i, ka) = (-gu - normal).transpose();
                j.block<1, 2>(i, ka + 2) = gu.transpose();
            }
        }
        return j;
    }, cid);
}


using ResidualFn = std::function<VectorXd(const VectorXd&)>;
void add_numeric(System& sys, const std::string& cid, const std::vector<int>& idx,
                 ResidualFn fn) {
    sys.add_residual(fn, idx, [fn](const VectorXd& v) {
        MatrixXd j(fn(v).size(), v.size());
        for (int i = 0; i < v.size(); ++i) {
            double h = 1e-6 * std::max(1.0, std::abs(v(i)));
            VectorXd a = v, b = v;
            a(i) += h; b(i) -= h;
            j.col(i) = (fn(a) - fn(b)) / (2 * h);
        }
        return j;
    }, cid);
}


void add_point_axis(System& sys, const std::string& cid, const std::string& point,
                     const std::string& axis, double distance, bool dimension,
                     const EntMap& entities, const VarMap& vars) {
    Handle p = resolve_handle(point, entities, vars);
    const int coordinate = axis == "axis.h" ? 1 : 0;
    sys.add_residual([p, coordinate, distance, dimension](const VectorXd& v) {
        double x = p.val(v)(coordinate);
        return r1((dimension ? std::abs(x) : x) - distance);
    }, p.idx, [p, coordinate, dimension](const VectorXd& v) {
        MatrixXd j = p.jac(v).row(coordinate);
        if (dimension && p.val(v)(coordinate) < 0) j = -j;
        return j;
    }, cid);
}

void add_line_distance(System& sys, const std::string& cid, const std::string& h0,
                        const std::string& h1, double value,
                        const EntMap& entities, const VarMap& vars) {
    LineRef a = line_ref(h0, entities, vars), b = line_ref(h1, entities, vars);
    int ka = static_cast<int>(a.idx.size()), kb = static_cast<int>(b.idx.size());
    sys.add_residual([a, b, ka, kb, value](const VectorXd& v) {
        Eigen::Vector4d av = a.val(v.head(ka)), bv = b.val(v.segment(ka, kb));
        Vector2d u = av.tail<2>() - av.head<2>();
        Vector2d d = (bv.head<2>() + bv.tail<2>()) * .5 - av.head<2>();
        return r1(std::abs(u.x() * d.y() - u.y() * d.x()) / std::max(u.norm(), 1e-12) - value);
    }, concat(a.idx, b.idx), [a, b, ka, kb](const VectorXd& v) {
        Eigen::Vector4d av = a.val(v.head(ka)), bv = b.val(v.segment(ka, kb));
        Vector2d u = av.tail<2>() - av.head<2>();
        Vector2d d = (bv.head<2>() + bv.tail<2>()) * .5 - av.head<2>();
        double length = std::max(u.norm(), 1e-12), cross = u.x() * d.y() - u.y() * d.x();
        double s = cross < 0 ? -1 : 1;
        Vector2d gu = s * Vector2d(d.y(), -d.x()) / length -
                      std::abs(cross) * u / (length * length * length);
        Vector2d gd = s * Vector2d(-u.y(), u.x()) / length;
        MatrixXd j(1, ka + kb);
        if (ka) { j.block<1, 2>(0, 0) = (-gu - gd).transpose(); j.block<1, 2>(0, 2) = gu.transpose(); }
        if (kb) { j.block<1, 2>(0, ka) = .5 * gd.transpose(); j.block<1, 2>(0, ka + 2) = .5 * gd.transpose(); }
        return j;
    }, cid);
}

// Match the directed sweep used by arc geometry (degrees, ccw by default).
double arc_sweep(double start, double end, bool ccw) {
    double sweep = std::fmod(ccw ? end - start : start - end, 360.0);
    if (sweep < 0) sweep += 360.0;
    if (sweep < 1e-12 && std::abs(end - start) > 1e-12) sweep = 360.0;
    return sweep;
}

void add_arc_midpoint(System& sys, const std::string& cid, const std::string& point,
                       const std::string& arc, const EntMap& entities, const VarMap& vars) {
    Handle p = resolve_handle(point, entities, vars);
    auto idx = pick(vars.at(arc), {0, 1, 2, 3, 4});
    int kp = static_cast<int>(p.idx.size());
    bool ccw = entities.at(arc).value("ccw", true);
    sys.add_residual([p, kp, ccw](const VectorXd& v) {
        VectorXd a = v.tail(5);
        double angle = (a(3) + (ccw ? .5 : -.5) * arc_sweep(a(3), a(4), ccw)) * kDeg;
        Vector2d mid(a(0) + a(2) * std::cos(angle), a(1) + a(2) * std::sin(angle));
        return r1Vec2(p.val(v.head(kp)) - mid);
    }, concat(p.idx, idx), [p, kp, ccw](const VectorXd& v) {
        VectorXd a = v.tail(5);
        double angle = (a(3) + (ccw ? .5 : -.5) * arc_sweep(a(3), a(4), ccw)) * kDeg;
        MatrixXd j = MatrixXd::Zero(2, kp + 5);
        j.leftCols(kp) = p.jac(v.head(kp));
        j(0, kp) = -1; j(1, kp + 1) = -1;
        j(0, kp + 2) = -std::cos(angle); j(1, kp + 2) = -std::sin(angle);
        j(0, kp + 3) = j(0, kp + 4) = .5 * a(2) * std::sin(angle) * kDeg;
        j(1, kp + 3) = j(1, kp + 4) = -.5 * a(2) * std::cos(angle) * kDeg;
        return j;
    }, cid);
}

struct LengthRef {
    std::vector<int> idx;
    std::function<double(const VectorXd&)> val;
    std::function<Eigen::RowVectorXd(const VectorXd&)> jac;
};
LengthRef curve_length_ref(const std::string& h, const EntMap& entities, const VarMap& vars) {
    auto it = entities.find(h);
    if (it == entities.end()) throw std::runtime_error("curve length needs line/arc ref '" + h + "'");
    std::string t = it->second.value("type", "");
    if (t == "line") return {pick(vars.at(h), {0, 1, 2, 3}),
        [](const VectorXd& v) { return (v.segment<2>(2) - v.head<2>()).norm(); },
        [](const VectorXd& v) {
            Vector2d d = v.segment<2>(2) - v.head<2>(); d /= std::max(d.norm(), 1e-12);
            Eigen::RowVectorXd j(4); j << -d.x(), -d.y(), d.x(), d.y(); return j;
        }};
    if (t == "arc") {
        bool ccw = it->second.value("ccw", true);
        return {pick(vars.at(h), {2, 3, 4}),
            [ccw](const VectorXd& v) { return v(0) * arc_sweep(v(1), v(2), ccw) * kDeg; },
            [ccw](const VectorXd& v) {
                Eigen::RowVectorXd j(3); double d = (ccw ? 1 : -1) * v(0) * kDeg;
                j << arc_sweep(v(1), v(2), ccw) * kDeg, -d, d; return j;
            }};
    }
    throw std::runtime_error("curve length needs line/arc ref '" + h + "' (got " + t + ")");
}

void add_curve_length(System& sys, const std::string& cid, const LengthRef& a,
                       const std::optional<LengthRef>& b, double value) {
    int ka = static_cast<int>(a.idx.size()), kb = b ? static_cast<int>(b->idx.size()) : 0;
    sys.add_residual([a, b, ka, kb, value](const VectorXd& v) {
        return r1(a.val(v.head(ka)) - (b ? b->val(v.segment(ka, kb)) : value));
    }, b ? concat(a.idx, b->idx) : a.idx, [a, b, ka, kb](const VectorXd& v) {
        MatrixXd j(1, ka + kb); j.leftCols(ka) = a.jac(v.head(ka));
        if (b) j.rightCols(kb) = -b->jac(v.segment(ka, kb));
        return j;
    }, cid);
}

// Endpoint derivatives avoid nested finite differences at clamped spline ends.
struct EndpointJet { Vector2d p, d, dd; };
EndpointJet spline_endpoint(const VectorXd& v, bool style, bool end) {
    const int n = static_cast<int>(v.size()) / 2;
    auto p = [&](int i) { return Vector2d(v(2 * i), v(2 * i + 1)); };
    if (n < 2) throw std::runtime_error("spline endpoint needs at least two points");
    Vector2d a = p(end ? n - 1 : 0), b = p(end ? n - 2 : 1);
    Vector2d d = end ? Vector2d(a - b) : Vector2d(b - a), dd = Vector2d::Zero();
    if (style) {
        int degree = std::min(3, n - 1), spans = n - degree;
        d *= degree * spans;
        if (degree > 1) {
            Vector2d c = p(end ? n - 3 : 2);
            double next_span = spans > 1 ? 2.0 / spans : 1.0;
            dd = (degree - 1) * spans *
                 (degree * (c - b) / next_span - degree * spans * (b - a));
        }
    } else if (n > 2) {
        dd = -a + 2 * b - p(end ? n - 3 : 2);
    }
    return {a, d, dd};
}

std::optional<bool> spline_end(const std::string& h, const EntMap& entities) {
    auto dot = h.find('.');
    if (dot == std::string::npos) return std::nullopt;
    auto it = entities.find(h.substr(0, dot));
    if (it == entities.end()) return std::nullopt;
    std::string t = it->second.value("type", "");
    if (t != "spline" && t != "style_spline") return std::nullopt;
    std::string sub = h.substr(dot + 1);
    if (sub == "start" || sub == "p0") return false;
    if (sub == "end" || sub == "p" + std::to_string(it->second.at("points").size() - 1)) return true;
    return std::nullopt;
}

struct UnionFind {
    std::map<std::string, std::string> parent;
    std::string find(const std::string& x) {
        auto it = parent.find(x);
        if (it == parent.end()) {
            parent[x] = x;
            return x;
        }
        if (it->second != x) it->second = find(it->second);
        return it->second;
    }
    void unite(const std::string& a, const std::string& b) {
        std::string ra = find(a), rb = find(b);
        if (ra != rb) parent[ra] = rb;
    }
};
bool arc_end(const std::string& h, const EntMap& entities) {
    auto it = entities.find(eid_of(h));
    return it != entities.end() && it->second.value("type", "") == "arc" &&
           (h == eid_of(h) + ".start" || h == eid_of(h) + ".end");
}

bool trimmed_end(const std::string& h, const EntMap& entities) {
    auto it = entities.find(eid_of(h));
    if (it == entities.end()) return false;
    std::string t = it->second.value("type", "");
    if (t != "arc" && t != "ellipse_arc" && t != "parabola" && t != "conic") return false;
    return h == eid_of(h) + ".start" || h == eid_of(h) + ".end" ||
           (t == "conic" && (h == eid_of(h) + ".p0" || h == eid_of(h) + ".p1"));
}

struct JetRef {
    std::vector<int> idx;
    std::function<EndpointJet(const VectorXd&, const Vector2d&)> val;
};
JetRef jet_ref(const std::string& h, const EntMap& entities, const VarMap& vars) {
    if (is_direction_ref(h, entities)) {
        LineRef l = line_ref(h, entities, vars);
        return {l.idx, [l](const VectorXd& v, const Vector2d&) {
            Eigen::Vector4d a = l.val(v);
            return EndpointJet{a.head<2>(), a.tail<2>() - a.head<2>(), Vector2d::Zero()};
        }};
    }
    std::string eid = eid_of(h), t = entities.at(eid).value("type", "");
    auto end = spline_end(h, entities);
    if (end) return {vars.at(eid), [end, style = t == "style_spline"](const VectorXd& v, const Vector2d&) {
        return spline_endpoint(v, style, *end);
    }};
    if (t == "circle" || t == "arc") {
        bool endpoint = arc_end(h, entities);
        bool at_end = h == eid + ".end";
        auto idx = endpoint ? pick(vars.at(eid), {0, 1, 2, at_end ? 4 : 3}) :
                              pick(vars.at(eid), {0, 1, 2});
        return {idx, [endpoint](const VectorXd& v, const Vector2d& p) {
            double a = endpoint ? v(3) * kDeg : std::atan2(p.y() - v(1), p.x() - v(0));
            CurvePair c = circle_curve(v, a);
            return EndpointJet{c.first, c.second, Vector2d(-v(2) * std::cos(a), -v(2) * std::sin(a))};
        }};
    }
    if (trimmed_end(h, entities)) {
        bool at_end = h == eid + ".end" || (t == "conic" && h == eid + ".p1");
        if (t == "ellipse_arc") {
            return {pick(vars.at(eid), {0, 1, 2, 3, 4, at_end ? 6 : 5}),
                [](const VectorXd& v, const Vector2d&) {
                    CurvePair c = ellipse_arc_curve(v, v(5) * kDeg);
                    return EndpointJet{c.first, c.second, Vector2d::Zero()};
                }};
        }
        if (t == "parabola") {
            return {pick(vars.at(eid), {0, 1, 2, 3, at_end ? 5 : 4}),
                [](const VectorXd& v, const Vector2d&) {
                    CurvePair c = parabola_curve(v, v(4));
                    return EndpointJet{c.first, c.second, Vector2d::Zero()};
                }};
        }
        if (t == "conic") {
            return {vars.at(eid), [at_end](const VectorXd& v, const Vector2d&) {
                CurvePair c = conic_curve(v, at_end ? 1 : 0);
                return EndpointJet{c.first, c.second, Vector2d::Zero()};
            }};
        }
    }
    CurveDescriptor desc = curve_descriptor(t, vars.at(eid));
    if (!desc.ok) throw std::runtime_error("tangent: unsupported curve ref '" + h + "'");
    return {desc.idx, [desc](const VectorXd& v, const Vector2d& p) {
        VectorXd scan = desc.scan(v, 81);
        double best = scan(0), dist = std::numeric_limits<double>::infinity();
        for (int i = 0; i < scan.size(); ++i) {
            double d = (desc.eval(v, scan(i)).first - p).squaredNorm();
            if (d < dist) { dist = d; best = scan(i); }
        }
        double u = newton_project([&](double x) { return desc.eval(v, x); }, best, p);
        CurvePair c = desc.eval(v, u);
        return EndpointJet{c.first, c.second, Vector2d::Zero()};
    }};
}

void add_endpoint_continuity(System& sys, const std::string& cid, const std::string& first,
                              const std::string& other, bool smooth,
                              const EntMap& entities, const VarMap& vars) {
    if (!spline_end(first, entities) && !(trimmed_end(first, entities) && !smooth))
        throw std::runtime_error("endpoint relation needs spline endpoint ref '" + first + "'");
    auto target_end = spline_end(other, entities);
    std::string t = is_axis(other) ? "line" : entities.at(eid_of(other)).value("type", "");
    if (smooth && !is_direction_ref(other, entities) && t != "circle" && t != "arc" && !target_end)
        throw std::runtime_error("smooth: unsupported target ref '" + other + "'");
    JetRef a = jet_ref(first, entities, vars), b = jet_ref(other, entities, vars);
    int ka = static_cast<int>(a.idx.size()), kb = static_cast<int>(b.idx.size());
    add_numeric(sys, cid, concat(a.idx, b.idx), [a, b, ka, kb, smooth](const VectorXd& v) {
        EndpointJet x = a.val(v.head(ka), Vector2d::Zero());
        EndpointJet y = b.val(v.segment(ka, kb), x.p);
        double nx = std::max(x.d.norm(), 1e-12), ny = std::max(y.d.norm(), 1e-12);
        double g1 = (x.d.x() * y.d.y() - x.d.y() * y.d.x()) / (nx * ny);
        if (!smooth) return r1(g1);
        double cx = (x.d.x() * x.dd.y() - x.d.y() * x.dd.x()) / (nx * nx * nx);
        double cy = (y.d.x() * y.dd.y() - y.d.y() * y.dd.x()) / (ny * ny * ny);
        VectorXd r(2); r << g1, cx - (x.d.dot(y.d) < 0 ? -cy : cy); return r;
    });
}

void add_extended_symmetric(System& sys, const std::string& cid, const std::string& h0,
                             const std::string& h1, const std::string& axis,
                             const EntMap& entities, const VarMap& vars) {
    LineRef ax = line_ref(axis, entities, vars);
    auto reflect = [](const Vector2d& p, const Eigen::Vector4d& a) {
        Vector2d u = a.tail<2>() - a.head<2>();
        return Vector2d(a.head<2>() + 2 * u * u.dot(p - a.head<2>()) /
                        std::max(u.squaredNorm(), 1e-24) - (p - a.head<2>()));
    };
    bool lines = is_direction_ref(h0, entities) && is_direction_ref(h1, entities);
    auto circle_ref = [&](const std::string& h) {
        auto it = entities.find(h);
        return it != entities.end() && (it->second.value("type", "") == "circle" ||
                                        it->second.value("type", "") == "arc");
    };
    bool circles = circle_ref(h0) && circle_ref(h1);
    if (lines) {
        LineRef a = line_ref(h0, entities, vars), b = line_ref(h1, entities, vars);
        int ka = static_cast<int>(a.idx.size()), kb = static_cast<int>(b.idx.size());
        int kc = static_cast<int>(ax.idx.size());
        VectorXd seed = sys.init_vector();
        auto local = [&](const std::vector<int>& idx) {
            VectorXd v(idx.size()); for (size_t i = 0; i < idx.size(); ++i) v(i) = seed(idx[i]); return v;
        };
        Eigen::Vector4d av = a.val(local(a.idx)), bv = b.val(local(b.idx)), cv = ax.val(local(ax.idx));
        bool reverse = (reflect(av.head<2>(), cv) - bv.tail<2>()).squaredNorm() +
                       (reflect(av.tail<2>(), cv) - bv.head<2>()).squaredNorm() <
                       (reflect(av.head<2>(), cv) - bv.head<2>()).squaredNorm() +
                       (reflect(av.tail<2>(), cv) - bv.tail<2>()).squaredNorm();
        add_numeric(sys, cid, concat(a.idx, b.idx, ax.idx),
            [a, b, ax, ka, kb, kc, reverse, reflect](const VectorXd& v) {
                Eigen::Vector4d a0 = a.val(v.head(ka)), b0 = b.val(v.segment(ka, kb));
                Eigen::Vector4d axis0 = ax.val(v.segment(ka + kb, kc));
                VectorXd r(4);
                Vector2d p = reverse ? Vector2d(b0.tail<2>()) : Vector2d(b0.head<2>());
                Vector2d q = reverse ? Vector2d(b0.head<2>()) : Vector2d(b0.tail<2>());
                r.head<2>() = reflect(a0.head<2>(), axis0) - p;
                r.tail<2>() = reflect(a0.tail<2>(), axis0) - q;
                return r;
            });
        return;
    }
    // Whole curved entities use centers; explicit point handles keep their points.
    auto point_ref = [&](const std::string& h) {
        if (h == "origin" || h == "origin.p") return true;
        auto it = entities.find(eid_of(h));
        return it != entities.end() && (h.find('.') != std::string::npos || it->second.value("type", "") == "point");
    };
    if (!circles && (!point_ref(h0) || !point_ref(h1)))
        throw std::runtime_error("symmetric: incompatible refs '" + h0 + "', '" + h1 + "'");
    Handle a = resolve_handle(h0, entities, vars), b = resolve_handle(h1, entities, vars);
    int ka = static_cast<int>(a.idx.size()), kb = static_cast<int>(b.idx.size()), kc = static_cast<int>(ax.idx.size());
    add_numeric(sys, cid, concat(a.idx, b.idx, ax.idx), [a, b, ax, ka, kb, kc, reflect](const VectorXd& v) {
        return r1Vec2(reflect(a.val(v.head(ka)), ax.val(v.segment(ka + kb, kc))) -
                      b.val(v.segment(ka, kb)));
    });
    if (circles) add_equal(sys, cid, h0, h1, entities, vars);
}

// Convex support gap gives actual tangency, not a zero gap at a crossing of two
// ellipses. Freeze external/contained contact intent from the authored geometry.
void add_ellipse_pair_tangent(System& sys, const std::string& cid, const std::string& h0,
                              const std::string& h1, const EntMap& entities, const VarMap& vars) {
    bool ell0 = entities.at(h0).value("type", "") == "ellipse" || entities.at(h0).value("type", "") == "ellipse_arc";
    bool ell1 = entities.at(h1).value("type", "") == "ellipse" || entities.at(h1).value("type", "") == "ellipse_arc";
    auto a = ell0 ? pick(vars.at(h0), {0, 1, 2, 3, 4}) : pick(vars.at(h0), {0, 1, 2});
    auto b = ell1 ? pick(vars.at(h1), {0, 1, 2, 3, 4}) : pick(vars.at(h1), {0, 1, 2});
    int ka = static_cast<int>(a.size()), kb = static_cast<int>(b.size());
    auto gap = [ell0, ell1, ka, kb](const VectorXd& v, int mode) {
        auto support = [](const VectorXd& p, bool ellipse, double angle) {
            if (!ellipse) return std::abs(p(2));
            double a = angle - p(4) * kDeg;
            return std::hypot(p(2) * std::cos(a), p(3) * std::sin(a));
        };
        VectorXd x = v.head(ka), y = v.segment(ka, kb);
        auto f = [&](double angle) {
            double center = (y(0) - x(0)) * std::cos(angle) + (y(1) - x(1)) * std::sin(angle);
            double sx = support(x, ell0, angle), sy = support(y, ell1, angle);
            if (mode == 1) return center + sy - sx;
            if (mode == 2) return -center + sx - sy;
            return center - sx - sy;
        };
        constexpr int samples = 96;
        double best = 0, value = f(0), step = 2 * kPi / samples;
        for (int i = 1; i < samples; ++i) {
            double angle = i * step, candidate = f(angle);
            if (candidate > value) { best = angle; value = candidate; }
        }
        double left = best - step, right = best + step;
        constexpr double ratio = .6180339887498948482;
        double p = right - ratio * (right - left), q = left + ratio * (right - left);
        double fp = f(p), fq = f(q);
        for (int i = 0; i < 48; ++i) {
            if (fp < fq) { left = p; p = q; fp = fq; q = left + ratio * (right - left); fq = f(q); }
            else { right = q; q = p; fq = fp; p = right - ratio * (right - left); fp = f(p); }
        }
        return std::max(fp, fq);
    };
    auto idx = concat(a, b);
    VectorXd init = sys.init_vector(), seed(idx.size());
    for (size_t i = 0; i < idx.size(); ++i) seed(i) = init(idx[i]);
    int mode = gap(seed, 1) < 0 ? 1 : (gap(seed, 2) < 0 ? 2 : 0);
    auto fn = [gap, mode](const VectorXd& v) { return gap(v, mode); };
    sys.add_residual([fn](const VectorXd& v) { return r1(fn(v)); }, idx,
                     [fn](const VectorXd& v) { return numeric_jac_1(fn, v); }, cid);
}

using JunctionMap = std::map<std::pair<std::string, std::string>,
                             std::pair<std::string, std::string>>;

// Coincidence topology, not coordinate proximity, identifies a tangent junction.
// Normalize aliases and union through standalone points or other coincidences.
JunctionMap tangent_junctions(const EntMap& entities, const VarMap& vars, const json& constraints) {
    JunctionMap result;
    std::set<std::pair<std::string, std::string>> pairs;
    for (const auto& c : constraints) {
        const json& refs = jat(c, "refs");
        if (jat(c, "type") != "tangent" || jat(c, "driven") == true ||
            !refs.is_array() || refs.size() != 2 || !refs[0].is_string() || !refs[1].is_string()) continue;
        std::string a = refs[0].get<std::string>(), b = refs[1].get<std::string>();
        if (a.find('.') != std::string::npos || b.find('.') != std::string::npos ||
            a == b || !entities.count(a) || !entities.count(b)) continue;
        if (b < a) std::swap(a, b);
        pairs.emplace(std::move(a), std::move(b));
    }
    if (pairs.empty()) return result;
    auto type_of = [&](const std::string& id) {
        auto it = entities.find(id);
        return it != entities.end() && jat(it->second, "type").is_string() ?
            it->second["type"].get<std::string>() : std::string();
    };
    auto canonical = [&](const std::string& h) {
        std::string id = eid_of(h), sub = h.find('.') == std::string::npos ? "" : h.substr(h.find('.') + 1);
        std::string t = type_of(id);
        if (id == "origin" || t == "point") return id;
        if (t == "line") {
            if (sub.empty() || sub == "start" || sub == "p1") return id + ".p1";
            if (sub == "end" || sub == "p2") return id + ".p2";
        }
        if (t == "spline" || t == "style_spline") {
            const json& points = jat(entities.at(id), "points");
            if (sub.empty() || sub == "start" || sub == "p0") return id + ".start";
            if (sub == "end" || (points.is_array() && !points.empty() &&
                sub == "p" + std::to_string(points.size() - 1))) return id + ".end";
        }
        if (t == "conic") {
            if (sub.empty() || sub == "start" || sub == "p0") return id + ".start";
            if (sub == "end" || sub == "p1") return id + ".end";
        }
        return h;
    };
    UnionFind joined;
    for (const auto& c : constraints) {
        const json& refs = jat(c, "refs");
        if (jat(c, "type") != "coincident" || jat(c, "driven") == true ||
            !refs.is_array() || refs.size() != 2 || !refs[0].is_string() || !refs[1].is_string()) continue;
        const std::string a = refs[0].get<std::string>(), b = refs[1].get<std::string>();
        try {
            resolve_handle(a, entities, vars);
            resolve_handle(b, entities, vars);
        } catch (...) { continue; }
        joined.unite(canonical(a), canonical(b));
    }
    auto ends = [&](const std::string& id) -> std::pair<std::string, std::string> {
        std::string t = type_of(id);
        if (t == "line") return {id + ".p1", id + ".p2"};
        if (t == "arc" || t == "ellipse_arc" || t == "parabola" || t == "conic" ||
            t == "spline" || t == "style_spline") return {id + ".start", id + ".end"};
        return {};
    };
    for (const auto& pair : pairs) {
        auto a = ends(pair.first), b = ends(pair.second);
        if (a.first.empty() || b.first.empty()) continue;
        for (const auto& x : {a.first, a.second}) {
            for (const auto& y : {b.first, b.second}) {
                if (joined.find(x) == joined.find(y)) {
                    result.emplace(pair, std::make_pair(x, y));
                    break;
                }
            }
            if (result.count(pair)) break;
        }
    }
    return result;
}

struct ConstraintContext {
    JunctionMap junctions;
    std::set<std::string> fixed_lines;
};

ConstraintContext constraint_context(const EntMap& entities, const VarMap& vars, const json& constraints) {
    ConstraintContext result{tangent_junctions(entities, vars, constraints), {}};
    bool needs_reference = false;
    for (const auto& c : constraints)
        if (jat(c, "type") == "collinear") { needs_reference = true; break; }
    if (!needs_reference) return result;
    std::map<std::string, unsigned> endpoints;
    for (const auto& c : constraints) {
        const json& refs = jat(c, "refs");
        if (jat(c, "type") != "fix" || jat(c, "driven") == true || !refs.is_array() ||
            refs.size() != 1 || !refs[0].is_string()) continue;
        std::string h = refs[0].get<std::string>(), id = eid_of(h);
        auto it = entities.find(id);
        if (it == entities.end()) continue;
        const json& t = jat(it->second, "type");
        if (t != "line" && t != "slot") continue;
        if (h == id && t == "line") endpoints[id] = 3;
        else if (h == id + ".p1" || (h == id && t == "slot")) endpoints[id] |= 1;
        else if (h == id + ".p2") endpoints[id] |= 2;
    }
    for (const auto& p : endpoints)
        if (p.second == 3) result.fixed_lines.insert(p.first);
    return result;
}

void add_constraint(System& sys, const json& c, const EntMap& entities,
                    const VarMap& var_map, const json& env,
                    bool interactive, const ConstraintContext& context) {
    if (!c.is_object() || !c.contains("type") || !c["type"].is_string())
        throw std::runtime_error("constraint missing type");
    const std::string ctype = c.at("type").get<std::string>();
    std::vector<std::string> refs;
    if (c.contains("refs") && c["refs"].is_array()) {
        for (const auto& r : c["refs"]) {
            if (!r.is_string())
                throw std::runtime_error(ctype + " constraint: refs must be strings");
            refs.push_back(r.get<std::string>());
        }
    }
    const std::string cid = c.value("id", std::string());
    if (c.value("driven", false)) return;  // reference dims measured after solve
    std::size_t maximum = ctype == "symmetric" || ctype == "intersection" ? 3 :
        (ctype == "fix" || ctype == "radius" || ctype == "diameter" ||
         ctype == "length" || ctype == "arc_length" ? 1 : 2);
    if (refs.size() > maximum)
        throw std::runtime_error(ctype + ": too many refs (" + std::to_string(refs.size()) + ")");

    // Bound-safe ref access. Every branch MUST go through this (or an explicit
    // refs.size() check) before indexing — a bad modeler payload used to
    // SIGSEGV hostd on refs[1] of a 1-element distance.
    auto need = [&](std::size_t n) {
        if (refs.size() < n) {
            std::string got;
            for (std::size_t i = 0; i < refs.size(); ++i) {
                if (i) got += ", ";
                got += "'" + refs[i] + "'";
            }
            if (got.empty()) got = "(none)";
            throw std::runtime_error(ctype + " constraint needs " + std::to_string(n) +
                                     " ref(s) (got " + std::to_string(refs.size()) +
                                     "): " + got +
                                     (cid.empty() ? std::string() : " [" + cid + "]"));
        }
    };
    auto known_entity = [&](const std::string& h) {
        const std::string eid = eid_of(h);
        if (eid == "origin" || is_axis(h)) return;
        if (!entities.count(eid))
            throw std::runtime_error(ctype + " constraint: unknown entity '" + eid +
                                     "' (ref '" + h + "')" +
                                     (cid.empty() ? std::string() : " [" + cid + "]"));
        if (!var_map.count(eid) && eid != "origin")
            throw std::runtime_error(ctype + " constraint: entity '" + eid +
                                     "' has no solver vars" +
                                     (cid.empty() ? std::string() : " [" + cid + "]"));
    };
    auto require_line = [&](const std::string& h) {
        known_entity(h);
        if (!is_direction_ref(h, entities))
            throw std::runtime_error(ctype + ": ref '" + h + "' needs line/slot/axis");
    };
    auto point_on_supported = [&](const std::string& h) {
        if (is_axis(h)) return;
        auto it = entities.find(h);
        known_entity(h);
        if (it != entities.end() && it->second.value("type", "") == "line") {
            pick(var_map.at(h), {0, 1, 2, 3});
            return;
        }
        if (it == entities.end() || !curve_descriptor(it->second.value("type", ""), var_map.at(h)).ok)
            throw std::runtime_error(ctype + ": unsupported curve ref '" + h + "'");
    };
    if (ctype == "parallel" || ctype == "perpendicular" || ctype == "collinear" || ctype == "angle") {
        need(2); require_line(refs[0]); require_line(refs[1]);
        if (ctype == "collinear")
            add_collinear(sys, cid, refs[0], refs[1], entities, var_map, context.fixed_lines);
        else add_line_orientation(sys, cid, ctype, refs[0], refs[1],
                                  ctype == "angle" ? resolve_dim(c, env) : 0, entities, var_map);
        return;
    }
    if (ctype == "intersection") {
        need(3);
        resolve_handle(refs[0], entities, var_map);
        point_on_supported(refs[1]); point_on_supported(refs[2]);
        for (int i = 1; i <= 2; ++i) {
            if (is_axis(refs[i])) add_point_axis(sys, cid, refs[0], refs[i], 0, false, entities, var_map);
            else add_point_on(sys, cid, refs[0], refs[i], entities, var_map);
        }
        return;
    }
    if (ctype == "arc_length" || ctype == "equal_curve_length") {
        need(ctype == "arc_length" ? 1 : 2);
        if (ctype == "arc_length" &&
            (!entities.count(refs[0]) || entities.at(refs[0]).value("type", "") != "arc"))
            throw std::runtime_error("arc_length needs arc ref '" + refs[0] + "'");
        LengthRef a = curve_length_ref(refs[0], entities, var_map);
        std::optional<LengthRef> b;
        if (ctype == "equal_curve_length") b = curve_length_ref(refs[1], entities, var_map);
        add_curve_length(sys, cid, a, b, b ? 0 : resolve_dim(c, env));
        return;
    }
    if (ctype == "smooth" || ctype == "tangent") {
        need(2);
        if (ctype == "tangent" &&
            refs[0] == eid_of(refs[0]) && refs[1] == eid_of(refs[1])) {
            const bool reverse = refs[1] < refs[0];
            auto key = reverse ? std::make_pair(refs[1], refs[0]) : std::make_pair(refs[0], refs[1]);
            auto it = context.junctions.find(key);
            if (it != context.junctions.end()) {
                refs[0] = reverse ? it->second.second : it->second.first;
                refs[1] = reverse ? it->second.first : it->second.second;
                for (auto& h : refs) {
                    const std::string id = eid_of(h);
                    if (entities.at(id).value("type", "") == "line") h = id;
                }
            }
        }
        bool first = spline_end(refs[0], entities).has_value() || (ctype == "tangent" && trimmed_end(refs[0], entities));
        bool second = spline_end(refs[1], entities).has_value() || (ctype == "tangent" && trimmed_end(refs[1], entities));
        if (ctype == "smooth" || first || second) {
            add_endpoint_continuity(sys, cid, first || ctype == "smooth" ? refs[0] : refs[1],
                                    first || ctype == "smooth" ? refs[1] : refs[0],
                                    ctype == "smooth", entities, var_map);
            return;
        }
        if (is_axis(refs[0]) || is_axis(refs[1])) {
            std::string axis = is_axis(refs[0]) ? refs[0] : refs[1];
            std::string other = is_axis(refs[0]) ? refs[1] : refs[0];
            known_entity(other);
            if (is_direction_ref(other, entities))
                throw std::runtime_error("tangent: line-line refs are not supported");
            std::string t = entities.at(eid_of(other)).value("type", "");
            CurveDescriptor desc = curve_descriptor(t, var_map.at(eid_of(other)));
            if (!desc.ok) throw std::runtime_error("tangent: unsupported ref '" + other + "'");
            bool horizontal = axis == "axis.h";
            auto fn = [desc, t, horizontal](const VectorXd& v) {
                VectorXd l(4); l << 0, 0, horizontal ? 1 : 0, horizontal ? 0 : 1;
                if (t == "circle" || t == "arc") return std::abs(v(horizontal ? 1 : 0)) - v(2);
                if (t == "ellipse" || t == "ellipse_arc") return ellipse_line_support(l, v);
                double u = extremum_param(desc.eval, v, horizontal ? 0 : -1, horizontal ? 1 : 0, desc.scan);
                return desc.eval(v, u).first(horizontal ? 1 : 0);
            };
            sys.add_residual([fn](const VectorXd& v) { return r1(fn(v)); }, desc.idx,
                             [fn](const VectorXd& v) { return numeric_jac_1(fn, v); }, cid);
            return;
        }
    }

    if (ctype == "coincident") {
        need(2);
        known_entity(refs[0]);
        known_entity(refs[1]);
        add_two_point(sys, cid, refs[0], refs[1], 0, entities, var_map);
    } else if (ctype == "horizontal" || ctype == "vertical") {
        need(1);
        bool horiz = ctype == "horizontal";
        if (refs.size() >= 2) {
            known_entity(refs[0]);
            known_entity(refs[1]);
            add_two_point(sys, cid, refs[0], refs[1], horiz ? 1 : 2, entities, var_map);
        } else if (refs[0].find('.') == std::string::npos) {
            known_entity(refs[0]);
            std::string eid = refs[0];
            std::string et = entities.at(eid).at("type");
            // line and slot both store [p1x,p1y,p2x,p2y,...]; whole-entity h/v
            // enforces p1y==p2y (h) or p1x==p2x (v).
            if (et == "line" || et == "slot")
                add_whole_line_hv(sys, cid, eid, horiz, var_map);
            else
                throw std::runtime_error(ctype +
                                         " constraint on a single ref needs a line/slot "
                                         "(got '" + et + "': '" + eid + "')");
        } else {
            // Single point-handle form is not meaningful for h/v; require 2.
            need(2);
        }
    } else if (ctype == "concentric") {
        need(2);
        known_entity(refs[0]);
        known_entity(refs[1]);
        for (const auto& h : refs) {
            if (h == "origin" || h == "origin.p") continue;
            std::string et = entities.at(eid_of(h)).value("type", "");
            if (h.find('.') == std::string::npos && et != "circle" && et != "arc" &&
                et != "ellipse" && et != "ellipse_arc" && et != "arc_slot" && et != "point")
                throw std::runtime_error("concentric: ref '" + h + "' needs a center or point handle");
            resolve_handle(h, entities, var_map);
        }
        add_two_point(sys, cid, refs[0], refs[1], 0, entities, var_map);
    } else if (ctype == "coradial") {
        need(2);
        known_entity(refs[0]);
        known_entity(refs[1]);
        for (const auto& h : refs) {
            if (!entities.count(h) || (entities.at(h).value("type", "") != "circle" &&
                                       entities.at(h).value("type", "") != "arc"))
                throw std::runtime_error("coradial: ref '" + h + "' needs circle/arc");
        }
        add_coradial(sys, cid, refs[0], refs[1], var_map);
    } else if (ctype == "equal") {
        need(2);
        known_entity(refs[0]);
        known_entity(refs[1]);
        add_equal(sys, cid, refs[0], refs[1], entities, var_map);
    } else if (ctype == "midpoint") {
        need(2);
        known_entity(refs[0]);
        known_entity(refs[1]);
        if (entities.count(refs[1]) && entities.at(refs[1]).value("type", "") == "arc")
            add_arc_midpoint(sys, cid, refs[0], refs[1], entities, var_map);
        else {
            require_line(refs[1]);
            if (!is_axis(refs[1]) && entities.at(refs[1]).value("type", "") != "line")
                throw std::runtime_error("midpoint: ref '" + refs[1] + "' needs line/arc");
            if (is_axis(refs[1])) add_two_point(sys, cid, refs[0], "origin", 0, entities, var_map);
            else add_midpoint(sys, cid, refs[0], refs[1], entities, var_map);
        }
    } else if (ctype == "symmetric") {
        need(3);
        known_entity(refs[0]);
        known_entity(refs[1]);
        known_entity(refs[2]);
        if (!is_axis(refs[2]) && refs[0].find('.') != std::string::npos &&
            refs[1].find('.') != std::string::npos) {
            require_line(refs[2]);
            add_symmetric(sys, cid, refs[0], refs[1], refs[2], entities, var_map);
        } else add_extended_symmetric(sys, cid, refs[0], refs[1], refs[2], entities, var_map);
    } else if (ctype == "fix") {
        need(1);
        known_entity(refs[0]);
        if (!is_axis(refs[0])) add_fix(sys, cid, refs[0], entities, var_map);
    } else if (ctype == "point_on") {
        need(2);
        known_entity(refs[0]);
        known_entity(refs[1]);
        point_on_supported(refs[1]);
        if (is_axis(refs[1])) add_point_axis(sys, cid, refs[0], refs[1], 0, false, entities, var_map);
        else add_point_on(sys, cid, refs[0], refs[1], entities, var_map);
    } else if (ctype == "tangent") {
        need(2);
        known_entity(refs[0]);
        known_entity(refs[1]);
        std::string e0 = eid_of(refs[0]), e1 = eid_of(refs[1]);
        std::string t0 = entities.at(e0).at("type"), t1 = entities.at(e1).at("type");
        auto is_curved = [](const std::string& t) { return t == "circle" || t == "arc"; };
        auto is_conic = [](const std::string& t) {
            return t == "ellipse" || t == "ellipse_arc" || t == "parabola" || t == "conic";
        };
        auto is_spline = [](const std::string& t) {
            return t == "spline" || t == "style_spline";
        };
        auto is_generic = [&](const std::string& t) {
            return is_curved(t) || is_conic(t) || is_spline(t);
        };
        bool curved0 = is_curved(t0), curved1 = is_curved(t1);
        // Mirror Python _add_tangent dispatch order verbatim.
        if (t0 == "line" && curved1)
            add_tangent_line_circle(sys, cid, e0, e1, var_map);
        else if (curved0 && t1 == "line")
            add_tangent_line_circle(sys, cid, e1, e0, var_map);
        else if (curved0 && curved1)
            add_tangent_circle_circle(sys, cid, e0, e1, entities, var_map);
        else if (t0 == "line" && is_conic(t1))
            add_tangent_line_conic(sys, cid, e0, e1, entities, var_map);
        else if (is_conic(t0) && t1 == "line")
            add_tangent_line_conic(sys, cid, e1, e0, entities, var_map);
        else if (t0 == "line" && is_spline(t1))
            add_tangent_line_curve(sys, cid, e0, e1, entities, var_map);
        else if (is_spline(t0) && t1 == "line")
            add_tangent_line_curve(sys, cid, e1, e0, entities, var_map);
        else if ((t0 == "ellipse" || t1 == "ellipse") &&
                 (curved0 || t0 == "ellipse" || t0 == "ellipse_arc") &&
                 (curved1 || t1 == "ellipse" || t1 == "ellipse_arc"))
            add_ellipse_pair_tangent(sys, cid, e0, e1, entities, var_map);
        else if (is_generic(t0) && is_generic(t1))
            add_tangent_curve_curve(sys, cid, e0, e1, entities, var_map, interactive);
        else throw std::runtime_error("tangent: unsupported refs '" + refs[0] + "', '" + refs[1] + "'");
    } else if (ctype == "distance") {
        // SEMANTIC: a single bare line/slot ref is a LINE-LENGTH dimension
        // (SolidWorks-style "select edge → Smart Dimension"). Two-ref form is
        // the classic point-point / point-line distance. Anything else is an error.
        if (refs.size() == 1) {
            known_entity(refs[0]);
            if (is_line_ref(refs[0], entities) ||
                (refs[0].find('.') == std::string::npos && entities.count(refs[0]) &&
                 entities.at(refs[0]).value("type", std::string()) == "slot")) {
                add_line_length(sys, cid, refs[0], resolve_dim(c, env), entities, var_map);
            } else {
                throw std::runtime_error(
                    "distance constraint needs 2 refs (got 1): '" + refs[0] + "'" +
                    (cid.empty() ? std::string() : " [" + cid + "]") +
                    " — single-ref distance is only valid on a bare line/slot (line length)");
            }
        } else {
            need(2);
            known_entity(refs[0]);
            known_entity(refs[1]);
            if (is_direction_ref(refs[0], entities) && is_direction_ref(refs[1], entities)) {
                add_line_distance(sys, cid, refs[0], refs[1], resolve_dim(c, env), entities, var_map);
                return;
            }
            if (is_axis(refs[0]) || is_axis(refs[1])) {
                bool first = is_axis(refs[0]);
                add_point_axis(sys, cid, first ? refs[1] : refs[0], first ? refs[0] : refs[1],
                                 resolve_dim(c, env), true, entities, var_map);
                return;
            }
            std::string axis;
            if (c.contains("axis") && c["axis"].is_string())
                axis = c["axis"].get<std::string>();
            std::string cond0, cond1;
            if (c.contains("cond") && c["cond"].is_array()) {
                const json& cd = c["cond"];
                if (cd.size() > 0 && cd[0].is_string()) cond0 = cd[0].get<std::string>();
                if (cd.size() > 1 && cd[1].is_string()) cond1 = cd[1].get<std::string>();
            }
            add_distance(sys, cid, refs[0], refs[1], resolve_dim(c, env), entities, var_map,
                         axis, cond0, cond1);
        }
    } else if (ctype == "length") {
        // Explicit line-length constraint (matches sketch3d "length").
        need(1);
        known_entity(refs[0]);
        add_line_length(sys, cid, refs[0], resolve_dim(c, env), entities, var_map);
    } else if (ctype == "radius" || ctype == "diameter") {
        need(1);
        known_entity(refs[0]);
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
        } else {
            throw std::runtime_error(ctype + " constraint needs a circle/arc/slot ref (got '" +
                                     et + "': '" + refs[0] + "')");
        }
    } else {
        throw std::runtime_error("unsupported constraint type '" + ctype + "'");
    }
}

// ---- result emission --------------------------------------------------------

json extract_entities(const VectorXd& x, const json& entities_list,
                      const VarMap& var_map) {
    json out = json::object();
    for (const auto& e : entities_list) {
        if (!e.is_object() || !e.contains("id") || !e["id"].is_string()) continue;
        const std::string id = e["id"].get<std::string>();
        const std::string t = e.value("type", std::string());
        auto vit = var_map.find(id);
        if (vit == var_map.end()) { out[id] = e; continue; }  // no vars -> raw pass-through
        const std::vector<int>& v = vit->second;
        // Bounds-safe read: a malformed var_map (too few vars for the type, or an index
        // past x) previously drove an UNCHECKED Eigen/std::vector out-of-bounds read ->
        // SIGSEGV crashing the whole daemon on some sketches. Return 0 for any
        // out-of-range slot instead. Valid entities are unaffected (g(k) == x(v[k])).
        auto g = [&](std::size_t k) -> double {
            return (k < v.size() && v[k] >= 0 && v[k] < x.size()) ? x(v[k]) : 0.0;
        };
        json o;
        o["type"] = t;
        if (t == "point") {
            o["p"] = {g(0), g(1)};
        } else if (t == "line") {
            o["p1"] = {g(0), g(1)};
            o["p2"] = {g(2), g(3)};
        } else if (t == "circle") {
            o["center"] = {g(0), g(1)};
            o["radius"] = g(2);
        } else if (t == "arc") {
            o["center"] = {g(0), g(1)};
            o["radius"] = g(2);
            o["start"] = g(3);
            o["end"] = g(4);
            o["ccw"] = e.value("ccw", true);
        } else if (t == "ellipse") {
            o["center"] = {g(0), g(1)};
            o["rx"] = g(2);
            o["ry"] = g(3);
            o["rot"] = g(4);
        } else if (t == "ellipse_arc") {
            o["center"] = {g(0), g(1)};
            o["rx"] = g(2);
            o["ry"] = g(3);
            o["rot"] = g(4);
            o["a0"] = g(5);
            o["a1"] = g(6);
        } else if (t == "parabola") {
            o["vertex"] = {g(0), g(1)};
            o["focus"] = {g(2), g(3)};
            o["t0"] = g(4);
            o["t1"] = g(5);
        } else if (t == "conic") {
            o["p0"] = {g(0), g(1)};
            o["p1"] = {g(2), g(3)};
            o["apex"] = {g(4), g(5)};
            o["rho"] = g(6);
        } else if (t == "slot") {
            o["p1"] = {g(0), g(1)};
            o["p2"] = {g(2), g(3)};
            o["width"] = g(4);
        } else if (t == "arc_slot") {
            o["center"] = {g(0), g(1)};
            o["radius"] = g(2);
            o["a0"] = g(3);
            o["a1"] = g(4);
            o["width"] = g(5);
        } else if (t == "spline" || t == "style_spline") {
            int n = static_cast<int>(v.size()) / 2;
            json pts = json::array();
            for (int i = 0; i < n; ++i) pts.push_back({g(2 * i), g(2 * i + 1)});
            o["points"] = pts;
        } else if (t == "text") {
            o["p"] = {g(0), g(1)};
        } else {
            o = e;  // pass through unknown types unchanged
        }
        if (e.contains("construction")) o["construction"] = e["construction"];
        out[id] = o;
    }
    return out;
}

// Exactly-zero columns are unconstrained coordinates. Keep their projection at one
// without constructing a quadratic full-V basis for unused spline control points.
VectorXd nullspace_projection(const MatrixXd& J) {
    VectorXd projection = VectorXd::Ones(J.cols());
    std::vector<int> active;
    active.reserve(J.cols());
    for (int i = 0; i < J.cols(); ++i)
        if (!J.col(i).isZero(0.0)) active.push_back(i);
    if (active.empty()) return projection;

    MatrixXd reduced;
    const MatrixXd* matrix = &J;
    if (active.size() != static_cast<size_t>(J.cols())) {
        reduced.resize(J.rows(), active.size());
        for (size_t i = 0; i < active.size(); ++i)
            reduced.col(i) = J.col(active[i]);
        matrix = &reduced;
    }
    Eigen::JacobiSVD<MatrixXd> svd(*matrix, Eigen::ComputeFullV);
    const VectorXd& s = svd.singularValues();
    const double s0 = s.size() ? s(0) : 1.0;
    const double thr =
        std::max(1e-7, 1e3 * std::numeric_limits<double>::epsilon() * s0);
    int rank = 0;
    for (int i = 0; i < s.size(); ++i)
        if (s(i) > thr) ++rank;
    const int n_null = static_cast<int>(active.size()) - rank;
    const MatrixXd& V = svd.matrixV();
    for (size_t i = 0; i < active.size(); ++i)
        projection(active[i]) = n_null ? V.row(i).segment(rank, n_null).norm() : 0.0;
    return projection;
}

// SVD-nullspace per-entity status coloring (port of _compute_entity_statuses).
std::map<std::string, std::string> compute_statuses(
    const VectorXd& x, const json& entities_list, const VarMap& var_map,
    int n_vars, const MatrixXd& J, const std::vector<std::string>& conflicting,
    const std::map<std::string, std::vector<int>>& tag_vars) {
    std::map<std::string, std::string> statuses;
    if (J.rows() == 0 || n_vars == 0) {
        for (const auto& e : entities_list) statuses[jid(e)] = "under";
        return statuses;
    }
    const VectorXd null_proj = nullspace_projection(J);
    const double tol_null = 1e-6;

    std::set<int> conflicting_vars;
    for (const auto& tag : conflicting) {
        auto it = tag_vars.find(tag);
        if (it != tag_vars.end())
            for (int v : it->second) conflicting_vars.insert(v);
    }

    for (const auto& e : entities_list) {
        std::string id = jid(e);
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

// ---- measurement (port of _handle_point / _line_endpoints / _measure_cond_adj /
//      measure_constraint) ----------------------------------------------------
//
// These read SOLVED entity coords (the extract_entities / SketchSolution::entities
// dict), NOT the solver variable vector. A point handle resolves to a world (x,y);
// helpers return std::nullopt where the Python returns None (dangling refs).

const std::set<std::string> kDimensionalTypes = {"distance", "angle", "radius",
                                                 "diameter", "length", "arc_length"};

// World (x, y) of a point handle against SOLVED entity coords. nullopt if it cannot
// be resolved (dangling). Port of _handle_point.
std::optional<Vector2d> handle_point(const std::string& handle, const json& coords) {
    std::string eid = handle, sub;
    bool has_sub = false;
    auto dot = handle.find('.');
    if (dot != std::string::npos) {
        eid = handle.substr(0, dot);
        sub = handle.substr(dot + 1);
        has_sub = true;
    }
    if (!coords.contains(eid)) {
        if (eid == "origin") return Vector2d(0.0, 0.0);  // virtual fixed origin
        return std::nullopt;
    }
    const json& ent = coords.at(eid);
    std::string et = ent.value("type", std::string());
    auto as_pt = [](const json& a) { return Vector2d(num(a[0]), num(a[1])); };
    if (et == "point") return as_pt(ent.at("p"));
    if (et == "line") {
        if (!has_sub || sub == "p1" || sub == "start") return as_pt(ent.at("p1"));
        if (sub == "p2" || sub == "end") return as_pt(ent.at("p2"));
        if (sub == "mid") {
            Vector2d a = as_pt(ent.at("p1")), b = as_pt(ent.at("p2"));
            return Vector2d((a(0) + b(0)) * 0.5, (a(1) + b(1)) * 0.5);
        }
    }
    bool has_center = ent.contains("center");
    if (et == "circle" || et == "arc" || et == "ellipse" || et == "ellipse_arc" ||
        et == "arc_slot") {
        if ((!has_sub || sub == "center") && has_center) return as_pt(ent.at("center"));
        if (et == "arc" && (sub == "start" || sub == "end")) {
            double cx = num(ent["center"][0]), cy = num(ent["center"][1]);
            double r = num(ent["radius"]);
            double a = (sub == "start" ? num(ent["start"]) : num(ent["end"]));
            double ar = a * kDeg;
            return Vector2d(cx + r * std::cos(ar), cy + r * std::sin(ar));
        }
    }
    if (et == "slot") {
        if (!has_sub || sub == "p1" || sub == "start") return as_pt(ent.at("p1"));
        if (sub == "p2" || sub == "end") return as_pt(ent.at("p2"));
    }
    if (et == "spline" || et == "style_spline") {
        const json& points = jat(ent, "points");
        if (!points.is_array() || points.empty()) return std::nullopt;
        size_t index = 0;
        if (sub == "end") index = points.size() - 1;
        else if (has_sub && sub != "start") {
            if (sub.size() < 2 || sub[0] != 'p') return std::nullopt;
            const char* end = sub.data() + sub.size();
            auto parsed = std::from_chars(sub.data() + 1, end, index);
            if (parsed.ec != std::errc() || parsed.ptr != end) return std::nullopt;
        }
        if (index >= points.size() || !points[index].is_array() || points[index].size() != 2 ||
            !points[index][0].is_number() || !points[index][1].is_number()) return std::nullopt;
        return as_pt(points[index]);
    }
    return std::nullopt;
}

// (p1, p2) of a line entity referenced by a (possibly sub-less) handle, or nullopt.
// Port of _line_endpoints.
std::optional<std::pair<Vector2d, Vector2d>> line_endpoints(const std::string& handle,
                                                            const json& coords) {
    if (is_axis(handle))
        return std::make_pair(Vector2d::Zero().eval(), handle == "axis.h" ? Vector2d(1, 0) : Vector2d(0, 1));
    std::string eid = handle.substr(0, handle.find('.'));
    if (!coords.contains(eid)) return std::nullopt;
    const json& ent = coords.at(eid);
    if (ent.value("type", std::string()) != "line" && ent.value("type", std::string()) != "slot")
        return std::nullopt;
    return std::make_pair(Vector2d(num(ent["p1"][0]), num(ent["p1"][1])),
                          Vector2d(num(ent["p2"][0]), num(ent["p2"][1])));
}

// Σ adj of a distance constraint's arc-conditions against SOLVED coords (C50).
// Mirrors _measure_cond_adj: only a BARE circle/arc entity-id ref honors its
// "min"/"max" condition (adj = -r / +r); everything else contributes 0.
double measure_cond_adj(const json& constraint, const std::vector<std::string>& refs,
                        const json& coords) {
    if (!constraint.contains("cond") || !constraint["cond"].is_array()) return 0.0;
    const json& cond = constraint["cond"];
    double adj = 0.0;
    int n = std::min<int>(2, std::min<int>(static_cast<int>(refs.size()),
                                           static_cast<int>(cond.size())));
    for (int i = 0; i < n; ++i) {
        if (!cond[i].is_string()) continue;
        std::string c = cond[i].get<std::string>();
        const std::string& h = refs[i];
        if ((c != "min" && c != "max") || h.find('.') != std::string::npos) continue;
        if (!coords.contains(h)) continue;
        const json& ent = coords.at(h);
        std::string t = ent.value("type", std::string());
        if (t != "circle" && t != "arc") continue;
        double r = ent.contains("radius") ? num(ent["radius"]) : 0.0;
        adj += (c == "min") ? -r : r;
    }
    return adj;
}

// ---- connected-component decomposition ----------------------------------------
// One dense System for a sketch with many independent profiles is O(n³) on the
// LM / SVD steps. Union-find over entities linked by constraint refs partitions
// the sketch into independent components that can be solved separately; DOF /
// statuses / diagnostics are summed/merged afterward. Exact solves only split
// independently fixed components; the movable remainder stays monolithic.


std::string eid_of_handle(const std::string& h) {
    auto d = h.find('.');
    return d == std::string::npos ? h : h.substr(0, d);
}

struct SketchComponent {
    json entities = json::array();
    json constraints = json::array();
    std::vector<std::string> entity_ids;
};

// Partition entities into connected components under the constraint graph.
// Constraints that only touch "origin" (or dangling refs) ride with the first
// real entity they reference; orphan constraints join component 0.
std::vector<SketchComponent> partition_components(const json& entities_list,
                                                    const json& constraints_list) {
    UnionFind uf;
    std::set<std::string> known;
    for (const auto& e : entities_list) {
        if (!e.is_object() || !e.contains("id")) continue;
        std::string id = e["id"].get<std::string>();
        known.insert(id);
        uf.find(id);
    }
    for (const auto& c : constraints_list) {
        if (!c.is_object() || !c.contains("refs") || !c["refs"].is_array()) continue;
        std::vector<std::string> eids;
        for (const auto& r : c["refs"]) {
            if (!r.is_string()) continue;
            std::string eid = eid_of_handle(r.get<std::string>());
            if (eid == "origin") continue;
            if (known.count(eid)) eids.push_back(eid);
        }
        for (size_t i = 1; i < eids.size(); ++i) uf.unite(eids[0], eids[i]);
    }

    std::map<std::string, int> root_to_idx;
    std::vector<SketchComponent> comps;
    for (const auto& e : entities_list) {
        if (!e.is_object() || !e.contains("id")) continue;
        std::string id = e["id"].get<std::string>();
        std::string root = uf.find(id);
        auto it = root_to_idx.find(root);
        if (it == root_to_idx.end()) {
            root_to_idx[root] = static_cast<int>(comps.size());
            comps.push_back(SketchComponent{});
            it = root_to_idx.find(root);
        }
        comps[it->second].entities.push_back(e);
        comps[it->second].entity_ids.push_back(id);
    }
    if (comps.empty()) return comps;

    for (const auto& c : constraints_list) {
        if (!c.is_object()) { comps[0].constraints.push_back(c); continue; }
        std::string root;
        if (c.contains("refs") && c["refs"].is_array()) {
            for (const auto& r : c["refs"]) {
                if (!r.is_string()) continue;
                std::string eid = eid_of_handle(r.get<std::string>());
                if (eid == "origin" || !known.count(eid)) continue;
                root = uf.find(eid);
                break;
            }
        }
        if (root.empty() || !root_to_idx.count(root))
            comps[0].constraints.push_back(c);
        else
            comps[root_to_idx[root]].constraints.push_back(c);
    }
    return comps;
}

// Monolithic System build + solve (the historical path). Used by the exact/
// oracle path always, and by each component of the interactive decomp path.
SketchSolution solve_sketch_monolithic(const json& sketch, const json& env,
                                       const std::optional<SolveState>& warm,
                                       std::optional<double> time_budget_ms,
                                       bool interactive) {
    json entities_list = sketch.value("entities", json::array());
    json constraints_list = sketch.value("constraints", json::array());

    EntMap entities;
    for (const auto& e : entities_list) entities[jid(e)] = e;

    System sys;
    sys.set_interactive(interactive);
    VarMap var_map;
    for (const auto& e : entities_list) {
        try {
            var_map[jid(e)] = allocate_entity(sys, e);
        } catch (...) {
            var_map[jid(e)] = sys.add_vars(0, {});
        }
    }

    std::map<std::string, std::vector<int>> tag_vars;
    SketchSolution sol;
    const auto context = constraint_context(entities, var_map, constraints_list);
    for (const auto& c : constraints_list) {
        try {
            add_constraint(sys, c, entities, var_map, env, interactive, context);
        } catch (const std::exception& e) {
            sol.skipped.emplace_back(jid(c), e.what());
        } catch (...) {
            sol.skipped.emplace_back(jid(c), "unknown constraint registration error");
        }
    }

    // Per-entity doc-geometry signatures for warm-start gating (port of the
    // SolveState.ent_sig path). nlohmann::json::dump() serializes object keys in
    // sorted order, mirroring json.dumps(e, sort_keys=True); equal entities produce
    // equal strings and a rewritten entity produces a different one, which is all the
    // warm/cold gate needs (byte-identity with Python is NOT required).
    std::map<std::string, std::string> cur_sig;
    for (const auto& e : entities_list) cur_sig[jid(e)] = e.dump();

    // Determine the warm-start vector. Warm values are applied PER ENTITY and only
    // while that entity's doc geometry is unchanged since the warm state was built
    // (matching widx, matching var count, matching signature). Mirrors solve_sketch's
    // warm path exactly: legacy states without signatures fall back to whole-vector.
    std::optional<Eigen::VectorXd> x0;
    if (warm) {
        if (warm->ent_sig.empty()) {
            if (warm->n_vars == sys.n_vars()) x0 = warm->x;
        } else {
            Eigen::VectorXd base = sys.init_vector();
            for (const auto& kv : var_map) {
                const std::string& eid = kv.first;
                const std::vector<int>& idx = kv.second;
                auto wit = warm->var_map.find(eid);
                if (wit == warm->var_map.end()) continue;
                const std::vector<int>& widx = wit->second;
                if (widx.size() != idx.size()) continue;
                auto sit = warm->ent_sig.find(eid);
                std::string wsig = (sit != warm->ent_sig.end()) ? sit->second
                                                                : std::string();
                auto cit = cur_sig.find(eid);
                std::string csig = (cit != cur_sig.end()) ? cit->second : std::string();
                if (wsig != csig) continue;  // doc geometry changed -> cold-start this entity
                for (size_t i = 0; i < idx.size(); ++i) base(idx[i]) = warm->x(widx[i]);
            }
            x0 = base;
        }
    }

    auto res = sys.solve(x0, 1e-10, 100, time_budget_ms);
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
        if (!c.is_object() || !c.contains("id") || !c["id"].is_string()) continue;
        std::string cid = c["id"];
        std::vector<int> vars;
        if (c.contains("refs") && c["refs"].is_array())
            for (const auto& r : c["refs"]) {
                if (!r.is_string()) continue;
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

    // Opaque warm-start state for the next solve / drag.
    sol.state.x = res.x;
    sol.state.var_map = var_map;
    sol.state.n_vars = sys.n_vars();
    sol.state.ent_sig = cur_sig;
    return sol;
}

// Merge component results back into the original entity-allocation order, so
// warm starts and consumers reusing x with a rebuilt System see identical indices.
SketchSolution merge_component_solutions(
    const std::vector<SketchSolution>& parts,
    const json& full_entities_list) {
    SketchSolution sol;
    sol.converged = true;
    sol.dof = 0;
    sol.entities = json::object();
    sol.statuses = {};
    int n_vars = 0;
    for (const auto& p : parts) n_vars += static_cast<int>(p.x.size());
    sol.x.resize(n_vars);
    std::map<std::string, std::pair<const SketchSolution*, const std::vector<int>*>> layout;
    std::map<std::string, std::string> cur_sig;
    for (const auto& e : full_entities_list)
        if (e.is_object() && e.contains("id")) cur_sig[e["id"]] = e.dump();

    for (const auto& p : parts) {
        sol.converged = sol.converged && p.converged;
        sol.dof += p.dof;
        sol.redundant.insert(sol.redundant.end(), p.redundant.begin(), p.redundant.end());
        sol.conflicting.insert(sol.conflicting.end(), p.conflicting.begin(),
                               p.conflicting.end());
        sol.skipped.insert(sol.skipped.end(), p.skipped.begin(), p.skipped.end());
        for (auto it = p.entities.begin(); it != p.entities.end(); ++it)
            sol.entities[it.key()] = it.value();
        for (const auto& kv : p.statuses) sol.statuses[kv.first] = kv.second;
        for (const auto& kv : p.var_map) layout[kv.first] = {&p, &kv.second};
    }
    int offset = 0;
    for (const auto& e : full_entities_list) {
        const std::string id = jid(e);
        auto it = layout.find(id);
        if (it == layout.end()) continue;
        const auto& part = *it->second.first;
        const auto& local = *it->second.second;
        auto& global = sol.var_map[id];
        global.reserve(local.size());
        for (int i : local) {
            global.push_back(offset);
            sol.x(offset++) = part.x(i);
        }
    }
    // Entities with no vars (or missing from a part) still need status defaults.
    for (const auto& e : full_entities_list) {
        if (!e.is_object() || !e.contains("id")) continue;
        std::string id = e["id"].get<std::string>();
        if (!sol.statuses.count(id)) sol.statuses[id] = "under";
        if (!sol.entities.contains(id)) {
            // Pass through authored geometry when a component produced nothing.
            json o = e;
            sol.entities[id] = o;
        }
    }
    sol.state.x = sol.x;
    sol.state.var_map = sol.var_map;
    sol.state.n_vars = n_vars;
    sol.state.ent_sig = cur_sig;
    return sol;
}

}  // namespace

SketchSolution solve_sketch(const json& sketch, const json& env,
                            const std::optional<SolveState>& warm,
                            std::optional<double> time_budget_ms,
                            bool interactive) {
    // Purely fixed conversions are common and should not enlarge the dense QR of
    // a later, unrelated edit. Other exact solves retain the historical system.

    json entities_list = sketch.value("entities", json::array());
    json constraints_list = sketch.value("constraints", json::array());
    // Small sketches: decomp overhead dominates. Also a single component is just
    // the monolithic path.
    if (!entities_list.is_array() || entities_list.size() < 2 || !constraints_list.is_array())
        return solve_sketch_monolithic(sketch, env, warm, time_budget_ms, interactive);
    std::set<std::string> whole_fixed;
    if (!interactive) {
        if (warm && warm->ent_sig.empty())
            return solve_sketch_monolithic(sketch, env, warm, time_budget_ms, false);
        std::set<std::string> ids;
        for (const auto& e : entities_list) {
            if (!e.is_object() || !jat(e, "id").is_string() || !jat(e, "type").is_string() ||
                !ids.insert(jid(e)).second)
                return solve_sketch_monolithic(sketch, env, warm, time_budget_ms, false);
        }
        for (const auto& c : constraints_list) {
            if (!c.is_object() || jat(c, "type") != "fix" || jat(c, "driven") == true ||
                !c.contains("refs") || !c["refs"].is_array() || c["refs"].size() != 1 ||
                !c["refs"][0].is_string()) continue;
            const std::string h = c["refs"][0].get<std::string>();
            if (h.find('.') == std::string::npos) whole_fixed.insert(h);
        }
        if (whole_fixed.empty())
            return solve_sketch_monolithic(sketch, env, warm, time_budget_ms, false);
    }

    std::vector<SketchComponent> comps =
        partition_components(entities_list, constraints_list);
    if (comps.size() <= 1)
        return solve_sketch_monolithic(sketch, env, warm, time_budget_ms, interactive);
    if (!interactive) {
        std::vector<bool> fixed(comps.size(), false);
        std::map<std::string, size_t> component_of;
        size_t count = 0;
        for (size_t i = 0; i < comps.size(); ++i) {
            fixed[i] = true;
            for (const auto& e : comps[i].entities) {
                const std::string id = jid(e), t = e.value("type", "");
                component_of[id] = i;
                // Whole fix on the remaining families only pins a derived point.
                if (!whole_fixed.count(id) || (t != "point" && t != "line" && t != "circle" &&
                    t != "arc" && t != "ellipse" && t != "spline" && t != "style_spline"))
                    fixed[i] = false;
            }
            if (fixed[i]) ++count;
        }
        if (count == 0)
            return solve_sketch_monolithic(sketch, env, warm, time_budget_ms, false);
        SketchComponent remainder;
        for (const auto& e : entities_list)
            if (!fixed[component_of.at(jid(e))]) remainder.entities.push_back(e);
        for (const auto& c : constraints_list) {
            size_t component = 0;
            if (c.is_object() && c.contains("refs") && c["refs"].is_array()) {
                for (const auto& h : c["refs"]) {
                    if (!h.is_string()) continue;
                    auto it = component_of.find(eid_of_handle(h.get<std::string>()));
                    if (it != component_of.end()) { component = it->second; break; }
                }
            }
            if (!fixed[component]) remainder.constraints.push_back(c);
        }
        std::vector<SketchComponent> split;
        split.reserve(count + (remainder.entities.empty() ? 0 : 1));
        if (!remainder.entities.empty()) split.push_back(std::move(remainder));
        for (size_t i = 0; i < comps.size(); ++i)
            if (fixed[i]) split.push_back(std::move(comps[i]));
        comps = std::move(split);
    }

    // Independent components — solve each with its own dense System.
    // Wall-clock budget is shared across components (remaining time each round).
    const auto t0 = std::chrono::steady_clock::now();
    std::vector<SketchSolution> parts;
    parts.reserve(comps.size());
    for (const auto& comp : comps) {
        json sub;
        sub["entities"] = comp.entities;
        sub["constraints"] = comp.constraints;
        std::optional<double> budget = time_budget_ms;
        if (time_budget_ms) {
            const double elapsed_ms =
                std::chrono::duration<double, std::milli>(
                    std::chrono::steady_clock::now() - t0)
                    .count();
            const double rem = *time_budget_ms - elapsed_ms;
            if (rem <= 0.0) {
                // Budget exhausted: still run a cold extract via monolithic with a
                // tiny budget so the caller gets finite geometry.
                budget = 0.1;
            } else {
                budget = rem;
            }
        }
        parts.push_back(
            solve_sketch_monolithic(sub, env, warm, budget, interactive));
    }
    auto sol = merge_component_solutions(parts, entities_list);
    // Component execution order must not change chronological diagnostics.
    std::map<std::string, size_t> order;
    for (size_t i = 0; i < constraints_list.size(); ++i) order.emplace(jid(constraints_list[i]), i);
    auto position = [&](const std::string& id) {
        auto it = order.find(id);
        return it == order.end() ? constraints_list.size() : it->second;
    };
    auto earlier = [&](const std::string& a, const std::string& b) { return position(a) < position(b); };
    std::stable_sort(sol.redundant.begin(), sol.redundant.end(), earlier);
    std::stable_sort(sol.conflicting.begin(), sol.conflicting.end(), earlier);
    std::stable_sort(sol.skipped.begin(), sol.skipped.end(), [&](const auto& a, const auto& b) {
        return position(a.first) < position(b.first);
    });
    return sol;
}

SketchSolution solve_sketch(const json& sketch, const json& env) {
    return solve_sketch(sketch, env, std::nullopt);
}

namespace {

// Entity id -> its fixed sub-handles ("" = the whole entity is fixed).
std::map<std::string, std::set<std::string>> fixed_handles(const json& constraints) {
    std::map<std::string, std::set<std::string>> out;
    if (!constraints.is_array()) return out;
    for (const json& c : constraints) {
        if (!c.is_object() || c.value("type", std::string()) != "fix") continue;
        if (!c.contains("refs") || !c["refs"].is_array() || c["refs"].empty()) continue;
        if (!c["refs"][0].is_string()) continue;
        const std::string h = c["refs"][0].get<std::string>();
        const auto dot = h.find('.');
        out[eid_of(h)].insert(dot == std::string::npos ? std::string() : h.substr(dot + 1));
    }
    return out;
}

bool handle_is_fixed(const std::string& handle,
                     const std::map<std::string, std::set<std::string>>& fixed) {
    const auto it = fixed.find(eid_of(handle));
    if (it == fixed.end()) return false;
    if (it->second.count(std::string())) return true;
    const auto dot = handle.find('.');
    return dot != std::string::npos && it->second.count(handle.substr(dot + 1)) > 0;
}

std::string fmt_mm(double v) {
    char buf[32];
    std::snprintf(buf, sizeof buf, "%.4g", v);
    return buf;
}

}  // namespace

std::string nonconvergence_reason(const json& sketch, const SketchSolution& sol) {
    const json constraints = sketch.value("constraints", json::array());
    const json entities = sketch.value("entities", json::array());
    std::map<std::string, std::string> ctype_of;
    if (constraints.is_array())
        for (const json& c : constraints)
            if (c.is_object() && c.contains("id") && c["id"].is_string())
                ctype_of[c["id"].get<std::string>()] = c.value("type", std::string("?"));

    std::string msg = "sketch did not converge: the constraints conflict.";
    if (!sol.conflicting.empty()) {
        msg += " Conflicting constraints:";
        for (std::size_t i = 0; i < sol.conflicting.size(); ++i) {
            const std::string& id = sol.conflicting[i];
            const auto t = ctype_of.find(id);
            msg += std::string(i ? ", " : " ") + id + " (" +
                   (t == ctype_of.end() ? std::string("?") : t->second) + ")";
        }
        msg += ".";
    }

    // The usual cause: `fix` pins the AUTHORED coordinates (the seeds), and the seeds of
    // two fixed points that a coincident constraint joins are apart. The solved geometry
    // the readback shows can be closed to 1e-11 mm while the seeds are 0.03 mm apart.
    const auto fixed = fixed_handles(constraints);
    std::vector<std::string> gaps;
    if (!fixed.empty() && constraints.is_array()) {
        json authored = json::object();
        if (entities.is_array())
            for (const json& e : entities)
                if (!jid(e).empty()) authored[jid(e)] = e;
        for (const json& c : constraints) {
            if (!c.is_object() || c.value("type", std::string()) != "coincident") continue;
            if (!c.contains("refs") || !c["refs"].is_array() || c["refs"].size() != 2) continue;
            if (!c["refs"][0].is_string() || !c["refs"][1].is_string()) continue;
            const std::string h0 = c["refs"][0].get<std::string>();
            const std::string h1 = c["refs"][1].get<std::string>();
            if (!handle_is_fixed(h0, fixed) || !handle_is_fixed(h1, fixed)) continue;
            std::optional<Vector2d> p0, p1;
            try {
                p0 = handle_point(h0, authored);
                p1 = handle_point(h1, authored);
            } catch (const std::exception&) {
                continue;  // malformed entity: no gap to report
            }
            if (!p0 || !p1) continue;
            const double gap = (*p0 - *p1).norm();
            if (gap <= 1e-9) continue;
            gaps.push_back(jid(c) + " (" + h0 + ", " + h1 + ") " + fmt_mm(gap) + " mm");
        }
    }
    if (!gaps.empty()) {
        msg += " A fix holds an entity at its authored coordinates, not at its solved position."
               " These coincident constraints join fixed points that are apart:";
        const std::size_t shown = std::min<std::size_t>(gaps.size(), 4);
        for (std::size_t i = 0; i < shown; ++i) msg += std::string(i ? "; " : " ") + gaps[i];
        if (gaps.size() > shown)
            msg += "; and " + std::to_string(gaps.size() - shown) + " more";
        msg += ". Remove the fix, or edit the authored coordinates so that the points touch.";
    } else if (sol.conflicting.empty()) {
        msg += " The solver found no position that satisfies all constraints."
               " Run sketch_health to find the constraints that disagree.";
    }
    return msg;
}

bool adopt_solved_coordinates(json& sketch, const json& env) {
    if (!sketch.is_object() || !sketch.contains("entities") || !sketch["entities"].is_array())
        return false;
    // The compile path's solve (exact, cold): the authored seeds then equal the geometry
    // compile_document builds, so the rewrite changes no solved position.
    const SketchSolution sol = solve_sketch(sketch, env);
    if (!sol.converged) return false;
    static const std::set<std::string> kSolverTypes = {
        "point", "line", "circle", "arc", "ellipse", "ellipse_arc", "parabola", "conic",
        "slot", "arc_slot", "spline", "style_spline", "text"};
    static const std::set<std::string> kKeep = {"type", "id", "construction", "ccw"};
    for (json& e : sketch["entities"]) {
        const std::string id = jid(e);
        if (id.empty() || !kSolverTypes.count(e.value("type", std::string()))) continue;
        if (!sol.entities.contains(id)) continue;
        const json& solved = sol.entities.at(id);
        for (auto it = solved.begin(); it != solved.end(); ++it) {
            if (kKeep.count(it.key()) || !e.contains(it.key())) continue;
            json& authored = e[it.key()];
            // Same shape only: a number stays a number, an [x,y] stays an [x,y].
            if (authored.is_array() != it.value().is_array()) continue;
            if (authored.is_array() && authored.size() != it.value().size()) continue;
            authored = it.value();
        }
    }
    return true;
}

std::map<std::string, std::string> warm_gate_decision(const json& sketch,
                                                      const SolveState& warm) {
    json entities_list = sketch.value("entities", json::array());
    System sys;
    VarMap var_map;
    for (const auto& e : entities_list) {
        try {
            var_map[jid(e)] = allocate_entity(sys, e);
        } catch (...) {
            var_map[jid(e)] = sys.add_vars(0, {});
        }
    }
    std::map<std::string, std::string> cur_sig;
    for (const auto& e : entities_list) cur_sig[jid(e)] = e.dump();

    std::map<std::string, std::string> decision;
    bool legacy = warm.ent_sig.empty();
    for (const auto& e : entities_list) {
        std::string eid = jid(e);
        if (legacy) {
            // Legacy whole-vector behavior: warm iff total var counts match.
            decision[eid] = (warm.n_vars == sys.n_vars()) ? "warm" : "cold";
            continue;
        }
        const std::vector<int>& idx = var_map.at(eid);
        auto wit = warm.var_map.find(eid);
        if (wit == warm.var_map.end() || wit->second.size() != idx.size()) {
            decision[eid] = "cold";
            continue;
        }
        auto sit = warm.ent_sig.find(eid);
        std::string wsig = (sit != warm.ent_sig.end()) ? sit->second : std::string();
        auto cit = cur_sig.find(eid);
        std::string csig = (cit != cur_sig.end()) ? cit->second : std::string();
        decision[eid] = (wsig == csig) ? "warm" : "cold";
    }
    return decision;
}

// ---- free-variable narration + endpoint enumeration -------------------------

namespace {

// Plain-word names for an entity's variables IN ALLOCATION ORDER — mirrors
// allocate_entity's per-type layout exactly (see that function). The i-th string
// names the i-th solver variable of the entity; free_variables maps a loose global
// index back to this name via the entity's var_map slot. Unknown types return {}.
std::vector<std::string> entity_var_names(const std::string& t, int nvars) {
    if (t == "point") return {"x", "y"};
    if (t == "line") return {"p1 x", "p1 y", "p2 x", "p2 y"};
    if (t == "circle") return {"center x", "center y", "radius"};
    if (t == "arc")
        return {"center x", "center y", "radius", "start angle", "end angle"};
    if (t == "ellipse") return {"center x", "center y", "rx", "ry", "rotation"};
    if (t == "ellipse_arc")
        return {"center x", "center y", "rx", "ry", "rotation", "start angle",
                "end angle"};
    if (t == "parabola")
        return {"vertex x", "vertex y", "focus x", "focus y", "t0", "t1"};
    if (t == "conic")
        return {"p0 x", "p0 y", "p1 x", "p1 y", "apex x", "apex y", "rho"};
    if (t == "slot") return {"p1 x", "p1 y", "p2 x", "p2 y", "width"};
    if (t == "arc_slot")
        return {"center x", "center y", "radius", "start angle", "end angle", "width"};
    if (t == "spline" || t == "style_spline") {
        std::vector<std::string> out;
        int npts = nvars / 2;
        for (int i = 0; i < npts; ++i) {
            out.push_back("point " + std::to_string(i) + " x");
            out.push_back("point " + std::to_string(i) + " y");
        }
        return out;
    }
    if (t == "text") return {"x", "y"};
    return {};
}

// Shared body of the two free_variables overloads. `solved_x` (nullable) is a prior
// solve_sketch cold solve of the SAME sketch/env — when its size matches the rebuilt
// system it is reused verbatim, skipping the redundant cold solve (the systems are
// built identically and the LM solve is deterministic, so the vectors are equal).
std::vector<FreeVar> free_variables_at(const json& sketch, const json& env,
                                       const Eigen::VectorXd* solved_x) {
    json entities_list = sketch.value("entities", json::array());
    json constraints_list = sketch.value("constraints", json::array());

    // Rebuild the exact same system solve_sketch builds so variable indices line up.
    System sys;
    VarMap var_map;
    EntMap entities;
    for (const auto& e : entities_list) {
        entities[jid(e)] = e;
        try {
            var_map[jid(e)] = allocate_entity(sys, e);
        } catch (...) {
            var_map[jid(e)] = sys.add_vars(0, {});
        }
    }
    const auto context = constraint_context(entities, var_map, constraints_list);
    for (const auto& c : constraints_list) {
        try {
            add_constraint(sys, c, entities, var_map, env, false, context);
        } catch (...) { /* forward-tolerant */ }
    }

    VectorXd x;
    if (solved_x && solved_x->size() == sys.n_vars()) {
        x = *solved_x;  // reuse the caller's solve — skip the second cold solve
    } else {
        auto res = sys.solve(std::nullopt, 1e-10, 50);
        x = res.x;
    }
    int n = sys.n_vars();
    MatrixXd J = sys.real_jacobian(x);

    std::vector<FreeVar> out;
    if (n == 0) return out;

    // Share the per-entity classifier's projection, including coordinates absent
    // from every constraint Jacobian.
    const VectorXd null_proj = nullspace_projection(J);
    const double tol_null = 1e-6;

    // Walk entities in declaration order; for each, map its loose local variables to
    // their plain-word names. A global->local lookup via the entity's var_map slot.
    for (const auto& e : entities_list) {
        std::string id = jid(e);
        const std::string t = e.value("type", std::string());
        const std::vector<int>& v = var_map.at(id);
        std::vector<std::string> names = entity_var_names(t, static_cast<int>(v.size()));
        for (size_t li = 0; li < v.size(); ++li) {
            int gi = v[li];
            if (gi < 0 || gi >= n) continue;
            if (null_proj(gi) < tol_null) continue;  // pinned — skip
            FreeVar fv;
            fv.entity = id;
            fv.param = (li < names.size()) ? names[li] : ("var " + std::to_string(li));
            fv.var = gi;
            out.push_back(std::move(fv));
        }
    }
    return out;
}

}  // namespace

std::vector<FreeVar> free_variables(const json& sketch, const json& env) {
    return free_variables_at(sketch, env, nullptr);
}

std::vector<FreeVar> free_variables(const json& sketch, const json& env,
                                    const Eigen::VectorXd& solved_x) {
    return free_variables_at(sketch, env, &solved_x);
}

std::vector<Endpoint> entity_endpoints(const json& coords) {
    std::vector<Endpoint> out;
    auto push = [&](const std::string& id, const std::string& sub, double x, double y) {
        Endpoint ep;
        ep.handle = sub.empty() ? id : (id + "." + sub);
        ep.x = x;
        ep.y = y;
        out.push_back(std::move(ep));
    };
    for (auto it = coords.begin(); it != coords.end(); ++it) {
        const std::string& id = it.key();
        const json& ent = it.value();
        if (!ent.is_object()) continue;
        if (ent.value("construction", false)) continue;
        const std::string t = ent.value("type", std::string());
        auto pt = [&](const char* k, double& ox, double& oy) -> bool {
            if (!ent.contains(k) || !ent[k].is_array() || ent[k].size() < 2) return false;
            ox = num(ent[k][0]);
            oy = num(ent[k][1]);
            return true;
        };
        double ax, ay;
        if (t == "point") {
            if (pt("p", ax, ay)) push(id, "", ax, ay);
        } else if (t == "line" || t == "slot") {
            if (pt("p1", ax, ay)) push(id, "p1", ax, ay);
            if (pt("p2", ax, ay)) push(id, "p2", ax, ay);
        } else if (t == "circle") {
            if (pt("center", ax, ay)) push(id, "center", ax, ay);
        } else if (t == "arc") {
            if (pt("center", ax, ay)) push(id, "center", ax, ay);
            if (ent.contains("center") && ent.contains("radius") &&
                ent.contains("start") && ent.contains("end")) {
                double cx = num(ent["center"][0]), cy = num(ent["center"][1]);
                double r = num(ent["radius"]);
                double sa = num(ent["start"]) * kDeg, ea = num(ent["end"]) * kDeg;
                push(id, "start", cx + r * std::cos(sa), cy + r * std::sin(sa));
                push(id, "end", cx + r * std::cos(ea), cy + r * std::sin(ea));
            }
        } else if (t == "spline" || t == "style_spline") {
            if (ent.contains("points") && ent["points"].is_array() &&
                ent["points"].size() >= 2) {
                const json& p0 = ent["points"].front();
                const json& pn = ent["points"].back();
                if (p0.is_array() && p0.size() >= 2)
                    push(id, "start", num(p0[0]), num(p0[1]));
                if (pn.is_array() && pn.size() >= 2)
                    push(id, "end", num(pn[0]), num(pn[1]));
            }
        }
    }
    return out;
}

// Current solved measurement for a DIMENSIONAL constraint. Port of measure_constraint.
std::optional<double> measure_constraint(const json& constraint,
                                         const json& entity_coords) {
    std::string ctype = constraint.value("type", std::string());
    std::vector<std::string> refs;
    if (constraint.contains("refs") && constraint["refs"].is_array())
        for (const auto& r : constraint["refs"])
            if (r.is_string()) refs.push_back(r.get<std::string>());

    if (kDimensionalTypes.find(ctype) == kDimensionalTypes.end()) return std::nullopt;

    auto eid0 = [&]() -> std::string {
        if (refs.empty()) return std::string();
        const std::string& h = refs[0];
        return h.substr(0, h.find('.'));
    };

    if (ctype == "arc_length") {
        if (refs.size() != 1 || !entity_coords.contains(refs[0])) return std::nullopt;
        const json& e = entity_coords.at(refs[0]);
        if (e.value("type", "") != "arc") return std::nullopt;
        return num(e.at("radius")) * arc_sweep(num(e.at("start")), num(e.at("end")),
                                              e.value("ccw", true)) * kDeg;
    }
    if (ctype == "radius") {
        std::string eid = eid0();
        if (!eid.empty() && entity_coords.contains(eid)) {
            const json& ent = entity_coords.at(eid);
            std::string t = ent.value("type", std::string());
            if (t == "circle" || t == "arc") return num(ent["radius"]);
            if (t == "slot" || t == "arc_slot") return num(ent["width"]) / 2.0;  // cap radius
        }
        return std::nullopt;
    }

    if (ctype == "diameter") {
        std::string eid = eid0();
        if (!eid.empty() && entity_coords.contains(eid)) {
            const json& ent = entity_coords.at(eid);
            std::string t = ent.value("type", std::string());
            if (t == "circle" || t == "arc") return num(ent["radius"]) * 2.0;
            if (t == "slot" || t == "arc_slot") return num(ent["width"]);  // slot width
        }
        return std::nullopt;
    }

    if (ctype == "angle") {
        if (refs.size() < 2) return std::nullopt;
        auto la = line_endpoints(refs[0], entity_coords);
        auto lb = line_endpoints(refs[1], entity_coords);
        if (!la || !lb) return std::nullopt;
        double ux = la->second(0) - la->first(0), uy = la->second(1) - la->first(1);
        double vx = lb->second(0) - lb->first(0), vy = lb->second(1) - lb->first(1);
        double cross = ux * vy - uy * vx;
        double dot = ux * vx + uy * vy;
        double theta = std::atan2(std::abs(cross), dot) * (180.0 / kPi);
        // atan2(|cross|, dot) already lies in [0, 180]; fmod folds 180 back to 0.
        double m = std::fmod(theta, 180.0);
        if (m < 0.0) m += 180.0;
        return m;
    }

    if (ctype == "distance" || ctype == "length") {
        // 1 bare line/slot ref → line length (Smart-Dimension / type "length").
        if (refs.size() == 1) {
            auto le = line_endpoints(refs[0], entity_coords);
            if (!le) {
                std::string eid = refs[0].substr(0, refs[0].find('.'));
                if (entity_coords.contains(eid)) {
                    const json& ent = entity_coords.at(eid);
                    if (ent.value("type", std::string()) == "slot" &&
                        ent.contains("p1") && ent.contains("p2")) {
                        double ax = num(ent["p1"][0]), ay = num(ent["p1"][1]);
                        double bx = num(ent["p2"][0]), by = num(ent["p2"][1]);
                        return std::hypot(bx - ax, by - ay);
                    }
                }
                return std::nullopt;
            }
            return std::hypot(le->second(0) - le->first(0), le->second(1) - le->first(1));
        }
        if (refs.size() < 2) return std::nullopt;
        auto is_line = [&](const std::string& h) {
            if (is_axis(h)) return true;
            if (!entity_coords.contains(h)) return false;
            std::string t = entity_coords.at(h).value("type", "");
            return t == "line" || t == "slot";
        };
        double adj = measure_cond_adj(constraint, refs, entity_coords);
        if (is_line(refs[0]) && is_line(refs[1])) {
            auto a = line_endpoints(refs[0], entity_coords), b = line_endpoints(refs[1], entity_coords);
            if (!a || !b) return std::nullopt;
            Vector2d u = a->second - a->first, d = (b->first + b->second) * .5 - a->first;
            return std::abs(u.x() * d.y() - u.y() * d.x()) / std::max(u.norm(), 1e-12);
        }
        if (is_line(refs[0]) || is_line(refs[1])) {
            const std::string& line_h = is_line(refs[0]) ? refs[0] : refs[1];
            const std::string& pt_h = is_line(refs[0]) ? refs[1] : refs[0];
            auto le = line_endpoints(line_h, entity_coords);
            auto P = handle_point(pt_h, entity_coords);
            if (!le || !P) return std::nullopt;
            double ax = le->first(0), ay = le->first(1);
            double bx = le->second(0), by = le->second(1);
            double dxab = bx - ax, dyab = by - ay;
            double L = std::hypot(dxab, dyab);
            if (L < 1e-12)
                return std::hypot((*P)(0) - ax, (*P)(1) - ay) + adj;
            double cross = dxab * ((*P)(1) - ay) - dyab * ((*P)(0) - ax);
            return std::abs(cross) / L + adj;
        }
        auto P0 = handle_point(refs[0], entity_coords);
        auto P1 = handle_point(refs[1], entity_coords);
        if (!P0 || !P1) return std::nullopt;
        std::string axis;
        if (constraint.contains("axis") && constraint["axis"].is_string())
            axis = constraint["axis"].get<std::string>();
        std::string axis_l = axis;
        std::transform(axis_l.begin(), axis_l.end(), axis_l.begin(),
                       [](unsigned char ch) { return std::tolower(ch); });
        // Axis dimension: |Δu| + Σadj (the SAME additive Σadj the aligned/radial case
        // uses below), NOT a per-coordinate rim projection. Verbatim with the oracle's
        // current axis branch (`abs(P0[0]-P1[0]) + adj` / `abs(P0[1]-P1[1]) + adj`).
        if (axis_l == "h") return std::abs((*P0)(0) - (*P1)(0)) + adj;
        if (axis_l == "v") return std::abs((*P0)(1) - (*P1)(1)) + adj;
        return std::hypot((*P0)(0) - (*P1)(0), (*P0)(1) - (*P1)(1)) + adj;
    }

    return std::nullopt;
}

// Per-drag cached solve context — see the sketch.h contract. Everything here is what
// drag() historically rebuilt per frame. Reusing the System reuses the exact residual
// blocks a fresh build would register (add_constraint's lambdas are pure per-call and
// the registration-time shape probe in nsolve's add_residual has no side effects the
// solve depends on), so caching preserves solve semantics by construction.
//
// When the sketch partitions into multiple connected components, only the
// component(s) containing the drag target(s) are built into Systems; other entities
// keep their pre-drag solved coords. A single-component sketch uses the historical
// one-System path (identical block registration order).
struct DragComp {
    System sys;
    EntMap entities;
    VarMap var_map;
    json entities_list = json::array();
    json constraints_list = json::array();
    std::unordered_set<std::string> eids;
};

struct DragContext {
    // Legacy single-System fields (used when comps is empty — one component / fallback).
    System sys;
    EntMap entities;
    VarMap var_map;
    json entities_list;
    json constraints_list;

    // Multi-component drag: one System per dragged component.
    std::vector<DragComp> comps;
    std::map<std::string, int> eid_to_comp;  // entity id -> index in comps (-1 = frozen)
    json frozen_coords = json::object();    // non-dragged entities (pre-drag positions)
    bool multi = false;
};

// Build one DragComp System from a SketchComponent (entities + constraints).
DragComp build_drag_comp(const SketchComponent& sc, const json& env) {
    DragComp dc;
    dc.entities_list = sc.entities;
    dc.constraints_list = sc.constraints;
    for (const auto& e : sc.entities) {
        if (!e.is_object() || !e.contains("id")) continue;
        std::string id = e["id"].get<std::string>();
        dc.entities[id] = e;
        dc.eids.insert(id);
        try {
            dc.var_map[id] = allocate_entity(dc.sys, e);
        } catch (...) {
            dc.var_map[id] = dc.sys.add_vars(0, {});
        }
    }
    dc.sys.set_interactive(true);
    const auto context = constraint_context(dc.entities, dc.var_map, sc.constraints);
    for (const auto& c : sc.constraints) {
        try {
            add_constraint(dc.sys, c, dc.entities, dc.var_map, env, true, context);
        } catch (...) {
        }
    }
    return dc;
}

json drag(const SolveState& state, const std::string& handle,
          const std::pair<double, double>& target_xy, const json& sketch,
          const json& env, const json& targets,
          std::shared_ptr<DragContext>* ctx_io) {
    // Cached fast path (ctx_io != nullptr): build the context once, reuse it on every
    // later frame of the same drag, and skip the per-frame diagnostics below. Topology
    // cannot change mid-drag; the CALLER invalidates the context on any doc change.
    const bool fast = ctx_io != nullptr;
    std::shared_ptr<DragContext> ctx = fast ? *ctx_io : nullptr;

    // Normalize to a list of (handle, (tx, ty)). The single-handle call becomes a
    // one-element list. `targets` (when given) wins over handle/target_xy.
    std::vector<std::pair<std::string, std::pair<double, double>>> target_pairs;
    if (targets.is_array() && !targets.empty()) {
        for (const auto& t : targets) {
            std::string h;
            json xy;
            if (t.is_object()) {
                if (t.contains("handle") && t["handle"].is_string())
                    h = t["handle"].get<std::string>();
                if (t.contains("xy")) xy = t["xy"];
            } else if (t.is_array() && t.size() == 2) {
                if (t[0].is_string()) h = t[0].get<std::string>();
                xy = t[1];
            }
            if (h.empty() || !xy.is_array() || xy.size() < 2) continue;
            target_pairs.emplace_back(h, std::make_pair(num(xy[0]), num(xy[1])));
        }
    } else if (!handle.empty()) {
        target_pairs.emplace_back(handle, target_xy);
    }

    if (!ctx) {
        ctx = std::make_shared<DragContext>();
        ctx->entities_list = sketch.value("entities", json::array());
        ctx->constraints_list = sketch.value("constraints", json::array());
        for (const auto& e : ctx->entities_list) {
            if (e.is_object() && e.contains("id")) ctx->entities[e["id"]] = e;
        }

        // Seed frozen coords from the warm state (last solved positions). Fallback
        // to authored entity geometry so non-dragged components never vanish from
        // the response when the warm vector layout is unavailable.
        if (state.n_vars > 0 && !state.var_map.empty() &&
            state.x.size() == state.n_vars) {
            try {
                ctx->frozen_coords =
                    extract_entities(state.x, ctx->entities_list, state.var_map);
            } catch (...) {
                ctx->frozen_coords = json::object();
            }
        }
        if (!ctx->frozen_coords.is_object() || ctx->frozen_coords.empty()) {
            ctx->frozen_coords = json::object();
            for (const auto& e : ctx->entities_list) {
                if (!e.is_object() || !e.contains("id")) continue;
                ctx->frozen_coords[e["id"].get<std::string>()] = e;
            }
        }

        // Partition; if multiple components, only build Systems for those
        // that contain a drag target. Otherwise fall back to the historical one-System.
        std::vector<SketchComponent> all_comps =
            partition_components(ctx->entities_list, ctx->constraints_list);
        std::set<int> active;
        if (all_comps.size() > 1) {
            std::map<std::string, int> eid_comp;
            for (size_t i = 0; i < all_comps.size(); ++i)
                for (const auto& id : all_comps[i].entity_ids) eid_comp[id] = static_cast<int>(i);
            for (const auto& tp : target_pairs) {
                std::string eid = eid_of_handle(tp.first);
                auto it = eid_comp.find(eid);
                if (it != eid_comp.end()) active.insert(it->second);
            }
        }

        if (all_comps.size() > 1 && !active.empty()) {
            ctx->multi = true;
            // Map every entity to either a live comp index or "frozen" (-1).
            std::map<int, int> old_to_new;  // original comp idx -> ctx->comps idx
            for (int oi : active) {
                old_to_new[oi] = static_cast<int>(ctx->comps.size());
                ctx->comps.push_back(build_drag_comp(all_comps[static_cast<size_t>(oi)], env));
            }
            for (size_t i = 0; i < all_comps.size(); ++i) {
                int mapped = old_to_new.count(static_cast<int>(i))
                                 ? old_to_new[static_cast<int>(i)]
                                 : -1;
                for (const auto& id : all_comps[i].entity_ids)
                    ctx->eid_to_comp[id] = mapped;
            }
        } else {
            // Single-component / no active target yet: full-system path.
            ctx->sys.set_interactive(true);
            for (const auto& e : ctx->entities_list) {
                try {
                    ctx->var_map[jid(e)] = allocate_entity(ctx->sys, e);
                } catch (...) {
                    ctx->var_map[jid(e)] = ctx->sys.add_vars(0, {});
                }
            }
            const auto context = constraint_context(ctx->entities, ctx->var_map, ctx->constraints_list);
            for (const auto& c : ctx->constraints_list) {
                try {
                    add_constraint(ctx->sys, c, ctx->entities, ctx->var_map, env,
                                   true, context);
                } catch (...) {
                }
            }
        }
        if (fast) *ctx_io = ctx;
    } else {
        // Frame N: clear previous soft targets on every live System.
        if (ctx->multi) {
            for (auto& dc : ctx->comps) {
                dc.sys.clear_soft_targets();
                dc.sys.set_interactive(true);
            }
        } else {
            ctx->sys.clear_soft_targets();
            ctx->sys.set_interactive(true);
        }
    }

    const json& entities_list = ctx->entities_list;
    const json& constraints_list = ctx->constraints_list;

    // ---- multi-component drag path ----------------------------------------
    if (ctx->multi) {
        bool any_target = false;
        std::vector<std::string> rejected_handles;
        std::set<int> touched_comps;
        for (const auto& tp : target_pairs) {
            const std::string& h = tp.first;
            double tx = tp.second.first, ty = tp.second.second;
            std::string eid = eid_of_handle(h);
            auto cit = ctx->eid_to_comp.find(eid);
            if (cit == ctx->eid_to_comp.end() || cit->second < 0) {
                rejected_handles.push_back(h);
                continue;
            }
            DragComp& dc = ctx->comps[static_cast<size_t>(cit->second)];
            Handle hd;
            try {
                hd = resolve_handle(h, dc.entities, dc.var_map);
            } catch (...) {
                rejected_handles.push_back(h);
                continue;
            }
            if (hd.idx.empty()) {
                rejected_handles.push_back(h);
                continue;
            }
            dc.sys.soft_target(hd.idx[0], tx, 0.01);
            if (hd.idx.size() > 1) dc.sys.soft_target(hd.idx[1], ty, 0.01);
            any_target = true;
            touched_comps.insert(cit->second);
        }

        json entity_coords = ctx->frozen_coords.is_object() ? ctx->frozen_coords
                                                            : json::object();
        if (!any_target) {
            json rej = json::array();
            if (!rejected_handles.empty()) {
                for (const auto& h : rejected_handles) rej.push_back(h);
            } else {
                for (const auto& tp : target_pairs) rej.push_back(tp.first);
            }
            return json{{"ok", false}, {"error", "no drag targets resolved"},
                        {"rejected_handles", rej}, {"entities", entity_coords},
                        {"statuses", json::object()}, {"converged", true},
                        {"budget_exceeded", false}};
        }

        bool converged = true;
        bool budget_exceeded = false;
        const double budget_ms = 10.0;
        for (int ci : touched_comps) {
            DragComp& dc = ctx->comps[static_cast<size_t>(ci)];
            // Per-entity warm start from the full state into this component's layout.
            std::optional<Eigen::VectorXd> x0;
            {
                Eigen::VectorXd base = dc.sys.init_vector();
                bool any_warm = false;
                for (const auto& kv : dc.var_map) {
                    auto wit = state.var_map.find(kv.first);
                    if (wit == state.var_map.end()) continue;
                    if (wit->second.size() != kv.second.size()) continue;
                    if (state.x.size() < 1) continue;
                    bool ok = true;
                    for (int wi : wit->second)
                        if (wi < 0 || wi >= state.x.size()) {
                            ok = false;
                            break;
                        }
                    if (!ok) continue;
                    for (size_t i = 0; i < kv.second.size(); ++i)
                        base(kv.second[i]) = state.x(wit->second[i]);
                    any_warm = true;
                }
                // Also warm from the previous frame's component solution when sizes match.
                if (!any_warm && state.n_vars == dc.sys.n_vars() &&
                    state.x.size() == dc.sys.n_vars())
                    base = state.x;
                x0 = base;
            }
            auto res = dc.sys.solve(x0, 1e-10, 50, budget_ms);
            Eigen::VectorXd x = res.x;
            if (!x.allFinite()) {
                if (x0 && x0->allFinite())
                    x = *x0;
                else
                    x = dc.sys.init_vector();
            }
            converged = converged && res.converged;
            budget_exceeded = budget_exceeded || res.budget_exceeded;
            json part = extract_entities(x, dc.entities_list, dc.var_map);
            for (auto it = part.begin(); it != part.end(); ++it)
                entity_coords[it.key()] = it.value();
        }

        // Multi-comp drag always skips per-frame diagnostics (topology fixed mid-drag;
        // full statuses run once on commit via solve_active).
        json rej_fast = json::array();
        for (const auto& h : rejected_handles) rej_fast.push_back(h);
        (void)fast;
        return json{{"ok", true}, {"entities", entity_coords},
                    {"statuses", json::object()}, {"rejected_handles", rej_fast},
                    {"converged", converged}, {"budget_exceeded", budget_exceeded}};
    }

    // ---- single-System path (historical; one component or decomp unused) ----
    System& sys = ctx->sys;
    const EntMap& entities = ctx->entities;
    const VarMap& var_map = ctx->var_map;

    // Soft-target each resolvable handle's variable(s) (weight 0.01). A point handle
    // (idx = {px, py}) is pulled to the cursor (tx, ty); a scalar handle (rim/angle,
    // idx = {one var}) is pulled to the single host-precomputed scalar in tx (xy[1]=0
    // is unused -- the size>1 guard skips it).
    bool any_target = false;
    std::vector<std::string> rejected_handles;
    for (const auto& tp : target_pairs) {
        const std::string& h = tp.first;
        double tx = tp.second.first, ty = tp.second.second;
        Handle hd;
        try {
            hd = resolve_handle(h, entities, var_map);
        } catch (...) {
            rejected_handles.push_back(h);
            continue;
        }
        if (hd.idx.empty()) {
            rejected_handles.push_back(h);  // zero-var virtual handle (origin)
            continue;
        }
        sys.soft_target(hd.idx[0], tx, 0.01);
        if (hd.idx.size() > 1) sys.soft_target(hd.idx[1], ty, 0.01);
        any_target = true;
    }

    if (!any_target) {
        json entity_coords =
            (!state.var_map.empty() && state.x.size() == state.n_vars)
                ? extract_entities(state.x, entities_list, state.var_map)
                : extract_entities(sys.init_vector(), entities_list, var_map);
        json rej = json::array();
        if (!rejected_handles.empty()) {
            for (const auto& h : rejected_handles) rej.push_back(h);
        } else {
            for (const auto& tp : target_pairs) rej.push_back(tp.first);
        }
        return json{{"ok", false}, {"error", "no drag targets resolved"},
                    {"rejected_handles", rej}, {"entities", entity_coords},
                    {"statuses", json::object()}, {"converged", true},
                    {"budget_exceeded", false}};
    }

    // Warm start from state if the topology matches.
    std::optional<Eigen::VectorXd> x0;
    if (state.n_vars == sys.n_vars()) x0 = state.x;
    double budget_ms = 10.0;
    auto res = sys.solve(x0, 1e-10, 50, budget_ms);
    Eigen::VectorXd x = res.x;

    // Last-good fallback: a non-finite drag vector must never reach the caller.
    bool finite = x.allFinite();
    if (!finite) {
        if (x0 && x0->allFinite())
            x = *x0;
        else
            x = sys.init_vector();
    }

    json entity_coords = extract_entities(x, entities_list, var_map);

    // Cached fast path: skip the per-frame diagnostics (analyze's dense ColPivQR,
    // real_jacobian, and compute_statuses' full-V SVD) — they are pure diagnostics
    // and topology cannot change mid-drag. "statuses" stays in the response shape
    // as an empty object; the caller serves its drag-start snapshot and runs full
    // diagnostics once on commit.
    if (fast) {
        json rej_fast = json::array();
        for (const auto& h : rejected_handles) rej_fast.push_back(h);
        return json{{"ok", true}, {"entities", entity_coords},
                    {"statuses", json::object()}, {"rejected_handles", rej_fast},
                    {"converged", res.converged}, {"budget_exceeded", res.budget_exceeded}};
    }

    auto analysis = sys.analyze(x);
    int n = sys.n_vars();
    MatrixXd J = sys.real_jacobian(x);
    std::map<std::string, std::vector<int>> tag_vars;  // (statuses below; empty refs ok)
    for (const auto& c : constraints_list) {
        if (!c.contains("id")) continue;
        std::string cid = c["id"];
        std::vector<int> vars;
        if (c.contains("refs"))
            for (const auto& r : c["refs"]) {
                std::string hh = r.get<std::string>();
                std::string eid = hh.substr(0, hh.find('.'));
                auto it = var_map.find(eid);
                if (it != var_map.end())
                    vars.insert(vars.end(), it->second.begin(), it->second.end());
            }
        tag_vars[cid] = vars;
    }
    auto statuses_map = compute_statuses(x, entities_list, var_map, n, J,
                                         analysis.conflicting_tags, tag_vars);
    json statuses = json::object();
    for (const auto& kv : statuses_map) statuses[kv.first] = kv.second;

    json rej = json::array();
    for (const auto& h : rejected_handles) rej.push_back(h);
    return json{{"ok", true}, {"entities", entity_coords}, {"statuses", statuses},
                {"rejected_handles", rej}, {"converged", res.converged},
                {"budget_exceeded", res.budget_exceeded}};
}

}  // namespace chudware::sketch
