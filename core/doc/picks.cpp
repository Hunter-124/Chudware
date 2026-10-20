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
#include "geom/face_normal.h"

#include <algorithm>
#include <cctype>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <iterator>
#include <limits>
#include <set>
#include <tuple>
#include <utility>

#include <Bnd_Box.hxx>
#include <BRepBndLib.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepExtrema_DistShapeShape.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepGProp.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <Standard_Failure.hxx>
#include <TopAbs_ShapeEnum.hxx>
#include <TopExp.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <gp_Ax1.hxx>
#include <gp_Cone.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

namespace chudware::doc {

// _RESOLVE_MAP_TOL_FRAC — the map tolerance is a small fraction of the bounding-box
// diagonal (mirrors dress.py). The unique element should coincide with the descriptor
// essentially exactly (same geometric entity), so anything beyond tol means the
// resolve landed on geometry no unique element reproduces — reported as dangling.
constexpr double kResolveMapTolFrac = 1e-3;

namespace {
// Keep these three sets byte-for-byte aligned with services/mcp/cad_common.py's
// GEOM_QUERY_NARROWING_KEYS / DOMAIN_KEYS / READBACK_KEYS. Direct core_bridge calls bypass
// that Python guard, so this is the authoritative engine-side boundary vocabulary.
static const std::set<std::string> kQueryNarrowingKeys = {
    "side", "surf", "parallel", "perpendicular", "pick", "near", "index",
    "count", "body_id",
};
static const std::set<std::string> kQueryDomainKeys = {"of", "kind"};
static const std::set<std::string> kQueryReadbackKeys = {
    "sid", "centroid", "normal", "axis", "radius", "area_or_len", "concavity",
    "hint_index", "owner_feature", "body", "topo", "axis_point",
    "major_radius", "minor_radius", "torus_axis", "torus_center",
};


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
        const std::string where = slot + "[" + std::to_string(i) + "]: ";
        // A plain-English geometry QUERY element ({"of":"face","side":"top"}) resolves
        // through the query resolver exactly like the bare single form every face slot
        // already accepts — a LIST of queries (shell remove top + bottom = a tube) used to
        // reach the topology scorer below and dangle "no candidates of matching kind", or,
        // with "kind":"face" added, leak a raw json key error. An element that carries a
        // measured centroid is a concrete pick (a query_geometry readback echoes "of"
        // beside its centroid) and keeps the scorer path.
        const auto& c = sel.is_object() ? sel.value("centroid", nlohmann::json()) : nlohmann::json();
        const bool located = c.is_array() && c.size() == 3 &&
                             std::all_of(c.begin(), c.end(),
                                         [](const nlohmann::json& v) { return v.is_number(); });
        if (is_geom_query(sel) && !located) {
            QueryPicks qp = resolve_geom_query(shape, sel);
            if (!qp.error.empty()) { out.dangling.push_back(where + qp.error); continue; }
            if (qp.faces.empty()) {
                out.dangling.push_back(where + (qp.edges.empty()
                    ? std::string("the face query matched no faces")
                    : std::string("an edge query in a face slot — use \"of\":\"face\"")));
                continue;
            }
            // Two queries naming the same face (side:"top" + pick:"largest") select it once.
            for (const TopoDS_Face& f : qp.faces) {
                bool dup = false;
                for (const TopoDS_Face& have : out.faces) if (have.IsSame(f)) { dup = true; break; }
                if (!dup) out.faces.push_back(f);
            }
            continue;
        }
        std::optional<int> idx = resolve_with_hint_fallback(sel, infos, diag);
        if (!idx.has_value()) {
            ResolveResult rr = resolve(sel, infos, diag);
            std::string reason = rr.dangling_reason.value_or(std::string("unresolved"));
            out.dangling.push_back(where + reason);
            continue;
        }
        // The resolved descriptor's centroid (face centre) -> nearest unique face.
        const Eigen::Vector3d& target = infos[*idx].centroid;
        double dist = std::numeric_limits<double>::infinity();
        TopoDS_Shape nearest_face = nearest_unique_subshape(shape, TopAbs_FACE, target, tol, dist);
        if (nearest_face.IsNull()) {
            out.dangling.push_back(
                slot + "[" + std::to_string(i) + "]: no unique face within " +
                fmt4g(tol) + " mm of the resolved descriptor (nearest " +
                fmt4g(dist) + " mm)");
            continue;
        }
        out.faces.push_back(TopoDS::Face(nearest_face));
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
    int index = 0;  // 0-based position in the shape's UNIQUE sub-shape map (TopExp::MapShapes)
    Eigen::Vector3d centroid{0, 0, 0};
    std::optional<Eigen::Vector3d> axis;
    // I3 — representative OUTWARD direction at the face centroid, for EVERY face type
    // (plane normal / cyl-cone radial / outward-from-bbox-center). Lets the side filter
    // rank a curved face by which way it faces, not just by centroid height. nullopt for
    // edges and degenerate faces.
    std::optional<Eigen::Vector3d> rep_dir;
    std::optional<double> radius;  // cylinder face / circle edge radius (for inner/outer)
    // Cylinder/cone face: the face centroid projected onto the surface axis — a point ON the
    // axis near the face (the centroid itself sits off-axis on a partial cylinder/fillet).
    std::optional<Eigen::Vector3d> axis_point;
    std::optional<TopoInfo::Torus> torus;  // torus face: analytic surface (sweep bend)
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
// filtering faces or edges, the specific type words (cylindrical/conical/spherical/
// toroidal; circular/elliptical), common abbreviations and plurals ("cyl", "cylinders",
// "sph", "tori", "arcs"), and every canonical kernel token unchanged. Returns the
// kSurfCurvedCategory sentinel for the "round"/"curved" CATEGORY, or "" for an
// unrecognised token (the caller turns that into a teaching error that lists
// kSurfFaceWords / kSurfEdgeWords). A live run (c243) lost a turn on surf:"cyl".
struct SurfWord { const char* word; const char* canon; };
constexpr SurfWord kSurfFaceWords[] = {
    {"flat", "plane"},        {"planar", "plane"},       {"planes", "plane"},
    {"round", kSurfCurvedCategory}, {"rounded", kSurfCurvedCategory},
    {"curved", kSurfCurvedCategory},
    {"cyl", "cylinder"},      {"cyls", "cylinder"},      {"cylinders", "cylinder"},
    {"cylindrical", "cylinder"},
    {"cones", "cone"},        {"conical", "cone"},
    {"sph", "sphere"},        {"spheres", "sphere"},     {"spherical", "sphere"},
    {"tori", "torus"},        {"toruses", "torus"},      {"toroid", "torus"},
    {"toroidal", "torus"},
    {"spline", "bspline"},    {"b-spline", "bspline"},   {"nurbs", "bspline"},
};
constexpr SurfWord kSurfEdgeWords[] = {
    {"straight", "line"},     {"lines", "line"},         {"linear", "line"},
    {"round", kSurfCurvedCategory}, {"rounded", kSurfCurvedCategory},
    {"curved", kSurfCurvedCategory},
    {"circles", "circle"},    {"circular", "circle"},    {"arc", "circle"},
    {"arcs", "circle"},
    {"ellipses", "ellipse"},  {"elliptical", "ellipse"},
    {"spline", "bspline"},    {"b-spline", "bspline"},   {"nurbs", "bspline"},
};

std::string canon_surf(const std::string& raw, bool want_face) {
    const std::string s = lc(raw);
    if (want_face) {
        for (const SurfWord& w : kSurfFaceWords)
            if (s == w.word) return w.canon;
    } else {
        for (const SurfWord& w : kSurfEdgeWords)
            if (s == w.word) return w.canon;
    }
    // Canonical kernel tokens pass through unchanged (preserves prior behaviour).
    static const char* kKnown[] = {"line", "circle", "ellipse", "bspline", "plane",
                                   "cylinder", "cone", "sphere", "torus", "other"};
    for (const char* k : kKnown)
        if (s == k) return s;
    return "";
}

// The accepted `surf` words for the refusal, built from the same tables canon_surf reads,
// so the error can never list a word the matcher refuses (or omit one it accepts).
// Format: "plane (flat, planar, planes), cylinder (cyl, ...), ..., other; round, rounded,
// curved = any non-planar face".
std::string surf_vocabulary(bool want_face) {
    static const char* kFaceCanon[] = {"plane", "cylinder", "cone", "sphere", "torus",
                                       "bspline", "other"};
    static const char* kEdgeCanon[] = {"line", "circle", "ellipse", "bspline", "other"};
    const SurfWord* words = want_face ? kSurfFaceWords : kSurfEdgeWords;
    const size_t n_words = want_face ? std::size(kSurfFaceWords) : std::size(kSurfEdgeWords);
    const char* const* canon = want_face ? kFaceCanon : kEdgeCanon;
    const size_t n_canon = want_face ? std::size(kFaceCanon) : std::size(kEdgeCanon);
    std::string out;
    for (size_t c = 0; c < n_canon; ++c) {
        if (c) out += ", ";
        out += canon[c];
        std::string syn;
        for (size_t w = 0; w < n_words; ++w)
            if (std::string(words[w].canon) == canon[c])
                syn += (syn.empty() ? "" : ", ") + std::string(words[w].word);
        if (!syn.empty()) out += " (" + syn + ")";
    }
    std::string category;
    for (size_t w = 0; w < n_words; ++w)
        if (std::string(words[w].canon) == kSurfCurvedCategory)
            category += (category.empty() ? "" : ", ") + std::string(words[w].word);
    out += "; " + category + (want_face ? " = any non-planar face" : " = any non-line edge");
    return out;
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

// ----------------------------------------------------------------------- `near` ranking
// contracts/selectors.md "near: geometric distance". A model writes `near` as a point ON (or at)
// the entity it means, so ranking by CENTROID distance alone picked the wrong entity whenever
// the meant entity's centroid sits far from its geometry: on a Ø110 flange, near:[55,0,12]
// (a point ON the outer rim) chose the PCD-80 bolt-hole edge (centroid (40,0,12), 15 mm away)
// over the rim (centroid = the axis point (0,0,12), 55 mm away). The primary key is therefore
// min(distance to the trimmed sub-shape geometry, distance to its centroid): a point on the
// entity and a point at its reported centroid (a describe_part / query match handed back as
// `near`, which session.cpp's selector re-resolution also does internally) both score 0.
// Candidates within the tie band of the best key go to the nearer centroid (a point on the
// shared edge of two faces picks the face whose centroid is nearer), then MapShapes order.
constexpr double kNearTieAbsMm = 1e-4;
constexpr double kNearTieRel = 1e-6;

double near_tie_band(double d) { return std::max(kNearTieAbsMm, kNearTieRel * d); }

// Exact point -> trimmed sub-shape distance; +inf when OCCT cannot compute it (the centroid
// term of the key still ranks the candidate).
double point_subshape_distance(const TopoDS_Shape& s, const gp_Pnt& p) {
    try {
        const TopoDS_Vertex v = BRepBuilderAPI_MakeVertex(p);
        BRepExtrema_DistShapeShape d(v, s, Extrema_ExtFlag_MIN);
        if (d.IsDone() && d.NbSolution() > 0) return d.Value();
    } catch (const Standard_Failure&) {}
    return std::numeric_limits<double>::infinity();
}

// Lower bound of the point -> sub-shape distance from its exact-geometry bounding box (0 when
// the box is void/open, i.e. no pruning).
double point_bbox_distance(const TopoDS_Shape& s, const Eigen::Vector3d& p) {
    Bnd_Box b;
    try { BRepBndLib::Add(s, b, /*useTriangulation=*/false); } catch (const Standard_Failure&) { return 0.0; }
    if (b.IsVoid() || b.IsOpen()) return 0.0;
    double x0, y0, z0, x1, y1, z1;
    b.Get(x0, y0, z0, x1, y1, z1);
    const double dx = std::max({0.0, x0 - p.x(), p.x() - x1});
    const double dy = std::max({0.0, y0 - p.y(), p.y() - y1});
    const double dz = std::max({0.0, z0 - p.z(), p.z() - z1});
    return std::sqrt(dx * dx + dy * dy + dz * dz);
}

// The first min(n, size) candidates of `keep` in `near` order. Exact distances are computed
// only for candidates whose bbox lower bound can still reach the n-th smallest centroid
// distance (an upper bound of every selected key) plus its tie band.
std::vector<const QCand*> rank_near(const std::vector<const QCand*>& keep,
                                    const Eigen::Vector3d& pt, std::size_t n) {
    const std::size_t m = keep.size();
    n = std::min(n, m);
    std::vector<double> cent(m), key(m, std::numeric_limits<double>::infinity());
    for (std::size_t i = 0; i < m; ++i) cent[i] = (keep[i]->centroid - pt).norm();
    double reach = std::numeric_limits<double>::infinity();
    if (n < m && n > 0) {
        std::vector<double> c = cent;
        std::nth_element(c.begin(), c.begin() + static_cast<std::ptrdiff_t>(n - 1), c.end());
        reach = c[n - 1] + near_tie_band(c[n - 1]);
    }
    const gp_Pnt gp(pt.x(), pt.y(), pt.z());
    for (std::size_t i = 0; i < m; ++i) {
        if (std::isfinite(reach) && std::min(cent[i], point_bbox_distance(keep[i]->shp, pt)) > reach)
            continue;  // can never enter a selection band: key >= lower bound > reach
        key[i] = std::min(cent[i], point_subshape_distance(keep[i]->shp, gp));
    }
    std::vector<const QCand*> out;
    out.reserve(n);
    std::vector<char> taken(m, 0);
    for (std::size_t step = 0; step < n; ++step) {
        double best = std::numeric_limits<double>::infinity();
        for (std::size_t i = 0; i < m; ++i)
            if (!taken[i]) best = std::min(best, key[i]);
        const double band = best + near_tie_band(best);
        std::size_t pick = m;
        for (std::size_t i = 0; i < m; ++i) {
            if (taken[i] || !(key[i] <= band || key[i] == best)) continue;
            if (pick == m || cent[i] < cent[pick]) pick = i;  // strict: earlier index wins ties
        }
        if (pick == m) break;
        taken[pick] = 1;
        out.push_back(keep[pick]);
    }
    return out;
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

// The shared query-grammar tail for every geom-query error now lives in doc/picks.h
// (chudware::doc::kQueryGrammar) so doc/validate.cpp emits the identical text.

// Echo a REJECTED string value back inside an error message: capped so a pasted sentence
// can't bloat the error, and cut on a UTF-8 code-point boundary because the message is
// serialised back to the caller as JSON (nlohmann throws on invalid UTF-8).
std::string clip_echo(const std::string& s, std::size_t max_len = 60) {
    if (s.size() <= max_len) return s;
    std::size_t n = max_len;
    while (n > 0 && (static_cast<unsigned char>(s[n]) & 0xC0) == 0x80) --n;
    return s.substr(0, n) + "...";
}

// Is a rejected `side` value a natural-language DESCRIPTION rather than a mistyped side
// word? Every legal token is one short word ("bottom", "+x"), so a space or real length
// means the model wrote prose ("outer cylindrical wall face at bottom near x=200") and must
// be pointed at describe_part — repeating the side vocabulary at it teaches nothing.
bool side_is_free_text(const std::string& s) {
    return s.size() > 24 || s.find(' ') != std::string::npos;
}

}  // namespace
std::vector<std::string> unknown_query_keys(const nlohmann::json& q) {
    if (!q.is_object()) return {};

    std::vector<std::string> unknown;
    for (auto it = q.begin(); it != q.end(); ++it) {
        const std::string& key = it.key();
        if (kQueryNarrowingKeys.find(key) == kQueryNarrowingKeys.end() &&
            kQueryDomainKeys.find(key) == kQueryDomainKeys.end() &&
            kQueryReadbackKeys.find(key) == kQueryReadbackKeys.end() &&
            key != "doc_id" && key != "query") {
            unknown.push_back(key);
        }
    }
    std::sort(unknown.begin(), unknown.end());
    return unknown;
}

std::string unknown_query_keys_error(const std::string& where,
                                     const std::vector<std::string>& keys) {
    std::string out = where + ": the geometry query has unknown ";
    out += keys.size() == 1 ? "key " : "keys ";
    for (std::size_t i = 0; i < keys.size(); ++i) {
        if (i != 0) out += ", ";
        out += "'" + keys[i] + "'";
    }
    out += ". A key the engine does not know narrows NOTHING and therefore silently matched "
           "every candidate. Real narrowing keys are side, surf, parallel, perpendicular, "
           "pick, near, index, count, body_id. For curved-style intent, use 'surf': "
           "'curved'/'round' means any non-planar face, and 'cylindrical', 'conical', "
           "'spherical', or 'toroidal' select a specific curved type (exact 'cylinder', "
           "'cone', 'sphere', and 'torus' also work).";
    out += kQueryGrammar;
    return out;
}


bool is_geom_query(const nlohmann::json& v) {
    return v.is_object() && (v.value("kind", std::string()) == "query" || v.contains("of"));
}

QueryPicks resolve_geom_query(const TopoDS_Shape& shape, const nlohmann::json& q) {
    QueryPicks out;
    // `of` DEFAULTS TO "face". A live model called query_geometry with {"side":"+x"} and hit
    // the dead end `'of' must be "edge" or "face" (got '')` — nothing in that message says
    // the fix is to add of:"face". Faces are what a query overwhelmingly means (side / surf /
    // pick / parallel all read as face predicates), and is_geom_query already accepts an
    // of-less {"kind":"query"} object, so the of-less form reached here only to be rejected.
    // Absent / null / empty `of` -> faces; a PRESENT value that isn't face|edge (matched
    // case-insensitively) is still an error, now carrying the whole grammar.
    std::string of_raw;
    if (q.contains("of") && !q["of"].is_null()) {
        if (!q["of"].is_string()) {
            out.error = std::string("geom query: 'of' must be the string \"face\" or \"edge\"") +
                        kQueryGrammar;
            return out;
        }
        of_raw = q["of"].get<std::string>();
    }
    const std::string of = of_raw.empty() ? std::string("face") : lc(of_raw);
    const bool want_face = (of == "face");
    if (of != "edge" && of != "face") {
        out.error = "geom query: 'of' must be \"edge\" or \"face\" (got '" + clip_echo(of_raw) +
                    "')" + kQueryGrammar;
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
        c.index = i - 1;
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
                    const gp_Pln pln = ad.Plane();
                    gp_Dir pn = pln.Axis().Direction();
                    // I3: signed outward normal (handedness of the plane frame AND face
                    // orientation — see geom/face_normal.h), so a flat cap's rep_dir
                    // distinguishes top from bottom — same convention as describe_faces.
                    // The stored `axis` keeps the geometric (unsigned) direction it always
                    // had, so the F5 score path is unchanged.
                    c.axis = Eigen::Vector3d(pn.X(), pn.Y(), pn.Z());
                    const gp_Dir pn_signed = ::chudware::geom::plane_outward_normal(f, pln);
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
                } else if (st == GeomAbs_Sphere) {
                    c.radius = ad.Sphere().Radius();
                } else if (st == GeomAbs_Torus) {
                    c.torus = face_torus(f);
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
        // Cylinder/cone: the centroid's foot on the surface axis (c.axis is unit here).
        if (want_face && c.axis && axis_loc)
            c.axis_point = *axis_loc + *c.axis * (c.centroid - *axis_loc).dot(*c.axis);
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
            out.error = "geom query: unknown 'surf' '" + clip_echo(q["surf"].get<std::string>()) +
                        "'. Use one of these " + (want_face ? "face" : "edge") +
                        " surf words: " + surf_vocabulary(want_face) + kQueryGrammar;
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
            const std::string bad = q["side"].get<std::string>();
            if (side_is_free_text(bad)) {
                out.error = "geom query: 'side' got a free-text description ('" +
                            clip_echo(bad) + "'), not a side word. Use one of top/bottom/left/"
                            "right/front/back or +x/-x/+y/-y/+z/-z, or call describe_part to "
                            "list the actual faces and pick one by sid." +
                            kQueryGrammar;
                return out;
            }
            out.error = "geom query: bad 'side' '" + clip_echo(bad) +
                        "' (use top/bottom/left/right/front/back or +x/-x/+y/-y/+z/-z)" +
                        kQueryGrammar;
            return out;
        }
        constexpr double kDirWeight = 0.25;
        const double dir_scale = kDirWeight * diag;
        auto side_score = [&](const QCand* c) -> double {
            double s = c->centroid.dot(*dir);
            if (c->rep_dir) s += dir_scale * c->rep_dir->dot(*dir);
            return s;
        };
        // W3 — side + size-pick interplay. `side` alone means "the extreme face that
        // way" (a body's TOP face). But combined with an explicit largest/smallest
        // pick, extremum-first makes the pick a no-op: {"side":"top","pick":"largest"}
        // kept only the single highest face (a 300 mm² wall-top sliver) and then picked
        // "largest" among that one — silently ignoring the 2400 mm² base top the query
        // obviously meant. With a size pick present, `side` instead FILTERS to every
        // face FACING that way (rep_dir·dir ≥ 0.7) and lets the pick choose among them.
        // Face queries only; falls back to the extremum path when nothing faces the
        // side (e.g. a sphere's continuous wall).
        bool side_done = false;
        if (want_face && q.contains("pick") && q["pick"].is_string()) {
            const std::string pk = lc(q["pick"].get<std::string>());
            if (pk == "largest" || pk == "biggest" || pk == "smallest") {
                constexpr double kFacing = 0.7;
                std::vector<const QCand*> facing;
                for (const QCand* c : keep)
                    if (c->rep_dir && c->rep_dir->dot(*dir) >= kFacing) facing.push_back(c);
                if (!facing.empty()) {
                    keep.swap(facing);
                    side_done = true;
                }
            }
        }
        if (!side_done) {
        // CAP PREFERENCE: when this is a FACE query and the body has PLANAR faces whose
        // normal is PARALLEL to the queried direction (|rep_dir·dir| ~ 1 — axis-perpendicular
        // CAPS like a flange top/bottom), restrict the set to those caps before taking the
        // centroid-extreme, so a planar cap beats any curved face (a bore/side wall, a dome)
        // for "the top/bottom face". Originally added because revolved end faces reported a
        // mis-signed normal (left-handed gp_Pln frames, now fixed in geom/face_normal.h) and
        // lost to an uncentred bore; it is KEPT because it still decides the curved-vs-cap
        // case (a dome whose centroid sits above a planar shoulder) and side-query results
        // must not move. Cap identification uses |rep_dir·dir| (sign-independent);
        // top-vs-bottom is then decided by the centroid projection below. Untouched for
        // curved-only bodies (sphere/cone — no parallel planar cap) and slanted tops
        // (|rep_dir·dir| < 0.9), which keep using the centroid+direction blend.
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
        }  // !side_done (W3 — skipped when side acted as a facing filter for a size pick)
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

    // 4) near: rank by geometric distance (see rank_near), keep the nearest `count` (default 1).
    if (q.contains("near")) {
        std::optional<Eigen::Vector3d> pt = json_vec3(q["near"]);
        if (!pt) { out.error = "geom query: 'near' must be [x,y,z]"; return out; }
        const int n = q.value("count", 1);
        keep = rank_near(keep, *pt, static_cast<std::size_t>(std::max(n, 0)));
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
        // The sub-shape's index in the WHOLE shape's unique map — never its position in the
        // filtered result. doc_query derives the body/topo join keys from it, the hint
        // fallback compares it with describe-space indices and measure picks validate it as
        // a topology index; a filtered-list ordinal made {"surf":"cylinder"} report topo 1
        // for whatever face happened to come first in the body.
        ti.index = c->index;
        ti.hint_index = c->index;
        // GSL Stage 0 — carry the semantics the scorer already computed into the LLM-facing
        // selector (make_selector emits them as normal/side/concavity/radius). Without this the
        // query_geometry read-back was raw {surf, unsigned axis, noisy centroid, area} with the
        // signed direction / bore-vs-boss / radius all thrown away one struct-copy short.
        ti.rep_dir = c->rep_dir;
        ti.concave = c->concave;
        ti.radius = c->radius;
        ti.axis_point = c->axis_point;
        ti.torus = c->torus;
        out.selectors.push_back(make_selector(ti));
    }
    return out;
}

}  // namespace chudware::doc
