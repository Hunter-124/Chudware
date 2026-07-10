// Rigid-body INVERSE dynamics on a prescribed motion trajectory (T-FIN20).
//
// doc_simulate_motion sweeps one driven mate and records every body's world pose
// per frame — pure kinematics. This module takes that trajectory (poses over time)
// plus mass properties and answers the DYNAMICS question: what joint reaction
// forces/moments must each mate transmit so the bodies actually follow that motion
// under gravity / applied loads / simple viscous damping?
//
// Method (classic prescribed-motion inverse dynamics):
//   1. Finite-difference each body's COM path and rotation history into
//      velocity / acceleration / angular velocity / angular acceleration.
//   2. Per frame, write Newton–Euler for every non-fixed body:
//        Σ F_joints + F_applied + m g − c_lin v = m a
//        Σ (M_joints + r×F_joints) + T_applied − c_ang ω = I_w α + ω × I_w ω
//   3. The joint wrenches are unknowns restricted to each mate's CONSTRAINED
//      directions (the transpose-jacobian column space of that mate type — a
//      revolute transmits 3 forces + 2 moments ⊥ its axis, a prismatic transmits
//      2 forces ⊥ its axis + 3 moments, a driver mate additionally transmits its
//      generalized drive force/torque along the driven direction, …).
//   4. Solve the assembled linear system per frame as a MINIMUM-NORM least-squares
//      problem (planar mechanisms modeled in 3D carry redundant out-of-plane
//      constraints; min-norm puts zero in the redundant null-space). The leftover
//      residual ‖Ax−b‖ is reported as a consistency/unbalance metric.
//
// Everything here is SI (m, kg, s, N, N·m) and OCCT-free (Eigen only) so the
// pendulum / slider-crank analytic V&V tests can drive it with synthetic
// trajectories (core/tests/test_motion_dynamics.cpp).
#pragma once

#include <Eigen/Dense>

#include <string>
#include <vector>

namespace aicad::doc::motiondyn {

// Per-body mass properties + its prescribed world pose per frame.
struct BodyProps {
    std::string id;                                        // component id
    double mass_kg = 0.0;
    Eigen::Vector3d com_local = Eigen::Vector3d::Zero();   // m, body local frame
    Eigen::Matrix3d inertia_local = Eigen::Matrix3d::Zero();  // kg·m² about COM, local frame
    bool fixed = false;                                    // grounded — no equations emitted
    std::vector<Eigen::Matrix4d> poses;                    // world 4×4 per frame, translation in m
};

// One joint (mate) between body_a and body_b (index into bodies; -1 = world).
// anchor/axis are expressed in body_a's LOCAL frame (world frame when body_a < 0),
// so they ride along with the mechanism as it moves.
//
// type ∈ { revolute, cylindrical, prismatic, spherical, planar, parallel, fixed,
//          drive_moment, drive_force } — see wrench_basis() in the .cpp for the
// exact transmitted-wrench subspace of each.
struct JointDef {
    std::string id;                                        // mate id
    std::string type = "fixed";
    int body_a = -1;
    int body_b = -1;
    Eigen::Vector3d anchor_local = Eigen::Vector3d::Zero();  // m
    Eigen::Vector3d axis_local = Eigen::Vector3d::UnitZ();   // unit
};

// Constant external load applied to one body (world frame, at its COM).
struct AppliedLoad {
    int body = -1;
    Eigen::Vector3d force_n = Eigen::Vector3d::Zero();
    Eigen::Vector3d torque_nm = Eigen::Vector3d::Zero();
};

struct Options {
    double dt = 1.0 / 24.0;                                 // s between frames
    Eigen::Vector3d gravity = Eigen::Vector3d(0, 0, -9.81); // m/s²
    double damping_lin = 0.0;                               // N·s/m   (F = −c v at COM)
    double damping_ang = 0.0;                               // N·m·s/rad (T = −c ω)
    std::vector<AppliedLoad> loads;
};

// The wrench joint j exerts ON body_b FROM body_a, world frame. `moment_nm` is the
// pure-couple part about the joint anchor (the r×F transport is inside the solve).
struct JointReaction {
    Eigen::Vector3d force_n = Eigen::Vector3d::Zero();
    Eigen::Vector3d moment_nm = Eigen::Vector3d::Zero();
};

struct FrameDyn {
    double t = 0.0;
    std::vector<JointReaction> reactions;   // per joint (joints order)
    std::vector<Eigen::Vector3d> vel;       // per body, COM m/s
    std::vector<Eigen::Vector3d> omega;     // rad/s
    std::vector<Eigen::Vector3d> accel;     // m/s²
    std::vector<Eigen::Vector3d> alpha;     // rad/s²
    double residual = 0.0;                  // ‖Ax−b‖ unbalance (N-ish mixed norm)
};

struct DynResult {
    std::vector<FrameDyn> frames;
    std::vector<std::string> warnings;
};

// Solve. All bodies must carry the same number of poses (≥ 3 for usable
// accelerations; 2 is accepted but ends copy their single interior estimate).
DynResult solve_inverse_dynamics(const std::vector<BodyProps>& bodies,
                                 const std::vector<JointDef>& joints,
                                 const Options& opt);

}  // namespace aicad::doc::motiondyn
