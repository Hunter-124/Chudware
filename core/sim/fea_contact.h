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
    // ── Interface resolution facts, measured ON the candidate set ───────────────
    // The element size AT THE INTERFACE, not the study's background size: the median
    // over candidate pairs of the mean boundary-face edge length incident on the
    // pair's own node. A locally refined interface reads its refined size here even
    // though the bulk mesh is coarse, which is the whole point of the number.
    double interface_h_mm = 0.0;
    // The candidate set's own gaps (mm, signed as ContactSpring::gap0_mm):
    // closest approach, and the median.
    double min_gap0_mm = 0.0;
    double median_gap0_mm = 0.0;
    // Candidates whose initial gap is within twice the closest approach — the pairs
    // that can plausibly SEAT under a small-deflection load. On a conforming
    // interface this is every pair; where the clearance opens up faster than one
    // element (a bore, a keyway flank, an angled seat) it is however many pairs the
    // discretization happened to place in the closing zone, and a load funnelled
    // through 1 of them is a point load wearing a contact-pressure label.
    int seatable_pairs = 0;
    // Total tributary area of the candidate pairs (mm^2) — the contact patch the
    // pressure of an active subset is measured against.
    double candidate_area_mm2 = 0.0;
    // Non-empty means the mesh could not be interpreted (bad stride, no boundary).
    std::string error;
};

ContactCandidateSet fea_contact_candidates(const std::vector<double>& points,
                                           const std::vector<int>& tets,
                                           int nodes_per_elem, double E_mpa,
                                           const ContactCandidateOptions& options);

// ── Is the interface RESOLVED, or is the answer a point load? ────────────────────
// A contact study can converge, balance to 0.0% equilibrium error and report a
// plausible displacement while being physically meaningless, and the live case that
// forced this guard shows exactly how: the real FRAME-PEDAL-BRAKE keyway (0.350 mm
// measured clearance) was auto-coarsened to h = 2.91 mm at the interface — 8x the
// clearance — so of 848 candidate pairs exactly ONE ever sat inside the closing
// zone. It carried the whole 2902 N through one node and reported 13,543 MPa of
// "contact pressure": a point load on a mesh, not a seated joint. Every existing
// check passed, because each of them is about the SOLVE and this defect is in the
// DISCRETIZATION.
//
// Two independent statements make an interface unresolved, and either one refuses:
//
//   1. A PRIORI, from the candidate set alone: the interface element size is more
//      than kMaxHOverGap x the clearance it has to resolve AND fewer than
//      kMinActivePairs candidates sit within twice the closest approach. Both
//      halves are required, and the second one is why: a CONFORMING interface
//      (two flat faces across a uniform 0.05 mm gap, or a concentric pin in a
//      0.1 mm bore) has h/gap of 19-133 and is perfectly resolved — every pair
//      seats together and the pressure is the real patch pressure. What kills a
//      study is a clearance that opens faster than one element, which shows up as
//      a closing zone the mesh placed almost no pairs in.
//   2. A POSTERIORI: load crossed the interface through fewer than
//      kMinActivePairs pairs. Three pairs cannot carry a distributed pressure
//      whatever the geometry looked like beforehand.
//
// The refusal names the element size that WOULD resolve it, which is what
// contact.interface_size_mm applies as LOCAL refinement on the candidate faces.
struct ContactResolution {
    double h_interface_mm = 0.0;   // measured interface element size
    double gap_mm = 0.0;           // the clearance it has to resolve
    double ratio = 0.0;            // h_interface_mm / gap_mm (0 when gap is 0)
    int candidate_pairs = 0;
    int seatable_pairs = 0;
    int active_pairs = -1;         // -1 until the solve has run
    int min_active_pairs = 0;      // the threshold that was applied
    double max_ratio = 0.0;        // the ratio threshold that was applied
    double contact_area_mm2 = 0.0; // tributary area of the load-carrying pairs
    // The element size at the interface that resolves the clearance. This is the
    // number to pass as contact.interface_size_mm.
    double required_interface_size_mm = 0.0;
    bool resolved = true;
    // force/area is a pressure only when a load-carrying patch of pairs exists.
    bool pressure_reliable = false;
    std::string reason;            // empty while resolved
};

inline constexpr double kContactMaxHOverGap = 4.0;
inline constexpr int kContactMinActivePairs = 6;

// A-priori resolution judgement on the candidate set (before any solve).
ContactResolution fea_contact_resolution(const ContactCandidateSet& candidates);

// Fold the solved active set into the judgement: `active_pairs` is the solver's own
// active count, `contact_force_n` the total resultant it carried and
// `contact_area_mm2` the tributary area of those active pairs. A refusal here is
// the a-posteriori clause; it never overturns an a-priori one.
void fea_contact_resolution_after_solve(ContactResolution& resolution,
                                        int active_pairs, double contact_force_n,
                                        double contact_area_mm2);

}  // namespace chudware::sim
