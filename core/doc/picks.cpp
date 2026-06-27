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
#include <cstdlib>
#include <limits>
#include <tuple>
#include <utility>

#include <Bnd_Box.hxx>
#include <BRepBndLib.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepGProp.hxx>
#include <BRepLProp_SLProps.hxx>
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
#include <gp_Ax1.hxx>
#include <gp_Cone.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

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

// GeomAbs_CurveType -> the edge surf contract string (line|circle|ellipse|bspline|other).
std::string edge_curve_string(GeomAbs_CurveType t) {
    switch (t) {
        case GeomAbs_Line:        return "line";
        case GeomAbs_Circle:      return "circle";
        case GeomAbs_Ellipse:     return "ellipse";
        case GeomAbs_BezierCurve: return "bspline";
        case GeomAbs_BSplineCurve:return "bspline";
        default:                  return "other";
    }
}

// GEOMETRIC concavity of a cylindrical face (C5). The raw face Orientation() flag is NOT
// reliable for bore-vs-fillet classification after boolean+fillet operations: OCCT may store
// either orientation depending on how the face was (re)built, so a convex corner fillet can
// come back REVERSED and a bore FORWARD. Instead we decide concavity from real geometry:
//   1. outward material normal n  = geometric surface normal, flipped when the face is REVERSED
//      (so n points OUT of the solid, away from material — the build123d normal_at convention).
//   2. radial-outward direction r = from the cylinder axis to a point on the surface.
//   n·r < 0  ->  the outward normal points TOWARD the axis: the void is the central cylinder and
//                material wraps its outside -> a BORE / hole -> concave.
//   n·r > 0  ->  the outward normal points AWAY from the axis: material is the central cylinder
//                (a boss / convex-corner fillet) -> convex.
// Returns nullopt when the normal is undefined (degenerate) so the caller can fall back to
// the radius heuristic instead of guessing.
std::optional<bool> cylinder_face_concave(const TopoDS_Face& f) {
    try {
        BRepAdaptor_Surface sa(f);
        if (sa.GetType() != GeomAbs_Cylinder) return std::nullopt;
        const double u = (sa.FirstUParameter() + sa.LastUParameter()) / 2.0;
        const double v = (sa.FirstVParameter() + sa.LastVParameter()) / 2.0;
        BRepLProp_SLProps sl(sa, u, v, 1, 1e-7);
        if (!sl.IsNormalDefined()) return std::nullopt;
        gp_Dir gn = sl.Normal();  // geometric normal (orientation-independent)
        gp_Vec n(gn);
        if (f.Orientation() == TopAbs_REVERSED) n.Reverse();  // -> outward material normal

        // Surface point + radial-outward direction (point projected onto the cylinder axis).
        const gp_Pnt p = sl.Value();
        const gp_Ax1 ax = sa.Cylinder().Axis();
        const gp_Pnt a0 = ax.Location();
        const gp_Vec ad(ax.Direction());
        gp_Vec ap(a0, p);                       // axis-origin -> surface point
        gp_Vec radial = ap - ad * ap.Dot(ad);   // remove the axial component
        if (radial.Magnitude() < 1e-9) return std::nullopt;

        return n.Dot(radial) < 0.0;  // outward normal points toward the axis -> concave bore
    } catch (const Standard_Failure&) {
        return std::nullopt;
    }
}

// Build a scoring descriptor for ONE unique edge (centroid, direction, length, curve type). `idx`
// is the edge's position in the unique-edge map (used as the hint_index for tie-breaking).
TopoInfo unique_edge_info(const TopoDS_Edge& e, int idx) {
    TopoInfo info;
    info.kind = "edge";
    info.index = idx;
    info.hint_index = idx;
    BRepAdaptor_Curve ac(e);
    GeomAbs_CurveType ct = ac.GetType();
    info.surf = edge_curve_string(ct);
    GProp_GProps props;
    BRepGProp::LinearProperties(e, props);
    info.measure = props.Mass();
    gp_Pnt cm = props.CentreOfMass();
    info.centroid = Eigen::Vector3d(cm.X(), cm.Y(), cm.Z());
    try {
        if (ct == GeomAbs_Line) {
            gp_Dir d = ac.Line().Direction();
            info.axis = Eigen::Vector3d(d.X(), d.Y(), d.Z());
        } else if (ct == GeomAbs_Circle) {
            gp_Dir d = ac.Circle().Axis().Direction();
            info.axis = Eigen::Vector3d(d.X(), d.Y(), d.Z());
        }
    } catch (const Standard_Failure&) {
        info.axis = std::nullopt;
    }
    return info;
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

    // Apply hint_index fallback on a MARGIN failure. describe_edges emits ONE descriptor per
    // incident face, so EVERY edge pick co-ranks its own two occurrences (margin ~0) — the
    // "symmetric ambiguity" is almost always just this self-duplicate, not two distinct edges.
    // The gate is resolve()'s own acceptance bound (best score < 0.35), NOT a near-exact 0.01:
    // a selector captured against a live fillet/draft PREVIEW carries a trimmed centroid+length
    // (an adjacent in-progress round shortens the edge), which pushes the score just over 0.01
    // and used to hard-fail the whole feature. hint_index + owner + nearest-unique mapping below
    // still disambiguate truly distinct symmetric edges, so widening the gate is safe.
    bool margin_fail = rr.dangling_reason.has_value() &&
                       rr.dangling_reason->find("margin") != std::string::npos;
    if (!margin_fail || !(rr.score < 0.35)) return std::nullopt;

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

    if (scored.empty() || !(scored[0].first < 0.35)) return std::nullopt;

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
    const std::size_t M = sel_list.size();

    // Enumerate UNIQUE edges (one descriptor each — NOT the per-incident-face 24-space, which made
    // every pick self-co-rank). Resolution is then a 1-TO-1 ASSIGNMENT: each selector claims its
    // best UNUSED edge. This is the fix for "N selectors mapped to the same edge" — when a fillet
    // trims/renumbers edges so two picks would otherwise both grab one base edge, the second now
    // falls through to its correct neighbour instead of silently duplicating (and inflating the
    // "rounded" count). Most-confident selectors are assigned first so a stale pick can't steal a
    // confident one's edge.
    TopTools_IndexedMapOfShape uniq;
    TopExp::MapShapes(shape, TopAbs_EDGE, uniq);
    const int N = uniq.Extent();
    std::vector<TopoInfo> infos;
    infos.reserve(N);
    for (int i = 1; i <= N; ++i)
        infos.push_back(unique_edge_info(TopoDS::Edge(uniq(i)), i - 1));
    const double diag = bbox_diag(shape);
    constexpr double kAccept = 0.35;  // matches resolve()'s acceptance bound

    auto valid_edge_sel = [](const nlohmann::json& s) -> bool {
        if (!s.is_object() || !s.contains("centroid") || !s.at("centroid").is_array()) return false;
        if (s.contains("kind") && !s.at("kind").is_null() &&
            s.at("kind").get<std::string>() != "edge")
            return false;
        return true;
    };

    // Pass 1 — per-selector confidence = its best score against ANY edge. Sort ascending so the
    // surest matches assign first.
    std::vector<std::pair<double, std::size_t>> order;
    order.reserve(M);
    for (std::size_t s = 0; s < M; ++s) {
        if (!valid_edge_sel(sel_list[s])) { order.emplace_back(1e30, s); continue; }
        double bs = 1e30;
        for (int i = 0; i < N; ++i) bs = std::min(bs, score_candidate(sel_list[s], infos[i], diag));
        order.emplace_back(bs, s);
    }
    std::sort(order.begin(), order.end(),
              [](const std::pair<double, std::size_t>& a, const std::pair<double, std::size_t>& b) {
                  return a.first < b.first;
              });

    // Pass 2 — greedy assignment in confidence order.
    std::vector<char> used(N, 0);
    std::vector<TopoDS_Edge> resolved(M);
    std::vector<char> ok(M, 0);
    std::vector<std::string> reason(M);
    for (const auto& pr : order) {
        const std::size_t s = pr.second;
        const nlohmann::json& sel = sel_list[s];
        if (!valid_edge_sel(sel)) { reason[s] = "not an edge selector"; continue; }
        std::optional<int> s_hint;
        if (sel.contains("hint_index") && !sel.at("hint_index").is_null())
            s_hint = sel.at("hint_index").get<int>();
        int best = -1; double best_score = 1e30; bool best_hint = false;
        for (int i = 0; i < N; ++i) {
            if (used[i]) continue;
            double sc = score_candidate(sel, infos[i], diag);
            bool hint = s_hint.has_value() && infos[i].hint_index == *s_hint;
            if (sc < best_score - 1e-9 ||
                (std::abs(sc - best_score) <= 1e-9 && hint && !best_hint)) {
                best = i; best_score = sc; best_hint = hint;
            }
        }
        if (best < 0) { reason[s] = "no unused edge remains"; continue; }
        if (best_score >= kAccept) {
            reason[s] = "best score " + fmt4g(best_score) + " >= 0.35 (no close edge)";
            continue;
        }
        used[best] = 1;
        resolved[s] = TopoDS::Edge(uniq(best + 1));
        ok[s] = 1;
    }

    // Assemble in ORIGINAL selector order.
    for (std::size_t s = 0; s < M; ++s) {
        if (ok[s]) out.edges.push_back(resolved[s]);
        else out.dangling.push_back("edges[" + std::to_string(s) + "]: " + reason[s]);
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

// I3 — the body's axis-aligned bbox CENTER (the outward-direction origin for sphere/torus/
// bspline faces, which carry no kernel axis). Computed once per query.
Eigen::Vector3d query_bbox_center(const TopoDS_Shape& shape) {
    Bnd_Box box;
    BRepBndLib::Add(shape, box);
    if (box.IsVoid()) return Eigen::Vector3d(0, 0, 0);
    double xmin, ymin, zmin, xmax, ymax, zmax;
    box.Get(xmin, ymin, zmin, xmax, ymax, zmax);
    return Eigen::Vector3d((xmin + xmax) * 0.5, (ymin + ymax) * 0.5, (zmin + zmax) * 0.5);
}

// I3 — REPRESENTATIVE OUTWARD DIRECTION at a face centroid (mirrors selectors.cpp). plane:
// the signed outward normal; cylinder/cone: radial-outward at the centroid (centroid minus
// its projection onto the surface axis through `axis_loc`); sphere/torus/bspline (no
// `surf_axis`): outward from the body bbox center. nullopt only if every fallback degenerates.
std::optional<Eigen::Vector3d> query_rep_dir(
    GeomAbs_SurfaceType st, const Eigen::Vector3d& centroid,
    const std::optional<Eigen::Vector3d>& plane_axis,
    const std::optional<Eigen::Vector3d>& surf_axis,
    const std::optional<Eigen::Vector3d>& axis_loc,
    const Eigen::Vector3d& body_center) {
    auto unit = [](const Eigen::Vector3d& v) -> std::optional<Eigen::Vector3d> {
        double n = v.norm();
        if (n > 1e-12) return v / n;
        return std::nullopt;
    };
    if (st == GeomAbs_Plane) return plane_axis;  // already the signed outward normal
    if ((st == GeomAbs_Cylinder || st == GeomAbs_Cone) && surf_axis && axis_loc) {
        const Eigen::Vector3d& d = *surf_axis;
        Eigen::Vector3d ap = centroid - *axis_loc;
        Eigen::Vector3d radial = ap - d * ap.dot(d);
        if (auto r = unit(radial)) return r;
        return surf_axis;  // centroid on the axis -> fall back to the axis
    }
    return unit(centroid - body_center);  // sphere/torus/bspline/other
}

// One unique sub-shape's geometric fingerprint (the same fields the describers compute).
struct QCand {
    TopoDS_Shape shp;
    Eigen::Vector3d centroid{0, 0, 0};
    std::optional<Eigen::Vector3d> axis;
    // I3 — representative OUTWARD direction at the face centroid, for EVERY face type
    // (plane normal / cyl-cone radial / outward-from-bbox-center). Lets the side filter
    // rank a curved face by which way it faces, not just by centroid height. nullopt for
    // edges and degenerate faces.
    std::optional<Eigen::Vector3d> rep_dir;
    std::optional<double> radius;  // cylinder face / circle edge radius (for inner/outer)
    std::string surf;
    double measure = 0.0;
    // Concavity flag for cylindrical faces (C5): geometry-based (outward normal vs radial),
    // not raw Orientation() — see cylinder_face_concave(). nullopt for non-cylinders.
    std::optional<bool> concave;  // true=bore/inner, false=boss/outer, nullopt=N/A
};

// ASCII-lowercase a copy of `raw` (the query vocabulary is case-insensitive so a model
// can write "Top"/"FRONT"/"Largest" and still hit).
std::string lc(const std::string& raw) {
    std::string s;
    s.reserve(raw.size());
    for (char c : raw) s.push_back(static_cast<char>(std::tolower(static_cast<unsigned char>(c))));
    return s;
}

// Parse a `side` token into a unit world direction. Accepts the canonical signed-axis
// tokens (+x/-x/+y/-y/+z/-z) AND the plain direction WORDS a spatially-unaware model
// reaches for first (top/bottom/up/down/left/right/front/back/rear). The word->axis
// mapping matches the host's named-view convention (viewport_panel.cpp kAx): top=+Z,
// bottom=-Z, right=+X, left=-X, front=-Y, back=+Y — so "the top face" and "side:+z"
// resolve to the same set. nullopt for anything unrecognised.
std::optional<Eigen::Vector3d> parse_side(const std::string& raw) {
    const std::string s = lc(raw);
    if (s == "top"    || s == "up")             return Eigen::Vector3d(0, 0, 1);
    if (s == "bottom" || s == "down")           return Eigen::Vector3d(0, 0, -1);
    if (s == "right")                           return Eigen::Vector3d(1, 0, 0);
    if (s == "left")                            return Eigen::Vector3d(-1, 0, 0);
    if (s == "front")                           return Eigen::Vector3d(0, -1, 0);
    if (s == "back"   || s == "rear")           return Eigen::Vector3d(0, 1, 0);
    if (s.size() < 2) return std::nullopt;
    double sign = (s[0] == '+') ? 1.0 : (s[0] == '-') ? -1.0 : 0.0;
    if (sign == 0.0) return std::nullopt;
    char ax = s[1];
    Eigen::Vector3d v(0, 0, 0);
    if (ax == 'x') v.x() = sign;
    else if (ax == 'y') v.y() = sign;
    else if (ax == 'z') v.z() = sign;
    else return std::nullopt;
    return v;
}

// I1 CATEGORY sentinel: "round"/"curved" is NOT a single kernel type — it means "any
// non-planar surface" (cylinder OR cone OR sphere OR torus OR bspline) for faces, and
// "any non-line curve" (circle OR ellipse OR bspline) for edges. canon_surf returns this
// sentinel so the surf filter can match a CATEGORY (q_surf_type != "plane") instead of a
// single type string. A user asking to "fillet all the round faces" of a cone/sphere/bottle
// now gets every curved face, not EMPTY (the previous round->cylinder-ONLY alias).
constexpr const char* kSurfCurvedCategory = "@curved";

// Canonicalise a `surf` token into a kernel surface/curve name. Accepts the everyday
// words a model uses ("flat"/"round"/"curved"/"straight") resolved against whether we're
// filtering faces or edges, plus specific curved-type words (conical/spherical/toroidal/
// cylindrical) and every canonical kernel token unchanged. Returns the kSurfCurvedCategory
// sentinel for the "round"/"curved" CATEGORY, or "" for an unrecognised token (the caller
// turns that into a teaching error).
std::string canon_surf(const std::string& raw, bool want_face) {
    const std::string s = lc(raw);
    if (want_face) {
        if (s == "flat"  || s == "planar")               return "plane";
        // "round"/"curved" => any non-planar face (category match — see filter below).
        if (s == "round" || s == "curved")               return kSurfCurvedCategory;
        // Specific curved-type words let a user pick ONE kernel surface type.
        if (s == "cylindrical")                          return "cylinder";
        if (s == "conical")                              return "cone";
        if (s == "spherical")                            return "sphere";
        if (s == "toroidal")                             return "torus";
    } else {
        if (s == "straight")                             return "line";
        // "round"/"curved" => any non-line edge (category match — circle/ellipse/bspline).
        if (s == "round" || s == "curved")               return kSurfCurvedCategory;
    }
    // Canonical kernel tokens pass through unchanged (preserves prior behaviour).
    static const char* kKnown[] = {"line", "circle", "ellipse", "bspline", "plane",
                                   "cylinder", "cone", "sphere", "torus", "other"};
    for (const char* k : kKnown)
        if (s == k) return s;
    return "";
}

// I1 category test: does candidate surf `cs` belong to the "curved" category? For both
// faces and edges the curved set is everything that is NOT the straight/flat primitive:
// faces -> any surf != "plane"; edges -> any surf != "line". ("other" counts as curved —
// an unusual surface is still non-planar; better to include than silently drop it.)
bool surf_is_curved(const std::string& cs) {
    return cs != "plane" && cs != "line";
}

std::optional<Eigen::Vector3d> json_vec3(const nlohmann::json& j) {
    if (!j.is_array() || j.size() < 3) return std::nullopt;
    return Eigen::Vector3d(j[0].get<double>(), j[1].get<double>(), j[2].get<double>());
}

// Parse a `parallel`/`perpendicular` value: either an explicit [x,y,z] OR a string axis
// word ("x"/"y"/"z", "+z", "vertical"). Sign is irrelevant downstream (the filter uses
// |dot|), so we only need the axis. Lets a model write parallel:"z" for "vertical edges".
std::optional<Eigen::Vector3d> parse_axis_vec(const nlohmann::json& j) {
    if (j.is_array()) return json_vec3(j);
    if (j.is_string()) {
        const std::string s = lc(j.get<std::string>());
        if (s == "x" || s == "+x" || s == "-x")                return Eigen::Vector3d(1, 0, 0);
        if (s == "y" || s == "+y" || s == "-y")                return Eigen::Vector3d(0, 1, 0);
        if (s == "z" || s == "+z" || s == "-z" || s == "vertical") return Eigen::Vector3d(0, 0, 1);
    }
    return std::nullopt;
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
    // I3 — body bbox center, the outward-direction origin for sphere/torus/bspline faces.
    const Eigen::Vector3d body_center = want_face ? query_bbox_center(shape)
                                                  : Eigen::Vector3d(0, 0, 0);
    TopTools_IndexedMapOfShape uniq;
    TopExp::MapShapes(shape, kind, uniq);
    std::vector<QCand> cands;
    for (int i = 1; i <= uniq.Extent(); ++i) {
        QCand c;
        c.shp = uniq(i);
        GProp_GProps props;
        // I3 — captured for the representative-direction computation (after centroid known).
        GeomAbs_SurfaceType face_st = GeomAbs_OtherSurface;
        std::optional<Eigen::Vector3d> plane_axis;  // signed outward plane normal
        std::optional<Eigen::Vector3d> axis_loc;    // a point ON the cyl/cone axis
        if (want_face) {
            TopoDS_Face f = TopoDS::Face(c.shp);
            BRepGProp::SurfaceProperties(f, props);
            BRepAdaptor_Surface ad(f);
            GeomAbs_SurfaceType st = ad.GetType();
            face_st = st;
            c.surf = q_surf_type(st);
            try {
                if (st == GeomAbs_Plane) {
                    gp_Dir pn = ad.Plane().Axis().Direction();
                    // I3: signed outward normal (REVERSED faces face the opposite way), so
                    // a flat cap's rep_dir distinguishes top from bottom — same convention
                    // as describe_faces. The stored `axis` keeps the geometric (unsigned)
                    // direction it always had, so the F5 score path is unchanged.
                    c.axis = Eigen::Vector3d(pn.X(), pn.Y(), pn.Z());
                    gp_Dir pn_signed = pn;
                    if (f.Orientation() == TopAbs_REVERSED) pn_signed.Reverse();
                    plane_axis = Eigen::Vector3d(pn_signed.X(), pn_signed.Y(), pn_signed.Z());
                } else if (st == GeomAbs_Cylinder) {
                    gp_Ax1 ax = ad.Cylinder().Axis();
                    c.axis = Eigen::Vector3d(ax.Direction().X(), ax.Direction().Y(),
                                             ax.Direction().Z());
                    axis_loc = Eigen::Vector3d(ax.Location().X(), ax.Location().Y(),
                                               ax.Location().Z());
                    c.radius = ad.Cylinder().Radius();  // inner/outer discriminator
                    // Concavity from REAL geometry (C5): compare the outward material normal to
                    // the radial direction. The raw face Orientation() flag is unreliable after
                    // boolean+fillet (a convex fillet can come back REVERSED), so we don't trust
                    // it. nullopt (degenerate normal) -> fall back to the radius heuristic.
                    c.concave = cylinder_face_concave(f);
                } else if (st == GeomAbs_Cone) {
                    gp_Ax1 ax = ad.Cone().Axis();
                    c.axis = Eigen::Vector3d(ax.Direction().X(), ax.Direction().Y(),
                                             ax.Direction().Z());
                    axis_loc = Eigen::Vector3d(ax.Location().X(), ax.Location().Y(),
                                               ax.Location().Z());
                }
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
                else if (ct == GeomAbs_Circle) {
                    c.axis = Eigen::Vector3d(ad.Circle().Axis().Direction().X(),
                        ad.Circle().Axis().Direction().Y(), ad.Circle().Axis().Direction().Z());
                    c.radius = ad.Circle().Radius();  // inner/outer discriminator
                }
            } catch (const Standard_Failure&) { c.axis = std::nullopt; }
        }
        c.measure = props.Mass();
        gp_Pnt cm = props.CentreOfMass();
        c.centroid = Eigen::Vector3d(cm.X(), cm.Y(), cm.Z());
        // I3 — representative outward direction for EVERY face (computed after the centroid).
        if (want_face) {
            try {
                c.rep_dir = query_rep_dir(face_st, c.centroid, plane_axis, c.axis, axis_loc,
                                          body_center);
            } catch (const Standard_Failure&) { c.rep_dir = std::nullopt; }
        }
        cands.push_back(std::move(c));
    }

    // 2) boolean filters: surf, parallel, perpendicular.
    std::vector<const QCand*> keep;
    keep.reserve(cands.size());
    std::string surf_f;
    if (q.contains("surf") && q["surf"].is_string()) {
        surf_f = canon_surf(q["surf"].get<std::string>(), want_face);
        if (surf_f.empty()) {
            out.error = "geom query: unknown 'surf' '" + q["surf"].get<std::string>() +
                        (want_face ? "' (faces: flat/round/curved, cylindrical/conical/spherical/"
                                     "toroidal, or plane/cylinder/cone/sphere/torus)"
                                   : "' (edges: straight/round/curved or line/circle/ellipse/bspline)");
            return out;
        }
    }
    std::optional<Eigen::Vector3d> par = q.contains("parallel") ? parse_axis_vec(q["parallel"]) : std::nullopt;
    std::optional<Eigen::Vector3d> per = q.contains("perpendicular") ? parse_axis_vec(q["perpendicular"]) : std::nullopt;
    if (par) par->normalize();
    if (per) per->normalize();
    for (const QCand& c : cands) {
        if (!surf_f.empty()) {
            // I1: the "round"/"curved" category matches ANY non-planar (face) / non-line
            // (edge) surface; every other token is an exact kernel-type match as before.
            if (surf_f == kSurfCurvedCategory) {
                if (!surf_is_curved(c.surf)) continue;
            } else if (c.surf != surf_f) {
                continue;
            }
        }
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

    // 3) side: keep only the extreme set along the signed axis (or direction word).
    //    I3 — rank by a SCORE that blends the centroid projection (primary, keeps the flat
    //    cap winning top/bottom) with the representative-direction alignment (so a CURVED
    //    face whose outward direction faces the requested side is selectable even when its
    //    centroid sits on the body axis — a cone/sphere/torus patch that centroid projection
    //    alone can't separate). The direction term is weighted by a fraction of the bbox
    //    diagonal so it acts as a tie-breaker among centroid-equal faces (e.g. a sphere's
    //    hemispheres, both centred on the body) and NEVER overrides a genuinely higher
    //    centroid (a box's top face still beats its side faces). B5 largest-area tie-break
    //    below still fires for genuine ties.
    //    score(c) = centroid·dir + kDirWeight * diag * (rep_dir·dir)
    //    kDirWeight = 0.25: for a unit-cube the side-face centroid sits 0.5*diag below the
    //    top, while a side face's rep_dir·(+z)=0 vs the top's +1 contributes 0.25*diag — so
    //    the top (0.5+0.25 ahead) still wins outright; planar ordering is unchanged. But two
    //    faces at the SAME centroid height are separated purely by which way they face.
    if (q.contains("side") && q["side"].is_string()) {
        std::optional<Eigen::Vector3d> dir = parse_side(q["side"].get<std::string>());
        if (!dir) {
            out.error = "geom query: bad 'side' '" + q["side"].get<std::string>() +
                        "' (use top/bottom/left/right/front/back or +x/-x/+y/-y/+z/-z)";
            return out;
        }
        constexpr double kDirWeight = 0.25;
        const double dir_scale = kDirWeight * diag;
        auto side_score = [&](const QCand* c) -> double {
            double s = c->centroid.dot(*dir);
            if (c->rep_dir) s += dir_scale * c->rep_dir->dot(*dir);
            return s;
        };
        // CAP PREFERENCE (revolved-part fix): when this is a FACE query and the body has
        // PLANAR faces whose normal is PARALLEL to the queried direction (|rep_dir·dir| ~ 1
        // — axis-perpendicular CAPS like a flange top/bottom), restrict the set to those
        // caps before taking the centroid-extreme. This stops a PERPENDICULAR curved face
        // (a bore/side wall, rep_dir ⟂ dir) from out-scoring a planar cap that genuinely
        // faces the queried side — seen on uncentred revolved tubes/flanges where the cap
        // sits at one end and the bore centroid lies BETWEEN the ends (so the bore's
        // centroid·dir beat the cap's, and the cap's small dir-term couldn't recover it).
        // Cap identification uses |rep_dir·dir| (SIGN-INDEPENDENT, so a mis-signed revolved
        // planar-face normal still counts as a cap); top-vs-bottom is then decided purely by
        // the centroid projection below (reliable). Untouched for curved-only bodies
        // (sphere/cone — no parallel planar cap) and slanted tops (|rep_dir·dir| < 0.9), which
        // keep using the centroid+direction blend.
        if (want_face) {
            constexpr double kCapParallel = 0.9;
            std::vector<const QCand*> caps;
            for (const QCand* c : keep)
                if (c->surf == "plane" && c->rep_dir &&
                    std::abs(c->rep_dir->dot(*dir)) >= kCapParallel)
                    caps.push_back(c);
            if (!caps.empty()) keep.swap(caps);
        }
        double best = -std::numeric_limits<double>::infinity();
        for (const QCand* c : keep) best = std::max(best, side_score(c));
        std::vector<const QCand*> ext;
        for (const QCand* c : keep)
            if (side_score(c) >= best - tol) ext.push_back(c);
        // B5 face-only tie-break: when multiple FACES share the same extreme centroid (e.g. a
        // linear_pattern that creates several co-planar top faces alongside the dominant plate
        // top), keep only the largest-area ones (with relative tolerance). This is a
        // deterministic tie-break so "the top face" always selects the dominant face, not an
        // arbitrarily chosen small boss top. Applied only to face queries (not edges — edge
        // tie-breaking at the same height is intentional, e.g. top-rim has 4 edges). Also
        // skipped when an explicit `pick` key is present (pick has its own selection logic).
        if (want_face && ext.size() > 1 && !q.contains("pick")) {
            double max_m = 0.0;
            for (const QCand* c : ext) max_m = std::max(max_m, c->measure);
            const double mtol = std::max(std::abs(max_m) * 1e-3, 1e-9);
            std::vector<const QCand*> dominant;
            for (const QCand* c : ext)
                if (c->measure >= max_m - mtol) dominant.push_back(c);
            // Only apply the tie-break if it actually reduces the set (paranoia guard:
            // if all faces have equal area, the set is unchanged and all are returned).
            if (!dominant.empty()) ext.swap(dominant);
        }
        keep.swap(ext);
    }

    // 3b) pick: keep the extreme set. largest/smallest face by area, longest/shortest
    //     edge by length — a model with no spatial sense can say "the largest face"
    //     instead of computing areas. inner/outer (also innermost/outermost/bore) classify
    //     by CONCAVITY for cylindrical faces (C5: geometry-based cylinder_face_concave —
    //     bore = concave, boss/fillet = convex; not raw TopAbs orientation). When all
    //     candidates lack concavity data (edges, or a body with only one cylinder), fall
    //     back to ranking by radius so "inner" still means the smaller circle. Near-equal
    //     co-rank (a relative tolerance) so a symmetric body keeps the opposite pair;
    //     combine with `side` or add count:1 to force a single result.
    if (q.contains("pick") && q["pick"].is_string()) {
        const std::string p = lc(q["pick"].get<std::string>());
        const bool want_max = (p == "largest" || p == "biggest" || p == "longest");
        const bool want_min = (p == "smallest" || p == "shortest");
        const bool want_outer = (p == "outer" || p == "outermost" || p == "outside");
        const bool want_inner = (p == "inner" || p == "innermost" || p == "inside" ||
                                 p == "bore");
        if (!want_max && !want_min && !want_outer && !want_inner) {
            out.error = "geom query: unknown 'pick' '" + p +
                        "' (use largest/smallest, longest/shortest, or inner/outer for "
                        "coaxial round faces/edges)";
            return out;
        }
        if (want_inner || want_outer) {
            // Collect round candidates only (those with a radius).
            std::vector<const QCand*> round;
            for (const QCand* c : keep) if (c->radius) round.push_back(c);
            if (round.empty()) {
                // I2: no round faces/edges to rank — return an EMPTY result GRACEFULLY
                // (0 matches, no error), consistent with every other no-match query (e.g.
                // surf:round on a box). Previously this THREW a teaching error, which made
                // an honest "this body has no bores" look like a query bug to the caller.
                keep.clear();
            } else {
                // C5 fix: classify by CONCAVITY when orientation data is available.
                // Concavity (c->concave) is set only for cylindrical faces (not edges).
                // Count how many round candidates have orientation info.
                int with_concavity = 0;
                for (const QCand* c : round) if (c->concave.has_value()) ++with_concavity;
                std::vector<const QCand*> sel;
                if (with_concavity > 0) {
                    // Primary: classify by concavity (bore=concave=inner; boss=convex=outer).
                    // Any candidate lacking orientation data (e.g. edges in a mixed query)
                    // is excluded from the concavity split and falls through to the radius
                    // fallback below. If the concavity split yields a non-empty result, use it.
                    for (const QCand* c : round) {
                        if (!c->concave.has_value()) continue;
                        const bool is_inner_by_shape = *c->concave;  // true=bore, false=boss
                        if (want_inner == is_inner_by_shape) sel.push_back(c);
                    }
                }
                if (sel.empty()) {
                    // Fallback: rank by radius (smaller = inner, larger = outer). Used for
                    // circular edges (which have no face orientation) and degenerate cases.
                    double ext = want_outer ? -std::numeric_limits<double>::infinity()
                                            :  std::numeric_limits<double>::infinity();
                    for (const QCand* c : round)
                        ext = want_outer ? std::max(ext, *c->radius) : std::min(ext, *c->radius);
                    const double rtol = std::max(std::abs(ext) * 1e-3, 1e-9);
                    for (const QCand* c : round)
                        if (std::abs(*c->radius - ext) <= rtol) sel.push_back(c);
                }
                keep.swap(sel);
            }
        } else if (!keep.empty()) {
            double ext = want_max ? -std::numeric_limits<double>::infinity()
                                  :  std::numeric_limits<double>::infinity();
            for (const QCand* c : keep)
                ext = want_max ? std::max(ext, c->measure) : std::min(ext, c->measure);
            const double mtol = std::max(std::abs(ext) * 1e-3, 1e-9);
            std::vector<const QCand*> sel;
            for (const QCand* c : keep)
                if (std::abs(c->measure - ext) <= mtol) sel.push_back(c);
            keep.swap(sel);
        }
    }

    // K10/K11: validate `count` early — an invalid value (< 1) is a logic error that
    // would silently return ALL matches (n >= 0 guard below would clamp to 0 and return
    // nothing, or a negative skipped the resize). Both are confusing; reject loudly so
    // the caller can fix the query rather than wondering why they got unexpected results.
    if (q.contains("count")) {
        if (!q["count"].is_number_integer() && !q["count"].is_number_unsigned()) {
            out.error = "geom query: 'count' must be an integer >= 1";
            return out;
        }
        const int count_val = q.value("count", 1);
        if (count_val < 1) {
            out.error = "geom query: count must be >= 1 (got " + std::to_string(count_val) + ")";
            return out;
        }
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
        // GSL Stage 0 — carry the semantics the scorer already computed into the LLM-facing
        // selector (make_selector emits them as normal/side/concavity/radius). Without this the
        // query_geometry read-back was raw {surf, unsigned axis, noisy centroid, area} with the
        // signed direction / bore-vs-boss / radius all thrown away one struct-copy short.
        ti.rep_dir = c->rep_dir;
        ti.concave = c->concave;
        ti.radius = c->radius;
        out.selectors.push_back(make_selector(ti));
    }
    return out;
}

}  // namespace aicad::doc
