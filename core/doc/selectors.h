// Selectors — stable geometric references into B-rep topology (C++ port of the
// Python oracle engine/doc/selectors.py; implements docs/contracts/selectors.md).
//
// WHY this layer exists:
//   A document references "that face" / "that edge" across rebuilds. Persistent
//   *naming* is brittle (topology-splitting edits invalidate ids), so we instead
//   record a small GEOMETRIC fingerprint at pick time (kind, surface/curve type,
//   centroid, axis, area/length, owner-feature, hint ordinal) and, on every
//   rebuild, RE-RESOLVE that fingerprint against the freshly enumerated topology
//   with a scored best-match. The match is documented honestly: when two
//   candidates are symmetric the margin rule REFUSES to guess (dangling-ref
//   warning) rather than silently pick the wrong one. This is the foundation the
//   pattern/mirror/hole/fillet-by-pick feature compilers build on.
//
// WHY two halves (describers vs. resolve):
//   The OCCT describers (describe_faces/edges/vertices) turn a live kernel shape
//   into kernel-free TopoInfo descriptors. The resolution algorithm (make_selector
//   + resolve) is then PURE — no OCCT — so contract §3/§5 is unit-testable without
//   a kernel, exactly as the Python keeps TopoInfo decoupled from OCP. The C++
//   scoring + accept/reject logic mirrors selectors.py::_score_candidate / resolve
//   bit-for-bit (weights 0.4/0.3/0.2/0.1 with-axis, 0.55/0.3/0.15 without; accept
//   iff best<0.35 AND (second-best)>0.05; tiebreak by nearest hint_index).
//
// See docs/contracts/selectors.md (§1 object, §2 creation, §3 normative algorithm,
// §5 torture suite) and engine/doc/selectors.py (the oracle this ports).
#pragma once

#include <map>
#include <optional>
#include <string>
#include <vector>

#include <Eigen/Dense>
#include <nlohmann/json.hpp>

class TopoDS_Shape;

namespace aicad::doc {

// Lightweight topology descriptor (port of selectors.py TopoInfo). Decoupled from
// OCCT so the resolution algorithm is testable without a kernel.
struct TopoInfo {
    std::string kind;                       // "face" | "edge" | "vertex"
    int index = 0;                          // 0-based enumeration ordinal (stable per describe)
    std::optional<std::string> surf;        // face surf / edge curve type string; nullopt for vertex
    Eigen::Vector3d centroid{0, 0, 0};      // mm, shape-space
    std::optional<Eigen::Vector3d> axis;    // unit vector or nullopt
    std::optional<double> measure;          // face area / edge length; nullopt for vertex
    std::optional<std::string> owner;       // owner feature id, or nullopt
    int hint_index = -1;                    // -1 => equals index (set in describers)
    // I3 — REPRESENTATIVE OUTWARD DIRECTION at the face centroid, for EVERY face (unlike
    // `axis`, which describe_faces sets only for plane/cylinder/cone). Used by the side
    // filter so a directional query (top/front/...) can rank a curved face by which way it
    // faces, not just by centroid height. plane = signed outward normal; cylinder/cone =
    // radial-outward at the centroid; sphere/torus/bspline = (centroid - body bbox center).
    // nullopt for edges/vertices and any degenerate face. NOT serialised by make_selector
    // (it is a transient ranking aid, not part of the persisted selector fingerprint).
    std::optional<Eigen::Vector3d> rep_dir;
};

struct ResolveResult {
    std::optional<int> index;               // accepted candidate's TopoInfo.index, or nullopt = dangling
    double score = 0.0;                     // best score (lower better); +inf if no candidates
    double second_score = 0.0;              // second-best, +inf if <2 candidates
    std::optional<std::string> dangling_reason;  // nullopt iff resolved
};

// --- OCCT describers (enumerate sub-shapes -> descriptors) ---
// surf/curve type strings per contract: faces plane|cylinder|cone|sphere|torus|
// bspline|other; edges line|circle|ellipse|bspline|other. axis from the adaptor
// (plane normal, cylinder/cone/circle/ellipse axis, line direction). centroid+measure
// via BRepGProp. `owners` maps a 0-based enumeration index -> owner feature id
// (empty => all nullopt).
std::vector<TopoInfo> describe_faces(const TopoDS_Shape& shape,
                                     const std::map<int, std::string>& owners = {});
std::vector<TopoInfo> describe_edges(const TopoDS_Shape& shape,
                                     const std::map<int, std::string>& owners = {});
std::vector<TopoInfo> describe_vertices(const TopoDS_Shape& shape,
                                        const std::map<int, std::string>& owners = {});

// --- pure resolution (no OCCT) ---
// Build a selector json dict from a descriptor (port of make_selector): keys kind,
// surf, centroid[3], axis[3]|null, area_or_len|null, owner_feature|null, hint_index.
nlohmann::json make_selector(const TopoInfo& info);

// Build a selector for a host PICK: the sub-shape of `kind` ("face"/"edge"/"vertex")
// at the 1-based TopExp::MapShapes index `topo_id` (the same numbering the AICM
// tessellator + owner_map use, so it matches the host's pick ids). `owner` is the
// owning feature id (or nullopt). Returns null if topo_id is out of range. Used by
// doc_resolve_pick to turn a viewport pick into a re-resolvable selector.
nlohmann::json make_pick_selector(const TopoDS_Shape& shape, const std::string& kind,
                                  int topo_id, std::optional<std::string> owner);

// Resolve a selector against candidates per contract §3 (verbatim from selectors.py).
ResolveResult resolve(const nlohmann::json& selector,
                      const std::vector<TopoInfo>& candidates, double bbox_diag);

// Score candidate against `selector` per contract §3 (lower is better). Port of
// selectors.py::_score_candidate. Exposed (vs. the resolve()-internal scorer) so the
// kernel-aware pick resolution (doc/picks.cpp) can re-score the same-kind candidates
// for the hint_index fallback exactly as the Python oracle does.
double score_candidate(const nlohmann::json& selector, const TopoInfo& candidate,
                       double bbox_diag);

}  // namespace aicad::doc
