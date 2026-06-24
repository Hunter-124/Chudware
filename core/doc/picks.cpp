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
    std::optional<double> radius;  // cylinder face / circle edge radius (for inner/outer)
    std::string surf;
    double measure = 0.0;
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

// Canonicalise a `surf` token into a kernel surface/curve name. Accepts the everyday
// words a model uses ("flat"/"round"/"straight") resolved against whether we're filtering
// faces or edges, plus every canonical kernel token unchanged. Returns "" for an
// unrecognised token (the caller turns that into a teaching error).
std::string canon_surf(const std::string& raw, bool want_face) {
    const std::string s = lc(raw);
    if (want_face) {
        if (s == "flat"  || s == "planar")               return "plane";
        if (s == "round" || s == "cylindrical")          return "cylinder";
    } else {
        if (s == "straight")                             return "line";
        if (s == "round" || s == "curved")               return "circle";
    }
    // Canonical kernel tokens pass through unchanged (preserves prior behaviour).
    static const char* kKnown[] = {"line", "circle", "ellipse", "bspline", "plane",
                                   "cylinder", "cone", "sphere", "torus", "other"};
    for (const char* k : kKnown)
        if (s == k) return s;
    return "";
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
                else if (st == GeomAbs_Cylinder) {
                    c.axis = Eigen::Vector3d(ad.Cylinder().Axis().Direction().X(),
                        ad.Cylinder().Axis().Direction().Y(), ad.Cylinder().Axis().Direction().Z());
                    c.radius = ad.Cylinder().Radius();  // inner/outer discriminator
                } else if (st == GeomAbs_Cone) c.axis = Eigen::Vector3d(ad.Cone().Axis().Direction().X(),
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
                        (want_face ? "' (faces: flat/round or plane/cylinder/cone/sphere/torus)"
                                   : "' (edges: straight/round or line/circle/ellipse/bspline)");
            return out;
        }
    }
    std::optional<Eigen::Vector3d> par = q.contains("parallel") ? parse_axis_vec(q["parallel"]) : std::nullopt;
    std::optional<Eigen::Vector3d> per = q.contains("perpendicular") ? parse_axis_vec(q["perpendicular"]) : std::nullopt;
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

    // 3) side: keep only the extreme-centroid set along the signed axis (or direction word).
    if (q.contains("side") && q["side"].is_string()) {
        std::optional<Eigen::Vector3d> dir = parse_side(q["side"].get<std::string>());
        if (!dir) {
            out.error = "geom query: bad 'side' '" + q["side"].get<std::string>() +
                        "' (use top/bottom/left/right/front/back or +x/-x/+y/-y/+z/-z)";
            return out;
        }
        double best = -std::numeric_limits<double>::infinity();
        for (const QCand* c : keep) best = std::max(best, c->centroid.dot(*dir));
        std::vector<const QCand*> ext;
        for (const QCand* c : keep)
            if (c->centroid.dot(*dir) >= best - tol) ext.push_back(c);
        keep.swap(ext);
    }

    // 3b) pick: keep the extreme set. largest/smallest face by area, longest/shortest
    //     edge by length — a model with no spatial sense can say "the largest face"
    //     instead of computing areas. inner/outer (also innermost/outermost/bore) rank by
    //     RADIUS among round candidates, the plain-language way to tell coaxial geometry
    //     apart: the inner vs outer wall of a tube, the bore vs the rim. Near-equal values
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
            // Rank by radius; ignore candidates with no radius (planes/lines) entirely.
            std::vector<const QCand*> round;
            for (const QCand* c : keep) if (c->radius) round.push_back(c);
            if (round.empty()) {
                out.error = "geom query: pick '" + p +
                            "' needs round faces/edges (cylinders or circles); none matched";
                return out;
            }
            double ext = want_outer ? -std::numeric_limits<double>::infinity()
                                    :  std::numeric_limits<double>::infinity();
            for (const QCand* c : round)
                ext = want_outer ? std::max(ext, *c->radius) : std::min(ext, *c->radius);
            const double rtol = std::max(std::abs(ext) * 1e-3, 1e-9);
            std::vector<const QCand*> sel;
            for (const QCand* c : round)
                if (std::abs(*c->radius - ext) <= rtol) sel.push_back(c);
            keep.swap(sel);
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
