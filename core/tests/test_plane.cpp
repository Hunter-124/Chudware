// doctest unit tests for aicad::geom::SketchPlane and polar helpers.
//
// Conventions verified:
//   - Standard-plane frames are orthonormal, right-handed, and match Z-up.
//   - top().lift / front().lift / right().lift produce the expected world coords.
//   - lift → project round-trips for axis-aligned and tilted general planes.
//   - polar ↔ cartesian round-trip, including a non-origin pole.
//   - Lifting a 2D point on TOP then projecting back returns the original.
// MSVC does not expose M_PI without this define.
#define _USE_MATH_DEFINES
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "geom/plane.h"

#include <cmath>

using aicad::geom::SketchPlane;
using aicad::geom::polarToCartesian;
using aicad::geom::cartesianToPolar;

static constexpr double kEps = 1e-12;

// Helper: check two 3D vectors are approximately equal.
static void check3(const Eigen::Vector3d& a, const Eigen::Vector3d& b,
                   double tol = kEps) {
    CHECK((a - b).norm() == doctest::Approx(0.0).epsilon(tol));
}
// Helper: check two 2D vectors are approximately equal.
static void check2(const Eigen::Vector2d& a, const Eigen::Vector2d& b,
                   double tol = kEps) {
    CHECK((a - b).norm() == doctest::Approx(0.0).epsilon(tol));
}

// ============================================================
// TOP plane
// ============================================================
TEST_CASE("top() frame is orthonormal, right-handed, Z-up") {
    auto p = SketchPlane::top();
    // Unit axes
    CHECK(p.uAxis.norm() == doctest::Approx(1.0).epsilon(kEps));
    CHECK(p.vAxis.norm() == doctest::Approx(1.0).epsilon(kEps));
    CHECK(p.normal.norm() == doctest::Approx(1.0).epsilon(kEps));
    // Orthogonality
    CHECK(p.uAxis.dot(p.vAxis)   == doctest::Approx(0.0).epsilon(kEps));
    CHECK(p.uAxis.dot(p.normal)  == doctest::Approx(0.0).epsilon(kEps));
    CHECK(p.vAxis.dot(p.normal)  == doctest::Approx(0.0).epsilon(kEps));
    // Right-handedness: u × v == normal
    check3(p.uAxis.cross(p.vAxis), p.normal);
    // Convention: u=+X, v=+Y, normal=+Z
    check3(p.uAxis,  Eigen::Vector3d::UnitX());
    check3(p.vAxis,  Eigen::Vector3d::UnitY());
    check3(p.normal, Eigen::Vector3d::UnitZ());
}

TEST_CASE("top().lift maps u→X, v→Y") {
    auto p = SketchPlane::top();
    // Lifting unit-U gives world +X
    check3(p.lift({1.0, 0.0}), Eigen::Vector3d(1.0, 0.0, 0.0));
    // Lifting unit-V gives world +Y
    check3(p.lift({0.0, 1.0}), Eigen::Vector3d(0.0, 1.0, 0.0));
    // Lifting (3, -2) gives (3, -2, 0)
    check3(p.lift({3.0, -2.0}), Eigen::Vector3d(3.0, -2.0, 0.0));
}

// ============================================================
// FRONT plane
// ============================================================
TEST_CASE("front() frame is orthonormal, right-handed") {
    auto p = SketchPlane::front();
    CHECK(p.uAxis.norm() == doctest::Approx(1.0).epsilon(kEps));
    CHECK(p.vAxis.norm() == doctest::Approx(1.0).epsilon(kEps));
    CHECK(p.normal.norm() == doctest::Approx(1.0).epsilon(kEps));
    CHECK(p.uAxis.dot(p.vAxis)   == doctest::Approx(0.0).epsilon(kEps));
    CHECK(p.uAxis.dot(p.normal)  == doctest::Approx(0.0).epsilon(kEps));
    CHECK(p.vAxis.dot(p.normal)  == doctest::Approx(0.0).epsilon(kEps));
    check3(p.uAxis.cross(p.vAxis), p.normal);
}

TEST_CASE("front() convention: u=+X, v=+Z, normal=-Y") {
    auto p = SketchPlane::front();
    check3(p.uAxis,  Eigen::Vector3d::UnitX());
    check3(p.vAxis,  Eigen::Vector3d::UnitZ());
    check3(p.normal, -Eigen::Vector3d::UnitY());
}

TEST_CASE("front().lift maps v to +Z") {
    auto p = SketchPlane::front();
    // unit-V on FRONT lifts to world +Z (height in the XZ plane)
    check3(p.lift({0.0, 1.0}), Eigen::Vector3d(0.0, 0.0, 1.0));
    // unit-U lifts to world +X
    check3(p.lift({1.0, 0.0}), Eigen::Vector3d(1.0, 0.0, 0.0));
    // (5, 3) -> (5, 0, 3)
    check3(p.lift({5.0, 3.0}), Eigen::Vector3d(5.0, 0.0, 3.0));
}

// ============================================================
// RIGHT plane
// ============================================================
TEST_CASE("right() frame is orthonormal, right-handed") {
    auto p = SketchPlane::right();
    CHECK(p.uAxis.norm() == doctest::Approx(1.0).epsilon(kEps));
    CHECK(p.vAxis.norm() == doctest::Approx(1.0).epsilon(kEps));
    CHECK(p.normal.norm() == doctest::Approx(1.0).epsilon(kEps));
    CHECK(p.uAxis.dot(p.vAxis)   == doctest::Approx(0.0).epsilon(kEps));
    CHECK(p.uAxis.dot(p.normal)  == doctest::Approx(0.0).epsilon(kEps));
    CHECK(p.vAxis.dot(p.normal)  == doctest::Approx(0.0).epsilon(kEps));
    check3(p.uAxis.cross(p.vAxis), p.normal);
}

TEST_CASE("right() convention: u=+Y, v=+Z, normal=+X") {
    auto p = SketchPlane::right();
    check3(p.uAxis,  Eigen::Vector3d::UnitY());
    check3(p.vAxis,  Eigen::Vector3d::UnitZ());
    check3(p.normal, Eigen::Vector3d::UnitX());
}

TEST_CASE("right().lift maps to YZ plane") {
    auto p = SketchPlane::right();
    // unit-U on RIGHT lifts to world +Y
    check3(p.lift({1.0, 0.0}), Eigen::Vector3d(0.0, 1.0, 0.0));
    // unit-V on RIGHT lifts to world +Z
    check3(p.lift({0.0, 1.0}), Eigen::Vector3d(0.0, 0.0, 1.0));
    // (2, 7) -> (0, 2, 7)
    check3(p.lift({2.0, 7.0}), Eigen::Vector3d(0.0, 2.0, 7.0));
}

// ============================================================
// lift / project round-trips
// ============================================================
TEST_CASE("top(): lift then project returns original 2D point") {
    auto p = SketchPlane::top();
    Eigen::Vector2d uv(4.2, -1.7);
    check2(p.project(p.lift(uv)), uv);
}

TEST_CASE("front(): lift then project returns original 2D point") {
    auto p = SketchPlane::front();
    Eigen::Vector2d uv(-3.5, 8.1);
    check2(p.project(p.lift(uv)), uv);
}

TEST_CASE("right(): lift then project returns original 2D point") {
    auto p = SketchPlane::right();
    Eigen::Vector2d uv(0.0, 5.5);
    check2(p.project(p.lift(uv)), uv);
}

TEST_CASE("general tilted plane: lift then project round-trips") {
    // Build a plane from an arbitrary origin/normal/ref.
    Eigen::Vector3d orig(1.0, 2.0, 3.0);
    Eigen::Vector3d norm(1.0, 1.0, 1.0);  // diagonal, will be normalised
    Eigen::Vector3d ref(1.0, 0.0, 0.0);
    auto p = SketchPlane::from(orig, norm, ref);

    // Frame should be orthonormal and right-handed.
    CHECK(p.uAxis.norm() == doctest::Approx(1.0).epsilon(1e-10));
    CHECK(p.vAxis.norm() == doctest::Approx(1.0).epsilon(1e-10));
    CHECK(p.normal.norm() == doctest::Approx(1.0).epsilon(1e-10));
    CHECK(p.uAxis.dot(p.vAxis)  == doctest::Approx(0.0).epsilon(1e-10));
    check3(p.uAxis.cross(p.vAxis), p.normal, 1e-10);

    // lift → project round-trip.
    Eigen::Vector2d uv(3.7, -2.1);
    check2(p.project(p.lift(uv)), uv, 1e-10);
}

TEST_CASE("general plane from() without ref: stable fallback") {
    // Normal nearly parallel to +X; fallback must not degenerate.
    Eigen::Vector3d orig(0, 0, 0);
    Eigen::Vector3d norm(1.0, 0.0, 0.0);
    auto p = SketchPlane::from(orig, norm);

    CHECK(p.uAxis.norm() == doctest::Approx(1.0).epsilon(1e-10));
    CHECK(p.vAxis.norm() == doctest::Approx(1.0).epsilon(1e-10));
    CHECK(p.uAxis.dot(p.vAxis)  == doctest::Approx(0.0).epsilon(1e-10));
    check3(p.uAxis.cross(p.vAxis), p.normal, 1e-10);

    Eigen::Vector2d uv(-5.0, 2.0);
    check2(p.project(p.lift(uv)), uv, 1e-10);
}

TEST_CASE("liftDir / projectDir round-trips (no translation)") {
    auto p = SketchPlane::front();
    Eigen::Vector2d d(3.0, 4.0);
    check2(p.projectDir(p.liftDir(d)), d);
}

TEST_CASE("liftDir does not include origin offset") {
    auto p = SketchPlane::top();
    p.origin = Eigen::Vector3d(10.0, 20.0, 30.0);  // move origin far away
    Eigen::Vector2d d(1.0, 0.0);
    // liftDir must give +X regardless of origin.
    check3(p.liftDir(d), Eigen::Vector3d::UnitX());
}

// ============================================================
// Polar ↔ Cartesian
// ============================================================
TEST_CASE("polarToCartesian: basic angles") {
    // At pole = origin, theta = 0 → pure +X
    auto pt = polarToCartesian(5.0, 0.0);
    check2(pt, Eigen::Vector2d(5.0, 0.0));

    // theta = pi/2 → pure +Y
    auto pt2 = polarToCartesian(3.0, M_PI / 2.0);
    check2(pt2, Eigen::Vector2d(0.0, 3.0), 1e-12);

    // theta = pi → pure -X
    auto pt3 = polarToCartesian(2.0, M_PI);
    check2(pt3, Eigen::Vector2d(-2.0, 0.0), 1e-12);
}

TEST_CASE("polarToCartesian: non-origin pole") {
    Eigen::Vector2d pole(3.0, 4.0);
    auto pt = polarToCartesian(1.0, 0.0, pole);
    // At theta=0 the point is pole + (1, 0)
    check2(pt, Eigen::Vector2d(4.0, 4.0));
}

TEST_CASE("cartesianToPolar: round-trip at origin pole") {
    double r, theta;
    Eigen::Vector2d p(3.0, 4.0);
    Eigen::Vector2d pole = Eigen::Vector2d::Zero();
    cartesianToPolar(p, pole, r, theta);
    CHECK(r == doctest::Approx(5.0).epsilon(1e-12));
    // Round-trip back to Cartesian
    auto p2 = polarToCartesian(r, theta, pole);
    check2(p2, p, 1e-12);
}

TEST_CASE("cartesianToPolar: round-trip with non-origin pole") {
    double r, theta;
    Eigen::Vector2d pole(1.0, -2.0);
    Eigen::Vector2d p(4.0, 2.0);
    cartesianToPolar(p, pole, r, theta);
    CHECK(r == doctest::Approx(5.0).epsilon(1e-12));  // (3,4) → r=5
    auto p2 = polarToCartesian(r, theta, pole);
    check2(p2, p, 1e-12);
}

TEST_CASE("cartesianToPolar: p == pole gives r=0, theta=0") {
    double r, theta;
    Eigen::Vector2d pole(7.0, -3.0);
    cartesianToPolar(pole, pole, r, theta);
    CHECK(r     == doctest::Approx(0.0).epsilon(1e-14));
    CHECK(theta == doctest::Approx(0.0).epsilon(1e-14));
}

TEST_CASE("polar round-trip: angle preservation across quadrants") {
    const double angles[6] = {0.1, M_PI / 4.0, M_PI / 2.0 + 0.1,
                               M_PI - 0.1, -M_PI / 3.0, -2.5};
    for (double a : angles) {
        double r2, theta2;
        auto pt = polarToCartesian(2.0, a);
        cartesianToPolar(pt, Eigen::Vector2d::Zero(), r2, theta2);
        CHECK(r2     == doctest::Approx(2.0).epsilon(1e-12));
        CHECK(theta2 == doctest::Approx(a).epsilon(1e-12));
    }
}

// ============================================================
// Combined: lift on TOP then project back
// ============================================================
TEST_CASE("TOP: lift 2D point into 3D, then project back = identity") {
    auto p = SketchPlane::top();
    // Move the origin just to check the math holds off-origin too.
    p.origin = Eigen::Vector3d(5.0, -3.0, 2.0);
    Eigen::Vector2d uv(10.0, -7.0);
    Eigen::Vector3d world = p.lift(uv);
    Eigen::Vector2d back  = p.project(world);
    check2(back, uv, 1e-11);
}
