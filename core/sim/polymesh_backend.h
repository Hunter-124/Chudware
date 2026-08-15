// Polymesh mesher backend — Polyhedral-FEA (github.com/Hunter-124/Polyhedral-FEA) driving
// chudware::sim::MeshOut.
//
// WHY a second mesher. Netgen is an advancing-front/Delaunay mesher on the exact BRep: it
// produces the better-shaped elements and it is the default. It also REFUSES some real
// STEP files outright (self-intersecting seams, sliver faces surviving the healing pass),
// and when it does, the FEA surface has nothing to fall back to except an STL
// re-tessellation of the same broken topology. Polymesh fills the bounding volume on a
// graded Cartesian lattice and only projects the boundary, so it is robust exactly where
// a front-marching mesher is fragile. Two independent meshers also mean a stress result
// can be checked for mesher dependence without leaving the app.
//
// The seam is deliberately narrow: this file converts geometry -> MeshOut and nothing
// else. Everything downstream (fea_static/modal/thermal, overlay skinning, the RPC
// payload, the viewport) stays backend-agnostic because MeshOut's contract does not move.
//
// UNITS. Chudware writes and reads STEP in MILLIMETRES, and OCCT's STEPControl_Reader
// keeps STEP file units, so polymesh's "metres" convention is really "whatever the file
// uses" for a file-loaded model. Coordinates therefore arrive in mm and are stored in
// MeshOut unscaled — the same as the Netgen path. Any element-size value handed to
// polymesh is likewise mm.
//
// LICENSE NOTE: BSD-3-Clause, statically linked. No dynamic-linking obligation (Netgen,
// LGPL-2.1) and no subprocess quarantine (CalculiX, GPL-2.0).
#pragma once

#include <memory>
#include <vector>
#include <string>

#include "sim/mesher.h"

namespace chudware::sim {

// Mesh a STEP file with the polymesh backend. Same contract as mesh_step: never throws,
// returns ok=false with a diagnostic on any failure. Honors options.fineness /
// target_size_mm / min_size_mm / order; options that describe Netgen-only machinery
// (curvature, structured hex, quad_surface, optimize) are not applicable and are
// reported in MeshOut::note rather than silently ignored.
//
// Compiled only under CHUDWARE_WITH_POLYMESH; otherwise a stub returns ok=false with a
// build-configuration message, so callers need no #ifdef.
MeshOut mesh_step_polymesh(const std::string& step_path, const MeshOptions& options);


// Adaptive (error-driven) meshing.
//
// Every study in the app today asks the user to guess: pick a fineness, solve, and hope
// the mesh was fine enough where the stress actually concentrates. A guess that is too
// coarse under-reports peak stress — the failure mode that matters, because it reads as a
// passing factor of safety. This class supplies the missing half of the loop: after a
// solve, it scores the discretization error per element (Zienkiewicz-Zhu recovery), and
// refines only where that error lives, so the next pass spends elements where they change
// the answer instead of uniformly everywhere.
//
// Chudware keeps its OWN solver throughout. Only the error estimator and the marking /
// size-field machinery come from polymesh, so an adaptive result is directly comparable
// with every ordinary study, cache entry and overlay this app already produces.
//
// Adaptive runs are polymesh-only: refinement needs a mesher that accepts a spatial size
// field, and the Netgen route does not expose one.
class PolymeshAdaptiveMesher {
  public:
    // Loads the STEP and resolves the starting element size (through the SAME
    // resolve_size_band the one-shot paths use). Null on failure, with `error` set.
    static std::unique_ptr<PolymeshAdaptiveMesher> open(const std::string& step_path,
                                                        const MeshOptions& options,
                                                        std::string& error);
    ~PolymeshAdaptiveMesher();

    PolymeshAdaptiveMesher(const PolymeshAdaptiveMesher&) = delete;
    PolymeshAdaptiveMesher& operator=(const PolymeshAdaptiveMesher&) = delete;

    // Generate at the current element size / refinement seeds.
    bool mesh(MeshOut& out, std::string& error);

    // Score the discretization error of a displacement solution computed on the mesh most
    // recently returned by mesh(). `disp_mm` is the flat 3N nodal displacement in mm, in
    // that mesh's node order; E in MPa. `global_eta` is a dimensionless relative
    // indicator, `element_eta` its per-element breakdown.
    bool estimate_error(const std::vector<double>& disp_mm, double e_mpa, double nu,
                        std::vector<double>& element_eta, double& global_eta,
                        std::string& error);

    // Mark the worst elements (Dorfler) and tighten the size field around them. Returns
    // false when the mesh cannot usefully refine further — either nothing is marked or the
    // size floor is reached — with the reason in `note`, which is NOT an error: it is the
    // normal way an adaptive run converges.
    bool refine(const std::vector<double>& element_eta, std::string& note, std::string& error);

    // Current target element size, mm (shrinks as passes refine).
    double element_size_mm() const;

  private:
    struct Impl;
    explicit PolymeshAdaptiveMesher(std::unique_ptr<Impl> impl);
    std::unique_ptr<Impl> impl_;
};

}  // namespace chudware::sim
