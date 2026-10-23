// 3D rigid-transform mate residuals and Jacobians. The implementation started
// from the Python solver, but correctness fixes here are authoritative rather
// than constrained to preserve defective oracle behavior.
#include "mate/mate.h"

#include "sim/numpy_rng.h"
#include "solver/nsolve.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <set>
#include <memory>
#include <random>
#include <stdexcept>
#include <tuple>
#include <utility>
#include <array>
#include <string>

namespace chudware::mate {
namespace {

using json = nlohmann::json;
using Eigen::Matrix3d;
using Eigen::MatrixXd;
using Eigen::Vector3d;
using Eigen::VectorXd;
using chudware::solver::System;

constexpr double kPi = 3.14159265358979323846;

double deg2rad(double d) { return d * kPi / 180.0; }

// Fold an angle difference into (-pi, pi]. An angle mate's residual is an ANGLE, so the
// difference between the measured and the requested one has to be the SHORT way round:
// without this, driving a mate from +179 deg to -179 deg is a 358 deg error instead of 2.
double wrap_pi(double a) {
    double r = std::fmod(a + kPi, 2.0 * kPi);
    if (r < 0.0) r += 2.0 * kPi;
    return r - kPi;
}

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

Matrix3d left_jacobian_inverse(const Vector3d& w) {
    const double theta = w.norm();
    const Matrix3d K = skew(w);
    const double c = theta < 1e-6 ? 1.0 / 12.0 + theta * theta / 720.0
        : (1.0 - 0.5 * theta / std::tan(0.5 * theta)) / (theta * theta);
    return Matrix3d::Identity() - 0.5 * K + c * (K * K);
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
    const Vector3d antisymmetric(R(2, 1) - R(1, 2), R(0, 2) - R(2, 0), R(1, 0) - R(0, 1));
    // atan2 retains the small sine near pi; acos amplifies trace roundoff there.
    double theta = std::atan2(0.5 * antisymmetric.norm(), cos_t);
    if (theta < 1e-8) {
        return 0.5 * antisymmetric;
    }
    if (std::abs(kPi - theta) < 1e-6) {
        Matrix3d A = (R + R.transpose() + 2.0 * Matrix3d::Identity()) * 0.25;
        int k = 0;
        double best = A(0, 0);
        for (int i = 1; i < 3; ++i) {
            if (A(i, i) > best) { best = A(i, i); k = i; }
        }
        Vector3d axis = A.col(k) / std::sqrt(std::max(A(k, k), 1e-12));
        axis = unit(axis);
        if (axis.dot(antisymmetric) < 0.0) axis = -axis;
        return axis * theta;
    }
    double s = std::sin(theta);
    return (antisymmetric / (2.0 * s)) * theta;
}

// --------------------------------------------------------------------------- vector primitives

void perp_basis(const Vector3d& axis, Vector3d& e1, Vector3d& e2) {
    Vector3d a = unit(axis);
    Vector3d helper = (std::abs(a[0]) < 0.9) ? Vector3d(1.0, 0.0, 0.0)
                                             : Vector3d(0.0, 1.0, 0.0);
    e1 = unit(a.cross(helper));
    e2 = unit(a.cross(e1));
}

Matrix3d unit_jacobian(const Vector3d& v) {
    const double norm = v.norm();
    if (norm <= 1e-12) return Matrix3d::Identity();
    const Vector3d direction = v / norm;
    return (Matrix3d::Identity() - direction * direction.transpose()) / norm;
}


// Two scalars measuring the component of vec perpendicular to axis.
VectorXd perp_residual2(const Vector3d& axis, const Vector3d& vec) {
    Vector3d e1, e2;
    perp_basis(axis, e1, e2);
    VectorXd r(2);
    r << vec.dot(e1), vec.dot(e2);
    return r;
}

// Differentiate the moving perpendicular basis as well as the measured vector.
MatrixXd perp_residual2_jac(const Vector3d& axis, const MatrixXd& Jaxis,
                            const Vector3d& vec, const MatrixXd& Jvec) {
    const Vector3d a = unit(axis);
    const Vector3d helper = std::abs(a[0]) < 0.9 ? Vector3d(1, 0, 0) : Vector3d(0, 1, 0);
    const Vector3d first = a.cross(helper), e1 = unit(first);
    const Vector3d second = a.cross(e1), e2 = unit(second);
    const Matrix3d Ja = unit_jacobian(axis);
    const Matrix3d Je1 = -unit_jacobian(first) * skew(helper) * Ja;
    const Matrix3d Je2 = unit_jacobian(second) * (-skew(e1) * Ja + skew(a) * Je1);
    MatrixXd out(2, Jvec.cols());
    out.row(0) = e1.transpose() * Jvec + (vec.transpose() * Je1) * Jaxis;
    out.row(1) = e2.transpose() * Jvec + (vec.transpose() * Je2) * Jaxis;
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
                                 const Vector3d& d_axis, const MatrixXd& Jd,
                                 const Vector3d& q, const MatrixXd& Jq) {
    Vector3d d = unit(d_axis);
    Vector3d v = q - p_axis;
    Vector3d perp = v - v.dot(d) * d;
    double nrm = perp.norm();
    if (nrm < 1e-9) return MatrixXd::Zero(1, Jp.cols());
    Vector3d u = perp / nrm;
    return u.transpose() * (Jq - Jp)
        - v.dot(d) * (u.transpose() * unit_jacobian(d_axis)) * Jd;
}

// --------------------------------------------------------------------------- component frame

struct CompFrame {
    std::string id;
    Vector3d t0 = Vector3d::Zero();
    Vector3d w0 = Vector3d::Zero();
    bool fixed = false;
    // A FIXED component's placement is authored, not solved: it is a hard constraint, so
    // both of these are the document's own bytes rather than anything re-derived. `R0` is
    // the transform's 3x3 block VERBATIM (a measured placement is orthonormal only to
    // within the measurement, and re-projecting it moves the anchor's geometry), and
    // `tf0` is the 16 doubles echoed back unchanged. Unused for free components, whose
    // rotation is the exp-map of their solved variables.
    Matrix3d R0 = Matrix3d::Identity();
    std::vector<double> tf0;
    // The DOCUMENT's rotation for this component, independent of any warm start. A mate
    // that has to derive geometry of its own (the angle mate's reference axis) derives it
    // from this, so a driven sweep — which re-solves the same document warm-started from
    // the previous frame — measures the same signed angle at every frame instead of one
    // that drifts with the solver's own path.
    Matrix3d R_auth = Matrix3d::Identity();
    Vector3d t_auth = Vector3d::Zero();
    std::vector<int> t_idx;   // 3 var indices (translation), empty if fixed
    std::vector<int> w_idx;   // 3 var indices (rotation vector), empty if fixed

    void alloc(System& sys) {
        if (fixed) return;
        t_idx = sys.add_vars(3, {t0[0], t0[1], t0[2]});
        w_idx = sys.add_vars(3, {w0[0], w0[1], w0[2]});
    }

    // Rotation matrix for a pose vector. A fixed component has no pose variables and no
    // exp-map: its rotation is the authored block, whatever `w` says.
    Matrix3d rot(const Vector3d& w) const { return fixed ? R0 : rodrigues_impl(w); }

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

// Split a 16-double row-major transform into the solver's pose parameters. `R_authored`,
// when given, receives the 3x3 block VERBATIM — the caller that pins a fixed component
// needs the document's own rotation, not the orthonormal projection the exp-map
// parameterization requires.
void transform_to_params(const std::vector<double>& transform, Vector3d& t, Vector3d& w,
                         Matrix3d* R_authored = nullptr) {
    Eigen::Matrix4d M;
    for (int r = 0; r < 4; ++r)
        for (int c = 0; c < 4; ++c)
            M(r, c) = transform[r * 4 + c];
    Matrix3d Rm = M.block(0, 0, 3, 3);
    t = M.block(0, 3, 3, 1);
    if (R_authored) *R_authored = Rm;
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
class MateBuilder {
public:
    MateBuilder(CompFrame* fa, MateGeom ga, CompFrame* fb, MateGeom gb,
                std::string mtype, const std::string& align, double value,
                std::vector<MateGeom> extra, json params,
                std::vector<std::string> extra_owners, const json* reference_frames)
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
        auto bind_frame = [&](const CompFrame* frame, Matrix3d& rotation, Vector3d& translation) {
            rotation = frame->R_auth;
            translation = frame->t_auth;
            if (!reference_frames) return;
            if (!reference_frames->is_object() || !reference_frames->contains(frame->id))
                throw std::runtime_error("mate reference_frames missing component " + frame->id);
            const json& encoded = reference_frames->at(frame->id);
            if (!encoded.is_array() || encoded.size() != 16)
                throw std::runtime_error("mate reference frame must contain 16 numbers");
            for (const auto& value : encoded)
                if (!value.is_number() || !std::isfinite(value.get<double>()))
                    throw std::runtime_error("mate reference frame must be finite and numeric");
            Vector3d w;
            transform_to_params(encoded.get<std::vector<double>>(), translation, w, &rotation);
            if (!frame->fixed) rotation = rodrigues_impl(w);
        };
        bind_frame(fa_, Ra_reference_, ta_reference_);
        bind_frame(fb_, Rb_reference_, tb_reference_);
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
    Matrix3d Ra_reference_, Rb_reference_;
    Vector3d ta_reference_, tb_reference_;

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

    const CompFrame* frame_of(const std::string& which) const {
        return (which == "a") ? fa_ : fb_;
    }

    // world point + 3x6 jac wrt that component's vars. A fixed component contributes a
    // CONSTANT: its rotation is the authored block (not the exp-map of a pose vector it
    // does not own) and its Jacobian block is dropped by `place`, so it stays zero here.
    void wp(const std::string& which, const VectorXd& xs, const Vector3d& p_local,
            Vector3d& wp_out, MatrixXd& J) const {
        const CompFrame* f = frame_of(which);
        VectorXd x6 = (which == "a") ? xa(xs) : xb(xs);
        Vector3d w = x6.segment(3, 3);
        wp_out = f->rot(w) * p_local + x6.head(3);
        J = MatrixXd::Zero(3, 6);
        if (f->fixed) return;
        J.block(0, 0, 3, 3) = Matrix3d::Identity();
        J.block(0, 3, 3, 3) = rot_jac(w, p_local);
    }

    void wd(const std::string& which, const VectorXd& xs, const Vector3d& d_local,
            Vector3d& wd_out, MatrixXd& J) const {
        const CompFrame* f = frame_of(which);
        VectorXd x6 = (which == "a") ? xa(xs) : xb(xs);
        Vector3d w = x6.segment(3, 3);
        wd_out = f->rot(w) * d_local;
        J = MatrixXd::Zero(3, 6);
        if (f->fixed) return;
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
        if (!v.is_number())
            throw std::runtime_error("mate parameter '" + key + "' must be numeric");
        const double out = v.get<double>();
        if (!std::isfinite(out))
            throw std::runtime_error("mate parameter '" + key + "' must be finite");
        return out;
    }

    // A 3-vector mate parameter under any of `keys`, or nullopt when none is authored.
    // Used for the angle mate's stored reference axis.
    std::optional<Vector3d> param_vec3(std::initializer_list<const char*> keys) const {
        if (!params_.is_object()) return std::nullopt;
        for (const char* key : keys) {
            if (!params_.contains(key)) continue;
            const json& v = params_[key];
            if (!v.is_array() || v.size() != 3)
                throw std::runtime_error(std::string("mate parameter '") + key
                                         + "' must be a 3-element vector");
            Vector3d out;
            for (int k = 0; k < 3; ++k) {
                if (!v[k].is_number())
                    throw std::runtime_error(std::string("mate parameter '") + key
                                             + "' must be numeric");
                out[k] = v[k].get<double>();
                if (!std::isfinite(out[k]))
                    throw std::runtime_error(std::string("mate parameter '") + key
                                             + "' must be finite");
            }
            if (out.norm() < 1e-9)
                throw std::runtime_error(std::string("mate parameter '") + key
                                         + "' must not be the zero vector");
            return unit(out);
        }
        return std::nullopt;
    }

    // Explicit virtual pins are stored in A's local frame. Bind their point and
    // direction to B using the authored placements, never a previous solve's pose.
    void bind_virtual_pin(const Vector3d& axis_a, Vector3d& point_a,
                          Vector3d& point_b, Vector3d& axis_b) const {
        const json& point = params_.at("axis_point");
        if (!point.is_array() || point.size() != 3)
            throw std::runtime_error(mtype_ + " axis_point must be a 3-element vector");
        for (int k = 0; k < 3; ++k) {
            if (!point[k].is_number() || !std::isfinite(point[k].get<double>()))
                throw std::runtime_error(mtype_ + " axis_point must be finite and numeric");
            point_a[k] = point[k].get<double>();
        }
        point_b = Rb_reference_.transpose() *
            (Ra_reference_ * point_a + ta_reference_ - tb_reference_);
        axis_b = Rb_reference_.transpose() * Ra_reference_ * axis_a;
    }

    Vector3d axis_world(const std::string& which) const {
        const MateGeom& g = (which == "a") ? ga_ : gb_;
        const CompFrame* f = frame_of(which);
        return unit(f->rot(f->w0) * g.dir_vector());
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
        if (ga_.kind == "axis" && gb_.kind == "axis") return concentric();
        const Vector3d pa_l = ga_.point, pb_l = gb_.point;
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

    // An angle mate is a SIGNED angle about a reference axis, never a dot product.
    //
    // `da . db - cos(target)` has a derivative of -sin(theta) in the angle it is trying
    // to control, so at 0 deg and at 180 deg its Jacobian row is exactly ZERO: the mate
    // adds no rank (reported `redundant`, residual 0, the solver cannot drive it) and a
    // mate authored a few degrees off zero has a first-order-flat residual the LM stalls
    // on (a rover steer joint stuck at 3.589e-03 across three passes). A dot
    // product also cannot tell +30 deg from -30 deg, so a sweep folds at zero instead of
    // passing through it.
    //
    // Instead: theta = atan2(ref . (da x db), da.db - (da.ref)(db.ref)) — the signed
    // angle from da to db about `ref`, measured on the projections of both directions
    // into the plane perpendicular to ref. Written this way it needs no explicit
    // normalization: numerator and denominator are both bilinear in (da, db), so the
    // quotient is exactly invariant to their lengths. d(theta) = 1 per radian of relative
    // rotation about ref at EVERY angle, zero included, so the row always carries rank.
    // `ref` is a direction attached to component A (see angle_reference_local) and is
    // differentiated with the rest, so the mate is exact when A itself rotates.
    Built angle() {
        if (!std::isfinite(value_))
            throw std::runtime_error("angle value must be finite");
        Vector3d da_l = unit(ga_.dir_vector());
        // `align: anti` measures to the opposite of b's direction, as it does for every
        // other direction mate here: an anti target of A deg is the aligned A - 180 deg.
        Vector3d db_l = unit(Vector3d(sign_ * gb_.dir_vector()));
        const double target = deg2rad(value_);
        const Vector3d ref_l = angle_reference_local(da_l, db_l);
        if (params_.is_object() && params_.contains("axis_point") &&
            (params_.contains("axis") || params_.contains("reference_axis"))) {
            const Vector3d db_a = Ra_reference_.transpose() * Rb_reference_ * db_l;
            if (da_l.cross(ref_l).norm() < 1e-6 || db_a.cross(ref_l).norm() < 1e-6) {
                // Faces normal to the pin cannot measure its phase. A line-carrying
                // mate is a joint between BODY frames, so measure rotation from
                // their authored relative orientation instead (zero at authoring).
                Vector3d unused;
                perp_basis(ref_l, da_l, unused);
                db_l = sign_ * Rb_reference_.transpose() * Ra_reference_ * da_l;
            }
        }
        auto self = this;
        // s = |pa||pb| sin(theta), c = |pa||pb| cos(theta) for the ref-perpendicular
        // projections pa, pb of da, db. `ref` is normalized here (s is linear in it, c
        // quadratic), which is exact: R is orthonormal for a free component, and a fixed
        // one carries no Jacobian block at all.
        auto measure = [=](const VectorXd& xs, Vector3d& da, Vector3d& db, Vector3d& ref,
                           MatrixXd& Jda, MatrixXd& Jdb, MatrixXd& Jref,
                           double& s, double& c) {
            self->wd("a", xs, da_l, da, Jda);
            self->wd("b", xs, db_l, db, Jdb);
            self->wd("a", xs, ref_l, ref, Jref);
            Jref = unit_jacobian(ref) * Jref;
            ref = unit(ref);
            s = ref.dot(da.cross(db));
            c = da.dot(db) - da.dot(ref) * db.dot(ref);
        };
        R rfunc = [=](const VectorXd& xs) {
            Vector3d da, db, ref; MatrixXd Jda, Jdb, Jref;
            double s = 0.0, c = 0.0;
            measure(xs, da, db, ref, Jda, Jdb, Jref, s, c);
            VectorXd r(1);
            r(0) = wrap_pi(std::atan2(s, c) - target);
            return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d da, db, ref; MatrixXd Jda, Jdb, Jref;
            double s = 0.0, c = 0.0;
            measure(xs, da, db, ref, Jda, Jdb, Jref, s, c);
            // d(atan2(s,c)) = (c ds - s dc) / (s^2 + c^2). The denominator is
            // |pa|^2|pb|^2 — it vanishes only if a direction turns onto the reference
            // axis itself, which no angle about that axis can describe.
            const double den = std::max(s * s + c * c, 1e-24);
            const Vector3d pa = da - da.dot(ref) * ref;
            const Vector3d pb = db - db.dot(ref) * ref;
            const Vector3d g_da = (c * db.cross(ref) - s * pb) / den;
            const Vector3d g_db = (c * ref.cross(da) - s * pa) / den;
            const Vector3d g_ref =
                (c * da.cross(db) + s * (db.dot(ref) * da + da.dot(ref) * db)) / den;
            return MatrixXd(self->place("a", (g_da.transpose() * Jda).eval())
                            + self->place("b", (g_db.transpose() * Jdb).eval())
                            + self->place("a", (g_ref.transpose() * Jref).eval()));
        };
        if (params_.is_object() && params_.contains("axis_point") &&
            (params_.contains("axis") || params_.contains("reference_axis"))) {
            Vector3d pa_l, pb_l, axis_b;
            bind_virtual_pin(ref_l, pa_l, pb_l, axis_b);
            // Three point rows preserve the pin's axial station as well as its line.
            R angle_r = rfunc;
            J angle_j = jfunc;
            rfunc = [=](const VectorXd& xs) {
                Vector3d a, b, pa, pb; MatrixXd unused;
                self->wd("a", xs, ref_l, a, unused);
                self->wd("b", xs, axis_b, b, unused);
                self->wp("a", xs, pa_l, pa, unused);
                self->wp("b", xs, pb_l, pb, unused);
                VectorXd r(7);
                r.head(3) = unit(a) - unit(b);
                r.segment(3, 3) = pb - pa;
                r(6) = angle_r(xs)(0);
                return r;
            };
            jfunc = [=](const VectorXd& xs) {
                Vector3d a, b, pa, pb; MatrixXd Ja, Jb, Jpa, Jpb;
                self->wd("a", xs, ref_l, a, Ja);
                self->wd("b", xs, axis_b, b, Jb);
                self->wp("a", xs, pa_l, pa, Jpa);
                self->wp("b", xs, pb_l, pb, Jpb);
                MatrixXd out = self->empty_jac(7);
                out.topRows(3) = self->place("a", (unit_jacobian(a) * Ja).eval())
                    - self->place("b", (unit_jacobian(b) * Jb).eval());
                out.middleRows(3, 3) = self->place("b", Jpb) - self->place("a", Jpa);
                out.bottomRows(1) = angle_j(xs);
                return out;
            };
            return {rfunc, jfunc, 7};
        }
        return {rfunc, jfunc, 1};
    }

    // The angle mate's reference axis, expressed in component A's LOCAL frame (so it
    // turns with A, the way a joint axis does). Priority:
    //   1. authored on the mate as `value.axis` (alias `reference_axis`), in A's local
    //      frame — the only way to NAME the joint axis, and what a mate driven through
    //      0 deg should carry;
    //   2. a third resolved geometry ref on the mate (ref_index 2), carried into A's
    //      frame;
    //   3. the cross product of the two directions at the components' AUTHORED poses —
    //      the axis that actually carries a to b whenever the authored angle is neither
    //      0 nor 180 deg;
    //   4. the geoms' own companion vector: for a plane its `direction`, for an axis its
    //      `normal`, a's first then b's. The document's selector resolver fills in only
    //      the vector a mate measures, so unless the caller supplied one this is the
    //      component's local +Z — a guess, which is why case 1 exists;
    //   5. any perpendicular of da.
    // An authored direction is normalized but NEVER projected onto a different
    // axis. The angle uses the perpendicular projections of the measured normals.
    // Derived fallback cases 2-5 are made perpendicular to da and sign-canonicalized
    // so crossing zero cannot silently turn +30 degrees into -30 degrees.
    //
    // Nothing here reads the SOLVER's seed. A driven sweep re-solves the same document
    // warm-started from the previous frame, so a reference derived from the warm pose
    // would be re-derived per frame — and would change kind (case 3 -> case 4) exactly at
    // the frame that lands on zero. Derived from the document, one sweep measures one
    // signed angle throughout.
    Vector3d angle_reference_local(const Vector3d& da_l, const Vector3d& db_l) const {
        auto perp_to_da = [&](const Vector3d& v) {
            return Vector3d(v - v.dot(da_l) * da_l);
        };
        const Matrix3d& Ra = Ra_reference_;
        const Matrix3d& Rb = Rb_reference_;

        if (auto authored = param_vec3({"axis", "reference_axis"})) {
            const Vector3d p = perp_to_da(*authored);
            if (p.norm() < 1e-6 && !params_.contains("axis_point"))
                throw std::runtime_error(
                    "angle reference axis is parallel to the mate's first direction: "
                    "no angle can be measured about it");
            return unit(*authored);
        }

        std::vector<Vector3d> candidates;
        if (!extra_.empty()) {
            const MateGeom& g = extra_.front();
            const Matrix3d& Re = (extra_owners_.front() == "a") ? Ra : Rb;
            candidates.push_back(Ra.transpose() * (Re * g.dir_vector()));
        }
        candidates.push_back(Ra.transpose() * (Ra * da_l).cross(Rb * db_l));
        candidates.push_back(ga_.kind == "plane" ? ga_.direction : ga_.normal);
        candidates.push_back(Ra.transpose()
                             * (Rb * (gb_.kind == "plane" ? gb_.direction : gb_.normal)));
        Vector3d e1, e2;
        perp_basis(da_l, e1, e2);
        candidates.push_back(e1);

        for (const Vector3d& cand : candidates) {
            const Vector3d p = perp_to_da(cand);
            if (p.norm() < 1e-6) continue;
            return canonical_sign(unit(p));
        }
        return canonical_sign(e1);
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
            VectorXd r(5);
            r.head(3) = unit(da) - sign * unit(db);
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
            Jm.block(0, 0, 3, self->icols()) =
                self->place("a", (unit_jacobian(da) * Jda).eval())
                - sign * self->place("b", (unit_jacobian(db) * Jdb).eval());
            Vector3d diff = pb - pa;
            MatrixXd Jdiff = self->place("b", Jpb) - self->place("a", Jpa);
            Jm.block(3, 0, 2, self->icols()) =
                perp_residual2_jac(da, self->place("a", Jda), diff, Jdiff);
            return Jm;
        };
        return {rfunc, jfunc, 5};
    }

    // ----- distance -------------------------------------------------------
    Built distance() {
        bool use_plane = (ga_.kind == "plane") || (gb_.kind == "plane");
        if (!std::isfinite(value_))
            throw std::runtime_error("distance value must be finite");
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
        const Matrix3d Ra0 = Ra_reference_, Rb0 = Rb_reference_;
        const Vector3d ta0 = ta_reference_, tb0 = tb_reference_;
        Matrix3d relR0 = Ra0.transpose() * Rb0;
        Vector3d relt0 = Ra0.transpose() * (tb0 - ta0);
        auto self = this;
        R rfunc = [=](const VectorXd& xs) {
            VectorXd xav = self->xa(xs), xbv = self->xb(xs);
            Matrix3d Ra = self->fa_->rot(xav.segment(3, 3));
            Vector3d ta = xav.head(3);
            Matrix3d Rb = self->fb_->rot(xbv.segment(3, 3));
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
            const VectorXd xav = self->xa(xs), xbv = self->xb(xs);
            const Vector3d wa = xav.tail(3), wb = xbv.tail(3);
            const Matrix3d Ra = self->fa_->rot(wa), Rb = self->fb_->rot(wb);
            const Vector3d diff = xbv.head(3) - xav.head(3);
            const Vector3d error = mat_to_rotvec_impl(relR0.transpose() * Ra.transpose() * Rb);
            const Matrix3d rotation_gradient =
                left_jacobian_inverse(error) * relR0.transpose() * Ra.transpose();
            MatrixXd Jm = self->empty_jac(6);
            if (self->fb_->var_size()) {
                Jm.block(0, self->na_, 3, 3) = Ra.transpose();
                Jm.block(3, self->na_ + 3, 3, 3) = rotation_gradient * left_jacobian(wb);
            }
            if (self->na_) {
                Jm.block(0, 0, 3, 3) = -Ra.transpose();
                Jm.block(0, 3, 3, 3) = Ra.transpose() * skew(diff) * left_jacobian(wa);
                Jm.block(3, 3, 3, 3) = -rotation_gradient * left_jacobian(wa);
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
        if (lo > hi)
            throw std::runtime_error("distance limit min must be <= max");
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
        if (lo > hi)
            throw std::runtime_error("angle limit min must be <= max");
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

    // Incremental rotation about the initial world axis.  Subtracting the
    // current solve's warm-start orientation preserves the authored gear phase
    // and makes coupled mates transmit only the rotation introduced by this
    // solve.  Using absolute Rodrigues coordinates forced an unchanged,
    // initially rotated mechanism to jump to an arbitrary zero-phase pose.
    void theta_row(const std::string& which,
                   std::function<double(const VectorXd&)>& theta_fn,
                   std::function<MatrixXd(const VectorXd&)>& row_fn) const {
        Vector3d ax = axis_world(which);
        const Vector3d w0 = (which == "a") ? fa_->w0 : fb_->w0;
        auto self = this;
        theta_fn = [=](const VectorXd& xs) {
            VectorXd x6 = (which == "a") ? self->xa(xs) : self->xb(xs);
            return double((x6.segment(3, 3) - w0).dot(ax));
        };
        row_fn = [=](const VectorXd& xs) {
            MatrixXd r6 = MatrixXd::Zero(1, 6);
            r6.block(0, 3, 1, 3) = ax.transpose();
            return self->place(which, r6);
        };
    }

    Built gear() {
        double ratio = param("ratio", 1.0);
        if (std::abs(ratio) <= 1e-12)
            throw std::runtime_error("gear ratio must be finite and non-zero");
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
        if (pr <= 0.0)
            throw std::runtime_error("rack/pinion pitch_radius must be finite and > 0");
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
        if (std::abs(pitch) <= 1e-12)
            throw std::runtime_error("screw pitch must be finite and non-zero");
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
        const auto axis = param_vec3({"axis", "reference_axis"});
        const bool explicit_pin = axis.has_value() || params_.contains("axis_point");
        if (explicit_pin) {
            if (!axis || !params_.contains("axis_point"))
                throw std::runtime_error("hinge virtual pin requires axis and axis_point");
            da_l = *axis;
            bind_virtual_pin(da_l, pa_l, pb_l, db_l);
            // Both directions name the same authored body-frame line; selector
            // directions and their align flag must not relocate this virtual pin.
            sign = 1.0;
        }
        Vector3d ma_l, mb_l;
        if (extra_.size() >= 2) {
            ma_l = extra_[0].dir_vector();
            mb_l = extra_[1].dir_vector();
        } else {
            Vector3d e1, e2;
            perp_basis(da_l, e1, e2); ma_l = e1;
            if (explicit_pin)
                mb_l = Rb_reference_.transpose() * Ra_reference_ * ma_l;
            else {
                perp_basis(db_l, e1, e2); mb_l = e1;
            }
        }
        double lo = deg2rad(param("min", 0.0));
        double hi = deg2rad(param("max", 360.0));
        if (lo > hi)
            throw std::runtime_error("hinge limit min must be <= max");
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
            VectorXd r(7);
            // An oriented pin must not accept the antiparallel branch. A cross
            // product vanishes at both directions, regardless of the align sign.
            // The three chord components have rank two at alignment and leave
            // rotation about the pin free.
            r.head(3) = unit(da) - sign * unit(db);
            // A revolute joint makes the two axis origins coincident.  The old
            // two-component perpendicular residual left translation along the
            // hinge axis free, so "hinge" actually behaved as a cylindrical
            // joint and motion sweeps could drift axially.
            r.segment(3, 3) = pb - pa;
            double ang; MatrixXd row; swing(xs, ang, row);
            if (ang < lo) r(6) = ang - lo;
            else if (ang > hi) r(6) = ang - hi;
            else r(6) = 0.0;
            return r;
        };
        J jfunc = [=](const VectorXd& xs) {
            Vector3d da, db, pa, pb; MatrixXd Jda, Jdb, Jpa, Jpb;
            self->wd("a", xs, da_l, da, Jda);
            self->wd("b", xs, db_l, db, Jdb);
            self->wp("a", xs, pa_l, pa, Jpa);
            self->wp("b", xs, pb_l, pb, Jpb);
            MatrixXd Jm = self->empty_jac(7);
            Jm.block(0, 0, 3, self->icols()) =
                self->place("a", (unit_jacobian(da) * Jda).eval())
                - sign * self->place("b", (unit_jacobian(db) * Jdb).eval());
            Jm.block(3, 0, 3, self->icols()) =
                self->place("b", Jpb) - self->place("a", Jpa);
            double ang; MatrixXd row; swing(xs, ang, row);
            if (ang < lo || ang > hi) Jm.block(6, 0, 1, self->icols()) = row;
            return Jm;
        };
        return {rfunc, jfunc, 7};
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
    if (std::min(M.rows(), M.cols()) >= 128 && M.allFinite()) {
        Eigen::BDCSVD<MatrixXd> fast(M);
        if (fast.info() == Eigen::Success) {
            const auto& values = fast.singularValues();
            // Keep the existing absolute cutoff. Near it, retain the original Jacobi
            // calculation rather than letting a different SVD's roundoff decide rank.
            const double guard = 8 * Eigen::NumTraits<double>::epsilon()
                               * static_cast<double>(std::max(M.rows(), M.cols())) * M.norm();
            bool decisive = values.allFinite() && std::isfinite(guard);
            int rank = 0;
            for (int i = 0; i < values.size(); ++i) {
                if (std::abs(values(i) - tol) <= guard) decisive = false;
                if (values(i) > tol) ++rank;
            }
            if (decisive) return rank;
        }
    }
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
    solver::JacobianFunc jfunc;
    int m;
    std::string type;   // mate type, for the residual report
    std::string cid_a;  // the two components this mate couples (for group scoping)
    std::string cid_b;
};

// Largest absolute entry and 2-norm of one mate's residual block at configuration x.
// Positional rows are mm, direction rows dimensionless — see MateResidual.
void mate_residual_at(const Builders& b, const VectorXd& x, double& max_abs, double& norm) {
    const std::vector<int>& idx = b.builder->idx();
    VectorXd xsub(static_cast<int>(idx.size()));
    for (size_t i = 0; i < idx.size(); ++i) xsub(static_cast<int>(i)) = x(idx[i]);
    const VectorXd r = b.rfunc(xsub);
    max_abs = r.size() ? r.cwiseAbs().maxCoeff() : 0.0;
    norm = r.norm();
}

// Component ids belonging to a mate-connected group that holds at least one mate whose
// residual exceeds `tol` at x. Components joined only by satisfied mates — and
// components in a different chain entirely — are NOT in the set, so the random-restart
// escape can only disturb the part of the assembly that is actually failing.
std::set<std::string> failing_groups(const std::vector<Builders>& builders,
                                     const std::vector<std::string>& frame_order,
                                     const VectorXd& x, double tol) {
    std::map<std::string, std::string> parent;  // union-find over component ids
    for (const std::string& cid : frame_order) parent[cid] = cid;
    std::function<std::string(const std::string&)> find =
        [&](const std::string& a) -> std::string {
            auto it = parent.find(a);
            if (it == parent.end()) return a;
            if (it->second == a) return a;
            return it->second = find(it->second);
        };
    auto unite = [&](const std::string& a, const std::string& b) {
        auto ra = find(a), rb = find(b);
        if (parent.count(ra) && parent.count(rb) && ra != rb) parent[ra] = rb;
    };
    for (const Builders& b : builders) unite(b.cid_a, b.cid_b);

    std::set<std::string> failing_roots;
    for (const Builders& b : builders) {
        double max_abs = 0.0, norm = 0.0;
        mate_residual_at(b, x, max_abs, norm);
        if (max_abs > tol) failing_roots.insert(find(b.cid_a));
    }
    std::set<std::string> out;
    for (const std::string& cid : frame_order)
        if (failing_roots.count(find(cid))) out.insert(cid);
    return out;
}

void classify_mates(std::vector<Builders>& builders, const VectorXd& x, double tol,
                    std::vector<std::string>& redundant,
                    std::vector<std::string>& conflicting) {
    const double rtol = std::max(1e-6, 1e3 * tol);
    const double rank_tol = std::max(1e-9, tol);
    struct Group {
        std::vector<int> columns;  // sorted global variable indices
        MatrixXd rows;
        int rank = 0;
    };
    std::vector<Group> groups;
    groups.reserve(builders.size());
    std::vector<int> owner(x.size(), -1);
    std::vector<int> local_column(x.size(), -1);
    for (auto& b : builders) {
        const std::vector<int>& idx = b.builder->idx();
        // sub-vector x[idx]
        VectorXd xsub(idx.size());
        for (size_t i = 0; i < idx.size(); ++i) xsub(static_cast<int>(i)) = x(idx[i]);
        // Untouched variable groups form independent diagonal blocks. With an
        // absolute singular-value threshold their cached ranks add exactly; only
        // groups touched by this mate need to be factored again.
        std::vector<int> touched;
        for (int column : idx)
            if (owner[column] >= 0) touched.push_back(owner[column]);
        std::sort(touched.begin(), touched.end());
        touched.erase(std::unique(touched.begin(), touched.end()), touched.end());
        std::vector<int> columns(idx.begin(), idx.end());
        Eigen::Index prior_rows = 0;
        int base_rank = 0;
        for (int id : touched) {
            const Group& group = groups[id];
            columns.insert(columns.end(), group.columns.begin(), group.columns.end());
            prior_rows += group.rows.rows();
            base_rank += group.rank;
        }
        std::sort(columns.begin(), columns.end());
        columns.erase(std::unique(columns.begin(), columns.end()), columns.end());
        MatrixXd candidate;
        int new_rank = base_rank;
        // Rank cannot exceed the number of columns. Once these groups already
        // span every variable, this mate cannot add rank; only its residual can
        // distinguish redundant from conflicting. Rejected mates change no group.
        if (base_rank < static_cast<int>(columns.size())) {
            const MatrixXd Jb = b.jfunc(xsub);
            for (size_t i = 0; i < columns.size(); ++i)
                local_column[columns[i]] = static_cast<int>(i);

            candidate = MatrixXd::Zero(prior_rows + Jb.rows(), columns.size());
            Eigen::Index row = 0;
            for (int id : touched) {
                const Group& group = groups[id];
                for (size_t i = 0; i < group.columns.size(); ++i)
                    candidate.col(local_column[group.columns[i]]).segment(row, group.rows.rows())
                        = group.rows.col(static_cast<int>(i));
                row += group.rows.rows();
            }
            for (size_t i = 0; i < idx.size(); ++i)
                candidate.col(local_column[idx[i]]).tail(Jb.rows()) = Jb.col(static_cast<int>(i));
            new_rank = matrix_rank_abs(candidate, rank_tol);
        }
        const int gained = new_rank - base_rank;
        if (gained <= 0) {
            const VectorXd rb = b.rfunc(xsub);
            if (rb.size() > 0 && rb.cwiseAbs().maxCoeff() > rtol)
                conflicting.push_back(b.mid);
            else
                redundant.push_back(b.mid);
        } else {
            // Keep every row of an accepted mate, even if only some add rank.
            // Rejected mates must not merge groups or contribute rows later.
            const int accepted = touched.empty() ? static_cast<int>(groups.size()) : touched.front();
            if (touched.empty()) groups.emplace_back();
            for (int id : touched) {
                if (id == accepted) continue;
                groups[id].columns.clear();
                groups[id].rows.resize(0, 0);
                groups[id].rank = 0;
            }
            Group& group = groups[accepted];
            group.columns = std::move(columns);
            group.rows = std::move(candidate);
            group.rank = new_rank;
            for (int column : group.columns) owner[column] = accepted;
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
// Pick one of the two directions along an axis, from the vector's own numbers. An axis
// and its negation describe the same line, but they are OPPOSITE references for a signed
// angle, so a derived reference axis has to land on the same one every time it is
// derived — including from a seed a hair either side of 0 deg, where the natural
// derivation (a cross product) flips. `g` is a generic direction: only an axis lying in
// the one plane perpendicular to it is ambiguous, and that case falls back to making the
// largest component positive.
Vector3d canonical_sign(const Vector3d& v) {
    static const Vector3d g = unit(Vector3d(5.0, 2.0, 1.0));
    const double d = v.dot(g);
    if (std::abs(d) > 1e-9) return (d < 0.0) ? Vector3d(-v) : v;
    int k = 0;
    for (int i = 1; i < 3; ++i)
        if (std::abs(v[i]) > std::abs(v[k])) k = i;
    return (v[k] < 0.0) ? Vector3d(-v) : v;
}

namespace {

// Why the first two refs of a mate did not reach the solver, named per ref. The caller
// (doc/assembly.cpp compile_assembly) leaves a ref without geometry when its component is
// not loaded or its selector does not resolve on that component, and leaves a component
// out of `components` when it is missing, suppressed or failed to load. "mate references
// could not be resolved" named neither the ref nor the cause (c250 m13).
std::string unresolved_ref_reason(const json& refs, const std::array<std::string, 2>& cids,
                                  const std::array<bool, 2>& in_solve,
                                  const std::array<bool, 2>& has_geom) {
    std::string out;
    for (int i = 0; i < 2; ++i) {
        if (in_solve[i] && has_geom[i]) continue;
        const std::string at = "refs[" + std::to_string(i) + "]";
        std::string why;
        if (cids[i].empty()) {
            why = at + " names no component";
        } else if (!in_solve[i]) {
            why = at + " component '" + cids[i] + "' is not in the solve. The component is "
                  "missing, suppressed, or its source did not load";
        } else {
            const json sel = refs[i].is_object() && refs[i].contains("selector")
                ? refs[i]["selector"] : json(nullptr);
            const std::string kind = sel.is_object() && sel.contains("kind") &&
                                             sel["kind"].is_string()
                ? sel["kind"].get<std::string>() : std::string();
            const std::string sid = sel.is_object() && sel.contains("sid") &&
                                            sel["sid"].is_string()
                ? sel["sid"].get<std::string>() : std::string();
            if (!sel.is_object()) {
                why = at + " on component '" + cids[i] + "' has no selector object";
            } else if (kind != "face" && kind != "edge" && kind != "vertex") {
                why = at + " on component '" + cids[i] + "' has selector kind '" + kind +
                      "'. A mate ref needs a face, edge or vertex selector";
            } else {
                why = at + " " + kind + (sid.empty() ? "" : " '" + sid + "'") +
                      " did not resolve on component '" + cids[i] + "'. No " + kind +
                      " of that component matches the stored selector, or its surface type "
                      "changed. This occurs when the component geometry changed after the "
                      "mate was made. Make the mate again with asm_mate and "
                      "{component, query}";
            }
        }
        out += (out.empty() ? "" : "; ") + why;
    }
    return out;
}

}  // namespace

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
        Matrix3d R_authored = Matrix3d::Identity();
        // The document's own placement, read whatever the warm map says: it is the pose a
        // fixed component is pinned at, and the frame every derived mate reference is
        // expressed in.
        std::vector<double> tf = comp.contains("transform") && !comp["transform"].is_null()
            ? comp["transform"].get<std::vector<double>>()
            : kIdentity16;
        if (tf.size() != 16) tf = kIdentity16;
        Vector3d t_auth, w_auth;
        transform_to_params(tf, t_auth, w_auth, &R_authored);
        // A fixed component's pose is AUTHORED, never solved: it must come from
        // `transform`, never from the warm map. `warm` carries the previous solve's params
        // for every component (fixed included, see the fill below), so reading it here
        // silently discards an authored re-pose of the anchor.
        const bool fixed = comp.value("fixed", false);
        auto wit = fixed ? warm.end() : warm.find(cid);
        if (wit != warm.end()) {
            t = wit->second.first;
            w = wit->second.second;
        } else {
            t = t_auth;
            w = w_auth;
        }
        CompFrame frame;
        frame.id = cid;
        frame.t0 = t;
        frame.w0 = w;
        frame.fixed = fixed;
        frame.R_auth = fixed ? R_authored : rodrigues_impl(w_auth);
        frame.t_auth = t_auth;
        if (fixed) {
            // Hard constraint, carried by the document's own numbers: the residuals see the
            // authored rotation block and the result echoes the authored 16 doubles.
            frame.R0 = R_authored;
            frame.tf0 = tf;
        }
        frames[cid] = frame;
        frame_order.push_back(cid);
    }

    for (auto& cid : frame_order) frames[cid].alloc(system);

    std::vector<std::pair<std::string, std::string>> mate_records;  // (mid, forced-status or "")
    std::map<std::string, std::string> unresolved_reasons;
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
            unresolved_reasons[mid] = "the mate has " + std::to_string(refs.size()) +
                " ref(s). A mate needs at least 2 refs, each {component, selector}";
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
            unresolved_reasons[mid] = unresolved_ref_reason(
                refs, {cid_a, cid_b}, {ca_it != frames.end(), cb_it != frames.end()},
                {ga_it != resolved_geometry.end(), gb_it != resolved_geometry.end()});
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
                mtype, align, value, extra_geoms, params, extra_owners,
                mate.contains("reference_frames") ? &mate.at("reference_frames") : nullptr);
            auto built = bptr->make();
            rfunc = std::get<0>(built);
            jfunc = std::get<1>(built);
            m = std::get<2>(built);
        } catch (const std::exception& e) {
            unresolved_reasons[mid] = e.what();
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
        br.rfunc = std::move(rfunc);
        br.jfunc = std::move(jfunc);
        br.m = m;
        br.type = mtype;
        br.cid_a = cid_a;
        br.cid_b = cid_b;
        builders.push_back(std::move(br));
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

    // --- assembly scale -----------------------------------------------------
    // The largest world coordinate the solve touches: seed translations plus the
    // mate geometry's local points (which, for components imported from a shared
    // STEP frame, ARE the assembly's coordinates). Everything scale-dependent below
    // is derived from it, and it stays 100 mm for a table-top assembly so small
    // models behave exactly as before.
    double L = 0.0;
    for (const auto& cid : frame_order)
        L = std::max(L, frames[cid].t0.cwiseAbs().maxCoeff());
    for (const auto& kv : resolved_geometry)
        L = std::max(L, kv.second.point.cwiseAbs().maxCoeff());
    const double scale = std::max(100.0, L);

    // A picometre is not a reachable residual across a 2 m assembly: one residual
    // evaluation of `n . (pb - pa)` at |p| ~ 2000 mm already carries ~1e-12 mm of
    // cancellation noise, and the LM then reports a perfectly seated network as
    // "did not converge". Widen the threshold with the model, never below `tol` —
    // and never past kMaxAbsTolMm, because the positional rows are LENGTHS IN MM and
    // no assembly size makes a nanometre of misplacement a converged mate. Without the
    // ceiling the threshold grows without bound with the model, so a big enough
    // assembly would accept a visible gap as "seated"; a test rover's own
    // ~0.046 mm micro-pose is 46 000 times this ceiling.
    constexpr double kMaxAbsTolMm = 1e-6;
    const double eff_tol = std::min(tol * (scale / 100.0), std::max(tol, kMaxAbsTolMm));

    // Trust region: a single LM iteration may move a component by a quarter of the
    // assembly (never less than 25 mm, so a small model can still be dragged across
    // itself in a few iterations) and may turn it by half a radian. The step direction
    // is untouched — only its length is bounded — so this costs iterations, never
    // solutions, and it is what stops a near-null least-squares direction from
    // launching a component thousands of mm on a step that lowers the cost by a hair.
    if (system.n_vars() > 0) {
        VectorXd limits = VectorXd::Zero(system.n_vars());
        const double t_cap = std::max(25.0, 0.25 * scale);
        for (const auto& cid : frame_order) {
            const CompFrame& f = frames[cid];
            if (f.fixed) continue;
            for (int k = 0; k < 3; ++k) {
                limits(f.t_idx[k]) = t_cap;
                limits(f.w_idx[k]) = 0.5;
            }
        }
        system.set_step_limits(limits);
    }

    VectorXd x;
    bool res_converged = true;
    double max_res = 0.0;
    if (system.n_vars() == 0) {
        x = VectorXd::Zero(0);
    } else {
        auto res = system.solve(std::nullopt, eff_tol, max_iter, time_budget_ms);
        x = res.x;
        res_converged = res.converged;
        max_res = res.max_residual;
        bool any_free = false;
        for (auto& kv : frames) if (!kv.second.fixed) any_free = true;
        if (!res_converged && !drag_opt && !time_budget_ms && any_free) {
            // Antipodal-start escape (port of mate_solver.py): perturb the rotational DOF and
            // re-solve. The oracle draws perturbations from numpy's default_rng(12345).normal(
            // scale=0.6); to land on the SAME random restarts (and so the SAME basin) we must
            // reproduce that stream bit-for-bit — std::mt19937_64 is a DIFFERENT generator and
            // probes different points, so a coupled rotational chain that the oracle rescues
            // here would otherwise stall. chudware::sim::NumpyGenerator is the validated clone.
            //
            // TWO rules make this an escape instead of a wrecking ball. (1) SCOPE: only
            // components in a mate-connected group that actually has an unsatisfied mate are
            // perturbed. A four-corner rover is four independent chains off the fixed frame;
            // one unsatisfiable mate on one corner used to randomize the rotation of every
            // free component in the document. (2) ACCEPTANCE: a restart replaces the incumbent
            // only when it converges or at least halves the residual. A basin escape improves
            // by orders of magnitude; anything less is noise, and taking it swapped a pose
            // seated on the authored transforms for a scrambled one at the SAME residual.
            const std::set<std::string> failing = failing_groups(builders, frame_order, x, eff_tol);
            chudware::sim::NumpyGenerator rng(12345);
            VectorXd x0_base = system.init_vector();
            std::vector<int> w_indices;
            for (auto& cid : frame_order) {
                CompFrame& f = frames[cid];
                if (!f.fixed && failing.count(cid))
                    w_indices.insert(w_indices.end(), f.w_idx.begin(), f.w_idx.end());
            }
            if (!w_indices.empty()) {
                auto best = res;
                for (int iter = 0; iter < 6; ++iter) {
                    VectorXd x0 = x0_base;
                    // numpy rng.normal(scale=0.6, size=N) = standard_normal() * 0.6, in order.
                    for (int wi : w_indices) x0(wi) += 0.6 * rng.standard_normal();
                    auto r2 = system.solve(x0, eff_tol, max_iter);
                    if (r2.converged || r2.max_residual < 0.5 * best.max_residual) best = r2;
                    if (best.converged) break;
                }
                x = best.x;
                res_converged = best.converged;
                max_res = best.max_residual;
            }
        }
    }

    MateSolveResult out;
    for (auto& cid : frame_order) {
        CompFrame& frame = frames[cid];
        if (frame.fixed) {
            // Bit-identical echo. Re-deriving the anchor's matrix from (t,w) put the
            // grounded rover frame ~0.046 mm off its authored placement — the
            // exp-map round trip cannot represent a measured 3x3 block exactly, and a
            // fixed component is a constraint, not a 6-variable pose with a stiff spring.
            out.transforms[cid] = frame.tf0.size() == 16 ? frame.tf0 : kIdentity16;
            out.params[cid] = {frame.t0, frame.w0};
            continue;
        }
        Vector3d t(x(frame.t_idx[0]), x(frame.t_idx[1]), x(frame.t_idx[2]));
        Vector3d w(x(frame.w_idx[0]), x(frame.w_idx[1]), x(frame.w_idx[2]));
        out.transforms[cid] = params_to_transform(t, w);
        out.params[cid] = {t, w};
    }

    std::vector<std::string> redundant, conflicting;
    int dof = system.n_vars();
    if (system.n_vars() > 0 && !active_tags.empty()) {
        dof = system.degrees_of_freedom(x);
        classify_mates(builders, x, eff_tol, redundant, conflicting);
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
        if (status == "unresolved") res_converged = false;
        // Every "unresolved" record above stores its reason.
        out.mate_status.push_back({mid, mtype, status,
            status == "unresolved" ? unresolved_reasons[mid] : std::string()});
    }

    // Per-mate residual report, worst first. This is the diagnosis a caller needs when
    // `converged` is false: which mate is not satisfied, and by how much.
    for (const Builders& b : builders) {
        MateResidual mr;
        mr.mate_id = b.mid;
        mr.type = b.type;
        mate_residual_at(b, x, mr.max_abs, mr.norm);
        out.residuals.push_back(std::move(mr));
    }
    std::stable_sort(out.residuals.begin(), out.residuals.end(),
                     [](const MateResidual& a, const MateResidual& b) {
                         return a.max_abs > b.max_abs;
                     });
    if (!out.residuals.empty()) out.worst_mate = out.residuals.front().mate_id;

    out.over_defined = !conflicting.empty();
    out.dof = dof;
    out.converged = res_converged;
    out.max_residual = max_res;
    out.tolerance = eff_tol;
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
        statuses.push_back({{"mate_id", s.mate_id}, {"type", s.type}, {"status", s.status},
                            {"reason", s.reason}});
    out["mate_status"] = statuses;

    json residuals = json::array();
    for (const auto& r : res.residuals)
        residuals.push_back({{"mate_id", r.mate_id}, {"type", r.type},
                             {"max_abs", r.max_abs}, {"norm", r.norm}});
    out["residuals"] = residuals;
    out["worst_mate"] = res.worst_mate;
    out["tolerance"] = res.tolerance;

    out["dof"] = res.dof;
    out["converged"] = res.converged;
    out["max_residual"] = res.max_residual;
    out["over_defined"] = res.over_defined;
    out["summary"] = res.summary;
    return out;
}

}  // namespace chudware::mate
