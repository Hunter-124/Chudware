// Sketch-plane 2D↔3D lift/project + polar↔cartesian helpers.
//
// A SketchPlane defines an orthonormal right-handed coordinate frame embedded in
// 3D world space (Z-up convention) so that sketch geometry solved in 2D can be
// lifted into world space for downstream features (extrude, revolve, mate solver).
//
// DATUM PLANE CONVENTION (must match the host-side datum-plane convention):
//   World is Z-up.
//   TOP   plane = XY plane (most natural work plane, like a table top).
//   FRONT plane = XZ plane (faces the viewer looking along -Y).
//   RIGHT plane = YZ plane (faces the viewer looking along -X, or +X depending
//                            on view; the normal points in the +X direction by
//                            convention — "right side" of the part).
//
// NORMAL-DIRECTION CHOICES (right-handed, outward-facing):
//   top()   : u=+X, v=+Y  → normal = u×v = +Z  (outward = up)
//   front() : u=+X, v=+Z  → normal = u×v = +X×+Z = -(+Z×+X) = -Y
//             i.e. normal = -Y (outward = towards the viewer in standard
//             SolidWorks/SW-like orientation where FRONT faces -Y)
//   right() : u=+Y, v=+Z  → normal = u×v = +Y×+Z = +X
//             i.e. normal = +X (outward = to the right of the part)
//
// ANGLE CONVENTION: all angles are stored and passed in RADIANS internally.
// The application layer (sketch_solver.py and the host) stores angles in DEGREES;
// callers must convert at the boundary.
#pragma once

#include <Eigen/Dense>
#include <optional>

namespace chudware::geom {

// ---------------------------------------------------------------------------
// SketchPlane — an orthonormal right-handed frame (origin, uAxis, vAxis, normal)
// ---------------------------------------------------------------------------
class SketchPlane {
public:
    // The four frame vectors. All are unit-length; normal = uAxis × vAxis.
    // Direct construction is allowed (caller must ensure orthonormality).
    Eigen::Vector3d origin;  // world-space origin of the sketch plane
    Eigen::Vector3d uAxis;   // local +U direction in world space
    Eigen::Vector3d vAxis;   // local +V direction in world space
    Eigen::Vector3d normal;  // = uAxis × vAxis (outward normal)

    // Default constructor: TOP plane at world origin.
    SketchPlane();

    // General constructor: build a stable orthonormal frame from an origin,
    // a normal direction, and an optional in-plane reference direction.
    //
    // If `ref` is supplied it is projected onto the plane and used as uAxis
    // (then vAxis = normal × uAxis to complete the right-handed triad).
    // If `ref` is omitted (or is parallel to `normal`), a stable fallback is
    // used: if |normal × (0,0,1)| > 1e-6, uAxis = normalize(normal × (0,0,1))
    // rotated to be the first in-plane axis; otherwise uAxis = (1,0,0) projected.
    // The algorithm guarantees uAxis ⊥ normal and |uAxis| = 1.
    //
    // `normal` need not be pre-normalised; it will be normalised internally.
    // `ref` need not be pre-normalised or perpendicular to `normal`.
    static SketchPlane from(const Eigen::Vector3d& origin,
                            const Eigen::Vector3d& normal,
                            std::optional<Eigen::Vector3d> ref = std::nullopt);

    // ---------------------------------------------------------------------------
    // Standard-plane factories (Z-up convention — see file header)
    // ---------------------------------------------------------------------------

    // TOP plane: u=+X, v=+Y, normal=+Z. (XY plane, origin at world origin)
    static SketchPlane top();

    // FRONT plane: u=+X, v=+Z, normal=-Y. (XZ plane, origin at world origin)
    // Normal points in the -Y direction (towards the conventional viewer).
    static SketchPlane front();

    // RIGHT plane: u=+Y, v=+Z, normal=+X. (YZ plane, origin at world origin)
    // Normal points in the +X direction (outward = to the right of the part).
    static SketchPlane right();

    // ---------------------------------------------------------------------------
    // Point lift / project
    // ---------------------------------------------------------------------------

    // Lift a 2D sketch-space point (u, v) to a 3D world-space point.
    //   result = origin + uv.x() * uAxis + uv.y() * vAxis
    Eigen::Vector3d lift(const Eigen::Vector2d& uv) const;

    // Project a 3D world-space point onto the sketch plane.
    //   result = { (p - origin) · uAxis,  (p - origin) · vAxis }
    // Points off the plane are silently projected (Z/normal component dropped).
    Eigen::Vector2d project(const Eigen::Vector3d& p) const;

    // ---------------------------------------------------------------------------
    // Direction lift / project (no origin translation — for vectors / tangents)
    // ---------------------------------------------------------------------------

    // Lift a 2D direction vector (no translation).
    //   result = d.x() * uAxis + d.y() * vAxis
    Eigen::Vector3d liftDir(const Eigen::Vector2d& d) const;

    // Project a 3D direction onto the plane's (u, v) axes (no translation).
    //   result = { d · uAxis,  d · vAxis }
    Eigen::Vector2d projectDir(const Eigen::Vector3d& d) const;
};

// ---------------------------------------------------------------------------
// Polar ↔ Cartesian helpers (2D, in sketch space)
//
// All angles are in RADIANS internally.  The app stores degrees at the UI
// boundary (matching sketch_solver.py); convert there, not here.
// ---------------------------------------------------------------------------

// Convert polar (r, thetaRad) with a given pole to Cartesian (x, y).
//   result = pole + r * (cos(theta), sin(theta))
// r may be negative (mirrors through the pole).
// Overload without pole uses the origin as the pole.
Eigen::Vector2d polarToCartesian(double r, double thetaRad,
                                 const Eigen::Vector2d& pole);
Eigen::Vector2d polarToCartesian(double r, double thetaRad);  // pole = origin

// Convert Cartesian point p with a given pole to polar (r, thetaRad).
//   r       = ||p - pole||  (always >= 0)
//   thetaRad = atan2(dy, dx) in (-pi, pi]
// If p == pole, r = 0 and thetaRad = 0.
void cartesianToPolar(const Eigen::Vector2d& p, const Eigen::Vector2d& pole,
                      double& r, double& thetaRad);

}  // namespace chudware::geom
