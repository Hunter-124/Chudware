// Rigid-body inverse dynamics on a prescribed trajectory — see motion_dynamics.h.
#include "doc/motion_dynamics.h"

#include "mate/mate.h"  // mat_to_rotvec / unit (OCCT-free rotation helpers)

#include <algorithm>
#include <cmath>

namespace aicad::doc::motiondyn {

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
    } else if (type == "parallel") {     // orientation-only mate: tilt moments
        M(u); M(v);
    } else if (type == "drive_moment") { // driver torque about the driven axis
        M(a);
    } else if (type == "drive_force") {  // driver force along the driven direction
        F(a);
    } else {                             // fixed / lock / unknown: full 6-wrench
        F(ex); F(ey); F(ez); M(ex); M(ey); M(ez);
    }
    return cols;
}

Matrix3d rot_of(const Matrix4d& T) { return T.block<3, 3>(0, 0); }
Vector3d trans_of(const Matrix4d& T) { return T.block<3, 1>(0, 3); }

}  // namespace

DynResult solve_inverse_dynamics(const std::vector<BodyProps>& bodies,
                                 const std::vector<JointDef>& joints,
                                 const Options& opt) {
    DynResult out;
    const int nb = (int)bodies.size();
    if (nb == 0) { out.warnings.push_back("no bodies"); return out; }
    const int nf = (int)bodies[0].poses.size();
    for (const BodyProps& b : bodies)
        if ((int)b.poses.size() != nf) {
            out.warnings.push_back("body '" + b.id + "' pose count mismatch — aborting dynamics");
            return out;
        }
    if (nf < 2) { out.warnings.push_back("need >= 2 frames for dynamics"); return out; }
    const double dt = opt.dt > 0 ? opt.dt : 1.0 / 24.0;

    // ---- kinematic differentiation ------------------------------------------
    // Per body: COM world path p[f], rotation R[f]; central differences inside,
    // one-sided at the ends; accelerations at the ends copy their neighbour
    // (a 3-point second difference does not exist at the boundary).
    std::vector<std::vector<Vector3d>> p(nb), vel(nb), acc(nb), omg(nb), alp(nb);
    std::vector<std::vector<Matrix3d>> R(nb);
    for (int b = 0; b < nb; ++b) {
        p[b].resize(nf); R[b].resize(nf);
        vel[b].assign(nf, Vector3d::Zero());
        acc[b].assign(nf, Vector3d::Zero());
        omg[b].assign(nf, Vector3d::Zero());
        alp[b].assign(nf, Vector3d::Zero());
        for (int f = 0; f < nf; ++f) {
            R[b][f] = rot_of(bodies[b].poses[f]);
            p[b][f] = R[b][f] * bodies[b].com_local + trans_of(bodies[b].poses[f]);
        }
        if (bodies[b].fixed) continue;
        for (int f = 0; f < nf; ++f) {
            const int lo = std::max(0, f - 1), hi = std::min(nf - 1, f + 1);
            const double span = (hi - lo) * dt;
            vel[b][f] = (p[b][hi] - p[b][lo]) / span;
            omg[b][f] = aicad::mate::mat_to_rotvec(R[b][hi] * R[b][lo].transpose()) / span;
        }
        for (int f = 1; f + 1 < nf; ++f)
            acc[b][f] = (p[b][f + 1] - 2.0 * p[b][f] + p[b][f - 1]) / (dt * dt);
        if (nf >= 3) { acc[b][0] = acc[b][1]; acc[b][nf - 1] = acc[b][nf - 2]; }
        for (int f = 0; f < nf; ++f) {
            const int lo = std::max(0, f - 1), hi = std::min(nf - 1, f + 1);
            alp[b][f] = (omg[b][hi] - omg[b][lo]) / ((hi - lo) * dt);
        }
    }

    // ---- equation rows: 6 per non-fixed body ---------------------------------
    std::vector<int> row_of(nb, -1);
    int nrows = 0;
    for (int b = 0; b < nb; ++b)
        if (!bodies[b].fixed) { row_of[b] = nrows; nrows += 6; }
    if (nrows == 0) { out.warnings.push_back("all bodies fixed — nothing to solve"); return out; }

    // ---- column layout: one coefficient per joint basis wrench ---------------
    // (basis geometry is frame-dependent; the LAYOUT — counts per type — is not)
    std::vector<int> col_of(joints.size(), 0), ncols_of(joints.size(), 0);
    int ncols = 0;
    for (size_t j = 0; j < joints.size(); ++j) {
        col_of[j] = ncols;
        ncols_of[j] = (int)wrench_basis(joints[j].type, Vector3d::UnitZ()).size();
        ncols += ncols_of[j];
    }
    if (ncols == 0) out.warnings.push_back("no joints — reporting accelerations only");

    // ---- per-frame assembly + min-norm least squares --------------------------
    out.frames.resize((size_t)nf);
    for (int f = 0; f < nf; ++f) {
        FrameDyn& fd = out.frames[(size_t)f];
        fd.t = f * dt;
        fd.vel.resize((size_t)nb); fd.omega.resize((size_t)nb);
        fd.accel.resize((size_t)nb); fd.alpha.resize((size_t)nb);
        for (int b = 0; b < nb; ++b) {
            fd.vel[(size_t)b] = vel[b][(size_t)f];   fd.omega[(size_t)b] = omg[b][(size_t)f];
            fd.accel[(size_t)b] = acc[b][(size_t)f]; fd.alpha[(size_t)b] = alp[b][(size_t)f];
        }
        fd.reactions.assign(joints.size(), JointReaction{});

        Eigen::MatrixXd A = Eigen::MatrixXd::Zero(nrows, std::max(1, ncols));
        Eigen::VectorXd rhs = Eigen::VectorXd::Zero(nrows);

        // b-vector: required net wrench minus known external loads.
        for (int b = 0; b < nb; ++b) {
            if (bodies[b].fixed) continue;
            const int r = row_of[b];
            const double m = bodies[b].mass_kg;
            const Matrix3d Iw = R[b][(size_t)f] * bodies[b].inertia_local *
                                R[b][(size_t)f].transpose();
            Vector3d Freq = m * (acc[b][(size_t)f] - opt.gravity) +
                            opt.damping_lin * vel[b][(size_t)f];
            Vector3d Mreq = Iw * alp[b][(size_t)f] +
                            omg[b][(size_t)f].cross(Iw * omg[b][(size_t)f]) +
                            opt.damping_ang * omg[b][(size_t)f];
            for (const AppliedLoad& L : opt.loads)
                if (L.body == b) { Freq -= L.force_n; Mreq -= L.torque_nm; }
            rhs.segment<3>(r) = Freq;
            rhs.segment<3>(r + 3) = Mreq;
        }

        // A columns: each joint's basis wrench, +on body_b / −on body_a.
        for (size_t j = 0; j < joints.size(); ++j) {
            const JointDef& J = joints[j];
            Vector3d anchor = J.anchor_local, axis = J.axis_local;
            if (J.body_a >= 0 && J.body_a < nb) {
                const Matrix4d& Ta = bodies[(size_t)J.body_a].poses[(size_t)f];
                anchor = rot_of(Ta) * J.anchor_local + trans_of(Ta);
                axis = rot_of(Ta) * J.axis_local;
            }
            const std::vector<WrenchCol> basis = wrench_basis(J.type, axis);
            for (int k = 0; k < (int)basis.size(); ++k) {
                const int c = col_of[j] + k;
                auto add = [&](int body, double sgn) {
                    if (body < 0 || body >= nb || bodies[(size_t)body].fixed) return;
                    const int r = row_of[body];
                    A.block<3, 1>(r, c) += sgn * basis[(size_t)k].f;
                    A.block<3, 1>(r + 3, c) +=
                        sgn * (basis[(size_t)k].m +
                               (anchor - p[body][(size_t)f]).cross(basis[(size_t)k].f));
                };
                add(J.body_b, +1.0);
                add(J.body_a, -1.0);
            }
        }

        Eigen::VectorXd x = Eigen::VectorXd::Zero(std::max(1, ncols));
        if (ncols > 0) {
            Eigen::CompleteOrthogonalDecomposition<Eigen::MatrixXd> cod(A);
            x = cod.solve(rhs);  // minimum-norm least squares
        }
        fd.residual = (A * x - rhs).norm();

        for (size_t j = 0; j < joints.size(); ++j) {
            Vector3d anchor = joints[j].anchor_local, axis = joints[j].axis_local;
            if (joints[j].body_a >= 0 && joints[j].body_a < nb) {
                const Matrix4d& Ta = bodies[(size_t)joints[j].body_a].poses[(size_t)f];
                anchor = rot_of(Ta) * joints[j].anchor_local + trans_of(Ta);
                axis = rot_of(Ta) * joints[j].axis_local;
            }
            const std::vector<WrenchCol> basis = wrench_basis(joints[j].type, axis);
            JointReaction& jr = fd.reactions[j];
            for (int k = 0; k < (int)basis.size(); ++k) {
                jr.force_n += x[col_of[j] + k] * basis[(size_t)k].f;
                jr.moment_nm += x[col_of[j] + k] * basis[(size_t)k].m;
            }
        }
    }
    return out;
}

}  // namespace aicad::doc::motiondyn
