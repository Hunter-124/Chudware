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
#include <cctype>
#include <cmath>
#include <cstdio>
#include <limits>
#include <tuple>
#include <utility>

#include <Bnd_Box.hxx>
#include <BRepBndLib.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <Standard_Failure.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <gp_Dir.hxx>
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

// --------------------------------------------------------------------- resolve_face_picks

FacePicks resolve_face_picks(const TopoDS_Shape& shape, const nlohmann::json& selectors,
                             const std::string& slot) {
    FacePicks out;
    const std::vector<nlohmann::json> sel_list = as_selector_list(selectors);

    // A solid's faces enumerate once each (describe_faces is de-duplicated), so face
    // resolution is unambiguous; we still route through the same hint-fallback +
    // nearest-unique-face mapping as edges for a single, consistent code path.
    const std::vector<TopoInfo> infos = describe_faces(shape);
    const double diag = bbox_diag(shape);
    const double tol = std::max(diag, 1.0) * kResolveMapTolFrac;

    for (std::size_t i = 0; i < sel_list.size(); ++i) {
        const nlohmann::json& sel = sel_list[i];
        std::optional<int> idx = resolve_with_hint_fallback(sel, infos, diag);
        if (!idx.has_value()) {
            ResolveResult rr = resolve(sel, infos, diag);
            std::string reason = rr.dangling_reason.value_or(std::string("unresolved"));
            out.dangling.push_back(slot + "[" + std::to_string(i) + "]: " + reason);
            continue;
        }
        // The resolved descriptor's centroid (face centre) -> nearest unique face.
        const Eigen::Vector3d& target = infos[*idx].centroid;
        double dist = std::numeric_limits<double>::infinity();
        TopoDS_Shape near = nearest_unique_subshape(shape, TopAbs_FACE, target, tol, dist);
        if (near.IsNull()) {
            out.dangling.push_back(
                slot + "[" + std::to_string(i) + "]: no unique face within " +
                fmt4g(tol) + " mm of the resolved descriptor (nearest " +
                fmt4g(dist) + " mm)");
            continue;
        }
        out.faces.push_back(TopoDS::Face(near));
    }

    return out;
}

// --------------------------------------------------------------------------- geom query

namespace {

std::string q_curve_type(GeomAbs_CurveType t) {
    switch (t) {
        case GeomAbs_Line:         return "line";
        case GeomAbs_Circle:       return "circle";
        case GeomAbs_Ellipse:      return "ellipse";
        case GeomAbs_BSplineCurve: return "bspline";
        default:                   return "other";
    }
}
std::string q_surf_type(GeomAbs_SurfaceType t) {
    switch (t) {
        case GeomAbs_Plane:          return "plane";
        case GeomAbs_Cylinder:       return "cylinder";
        case GeomAbs_Cone:           return "cone";
        case GeomAbs_Sphere:         return "sphere";
        case GeomAbs_Torus:          return "torus";
        case GeomAbs_BSplineSurface: return "bspline";
        default:                     return "other";
    }
}

// One unique sub-shape's geometric fingerprint (the same fields the describers compute).
struct QCand {
    TopoDS_Shape shp;
    Eigen::Vector3d centroid{0, 0, 0};
    std::optional<Eigen::Vector3d> axis;
    std::string surf;
    double measure = 0.0;
};

// Parse a signed-axis token ("+z","-x",...) into a unit direction; nullopt if invalid.
std::optional<Eigen::Vector3d> parse_side(const std::string& s) {
    if (s.size() < 2) return std::nullopt;
    double sign = (s[0] == '+') ? 1.0 : (s[0] == '-') ? -1.0 : 0.0;
    if (sign == 0.0) return std::nullopt;
    char ax = static_cast<char>(std::tolower(static_cast<unsigned char>(s[1])));
    Eigen::Vector3d v(0, 0, 0);
    if (ax == 'x') v.x() = sign;
    else if (ax == 'y') v.y() = sign;
    else if (ax == 'z') v.z() = sign;
    else return std::nullopt;
    return v;
}

std::optional<Eigen::Vector3d> json_vec3(const nlohmann::json& j) {
    if (!j.is_array() || j.size() < 3) return std::nullopt;
    return Eigen::Vector3d(j[0].get<double>(), j[1].get<double>(), j[2].get<double>());
}

}  // namespace

bool is_geom_query(const nlohmann::json& v) {
    return v.is_object() && (v.value("kind", std::string()) == "query" || v.contains("of"));
}

QueryPicks resolve_geom_query(const TopoDS_Shape& shape, const nlohmann::json& q) {
    QueryPicks out;
    const std::string of = q.value("of", std::string());
    const bool want_face = (of == "face");
    if (of != "edge" && of != "face") {
        out.error = "geom query: 'of' must be \"edge\" or \"face\" (got '" + of + "')";
        return out;
    }
    const TopAbs_ShapeEnum kind = want_face ? TopAbs_FACE : TopAbs_EDGE;
    const double diag = bbox_diag(shape);
    const double tol = std::max(diag * 1e-3, 1e-6);

    // 1) enumerate UNIQUE sub-shapes + fingerprint each.
    TopTools_IndexedMapOfShape uniq;
    TopExp::MapShapes(shape, kind, uniq);
    std::vector<QCand> cands;
    for (int i = 1; i <= uniq.Extent(); ++i) {
        QCand c;
        c.shp = uniq(i);
        GProp_GProps props;
        if (want_face) {
            TopoDS_Face f = TopoDS::Face(c.shp);
            BRepGProp::SurfaceProperties(f, props);
            BRepAdaptor_Surface ad(f);
            GeomAbs_SurfaceType st = ad.GetType();
            c.surf = q_surf_type(st);
            try {
                if (st == GeomAbs_Plane) c.axis = Eigen::Vector3d(ad.Plane().Axis().Direction().X(),
                    ad.Plane().Axis().Direction().Y(), ad.Plane().Axis().Direction().Z());
                else if (st == GeomAbs_Cylinder) c.axis = Eigen::Vector3d(ad.Cylinder().Axis().Direction().X(),
                    ad.Cylinder().Axis().Direction().Y(), ad.Cylinder().Axis().Direction().Z());
                else if (st == GeomAbs_Cone) c.axis = Eigen::Vector3d(ad.Cone().Axis().Direction().X(),
                    ad.Cone().Axis().Direction().Y(), ad.Cone().Axis().Direction().Z());
            } catch (const Standard_Failure&) { c.axis = std::nullopt; }
        } else {
            TopoDS_Edge e = TopoDS::Edge(c.shp);
            BRepGProp::LinearProperties(e, props);
            BRepAdaptor_Curve ad(e);
            GeomAbs_CurveType ct = ad.GetType();
            c.surf = q_curve_type(ct);
            try {
                if (ct == GeomAbs_Line) c.axis = Eigen::Vector3d(ad.Line().Direction().X(),
                    ad.Line().Direction().Y(), ad.Line().Direction().Z());
                else if (ct == GeomAbs_Circle) c.axis = Eigen::Vector3d(ad.Circle().Axis().Direction().X(),
                    ad.Circle().Axis().Direction().Y(), ad.Circle().Axis().Direction().Z());
            } catch (const Standard_Failure&) { c.axis = std::nullopt; }
        }
        c.measure = props.Mass();
        gp_Pnt cm = props.CentreOfMass();
        c.centroid = Eigen::Vector3d(cm.X(), cm.Y(), cm.Z());
        cands.push_back(std::move(c));
    }

    // 2) boolean filters: surf, parallel, perpendicular.
    std::vector<const QCand*> keep;
    keep.reserve(cands.size());
    const std::string surf_f = q.value("surf", std::string());
    std::optional<Eigen::Vector3d> par = q.contains("parallel") ? json_vec3(q["parallel"]) : std::nullopt;
    std::optional<Eigen::Vector3d> per = q.contains("perpendicular") ? json_vec3(q["perpendicular"]) : std::nullopt;
    if (par) par->normalize();
    if (per) per->normalize();
    for (const QCand& c : cands) {
        if (!surf_f.empty() && c.surf != surf_f) continue;
        if (par) {
            if (!c.axis) continue;
            if (std::abs(std::abs(c.axis->dot(*par)) - 1.0) > 1e-6) continue;
        }
        if (per) {
            if (!c.axis) continue;
            if (std::abs(c.axis->dot(*per)) > 1e-6) continue;
        }
        keep.push_back(&c);
    }

    // 3) side: keep only the extreme-centroid set along the signed axis.
    if (q.contains("side") && q["side"].is_string()) {
        std::optional<Eigen::Vector3d> dir = parse_side(q["side"].get<std::string>());
        if (!dir) { out.error = "geom query: bad 'side' (use +x/-x/+y/-y/+z/-z)"; return out; }
        double best = -std::numeric_limits<double>::infinity();
        for (const QCand* c : keep) best = std::max(best, c->centroid.dot(*dir));
        std::vector<const QCand*> ext;
        for (const QCand* c : keep)
            if (c->centroid.dot(*dir) >= best - tol) ext.push_back(c);
        keep.swap(ext);
    }

    // 4) near: sort by centroid distance, keep nearest `count` (default 1).
    if (q.contains("near")) {
        std::optional<Eigen::Vector3d> pt = json_vec3(q["near"]);
        if (!pt) { out.error = "geom query: 'near' must be [x,y,z]"; return out; }
        std::sort(keep.begin(), keep.end(), [&](const QCand* a, const QCand* b) {
            return (a->centroid - *pt).squaredNorm() < (b->centroid - *pt).squaredNorm();
        });
        int n = q.value("count", 1);
        if (n >= 0 && static_cast<int>(keep.size()) > n) keep.resize(n);
    }

    // 5) index: keep the listed ordinals of the filtered set.
    if (q.contains("index") && q["index"].is_array()) {
        std::vector<const QCand*> pick;
        for (const auto& iv : q["index"]) {
            int i = iv.get<int>();
            if (i >= 0 && i < static_cast<int>(keep.size())) pick.push_back(keep[i]);
        }
        keep.swap(pick);
    } else if (!q.contains("near") && q.contains("count")) {
        // 6) count cap (when not already applied via `near`).
        int n = q.value("count", static_cast<int>(keep.size()));
        if (n >= 0 && static_cast<int>(keep.size()) > n) keep.resize(n);
    }

    // 7) materialise: kernel sub-shapes + introspection selectors.
    int ordinal = 0;
    for (const QCand* c : keep) {
        if (want_face) out.faces.push_back(TopoDS::Face(c->shp));
        else out.edges.push_back(TopoDS::Edge(c->shp));
        TopoInfo ti;
        ti.kind = of;
        ti.surf = c->surf;
        ti.centroid = c->centroid;
        ti.axis = c->axis;
        ti.measure = c->measure;
        ti.owner = std::nullopt;
        ti.hint_index = ordinal++;
        out.selectors.push_back(make_selector(ti));
    }
    return out;
}

}  // namespace aicad::doc
