// Rigid-body inverse dynamics on a prescribed trajectory — see motion_dynamics.h.
#include "doc/motion_dynamics.h"

#include "mate/mate.h"  // mat_to_rotvec / unit (OCCT-free rotation helpers)

#include <algorithm>
#include <cmath>
#include <set>

namespace chudware::doc::motiondyn {

namespace {

using Eigen::Matrix3d;
using Eigen::Matrix4d;
using Eigen::Vector3d;

// One basis wrench a joint can transmit: unit force dir + unit couple dir
// (either may be zero). Force acts at the joint anchor.
struct WrenchCol {
    Vector3d f = Vector3d::Zero();
    Vector3d m = Vector3d::Zero();
};

// Two unit vectors ⊥ a (and each other).
void perp_pair(const Vector3d& a, Vector3d& u, Vector3d& v) {
    Vector3d seed = std::abs(a.x()) < 0.9 ? Vector3d::UnitX() : Vector3d::UnitY();
    u = a.cross(seed).normalized();
    v = a.cross(u).normalized();
}

// The transmitted-wrench subspace of each joint type — the columns of the mate
// constraint jacobian transpose, written out per type. `axis` is the joint's
// world axis at this frame.
std::vector<WrenchCol> wrench_basis(const std::string& type, const Vector3d& axis) {
    Vector3d a = axis.norm() > 1e-12 ? axis.normalized() : Vector3d::UnitZ();
    Vector3d u, v;
    perp_pair(a, u, v);
    const Vector3d ex = Vector3d::UnitX(), ey = Vector3d::UnitY(), ez = Vector3d::UnitZ();
    std::vector<WrenchCol> cols;
    auto F = [&](const Vector3d& d) { WrenchCol c; c.f = d; cols.push_back(c); };
    auto M = [&](const Vector3d& d) { WrenchCol c; c.m = d; cols.push_back(c); };

    if (type == "revolute") {            // pin: all forces, no moment about axis
        F(ex); F(ey); F(ez); M(u); M(v);
    } else if (type == "cylindrical") {  // free to slide + spin along axis
        F(u); F(v); M(u); M(v);
    } else if (type == "prismatic") {    // slider: no force along axis
        F(u); F(v); M(ex); M(ey); M(ez);
    } else if (type == "spherical") {    // ball: forces only
        F(ex); F(ey); F(ez);
    } else if (type == "planar") {       // plane contact: normal force + tilt moments
        F(a); M(u); M(v);
    } else if (type == "parallel") {     // two independent orientation constraints
        M(u); M(v);
    } else if (type == "perpendicular" || type == "drive_moment") {
        M(a);                            // one scalar orientation/driver constraint
    } else if (type == "contact" || type == "drive_force") {
        F(a);                            // one scalar contact/driver constraint
    } else if (type == "fixed") {
        F(ex); F(ey); F(ez); M(ex); M(ey); M(ez);
    }
    return cols;
}

Matrix3d rot_of(const Matrix4d& T) { return T.block<3, 3>(0, 0); }
Vector3d trans_of(const Matrix4d& T) { return T.block<3, 1>(0, 3); }

bool known_joint_type(const std::string& type) {
    return type == "revolute" || type == "cylindrical" || type == "prismatic" ||
           type == "spherical" || type == "planar" || type == "parallel" ||
           type == "perpendicular" || type == "contact" || type == "fixed" ||
           type == "drive_moment" || type == "drive_force";
}

bool axis_required(const std::string& type) {
    return type != "spherical" && type != "fixed";
}

bool valid_rotation(const Matrix3d& R) {
    if (!R.allFinite()) return false;
    return (R.transpose() * R - Matrix3d::Identity()).norm() <= 1e-7 &&
           std::abs(R.determinant() - 1.0) <= 1e-7;
}

}  // namespace

DynResult solve_inverse_dynamics(const std::vector<BodyProps>& bodies,
                                 const std::vector<JointDef>& joints,
                                 const Options& opt) {
    DynResult out;
    auto reject = [&](const std::string& why) -> DynResult {
        out.frames.clear();
        out.warnings.push_back(why);
        out.ok = false;
        return out;
    };

    const int nb = static_cast<int>(bodies.size());
    if (nb == 0) return reject("no bodies");
    if (!std::isfinite(opt.dt) || opt.dt <= 0.0)
        return reject("dt must be finite and > 0 s");
    if (!opt.gravity.allFinite())
        return reject("gravity must contain three finite values in m/s^2");
    if (!std::isfinite(opt.damping_lin) || opt.damping_lin < 0.0 ||
        !std::isfinite(opt.damping_ang) || opt.damping_ang < 0.0)
        return reject("damping coefficients must be finite and non-negative");

    const int nf = static_cast<int>(bodies[0].poses.size());
    if (nf < 3) return reject("need >= 3 frames for acceleration-based dynamics");

    std::set<std::string> body_ids;
    for (const BodyProps& b : bodies) {
        if (b.id.empty() || !body_ids.insert(b.id).second)
            return reject("body ids must be non-empty and unique");
        if (static_cast<int>(b.poses.size()) != nf)
            return reject("body '" + b.id + "' pose count mismatch");
        if (!std::isfinite(b.mass_kg) || b.mass_kg < 0.0)
            return reject("body '" + b.id + "' mass must be finite and non-negative kg");
        if (!b.com_local.allFinite() || !b.inertia_local.allFinite())
            return reject("body '" + b.id + "' mass properties contain non-finite values");
        const double iscale = std::max(1.0, b.inertia_local.norm());
        if ((b.inertia_local - b.inertia_local.transpose()).norm() > 1e-10 * iscale)
            return reject("body '" + b.id + "' inertia tensor must be symmetric");
        Eigen::SelfAdjointEigenSolver<Matrix3d> ies(
            0.5 * (b.inertia_local + b.inertia_local.transpose()));
        if (ies.info() != Eigen::Success || ies.eigenvalues().minCoeff() < -1e-12 * iscale)
            return reject("body '" + b.id + "' inertia tensor must be positive semidefinite");
        const Vector3d iv = ies.eigenvalues().cwiseMax(0.0);
        if (iv[0] + iv[1] + 1e-12 * iscale < iv[2])
            return reject("body '" + b.id + "' inertia principal moments are non-physical");
        if (b.mass_kg == 0.0 && b.inertia_local.norm() > 1e-14)
            return reject("body '" + b.id + "' has inertia but zero mass");
        for (int f = 0; f < nf; ++f) {
            const Matrix4d& T = b.poses[static_cast<size_t>(f)];
            if (!T.allFinite())
                return reject("body '" + b.id + "' pose contains non-finite values");
            if ((T.row(3) - Eigen::RowVector4d(0, 0, 0, 1)).norm() > 1e-9 ||
                !valid_rotation(rot_of(T)))
                return reject("body '" + b.id + "' pose is not a rigid transform");
            if (f > 0) {
                const double da = chudware::mate::mat_to_rotvec(
                    rot_of(T) * rot_of(b.poses[static_cast<size_t>(f - 1)]).transpose()).norm();
                if (!std::isfinite(da) || da >= 3.14159265358979323846 - 1e-6)
                    return reject("body '" + b.id +
                                  "' has an ambiguous >=180 deg rotation between frames");
            }
        }
    }

    std::set<std::string> joint_ids;
    for (const JointDef& j : joints) {
        if (j.id.empty() || !joint_ids.insert(j.id).second)
            return reject("joint ids must be non-empty and unique");
        if (!known_joint_type(j.type))
            return reject("joint '" + j.id + "' has unsupported type '" + j.type + "'");
        auto valid_body = [&](int i) { return i >= -1 && i < nb; };
        if (!valid_body(j.body_a) || !valid_body(j.body_b) ||
            (j.body_a == j.body_b && j.body_a >= 0) ||
            (j.body_a < 0 && j.body_b < 0))
            return reject("joint '" + j.id + "' has invalid body endpoints");
        if (!j.anchor_local.allFinite() ||
            (j.has_anchor_b && !j.anchor_b_local.allFinite()))
            return reject("joint '" + j.id + "' anchor contains non-finite values");
        if (!j.axis_world_per_frame.empty()) {
            if (static_cast<int>(j.axis_world_per_frame.size()) != nf)
                return reject("joint '" + j.id + "' per-frame axis count mismatch");
            for (const Vector3d& axis : j.axis_world_per_frame)
                if (!axis.allFinite() || axis.norm() <= 1e-12)
                    return reject("joint '" + j.id +
                                  "' per-frame axes must be finite and non-zero");
        } else if (axis_required(j.type) &&
                   (!j.axis_local.allFinite() || j.axis_local.norm() <= 1e-12)) {
            return reject("joint '" + j.id + "' axis must be finite and non-zero");
        }
    }

    for (const AppliedLoad& load : opt.loads) {
        if (load.body < 0 || load.body >= nb)
            return reject("applied load references an invalid body");
        if (!load.force_n.allFinite() || !load.torque_nm.allFinite())
            return reject("applied loads must contain finite N and N*m vectors");
        if (bodies[static_cast<size_t>(load.body)].fixed)
            return reject("applied load targets a fixed body");
    }

    // ---- kinematic differentiation ------------------------------------------
    // Translational derivatives use second-order central/interior and one-sided
    // boundary stencils. Angular derivatives are built from interval rotation
    // logarithms, avoiding a global rotation-vector branch cut.
    std::vector<std::vector<Vector3d>> p(nb), vel(nb), acc(nb), omg(nb), alp(nb);
    std::vector<std::vector<Matrix3d>> R(nb);
    for (int b = 0; b < nb; ++b) {
        p[b].resize(nf);
        R[b].resize(nf);
        vel[b].assign(nf, Vector3d::Zero());
        acc[b].assign(nf, Vector3d::Zero());
        omg[b].assign(nf, Vector3d::Zero());
        alp[b].assign(nf, Vector3d::Zero());
        for (int f = 0; f < nf; ++f) {
            R[b][f] = rot_of(bodies[b].poses[static_cast<size_t>(f)]);
            p[b][f] = R[b][f] * bodies[b].com_local +
                      trans_of(bodies[b].poses[static_cast<size_t>(f)]);
        }
        if (bodies[b].fixed) continue;

        const double dt = opt.dt;
        for (int f = 1; f + 1 < nf; ++f) {
            vel[b][f] = (p[b][f + 1] - p[b][f - 1]) / (2.0 * dt);
            acc[b][f] = (p[b][f + 1] - 2.0 * p[b][f] + p[b][f - 1]) / (dt * dt);
        }
        vel[b][0] = (-3.0 * p[b][0] + 4.0 * p[b][1] - p[b][2]) / (2.0 * dt);
        vel[b][nf - 1] =
            (3.0 * p[b][nf - 1] - 4.0 * p[b][nf - 2] + p[b][nf - 3]) / (2.0 * dt);
        if (nf == 3) {
            const Vector3d a = (p[b][2] - 2.0 * p[b][1] + p[b][0]) / (dt * dt);
            acc[b][0] = a;
            acc[b][2] = a;
        } else {
            acc[b][0] = (2.0 * p[b][0] - 5.0 * p[b][1] + 4.0 * p[b][2] - p[b][3]) /
                        (dt * dt);
            acc[b][nf - 1] =
                (2.0 * p[b][nf - 1] - 5.0 * p[b][nf - 2] +
                 4.0 * p[b][nf - 3] - p[b][nf - 4]) / (dt * dt);
        }

        std::vector<Vector3d> wi(static_cast<size_t>(nf - 1), Vector3d::Zero());
        for (int f = 0; f + 1 < nf; ++f)
            wi[static_cast<size_t>(f)] = chudware::mate::mat_to_rotvec(
                R[b][f + 1] * R[b][f].transpose()) / dt;
        omg[b][0] = 1.5 * wi[0] - 0.5 * wi[1];
        for (int f = 1; f + 1 < nf; ++f)
            omg[b][f] = 0.5 * (wi[static_cast<size_t>(f - 1)] +
                               wi[static_cast<size_t>(f)]);
        omg[b][nf - 1] = 1.5 * wi[static_cast<size_t>(nf - 2)] -
                          0.5 * wi[static_cast<size_t>(nf - 3)];
        for (int f = 1; f + 1 < nf; ++f)
            alp[b][f] = (wi[static_cast<size_t>(f)] -
                         wi[static_cast<size_t>(f - 1)]) / dt;
        if (nf == 3) {
            alp[b][0] = alp[b][1];
            alp[b][2] = alp[b][1];
        } else {
            alp[b][0] = (-2.0 * wi[0] + 3.0 * wi[1] - wi[2]) / dt;
            alp[b][nf - 1] =
                (2.0 * wi[static_cast<size_t>(nf - 2)] -
                 3.0 * wi[static_cast<size_t>(nf - 3)] +
                 wi[static_cast<size_t>(nf - 4)]) / dt;
        }
    }

    // ---- equation rows: 6 per non-fixed body ---------------------------------
    std::vector<int> row_of(nb, -1);
    int nrows = 0;
    for (int b = 0; b < nb; ++b)
        if (!bodies[b].fixed) { row_of[b] = nrows; nrows += 6; }
    if (nrows == 0) return reject("all bodies fixed — nothing to solve");

    // ---- column layout: one coefficient per joint basis wrench ---------------
    std::vector<int> col_of(joints.size(), 0), ncols_of(joints.size(), 0);
    int ncols = 0;
    for (size_t j = 0; j < joints.size(); ++j) {
        col_of[j] = ncols;
        ncols_of[j] = static_cast<int>(
            wrench_basis(joints[j].type, Vector3d::UnitZ()).size());
        ncols += ncols_of[j];
        const bool a_dynamic = joints[j].body_a >= 0 &&
                               !bodies[static_cast<size_t>(joints[j].body_a)].fixed;
        const bool b_dynamic = joints[j].body_b >= 0 &&
                               !bodies[static_cast<size_t>(joints[j].body_b)].fixed;
        if (!a_dynamic && !b_dynamic)
            out.warnings.push_back("joint '" + joints[j].id +
                                   "' connects no dynamic body and carries no solved reaction");
    }
    if (ncols == 0)
        out.warnings.push_back("no joints — only a zero-load free trajectory can balance");

    int unbalanced_count = 0;
    double max_force_residual = 0.0, max_moment_residual = 0.0;
    int first_unbalanced = -1;

    // ---- per-frame assembly + minimum-norm least squares ----------------------
    out.frames.resize(static_cast<size_t>(nf));
    for (int f = 0; f < nf; ++f) {
        FrameDyn& fd = out.frames[static_cast<size_t>(f)];
        fd.t = f * opt.dt;
        fd.vel.resize(static_cast<size_t>(nb));
        fd.omega.resize(static_cast<size_t>(nb));
        fd.accel.resize(static_cast<size_t>(nb));
        fd.alpha.resize(static_cast<size_t>(nb));
        for (int b = 0; b < nb; ++b) {
            fd.vel[static_cast<size_t>(b)] = vel[b][static_cast<size_t>(f)];
            fd.omega[static_cast<size_t>(b)] = omg[b][static_cast<size_t>(f)];
            fd.accel[static_cast<size_t>(b)] = acc[b][static_cast<size_t>(f)];
            fd.alpha[static_cast<size_t>(b)] = alp[b][static_cast<size_t>(f)];
        }
        fd.reactions.assign(joints.size(), JointReaction{});

        Eigen::MatrixXd A = Eigen::MatrixXd::Zero(nrows, ncols);
        Eigen::VectorXd rhs = Eigen::VectorXd::Zero(nrows);

        for (int b = 0; b < nb; ++b) {
            if (bodies[b].fixed) continue;
            const int r = row_of[b];
            const double m = bodies[b].mass_kg;
            const Matrix3d Iw = R[b][static_cast<size_t>(f)] * bodies[b].inertia_local *
                                R[b][static_cast<size_t>(f)].transpose();
            Vector3d Freq = m * (acc[b][static_cast<size_t>(f)] - opt.gravity) +
                            opt.damping_lin * vel[b][static_cast<size_t>(f)];
            Vector3d Mreq = Iw * alp[b][static_cast<size_t>(f)] +
                            omg[b][static_cast<size_t>(f)].cross(
                                Iw * omg[b][static_cast<size_t>(f)]) +
                            opt.damping_ang * omg[b][static_cast<size_t>(f)];
            for (const AppliedLoad& load : opt.loads)
                if (load.body == b) {
                    Freq -= load.force_n;
                    Mreq -= load.torque_nm;
                }
            rhs.segment<3>(r) = Freq;
            rhs.segment<3>(r + 3) = Mreq;
        }

        for (size_t j = 0; j < joints.size(); ++j) {
            const JointDef& joint = joints[j];
            Vector3d anchor_a = joint.anchor_local;
            Vector3d anchor_b = joint.anchor_b_local;
            Vector3d axis = joint.axis_local;
            if (joint.body_a >= 0) {
                const Matrix4d& Ta =
                    bodies[static_cast<size_t>(joint.body_a)].poses[static_cast<size_t>(f)];
                anchor_a = rot_of(Ta) * joint.anchor_local + trans_of(Ta);
                axis = rot_of(Ta) * joint.axis_local;
            }
            if (!joint.axis_world_per_frame.empty())
                axis = joint.axis_world_per_frame[static_cast<size_t>(f)];
            if (joint.has_anchor_b && joint.body_b >= 0) {
                const Matrix4d& Tb =
                    bodies[static_cast<size_t>(joint.body_b)].poses[static_cast<size_t>(f)];
                anchor_b = rot_of(Tb) * joint.anchor_b_local + trans_of(Tb);
            } else if (!joint.has_anchor_b) {
                // Backward-compatible single-anchor callers specify a world point
                // through body_a; both action/reaction forces act at that same point.
                anchor_b = anchor_a;
            }
            const std::vector<WrenchCol> basis = wrench_basis(joint.type, axis);
            for (int k = 0; k < static_cast<int>(basis.size()); ++k) {
                const int c = col_of[j] + k;
                auto add = [&](int body, double sign, const Vector3d& anchor) {
                    if (body < 0 || bodies[static_cast<size_t>(body)].fixed) return;
                    const int r = row_of[body];
                    A.block<3, 1>(r, c) += sign * basis[static_cast<size_t>(k)].f;
                    A.block<3, 1>(r + 3, c) +=
                        sign * (basis[static_cast<size_t>(k)].m +
                                (anchor - p[body][static_cast<size_t>(f)]).cross(
                                    basis[static_cast<size_t>(k)].f));
                };
                add(joint.body_b, +1.0, anchor_b);
                add(joint.body_a, -1.0, anchor_a);
            }
        }

        Eigen::VectorXd x = Eigen::VectorXd::Zero(ncols);
        if (ncols > 0) {
            Eigen::CompleteOrthogonalDecomposition<Eigen::MatrixXd> cod(A);
            x = cod.solve(rhs);
        }
        const Eigen::VectorXd err = A * x - rhs;
        fd.residual = err.norm();
        double rhs_force_sq = 0.0, rhs_moment_sq = 0.0;
        double err_force_sq = 0.0, err_moment_sq = 0.0;
        for (int b = 0; b < nb; ++b) {
            if (bodies[b].fixed) continue;
            const int r = row_of[b];
            rhs_force_sq += rhs.segment<3>(r).squaredNorm();
            rhs_moment_sq += rhs.segment<3>(r + 3).squaredNorm();
            err_force_sq += err.segment<3>(r).squaredNorm();
            err_moment_sq += err.segment<3>(r + 3).squaredNorm();
        }
        fd.force_residual_n = std::sqrt(err_force_sq);
        fd.moment_residual_nm = std::sqrt(err_moment_sq);
        const double force_tol = 1e-9 + 1e-6 * std::max(1.0, std::sqrt(rhs_force_sq));
        const double moment_tol = 1e-9 + 1e-6 * std::max(1.0, std::sqrt(rhs_moment_sq));
        fd.balanced = fd.force_residual_n <= force_tol &&
                      fd.moment_residual_nm <= moment_tol;
        if (!fd.balanced) {
            if (first_unbalanced < 0) first_unbalanced = f;
            ++unbalanced_count;
            max_force_residual = std::max(max_force_residual, fd.force_residual_n);
            max_moment_residual = std::max(max_moment_residual, fd.moment_residual_nm);
        }

        for (size_t j = 0; j < joints.size(); ++j) {
            Vector3d axis = joints[j].axis_local;
            if (joints[j].body_a >= 0) {
                const Matrix4d& Ta =
                    bodies[static_cast<size_t>(joints[j].body_a)].poses[static_cast<size_t>(f)];
                axis = rot_of(Ta) * joints[j].axis_local;
            }
            if (!joints[j].axis_world_per_frame.empty())
                axis = joints[j].axis_world_per_frame[static_cast<size_t>(f)];
            const std::vector<WrenchCol> basis = wrench_basis(joints[j].type, axis);
            JointReaction& reaction = fd.reactions[j];
            for (int k = 0; k < static_cast<int>(basis.size()); ++k) {
                reaction.force_n += x[col_of[j] + k] * basis[static_cast<size_t>(k)].f;
                reaction.moment_nm += x[col_of[j] + k] * basis[static_cast<size_t>(k)].m;
            }
        }
    }

    if (unbalanced_count > 0) {
        out.warnings.push_back(
            std::to_string(unbalanced_count) + " of " + std::to_string(nf) +
            " frames have an unresolved force/moment path (first frame " +
            std::to_string(first_unbalanced) + ", max force residual " +
            std::to_string(max_force_residual) + " N, max moment residual " +
            std::to_string(max_moment_residual) + " N*m)");
    }
    out.ok = unbalanced_count == 0;
    return out;
}

}  // namespace chudware::doc::motiondyn
