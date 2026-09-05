// geom/fit.cpp — least-squares cylinder fit (the mating-axis primitive behind the
// eval_fit_cylinder RPC). Merging independently sourced CAD needs the axis of an
// IMPORTED bore/shaft, which has no parametric feature to read it from; the fit is
// only usable as evidence if it recovers the axis and radius from noisy surface
// samples and reports an honest residual. This file feeds synthetic samples off a
// known cylinder (deterministic pseudo-noise, no RNG dependency) and asserts the
// recovered axis point/direction/radius to 1e-3 relative.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <vector>

#include <Eigen/Dense>

#include "geom/fit.h"

using chudware::geom::fit_cylinder;
using chudware::geom::FitCylinderResult;

namespace {

constexpr double PI = 3.14159265358979323846;

// Deterministic, zero-mean-ish "noise" in [-1, 1]: a pair of incommensurate sines,
// so the cloud is not a perfect analytic cylinder but the test never flakes.
double wobble(int i, double phase) {
    return std::sin(i * 1.113 + phase) * 0.6 + std::sin(i * 0.317 + 2.0 * phase) * 0.4;
}

// Sample a cylinder of radius `r` about axis (`base`, unit `dir`) over `len` of
// length, `n_ring` points per ring and `n_ax` rings, perturbed by `noise` (mm)
// radially and axially.
std::vector<Eigen::Vector3d> sample_cylinder(const Eigen::Vector3d& base,
                                            const Eigen::Vector3d& dir, double r,
                                            double len, int n_ring, int n_ax,
                                            double noise) {
    const Eigen::Vector3d d = dir.normalized();
    const Eigen::Vector3d u =
        (std::abs(d.z()) < 0.9 ? d.cross(Eigen::Vector3d(0, 0, 1))
                               : d.cross(Eigen::Vector3d(1, 0, 0)))
            .normalized();
    const Eigen::Vector3d v = d.cross(u);
    std::vector<Eigen::Vector3d> pts;
    pts.reserve(static_cast<std::size_t>(n_ring) * static_cast<std::size_t>(n_ax));
    int k = 0;
    for (int j = 0; j < n_ax; ++j) {
        const double t = len * static_cast<double>(j) / static_cast<double>(n_ax - 1);
        for (int i = 0; i < n_ring; ++i, ++k) {
            const double th = 2.0 * PI * static_cast<double>(i) / static_cast<double>(n_ring);
            const double rr = r + noise * wobble(k, 0.7);
            const double tt = t + noise * wobble(k, 1.9);
            pts.push_back(base + tt * d + rr * (std::cos(th) * u + std::sin(th) * v));
        }
    }
    return pts;
}

}  // namespace

TEST_CASE("fit_cylinder recovers a noisy oblique cylinder's axis and radius") {
    const Eigen::Vector3d base(12.5, -7.25, 3.0);
    const Eigen::Vector3d dir = Eigen::Vector3d(0.3, -0.5, 1.0).normalized();
    const double radius = 8.0;
    const double noise = 0.004;  // 4 µm of surface scatter (tessellation-grade)

    const std::vector<Eigen::Vector3d> pts =
        sample_cylinder(base, dir, radius, 40.0, 24, 7, noise);
    const FitCylinderResult f = fit_cylinder(pts);

    REQUIRE(f.ok);
    CHECK(f.n_points == pts.size());

    // Radius to 1e-3 RELATIVE (8 mm -> 8 µm).
    CHECK(f.radius == doctest::Approx(radius).epsilon(1e-3));

    // Axis DIRECTION: parallel to the truth (sign is arbitrary), so compare |cos|.
    CHECK(std::abs(f.axis_dir.dot(dir)) == doctest::Approx(1.0).epsilon(1e-3));
    CHECK(f.axis_dir.norm() == doctest::Approx(1.0).epsilon(1e-12));

    // Axis POSITION: an axis point slides freely along the axis, so the meaningful
    // error is the PERPENDICULAR offset from the true line — held to 1e-3 of the
    // radius (8 µm).
    const Eigen::Vector3d q = f.axis_point - base;
    const double perp = (q - q.dot(dir) * dir).norm();
    CHECK(perp < 1e-3 * radius);

    // The residual is the fit's own evidence: it must reflect the injected scatter,
    // not be silently zero (which would mean it is not being measured).
    CHECK(f.rms_residual < 2.0 * noise);
    CHECK(f.rms_residual > 0.0);
}

TEST_CASE("fit_cylinder on a short ring seeds the axis from the right principal direction") {
    // A bore sampled over 2 mm of a 30 mm radius: the axis is the SMALLEST-variance
    // principal direction here, the opposite of the long-shaft case above. Locks the
    // all-three-eigenvectors seeding.
    const Eigen::Vector3d base(0, 0, 0);
    const Eigen::Vector3d dir(0, 0, 1);
    const std::vector<Eigen::Vector3d> pts =
        sample_cylinder(base, dir, 30.0, 2.0, 32, 3, 0.002);
    const FitCylinderResult f = fit_cylinder(pts);

    REQUIRE(f.ok);
    CHECK(f.radius == doctest::Approx(30.0).epsilon(1e-3));
    CHECK(std::abs(f.axis_dir.dot(dir)) == doctest::Approx(1.0).epsilon(1e-3));
}

TEST_CASE("fit_cylinder refuses a cloud that cannot constrain the model") {
    const std::vector<Eigen::Vector3d> few = {{0, 0, 0}, {1, 0, 0}, {0, 1, 0}};
    const FitCylinderResult a = fit_cylinder(few);
    CHECK_FALSE(a.ok);
    CHECK(a.message.find("at least 6 points") != std::string::npos);

    const std::vector<Eigen::Vector3d> same(12, Eigen::Vector3d(4, 4, 4));
    const FitCylinderResult b = fit_cylinder(same);
    CHECK_FALSE(b.ok);
    CHECK_FALSE(b.message.empty());
}
