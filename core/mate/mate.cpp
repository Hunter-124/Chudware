// C++ port of engine/doc/mate_solver.py. Residual/Jacobian formulas are copied
// from the Python so the two stay at parity under tools/mate_diff.py. See mate.h
// for the coverage scope. OCCT-free: only solver/Eigen/json.
#include "mate/mate.h"

#include "solver/nsolve.h"

#include <algorithm>
#include <cmath>
#include <functional>
#include <memory>
#include <random>
#include <stdexcept>
#include <tuple>
#include <utility>

namespace aicad::mate {
namespace {

using json = nlohmann::json;
using Eigen::Matrix3d;
using Eigen::MatrixXd;
using Eigen::Vector3d;
using Eigen::VectorXd;
using aicad::solver::System;

constexpr double kPi = 3.14159265358979323846;

double deg2rad(double d) { return d * kPi / 180.0; }

// --------------------------------------------------------------------------- rotation helpers

Matrix3d skew(const Vector3d& v) {
    Matrix3d K;
    K << 0.0, -v[2], v[1],
         v[2], 0.0, -v[0],
         -v[1], v[0], 0.0;
    return K;
}

Matrix3d rodrigues_impl(const Vector3d& w) {
    double theta = w.norm();
    Matrix3d K = skew(w);
    if (theta < 1e-8) {
        return Matrix3d::Identity() + K + 0.5 * (K * K);
    }
    double a = std::sin(theta) / theta;
    double b = (1.0 - std::cos(theta)) / (theta * theta);
    return Matrix3d::Identity() + a * K + b * (K * K);
}

Matrix3d left_jacobian(const Vector3d& w) {
    double theta = w.norm();
    Matrix3d K = skew(w);
    if (theta < 1e-8) {
        return Matrix3d::Identity() + 0.5 * K + (1.0 / 6.0) * (K * K);
    }
    double a = (1.0 - std::cos(theta)) / (theta * theta);
    double b = (theta - std::sin(theta)) / (theta * theta * theta);
    return Matrix3d::Identity() + a * K + b * (K * K);
}

// Analytic 3x3 Jacobian of R(w) @ v w.r.t. w. -[R v]_x J_l.
Matrix3d rot_jac(const Vector3d& w, const Vector3d& v) {
    Vector3d Rv = rodrigues_impl(w) * v;
    Matrix3d Jl = left_jacobian(w);
    return -skew(Rv) * Jl;
}

Vector3d mat_to_rotvec_impl(const Matrix3d& R) {
    double cos_t = (R.trace() - 1.0) * 0.5;
    cos_t = std::max(-1.0, std::min(1.0, cos_t));
    double theta = std::acos(cos_t);
    if (theta < 1e-8) {
        return Vector3d(R(2, 1) - R(1, 2), R(0, 2) - R(2, 0), R(1, 0) - R(0, 1)) * 0.5;
    }
    if (std::abs(kPi - theta) < 1e-6) {
        Matrix3d A = (R + Matrix3d::Identity()) * 0.5;
        int k = 0;
        double best = A(0, 0);
        for (int i = 1; i < 3; ++i) {
            if (A(i, i) > best) { best = A(i, i); k = i; }
        }
        Vector3d axis = A.col(k) / std::sqrt(std::max(A(k, k), 1e-12));
        axis = unit(axis);
        return axis * theta;
    }
    double s = std::sin(theta);
    Vector3d w(R(2, 1) - R(1, 2), R(0, 2) - R(2, 0), R(1, 0) - R(0, 1));
    return (w / (2.0 * s)) * theta;
}

// --------------------------------------------------------------------------- vector primitives

void perp_basis(const Vector3d& axis, Vector3d& e1, Vector3d& e2) {
    Vector3d a = unit(axis);
    Vector3d helper = (std::abs(a[0]) < 0.9) ? Vector3d(1.0, 0.0, 0.0)
                                             : Vector3d(0.0, 1.0, 0.0);
    e1 = unit(a.cross(helper));
    e2 = unit(a.cross(e1));
}

// Two scalars measuring the component of vec perpendicular to axis.
VectorXd perp_residual2(const Vector3d& axis, const Vector3d& vec) {
    Vector3d e1, e2;
    perp_basis(axis, e1, e2);
    VectorXd r(2);
    r << vec.dot(e1), vec.dot(e2);
    return r;
}

// Jacobian of perp_residual2 (basis locally constant). Jvec is (3, k).
MatrixXd perp_residual2_jac(const Vector3d& axis, const MatrixXd& /*Jaxis*/,
                            const Vector3d& /*vec*/, const MatrixXd& Jvec) {
    Vector3d e1, e2;
    perp_basis(axis, e1, e2);
    MatrixXd out(2, Jvec.cols());
    out.row(0) = e1.transpose() * Jvec;
    out.row(1) = e2.transpose() * Jvec;
    return out;
}

// Two independent scalars that vanish iff da || db. Projects da x db onto perp(da).
VectorXd two_indep_cross(const Vector3d& da, const Vector3d& db) {
    Vector3d c = da.cross(db);
    Vector3d e1, e2;
    perp_basis(da, e1, e2);
    VectorXd r(2);
    r << c.dot(e1), c.dot(e2);
    return r;
}

MatrixXd two_indep_cross_jac(const Vector3d& da, const Vector3d& /*db*/,
                             const MatrixXd& cross_jac) {
    Vector3d e1, e2;
    perp_basis(da, e1, e2);
    MatrixXd out(2, cross_jac.cols());
    out.row(0) = e1.transpose() * cross_jac;
    out.row(1) = e2.transpose() * cross_jac;
    return out;
}

double axis_point_distance(const Vector3d& p_axis, const Vector3d& d_axis,
                           const Vector3d& q) {
    Vector3d d = unit(d_axis);
    Vector3d v = q - p_axis;
    Vector3d perp = v - v.dot(d) * d;
    return perp.norm();
}

// (1, k) jacobian. Jp, Jd, Jq are (3, k).
MatrixXd axis_point_distance_jac(const Vector3d& p_axis, const MatrixXd& Jp,
                                 const Vector3d& d_axis, const MatrixXd& /*Jd*/,
                                 const Vector3d& q, const MatrixXd& Jq) {
    Vector3d d = unit(d_axis);
    Vector3d v = q - p_axis;
    Vector3d perp = v - v.dot(d) * d;
    double nrm = perp.norm();
    if (nrm < 1e-9) return MatrixXd::Zero(1, Jp.cols());
    Vector3d u = perp / nrm;
    MatrixXd Jv = Jq - Jp;                          // (3, k)
    // Jperp = Jv - d (d^T Jv)
    MatrixXd Jperp = Jv - d * (d.transpose() * Jv);  // (3, k)
    return (u.transpose() * Jperp);                  // (1, k)
}

// --------------------------------------------------------------------------- component frame

struct CompFrame {
    std::string id;
    Vector3d t0 = Vector3d::Zero();
    Vector3d w0 = Vector3d::Zero();
    bool fixed = false;
    std::vector<int> t_idx;   // 3 var indices (translation), empty if fixed
    std::vector<int> w_idx;   // 3 var indices (rotation vector), empty if fixed

    void alloc(System& sys) {
        if (fixed) return;
        t_idx = sys.add_vars(3, {t0[0], t0[1], t0[2]});
        w_idx = sys.add_vars(3, {w0[0], w0[1], w0[2]});
    }

    // Concatenated [tx,ty,tz, wx,wy,wz] global indices. Empty for fixed.
    std::vector<int> var_idx() const {
        if (fixed) return {};
        std::vector<int> r = t_idx;
        r.insert(r.end(), w_idx.begin(), w_idx.end());
        return r;
    }
    int var_size() const { return fixed ? 0 : 6; }
};

// --------------------------------------------------------------------------- residual builder

class MateBuilder {
public:
    MateBuilder(CompFrame* fa, MateGeom ga, CompFrame* fb, MateGeom gb,
                std::string mtype, const std::string& align, double value,
                std::vector<MateGeom> extra, json params,
                std::vector<std::string> extra_owners)
        : fa_(fa), ga_(std::move(ga)), fb_(fb), gb_(std::move(gb)),
          mtype_(std::move(mtype)), sign_(align == "anti" ? -1.0 : 1.0),
          value_(value), extra_(std::move(extra)), params_(std::move(params)),
          extra_owners_(std::move(extra_owners)) {
        idx_ = fa_->var_idx();
        auto bidx = fb_->var_idx();
        idx_.insert(idx_.end(), bidx.begin(), bidx.end());
        na_ = fa_->var_size();
        xa_const_ << fa_->t0, fa_->w0;
        xb_const_ << fb_->t0, fb_->w0;
        if (extra_owners_.size() < extra_.size())
            extra_owners_.resize(extra_.size(), "b");
    }

    const std::vector<int>& idx() const { return idx_; }
    int idx_size() const { return static_cast<int>(idx_.size()); }

    // Returns (rfunc, jfunc, m). Throws on an unsupported / unbuildable mate.
    std::tuple<solver::ResidualFunc, solver::JacobianFunc, int> make() {
        const std::string& t = mtype_;
        if (t == "coincident") return coincident();
        if (t == "parallel") return parallel();
        if (t == "perpendicular") return perpendicular();
        if (t == "concentric") return concentric();
        if (t == "distance") return distance();
        if (t == "angle") return angle();
        if (t == "tangent") return tangent();
        if (t == "lock") return lock();
        if (t == "width") return width();
        if (t == "symmetric") return symmetric();
        if (t == "distance_limit") return distance_limit();
        if (t == "angle_limit") return angle_limit();
        if (t == "profile_center") return profile_center();
        if (t == "gear") return gear();
        if (t == "rack_pinion") return rack_pinion();
        if (t == "screw") return screw();
        if (t == "hinge") return hinge();
        if (t == "slot") return slot();
        if (t == "cam_follower") return cam_follower();
        throw std::runtime_error("mate_solver: unsupported mate type '" + t + "'");
    }

private:
    CompFrame* fa_;
    MateGeom ga_;
    CompFrame* fb_;
    MateGeom gb_;
    std::string mtype_;
    double sign_;
    double value_;
    std::vector<MateGeom> extra_;
    json params_;
    std::vector<std::string> extra_owners_;
    std::vector<int> idx_;
    int na_ = 0;
    Eigen::Matrix<double, 6, 1> xa_const_;
    Eigen::Matrix<double, 6, 1> xb_const_;

    // -- sub-vector helpers ------------------------------------------------
    VectorXd xa(const VectorXd& xs) const {
        if (na_) return xs.head(na_);
        return xa_const_;
    }
    VectorXd xb(const VectorXd& xs) const {
        int nb = fb_->var_size();
        if (nb) return xs.segment(na_, nb);
        return xb_const_;
    }

    int icols() const { return static_cast<int>(idx_.size()); }
    MatrixXd empty_jac(int m) const { return MatrixXd::Zero(m, icols()); }

    // world point + 3x6 jac wrt that component's vars
    void wp(const std::string& which, const VectorXd& xs, const Vector3d& p_local,
            Vector3d& wp_out, MatrixXd& J) const {
        VectorXd x6 = (which == "a") ? xa(xs) : xb(xs);
        Vector3d w = x6.segment(3, 3);
        wp_out = rodrigues_impl(w) * p_local + x6.head(3);
        J = MatrixXd::Zero(3, 6);
        J.block(0, 0, 3, 3) = Matrix3d::Identity();
        J.block(0, 3, 3, 3) = rot_jac(w, p_local);
    }

    void wd(const std::string& which, const VectorXd& xs, const Vector3d& d_local,
            Vector3d& wd_out, MatrixXd& J) const {
        VectorXd x6 = (which == "a") ? xa(xs) : xb(xs);
        Vector3d w = x6.segment(3, 3);
        wd_out = rodrigues_impl(w) * d_local;
        J = MatrixXd::Zero(3, 6);
        J.block(0, 3, 3, 3) = rot_jac(w, d_local);
    }

    // Embed a (rows x 6) per-component jac into a (rows x icols) block.
    MatrixXd place(const std::string& which, const MatrixXd& J6) const {
        MatrixXd out = MatrixXd::Zero(J6.rows(), icols());
        if (which == "a") {
            if (na_) out.block(0, 0, J6.rows(), 6) = J6;
        } else {
            if (fb_->var_size()) out.block(0, na_, J6.rows(), 6) = J6;
        }
        return out;
    }

    // Row helpers to mirror Python's (vec @ J)[None,:] -> (1,6) placed block.
    MatrixXd place_row(const std::string& which, const Eigen::RowVectorXd& r6) const {
        MatrixXd m(1, 6);
        m.row(0) = r6;
        return place(which, m);
    }

    // -- coupling-param helpers --------------------------------------------
    double param(const std::string& key, double dflt) const {
        if (!params_.is_object() || !params_.contains(key)) return dflt;
        const json& v = params_[key];
        if (v.is_number()) return v.get<double>();
        return dflt;
    }

    Vector3d axis_world(const std::string& which) const {
        const MateGeom& g = (which == "a") ? ga_ : gb_;
        const Vector3d& w0 = (which == "a") ? fa_->w0 : fb_->w0;
        return unit(rodrigues_impl(w0) * g.dir_vector());
    }

    using R = solver::ResidualFunc;
    using J = solver::JacobianFunc;
    using Built = std::tuple<R, J, int>;

    // ----- coincident -----------------------------------------------------
    Built coincident() {
        if (ga_.kind == "plane" && gb_.kind == "plane") return coincident_plane();
        return coincident_axis();
    }

    Built coincident_plane() {
        Vector3d na_l = ga_.normal, nb_l = gb_.normal;
        Vector3d pa_l = ga_.point, pb_l = gb_.point;
        double sign = sign_;
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d na, nb, pa, pb; MatrixXd Jd;
            self->wd("a", xs, na_l, na, Jd);
            self->wd("b", xs, nb_l, nb, Jd);
            self->wp("a", xs, pa_l, pa, Jd);
            self->wp("b", xs, pb_l, pb, Jd);
            VectorXd r(4);
            r.head(3) = na + sign * nb;
            r(3) = na.dot(pb - pa);
            return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d na, nb, pa, pb;
            MatrixXd Jna, Jnb, Jpa, Jpb;
            self->wd("a", xs, na_l, na, Jna);
            self->wd("b", xs, nb_l, nb, Jnb);
            self->wp("a", xs, pa_l, pa, Jpa);
            self->wp("b", xs, pb_l, pb, Jpb);
            MatrixXd Jm = self->empty_jac(4);
            Jm.block(0, 0, 3, self->icols()) =
                self->place("a", Jna) + self->place("b", (sign * Jnb).eval());
            Vector3d diff = pb - pa;
            MatrixXd row = self->place("a", (diff.transpose() * Jna).eval())
                + self->place("a", (-(na.transpose() * Jpa)).eval())
                + self->place("b", (na.transpose() * Jpb).eval());
            Jm.block(3, 0, 1, self->icols()) = row;
            return Jm;
        };
        return {rfunc, jfunc, 4};
    }

    Built coincident_axis() {
        Vector3d da_l = ga_.dir_vector();
        Vector3d db_l = gb_.dir_vector();
        Vector3d pa_l = ga_.point, pb_l = gb_.point;
        double sign = sign_;
        bool has_dir = (ga_.kind == "axis") && (gb_.kind == "axis");
        auto self = this;

        if (!has_dir) {
            R rfunc = [=](const VectorXd& xs) {
                Vector3d pa, pb; MatrixXd Jd;
                self->wp("a", xs, pa_l, pa, Jd);
                self->wp("b", xs, pb_l, pb, Jd);
                return VectorXd(pa - pb);
            };
            J jfunc = [=](const VectorXd& xs) {
                Vector3d pa, pb; MatrixXd Jpa, Jpb;
                self->wp("a", xs, pa_l, pa, Jpa);
                self->wp("b", xs, pb_l, pb, Jpb);
                return MatrixXd(self->place("a", Jpa) - self->place("b", Jpb));
            };
            return {rfunc, jfunc, 3};
        }

        R rfunc = [=](const VectorXd& xs) {
            Vector3d da, db, pa, pb; MatrixXd Jd;
            self->wd("a", xs, da_l, da, Jd);
            self->wd("b", xs, db_l, db, Jd);
            self->wp("a", xs, pa_l, pa, Jd);
            self->wp("b", xs, pb_l, pb, Jd);
            VectorXd r(5);
            r.head(3) = da.cross(sign * db);
            r.segment(3, 2) = perp_residual2(da, pb - pa);
            return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d da, db, pa, pb;
            MatrixXd Jda, Jdb, Jpa, Jpb;
            self->wd("a", xs, da_l, da, Jda);
            self->wd("b", xs, db_l, db, Jdb);
            self->wp("a", xs, pa_l, pa, Jpa);
            self->wp("b", xs, pb_l, pb, Jpb);
            MatrixXd Jm = self->empty_jac(5);
            Vector3d sdb = sign * db;
            Jm.block(0, 0, 3, self->icols()) =
                self->place("a", (-skew(sdb) * Jda).eval())
                + self->place("b", (skew(da) * (sign * Jdb)).eval());
            Vector3d diff = pb - pa;
            MatrixXd Jdiff = self->place("b", Jpb) - self->place("a", Jpa);
            Jm.block(3, 0, 2, self->icols()) =
                perp_residual2_jac(da, self->place("a", Jda), diff, Jdiff);
            return Jm;
        };
        return {rfunc, jfunc, 5};
    }

    // ----- parallel / perpendicular / angle -------------------------------
    Built parallel() {
        Vector3d da_l = ga_.dir_vector();
        Vector3d db_l = gb_.dir_vector();
        double sign = sign_;
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d da, db; MatrixXd Jd;
            self->wd("a", xs, da_l, da, Jd);
            self->wd("b", xs, db_l, db, Jd);
            return VectorXd(da - sign * db);
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d da, db; MatrixXd Jda, Jdb;
            self->wd("a", xs, da_l, da, Jda);
            self->wd("b", xs, db_l, db, Jdb);
            return MatrixXd(self->place("a", Jda) - self->place("b", (sign * Jdb).eval()));
        };
        return {rfunc, jfunc, 3};
    }

    Built perpendicular() {
        Vector3d da_l = ga_.dir_vector();
        Vector3d db_l = gb_.dir_vector();
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d da, db; MatrixXd Jd;
            self->wd("a", xs, da_l, da, Jd);
            self->wd("b", xs, db_l, db, Jd);
            VectorXd r(1); r(0) = da.dot(db); return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d da, db; MatrixXd Jda, Jdb;
            self->wd("a", xs, da_l, da, Jda);
            self->wd("b", xs, db_l, db, Jdb);
            return MatrixXd(self->place("a", (db.transpose() * Jda).eval())
                            + self->place("b", (da.transpose() * Jdb).eval()));
        };
        return {rfunc, jfunc, 1};
    }

    Built angle() {
        Vector3d da_l = ga_.dir_vector();
        Vector3d db_l = gb_.dir_vector();
        double target = std::cos(deg2rad(value_));
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d da, db; MatrixXd Jd;
            self->wd("a", xs, da_l, da, Jd);
            self->wd("b", xs, db_l, db, Jd);
            VectorXd r(1); r(0) = da.dot(db) - target; return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d da, db; MatrixXd Jda, Jdb;
            self->wd("a", xs, da_l, da, Jda);
            self->wd("b", xs, db_l, db, Jdb);
            return MatrixXd(self->place("a", (db.transpose() * Jda).eval())
                            + self->place("b", (da.transpose() * Jdb).eval()));
        };
        return {rfunc, jfunc, 1};
    }

    // ----- concentric -----------------------------------------------------
    Built concentric() {
        Vector3d da_l = ga_.dir_vector();
        Vector3d db_l = gb_.dir_vector();
        Vector3d pa_l = ga_.point, pb_l = gb_.point;
        double sign = sign_;
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d da, db, pa, pb; MatrixXd Jd;
            self->wd("a", xs, da_l, da, Jd);
            self->wd("b", xs, db_l, db, Jd);
            self->wp("a", xs, pa_l, pa, Jd);
            self->wp("b", xs, pb_l, pb, Jd);
            VectorXd r(4);
            r.head(2) = two_indep_cross(da, sign * db);
            r.segment(2, 2) = perp_residual2(da, pb - pa);
            return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d da, db, pa, pb;
            MatrixXd Jda, Jdb, Jpa, Jpb;
            self->wd("a", xs, da_l, da, Jda);
            self->wd("b", xs, db_l, db, Jdb);
            self->wp("a", xs, pa_l, pa, Jpa);
            self->wp("b", xs, pb_l, pb, Jpb);
            MatrixXd Jm = self->empty_jac(4);
            Vector3d sdb = sign * db;
            MatrixXd cross_jac = self->place("a", (-skew(sdb) * Jda).eval())
                + self->place("b", (skew(da) * (sign * Jdb)).eval());
            Jm.block(0, 0, 2, self->icols()) = two_indep_cross_jac(da, sdb, cross_jac);
            Vector3d diff = pb - pa;
            MatrixXd Jdiff = self->place("b", Jpb) - self->place("a", Jpa);
            Jm.block(2, 0, 2, self->icols()) =
                perp_residual2_jac(da, self->place("a", Jda), diff, Jdiff);
            return Jm;
        };
        return {rfunc, jfunc, 4};
    }

    // ----- distance -------------------------------------------------------
    Built distance() {
        bool use_plane = (ga_.kind == "plane") || (gb_.kind == "plane");
        Vector3d n_l = (ga_.kind == "plane") ? ga_.normal : gb_.normal;
        bool n_on_a = (ga_.kind == "plane");
        Vector3d pa_l = ga_.point, pb_l = gb_.point;
        double sign = sign_;
        double value = value_;
        auto self = this;

        if (use_plane) {
            R rfunc = [=](const VectorXd& xs) {
                Vector3d n, pa, pb; MatrixXd Jd;
                if (n_on_a) self->wd("a", xs, n_l, n, Jd);
                else self->wd("b", xs, n_l, n, Jd);
                self->wp("a", xs, pa_l, pa, Jd);
                self->wp("b", xs, pb_l, pb, Jd);
                double gap = n.dot(pb - pa);
                VectorXd r(1); r(0) = gap - sign * value; return r;
            };
            J jfunc = [=](const VectorXd& xs) {
                Vector3d n, pa, pb; MatrixXd Jn, Jpa, Jpb;
                if (n_on_a) self->wd("a", xs, n_l, n, Jn);
                else self->wd("b", xs, n_l, n, Jn);
                self->wp("a", xs, pa_l, pa, Jpa);
                self->wp("b", xs, pb_l, pb, Jpb);
                Vector3d diff = pb - pa;
                MatrixXd place_n = n_on_a
                    ? self->place("a", (diff.transpose() * Jn).eval())
                    : self->place("b", (diff.transpose() * Jn).eval());
                MatrixXd row = place_n
                    + self->place("b", (n.transpose() * Jpb).eval())
                    - self->place("a", (n.transpose() * Jpa).eval());
                return row;
            };
            return {rfunc, jfunc, 1};
        }

        R rfunc = [=](const VectorXd& xs) {
            Vector3d pa, pb; MatrixXd Jd;
            self->wp("a", xs, pa_l, pa, Jd);
            self->wp("b", xs, pb_l, pb, Jd);
            Vector3d d = pb - pa;
            VectorXd r(1); r(0) = d.norm() - value; return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d pa, pb; MatrixXd Jpa, Jpb;
            self->wp("a", xs, pa_l, pa, Jpa);
            self->wp("b", xs, pb_l, pb, Jpb);
            Vector3d d = pb - pa;
            double nrm = d.norm();
            Vector3d u = (nrm > 1e-9) ? Vector3d(d / nrm) : Vector3d::Zero();
            return MatrixXd(self->place("b", (u.transpose() * Jpb).eval())
                            - self->place("a", (u.transpose() * Jpa).eval()));
        };
        return {rfunc, jfunc, 1};
    }

    // ----- tangent --------------------------------------------------------
    Built tangent() {
        bool a_cyl = (ga_.kind == "axis");
        bool b_cyl = (gb_.kind == "axis");
        double sign = sign_;
        if (a_cyl && gb_.kind == "plane") return tangent_cyl_plane("a", "b");
        if (b_cyl && ga_.kind == "plane") return tangent_cyl_plane("b", "a");
        double ra = ga_.radius, rb = gb_.radius;
        Vector3d pa_l = ga_.point, da_l = ga_.direction;
        Vector3d pb_l = gb_.point;
        double target = (sign > 0) ? (ra + rb) : std::abs(ra - rb);
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d pa, pb, da; MatrixXd Jd;
            self->wp("a", xs, pa_l, pa, Jd);
            self->wp("b", xs, pb_l, pb, Jd);
            self->wd("a", xs, da_l, da, Jd);
            double dist = axis_point_distance(pa, da, pb);
            VectorXd r(1); r(0) = dist - target; return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d pa, pb, da; MatrixXd Jpa, Jpb, Jda;
            self->wp("a", xs, pa_l, pa, Jpa);
            self->wp("b", xs, pb_l, pb, Jpb);
            self->wd("a", xs, da_l, da, Jda);
            return axis_point_distance_jac(
                pa, self->place("a", Jpa), da, self->place("a", Jda),
                pb, self->place("b", Jpb));
        };
        return {rfunc, jfunc, 1};
    }

    Built tangent_cyl_plane(const std::string& cyl, const std::string& pln) {
        const MateGeom& cyl_geom = (cyl == "a") ? ga_ : gb_;
        const MateGeom& pln_geom = (pln == "a") ? ga_ : gb_;
        double r = cyl_geom.radius;
        Vector3d pc_l = cyl_geom.point;
        Vector3d pp_l = pln_geom.point, np_l = pln_geom.normal;
        double sign = sign_;
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d pc, pp, nn; MatrixXd Jd;
            self->wp(cyl, xs, pc_l, pc, Jd);
            self->wp(pln, xs, pp_l, pp, Jd);
            self->wd(pln, xs, np_l, nn, Jd);
            double gap = nn.dot(pc - pp);
            VectorXd rr(1); rr(0) = gap - sign * r; return rr;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d pc, pp, nn; MatrixXd Jpc, Jpp, Jnn;
            self->wp(cyl, xs, pc_l, pc, Jpc);
            self->wp(pln, xs, pp_l, pp, Jpp);
            self->wd(pln, xs, np_l, nn, Jnn);
            Vector3d diff = pc - pp;
            MatrixXd row = self->place(pln, (diff.transpose() * Jnn).eval())
                + self->place(cyl, (nn.transpose() * Jpc).eval())
                - self->place(pln, (nn.transpose() * Jpp).eval());
            return row;
        };
        return {rfunc, jfunc, 1};
    }

    // ----- lock -----------------------------------------------------------
    Built lock() {
        Matrix3d Ra0 = rodrigues_impl(fa_->w0);
        Vector3d ta0 = fa_->t0;
        Matrix3d Rb0 = rodrigues_impl(fb_->w0);
        Vector3d tb0 = fb_->t0;
        Matrix3d relR0 = Ra0.transpose() * Rb0;
        Vector3d relt0 = Ra0.transpose() * (tb0 - ta0);
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            VectorXd xav = self->xa(xs), xbv = self->xb(xs);
            Matrix3d Ra = rodrigues_impl(xav.segment(3, 3));
            Vector3d ta = xav.head(3);
            Matrix3d Rb = rodrigues_impl(xbv.segment(3, 3));
            Vector3d tb = xbv.head(3);
            Matrix3d relR = Ra.transpose() * Rb;
            Vector3d relt = Ra.transpose() * (tb - ta);
            Matrix3d dR = relR0.transpose() * relR;
            Vector3d w_err = mat_to_rotvec_impl(dR);
            Vector3d t_err = relt - relt0;
            VectorXd r(6);
            r.head(3) = t_err;
            r.tail(3) = w_err;
            return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            VectorXd xav = self->xa(xs);
            MatrixXd Jm = self->empty_jac(6);
            Matrix3d Ra = rodrigues_impl(xav.segment(3, 3));
            if (self->fb_->var_size()) {
                Jm.block(0, self->na_, 3, 3) = Ra.transpose();
                Jm.block(3, self->na_ + 3, 3, 3) = Matrix3d::Identity();
            }
            if (self->na_) {
                Jm.block(0, 0, 3, 3) = -Ra.transpose();
                Jm.block(3, 3, 3, 3) = -Matrix3d::Identity();
            }
            return Jm;
        };
        return {rfunc, jfunc, 6};
    }

    // ===================================================================== ADVANCED MATES

    Built width() {
        Vector3d tab_l = ga_.point;
        Vector3d jaw1_p_l = gb_.point, n_l = gb_.normal;
        if (extra_.empty())
            throw std::runtime_error("width mate needs a second jaw plane (extra geom)");
        Vector3d jaw2_p_l = extra_[0].point;
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d tp, j1p, j2p, n; MatrixXd Jd;
            self->wp("a", xs, tab_l, tp, Jd);
            self->wp("b", xs, jaw1_p_l, j1p, Jd);
            self->wp("b", xs, jaw2_p_l, j2p, Jd);
            self->wd("b", xs, n_l, n, Jd);
            double d1 = n.dot(tp - j1p);
            double d2 = n.dot(tp - j2p);
            VectorXd r(1); r(0) = d1 + d2; return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d tp, j1p, j2p, n; MatrixXd Jtp, Jj1p, Jj2p, Jn;
            self->wp("a", xs, tab_l, tp, Jtp);
            self->wp("b", xs, jaw1_p_l, j1p, Jj1p);
            self->wp("b", xs, jaw2_p_l, j2p, Jj2p);
            self->wd("b", xs, n_l, n, Jn);
            Vector3d diff = (tp - j1p) + (tp - j2p);
            MatrixXd row = self->place("a", (2.0 * (n.transpose() * Jtp)).eval())
                - self->place("b", (n.transpose() * Jj1p).eval())
                - self->place("b", (n.transpose() * Jj2p).eval())
                + self->place("b", (diff.transpose() * Jn).eval());
            return row;
        };
        return {rfunc, jfunc, 1};
    }

    Built symmetric() {
        Vector3d pa_l = ga_.point;
        Vector3d pb_l = gb_.point;
        if (extra_.empty())
            throw std::runtime_error("symmetric mate needs a symmetry plane (extra geom)");
        Vector3d sp_p_l = extra_[0].point, sp_n_l = extra_[0].normal;
        std::string sym_on = extra_owners_.empty() ? "b" : extra_owners_[0];
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d pa, pb, spp, spn; MatrixXd Jd;
            self->wp("a", xs, pa_l, pa, Jd);
            self->wp("b", xs, pb_l, pb, Jd);
            self->wp(sym_on, xs, sp_p_l, spp, Jd);
            self->wd(sym_on, xs, sp_n_l, spn, Jd);
            double da = spn.dot(pa - spp);
            double db = spn.dot(pb - spp);
            VectorXd r(1); r(0) = da + db; return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d pa, pb, spp, spn; MatrixXd Jpa, Jpb, Jspp, Jspn;
            self->wp("a", xs, pa_l, pa, Jpa);
            self->wp("b", xs, pb_l, pb, Jpb);
            self->wp(sym_on, xs, sp_p_l, spp, Jspp);
            self->wd(sym_on, xs, sp_n_l, spn, Jspn);
            Vector3d diff_a = pa - spp, diff_b = pb - spp;
            MatrixXd rowa = self->place("a", (spn.transpose() * Jpa).eval())
                + self->place(sym_on, (diff_a.transpose() * Jspn).eval())
                - self->place(sym_on, (spn.transpose() * Jspp).eval());
            MatrixXd rowb = self->place("b", (spn.transpose() * Jpb).eval())
                + self->place(sym_on, (diff_b.transpose() * Jspn).eval())
                - self->place(sym_on, (spn.transpose() * Jspp).eval());
            return MatrixXd(rowa + rowb);
        };
        return {rfunc, jfunc, 1};
    }

    Built distance_limit() {
        bool use_plane = (ga_.kind == "plane") || (gb_.kind == "plane");
        Vector3d n_l = (ga_.kind == "plane") ? ga_.normal : gb_.normal;
        bool n_on_a = (ga_.kind == "plane");
        Vector3d pa_l = ga_.point, pb_l = gb_.point;
        double lo = param("min", 0.0);
        double hi = param("max", 0.0);
        auto self = this;

        auto gap_and_row = [=](const VectorXd& xs, double& gap, MatrixXd& row) {
            if (use_plane) {
                Vector3d n, pa, pb; MatrixXd Jn, Jpa, Jpb;
                if (n_on_a) self->wd("a", xs, n_l, n, Jn);
                else self->wd("b", xs, n_l, n, Jn);
                self->wp("a", xs, pa_l, pa, Jpa);
                self->wp("b", xs, pb_l, pb, Jpb);
                Vector3d diff = pb - pa;
                gap = n.dot(diff);
                MatrixXd place_n = n_on_a
                    ? self->place("a", (diff.transpose() * Jn).eval())
                    : self->place("b", (diff.transpose() * Jn).eval());
                row = place_n + self->place("b", (n.transpose() * Jpb).eval())
                    - self->place("a", (n.transpose() * Jpa).eval());
                return;
            }
            Vector3d pa, pb; MatrixXd Jpa, Jpb;
            self->wp("a", xs, pa_l, pa, Jpa);
            self->wp("b", xs, pb_l, pb, Jpb);
            Vector3d d = pb - pa;
            double nrm = d.norm();
            Vector3d u = (nrm > 1e-9) ? Vector3d(d / nrm) : Vector3d::Zero();
            gap = nrm;
            row = self->place("b", (u.transpose() * Jpb).eval())
                - self->place("a", (u.transpose() * Jpa).eval());
        };

        R rfunc = [=](const VectorXd& xs) {
            double gap; MatrixXd row; gap_and_row(xs, gap, row);
            double v = 0.0;
            if (gap < lo) v = gap - lo;
            else if (gap > hi) v = gap - hi;
            VectorXd r(1); r(0) = v; return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            double gap; MatrixXd row; gap_and_row(xs, gap, row);
            if (lo <= gap && gap <= hi) return MatrixXd(MatrixXd::Zero(row.rows(), row.cols()));
            return row;
        };
        return {rfunc, jfunc, 1};
    }

    Built angle_limit() {
        Vector3d da_l = ga_.dir_vector();
        Vector3d db_l = gb_.dir_vector();
        double lo = deg2rad(param("min", 0.0));
        double hi = deg2rad(param("max", 0.0));
        auto self = this;
        auto angle_and_row = [=](const VectorXd& xs, double& ang, MatrixXd& row) {
            Vector3d da, db; MatrixXd Jda, Jdb;
            self->wd("a", xs, da_l, da, Jda);
            self->wd("b", xs, db_l, db, Jdb);
            double c = std::max(-1.0, std::min(1.0, da.dot(db)));
            ang = std::acos(c);
            double s = std::sqrt(std::max(1.0 - c * c, 1e-12));
            MatrixXd ddot = self->place("a", (db.transpose() * Jda).eval())
                + self->place("b", (da.transpose() * Jdb).eval());
            row = (-1.0 / s) * ddot;
        };
        R rfunc = [=](const VectorXd& xs) {
            double ang; MatrixXd row; angle_and_row(xs, ang, row);
            double v = 0.0;
            if (ang < lo) v = ang - lo;
            else if (ang > hi) v = ang - hi;
            VectorXd r(1); r(0) = v; return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            double ang; MatrixXd row; angle_and_row(xs, ang, row);
            if (lo <= ang && ang <= hi) return MatrixXd(MatrixXd::Zero(row.rows(), row.cols()));
            return row;
        };
        return {rfunc, jfunc, 1};
    }

    Built profile_center() {
        Vector3d pa_l = ga_.point, pb_l = gb_.point;
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d pa, pb; MatrixXd Jd;
            self->wp("a", xs, pa_l, pa, Jd);
            self->wp("b", xs, pb_l, pb, Jd);
            return VectorXd(pa - pb);
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d pa, pb; MatrixXd Jpa, Jpb;
            self->wp("a", xs, pa_l, pa, Jpa);
            self->wp("b", xs, pb_l, pb, Jpb);
            return MatrixXd(self->place("a", Jpa) - self->place("b", Jpb));
        };
        return {rfunc, jfunc, 3};
    }

    // ===================================================================== MECHANICAL MATES

    // theta = (w - w0)·â (w0 == 0). Returns scalar fn + row fn.
    void theta_row(const std::string& which,
                   std::function<double(const VectorXd&)>& theta_fn,
                   std::function<MatrixXd(const VectorXd&)>& row_fn) const {
        Vector3d ax = axis_world(which);
        auto self = this;
        theta_fn = [=](const VectorXd& xs) {
            VectorXd x6 = (which == "a") ? self->xa(xs) : self->xb(xs);
            return double(x6.segment(3, 3).dot(ax));
        };
        row_fn = [=](const VectorXd& xs) {
            MatrixXd r6 = MatrixXd::Zero(1, 6);
            r6.block(0, 3, 1, 3) = ax.transpose();
            return self->place(which, r6);
        };
    }

    Built gear() {
        double ratio = param("ratio", 1.0);
        std::function<double(const VectorXd&)> theta_a, theta_b;
        std::function<MatrixXd(const VectorXd&)> row_a, row_b;
        theta_row("a", theta_a, row_a);
        theta_row("b", theta_b, row_b);
        double coup = -sign_;
        R rfunc = [=](const VectorXd& xs) {
            VectorXd r(1); r(0) = theta_b(xs) - coup * ratio * theta_a(xs); return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            return MatrixXd(row_b(xs) - coup * ratio * row_a(xs));
        };
        return {rfunc, jfunc, 1};
    }

    Built rack_pinion() {
        double pr = param("pitch_radius", ga_.radius != 0.0 ? ga_.radius : 1.0);
        std::function<double(const VectorXd&)> theta_a;
        std::function<MatrixXd(const VectorXd&)> row_a;
        theta_row("a", theta_a, row_a);
        Vector3d d_rack = axis_world("b");
        Vector3d tb0 = fb_->t0;
        double coup = sign_;
        auto self = this;
        auto s_b = [=](const VectorXd& xs) {
            VectorXd x6 = self->xb(xs);
            return double((x6.head(3) - tb0).dot(d_rack));
        };
        auto s_b_row = [=](const VectorXd& xs) {
            MatrixXd r6 = MatrixXd::Zero(1, 6);
            r6.block(0, 0, 1, 3) = d_rack.transpose();
            return self->place("b", r6);
        };
        R rfunc = [=](const VectorXd& xs) {
            VectorXd r(1); r(0) = s_b(xs) - coup * pr * theta_a(xs); return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            return MatrixXd(s_b_row(xs) - coup * pr * row_a(xs));
        };
        return {rfunc, jfunc, 1};
    }

    Built screw() {
        double pitch = param("pitch", 1.0);
        double k = pitch / (2.0 * kPi);
        std::function<double(const VectorXd&)> theta_a, theta_b;
        std::function<MatrixXd(const VectorXd&)> rowt_a, rowt_b;
        theta_row("a", theta_a, rowt_a);
        theta_row("b", theta_b, rowt_b);
        Vector3d ax_a = axis_world("a");
        Vector3d ax_b = axis_world("b");
        Vector3d ta0 = fa_->t0, tb0 = fb_->t0;
        double coup = sign_;
        auto self = this;
        auto s_a = [=](const VectorXd& xs) {
            return double((self->xa(xs).head(3) - ta0).dot(ax_a));
        };
        auto s_b = [=](const VectorXd& xs) {
            return double((self->xb(xs).head(3) - tb0).dot(ax_b));
        };
        auto s_a_row = [=](const VectorXd& xs) {
            MatrixXd r6 = MatrixXd::Zero(1, 6);
            r6.block(0, 0, 1, 3) = ax_a.transpose();
            return self->place("a", r6);
        };
        auto s_b_row = [=](const VectorXd& xs) {
            MatrixXd r6 = MatrixXd::Zero(1, 6);
            r6.block(0, 0, 1, 3) = ax_b.transpose();
            return self->place("b", r6);
        };
        R rfunc = [=](const VectorXd& xs) {
            double ds = s_b(xs) - s_a(xs);
            double dth = theta_b(xs) - theta_a(xs);
            VectorXd r(1); r(0) = ds - coup * k * dth; return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            MatrixXd ds_row = s_b_row(xs) - s_a_row(xs);
            MatrixXd dth_row = rowt_b(xs) - rowt_a(xs);
            return MatrixXd(ds_row - coup * k * dth_row);
        };
        return {rfunc, jfunc, 1};
    }

    Built hinge() {
        Vector3d da_l = ga_.dir_vector();
        Vector3d db_l = gb_.dir_vector();
        Vector3d pa_l = ga_.point, pb_l = gb_.point;
        double sign = sign_;
        Vector3d ma_l, mb_l;
        if (extra_.size() >= 2) {
            ma_l = extra_[0].dir_vector();
            mb_l = extra_[1].dir_vector();
        } else {
            Vector3d e1, e2;
            perp_basis(da_l, e1, e2); ma_l = e1;
            perp_basis(db_l, e1, e2); mb_l = e1;
        }
        double lo = deg2rad(param("min", 0.0));
        double hi = deg2rad(param("max", 360.0));
        auto self = this;
        auto swing = [=](const VectorXd& xs, double& ang, MatrixXd& row) {
            Vector3d ma, mb; MatrixXd Jma, Jmb;
            self->wd("a", xs, ma_l, ma, Jma);
            self->wd("b", xs, mb_l, mb, Jmb);
            double c = std::max(-1.0, std::min(1.0, ma.dot(mb)));
            ang = std::acos(c);
            double s = std::sqrt(std::max(1.0 - c * c, 1e-12));
            MatrixXd ddot = self->place("a", (mb.transpose() * Jma).eval())
                + self->place("b", (ma.transpose() * Jmb).eval());
            row = (-1.0 / s) * ddot;
        };
        R rfunc = [=](const VectorXd& xs) {
            Vector3d da, db, pa, pb; MatrixXd Jd;
            self->wd("a", xs, da_l, da, Jd);
            self->wd("b", xs, db_l, db, Jd);
            self->wp("a", xs, pa_l, pa, Jd);
            self->wp("b", xs, pb_l, pb, Jd);
            VectorXd r(5);
            r.head(2) = two_indep_cross(da, sign * db);
            r.segment(2, 2) = perp_residual2(da, pb - pa);
            double ang; MatrixXd row; swing(xs, ang, row);
            if (ang < lo) r(4) = ang - lo;
            else if (ang > hi) r(4) = ang - hi;
            else r(4) = 0.0;
            return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d da, db, pa, pb; MatrixXd Jda, Jdb, Jpa, Jpb;
            self->wd("a", xs, da_l, da, Jda);
            self->wd("b", xs, db_l, db, Jdb);
            self->wp("a", xs, pa_l, pa, Jpa);
            self->wp("b", xs, pb_l, pb, Jpb);
            MatrixXd Jm = self->empty_jac(5);
            Vector3d sdb = sign * db;
            MatrixXd cross_jac = self->place("a", (-skew(sdb) * Jda).eval())
                + self->place("b", (skew(da) * (sign * Jdb)).eval());
            Jm.block(0, 0, 2, self->icols()) = two_indep_cross_jac(da, sdb, cross_jac);
            Vector3d diff = pb - pa;
            MatrixXd Jdiff = self->place("b", Jpb) - self->place("a", Jpa);
            Jm.block(2, 0, 2, self->icols()) =
                perp_residual2_jac(da, self->place("a", Jda), diff, Jdiff);
            double ang; MatrixXd row; swing(xs, ang, row);
            if (ang < lo || ang > hi) Jm.block(4, 0, 1, self->icols()) = row;
            return Jm;
        };
        return {rfunc, jfunc, 5};
    }

    Built slot() {
        Vector3d pslot_l = gb_.point, dslot_l = gb_.direction;
        Vector3d ppin_l = ga_.point;
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d ps, ds, pp; MatrixXd Jd;
            self->wp("b", xs, pslot_l, ps, Jd);
            self->wd("b", xs, dslot_l, ds, Jd);
            self->wp("a", xs, ppin_l, pp, Jd);
            return VectorXd(perp_residual2(ds, pp - ps));
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d ps, ds, pp; MatrixXd Jps, Jds, Jpp;
            self->wp("b", xs, pslot_l, ps, Jps);
            self->wd("b", xs, dslot_l, ds, Jds);
            self->wp("a", xs, ppin_l, pp, Jpp);
            Vector3d diff = pp - ps;
            MatrixXd Jdiff = self->place("a", Jpp) - self->place("b", Jps);
            return perp_residual2_jac(ds, self->place("b", Jds), diff, Jdiff);
        };
        return {rfunc, jfunc, 2};
    }

    Built cam_follower() {
        Vector3d pf_l = ga_.point;
        double rf = ga_.radius;
        Vector3d pc_l = gb_.point, dc_l = gb_.direction;
        double Rr = gb_.radius;
        double sign = sign_;
        double target = (sign > 0) ? (Rr + rf) : std::abs(Rr - rf);
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            Vector3d pc, dc, pf; MatrixXd Jd;
            self->wp("b", xs, pc_l, pc, Jd);
            self->wd("b", xs, dc_l, dc, Jd);
            self->wp("a", xs, pf_l, pf, Jd);
            double dist = axis_point_distance(pc, dc, pf);
            VectorXd r(1); r(0) = dist - target; return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d pc, dc, pf; MatrixXd Jpc, Jdc, Jpf;
            self->wp("b", xs, pc_l, pc, Jpc);
            self->wd("b", xs, dc_l, dc, Jdc);
            self->wp("a", xs, pf_l, pf, Jpf);
            return axis_point_distance_jac(
                pc, self->place("b", Jpc), dc, self->place("b", Jdc),
                pf, self->place("a", Jpf));
        };
        return {rfunc, jfunc, 1};
    }
};

// --------------------------------------------------------------------------- transform <-> params

const std::vector<double> kIdentity16 = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};

void transform_to_params(const std::vector<double>& transform, Vector3d& t, Vector3d& w) {
    Eigen::Matrix4d M;
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c)
            M(r, c) = transform[r * 4 + c];
    Matrix3d Rm = M.block(0, 0, 3, 3);
    t = M.block(0, 3, 3, 1);
    // Orthonormalize defensively (SVD), matching numpy's U @ Vt with det fix.
    Eigen::JacobiSVD<Matrix3d> svd(Rm, Eigen::ComputeFullU | Eigen::ComputeFullV);
    Matrix3d U = svd.matrixU();
    Matrix3d V = svd.matrixV();
    Matrix3d Rn = U * V.transpose();
    if (Rn.determinant() < 0) {
        U.col(2) *= -1;
        Rn = U * V.transpose();
    }
    w = mat_to_rotvec_impl(Rn);
}

std::vector<double> params_to_transform(const Vector3d& t, const Vector3d& w) {
    Matrix3d Rm = rodrigues_impl(w);
    std::vector<double> out(16, 0.0);
    out[15] = 1.0;
    for (int r = 0; r < 3; ++r) {
        for (int c = 0; c < 3; ++c) out[r * 4 + c] = Rm(r, c);
        out[r * 4 + 3] = t[r];
    }
    return out;
}

double mate_value(const json& mate) {
    if (!mate.contains("value")) return 0.0;
    const json& v = mate["value"];
    if (v.is_object()) return v.value("value", 0.0);
    if (v.is_number()) return v.get<double>();
    return 0.0;
}

std::string dof_summary(int dof, bool over, int n_comp, int n_fixed) {
    if (over)
        return "over-defined (" + std::to_string(n_comp) + " components, "
               + std::to_string(n_fixed) + " fixed)";
    if (dof <= 0)
        return "fully constrained (" + std::to_string(n_comp) + " components, "
               + std::to_string(n_fixed) + " fixed)";
    return std::to_string(dof) + " DOF remaining (" + std::to_string(n_comp)
           + " components, " + std::to_string(n_fixed) + " fixed)";
}

// numpy.linalg.matrix_rank with an explicit absolute tol: count singular values > tol.
int matrix_rank_abs(const MatrixXd& M, double tol) {
    if (M.size() == 0) return 0;
    Eigen::JacobiSVD<MatrixXd> svd(M);
    const auto& sv = svd.singularValues();
    int r = 0;
    for (int i = 0; i < sv.size(); ++i)
        if (sv(i) > tol) ++r;
    return r;
}

// Per-mate redundant/conflicting classification (mate-granular). Direct port of
// mate_solver._classify_mates.
struct Builders {
    std::string mid;
    MateBuilder* builder;
    solver::ResidualFunc rfunc;
    int m;
};

void classify_mates(std::vector<Builders>& builders, const VectorXd& x, double tol,
                    std::vector<std::string>& redundant,
                    std::vector<std::string>& conflicting) {
    double rtol = std::max(1e-6, 1e3 * tol);
    bool have_accum = false;
    MatrixXd accum;
    int base_rank = 0;
    int n = static_cast<int>(x.size());
    for (auto& b : builders) {
        const std::vector<int>& idx = b.builder->idx();
        // rebuild jfunc (matches Python building a fresh jfunc via make()[1]).
        auto built = b.builder->make();
        solver::JacobianFunc jfunc = std::get<1>(built);
        // sub-vector x[idx]
        VectorXd xsub(idx.size());
        for (size_t i = 0; i < idx.size(); ++i) xsub(static_cast<int>(i)) = x(idx[i]);
        MatrixXd Jb = jfunc(xsub);
        VectorXd rb = b.rfunc(xsub);
        MatrixXd Wb = MatrixXd::Zero(Jb.rows(), n);
        for (size_t i = 0; i < idx.size(); ++i) Wb.col(idx[i]) = Jb.col(static_cast<int>(i));
        MatrixXd stacked;
        if (!have_accum) {
            stacked = Wb;
        } else {
            stacked.resize(accum.rows() + Wb.rows(), n);
            stacked.topRows(accum.rows()) = accum;
            stacked.bottomRows(Wb.rows()) = Wb;
        }
        int new_rank = matrix_rank_abs(stacked, std::max(1e-9, tol));
        int gained = new_rank - base_rank;
        if (gained <= 0) {
            if (rb.size() > 0 && rb.cwiseAbs().maxCoeff() > rtol)
                conflicting.push_back(b.mid);
            else
                redundant.push_back(b.mid);
        } else {
            accum = stacked;
            have_accum = true;
            base_rank = new_rank;
        }
    }
}

}  // namespace

// --------------------------------------------------------------------------- exposed helpers

Vector3d unit(const Vector3d& v) {
    double n = v.norm();
    return (n > 1e-12) ? Vector3d(v / n) : v;
}

Matrix3d rodrigues(const Vector3d& w) { return rodrigues_impl(w); }
Vector3d mat_to_rotvec(const Matrix3d& R) { return mat_to_rotvec_impl(R); }

// --------------------------------------------------------------------------- public solve

MateSolveResult solve_mates(const json& components, const json& mates,
                            const ResolvedGeometry& resolved_geometry,
                            const std::optional<WarmMap>& warm_opt,
                            const std::optional<Drag>& drag_opt,
                            double tol, int max_iter,
                            std::optional<double> time_budget_ms) {
    WarmMap warm = warm_opt.value_or(WarmMap{});
    System system;
    std::map<std::string, CompFrame> frames;
    std::vector<std::string> frame_order;  // insertion order (mirrors dict iteration)

    for (const auto& comp : components) {
        if (comp.value("suppressed", false)) continue;
        std::string cid = comp.at("id").get<std::string>();
        Vector3d t, w;
        auto wit = warm.find(cid);
        if (wit != warm.end()) {
            t = wit->second.first;
            w = wit->second.second;
        } else {
            std::vector<double> tf = comp.contains("transform") && !comp["transform"].is_null()
                ? comp["transform"].get<std::vector<double>>()
                : kIdentity16;
            if (tf.size() != 16) tf = kIdentity16;
            transform_to_params(tf, t, w);
        }
        CompFrame frame;
        frame.id = cid;
        frame.t0 = t;
        frame.w0 = w;
        frame.fixed = comp.value("fixed", false);
        frames[cid] = frame;
        frame_order.push_back(cid);
    }

    for (auto& cid : frame_order) frames[cid].alloc(system);

    std::vector<std::pair<std::string, std::string>> mate_records;  // (mid, forced-status or "")
    std::vector<std::string> active_tags;
    // Builders own MateBuilder via stable storage (deque-like vector of unique_ptr).
    std::vector<std::unique_ptr<MateBuilder>> builder_store;
    std::vector<Builders> builders;

    for (const auto& mate : mates) {
        std::string mid = mate.at("id").get<std::string>();
        if (mate.value("suppressed", false)) {
            mate_records.push_back({mid, "suppressed"});
            continue;
        }
        json refs = mate.value("refs", json::array());
        if (refs.size() < 2) {
            mate_records.push_back({mid, "unresolved"});
            continue;
        }
        auto ga_it = resolved_geometry.find({mid, 0});
        auto gb_it = resolved_geometry.find({mid, 1});
        std::string cid_a = refs[0].value("component", std::string());
        std::string cid_b = refs[1].value("component", std::string());
        auto ca_it = frames.find(cid_a);
        auto cb_it = frames.find(cid_b);
        if (ga_it == resolved_geometry.end() || gb_it == resolved_geometry.end()
            || ca_it == frames.end() || cb_it == frames.end()) {
            mate_records.push_back({mid, "unresolved"});
            continue;
        }
        std::vector<MateGeom> extra_geoms;
        std::vector<std::string> extra_owners;
        int gi = 2;
        while (true) {
            auto it = resolved_geometry.find({mid, gi});
            if (it == resolved_geometry.end()) break;
            extra_geoms.push_back(it->second);
            std::string ec = (gi < static_cast<int>(refs.size()))
                ? refs[gi].value("component", std::string()) : std::string();
            extra_owners.push_back((ec == cid_a && ec != cid_b) ? "a" : "b");
            ++gi;
        }
        double value = mate_value(mate);
        json params = (mate.contains("value") && mate["value"].is_object())
            ? mate["value"] : json::object();
        std::string mtype = mate.at("type").get<std::string>();
        std::string align = mate.value("align", std::string("aligned"));

        std::unique_ptr<MateBuilder> bptr;
        solver::ResidualFunc rfunc;
        solver::JacobianFunc jfunc;
        int m = 0;
        try {
            bptr = std::make_unique<MateBuilder>(
                &ca_it->second, ga_it->second, &cb_it->second, gb_it->second,
                mtype, align, value, extra_geoms, params, extra_owners);
            auto built = bptr->make();
            rfunc = std::get<0>(built);
            jfunc = std::get<1>(built);
            m = std::get<2>(built);
        } catch (const std::exception&) {
            mate_records.push_back({mid, "unresolved"});
            continue;
        }
        if (bptr->idx_size() == 0) {
            mate_records.push_back({mid, ""});
            continue;
        }
        system.add_residual(rfunc, bptr->idx(), jfunc, mid, 1.0);
        active_tags.push_back(mid);
        Builders br;
        br.mid = mid;
        br.builder = bptr.get();
        br.rfunc = rfunc;
        br.m = m;
        builders.push_back(br);
        builder_store.push_back(std::move(bptr));
        mate_records.push_back({mid, ""});
    }

    // Drag soft target.
    if (drag_opt) {
        auto dit = frames.find(drag_opt->component);
        if (dit != frames.end() && !dit->second.fixed) {
            for (int k = 0; k < 3; ++k)
                system.soft_target(dit->second.t_idx[k], drag_opt->target[k], 1e-3);
        }
    }

    VectorXd x;
    bool res_converged = true;
    double max_res = 0.0;
    if (system.n_vars() == 0) {
        x = VectorXd::Zero(0);
    } else {
        auto res = system.solve(std::nullopt, tol, max_iter, time_budget_ms);
        x = res.x;
        res_converged = res.converged;
        max_res = res.max_residual;
        bool any_free = false;
        for (auto& kv : frames) if (!kv.second.fixed) any_free = true;
        if (!res_converged && !drag_opt && !time_budget_ms && any_free) {
            std::mt19937_64 rng(12345);
            std::normal_distribution<double> nd(0.0, 0.6);
            VectorXd x0_base = system.init_vector();
            std::vector<int> w_indices;
            for (auto& cid : frame_order) {
                CompFrame& f = frames[cid];
                if (!f.fixed)
                    w_indices.insert(w_indices.end(), f.w_idx.begin(), f.w_idx.end());
            }
            auto best = res;
            for (int iter = 0; iter < 6; ++iter) {
                VectorXd x0 = x0_base;
                for (int wi : w_indices) x0(wi) += nd(rng);
                auto r2 = system.solve(x0, tol, max_iter);
                if (r2.max_residual < best.max_residual) best = r2;
                if (best.converged) break;
            }
            x = best.x;
            res_converged = best.converged;
            max_res = best.max_residual;
        }
    }

    MateSolveResult out;
    for (auto& cid : frame_order) {
        CompFrame& frame = frames[cid];
        Vector3d t, w;
        if (frame.fixed) {
            t = frame.t0;
            w = frame.w0;
        } else {
            t = Vector3d(x(frame.t_idx[0]), x(frame.t_idx[1]), x(frame.t_idx[2]));
            w = Vector3d(x(frame.w_idx[0]), x(frame.w_idx[1]), x(frame.w_idx[2]));
        }
        out.transforms[cid] = params_to_transform(t, w);
        out.params[cid] = {t, w};
    }

    std::vector<std::string> redundant, conflicting;
    int dof = system.n_vars();
    if (system.n_vars() > 0 && !active_tags.empty()) {
        auto analysis = system.analyze(x);
        dof = analysis.dof;
        classify_mates(builders, x, tol, redundant, conflicting);
    }

    auto in_set = [](const std::vector<std::string>& s, const std::string& v) {
        return std::find(s.begin(), s.end(), v) != s.end();
    };

    int n_comp = static_cast<int>(frames.size());
    int n_fixed = 0;
    for (auto& kv : frames) if (kv.second.fixed) ++n_fixed;

    for (auto& rec : mate_records) {
        const std::string& mid = rec.first;
        const std::string& forced = rec.second;
        std::string mtype = "?";
        for (const auto& mt : mates) {
            if (mt.at("id").get<std::string>() == mid) {
                mtype = mt.value("type", std::string("?"));
                break;
            }
        }
        std::string status;
        if (!forced.empty()) status = forced;
        else if (in_set(conflicting, mid)) status = "conflicting";
        else if (in_set(redundant, mid)) status = "redundant";
        else status = "ok";
        out.mate_status.push_back({mid, mtype, status});
    }

    out.over_defined = !conflicting.empty();
    out.dof = dof;
    out.converged = res_converged;
    out.max_residual = max_res;
    out.summary = dof_summary(dof, out.over_defined, n_comp, n_fixed);
    return out;
}

// --------------------------------------------------------------------------- JSON bridge

namespace {

Vector3d vec3(const json& j, const Vector3d& dflt) {
    if (!j.is_array() || j.size() != 3) return dflt;
    return Vector3d(j[0].get<double>(), j[1].get<double>(), j[2].get<double>());
}

MateGeom parse_geom(const json& g) {
    MateGeom mg;
    mg.kind = g.value("kind", std::string("point"));
    mg.point = vec3(g.value("point", json::array()), Vector3d::Zero());
    Vector3d n = vec3(g.value("normal", json::array()), Vector3d(0, 0, 1));
    Vector3d d = vec3(g.value("direction", json::array()), Vector3d(0, 0, 1));
    // MateGeom.__post_init__ normalizes normal+direction.
    mg.normal = unit(n);
    mg.direction = unit(d);
    mg.radius = g.value("radius", 0.0);
    return mg;
}

}  // namespace

json solve_mates_json(const json& input) {
    json components = input.value("components", json::array());
    json mates = input.value("mates", json::array());

    ResolvedGeometry geom;
    for (const auto& entry : input.value("resolved_geometry", json::array())) {
        std::string mid = entry.at("mate_id").get<std::string>();
        int ri = entry.at("ref_index").get<int>();
        geom[{mid, ri}] = parse_geom(entry.at("geom"));
    }

    std::optional<WarmMap> warm;
    if (input.contains("warm") && input["warm"].is_object() && !input["warm"].empty()) {
        WarmMap wm;
        for (auto it = input["warm"].begin(); it != input["warm"].end(); ++it) {
            const json& tw = it.value();  // {"t":[...], "w":[...]} or [[t],[w]]
            Vector3d t, w;
            if (tw.is_object()) {
                t = vec3(tw.value("t", json::array()), Vector3d::Zero());
                w = vec3(tw.value("w", json::array()), Vector3d::Zero());
            } else if (tw.is_array() && tw.size() == 2) {
                t = vec3(tw[0], Vector3d::Zero());
                w = vec3(tw[1], Vector3d::Zero());
            }
            wm[it.key()] = {t, w};
        }
        warm = wm;
    }

    std::optional<Drag> drag;
    if (input.contains("drag") && input["drag"].is_object() && !input["drag"].empty()) {
        Drag d;
        d.component = input["drag"].value("component", std::string());
        d.target = vec3(input["drag"].value("target", json::array()), Vector3d::Zero());
        drag = d;
    }

    double tol = input.value("tol", 1e-9);
    int max_iter = input.value("max_iter", 80);
    std::optional<double> tbm;
    if (input.contains("time_budget_ms") && input["time_budget_ms"].is_number())
        tbm = input["time_budget_ms"].get<double>();

    MateSolveResult res = solve_mates(components, mates, geom, warm, drag, tol, max_iter, tbm);

    json out;
    json transforms = json::object();
    for (const auto& kv : res.transforms) transforms[kv.first] = kv.second;
    out["transforms"] = transforms;

    json params = json::object();
    for (const auto& kv : res.params) {
        json p;
        p["t"] = {kv.second.first[0], kv.second.first[1], kv.second.first[2]};
        p["w"] = {kv.second.second[0], kv.second.second[1], kv.second.second[2]};
        params[kv.first] = p;
    }
    out["params"] = params;

    json statuses = json::array();
    for (const auto& s : res.mate_status)
        statuses.push_back({{"mate_id", s.mate_id}, {"type", s.type}, {"status", s.status}});
    out["mate_status"] = statuses;

    out["dof"] = res.dof;
    out["converged"] = res.converged;
    out["max_residual"] = res.max_residual;
    out["over_defined"] = res.over_defined;
    out["summary"] = res.summary;
    return out;
}

}  // namespace aicad::mate
