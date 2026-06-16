// Kernel-aware pick resolution — see picks.h for the WHY and the two-stage algorithm.
//
// This is a bit-for-bit C++ port of the resolution core of
// engine/doc/features/dress.py:
//   - _bbox_diag           -> bbox_diag
//   - _resolve_with_hint_fallback -> resolve_with_hint_fallback
//   - _nearest_by_centroid + the describe-space->unique-element mapping -> the
//     private nearest_unique_subshape helper + resolve_edge_picks.
//
// The describe-space index lives in the WIDER 24-occurrence space (one descriptor per
// incident face), so we never index the de-duplicated edge list directly; we map the
// resolved descriptor's centroid to the nearest unique edge by centroid distance.
#include "doc/picks.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <limits>
#include <tuple>
#include <utility>

#include <Bnd_Box.hxx>
#include <BRepBndLib.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <gp_Pnt.hxx>

namespace aicad::doc {

// _RESOLVE_MAP_TOL_FRAC — the map tolerance is a small fraction of the bounding-box
// diagonal (mirrors dress.py). The unique element should coincide with the descriptor
// essentially exactly (same geometric entity), so anything beyond tol means the
// resolve landed on geometry no unique element reproduces — reported as dangling.
constexpr double kResolveMapTolFrac = 1e-3;

namespace {

// Format a double like Python's f"{x:.4g}" (general format, 4 significant digits) for
// the no-unique-subshape reason strings.
std::string fmt4g(double x) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.4g", x);
    return std::string(buf);
}

// Centre of mass of a sub-shape, matching the describe convention: edges report the
// LinearProperties midpoint, faces the SurfaceProperties centroid (BRepGProp centre of
// mass). Mirrors dress.py::_mass_center.
gp_Pnt subshape_centroid(const TopoDS_Shape& sub, TopAbs_ShapeEnum kind) {
    GProp_GProps props;
    if (kind == TopAbs_FACE) {
        BRepGProp::SurfaceProperties(sub, props);
    } else {  // edges (and any 1D sub-shape)
        BRepGProp::LinearProperties(sub, props);
    }
    return props.CentreOfMass();
}

// Map a target centroid to the NEAREST UNIQUE sub-shape of `shape` of the given kind,
// within `tol`. `shape` is enumerated via TopExp::MapShapes (which de-duplicates, so a
// box yields its 12 unique edges, not 24 occurrences). Returns the nearest sub-shape on
// success; on failure (`out_dist` > tol or no sub-shapes) returns a null shape and sets
// `out_dist` to the nearest distance found (+inf when there were no candidates).
// Generic over TopAbs_ShapeEnum so a later increment can map face picks identically.
TopoDS_Shape nearest_unique_subshape(const TopoDS_Shape& shape, TopAbs_ShapeEnum kind,
                                     const Eigen::Vector3d& target, double tol,
                                     double& out_dist) {
    TopTools_IndexedMapOfShape uniq;
    TopExp::MapShapes(shape, kind, uniq);

    TopoDS_Shape best;
    double best_d2 = std::numeric_limits<double>::infinity();
    for (int i = 1; i <= uniq.Extent(); ++i) {
        const TopoDS_Shape& sub = uniq(i);
        gp_Pnt c = subshape_centroid(sub, kind);
        double dx = c.X() - target.x();
        double dy = c.Y() - target.y();
        double dz = c.Z() - target.z();
        double d2 = dx * dx + dy * dy + dz * dz;
        if (d2 < best_d2) {
            best_d2 = d2;
            best = sub;
        }
    }

    out_dist = std::isfinite(best_d2) ? std::sqrt(best_d2)
                                      : std::numeric_limits<double>::infinity();
    if (best.IsNull() || out_dist > tol) return TopoDS_Shape();
    return best;
}

// Normalise the `selectors` payload to a vector of selector objects (a single object or
// a json array), mirroring dress.py's `sel if isinstance(sel, list) else [sel]`.
std::vector<nlohmann::json> as_selector_list(const nlohmann::json& selectors) {
    std::vector<nlohmann::json> out;
    if (selectors.is_array()) {
        for (const auto& s : selectors) out.push_back(s);
    } else if (!selectors.is_null()) {
        out.push_back(selectors);
    }
    return out;
}

}  // namespace

// --------------------------------------------------------------------------- bbox_diag

double bbox_diag(const TopoDS_Shape& shape) {
    Bnd_Box box;
    BRepBndLib::Add(shape, box);
    if (box.IsVoid()) return 1.0;
    double xmin, ymin, zmin, xmax, ymax, zmax;
    box.Get(xmin, ymin, zmin, xmax, ymax, zmax);
    double dx = xmax - xmin, dy = ymax - ymin, dz = zmax - zmin;
    double d = std::sqrt(dx * dx + dy * dy + dz * dz);
    return d > 1e-12 ? d : 1.0;
}

// ----------------------------------------------------------- resolve_with_hint_fallback

std::optional<int> resolve_with_hint_fallback(const nlohmann::json& sel,
                                              const std::vector<TopoInfo>& infos,
                                              double diag) {
    ResolveResult rr = resolve(sel, infos, diag);
    if (rr.index.has_value()) return rr.index;

    // Apply hint_index fallback: only when a MARGIN failure and best score < 0.01. At
    // that point only IDENTICAL duplicates co-rank, so the reference is clearly correct
    // and just geometrically ambiguous (e.g. a box's two identical edge occurrences).
    bool margin_fail = rr.dangling_reason.has_value() &&
                       rr.dangling_reason->find("margin") != std::string::npos;
    if (!margin_fail || !(rr.score < 0.01)) return std::nullopt;

    const std::string kind = sel.value("kind", std::string());
    std::optional<std::string> s_owner;
    if (sel.contains("owner_feature") && !sel.at("owner_feature").is_null())
        s_owner = sel.at("owner_feature").get<std::string>();
    std::optional<int> s_hint;
    if (sel.contains("hint_index") && !sel.at("hint_index").is_null())
        s_hint = sel.at("hint_index").get<int>();

    // Same-kind candidates, owner-filtered with fallback to all same-kind (only when
    // owner is non-null and the filtered set is non-empty) — mirrors resolve().
    std::vector<const TopoInfo*> candidates;
    for (const TopoInfo& i : infos)
        if (i.kind == kind) candidates.push_back(&i);
    if (s_owner) {
        std::vector<const TopoInfo*> owner_set;
        for (const TopoInfo* i : candidates)
            if (i->owner == s_owner) owner_set.push_back(i);
        if (!owner_set.empty()) candidates = owner_set;
    }

    // Score, then sort by (score, hint_index) (the nearest-hint tiebreak).
    std::vector<std::pair<double, const TopoInfo*>> scored;
    scored.reserve(candidates.size());
    for (const TopoInfo* c : candidates)
        scored.emplace_back(score_candidate(sel, *c, diag), c);
    std::sort(scored.begin(), scored.end(),
              [](const std::pair<double, const TopoInfo*>& a,
                 const std::pair<double, const TopoInfo*>& b) {
                  return std::tie(a.first, a.second->hint_index) <
                         std::tie(b.first, b.second->hint_index);
              });

    if (scored.empty() || !(scored[0].first < 0.01)) return std::nullopt;

    // If hint_index is unspecified or matches the best after sort, take the best;
    // otherwise prefer the candidate whose hint_index matches; else the overall best.
    const TopoInfo* best_cand = scored[0].second;
    if (!s_hint.has_value() || best_cand->hint_index == *s_hint)
        return best_cand->index;
    for (const auto& sc : scored)
        if (sc.second->hint_index == *s_hint) return sc.second->index;
    return best_cand->index;
}

// --------------------------------------------------------------------- resolve_edge_picks

EdgePicks resolve_edge_picks(const TopoDS_Shape& shape, const nlohmann::json& selectors) {
    EdgePicks out;
    const std::vector<nlohmann::json> sel_list = as_selector_list(selectors);

    // describe_edges enumerates one descriptor per incident face (the wider 24-edge
    // space for a box); we map each resolved descriptor back to the nearest unique edge.
    const std::vector<TopoInfo> infos = describe_edges(shape);
    const double diag = bbox_diag(shape);
    const double tol = std::max(diag, 1.0) * kResolveMapTolFrac;

    for (std::size_t i = 0; i < sel_list.size(); ++i) {
        const nlohmann::json& sel = sel_list[i];
        std::optional<int> idx = resolve_with_hint_fallback(sel, infos, diag);
        if (!idx.has_value()) {
            ResolveResult rr = resolve(sel, infos, diag);
            std::string reason = rr.dangling_reason.value_or(std::string("unresolved"));
            out.dangling.push_back("edges[" + std::to_string(i) + "]: " + reason);
            continue;
        }
        // The resolved descriptor's centroid (edge midpoint) -> nearest unique edge.
        const Eigen::Vector3d& target = infos[*idx].centroid;
        double dist = std::numeric_limits<double>::infinity();
        TopoDS_Shape near = nearest_unique_subshape(shape, TopAbs_EDGE, target, tol, dist);
        if (near.IsNull()) {
            out.dangling.push_back(
                "edges[" + std::to_string(i) + "]: no unique edge within " +
                fmt4g(tol) + " mm of the resolved descriptor (nearest " +
                fmt4g(dist) + " mm)");
            continue;
        }
        out.edges.push_back(TopoDS::Edge(near));
    }

    return out;
}

}  // namespace aicad::doc
