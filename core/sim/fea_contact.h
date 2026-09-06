// Contact-candidate construction for unilateral (frictionless) contact FEA.
//
// fea_static.cpp is the pure kernel: it takes ContactSpring pairs that are ALREADY
// resolved to mesh nodes, normals and initial gaps. This file is the other half — the
// mesh-topology pass that finds those pairs on a real meshed assembly:
//
//   1. label the node-adjacency components of the volume mesh (one per solid body),
//   2. extract the mesh's own boundary faces (a face owned by exactly ONE element),
//      and from them the outward normal + tributary area of every surface node,
//   3. pair surface nodes of DIFFERENT components that FACE each other across a gap
//      no wider than max_gap_mm, and record each pair's initial signed gap.
//
// The normals come from the tet/hex boundary faces, never from the CAD tessellation:
// the solve happens on the mesh, so the gap that matters is the mesh's gap.
#pragma once

#include <array>
#include <string>
#include <vector>

#include "sim/fea_static.h"

namespace chudware::sim {

struct ContactCandidateOptions {
    // Widest mesh gap still treated as a contact candidate (mm).
    double max_gap_mm = 0.5;
    // Multiplies the default penalty stiffness E * characteristic element length.
    double penalty_scale = 1.0;
    // How opposed two surface normals must be to count as facing surfaces:
    // dot(n_a, n_b) <= -min_opposition. 0.35 admits a curved bore/pin interface
    // (where the two normals are only approximately anti-parallel) while rejecting
    // two surfaces that merely pass close by at an angle.
    double min_opposition = 0.35;
};

struct ContactCandidateSet {
    std::vector<ContactSpring> springs;
    // Dense component ordinal per node (0..n_components-1); -1 for a node no element
    // references. Stable: ordinals follow ascending lowest-node-id per component.
    std::vector<int> component;
    int n_components = 0;
    // group id (ContactSpring::group) -> the two component ordinals it joins, ascending.
    std::vector<std::array<int, 2>> group_components;
    // Mean first-edge length of the mesh (mm) — the penalty-stiffness and lateral
    // pairing scale.
    double char_len_mm = 0.0;
    int surface_nodes = 0;
    // Non-empty means the mesh could not be interpreted (bad stride, no boundary).
    std::string error;
};

ContactCandidateSet fea_contact_candidates(const std::vector<double>& points,
                                           const std::vector<int>& tets,
                                           int nodes_per_elem, double E_mpa,
                                           const ContactCandidateOptions& options);

}  // namespace chudware::sim
