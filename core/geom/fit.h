// Least-squares primitive fitting from a point cloud (OCCT-free, pure Eigen).
//
// Why this exists: merging INDEPENDENTLY SOURCED CAD (a wheel from one vendor onto
// a frame from another) needs a verifiable mating axis, and an imported body has no
// parametric feature to read it from — only geometry. `fit_cylinder` recovers the
// bore/shaft axis and radius from surface samples (tessellation vertices, face
// points, a scan), and reports the RMS residual so a caller can PROVE the fit is a
// cylinder rather than trusting it (a hole picked by eye is not evidence).
//
// Units are whatever the caller feeds in; the document pipeline is millimetres.
#pragma once

#include <Eigen/Dense>
#include <cstddef>
#include <string>
#include <vector>

namespace chudware::geom {

// Result of fit_cylinder. On failure `ok` is false and `message` says why; the
// numeric fields are then undefined.
struct FitCylinderResult {
    bool ok = false;
    std::string message;          // failure reason when !ok (empty on success)
    Eigen::Vector3d axis_point{0, 0, 0};  // point ON the axis, nearest the centroid
    Eigen::Vector3d axis_dir{0, 0, 1};    // unit axis direction (sign is arbitrary)
    double radius = 0.0;                  // fitted radius
    double rms_residual = 0.0;            // sqrt(mean((dist_to_axis - radius)^2))
    std::size_t n_points = 0;             // points used
};

// Fit an infinite cylinder to `pts` in the least-squares sense.
//
// Two stages:
//  1. PCA of the covariance about the centroid gives three candidate axis guesses.
//     Which eigenvector is the axis depends on the sampled patch (a long shaft's
//     axis is the LARGEST-variance direction; a short ring's is the smallest), so
//     all three are seeded rather than guessing from the spectrum.
//  2. Gauss-Newton refine of a 5-parameter model — axis point in the two in-plane
//     directions, axis tilt in the same two, radius — on the geometric residual
//     dist(P_i, axis) - r. The best (lowest RMS) of the three seeds wins.
//
// Needs >= 6 points (5 parameters + 1) and a non-degenerate cloud.
FitCylinderResult fit_cylinder(const std::vector<Eigen::Vector3d>& pts);

}  // namespace chudware::geom
