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
#include <cstring>
#include <functional>
#include <string>
#include <vector>

#include "sim/fea_static.h"

namespace chudware::sim {

// ── The gap a contact study has to resolve is a property of the SURFACES ────────
// Everything else in this file is mesh topology, and for a FLAT interface the mesh
// gap and the real gap are the same number. On a CURVED fit they are not, and the
// difference is not small. A candidate is formed between two boundary NODES, so the
// gap it records is n̂·(p_b − p_a) = r_bore·cos(dtheta) − r_pin, where dtheta is
// however far apart the two meshes happened to sample the arc — up to a whole
// element of lateral offset, since that is the pairing tolerance. On a rover
// hinge (pin r = 6.000 in a saddle r = 6.300, a 0.300 mm clearance) the closest
// approach over 1899 candidate pairs read 0.03665 mm: linear facet sampling ate
// 87.8 % of the real clearance. The h/gap guard below then demanded an interface
// element size of 0.147 mm to resolve a gap that is really 0.300 mm wide, which no
// mesher on the machine could deliver, and the study was refused at every setting.
//
// So the clearance is measured against the ANALYTIC CAD face instead: the signed
// distance from the node to the other body's own surface, which is 0.300 mm at
// every node of a nested pair of cylinders however coarsely they are faceted. The
// facet value is kept beside it and the difference is reported (facet_error_mm) —
// it is the one direct measurement of what the discretization is doing to this
// interface, and it is what tells a reader whether the mesh describes the fit.
struct ContactGapProbe {
    virtual ~ContactGapProbe() = default;
    // Analytic clearance at this candidate pair (mm, signed like
    // ContactSpring::gap0_mm: positive = separated, negative = interpenetrating).
    // The node ids are passed so an implementation can memoize per node — a node
    // takes part in one or two pairs, and the underlying projection is the
    // expensive part.
    //
    // false means neither end can be attributed to a CAD solid — a repaired or
    // dissolved region, an inline mesh, a study with no STEP — which is the
    // caller's cue to keep the facet measurement for that pair and count the
    // nodes in `gap_fallback_nodes`.
    virtual bool pair_gap_mm(int node_a, const double* pa, int node_b,
                             const double* pb, double& gap_mm) const = 0;
    // Optional closest-surface direction from a toward b, for the support-cone
    // proof. It does not change the mesh-normal penalty spring law.
    virtual bool pair_normal(int, const double*, int, const double*,
                             std::array<double, 3>&) const { return false; }
};

enum class InterfaceKind { Contact, Bonded, Free };

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
    // The analytic-surface oracle (above). Null = measure the gaps on the mesh
    // facets alone, which is all an inline mesh or a mesher-free unit test has.
    const ContactGapProbe* gap_probe = nullptr;
    // Optional body-range lookup; returns min(E_a,E_b) for heterogeneous contact.
    std::function<double(int, int)> pair_modulus;
    // Explicit STEP solid ordinals; absent node lookup uses mesh component + 1
    // (inline meshes). Explicit pairs override the default, never each other.
    std::vector<std::array<int, 2>> bonded_pairs;
    std::vector<std::array<int, 2>> contact_pairs;
    InterfaceKind default_interface = InterfaceKind::Contact;
    // Negative: bond the measured interface gap within max_gap_mm. Otherwise
    // require the measured bonded gap to be at most this explicit tolerance.
    double bond_gap_mm = -1.0;
    std::function<int(int)> node_body_ordinal;
    // Named conformal-fit idealization, only for gaps measured on CAD surfaces.
    // Negative threshold selects one quarter of the measured interface size.
    bool closed_fit = true;
    double closed_fit_gap_mm = -1.0;
};

struct ContactCandidateSet {
    std::vector<ContactSpring> springs;
    std::vector<std::array<double, 3>> support_normals; // same order/orientation as springs
    int support_normal_analytic_pairs = 0;
    // Same geometric pairing as contact, but these ties carry all three components
    // of relative displacement, including tension, through the BondSpring kernel.
    std::vector<ContactSpring> bonded;
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
    // The candidate set's own gaps (mm, signed as ContactSpring::gap0_mm), measured
    // BEFORE the penetration clamp below: closest approach, and the median. When a
    // ContactGapProbe was supplied these are ANALYTIC gaps — the clearance of the
    // CAD surfaces, which is the clearance the guard, the seat limit and the
    // penetration accounting below all have to judge.
    double min_gap0_mm = 0.0;
    double median_gap0_mm = 0.0;
    // The same two statistics measured on the MESH FACETS (n̂·(p_b − p_a)), always.
    // With no probe they equal min_gap0_mm / median_gap0_mm exactly.
    double min_gap_facet_mm = 0.0;
    double median_gap_facet_mm = 0.0;
    // min_gap0_mm − min_gap_facet_mm: how much of the real clearance the facet
    // sampling of this interface consumed. 0 without a probe. It is a measurement,
    // not a defect count — a coarse curved fit legitimately has a large one — but
    // it is the number that says whether the mesh describes the fit at all (the
    // rover hinge: 0.2633 mm of a 0.300 mm clearance).
    double facet_error_mm = 0.0;
    // Pairs whose gap came from the probe, and the distinct NODES of the pairs that
    // had to fall back to the facet value because neither end could be attributed
    // to a CAD solid (mesh repair dissolved the region, or there is no CAD).
    int gap_analytic_pairs = 0;
    int gap_fallback_nodes = 0;
    // Candidates the mesh found within max_gap_mm but whose ANALYTIC clearance is
    // wider than it — the facet sampling had understated the gap. They are not
    // candidates: max_gap_mm is the caller's statement of what counts as contact,
    // so they are dropped and counted here rather than left to widen the median.
    int gap_beyond_max_pairs = 0;
    // Deepest initial PENETRATION that was clamped away (mm, positive). Left alone
    // it is a preload of k*|gap0| — on the live keyway 11 such pairs carried 56 kN
    // through a 0.019 mm^2 patch and reported 3.0e6 MPa of "contact pressure" for a
    // 1500 N load. Every spring's gap0_mm is therefore clamped at 0 and the depth
    // is reported here instead of being silently converted into force.
    //
    // What it MEANS depends on which measurement produced it. On the mesh facets a
    // negative gap is discretization error, not a press fit: the bodies were placed
    // touching (or curved, and sampled out of phase) and the projection put nodes
    // through the partner face — the r 6.000 in r 6.300 case of test_fea_contact
    // reads -0.247 mm on a fit that is 0.300 mm APART. Measured on the CAD faces it
    // is real: the two solids interfere, which is a modelling defect the study
    // cannot idealize away.
    double initial_penetration_max_mm = 0.0;
    // The gap below which a candidate SEATS (mm) — a length derived from the
    // interface, never a multiple of the closest approach. It used to be
    // 2*min_gap0_mm, which collapses to 0 the instant one node penetrates, so on a
    // nominally-touching fit only the penetrating pairs could seat and the
    // population was mesh luck (the live keyway seated 8, 12, 13 and then 2 pairs
    // across otherwise identical runs). See kSeatFractionH.
    double seat_limit_mm = 0.0;
    // Candidates whose initial gap is at or below seat_limit_mm — the pairs the mesh
    // cannot distinguish from touching, i.e. the geometric contact arc it resolved.
    // They are the active set the solve STARTS from; the active-set loop then seats
    // the rest as the bodies deform and releases any of these that ends tensile.
    // Where the clearance opens up faster than the mesh can resolve (a bore, a
    // keyway flank, an angled seat) this is however many pairs the discretization
    // placed in the closing zone, and a load funnelled through 1 of them is a point
    // load wearing a contact-pressure label.
    int seatable_pairs = 0;
    int closed_fit_pairs = 0;
    double closed_fit_gap_mm_max = 0.0;
    double closed_fit_limit_mm = 0.0;
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
//   1. A PRIORI, from the candidate set alone. WHICH statement depends on
//      whether there is a clearance at all:
//        * clearance (min gap > 0), criterion kCriterionClearance: the
//          interface element size is more than kContactMaxHOverGap x the
//          clearance AND fewer than kContactMinActivePairs candidates sit
//          within the seat limit (a LENGTH — see kSeatFractionH). Both halves
//          are required, and the second one is why: a CONFORMING interface
//          (two flat faces across a uniform 0.05 mm gap, or a concentric pin
//          in a 0.1 mm bore) has h/gap
//          of 19-133 and is perfectly resolved — every pair seats together and
//          the pressure is the real patch pressure. What kills a study is a
//          clearance that opens faster than one element, which shows up as a
//          closing zone the mesh placed almost no pairs in.
//        * TOUCHING or coincident faces (min gap <= 0), criterion
//          kCriterionPatchArea: there is no clearance, so h/gap is undefined
//          (it used to be reported as "clearance 0 mm" beside a ratio of 0,
//          which reads as a resolved interface no matter how coarse the mesh
//          is). The interface is judged on the PATCH instead: at least
//          kContactMinActivePairs pairs actually in contact AND at least
//          kContactMinPatchElems elements across the candidate patch,
//          h_interface <= sqrt(candidate_area) / kContactMinPatchElems.
//   2. A POSTERIORI: load crossed the interface through fewer than
//      kContactMinActivePairs pairs. Three pairs cannot carry a distributed
//      pressure whatever the geometry looked like beforehand.
//
// The refusal names the element size that WOULD resolve it, which is what
// contact.interface_size_mm applies as LOCAL refinement on the candidate faces.
struct ContactResolution {
    double h_interface_mm = 0.0;   // measured interface element size
    // The clearance it has to resolve (0 = touching). ANALYTIC — the CAD surfaces'
    // own clearance — whenever a ContactGapProbe was available (ContactGapProbe
    // above). `gap_facet_mm` is the same closest approach measured on the mesh
    // facets and `facet_error_mm` the difference: quoting both is what lets a
    // reader tell a genuinely tight fit from a coarsely faceted one.
    double gap_mm = 0.0;
    double gap_facet_mm = 0.0;
    double facet_error_mm = 0.0;
    // False when no probe answered for any pair, i.e. gap_mm IS gap_facet_mm.
    bool gap_analytic = false;
    // Distinct nodes whose pair had no CAD face to measure against and kept the
    // facet value (repaired/dissolved mesh regions).
    int gap_fallback_nodes = 0;
    double ratio = 0.0;            // h_interface_mm / gap_mm (0 under kCriterionPatchArea)
    int candidate_pairs = 0;
    int seatable_pairs = 0;
    // The length that decided `seatable_pairs` (mm) — see kSeatFractionH — and the
    // deepest initial penetration the placement/facet error produced (mm, positive),
    // which was clamped to a touching gap instead of becoming k*penetration of
    // preload.
    double seat_limit_mm = 0.0;
    double initial_penetration_max_mm = 0.0;
    int active_pairs = -1;         // -1 until the solve has run
    int min_active_pairs = 0;      // the threshold that was applied
    double max_ratio = 0.0;        // the ratio threshold (0 under kCriterionPatchArea)
    double contact_area_mm2 = 0.0; // tributary area of the load-carrying pairs
    // Tributary area of every candidate pair — the patch the interface has to
    // resolve when there is no clearance to resolve (see `criterion`).
    double candidate_area_mm2 = 0.0;
    // The coarsest interface element size the PATCH criterion admits:
    // sqrt(candidate_area_mm2) / kContactMinPatchElems. 0 under
    // kCriterionClearance.
    double max_h_interface_mm = 0.0;
    // Which of the two a-priori criteria was applied. A clearance can only be
    // judged against the element size that has to resolve it when there IS a
    // clearance: two touching or coincident faces (gap <= 0) have none, the
    // ratio is undefined, and the interface is judged on how many elements span
    // the candidate patch instead.
    const char* criterion = nullptr;
    // The element size at the interface that resolves the clearance (or the
    // patch). This is the number to pass as contact.interface_size_mm.
    double required_interface_size_mm = 0.0;
    int closed_fit_pairs = 0;
    double closed_fit_gap_mm_max = 0.0;
    double closed_fit_limit_mm = 0.0;
    bool resolved = true;
    // force/area is a pressure only when a load-carrying patch of pairs exists.
    bool pressure_reliable = false;
    std::string reason;            // empty while resolved
    // ── Load path: does the load actually TRAVEL through this interface? ────────
    // A resolved, well-seated patch can still be an answer to nothing. On the live
    // keyway isolate both bodies were grounded (the fixture face-set captured the
    // mating bore too), so 6 active pairs carried 9.9 N of a 1500 N applied load:
    // a pressure, in the sense that force/area is a number, but not the seat
    // pressure anybody asked for. `applied_load_n` is the resultant of the applied
    // loads on the bodies that are held ONLY by contact, `carried_load_n` the total
    // contact resultant across the active pairs, and `load_fraction` their quotient
    // (0 when there is nothing to compare against). The interface is the load path
    // only when that fraction reaches kMinLoadFraction.
    double applied_load_n = 0.0;
    double carried_load_n = 0.0;
    double load_fraction = 0.0;
    bool contact_carries_load = false;
    // Why the load path is not proven — empty exactly when it is. Populated for
    // both non-carrying shapes: the redundant interface (nothing is held by
    // contact) and the under-carrying one (a fraction below kMinLoadFraction).
    std::string load_path_note;
};

inline constexpr double kContactMaxHOverGap = 4.0;
inline constexpr int kContactMinActivePairs = 6;
// Elements across the candidate patch required when the faces touch (gap <= 0):
// h_interface <= sqrt(candidate_area) / 3, i.e. three elements across a square
// patch of the same area. Below that a "pressure" is one or two nodes wide.
inline constexpr double kContactMinPatchElems = 3.0;
// ── The seat limit: a LENGTH, not a multiple of the closest approach ────────────
// Which candidates can SEAT is the question that decides whether a contact study
// has a patch or a point load, and it used to be answered with 2*min_gap0_mm — a
// multiple of the interface's closest approach. On a nominally-touching fit that
// closest approach is 0 or slightly negative (the bodies were placed touching and
// the mesh puts a node or two through the partner face), the limit collapses to 0,
// and only the pairs the discretization happened to push through could seat: the
// live keyway seated 8, 12, 13 and then 2 pairs across otherwise identical runs of
// the same request, and at 2 the study was refused as a point load.
//
// The limit is now a length the interface itself supplies:
//
//   seat_limit = max(kSeatFractionH * h_interface,
//                    kSeatFractionGap * max_gap_mm, kSeatMinAbsMm)
//
// HALF the interface element size is the resolution statement: the elements that
// bound the interface cannot describe a gap finer than that (on a curved fit the
// facet chord error and the node-snap residual are of exactly that order), so a
// candidate below it is indistinguishable from touching and seats. The max_gap
// term is the floor for a mesh whose interface elements are finer than the
// clearance the caller declared as contact, and the absolute term keeps a
// degenerate interface from producing a zero-length limit.
//
// Measured on the FRAME-PEDAL-BRAKE keyway (h_interface 0.2535 mm, max_gap 0.4 mm):
// 0.127 mm, i.e. the geometric arc of a 0.352 mm eccentric fit out to ~50 degrees
// from the tangency line, thousands of pairs instead of a knife edge.
inline constexpr double kSeatFractionH = 0.5;
inline constexpr double kSeatFractionGap = 0.25;
inline constexpr double kSeatMinAbsMm = 1e-4;
// The two a-priori criteria, reported verbatim in the resolution block.
inline constexpr const char* kCriterionClearance = "h_over_clearance";
inline constexpr const char* kCriterionPatchArea = "elements_across_patch";
// Compare criteria by text, never by pointer: across shared-library boundaries (the macOS
// release test binaries vs libchudware) the same literal has distinct addresses.
inline bool criterion_is(const char* criterion, const char* expected) {
    return criterion != nullptr && expected != nullptr && std::strcmp(criterion, expected) == 0;
}
// ── The load-path bar ──────────────────────────────────────────────────────────
// Half the applied resultant. A contact interface that carries less than this of
// what is applied to the body it is supposed to hold is a parallel stiffness path,
// not the load path, and its "pressure" answers a different question than the one
// asked. Half is deliberately generous: it admits the honest cases where a real
// seat shares load with a second interface or a bolted tie, and still refuses the
// 9.9-of-1500-N shape (fraction 0.0066) that a doubly-grounded fixture produces.
inline constexpr double kMinLoadFraction = 0.5;
// Below this the applied resultant on the contact-held bodies is treated as
// absent rather than small: there is no load to trace, so no fraction to judge.
inline constexpr double kLoadPathMinAppliedN = 1e-9;

// Necessary static equilibrium condition, before any numerical tangential
// stabilizer can manufacture an opposing reaction. Refusal is a proof; passing
// this axis-separation test is not proof that the complete normal cone contains
// the load. Bond-connected floating clusters must be considered together by the
// caller, not mistaken for individually contact-held bodies.
struct ContactSupportCone {
    bool excludes_load = false;
    int separating_axis = -1;
    int group = -1;
    int pairs = 0;
    std::array<double, 3> normal_min{}, normal_max{}, load_direction{};
    double supported_arc_deg = 0.0;
};
ContactSupportCone fea_contact_support_cone(const ContactCandidateSet& candidates,
                                           int component,
                                           const std::array<double, 3>& applied_force);

// A-priori resolution judgement on the candidate set (before any solve).
ContactResolution fea_contact_resolution(const ContactCandidateSet& candidates);

// Fold the solved active set into the judgement: `active_pairs` is the solver's own
// active count, `contact_force_n` the total resultant it carried and
// `contact_area_mm2` the tributary area of those active pairs. A refusal here is
// the a-posteriori clause; it never overturns an a-priori one.
void fea_contact_resolution_after_solve(ContactResolution& resolution,
                                        int active_pairs, double contact_force_n,
                                        double contact_area_mm2);

// Fold the LOAD PATH into the judgement — call this after
// fea_contact_resolution_after_solve, which owns the patch clause.
// `applied_load_n` is the resultant of the applied loads on the components held
// ONLY by contact, `carried_load_n` the total contact resultant across the active
// pairs, and `held_by_contact_only` false when every body is grounded on its own.
//
// Two non-carrying shapes, and they are not the same failure:
//   * NOTHING IS HELD BY CONTACT. Every body reaches a fixture without the
//     interface, so the interface is a REDUNDANT path. The study is not wrong —
//     it is an answer about a parallel spring — so this does not refuse it; it
//     sets contact_carries_load false, clears pressure_reliable, and says so in
//     load_path_note.
//   * THE LOAD DOES NOT CROSS. A body IS held only by contact, carries an applied
//     resultant, and the interface picks up less than kMinLoadFraction of it. That
//     is a broken model (over-constrained fixtures, or an interface that separated)
//     dressed as a converged answer, so it REFUSES: resolved false with a reason.
void fea_contact_resolution_load_path(ContactResolution& resolution,
                                      double applied_load_n, double carried_load_n,
                                      bool held_by_contact_only);

}  // namespace chudware::sim
