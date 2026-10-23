// Analytic V&V for the motion inverse-dynamics module.
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
#include <limits>
#include <vector>

#include "mate/mate.h"
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
        CHECK(res.frames[(size_t)f].balanced);
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
        CHECK(res.frames[(size_t)f].balanced);
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
        CHECK(res.frames[(size_t)f].balanced);
    }
}

// ---------------------------------------------------------------------------
TEST_CASE("translation: finite differences, applied force and damping match closed form") {
    // x(t)=0.5*a*t^2, v=a*t. A drive-force constraint along X must provide
    // F = m*a - F_applied + c*v exactly (quadratic position stencils).
    const double m = 3.0, a = 2.5, applied = 1.25, damping = 0.4, dt = 0.01;
    const int nf = 101;
    md::BodyProps body;
    body.id = "slider";
    body.mass_kg = m;
    body.inertia_local = Matrix3d::Zero();  // point-mass translation
    for (int f = 0; f < nf; ++f) {
        const double t = f * dt;
        body.poses.push_back(pose(Matrix3d::Identity(),
                                  Vector3d(0.5 * a * t * t, 0, 0)));
    }
    md::JointDef drive;
    drive.id = "drive";
    drive.type = "drive_force";
    drive.body_a = -1;
    drive.body_b = 0;
    drive.axis_local = Vector3d::UnitX();
    md::Options opt;
    opt.dt = dt;
    opt.gravity = Vector3d::Zero();
    opt.damping_lin = damping;
    md::AppliedLoad load;
    load.body = 0;
    load.force_n = Vector3d(applied, 0, 0);
    opt.loads.push_back(load);

    md::DynResult res = md::solve_inverse_dynamics({body}, {drive}, opt);
    REQUIRE(res.ok);
    REQUIRE((int)res.frames.size() == nf);
    for (int f = 0; f < nf; f += 10) {
        const double t = f * dt;
        const double expected = m * a - applied + damping * a * t;
        CAPTURE(f);
        CHECK(std::abs(res.frames[(size_t)f].vel[0].x() - a * t) < 1e-10);
        CHECK(std::abs(res.frames[(size_t)f].accel[0].x() - a) < 1e-9);
        CHECK(std::abs(res.frames[(size_t)f].reactions[0].force_n.x() - expected) < 1e-9);
        CHECK(res.frames[(size_t)f].balanced);
    }
}

TEST_CASE("unresolved force path is explicit failure, never a zero-reaction success") {
    md::BodyProps body;
    body.id = "unsupported";
    body.mass_kg = 2.0;
    body.inertia_local = 0.01 * Matrix3d::Identity();
    body.poses.assign(3, Matrix4d::Identity());

    // Orientation-only constraint cannot react gravity.
    md::JointDef orient;
    orient.id = "angle";
    orient.type = "drive_moment";
    orient.body_a = -1;
    orient.body_b = 0;
    orient.axis_local = Vector3d::UnitZ();
    md::Options opt;
    opt.dt = 0.1;
    opt.gravity = Vector3d(0, 0, -9.81);

    md::DynResult res = md::solve_inverse_dynamics({body}, {orient}, opt);
    CHECK_FALSE(res.ok);
    REQUIRE(res.frames.size() == 3);
    for (const md::FrameDyn& frame : res.frames) {
        CHECK_FALSE(frame.balanced);
        CHECK(std::abs(frame.force_residual_n - 2.0 * 9.81) < 1e-10);
        CHECK(frame.reactions[0].force_n.norm() == 0.0);
    }
    REQUIRE_FALSE(res.warnings.empty());
    CHECK(res.warnings.back().find("unresolved force/moment path") != std::string::npos);
}

TEST_CASE("invalid timing, mass, inertia, pose and load data are rejected") {
    md::BodyProps body;
    body.id = "b";
    body.mass_kg = 1.0;
    body.inertia_local = Matrix3d::Zero();
    body.poses.assign(3, Matrix4d::Identity());
    md::Options opt;
    opt.dt = 0.1;

    CHECK(md::solve_inverse_dynamics({}, {}, opt).frames.empty());

    md::BodyProps two_frames = body;
    two_frames.poses.resize(2);
    CHECK_FALSE(md::solve_inverse_dynamics({two_frames}, {}, opt).ok);

    md::Options bad_dt = opt;
    bad_dt.dt = 0.0;
    CHECK_FALSE(md::solve_inverse_dynamics({body}, {}, bad_dt).ok);

    md::BodyProps bad_mass = body;
    bad_mass.mass_kg = -1.0;
    CHECK_FALSE(md::solve_inverse_dynamics({bad_mass}, {}, opt).ok);

    md::BodyProps bad_inertia = body;
    bad_inertia.inertia_local = Matrix3d::Identity();
    bad_inertia.inertia_local(2, 2) = 3.0;  // violates Izz <= Ixx + Iyy
    CHECK_FALSE(md::solve_inverse_dynamics({bad_inertia}, {}, opt).ok);

    md::BodyProps bad_pose = body;
    bad_pose.poses[1](0, 0) = 2.0;  // scale is not a rigid pose
    CHECK_FALSE(md::solve_inverse_dynamics({bad_pose}, {}, opt).ok);

    md::Options bad_load = opt;
    md::AppliedLoad load;
    load.body = 0;
    load.force_n.x() = std::numeric_limits<double>::quiet_NaN();
    bad_load.loads.push_back(load);
    CHECK_FALSE(md::solve_inverse_dynamics({body}, {}, bad_load).ok);
}

TEST_CASE("hinge constrains axial translation while preserving its rotational DOF") {
    using nlohmann::json;
    const std::vector<double> identity = {
        1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1};
    std::vector<double> shifted = identity;
    shifted[11] = 12.0;  // along the hinge Z axis
    json components = json::array({
        {{"id", "ground"}, {"fixed", true}, {"transform", identity}},
        {{"id", "link"}, {"fixed", false}, {"transform", shifted}}});
    json mates = json::array({
        {{"id", "hinge"}, {"type", "hinge"}, {"align", "aligned"},
         {"refs", json::array({json{{"component", "ground"}},
                               json{{"component", "link"}}})}}});
    chudware::mate::MateGeom axis;
    axis.kind = "axis";
    axis.point = Vector3d::Zero();
    axis.direction = Vector3d::UnitZ();
    chudware::mate::ResolvedGeometry geometry;
    geometry[{"hinge", 0}] = axis;
    geometry[{"hinge", 1}] = axis;

    chudware::mate::MateSolveResult solved =
        chudware::mate::solve_mates(components, mates, geometry);
    REQUIRE(solved.converged);
    REQUIRE(solved.transforms.count("link") == 1);
    CHECK(std::abs(solved.transforms.at("link")[3]) < 1e-8);
    CHECK(std::abs(solved.transforms.at("link")[7]) < 1e-8);
    CHECK(std::abs(solved.transforms.at("link")[11]) < 1e-8);
    CHECK(solved.dof == 1);
}

TEST_CASE("gear coupling preserves its authored phase and rejects a zero ratio") {
    using nlohmann::json;
    auto transform_z = [](double angle) {
        const double c = std::cos(angle), s = std::sin(angle);
        return std::vector<double>{
            c, -s, 0, 0,
            s,  c, 0, 0,
            0,  0, 1, 0,
            0,  0, 0, 1};
    };
    const std::vector<double> ground_pose = transform_z(0.4);
    const std::vector<double> gear_pose = transform_z(0.7);
    json components = json::array({
        {{"id", "ground"}, {"fixed", true}, {"transform", ground_pose}},
        {{"id", "gear"}, {"fixed", false}, {"transform", gear_pose}}});
    json mates = json::array({
        {{"id", "pair"}, {"type", "gear"}, {"align", "aligned"},
         {"refs", json::array({json{{"component", "ground"}},
                               json{{"component", "gear"}}})},
         {"value", {{"ratio", 2.0}}}}});
    chudware::mate::MateGeom axis;
    axis.kind = "axis";
    axis.direction = Vector3d::UnitZ();
    chudware::mate::ResolvedGeometry geometry;
    geometry[{"pair", 0}] = axis;
    geometry[{"pair", 1}] = axis;

    const chudware::mate::MateSolveResult unchanged =
        chudware::mate::solve_mates(components, mates, geometry);
    REQUIRE(unchanged.converged);
    REQUIRE(unchanged.transforms.count("gear") == 1);
    const std::vector<double>& solved_pose = unchanged.transforms.at("gear");
    REQUIRE(solved_pose.size() == gear_pose.size());
    for (size_t i = 0; i < gear_pose.size(); ++i) {
        CAPTURE(i);
        CHECK(std::abs(solved_pose[i] - gear_pose[i]) < 1e-10);
    }

    mates[0]["value"]["ratio"] = 0.0;
    const chudware::mate::MateSolveResult invalid =
        chudware::mate::solve_mates(components, mates, geometry);
    REQUIRE(invalid.mate_status.size() == 1);
    CHECK(invalid.mate_status[0].status == "unresolved");

    mates[0]["type"] = "distance_limit";
    mates[0]["value"] = {{"min", 5.0}, {"max", 1.0}};
    const chudware::mate::MateSolveResult reversed_limit =
        chudware::mate::solve_mates(components, mates, geometry);
    REQUIRE(reversed_limit.mate_status.size() == 1);
    CHECK(reversed_limit.mate_status[0].status == "unresolved");
}
