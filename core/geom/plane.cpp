// Implementation of aicad::geom::SketchPlane and polar helpers.
// See plane.h for the full design rationale and datum-plane convention.
// MSVC requires _USE_MATH_DEFINES before <cmath> to get M_PI etc.
#define _USE_MATH_DEFINES
#include "geom/plane.h"

#include <cmath>
#include <stdexcept>

namespace aicad::geom {

// ---------------------------------------------------------------------------
// SketchPlane implementation
// ---------------------------------------------------------------------------

SketchPlane::SketchPlane()
    : origin(Eigen::Vector3d::Zero()),
      uAxis(Eigen::Vector3d::UnitX()),
      vAxis(Eigen::Vector3d::UnitY()),
      normal(Eigen::Vector3d::UnitZ()) {}

// static
SketchPlane SketchPlane::from(const Eigen::Vector3d& origin_in,
                              const Eigen::Vector3d& normal_in,
                              std::optional<Eigen::Vector3d> ref) {
    const double kEps = 1e-9;

    Eigen::Vector3d n = normal_in.normalized();

    // Build uAxis from ref or a stable fallback.
    Eigen::Vector3d u;
    if (ref.has_value()) {
        // Project ref onto the plane (subtract normal component).
        Eigen::Vector3d r = ref.value();
        r = r - r.dot(n) * n;
        double len = r.norm();
        if (len < kEps) {
            // ref was parallel to normal — fall through to auto-pick.
            ref = std::nullopt;
        } else {
            u = r / len;
        }
    }

    if (!ref.has_value()) {
        // Stable fallback: try +X, then +Y if +X is parallel to normal.
        Eigen::Vector3d candidate = Eigen::Vector3d::UnitX();
        if (std::abs(candidate.dot(n)) > 1.0 - kEps) {
            candidate = Eigen::Vector3d::UnitY();
        }
        u = (candidate - candidate.dot(n) * n).normalized();
    }

    Eigen::Vector3d v = n.cross(u);
    // Right-handed check: u × v should equal n.
    // n = normalised input; u ⊥ n; v = n × u ensures u × v = u × (n × u).
    // Actually we want n = u × v, so recompute: v = n.cross(u) gives
    //   u × v = u × (n × u) = n(u·u) - u(u·n) = n  ✓ (u⊥n, |u|=1)
    v.normalize();  // should already be unit but guard floating point drift

    SketchPlane sp;
    sp.origin = origin_in;
    sp.uAxis  = u;
    sp.vAxis  = v;
    sp.normal = n;
    return sp;
}

// static
SketchPlane SketchPlane::top() {
    // TOP = XY plane. u=+X, v=+Y, normal = u×v = +Z.
    SketchPlane sp;
    sp.origin = Eigen::Vector3d::Zero();
    sp.uAxis  = Eigen::Vector3d::UnitX();
    sp.vAxis  = Eigen::Vector3d::UnitY();
    sp.normal = Eigen::Vector3d::UnitZ();
    return sp;
}

// static
SketchPlane SketchPlane::front() {
    // FRONT = XZ plane. u=+X, v=+Z, normal = u×v = +X × +Z = -(+Z × +X) = -Y.
    // Normal = -Y: the FRONT plane faces the viewer who looks along +Y toward -Y.
    // In SolidWorks-like Z-up layout this is the conventional "front" orientation.
    SketchPlane sp;
    sp.origin = Eigen::Vector3d::Zero();
    sp.uAxis  = Eigen::Vector3d::UnitX();
    sp.vAxis  = Eigen::Vector3d::UnitZ();
    sp.normal = -Eigen::Vector3d::UnitY();  // +X × +Z = -Y
    return sp;
}

// static
SketchPlane SketchPlane::right() {
    // RIGHT = YZ plane. u=+Y, v=+Z, normal = u×v = +Y × +Z = +X.
    // Normal = +X: the RIGHT plane faces the viewer who looks along -X.
    // "Right side" of the part (looking from the right = along -X, normal points +X).
    SketchPlane sp;
    sp.origin = Eigen::Vector3d::Zero();
    sp.uAxis  = Eigen::Vector3d::UnitY();
    sp.vAxis  = Eigen::Vector3d::UnitZ();
    sp.normal = Eigen::Vector3d::UnitX();  // +Y × +Z = +X
    return sp;
}

Eigen::Vector3d SketchPlane::lift(const Eigen::Vector2d& uv) const {
    return origin + uv.x() * uAxis + uv.y() * vAxis;
}

Eigen::Vector2d SketchPlane::project(const Eigen::Vector3d& p) const {
    Eigen::Vector3d d = p - origin;
    return Eigen::Vector2d(d.dot(uAxis), d.dot(vAxis));
}

Eigen::Vector3d SketchPlane::liftDir(const Eigen::Vector2d& d) const {
    return d.x() * uAxis + d.y() * vAxis;
}

Eigen::Vector2d SketchPlane::projectDir(const Eigen::Vector3d& d) const {
    return Eigen::Vector2d(d.dot(uAxis), d.dot(vAxis));
}

// ---------------------------------------------------------------------------
// Polar ↔ Cartesian
// ---------------------------------------------------------------------------

Eigen::Vector2d polarToCartesian(double r, double thetaRad,
                                 const Eigen::Vector2d& pole) {
    return pole + Eigen::Vector2d(r * std::cos(thetaRad), r * std::sin(thetaRad));
}

Eigen::Vector2d polarToCartesian(double r, double thetaRad) {
    return Eigen::Vector2d(r * std::cos(thetaRad), r * std::sin(thetaRad));
}

void cartesianToPolar(const Eigen::Vector2d& p, const Eigen::Vector2d& pole,
                      double& r, double& thetaRad) {
    Eigen::Vector2d d = p - pole;
    r        = d.norm();
    thetaRad = (r < 1e-14) ? 0.0 : std::atan2(d.y(), d.x());
}

}  // namespace aicad::geom
