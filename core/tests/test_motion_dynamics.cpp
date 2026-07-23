// Analytic V&V for the motion inverse-dynamics module (T-FIN20).
//
// Two closed-form mechanics cross-checks on SYNTHETIC prescribed trajectories
// (no OCCT, no doc session — this exercises exactly the math that
// doc_simulate_motion's dynamics pass runs on real assemblies):
//
//   1. PENDULUM — a point-mass body on a rigid arm about a fixed pin, both a
//      uniform-rotation case (pure centripetal) and an oscillating case
//      (tangential + centripetal + varying drive torque). The pin reaction must
//      equal m(a − g) and the driver torque (I_com + m r²)·θ̈ + m·g·r·cosθ.
//
//   2. SLIDER-CRANK — massless crank + massless rod driving a slider, gravity
//      off. The rod is a two-force member: the rod-slider pin reaction must be
//      ALONG the rod with x-component m_s·ẍ, the prismatic guide takes the rod's
//      lateral component, and the crank drive torque must satisfy the power
//      balance τ·Ω = m_s·ẍ·ẋ.
//
// Tolerances reflect the trajectory finite-differencing (O(dt²)) — the solver
// itself is exact linear algebra per frame.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
// MSVC only exposes M_PI from <cmath> when _USE_MATH_DEFINES is set (before the include).
#define _USE_MATH_DEFINES
#include <doctest/doctest.h>

#include <cmath>
#include <vector>

#include "doc/motion_dynamics.h"

namespace md = chudware::doc::motiondyn;
using Eigen::Matrix3d;
using Eigen::Matrix4d;
using Eigen::Vector3d;

namespace {

Matrix4d pose(const Matrix3d& R, const Vector3d& t) {
    Matrix4d T = Matrix4d::Identity();
    T.block<3, 3>(0, 0) = R;
    T.block<3, 1>(0, 3) = t;
    return T;
}

Matrix3d rot_z(double a) {
    return Eigen::AngleAxisd(a, Vector3d::UnitZ()).toRotationMatrix();
}

}  // namespace

// ---------------------------------------------------------------------------
TEST_CASE("pendulum: uniform rotation — centripetal pin reaction + gravity torque") {
    // m = 2 kg on r = 0.5 m arm, spinning at Ω = 2 rad/s in the XY plane,
    // gravity −Y. Body frame: origin at the COM, axes rotating with the arm.
    const double m = 2.0, r = 0.5, W = 2.0, g = 9.81;
    const int nf = 400;
    const double dt = 2.0 * M_PI / W / (nf - 1);

    md::BodyProps body;
    body.id = "arm";
    body.mass_kg = m;
    body.com_local = Vector3d::Zero();
    body.inertia_local = 0.02 * Matrix3d::Identity();
    for (int f = 0; f < nf; ++f) {
        const double th = W * f * dt;
        body.poses.push_back(pose(rot_z(th), r * Vector3d(std::cos(th), std::sin(th), 0)));
    }

    md::JointDef pin;    // ground revolute at the origin, axis Z
    pin.id = "pin"; pin.type = "revolute";
    pin.body_a = -1; pin.body_b = 0;
    pin.anchor_local = Vector3d::Zero(); pin.axis_local = Vector3d::UnitZ();

    md::JointDef drv;    // the driven angle mate: torque about Z
    drv.id = "drive"; drv.type = "drive_moment";
    drv.body_a = -1; drv.body_b = 0;
    drv.anchor_local = Vector3d::Zero(); drv.axis_local = Vector3d::UnitZ();

    md::Options opt;
    opt.dt = dt;
    opt.gravity = Vector3d(0, -g, 0);

    md::DynResult res = md::solve_inverse_dynamics({body}, {pin, drv}, opt);
    REQUIRE((int)res.frames.size() == nf);

    for (int f = 4; f < nf - 4; f += 25) {
        const double th = W * f * dt;
        const Vector3d p = r * Vector3d(std::cos(th), std::sin(th), 0);
        // F_pin = m(a − g),  a = −Ω² p (pure centripetal)
        const Vector3d expF = m * (-W * W * p) + Vector3d(0, m * g, 0);
        const md::JointReaction& pinr = res.frames[(size_t)f].reactions[0];
        const md::JointReaction& drvr = res.frames[(size_t)f].reactions[1];
        CAPTURE(f);
        CHECK((pinr.force_n - expF).norm() < 0.01 * (expF.norm() + 1.0));
        // Driver torque holds the arm against gravity: τ = m g r cosθ
        CHECK(std::abs(drvr.moment_nm.z() - m * g * r * std::cos(th)) <
              0.01 * (m * g * r + 1.0));
        // The pin transmits no moment about its own axis.
        CHECK(std::abs(pinr.moment_nm.z()) < 1e-6);
        // System is determinate in-plane: no unbalance left.
        CHECK(res.frames[(size_t)f].residual < 1e-6);
    }
}

// ---------------------------------------------------------------------------
TEST_CASE("pendulum: oscillating — tangential terms + full drive-torque law") {
    // θ(t) = θ0 sin(Ωt): non-zero θ̈, so the reaction picks up the tangential
    // component and the drive torque the inertial term (I_com + m r²)θ̈.
    const double m = 1.5, r = 0.4, W = 3.0, th0 = 0.6, g = 9.81, Icom = 0.05;
    const int nf = 600;
    const double dt = 2.0 * M_PI / W / (nf - 1);

    md::BodyProps body;
    body.id = "arm";
    body.mass_kg = m;
    body.com_local = Vector3d::Zero();
    body.inertia_local = Icom * Matrix3d::Identity();
    for (int f = 0; f < nf; ++f) {
        const double th = th0 * std::sin(W * f * dt);
        body.poses.push_back(pose(rot_z(th), r * Vector3d(std::cos(th), std::sin(th), 0)));
    }

    md::JointDef pin;
    pin.id = "pin"; pin.type = "revolute";
    pin.body_a = -1; pin.body_b = 0;
    pin.anchor_local = Vector3d::Zero(); pin.axis_local = Vector3d::UnitZ();
    md::JointDef drv = pin;
    drv.id = "drive"; drv.type = "drive_moment";

    md::Options opt;
    opt.dt = dt;
    opt.gravity = Vector3d(0, -g, 0);

    md::DynResult res = md::solve_inverse_dynamics({body}, {pin, drv}, opt);
    REQUIRE((int)res.frames.size() == nf);

    for (int f = 6; f < nf - 6; f += 37) {
        const double t = f * dt;
        const double th  = th0 * std::sin(W * t);
        const double thd = th0 * W * std::cos(W * t);
        const double thdd = -th0 * W * W * std::sin(W * t);
        const double c = std::cos(th), s = std::sin(th);
        // a = r(−θ̈ s − θ̇² c,  θ̈ c − θ̇² s, 0)
        const Vector3d a = r * Vector3d(-thdd * s - thd * thd * c,
                                        thdd * c - thd * thd * s, 0);
        const Vector3d expF = m * a + Vector3d(0, m * g, 0);
        const double expT = (Icom + m * r * r) * thdd + m * g * r * c;
        const md::JointReaction& pinr = res.frames[(size_t)f].reactions[0];
        const md::JointReaction& drvr = res.frames[(size_t)f].reactions[1];
        CAPTURE(f);
        CHECK((pinr.force_n - expF).norm() < 0.02 * (expF.norm() + 1.0));
        CHECK(std::abs(drvr.moment_nm.z() - expT) <
              0.02 * (std::abs(expT) + m * g * r));
    }
}

// ---------------------------------------------------------------------------
TEST_CASE("slider-crank: rod-pin reaction along the rod, guide normal, drive power") {
    // Crank r = 0.1 m at Ω = 2 rad/s, rod l = 0.3 m, slider m_s = 0.5 kg on the
    // X axis. Crank + rod massless, gravity off — the only inertial load is the
    // slider, so every reaction has a closed form.
    const double r = 0.1, l = 0.3, W = 2.0, ms = 0.5;
    const int nf = 720;
    const double dt = 2.0 * M_PI / W / (nf - 1);

    auto xB = [&](double th) {
        const double s = std::sin(th);
        return r * std::cos(th) + std::sqrt(l * l - r * r * s * s);
    };

    md::BodyProps crank;   // frame rotates with the crank, origin at the pivot
    crank.id = "crank"; crank.mass_kg = 0.0;
    crank.com_local = Vector3d(r / 2, 0, 0);
    crank.inertia_local = Matrix3d::Zero();
    md::BodyProps rod;     // origin at pin A, local +X toward pin B
    rod.id = "rod"; rod.mass_kg = 0.0;
    rod.com_local = Vector3d(l / 2, 0, 0);
    rod.inertia_local = Matrix3d::Zero();
    md::BodyProps slider;  // origin at pin B
    slider.id = "slider"; slider.mass_kg = ms;
    slider.com_local = Vector3d::Zero();
    slider.inertia_local = 1e-4 * Matrix3d::Identity();

    for (int f = 0; f < nf; ++f) {
        const double th = W * f * dt;
        const Vector3d A(r * std::cos(th), r * std::sin(th), 0);
        const Vector3d B(xB(th), 0, 0);
        const Vector3d u = (B - A);
        crank.poses.push_back(pose(rot_z(th), Vector3d::Zero()));
        rod.poses.push_back(pose(rot_z(std::atan2(u.y(), u.x())), A));
        slider.poses.push_back(pose(Matrix3d::Identity(), B));
    }

    auto rev = [](const char* id, int a, int b, const Vector3d& anchor) {
        md::JointDef j; j.id = id; j.type = "revolute";
        j.body_a = a; j.body_b = b; j.anchor_local = anchor;
        j.axis_local = Vector3d::UnitZ();
        return j;
    };
    md::JointDef pivot = rev("pivot", -1, 0, Vector3d::Zero());
    md::JointDef drive = pivot; drive.id = "drive"; drive.type = "drive_moment";
    md::JointDef jA = rev("A", 0, 1, Vector3d(r, 0, 0));        // crank -> rod
    md::JointDef jB = rev("B", 1, 2, Vector3d(l, 0, 0));        // rod -> slider
    md::JointDef guide;                                          // ground -> slider
    guide.id = "guide"; guide.type = "prismatic";
    guide.body_a = -1; guide.body_b = 2;
    guide.anchor_local = Vector3d::Zero(); guide.axis_local = Vector3d::UnitX();

    md::Options opt;
    opt.dt = dt;
    opt.gravity = Vector3d::Zero();

    md::DynResult res = md::solve_inverse_dynamics(
        {crank, rod, slider}, {pivot, drive, jA, jB, guide}, opt);
    REQUIRE((int)res.frames.size() == nf);

    // Analytic slider acceleration: ẍ = x''(θ)·Ω²  (θ̈ = 0).
    auto d2x = [&](double th) {
        const double s = std::sin(th), c = std::cos(th);
        const double q = std::sqrt(l * l - r * r * s * s);
        const double dq = -r * r * s * c / q;
        // d²/dθ² of sqrt(l² − r² sin²θ):
        const double d2q = (-r * r * (c * c - s * s) - dq * dq) / q;
        return -r * c + d2q;
    };
    auto d1x = [&](double th) {
        const double s = std::sin(th), c = std::cos(th);
        const double q = std::sqrt(l * l - r * r * s * s);
        return -r * s - r * r * s * c / q;
    };

    const double Fscale = ms * r * W * W;  // characteristic force
    for (int f = 8; f < nf - 8; f += 41) {
        const double th = W * f * dt;
        const double acc = d2x(th) * W * W;
        const double vel = d1x(th) * W;
        const Vector3d A(r * std::cos(th), r * std::sin(th), 0);
        const Vector3d B(xB(th), 0, 0);
        const Vector3d u = (A - B).normalized();  // rod direction at the slider pin

        const md::JointReaction& fB = res.frames[(size_t)f].reactions[3];
        const md::JointReaction& fG = res.frames[(size_t)f].reactions[4];
        const md::JointReaction& fD = res.frames[(size_t)f].reactions[1];
        CAPTURE(f);
        // Slider Newton: pin B carries the full axial inertial load.
        CHECK(std::abs(fB.force_n.x() - ms * acc) < 0.02 * Fscale);
        // Two-force rod: reaction along the rod line.
        CHECK(std::abs(fB.force_n.y() - fB.force_n.x() * u.y() / u.x()) < 0.02 * Fscale);
        // Guide takes the lateral component.
        CHECK(std::abs(fG.force_n.y() + fB.force_n.y()) < 0.02 * Fscale);
        // Power balance: τ·Ω = m_s·ẍ·ẋ (massless linkage, no gravity).
        CHECK(std::abs(fD.moment_nm.z() * W - ms * acc * vel) < 0.02 * Fscale * r * W);
        CHECK(res.frames[(size_t)f].residual < 1e-6);
    }
}

// ---------------------------------------------------------------------------
TEST_CASE("degenerate inputs stay soft") {
    md::Options opt;
    CHECK(md::solve_inverse_dynamics({}, {}, opt).warnings.size() == 1);

    md::BodyProps one;
    one.id = "b"; one.mass_kg = 1.0;
    one.poses.push_back(Matrix4d::Identity());  // single frame: too short
    CHECK(md::solve_inverse_dynamics({one}, {}, opt).frames.empty());
}
