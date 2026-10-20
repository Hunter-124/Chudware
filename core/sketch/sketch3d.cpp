// 3D free-space sketch solver — see sketch3d.h for the schema. Parameterizes
// point/line/arc/spline entities into the shared nsolve System and registers one
// residual block per constraint, mirroring the 2D solver's structure (allocate ->
// constrain -> solve -> analyze -> extract -> statuses) so the two read the same.
#include "sketch/sketch3d.h"

#include "solver/nsolve.h"
#include "util/expr.h"

#include <algorithm>
#include <limits>
#include <cmath>
#include <set>
#include <stdexcept>

namespace chudware::sketch3d {

using chudware::solver::System;
using Eigen::Matrix3d;
using Eigen::MatrixXd;
using Eigen::Vector3d;
using Eigen::VectorXd;
using nlohmann::json;

namespace {
constexpr double kPi = 3.14159265358979323846;

using EntMap = std::map<std::string, json>;
using VarMap = std::map<std::string, std::vector<int>>;

double num(const json& v) { return v.is_number() ? v.get<double>() : 0.0; }

Vector3d pt3(const json& a) {
    if (!a.is_array() || a.size() < 3) throw std::runtime_error("bad 3D point");
    return Vector3d(num(a[0]), num(a[1]), num(a[2]));
}

std::map<std::string, double> env_to_map(const json& env) {
    std::map<std::string, double> m;
    if (env.is_object())
        for (auto it = env.begin(); it != env.end(); ++it)
            if (it.value().is_number()) m[it.key()] = it.value().get<double>();
    return m;
}

// Dimension value resolution — same semantics as the 2D solver's resolve_dim:
// prefer a bare env lookup for the whole expr string, else evaluate as
// arithmetic, else the literal value. Forward-tolerant on evaluator errors.
double resolve_dim(const json& c, const json& env) {
    if (!c.contains("value")) return 0.0;
    const json& v = c["value"];
    if (v.is_number()) return v.get<double>();
    if (v.is_object()) {
        if (v.contains("expr") && v["expr"].is_string()) {
            std::string e = v["expr"];
            if (env.is_object() && env.contains(e) && env[e].is_number())
                return env[e].get<double>();
            try {
                return chudware::util::evaluate(e, env_to_map(env));
            } catch (const chudware::util::ExprError&) {
            }
        }
        if (v.contains("value") && v["value"].is_number()) return v["value"].get<double>();
    }
    return 0.0;
}

Matrix3d skew(const Vector3d& v) {
    Matrix3d m;
    m << 0, -v.z(), v.y(), v.z(), 0, -v.x(), -v.y(), v.x(), 0;
    return m;
}

// Orthonormal basis perpendicular to `axis` (the mate solver's perp_basis).
void perp_basis(const Vector3d& axis, Vector3d& e1, Vector3d& e2) {
    Vector3d a = axis.norm() > 1e-12 ? axis.normalized() : Vector3d(0, 0, 1);
    Vector3d helper = (std::abs(a[0]) < 0.9) ? Vector3d(1, 0, 0) : Vector3d(0, 1, 0);
    e1 = a.cross(helper).normalized();
    e2 = a.cross(e1).normalized();
}

// eq_curve3d: evaluate x(t)/y(t)/z(t) expression strings (doc equation grammar,
// chudware::util::evaluate — degrees trig, `env` globals, `t` bound per-call) at a
// given parameter value. Throws chudware::util::ExprError on a bad expression.
Vector3d eval_eq_curve3d(const json& e, double t, const json& env) {
    std::map<std::string, double> m = env_to_map(env);
    m["t"] = t;
    double x = chudware::util::evaluate(e.value("xt", std::string("0")), m);
    double y = chudware::util::evaluate(e.value("yt", std::string("0")), m);
    double z = chudware::util::evaluate(e.value("zt", std::string("0")), m);
    return Vector3d(x, y, z);
}

// Sample an eq_curve3d at N points over [t1, t2] inclusive (for wire compilation
// — see profile3d.cpp's build_edge3, which interpolates a BSpline through these
// exactly like the 2D eq_curve samples-to-spline compile).
std::vector<Vector3d> sample_eq_curve3d(const json& e, const json& env, int n = 33) {
    double t1 = num(e.value("t1", json(0.0)));
    double t2 = num(e.value("t2", json(1.0)));
    std::vector<Vector3d> pts;
    pts.reserve(n);
    for (int i = 0; i < n; ++i) {
        double t = t1 + (t2 - t1) * (static_cast<double>(i) / (n - 1));
        pts.push_back(eval_eq_curve3d(e, t, env));
    }
    return pts;
}

// -------------------------------------------------------------- entity layout
// Variable allocation order per entity (all coordinates are variables):
//   point  p(x,y,z)                          -> 3
//   line   p1(x,y,z) p2(x,y,z)               -> 6
//   arc    p1(x,y,z) pm(x,y,z) p2(x,y,z)     -> 9   (three-point arc)
//   spline points[0..N-1] each (x,y,z)       -> 3N
//   eq_curve3d p1(x,y,z)=P(t1) p2(x,y,z)=P(t2) -> 6 (rigid/fixed — see build_system's
//          post-allocation pin pass; v1 has no solve variables, matching the 2D
//          eq_curve "0 DOF — equation-fixed" convention. Endpoints are usable as
//          coincident/point_on targets like any fixed geometry.)
std::vector<int> allocate_entity(System& sys, const json& e, const json& env) {
    const std::string t = e.value("type", std::string());
    if (t == "eq_curve3d") {
        try {
            Vector3d a = eval_eq_curve3d(e, num(e.value("t1", json(0.0))), env);
            Vector3d b = eval_eq_curve3d(e, num(e.value("t2", json(1.0))), env);
            return sys.add_vars(6, {a.x(), a.y(), a.z(), b.x(), b.y(), b.z()});
        } catch (const chudware::util::ExprError&) {
            return sys.add_vars(6, {0, 0, 0, 0, 0, 0});
        }
    }
    if (t == "point") {
        Vector3d p = pt3(e.at("p"));
        return sys.add_vars(3, {p.x(), p.y(), p.z()});
    }
    if (t == "line") {
        Vector3d a = pt3(e.at("p1")), b = pt3(e.at("p2"));
        return sys.add_vars(6, {a.x(), a.y(), a.z(), b.x(), b.y(), b.z()});
    }
    if (t == "arc") {
        Vector3d a = pt3(e.at("p1")), m = pt3(e.at("pm")), b = pt3(e.at("p2"));
        return sys.add_vars(9, {a.x(), a.y(), a.z(), m.x(), m.y(), m.z(),
                                b.x(), b.y(), b.z()});
    }
    if (t == "spline") {
        const json& pts = e.at("points");
        if (!pts.is_array() || pts.size() < 2)
            throw std::runtime_error("spline needs >= 2 points");
        std::vector<double> init;
        init.reserve(pts.size() * 3);
        for (const auto& p : pts) {
            Vector3d v = pt3(p);
            init.push_back(v.x());
            init.push_back(v.y());
            init.push_back(v.z());
        }
        return sys.add_vars(static_cast<int>(init.size()), init);
    }
    // Unknown entity type: zero vars (forward-tolerant, like the 2D solver).
    return sys.add_vars(0, {});
}

// ---------------------------------------------------------------- handles
struct Handle3 {
    std::vector<int> idx;  // 3 indices for a point handle; all vars for a whole entity
    bool whole = false;
};

std::string eid_of(const std::string& h) { return h.substr(0, h.find('.')); }

// Resolve "e3", "e3.p", "e3.p1", "e3.p2", "e3.pm", "e3.p0".."e3.pN" (spline),
// plus 2D-uniform aliases "e3.start" (== p1) / "e3.end" (== p2) on line/arc.
Handle3 resolve_handle(const std::string& h, const EntMap& entities,
                       const VarMap& var_map) {
    std::string eid = h, sub;
    auto dot = h.find('.');
    if (dot != std::string::npos) {
        eid = h.substr(0, dot);
        sub = h.substr(dot + 1);
    }
    auto eit = entities.find(eid);
    auto vit = var_map.find(eid);
    if (eit == entities.end() || vit == var_map.end())
        throw std::runtime_error("unknown entity in handle '" + h + "'");
    const std::vector<int>& v = vit->second;
    const std::string t = eit->second.value("type", std::string());
    auto triple = [&](int k) {
        if (static_cast<size_t>(k * 3 + 2) >= v.size())
            throw std::runtime_error("handle '" + h + "' out of range");
        return Handle3{{v[k * 3], v[k * 3 + 1], v[k * 3 + 2]}, false};
    };
    if (sub.empty()) {
        if (t == "point") return triple(0);
        return Handle3{v, true};  // whole entity (fix)
    }
    if (t == "point" && sub == "p") return triple(0);
    if (t == "line" || t == "eq_curve3d") {
        if (sub == "p1" || sub == "start") return triple(0);
        if (sub == "p2" || sub == "end") return triple(1);
    }
    if (t == "arc") {
        if (sub == "p1" || sub == "start") return triple(0);
        if (sub == "pm" || sub == "mid") return triple(1);
        if (sub == "p2" || sub == "end") return triple(2);
    }
    if (t == "spline" && sub.size() > 1 && sub[0] == 'p') {
        int k = std::atoi(sub.c_str() + 1);
        if (k >= 0 && static_cast<size_t>(k * 3 + 2) < v.size()) return triple(k);
    }
    throw std::runtime_error("bad handle '" + h + "' for entity type '" + t + "'");
}

// A POINT handle (3 vars). Whole-entity refs resolve to their natural point where
// unambiguous (point entity); otherwise reject.
Handle3 resolve_point_handle(const std::string& h, const EntMap& entities,
                             const VarMap& var_map) {
    Handle3 hd = resolve_handle(h, entities, var_map);
    if (hd.idx.size() != 3)
        throw std::runtime_error("handle '" + h + "' is not a point");
    return hd;
}

// The 6 vars of a LINE entity referenced by a (possibly sub-less) handle.
std::vector<int> line_vars(const std::string& h, const EntMap& entities,
                           const VarMap& var_map) {
    std::string eid = eid_of(h);
    auto eit = entities.find(eid);
    auto vit = var_map.find(eid);
    if (eit == entities.end() || vit == var_map.end() ||
        eit->second.value("type", std::string()) != "line" || vit->second.size() != 6)
        throw std::runtime_error("'" + h + "' is not a line");
    return vit->second;
}

// ------------------------------------------------------------ residual blocks

// coincident: pa - pb = 0 (3 residuals, constant Jacobian [I | -I]).
void add_coincident(System& sys, const std::string& cid, const std::string& h0,
                    const std::string& h1, const EntMap& entities, const VarMap& vm) {
    Handle3 a = resolve_point_handle(h0, entities, vm);
    Handle3 b = resolve_point_handle(h1, entities, vm);
    std::vector<int> idx = a.idx;
    idx.insert(idx.end(), b.idx.begin(), b.idx.end());
    sys.add_residual(
        [](const VectorXd& x) {
            VectorXd r(3);
            r << x(0) - x(3), x(1) - x(4), x(2) - x(5);
            return r;
        },
        idx,
        [](const VectorXd&) {
            MatrixXd J = MatrixXd::Zero(3, 6);
            J.block(0, 0, 3, 3) = Matrix3d::Identity();
            J.block(0, 3, 3, 3) = -Matrix3d::Identity();
            return J;
        },
        cid);
}

// fix: pin the handle's (or whole entity's) variables at their CURRENT initial
// values (the authored coords), exactly like the 2D add_fix.
void add_fix(System& sys, const std::string& cid, const std::string& h,
             const EntMap& entities, const VarMap& vm) {
    Handle3 hd = resolve_handle(h, entities, vm);
    if (hd.idx.empty()) throw std::runtime_error("fix: entity has no coordinates");
    VectorXd init = sys.init_vector();
    std::vector<double> targets;
    for (int i : hd.idx) targets.push_back(init(i));
    const int m = static_cast<int>(hd.idx.size());
    sys.add_residual(
        [targets, m](const VectorXd& x) {
            VectorXd r(m);
            for (int i = 0; i < m; ++i) r(i) = x(i) - targets[i];
            return r;
        },
        hd.idx,
        [m](const VectorXd&) { return MatrixXd::Identity(m, m); },
        cid);
}

// distance point-point / length: |pa - pb| - d = 0.
void add_point_point_distance(System& sys, const std::string& cid,
                              const std::vector<int>& ia, const std::vector<int>& ib,
                              double d) {
    std::vector<int> idx = ia;
    idx.insert(idx.end(), ib.begin(), ib.end());
    sys.add_residual(
        [d](const VectorXd& x) {
            Vector3d a = x.segment<3>(0), b = x.segment<3>(3);
            VectorXd r(1);
            r(0) = (a - b).norm() - d;
            return r;
        },
        idx,
        [](const VectorXd& x) {
            Vector3d a = x.segment<3>(0), b = x.segment<3>(3);
            Vector3d u = a - b;
            double n = u.norm();
            if (n > 1e-12) u /= n;
            else u.setZero();
            MatrixXd J(1, 6);
            J << u.x(), u.y(), u.z(), -u.x(), -u.y(), -u.z();
            return J;
        },
        cid);
}

// distance point-line: |(p - a) x v| / |v| - d = 0 (perpendicular distance to the
// INFINITE line). Analytic gradients (w = p - a, v = b - a, c = w x v):
//   grad_p = (v x c^)/|v| ; grad_a = ((w - v) x c^)/|v| + |c| v^/|v|^2 ;
//   grad_b = (c^ x w)/|v| - |c| v^/|v|^2.
void add_point_line_distance(System& sys, const std::string& cid,
                             const std::vector<int>& ip, const std::vector<int>& il,
                             double d) {
    std::vector<int> idx = ip;
    idx.insert(idx.end(), il.begin(), il.end());
    sys.add_residual(
        [d](const VectorXd& x) {
            Vector3d p = x.segment<3>(0), a = x.segment<3>(3), b = x.segment<3>(6);
            Vector3d v = b - a;
            double nv = v.norm();
            VectorXd r(1);
            r(0) = (nv > 1e-12 ? (p - a).cross(v).norm() / nv : 0.0) - d;
            return r;
        },
        idx,
        [](const VectorXd& x) {
            Vector3d p = x.segment<3>(0), a = x.segment<3>(3), b = x.segment<3>(6);
            Vector3d w = p - a, v = b - a;
            Vector3d c = w.cross(v);
            double nc = c.norm(), nv = v.norm();
            MatrixXd J = MatrixXd::Zero(1, 9);
            if (nv < 1e-12) return J;
            Vector3d ch = nc > 1e-12 ? Vector3d(c / nc) : Vector3d::Zero();
            Vector3d vh = v / nv;
            Vector3d gp = v.cross(ch) / nv;
            Vector3d ga = (w - v).cross(ch) / nv + (nc / (nv * nv)) * vh;
            Vector3d gb = ch.cross(w) / nv - (nc / (nv * nv)) * vh;
            J << gp.x(), gp.y(), gp.z(), ga.x(), ga.y(), ga.z(), gb.x(), gb.y(), gb.z();
            return J;
        },
        cid);
}

// Direction Jacobians for one line's 6 vars laid out [a, b]: d = b - a.
// Jd (3 x 6) = [-I | I].
MatrixXd line_dir_jac() {
    MatrixXd J = MatrixXd::Zero(3, 6);
    J.block(0, 0, 3, 3) = -Matrix3d::Identity();
    J.block(0, 3, 3, 3) = Matrix3d::Identity();
    return J;
}

// parallel: two independent projections of d1 x d2 onto a basis perpendicular to
// d1 (mate-solver two_indep_cross pattern; basis treated locally constant).
void add_parallel(System& sys, const std::string& cid, const std::vector<int>& l1,
                  const std::vector<int>& l2) {
    std::vector<int> idx = l1;
    idx.insert(idx.end(), l2.begin(), l2.end());
    sys.add_residual(
        [](const VectorXd& x) {
            Vector3d d1 = x.segment<3>(3) - x.segment<3>(0);
            Vector3d d2 = x.segment<3>(9) - x.segment<3>(6);
            Vector3d c = d1.cross(d2);
            Vector3d e1, e2;
            perp_basis(d1, e1, e2);
            VectorXd r(2);
            r << c.dot(e1), c.dot(e2);
            return r;
        },
        idx,
        [](const VectorXd& x) {
            Vector3d d1 = x.segment<3>(3) - x.segment<3>(0);
            Vector3d d2 = x.segment<3>(9) - x.segment<3>(6);
            MatrixXd Jd1 = MatrixXd::Zero(3, 12), Jd2 = MatrixXd::Zero(3, 12);
            Jd1.block(0, 0, 3, 6) = line_dir_jac();
            Jd2.block(0, 6, 3, 6) = line_dir_jac();
            // c = d1 x d2 ; dc = -skew(d2) dd1 + skew(d1) dd2
            MatrixXd cross_jac = -skew(d2) * Jd1 + skew(d1) * Jd2;
            Vector3d e1, e2;
            perp_basis(d1, e1, e2);
            MatrixXd J(2, 12);
            J.row(0) = e1.transpose() * cross_jac;
            J.row(1) = e2.transpose() * cross_jac;
            return J;
        },
        cid);
}

// perpendicular: d1 . d2 = 0.
void add_perpendicular(System& sys, const std::string& cid, const std::vector<int>& l1,
                       const std::vector<int>& l2) {
    std::vector<int> idx = l1;
    idx.insert(idx.end(), l2.begin(), l2.end());
    sys.add_residual(
        [](const VectorXd& x) {
            Vector3d d1 = x.segment<3>(3) - x.segment<3>(0);
            Vector3d d2 = x.segment<3>(9) - x.segment<3>(6);
            VectorXd r(1);
            r(0) = d1.dot(d2);
            return r;
        },
        idx,
        [](const VectorXd& x) {
            Vector3d d1 = x.segment<3>(3) - x.segment<3>(0);
            Vector3d d2 = x.segment<3>(9) - x.segment<3>(6);
            MatrixXd Jd1 = MatrixXd::Zero(3, 12), Jd2 = MatrixXd::Zero(3, 12);
            Jd1.block(0, 0, 3, 6) = line_dir_jac();
            Jd2.block(0, 6, 3, 6) = line_dir_jac();
            MatrixXd J(1, 12);
            J = d2.transpose() * Jd1 + d1.transpose() * Jd2;
            return J;
        },
        cid);
}

// angle: d1 . d2 - cos(theta) |d1||d2| = 0 (theta in degrees, [0,180]).
void add_angle(System& sys, const std::string& cid, const std::vector<int>& l1,
               const std::vector<int>& l2, double deg) {
    constexpr double kPi = 3.14159265358979323846;
    const double ct = std::cos(deg * kPi / 180.0);
    std::vector<int> idx = l1;
    idx.insert(idx.end(), l2.begin(), l2.end());
    sys.add_residual(
        [ct](const VectorXd& x) {
            Vector3d d1 = x.segment<3>(3) - x.segment<3>(0);
            Vector3d d2 = x.segment<3>(9) - x.segment<3>(6);
            VectorXd r(1);
            r(0) = d1.dot(d2) - ct * d1.norm() * d2.norm();
            return r;
        },
        idx,
        [ct](const VectorXd& x) {
            Vector3d d1 = x.segment<3>(3) - x.segment<3>(0);
            Vector3d d2 = x.segment<3>(9) - x.segment<3>(6);
            double n1 = d1.norm(), n2 = d2.norm();
            Vector3d g1 = d2, g2 = d1;
            if (n1 > 1e-12) g1 -= ct * n2 * (d1 / n1);
            if (n2 > 1e-12) g2 -= ct * n1 * (d2 / n2);
            MatrixXd Jd1 = MatrixXd::Zero(3, 12), Jd2 = MatrixXd::Zero(3, 12);
            Jd1.block(0, 0, 3, 6) = line_dir_jac();
            Jd2.block(0, 6, 3, 6) = line_dir_jac();
            MatrixXd J(1, 12);
            J = g1.transpose() * Jd1 + g2.transpose() * Jd2;
            return J;
        },
        cid);
}

// along_axis: line direction parallel to a FIXED axis u ("x"|"y"|"z" or dir:[..]).
// Residuals project d onto a constant basis perpendicular to u (2 rows, analytic).
void add_along_axis(System& sys, const std::string& cid, const std::vector<int>& l,
                    const Vector3d& u) {
    Vector3d e1, e2;
    perp_basis(u, e1, e2);
    sys.add_residual(
        [e1, e2](const VectorXd& x) {
            Vector3d d = x.segment<3>(3) - x.segment<3>(0);
            VectorXd r(2);
            r << d.dot(e1), d.dot(e2);
            return r;
        },
        l,
        [e1, e2](const VectorXd&) {
            MatrixXd Jd = line_dir_jac();
            MatrixXd J(2, 6);
            J.row(0) = e1.transpose() * Jd;
            J.row(1) = e2.transpose() * Jd;
            return J;
        },
        cid);
}

// point_on(line): (p - a) x (b - a) = 0, projected onto a basis perpendicular to
// the line direction (2 rows; basis locally constant in the Jacobian).
void add_point_on_line(System& sys, const std::string& cid, const std::vector<int>& ip,
                       const std::vector<int>& il) {
    std::vector<int> idx = ip;
    idx.insert(idx.end(), il.begin(), il.end());
    sys.add_residual(
        [](const VectorXd& x) {
            Vector3d p = x.segment<3>(0), a = x.segment<3>(3), b = x.segment<3>(6);
            Vector3d v = b - a, c = (p - a).cross(v);
            Vector3d e1, e2;
            perp_basis(v, e1, e2);
            VectorXd r(2);
            r << c.dot(e1), c.dot(e2);
            return r;
        },
        idx,
        [](const VectorXd& x) {
            Vector3d p = x.segment<3>(0), a = x.segment<3>(3), b = x.segment<3>(6);
            Vector3d w = p - a, v = b - a;
            // c = w x v ; dc/dp = -skew(v) ; dc/da = skew(v) - skew(w) ; dc/db = skew(w)
            MatrixXd cross_jac = MatrixXd::Zero(3, 9);
            cross_jac.block(0, 0, 3, 3) = -skew(v);
            cross_jac.block(0, 3, 3, 3) = skew(v) - skew(w);
            cross_jac.block(0, 6, 3, 3) = skew(w);
            Vector3d e1, e2;
            perp_basis(v, e1, e2);
            MatrixXd J(2, 9);
            J.row(0) = e1.transpose() * cross_jac;
            J.row(1) = e2.transpose() * cross_jac;
            return J;
        },
        cid);
}

// Circumcenter + radius of the circle through A, M, B. Throws on collinear.
void circumcircle(const Vector3d& A, const Vector3d& M, const Vector3d& B,
                  Vector3d& center, double& radius, Vector3d& normal) {
    Vector3d u = M - A, v = B - A;
    Vector3d n = u.cross(v);
    double nn = n.squaredNorm();
    if (nn < 1e-18) throw std::runtime_error("degenerate (collinear) arc");
    // center = A + (|v|^2 (u.u) v_perp ... ) — standard barycentric form:
    // c = A + ( |u|^2 (v x n) + |v|^2 (n x u) ) / (2 |n|^2 )
    center = A + (u.squaredNorm() * v.cross(n) + v.squaredNorm() * n.cross(u)) / (2.0 * nn);
    radius = (center - A).norm();
    normal = n.normalized();
}

// point_on(arc): p lies on the circumcircle of the three-point arc (full circle,
// range-free — same convention as the 2D solver's point_on(arc)). Two residuals:
// coplanarity ((p - c) . n^) and rim (|p - c| - r). The circumcenter is a hairy
// rational function of the three arc points, so the Jacobian is CENTRAL-DIFFERENCE
// numeric (h = 1e-6) — precedent: the 2D solver's numeric conic projections.
void add_point_on_arc(System& sys, const std::string& cid, const std::vector<int>& ip,
                      const std::vector<int>& iarc) {
    if (iarc.size() != 9) throw std::runtime_error("point_on: arc has no coordinates");
    const VectorXd init = sys.init_vector();
    Vector3d a, m, b, center, normal;
    for (int k = 0; k < 3; ++k) {
        a(k) = init(iarc[k]); m(k) = init(iarc[k + 3]); b(k) = init(iarc[k + 6]);
    }
    double radius;
    circumcircle(a, m, b, center, radius, normal);
    std::vector<int> idx = ip;
    idx.insert(idx.end(), iarc.begin(), iarc.end());
    auto rfunc = [](const VectorXd& x) {
        Vector3d p = x.segment<3>(0);
        Vector3d A = x.segment<3>(3), M = x.segment<3>(6), B = x.segment<3>(9);
        VectorXd r(2);
        try {
            Vector3d c, n;
            double rad;
            circumcircle(A, M, B, c, rad, n);
            r(0) = (p - c).dot(n);
            r(1) = (p - c).norm() - rad;
        } catch (...) {
            r.setZero();  // degenerate arc: no pull (compile rejects it anyway)
        }
        return r;
    };
    sys.add_residual(
        rfunc, idx,
        [rfunc](const VectorXd& x) {
            const double h = 1e-6;
            MatrixXd J(2, x.size());
            VectorXd xp = x, xm = x;
            for (int k = 0; k < x.size(); ++k) {
                xp(k) = x(k) + h;
                xm(k) = x(k) - h;
                J.col(k) = (rfunc(xp) - rfunc(xm)) / (2.0 * h);
                xp(k) = x(k);
                xm(k) = x(k);
            }
            return J;
        },
        cid);
}

// Numeric Jacobians are reserved for derived circle/spline geometry; linear plane
// relations below use constant analytic rows. Preflight catches unusable geometry
// before registering a block, so it appears in skipped rather than going inert.
template <typename F>
void add_numeric(System& sys, const std::string& cid, const std::vector<int>& idx, F f) {
    const VectorXd init = sys.init_vector();
    VectorXd local(idx.size());
    for (size_t i = 0; i < idx.size(); ++i) local(i) = init(idx[i]);
    f(local);
    sys.add_residual(f, idx, [f](const VectorXd& x) {
        VectorXd xp = x, xm = x;
        MatrixXd J(f(x).size(), x.size());
        for (int k = 0; k < x.size(); ++k) {
            const double h = 1e-6 * std::max(1.0, std::abs(x(k)));
            xp(k) += h;
            xm(k) -= h;
            J.col(k) = (f(xp) - f(xm)) / (2.0 * h);
            xp(k) = xm(k) = x(k);
        }
        return J;
    }, cid);
}

struct Circle3 {
    Vector3d c, n;
    double r;
    explicit Circle3(const VectorXd& x) {
        circumcircle(x.segment<3>(0), x.segment<3>(3), x.segment<3>(6), c, r, n);
    }
    Vector3d midpoint(const Vector3d& a, const Vector3d& m, const Vector3d& b) const {
        const Vector3d u = (a - c) / r, v = n.cross(u);
        auto angle = [&](const Vector3d& p) {
            double t = std::atan2((p - c).dot(v), (p - c).dot(u));
            return t < 0 ? t + 2 * kPi : t;
        };
        double sweep = angle(b);
        if (angle(m) > sweep) sweep -= 2 * kPi;
        return c + r * (u * std::cos(sweep / 2) + v * std::sin(sweep / 2));
    }
};

// Same non-periodic interpolation as profile3d's GeomAPI_Interpolate:
// chord-length parameters; 2 points linear, 3 quadratic, otherwise C2 cubic with
// endpoint derivatives from the first/last four-point Lagrange interpolants.
// Keeping this solver-only avoids pulling OCCT into interactive solver builds.
struct Spline3 {
    std::vector<Vector3d> p, second;
    std::vector<double> t;
    explicit Spline3(const VectorXd& x) {
        const int n = static_cast<int>(x.size()) / 3;
        if (n < 2) throw std::runtime_error("spline needs >= 2 points");
        p.reserve(n);
        t.resize(n, 0);
        for (int i = 0; i < n; ++i) {
            p.emplace_back(x.segment<3>(3 * i));
            if (i) {
                double h = (p[i] - p[i - 1]).norm();
                if (h < 1e-7) throw std::runtime_error("spline has coincident adjacent points");
                t[i] = t[i - 1] + h;
            }
        }
        if (n < 4) return;
        second.resize(n);
        std::vector<double> diagonal(n), upper(n);
        auto derivative = [&](int first, int at) {
            Vector3d d = Vector3d::Zero();
            for (int j = first; j < first + 4; ++j) {
                double coefficient = 0;
                for (int k = first; k < first + 4; ++k) {
                    if (k == j) continue;
                    double term = 1 / (t[j] - t[k]);
                    for (int l = first; l < first + 4; ++l)
                        if (l != j && l != k) term *= (t[at] - t[l]) / (t[j] - t[l]);
                    coefficient += term;
                }
                d += coefficient * p[j];
            }
            return d;
        };
        double h = t[1] - t[0];
        diagonal[0] = 2 * h;
        upper[0] = h;
        second[0] = 6 * ((p[1] - p[0]) / h - derivative(0, 0));
        for (int i = 1; i < n; ++i) {
            const double prev = t[i] - t[i - 1];
            double lower = prev;
            if (i == n - 1) {
                diagonal[i] = 2 * prev;
                second[i] = 6 * (derivative(n - 4, n - 1) - (p[i] - p[i - 1]) / prev);
            } else {
                const double next = t[i + 1] - t[i];
                diagonal[i] = 2 * (prev + next);
                upper[i] = next;
                second[i] = 6 * ((p[i + 1] - p[i]) / next - (p[i] - p[i - 1]) / prev);
            }
            const double factor = lower / diagonal[i - 1];
            diagonal[i] -= factor * upper[i - 1];
            second[i] -= factor * second[i - 1];
        }
        second.back() /= diagonal.back();
        for (int i = n - 2; i >= 0; --i)
            second[i] = (second[i] - upper[i] * second[i + 1]) / diagonal[i];
    }
    std::pair<Vector3d, Vector3d> eval(double u) const {
        const int n = static_cast<int>(p.size());
        if (n < 4) {
            Vector3d q = Vector3d::Zero(), d = Vector3d::Zero();
            for (int j = 0; j < n; ++j) {
                double value = 1, derivative = 0;
                for (int k = 0; k < n; ++k)
                    if (k != j) value *= (u - t[k]) / (t[j] - t[k]);
                for (int k = 0; k < n; ++k) {
                    if (k == j) continue;
                    double term = 1 / (t[j] - t[k]);
                    for (int l = 0; l < n; ++l)
                        if (l != j && l != k) term *= (u - t[l]) / (t[j] - t[l]);
                    derivative += term;
                }
                q += value * p[j];
                d += derivative * p[j];
            }
            return {q, d};
        }
        int i = std::clamp(static_cast<int>(std::upper_bound(t.begin(), t.end(), u) - t.begin()) - 1,
                           0, n - 2);
        double h = t[i + 1] - t[i], a = (t[i + 1] - u) / h, b = (u - t[i]) / h;
        Vector3d q = a * p[i] + b * p[i + 1] +
            ((a * a * a - a) * second[i] + (b * b * b - b) * second[i + 1]) * h * h / 6;
        Vector3d d = (p[i + 1] - p[i]) / h +
            ((1 - 3 * a * a) * second[i] + (3 * b * b - 1) * second[i + 1]) * h / 6;
        return {q, d};
    }
    double closest_parameter(const Vector3d& point) const {
        double best = (point - p[0]).squaredNorm(), parameter = 0, left = 0, right = t[1];
        // Scan every span; then minimize in the best sample's neighboring bracket.
        for (size_t i = 0; i + 1 < t.size(); ++i) {
            double step = (t[i + 1] - t[i]) / 12;
            for (int k = 0; k <= 12; ++k) {
                double u = t[i] + k * step, d = (eval(u).first - point).squaredNorm();
                if (d <= best) {
                    best = d; parameter = u;
                    left = std::max(0.0, u - step);
                    right = std::min(t.back(), u + step);
                }
            }
        }
        for (int i = 0; i < 80; ++i) {
            double a = left + (right - left) * 0.3819660112501051;
            double b = left + (right - left) * 0.6180339887498949;
            if ((eval(a).first - point).squaredNorm() < (eval(b).first - point).squaredNorm())
                right = b;
            else left = a;
        }
        double u = (left + right) / 2;
        return (eval(u).first - point).squaredNorm() < best ? u : parameter;
    }
};

struct Plane3 { Vector3d origin, normal; };
Plane3 constraint_plane(const json& c) {
    if (!c.contains("plane")) throw std::runtime_error("plane is required");
    const json& p = c["plane"];
    if (p.is_string()) {
        const std::string name = p.get<std::string>();
        // Match eval_resolve_plane / SketchPlane factories, including front's sign.
        if (name == "front") return {Vector3d::Zero(), Vector3d(0, -1, 0)};
        if (name == "top") return {Vector3d::Zero(), Vector3d(0, 0, 1)};
        if (name == "right") return {Vector3d::Zero(), Vector3d(1, 0, 0)};
        throw std::runtime_error("unknown datum plane '" + name + "'");
    }
    Vector3d origin = pt3(p.at("origin")), normal = pt3(p.at("normal"));
    if (!origin.allFinite() || !normal.allFinite() || normal.norm() < 1e-12)
        throw std::runtime_error("plane needs finite origin and nonzero normal");
    return {origin, normal.normalized()};
}

void add_plane_constraint(System& sys, const std::string& cid, const std::string& type,
                          const std::vector<std::string>& refs, const Plane3& plane,
                          const EntMap& entities, const VarMap& vm) {
    const Vector3d n = plane.normal, o = plane.origin;
    if (type == "normal_plane") {
        add_along_axis(sys, cid, line_vars(refs[0], entities, vm), n);
        return;
    }
    if (type == "parallel_plane") {
        const auto idx = line_vars(refs[0], entities, vm);
        sys.add_residual([n](const VectorXd& x) {
            VectorXd r(1); r(0) = n.dot(x.segment<3>(3) - x.segment<3>(0)); return r;
        }, idx, [n](const VectorXd&) {
            MatrixXd J(1, 6); J << -n.transpose(), n.transpose(); return J;
        }, cid);
        return;
    }
    if (type == "symmetric") {
        auto idx = resolve_point_handle(refs[0], entities, vm).idx;
        auto other = resolve_point_handle(refs[1], entities, vm).idx;
        idx.insert(idx.end(), other.begin(), other.end());
        const Matrix3d mirror = Matrix3d::Identity() - 2 * n * n.transpose();
        sys.add_residual([mirror, o](const VectorXd& x) {
            VectorXd r = x.segment<3>(3) - o - mirror * (x.segment<3>(0) - o); return r;
        }, idx, [mirror](const VectorXd&) {
            MatrixXd J(3, 6); J << -mirror, Matrix3d::Identity(); return J;
        }, cid);
        return;
    }
    Handle3 h = resolve_handle(refs[0], entities, vm);
    const std::string t = entities.at(eid_of(refs[0])).value("type", std::string());
    if (h.whole && t != "line" && t != "arc")
        throw std::runtime_error("on_plane expects a point, line or arc");
    const int count = static_cast<int>(h.idx.size()) / 3;
    if (!count) throw std::runtime_error("on_plane has no point coordinates");
    sys.add_residual([n, o, count](const VectorXd& x) {
        VectorXd r(count);
        for (int i = 0; i < count; ++i) r(i) = n.dot(x.segment<3>(i * 3) - o);
        return r;
    }, h.idx, [n, count](const VectorXd&) {
        MatrixXd J = MatrixXd::Zero(count, count * 3);
        for (int i = 0; i < count; ++i) J.block<1, 3>(i, i * 3) = n.transpose();
        return J;
    }, cid);
}

void add_curve_relation(System& sys, const std::string& cid, const std::string& type,
                         const std::vector<std::string>& refs, const EntMap& entities,
                         const VarMap& vm) {
    const std::string a = eid_of(refs[0]), b = eid_of(refs[1]);
    const std::string ta = entities.at(a).value("type", std::string());
    const std::string tb = entities.at(b).value("type", std::string());
    if ((ta == "line" && vm.at(a).size() != 6) || (tb == "line" && vm.at(b).size() != 6) ||
        (ta == "arc" && vm.at(a).size() != 9) || (tb == "arc" && vm.at(b).size() != 9))
        throw std::runtime_error(type + ": missing curve coordinates");
    auto idx = vm.at(a);
    const int split = static_cast<int>(idx.size());
    idx.insert(idx.end(), vm.at(b).begin(), vm.at(b).end());
    if (type == "equal" && ta == "line" && tb == "line") {
        add_numeric(sys, cid, idx, [](const VectorXd& x) {
            VectorXd r(1);
            r(0) = (x.segment<3>(3) - x.segment<3>(0)).norm() -
                   (x.segment<3>(9) - x.segment<3>(6)).norm();
            return r;
        });
    } else if ((type == "equal" || type == "concentric" || type == "coradial") &&
               ta == "arc" && tb == "arc") {
        add_numeric(sys, cid, idx, [type](const VectorXd& x) {
            Circle3 a(x.head(9)), b(x.tail(9));
            VectorXd r(type == "equal" ? 1 : type == "coradial" ? 4 : 3);
            if (type != "equal") r.head<3>() = a.c - b.c;
            if (type != "concentric") r(r.size() - 1) = a.r - b.r;
            return r;
        });
    } else if (type == "tangent" &&
               ((ta == "line" && (tb == "arc" || tb == "spline")) ||
                (tb == "line" && (ta == "arc" || ta == "spline")) ||
                (ta == "arc" && tb == "arc"))) {
        // A supplied endpoint selects that end; bare curves choose their nearest
        // authored endpoint pair, and keep that branch through the solve.
        const VectorXd init = sys.init_vector();
        auto ends = [&](const std::string& ref, const std::string& t, const std::vector<int>& v) {
            if (ref.find('.') != std::string::npos) {
                auto h = resolve_point_handle(ref, entities, vm);
                if (h.idx[0] != v.front() && h.idx[0] != v[v.size() - 3])
                    throw std::runtime_error("tangent requires a curve endpoint");
                return std::vector<int>{h.idx[0] == v.front() ? 0 : static_cast<int>(v.size()) - 3};
            }
            return std::vector<int>{0, t == "line" ? 3 : static_cast<int>(v.size()) - 3};
        };
        auto ea = ends(refs[0], ta, vm.at(a)), eb = ends(refs[1], tb, vm.at(b));
        int ka = 0, kb = 0;
        double best = std::numeric_limits<double>::infinity();
        for (int i : ea) for (int j : eb) {
            Vector3d pa, pb;
            for (int k = 0; k < 3; ++k) { pa(k) = init(vm.at(a)[i + k]); pb(k) = init(vm.at(b)[j + k]); }
            if ((pa - pb).squaredNorm() < best) { best = (pa - pb).squaredNorm(); ka = i; kb = j; }
        }
        add_numeric(sys, cid, idx, [ta, tb, split, ka, kb](const VectorXd& x) {
            auto tangent = [](const VectorXd& v, const std::string& type, int end) -> Vector3d {
                if (type == "line") return v.segment<3>(3) - v.segment<3>(0);
                if (type == "arc") {
                    Circle3 circle(v);
                    return circle.n.cross(v.segment<3>(end) - circle.c);
                }
                Spline3 spline(v);
                return spline.eval(end == 0 ? 0 : spline.t.back()).second;
            };
            Vector3d da = tangent(x.head(split), ta, ka), db = tangent(x.tail(x.size() - split), tb, kb);
            if (da.norm() < 1e-12 || db.norm() < 1e-12)
                throw std::runtime_error("tangent has a degenerate direction");
            da.normalize(); db.normalize();
            Vector3d e1, e2;
            perp_basis(da, e1, e2);
            const Vector3d cross = da.cross(db);
            VectorXd r(2);
            r << cross.dot(e1), cross.dot(e2);
            return r;
        });
    } else {
        throw std::runtime_error(type + ": unsupported entity pair '" + ta + "', '" + tb + "'");
    }
}

// ------------------------------------------------------------ constraint wiring
void add_constraint(System& sys, const json& c, const EntMap& entities,
                    const VarMap& vm, const json& env) {
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
    if (c.value("driven", false)) return;  // reference dims are measured, not enforced

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

    if (ctype == "coincident") {
        need(2);
        add_coincident(sys, cid, refs[0], refs[1], entities, vm);
    } else if (ctype == "fix") {
        need(1);
        add_fix(sys, cid, refs[0], entities, vm);
    } else if (ctype == "distance") {
        // 1 bare line ref → length; 2 refs → point-point / point-line.
        if (refs.size() == 1) {
            const std::string& h = refs[0];
            bool line_ref = h.find('.') == std::string::npos && entities.count(h) &&
                            entities.at(h).value("type", std::string()) == "line";
            if (!line_ref)
                throw std::runtime_error(
                    "distance constraint needs 2 refs (got 1): '" + h + "'" +
                    (cid.empty() ? std::string() : " [" + cid + "]") +
                    " — single-ref distance is only valid on a bare line (line length)");
            std::vector<int> l = line_vars(h, entities, vm);
            add_point_point_distance(sys, cid, {l[0], l[1], l[2]}, {l[3], l[4], l[5]},
                                     resolve_dim(c, env));
        } else {
            need(2);
            double d = resolve_dim(c, env);
            const std::string& h1 = refs[1];
            bool line_ref = h1.find('.') == std::string::npos && entities.count(h1) &&
                            entities.at(h1).value("type", std::string()) == "line";
            Handle3 p = resolve_point_handle(refs[0], entities, vm);
            if (line_ref)
                add_point_line_distance(sys, cid, p.idx, line_vars(h1, entities, vm), d);
            else
                add_point_point_distance(sys, cid, p.idx,
                                         resolve_point_handle(h1, entities, vm).idx, d);
        }
    } else if (ctype == "length") {
        need(1);
        std::vector<int> l = line_vars(refs[0], entities, vm);
        add_point_point_distance(sys, cid, {l[0], l[1], l[2]}, {l[3], l[4], l[5]},
                                 resolve_dim(c, env));
    } else if (ctype == "parallel") {
        need(2);
        add_parallel(sys, cid, line_vars(refs[0], entities, vm),
                     line_vars(refs[1], entities, vm));
    } else if (ctype == "on_plane" || ctype == "parallel_plane" ||
               ctype == "normal_plane" || ctype == "symmetric") {
        need(ctype == "symmetric" ? 2 : 1);
        add_plane_constraint(sys, cid, ctype, refs, constraint_plane(c), entities, vm);
    } else if (ctype == "equal" || ctype == "tangent" || ctype == "concentric" ||
               ctype == "coradial") {
        need(2);
        add_curve_relation(sys, cid, ctype, refs, entities, vm);
    } else if (ctype == "collinear") {
        need(2);
        const auto a = line_vars(refs[0], entities, vm), b = line_vars(refs[1], entities, vm);
        add_parallel(sys, cid, a, b);
        add_point_on_line(sys, cid, {b[0], b[1], b[2]}, a);
    } else if (ctype == "midpoint") {
        need(2);
        auto idx = resolve_point_handle(refs[0], entities, vm).idx;
        const std::string eid = eid_of(refs[1]);
        const std::string type = entities.at(eid).value("type", std::string());
        if (type != "line" && type != "arc")
            throw std::runtime_error("midpoint expects a point and a line or arc");
        const auto& curve = vm.at(eid);
        if (curve.size() != (type == "line" ? 6u : 9u))
            throw std::runtime_error("midpoint: missing curve coordinates");
        idx.insert(idx.end(), curve.begin(), curve.end());
        add_numeric(sys, cid, idx, [type](const VectorXd& x) {
            Vector3d mid;
            if (type == "line") mid = (x.segment<3>(3) + x.segment<3>(6)) / 2;
            else {
                Circle3 circle(x.tail(9));
                mid = circle.midpoint(x.segment<3>(3), x.segment<3>(6), x.segment<3>(9));
            }
            VectorXd r = x.head<3>() - mid;
            return r;
        });
    } else if (ctype == "perpendicular") {
        need(2);
        add_perpendicular(sys, cid, line_vars(refs[0], entities, vm),
                          line_vars(refs[1], entities, vm));
    } else if (ctype == "angle") {
        need(2);
        add_angle(sys, cid, line_vars(refs[0], entities, vm),
                  line_vars(refs[1], entities, vm), resolve_dim(c, env));
    } else if (ctype == "along_axis") {
        need(1);
        Vector3d u(0, 0, 1);
        if (c.contains("axis") && c["axis"].is_string()) {
            std::string a = c["axis"].get<std::string>();
            if (a == "x") u = Vector3d(1, 0, 0);
            else if (a == "y") u = Vector3d(0, 1, 0);
            else if (a == "z") u = Vector3d(0, 0, 1);
            else throw std::runtime_error("along_axis: bad axis '" + a + "'");
        } else if (c.contains("dir") && c["dir"].is_array()) {
            u = pt3(c["dir"]);
            if (u.norm() < 1e-12) throw std::runtime_error("along_axis: zero dir");
        }
        add_along_axis(sys, cid, line_vars(refs[0], entities, vm), u);
    } else if (ctype == "point_on") {
        need(2);
        Handle3 p = resolve_point_handle(refs[0], entities, vm);
        const std::string eid = eid_of(refs[1]);
        auto eit = entities.find(eid);
        if (eit == entities.end()) throw std::runtime_error("point_on: unknown entity");
        const std::string t = eit->second.value("type", std::string());
        if (t == "line")
            add_point_on_line(sys, cid, p.idx, line_vars(eid, entities, vm));
        else if (t == "arc")
            add_point_on_arc(sys, cid, p.idx, vm.at(eid));
        else if (t == "spline") {
            auto idx = p.idx;
            const auto& curve = vm.at(eid);
            idx.insert(idx.end(), curve.begin(), curve.end());
            add_numeric(sys, cid, idx, [](const VectorXd& x) {
                Spline3 spline(x.tail(x.size() - 3));
                const double parameter = spline.closest_parameter(x.head<3>());
                auto curve = spline.eval(parameter);
                Vector3d e1, e2;
                perp_basis(curve.second, e1, e2);
                const Vector3d delta = x.head<3>() - curve.first;
                VectorXd r(3);
                r << delta.dot(e1), delta.dot(e2),
                    parameter == 0 || parameter == spline.t.back()
                        ? delta.dot(curve.second.normalized()) : 0.0;
                return r;
            });
        } else
            throw std::runtime_error("point_on: unsupported target '" + t + "'");
    } else {
        throw std::runtime_error("unknown constraint '" + ctype + "'");
    }
}

// ------------------------------------------------------------------- extraction
json extract_entities(const VectorXd& x, const json& entities_list, const VarMap& vm,
                      const json& env) {
    json out = json::object();
    auto arr3 = [&](const std::vector<int>& v, int k) {
        return json::array({x(v[k * 3]), x(v[k * 3 + 1]), x(v[k * 3 + 2])});
    };
    for (const auto& e : entities_list) {
        const std::string id = e.value("id", std::string());
        const std::string t = e.value("type", std::string());
        auto vit = vm.find(id);
        if (vit == vm.end()) continue;
        const std::vector<int>& v = vit->second;
        json rec = json{{"type", t}, {"construction", e.value("construction", false)}};
        if (t == "eq_curve3d" && v.size() >= 6) {
            rec["p1"] = arr3(v, 0);
            rec["p2"] = arr3(v, 1);
            rec["xt"] = e.value("xt", std::string());
            rec["yt"] = e.value("yt", std::string());
            rec["zt"] = e.value("zt", std::string());
            rec["t1"] = num(e.value("t1", json(0.0)));
            rec["t2"] = num(e.value("t2", json(1.0)));
            json samples = json::array();
            try {
                for (const Vector3d& p : sample_eq_curve3d(e, env))
                    samples.push_back(json::array({p.x(), p.y(), p.z()}));
            } catch (const chudware::util::ExprError&) {
                // leave samples empty — profile3d skips a bad eq_curve3d edge
            }
            rec["samples"] = std::move(samples);
        } else if (t == "point" && v.size() >= 3) {
            rec["p"] = arr3(v, 0);
        } else if (t == "line" && v.size() >= 6) {
            rec["p1"] = arr3(v, 0);
            rec["p2"] = arr3(v, 1);
        } else if (t == "arc" && v.size() >= 9) {
            rec["p1"] = arr3(v, 0);
            rec["pm"] = arr3(v, 1);
            rec["p2"] = arr3(v, 2);
        } else if (t == "spline" && v.size() >= 6) {
            json pts = json::array();
            for (size_t k = 0; k * 3 + 2 < v.size(); ++k) pts.push_back(arr3(v, (int)k));
            rec["points"] = pts;
            json samples = json::array();
            try {
                Spline3 spline(x.segment(v.front(), v.size()));
                constexpr int steps = 12;
                for (size_t span = 0; span + 1 < spline.t.size(); ++span)
                    for (int k = span == 0 ? 0 : 1; k <= steps; ++k) {
                        const double u = spline.t[span] +
                            (spline.t[span + 1] - spline.t[span]) * k / steps;
                        const Vector3d p = spline.eval(u).first;
                        samples.push_back(json::array({p.x(), p.y(), p.z()}));
                    }
            } catch (const std::runtime_error&) {
                // Coincident interpolation points cannot produce a compiled wire.
                // Keep their handles, but do not draw an invented substitute curve.
            }
            rec["samples"] = std::move(samples);
        } else {
            rec = e;  // unknown type: echo authored params
            rec.erase("id");
        }
        out[id] = std::move(rec);
    }
    return out;
}

// Per-entity status coloring — same SVD null-space projection as the 2D solver's
// compute_statuses: a variable is FREE when it has a component in the Jacobian's
// null space; an entity is "full" when none of its variables are free, "over"
// when a conflicting constraint touches it.
std::map<std::string, std::string> compute_statuses(
    const json& entities_list, const VarMap& var_map, int n_vars, const MatrixXd& J,
    const std::vector<std::string>& conflicting,
    const std::map<std::string, std::vector<int>>& tag_vars) {
    std::map<std::string, std::string> statuses;
    VectorXd null_proj = VectorXd::Ones(n_vars);  // no constraints: all free
    if (J.rows() > 0 && n_vars > 0) {
        Eigen::JacobiSVD<MatrixXd> svd(J, Eigen::ComputeFullV);
        const auto& s = svd.singularValues();
        double smax = s.size() ? s(0) : 0.0;
        // Cast both to double so MSVC can pick std::max without Eigen::Index vs long ambiguity.
        double thr = std::max(static_cast<double>(J.rows()), static_cast<double>(n_vars)) * smax * 1e-10;
        int rank = 0;
        for (int i = 0; i < s.size(); ++i)
            if (s(i) > thr) ++rank;
        int n_null = n_vars - rank;
        null_proj.setZero();
        if (n_null > 0) {
            const MatrixXd& V = svd.matrixV();
            MatrixXd Vn = V.block(0, rank, n_vars, n_null);
            for (int i = 0; i < n_vars; ++i) null_proj(i) = Vn.row(i).norm();
        }
    }
    const double tol_null = 1e-6;
    std::set<int> conflicting_vars;
    for (const auto& tag : conflicting) {
        auto it = tag_vars.find(tag);
        if (it != tag_vars.end())
            for (int v : it->second) conflicting_vars.insert(v);
    }
    for (const auto& e : entities_list) {
        std::string id = e.value("id", std::string());
        auto vit = var_map.find(id);
        if (vit == var_map.end()) continue;
        const std::vector<int>& v = vit->second;
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

// Union of the variable blocks a constraint's refs touch (for "over" coloring).
std::map<std::string, std::vector<int>> build_tag_vars(const json& constraints_list,
                                                       const VarMap& var_map) {
    std::map<std::string, std::vector<int>> tag_vars;
    for (const auto& c : constraints_list) {
        if (!c.contains("id")) continue;
        std::string cid = c["id"];
        std::vector<int> vars;
        if (c.contains("refs"))
            for (const auto& r : c["refs"]) {
                if (!r.is_string()) continue;
                std::string eid = eid_of(r.get<std::string>());
                auto it = var_map.find(eid);
                if (it != var_map.end())
                    vars.insert(vars.end(), it->second.begin(), it->second.end());
            }
        tag_vars[cid] = vars;
    }
    return tag_vars;
}

// Build the System (allocate entities + register constraints, forward-tolerant).
void build_system(const json& entities_list, const json& constraints_list,
                  const json& env, System& sys, EntMap& entities, VarMap& var_map,
                  std::vector<std::pair<std::string, std::string>>* skipped = nullptr) {
    for (const auto& e : entities_list) entities[e.value("id", std::string())] = e;
    for (const auto& e : entities_list) {
        try {
            var_map[e.value("id", std::string())] = allocate_entity(sys, e, env);
        } catch (...) {
            var_map[e.value("id", std::string())] = sys.add_vars(0, {});
        }
    }
    // eq_curve3d is rigid/equation-fixed (v1, mirroring the 2D eq_curve "0 DOF"
    // convention): pin its 6 vars right after allocation so no downstream
    // constraint can move the equation-computed endpoints — they're only usable
    // as fixed targets (coincident/point_on/distance), not free geometry.
    for (const auto& e : entities_list) {
        if (e.value("type", std::string()) != "eq_curve3d") continue;
        const std::string id = e.value("id", std::string());
        auto vit = var_map.find(id);
        if (vit == var_map.end() || vit->second.size() != 6) continue;
        VectorXd init = sys.init_vector();
        std::vector<double> targets;
        for (int i : vit->second) targets.push_back(init(i));
        std::vector<int> idx = vit->second;
        sys.add_residual(
            [targets](const VectorXd& x) {
                VectorXd r(6);
                for (int i = 0; i < 6; ++i) r(i) = x(i) - targets[i];
                return r;
            },
            idx,
            [](const VectorXd&) { return MatrixXd::Identity(6, 6); },
            "__eq_curve3d_pin_" + id);
    }
    for (const auto& c : constraints_list) {
        try {
            add_constraint(sys, c, entities, var_map, env);
        } catch (const std::exception& error) {
            if (skipped) skipped->emplace_back(c.is_object() ? c.value("id", std::string()) : "",
                                              error.what());
        } catch (...) {
            if (skipped) skipped->emplace_back(c.is_object() ? c.value("id", std::string()) : "",
                                              "unusable constraint");
        }
    }
}

}  // namespace

Sketch3DSolution solve_sketch3d(const json& sketch, const json& env,
                                const std::optional<SolveState3>& warm) {
    json entities_list = sketch.value("entities", json::array());
    json constraints_list = sketch.value("constraints", json::array());

    System sys;
    EntMap entities;
    VarMap var_map;
    Sketch3DSolution sol;
    build_system(entities_list, constraints_list, env, sys, entities, var_map, &sol.skipped);

    std::map<std::string, std::string> cur_sig;
    for (const auto& e : entities_list) cur_sig[e.value("id", std::string())] = e.dump();

    // Per-entity warm start, gated on the entity signature (same as 2D).
    std::optional<VectorXd> x0;
    if (warm) {
        if (warm->ent_sig.empty()) {
            if (warm->n_vars == sys.n_vars()) x0 = warm->x;
        } else {
            VectorXd base = sys.init_vector();
            for (const auto& kv : var_map) {
                auto wit = warm->var_map.find(kv.first);
                if (wit == warm->var_map.end() || wit->second.size() != kv.second.size())
                    continue;
                auto sit = warm->ent_sig.find(kv.first);
                auto cit = cur_sig.find(kv.first);
                if (sit == warm->ent_sig.end() || cit == cur_sig.end() ||
                    sit->second != cit->second)
                    continue;
                for (size_t i = 0; i < kv.second.size(); ++i)
                    base(kv.second[i]) = warm->x(wit->second[i]);
            }
            x0 = base;
        }
    }

    auto res = sys.solve(x0, 1e-10, 50);
    sol.converged = res.converged;

    auto analysis = sys.analyze(res.x);
    sol.dof = analysis.dof;
    sol.redundant = analysis.redundant_tags;
    sol.conflicting = analysis.conflicting_tags;

    sol.entities = extract_entities(res.x, entities_list, var_map, env);
    MatrixXd J = sys.real_jacobian(res.x);
    sol.statuses = compute_statuses(entities_list, var_map, sys.n_vars(), J,
                                    sol.conflicting, build_tag_vars(constraints_list, var_map));

    sol.state.x = res.x;
    sol.state.var_map = var_map;
    sol.state.n_vars = sys.n_vars();
    sol.state.ent_sig = cur_sig;
    return sol;
}

Sketch3DSolution solve_sketch3d(const json& sketch, const json& env) {
    return solve_sketch3d(sketch, env, std::nullopt);
}

std::vector<FreeVar3> free_variables3(const json& sketch, const json& env) {
    json entities_list = sketch.value("entities", json::array());
    json constraints_list = sketch.value("constraints", json::array());

    System sys;
    EntMap entities;
    VarMap var_map;
    build_system(entities_list, constraints_list, env, sys, entities, var_map);

    auto res = sys.solve(std::nullopt, 1e-10, 50);
    const int n = sys.n_vars();
    MatrixXd J = sys.real_jacobian(res.x);
    VectorXd null_proj = VectorXd::Ones(n);
    if (J.rows() > 0 && n > 0) {
        Eigen::JacobiSVD<MatrixXd> svd(J, Eigen::ComputeFullV);
        const auto& s = svd.singularValues();
        double smax = s.size() ? s(0) : 0.0;
        // Cast both to double so MSVC can pick std::max without Eigen::Index vs long ambiguity.
        double thr = std::max(static_cast<double>(J.rows()), static_cast<double>(n)) * smax * 1e-10;
        int rank = 0;
        for (int i = 0; i < s.size(); ++i)
            if (s(i) > thr) ++rank;
        int n_null = n - rank;
        null_proj.setZero();
        if (n_null > 0) {
            MatrixXd Vn = svd.matrixV().block(0, rank, n, n_null);
            for (int i = 0; i < n; ++i) null_proj(i) = Vn.row(i).norm();
        }
    }

    // Semantic names per entity type, in allocation order.
    static const char* kAxes[] = {"x", "y", "z"};
    std::vector<FreeVar3> out;
    for (const auto& e : entities_list) {
        const std::string id = e.value("id", std::string());
        const std::string t = e.value("type", std::string());
        auto vit = var_map.find(id);
        if (vit == var_map.end()) continue;
        const std::vector<int>& v = vit->second;
        for (size_t k = 0; k < v.size(); ++k) {
            if (null_proj(v[k]) < 1e-6) continue;
            std::string param;
            int grp = static_cast<int>(k / 3);
            const char* ax = kAxes[k % 3];
            if (t == "point") param = std::string("p ") + ax;
            else if (t == "line") param = (grp == 0 ? std::string("p1 ") : std::string("p2 ")) + ax;
            else if (t == "arc")
                param = (grp == 0 ? std::string("p1 ")
                                  : grp == 1 ? std::string("pm ") : std::string("p2 ")) + ax;
            else if (t == "spline")
                param = "p" + std::to_string(grp) + " " + ax;
            else
                param = "v" + std::to_string(k);
            out.push_back(FreeVar3{id, param, v[k]});
        }
    }
    return out;
}

json drag3(const SolveState3& state, const std::string& handle,
           const Vector3d& target_xyz, const json& sketch, const json& env,
           const json& targets) {
    json entities_list = sketch.value("entities", json::array());
    json constraints_list = sketch.value("constraints", json::array());

    System sys;
    EntMap entities;
    VarMap var_map;
    build_system(entities_list, constraints_list, env, sys, entities, var_map);

    // Normalize to (handle, xyz) pairs; `targets` wins over handle/target_xyz.
    std::vector<std::pair<std::string, Vector3d>> target_pairs;
    if (targets.is_array() && !targets.empty()) {
        for (const auto& t : targets) {
            if (!t.is_object()) continue;
            std::string h = t.value("handle", std::string());
            const json xyz = t.contains("xyz") ? t["xyz"] : json();
            if (h.empty() || !xyz.is_array() || xyz.size() < 3) continue;
            target_pairs.emplace_back(h, Vector3d(num(xyz[0]), num(xyz[1]), num(xyz[2])));
        }
    } else if (!handle.empty()) {
        target_pairs.emplace_back(handle, target_xyz);
    }

    bool any_target = false;
    std::vector<std::string> rejected_handles;
    for (const auto& tp : target_pairs) {
        Handle3 hd;
        try {
            hd = resolve_point_handle(tp.first, entities, var_map);
        } catch (...) {
            rejected_handles.push_back(tp.first);
            continue;
        }
        for (int k = 0; k < 3; ++k) sys.soft_target(hd.idx[k], tp.second(k), 0.01);
        any_target = true;
    }

    if (!any_target) {
        json rej = json::array();
        for (const auto& h : rejected_handles) rej.push_back(h);
        if (rej.empty())
            for (const auto& tp : target_pairs) rej.push_back(tp.first);
        return json{{"ok", false}, {"error", "no drag targets resolved"},
                    {"rejected_handles", rej},
                    {"entities", extract_entities(
                                     state.n_vars == sys.n_vars() ? state.x
                                                                  : sys.init_vector(),
                                     entities_list, var_map, env)},
                    {"statuses", json::object()}, {"converged", true},
                    {"budget_exceeded", false}};
    }

    std::optional<VectorXd> x0;
    if (state.n_vars == sys.n_vars()) x0 = state.x;
    auto res = sys.solve(x0, 1e-10, 50, /*time_budget_ms=*/10.0);
    VectorXd x = res.x;
    if (!x.allFinite()) x = (x0 && x0->allFinite()) ? *x0 : sys.init_vector();

    auto analysis = sys.analyze(x);
    MatrixXd J = sys.real_jacobian(x);
    auto statuses_map = compute_statuses(entities_list, var_map, sys.n_vars(), J,
                                         analysis.conflicting_tags,
                                         build_tag_vars(constraints_list, var_map));
    json statuses = json::object();
    for (const auto& kv : statuses_map) statuses[kv.first] = kv.second;
    json rej = json::array();
    for (const auto& h : rejected_handles) rej.push_back(h);
    return json{{"ok", true}, {"entities", extract_entities(x, entities_list, var_map, env)},
                {"statuses", statuses}, {"rejected_handles", rej},
                {"converged", res.converged}, {"budget_exceeded", res.budget_exceeded}};
}

}  // namespace chudware::sketch3d
