// OCCT dress-up operators: fillet (round) + chamfer (bevel) on a solid's edges.
// C++ port of the edge-path core of engine/doc/features/dress.py. These are the
// finishing ops that soften a part: rounding the four vertical edges of a block,
// breaking a sharp rim with a chamfer. Together with the boolean ops they turn a raw
// extrude into a manufacturable, SolidWorks-class part.
//
// WHY a separate translation unit (not part of features.cpp):
//   Same rationale as booleans.cpp — features.cpp makes primitives, dress.cpp MODIFIES
//   a finished solid by blending/beveling its edges. Mirrors the Python split
//   (dress.py is its own compiler) and keeps each builder a clean shape->shape function.
//
// EDGE SELECTION (`dir` filter):
//   With no `dir` the op rounds/bevels EVERY edge of the solid. With a `dir` it selects
//   only STRAIGHT edges whose line direction is parallel to `dir` (either orientation) —
//   e.g. dir=+Z picks the four vertical edges of a box but leaves the eight horizontal
//   rim edges alone. This is the kernel-level analogue of the doc model's edge selectors:
//   "fillet the vertical edges" without per-edge picking. Non-line edges are skipped
//   when a `dir` filter is active (a curved edge has no single direction).
//
// EDGE ENUMERATION GOTCHA:
//   A raw TopExp_Explorer over TopAbs_EDGE visits each shared edge ONCE PER INCIDENT
//   FACE — so a box yields 24 edge hits for its 12 unique edges (dress.py documents the
//   exact same trap). We therefore enumerate via TopExp::MapShapes into a
//   TopTools_IndexedMapOfShape, which de-duplicates: each unique edge is added to the
//   MakeFillet/MakeChamfer builder exactly once.
//
// Fillets return a classified, all-selected-edges result; chamfer failures are
// contained at the API boundary and retain their existing best-effort semantics.
#pragma once

#include <optional>
#include <string>
#include <vector>

#include <Eigen/Dense>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Shape.hxx>

namespace geom {

enum class FilletFailure {
    none,
    invalid_input,
    invalid_selection,
    unsupported_topology,
    construction_failed,
    invalid_geometry,
    isolation_failed,
    timed_out,
    budget_exhausted,
    unsupported_precision
};

enum class FilletValidationStatus { valid, rejected, timed_out };

struct FilletValidationResult {
    FilletValidationStatus status = FilletValidationStatus::valid;
    std::string reason;
};

// An isolated probe executes a named policy, not a callback inherited from a
// multithreaded host. The document supplies its matching function for explicitly
// in-process builds; the worker links the same implementation. A basic_solid
// callback is permitted only in-process; strict isolation rejects it rather
// than silently ignoring it. A timeout stops the search, not a radius bound;
// an earlier fully validated candidate remains usable.
enum class FilletValidationPolicy { basic_solid, document_solid };
using FilletValidationFn = FilletValidationResult (*)(const TopoDS_Shape&);
struct FilletValidation {
    FilletValidationPolicy policy = FilletValidationPolicy::basic_solid;
    FilletValidationFn in_process = nullptr;
};

struct FilletResult {
    TopoDS_Shape shape;  // null on failure; never a partial result
    double requested_radius = 0.0;
    std::optional<double> requested_radius2;
    double effective_radius = 0.0;
    std::optional<double> effective_radius2;
    int selected_edges = 0;
    int covered_edges = 0;
    int probes = 0;
    double tolerance_mm = 0.0;
    FilletFailure failure = FilletFailure::none;
    std::string reason;
};

// On the requested-radius fast path exactly one build is attempted. Otherwise a
// bounded search finds a verified all-edge candidate. The tested tolerance is an
// observed search resolution, not a guarantee of a global optimum: OCCT feasibility
// can be non-monotonic. For unequal variable endpoints, each is improved toward
// its request while keeping the other fixed, repeating in stable endpoint order
// when the other changes. Equal requested endpoints remain equal. This preserves a
// requested endpoint when possible; it does not claim a globally nearest pair.
// All radii and tolerance are millimetres.
// Search makes at most 72 probes under a 30 s aggregate budget, with at most
// 5 s per isolated probe; on platforms or configurations using in-process
// builds an individual native call has no hard wall-clock deadline. On timeout
// or budget exhaustion after a verified candidate, the best candidate is
// returned with a conservative resolution instead of discarding it.
// Finite nonpositive radii are searched from model/kernel tolerances, not a fixed floor.
// Variable endpoints must also exceed the relevant source and generated B-rep
// tolerances; the check certifies resolvability, not a measured section radius.
//
// Implicit selection enumerates blendable edges (optionally parallel to dir).
// Explicit picks must be unique edges of shape; seams, degenerates, unsupported
// topology, and missing or unmappable picks are classified failures. Straight and
// circular contact joins are supported for constant fillets; each selected
// contact's two source bodies and every constructed corner must remain in the
// same result solid, including after any ordinary selected edges are blended.
// A contact is classified as volumetrically overlapping only when the Boolean
// intersection establishes material above the local topology resolution.
// A shallow, straight C0 crease rejected as tangent by OCCT can be rounded by
// an exact planar-arc sweep when both support faces are constant along the edge.
// This path is limited to a single solid, one such edge, constant radius and
// a local material-removing blend; unsupported cases fail without a partial result.
// Variable contact joins, conflicting radii at a shared contour vertex and unequal
// endpoints on a closed edge are explicitly unsupported. Positive unequal
// endpoints within OCCT's radius resolution are unsupported when both are
// individually resolvable; an unresolvable endpoint instead enters correction.
// OCCT interpolates along the contour spine; linear interpolation is not promised.
FilletResult fillet_edges(const TopoDS_Shape& shape, double radius,
                          std::optional<Eigen::Vector3d> dir = std::nullopt,
                          std::optional<double> radius2 = std::nullopt,
                          const FilletValidation& validate = {});

FilletResult fillet_edges(const TopoDS_Shape& shape, double radius,
                          const std::vector<TopoDS_Edge>& edges,
                          std::optional<double> radius2 = std::nullopt,
                          const FilletValidation& validate = {});

// Entry point for the dedicated executable, not a public geometry operation.
int fillet_worker_main(int argc, char** argv, FilletValidationFn document_validate);

// Bevel the edges of `shape` with a symmetric setback `d` (BRepFilletAPI_MakeChamfer +
// Add(d, edge)). Same `dir` selection rule as fillet_edges. Empty shape on kernel
// failure.
TopoDS_Shape chamfer_edges(const TopoDS_Shape& shape, double d,
                           std::optional<Eigen::Vector3d> dir = std::nullopt);

// Explicit-edge chamfer overloads retain best-effort edge dropping.
TopoDS_Shape chamfer_edges(const TopoDS_Shape& shape, double d,
                           const std::vector<TopoDS_Edge>& edges, int* n_dropped = nullptr);

// Variable-radius fillets use radius2 on fillet_edges above; the requested
// endpoint law is never silently replaced by a constant-radius fillet.

// Two-distance (asymmetric) chamfer: leg `d1` on the first incident face, `d2` on the
// other (BRepFilletAPI_MakeChamfer::Add(d1, d2, edge, face)). Matches build123d
// chamfer(length=d1, length2=d2). Edges the kernel can't bevel at this size are
// dropped (counted in `n_dropped`) instead of failing the whole feature.
// Empty shape on total failure / empty `edges`.
TopoDS_Shape chamfer_edges_two_distance(const TopoDS_Shape& shape, double d1, double d2,
                                        const std::vector<TopoDS_Edge>& edges,
                                        int* n_dropped = nullptr);

// Distance-angle chamfer: setback `d` is measured on the reference face and
// `angle_rad` is the angle to that face. If no face is given, each edge uses
// its first adjacent face in OCCT's stable topology traversal order.
TopoDS_Shape chamfer_edges_distance_angle(const TopoDS_Shape& shape, double d,
                                          double angle_rad,
                                          const std::vector<TopoDS_Edge>& edges,
                                          const std::optional<TopoDS_Face>& reference_face = std::nullopt,
                                          int* n_dropped = nullptr);

// Cut off each selected three-edge convex corner with a triangular face at a
// setback `d` along its incident straight edges. Failure details go to `diag`.
TopoDS_Shape chamfer_vertices(const TopoDS_Shape& shape, double d,
                              const std::vector<TopoDS_Vertex>& vertices,
                              std::string* diag = nullptr);

// --- shell (hollow to a wall) + draft (taper faces) — port of dress.py/draft.py ---

// Hollow `body` to a wall of thickness `t`. `remove` faces become the open mouths of
// the shell; `outward` thickens outward (+t) instead of the SolidWorks default inward
// (-t).
//   OPEN shell (`remove` non-empty): BRepOffsetAPI_MakeThickSolid::MakeThickSolidByJoin
//     opens the removed faces and offsets the remaining skin (Arc join first, then an
//     Intersection join + looser tolerance for curved/B-spline skins). When no offset
//     strategy validates and the shell is inward, geom::shell_by_sections
//     (shell_sections.h) hollows a tube-like body by lofting its exact eroded
//     cross-sections instead — the path a rectangle-to-circle loft takes, since OCCT's
//     offset refuses its corner seams (BRepOffset_MixedConnectivity).
//   CLOSED shell (`remove` EMPTY = a fully-sealed hollow part, e.g. a hollow ball /
//     sealed enclosure): MakeThickSolidByJoin no-ops with no faces to open (it hands
//     back the original solid), so this case instead builds an offset solid
//     (PerformByJoin by ±t) and BOOLEAN-CUTs it against the original to leave a closed
//     wall of thickness `t` (a 40^3 box at t=2 -> 40^3-36^3 = 17344, sealed cavity).
// Every strategy's result is ACCEPTED only when it is a BRepCheck-valid, watertight solid
// whose volume moved the right way (inward: smaller, outward: larger); otherwise the next
// join/tolerance strategy is tried. When none validates, a valid result whose volume did
// NOT move (the OCCT "thickness too large" no-op, Bug A2/A3) is returned so the caller's
// volume guard names it; anything else returns an empty shape. `diag` (optional) receives
// the precise kernel reason per strategy — the BRepOffset_Error name (e.g.
// MixedConnectivity, with the offending crease-to-tangent edges located) or the
// validation that rejected the result.
TopoDS_Shape shell_solid(const TopoDS_Shape& body, double t, bool outward,
                         const std::vector<TopoDS_Face>& remove,
                         std::string* diag = nullptr);

// True if `shape` has any freeform (B-spline/Bezier/revolution/extrusion) face — the
// surface types a loft/sweep/revolve makes, on which the thick-solid offset often
// fails. Lets the shell feature emit an actionable error instead of "empty shape".
bool has_freeform_faces(const TopoDS_Shape& shape);

// Taper the given `faces` of `body` by `angle_rad` about the neutral plane
// (`neutral_pt`, `neutral_n`), pulled along `pull` (BRepOffsetAPI_DraftAngle::Add per
// face). Each face tilts about the line where it meets the neutral plane. Faces OCCT
// cannot draft about this plane (e.g. caps parallel to it) are SKIPPED so a valid
// subset still drafts; returns an empty shape only when NO face is draftable or the
// kernel build fails, with the reason written to `*diag` (if non-null) for an
// actionable error. (neutral_plane mode only; parting_line/step are deferred.)
TopoDS_Shape draft_faces(const TopoDS_Shape& body, const std::vector<TopoDS_Face>& faces,
                         const Eigen::Vector3d& neutral_pt, const Eigen::Vector3d& neutral_n,
                         const Eigen::Vector3d& pull, double angle_rad,
                         std::string* diag = nullptr);

}  // namespace geom
