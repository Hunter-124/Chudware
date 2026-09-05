#include "geom/fit.h"

#include <algorithm>
#include <cmath>
#include <limits>

namespace chudware::geom {
namespace {

// Any unit vector perpendicular to d, chosen stably (avoids the near-parallel
// cross product that collapses to zero).
Eigen::Vector3d perp_of(const Eigen::Vector3d& d) {
    const Eigen::Vector3d ref =
        (std::abs(d.z()) < 0.9) ? Eigen::Vector3d(0, 0, 1) : Eigen::Vector3d(1, 0, 0);
    return d.cross(ref).normalized();
}

// Perpendicular distance from p to the line (a, unit d).
inline double dist_to_axis(const Eigen::Vector3d& p, const Eigen::Vector3d& a,
                           const Eigen::Vector3d& d, Eigen::Vector3d& w_out) {
    const Eigen::Vector3d q = p - a;
    w_out = q - q.dot(d) * d;
    return w_out.norm();
}

// One Gauss-Newton refine from a seed axis direction. Returns false if the model
// never became evaluable (degenerate cloud collapsed onto the axis).
bool refine(const std::vector<Eigen::Vector3d>& pts, const Eigen::Vector3d& centroid,
            const Eigen::Vector3d& seed_dir, FitCylinderResult& out) {
    Eigen::Vector3d d = seed_dir.normalized();
    Eigen::Vector3d a = centroid;
    // Seed the radius with the mean perpendicular distance about the seed axis.
    double r = 0.0;
    {
        Eigen::Vector3d w;
        for (const Eigen::Vector3d& p : pts) r += dist_to_axis(p, a, d, w);
        r /= static_cast<double>(pts.size());
    }
    if (!(r > 0.0)) return false;

    // Parameters: [da_u, da_v, dtilt_u, dtilt_v, dr] in the frame (u, v) ⟂ d.
    // Residual_i = |w_i| - r, with w_i the component of (P_i - a) ⟂ d.
    //   d|w|/da  along u  = -(e_i·u)          (e_i = w_i/|w_i|, already ⟂ d)
    //   d|w|/dd  along u  = -(q_i·d)(e_i·u)
    //   d|w|/dr           = -1
    double prev_cost = std::numeric_limits<double>::infinity();
    for (int iter = 0; iter < 60; ++iter) {
        const Eigen::Vector3d u = perp_of(d);
        const Eigen::Vector3d v = d.cross(u);
        Eigen::Matrix<double, 5, 5> JtJ = Eigen::Matrix<double, 5, 5>::Zero();
        Eigen::Matrix<double, 5, 1> Jtr = Eigen::Matrix<double, 5, 1>::Zero();
        double cost = 0.0;
        for (const Eigen::Vector3d& p : pts) {
            const Eigen::Vector3d q = p - a;
            Eigen::Vector3d w;
            const double rho = dist_to_axis(p, a, d, w);
            if (!(rho > 1e-12)) continue;  // point sits on the axis: no gradient
            const Eigen::Vector3d e = w / rho;
            const double eu = e.dot(u), ev = e.dot(v), qd = q.dot(d);
            Eigen::Matrix<double, 5, 1> J;
            J << -eu, -ev, -qd * eu, -qd * ev, -1.0;
            const double res = rho - r;
            JtJ.noalias() += J * J.transpose();
            Jtr.noalias() += J * res;
            cost += res * res;
        }
        // Tiny Levenberg damping: the tilt columns vanish on a perfectly planar
        // ring of samples (every q·d is 0), which would make JtJ singular.
        for (int k = 0; k < 5; ++k) JtJ(k, k) += 1e-12 * (1.0 + JtJ(k, k));
        const Eigen::Matrix<double, 5, 1> step = JtJ.ldlt().solve(-Jtr);
        if (!step.allFinite()) break;
        a += step(0) * u + step(1) * v;
        d = (d + step(2) * u + step(3) * v).normalized();
        r += step(4);
        if (!(r > 0.0)) return false;
        const double rel = std::abs(prev_cost - cost) / std::max(cost, 1e-30);
        prev_cost = cost;
        if (step.norm() < 1e-12 || (iter > 2 && rel < 1e-14)) break;
    }

    // Final residual at the converged model, and the axis point nearest the centroid
    // (an axis point is only defined up to a slide along d — pin it deterministically).
    double sse = 0.0;
    for (const Eigen::Vector3d& p : pts) {
        Eigen::Vector3d w;
        const double res = dist_to_axis(p, a, d, w) - r;
        sse += res * res;
    }
    out.ok = true;
    out.message.clear();
    out.axis_dir = d;
    out.axis_point = a + (centroid - a).dot(d) * d;
    out.radius = r;
    out.rms_residual = std::sqrt(sse / static_cast<double>(pts.size()));
    out.n_points = pts.size();
    return true;
}

}  // namespace

FitCylinderResult fit_cylinder(const std::vector<Eigen::Vector3d>& pts) {
    FitCylinderResult fail;
    if (pts.size() < 6) {
        fail.message = "cylinder fit needs at least 6 points, got " +
                       std::to_string(pts.size());
        return fail;
    }

    Eigen::Vector3d centroid = Eigen::Vector3d::Zero();
    for (const Eigen::Vector3d& p : pts) centroid += p;
    centroid /= static_cast<double>(pts.size());

    Eigen::Matrix3d cov = Eigen::Matrix3d::Zero();
    for (const Eigen::Vector3d& p : pts) {
        const Eigen::Vector3d q = p - centroid;
        cov.noalias() += q * q.transpose();
    }
    cov /= static_cast<double>(pts.size());
    if (cov.trace() <= 1e-24) {
        fail.message = "cylinder fit: all points are coincident";
        return fail;
    }

    Eigen::SelfAdjointEigenSolver<Eigen::Matrix3d> es(cov);
    if (es.info() != Eigen::Success) {
        fail.message = "cylinder fit: point covariance eigendecomposition failed";
        return fail;
    }

    // Seed from all three principal directions: which one is the axis depends on the
    // sampled patch (long shaft -> largest variance, short ring -> smallest), so the
    // residual decides rather than the spectrum.
    FitCylinderResult best;
    double best_rms = std::numeric_limits<double>::infinity();
    for (int k = 0; k < 3; ++k) {
        FitCylinderResult cand;
        if (!refine(pts, centroid, es.eigenvectors().col(k), cand)) continue;
        if (cand.rms_residual < best_rms) {
            best_rms = cand.rms_residual;
            best = cand;
        }
    }
    if (!best.ok) {
        fail.message = "cylinder fit did not converge (points are degenerate — "
                       "collinear, or a single point repeated)";
        return fail;
    }
    return best;
}

}  // namespace chudware::geom
