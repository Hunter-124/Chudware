// Selectors implementation — see selectors.h for the WHY and the contract pointer.
//
// This is a bit-for-bit C++ port of engine/doc/selectors.py:
//   - describe_faces / describe_edges / describe_vertices: OCCT-backed describers
//     (TopExp_Explorer enumeration; BRepAdaptor surface/curve type + axis; BRepGProp
//     centroid + area/length; BRep_Tool::Pnt_s for vertices).
//   - make_selector: TopoInfo -> selector json dict.
//   - resolve / _score_candidate: §3 scored best-match with the exact weights,
//     accept thresholds, owner filter+fallback, and hint_index tiebreak.
//
// AXIS CHOICES MIRROR THE PYTHON ORACLE (deliberate — read selectors.py):
//   describe_faces sets an axis ONLY for plane/cylinder/cone (sphere/torus/bspline
//   have no single meaningful axis, so they stay nullopt). describe_edges sets an
//   axis ONLY for line/circle — even though the curve-type STRING table also names
//   "ellipse", the Python's axis branch handles only GeomAbs_Line and GeomAbs_Circle,
//   so an ellipse edge gets surf=="ellipse" but axis==nullopt. We match that exactly.
#include "doc/selectors.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <tuple>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepGProp.hxx>
#include <BRep_Tool.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Vertex.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>

namespace aicad::doc {

namespace {

// GeomAbs_SurfaceType -> contract string (port of _surf_type_string).
std::string surf_type_string(GeomAbs_SurfaceType t) {
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

// GeomAbs_CurveType -> contract string (port of _curve_type_string).
std::string curve_type_string(GeomAbs_CurveType t) {
    switch (t) {
        case GeomAbs_Line:         return "line";
        case GeomAbs_Circle:       return "circle";
        case GeomAbs_Ellipse:      return "ellipse";
        case GeomAbs_BSplineCurve: return "bspline";
        default:                   return "other";
    }
}

// Normalise a gp_Dir into an Eigen unit vector; nullopt if degenerate (mirrors the
// Python's `axis = v / n if n > 1e-12 else None`). gp_Dir is already unit-length, but
// we keep the guard for parity with the oracle.
std::optional<Eigen::Vector3d> unit_axis(const gp_Dir& d) {
    Eigen::Vector3d v(d.X(), d.Y(), d.Z());
    double n = v.norm();
    if (n > 1e-12) return v / n;
    return std::nullopt;
}

}  // namespace

// --------------------------------------------------------------------------- describe_faces

std::vector<TopoInfo> describe_faces(const TopoDS_Shape& shape,
                                     const std::map<int, std::string>& owners) {
    std::vector<TopoInfo> result;
    int idx = 0;
    for (TopExp_Explorer exp(shape, TopAbs_FACE); exp.More(); exp.Next(), ++idx) {
        TopoDS_Face face = TopoDS::Face(exp.Current());
        BRepAdaptor_Surface adaptor(face);
        GeomAbs_SurfaceType st = adaptor.GetType();

        // Centroid + area via surface mass properties (props.CentreOfMass()).
        GProp_GProps props;
        BRepGProp::SurfaceProperties(face, props);
        double area = props.Mass();
        gp_Pnt cm = props.CentreOfMass();
        Eigen::Vector3d centroid(cm.X(), cm.Y(), cm.Z());

        // Axis — plane normal, cylinder/cone axis only (matches the Python oracle).
        std::optional<Eigen::Vector3d> axis;
        try {
            if (st == GeomAbs_Plane) {
                axis = unit_axis(adaptor.Plane().Axis().Direction());
            } else if (st == GeomAbs_Cylinder) {
                axis = unit_axis(adaptor.Cylinder().Axis().Direction());
            } else if (st == GeomAbs_Cone) {
                axis = unit_axis(adaptor.Cone().Axis().Direction());
            }
        } catch (const Standard_Failure&) {
            axis = std::nullopt;
        }

        std::optional<std::string> owner;
        auto it = owners.find(idx);
        if (it != owners.end()) owner = it->second;

        TopoInfo info;
        info.kind = "face";
        info.index = idx;
        info.surf = surf_type_string(st);
        info.centroid = centroid;
        info.axis = axis;
        info.measure = area;
        info.owner = owner;
        info.hint_index = idx;
        result.push_back(std::move(info));
    }
    return result;
}

// --------------------------------------------------------------------------- describe_edges

// Build the geometric descriptor for ONE edge (centroid/axis/surf/measure). Shared by
// describe_edges (per-incident-face enumeration) and make_pick_selector (unique-edge
// enumeration) so a pick and a compile-time describe agree on an edge's geometry.
TopoInfo edge_info(const TopoDS_Edge& edge, int idx) {
    BRepAdaptor_Curve adaptor(edge);
    GeomAbs_CurveType ct = adaptor.GetType();

    // Centroid (midpoint) + length via linear mass properties.
    GProp_GProps props;
    BRepGProp::LinearProperties(edge, props);
    double length = props.Mass();
    gp_Pnt cm = props.CentreOfMass();
    Eigen::Vector3d centroid(cm.X(), cm.Y(), cm.Z());

    // Axis — line direction, circle axis only (matches the Python oracle; an
    // ellipse edge carries surf=="ellipse" but no axis).
    std::optional<Eigen::Vector3d> axis;
    try {
        if (ct == GeomAbs_Line) {
            axis = unit_axis(adaptor.Line().Direction());
        } else if (ct == GeomAbs_Circle) {
            axis = unit_axis(adaptor.Circle().Axis().Direction());
        }
    } catch (const Standard_Failure&) {
        axis = std::nullopt;
    }

    TopoInfo info;
    info.kind = "edge";
    info.index = idx;
    info.surf = curve_type_string(ct);
    info.centroid = centroid;
    info.axis = axis;
    info.measure = length;
    info.hint_index = idx;
    return info;
}

std::vector<TopoInfo> describe_edges(const TopoDS_Shape& shape,
                                     const std::map<int, std::string>& owners) {
    std::vector<TopoInfo> result;
    int idx = 0;
    for (TopExp_Explorer exp(shape, TopAbs_EDGE); exp.More(); exp.Next(), ++idx) {
        TopoInfo info = edge_info(TopoDS::Edge(exp.Current()), idx);
        auto it = owners.find(idx);
        if (it != owners.end()) info.owner = it->second;
        result.push_back(std::move(info));
    }
    return result;
}

// --------------------------------------------------------------------------- describe_vertices

std::vector<TopoInfo> describe_vertices(const TopoDS_Shape& shape,
                                        const std::map<int, std::string>& owners) {
    std::vector<TopoInfo> result;
    int idx = 0;
    for (TopExp_Explorer exp(shape, TopAbs_VERTEX); exp.More(); exp.Next(), ++idx) {
        TopoDS_Vertex vtx = TopoDS::Vertex(exp.Current());
        gp_Pnt p = BRep_Tool::Pnt(vtx);
        Eigen::Vector3d centroid(p.X(), p.Y(), p.Z());

        std::optional<std::string> owner;
        auto it = owners.find(idx);
        if (it != owners.end()) owner = it->second;

        TopoInfo info;
        info.kind = "vertex";
        info.index = idx;
        info.surf = std::nullopt;
        info.centroid = centroid;
        info.axis = std::nullopt;
        info.measure = std::nullopt;
        info.owner = owner;
        info.hint_index = idx;
        result.push_back(std::move(info));
    }
    return result;
}

// --------------------------------------------------------------------------- make_selector

nlohmann::json make_selector(const TopoInfo& info) {
    nlohmann::json sel;
    sel["kind"] = info.kind;
    sel["surf"] = info.surf ? nlohmann::json(*info.surf) : nlohmann::json(nullptr);
    sel["centroid"] = nlohmann::json::array(
        {info.centroid.x(), info.centroid.y(), info.centroid.z()});
    if (info.axis) {
        sel["axis"] = nlohmann::json::array(
            {info.axis->x(), info.axis->y(), info.axis->z()});
    } else {
        sel["axis"] = nullptr;
    }
    sel["area_or_len"] = info.measure ? nlohmann::json(*info.measure) : nlohmann::json(nullptr);
    sel["owner_feature"] = info.owner ? nlohmann::json(*info.owner) : nlohmann::json(nullptr);
    sel["hint_index"] = info.hint_index;
    return sel;
}

// --------------------------------------------------------------------------- make_pick_selector

nlohmann::json make_pick_selector(const TopoDS_Shape& shape, const std::string& kind,
                                  int topo_id, std::optional<std::string> owner) {
    if (shape.IsNull()) return nlohmann::json(nullptr);
    // The host's pick topo_id is the tessellator's edge/vertex id = the 1-based index into
    // TopExp::MapShapes (the UNIQUE sub-shape map). describe_edges/_vertices enumerate via
    // TopExp_Explorer, which visits a SHARED edge/vertex once PER incident face — a box yields
    // 24 edge-visits for 12 unique edges, in a DIFFERENT order. Indexing that 24-space by the
    // 12-space topo_id mapped DISTINCT picked edges onto the SAME descriptor (two picks, one
    // centroid), so the 1-to-1 resolver dropped the colliding one: the "N selectors couldn't be
    // located" / "3-way corner only rounds 2/3" / "added edge not applied" bug. Build the
    // descriptor from the unique sub-shape at the SAME MapShapes index the tessellator used.
    // Faces are NOT shared (Explorer order == MapShapes order), so they keep describe_faces.
    TopoInfo info;
    if (kind == "edge") {
        TopTools_IndexedMapOfShape m;
        TopExp::MapShapes(shape, TopAbs_EDGE, m);
        if (topo_id < 1 || topo_id > m.Extent()) return nlohmann::json(nullptr);
        info = edge_info(TopoDS::Edge(m.FindKey(topo_id)), topo_id - 1);
    } else if (kind == "vertex") {
        TopTools_IndexedMapOfShape m;
        TopExp::MapShapes(shape, TopAbs_VERTEX, m);
        if (topo_id < 1 || topo_id > m.Extent()) return nlohmann::json(nullptr);
        gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(m.FindKey(topo_id)));
        info.kind = "vertex";
        info.index = topo_id - 1;
        info.centroid = Eigen::Vector3d(p.X(), p.Y(), p.Z());
        info.hint_index = topo_id - 1;
    } else if (kind == "face") {
        std::vector<TopoInfo> infos = describe_faces(shape);
        if (topo_id < 1 || topo_id > static_cast<int>(infos.size()))
            return nlohmann::json(nullptr);
        info = infos[topo_id - 1];
    } else {
        return nlohmann::json(nullptr);
    }
    if (owner) info.owner = owner;
    info.hint_index = topo_id - 1;
    return make_selector(info);
}

// --------------------------------------------------------------------------- resolution

// Score candidate `c` against selector `sel` per contract §3. Lower is better.
// Port of _score_candidate: bbox_diag normalises the centroid distance. PUBLIC (see
// selectors.h) so doc/picks.cpp can re-score same-kind candidates for the hint
// fallback; resolve() below still calls it unqualified (same namespace).
double score_candidate(const nlohmann::json& sel, const TopoInfo& c, double bbox_diag) {
    const auto& sc = sel.at("centroid");
    Eigen::Vector3d s_centroid(sc.at(0).get<double>(), sc.at(1).get<double>(),
                               sc.at(2).get<double>());

    // Surface/curve type. The selector's surf may be null (vertices); compare optionals the same
    // way the Python compares c.surf == sel.get("surf").
    std::optional<std::string> sel_surf;
    if (sel.contains("surf") && !sel.at("surf").is_null())
        sel_surf = sel.at("surf").get<std::string>();
    double tm = (c.surf == sel_surf) ? 0.0 : 1.0;

    // Axis term — only when BOTH selector and candidate have an axis.
    bool sel_has_axis = sel.contains("axis") && !sel.at("axis").is_null();
    bool has_axis = sel_has_axis && c.axis.has_value();
    double ax = 0.0;
    if (has_axis) {
        const auto& sa = sel.at("axis");
        Eigen::Vector3d s_ax(sa.at(0).get<double>(), sa.at(1).get<double>(),
                             sa.at(2).get<double>());
        ax = 1.0 - std::abs(s_ax.dot(*c.axis));
    }

    // Position term. For a straight edge picked against a LIVE preview (a fillet trims the edge an
    // adjacent round eats into), the stored MIDPOINT and LENGTH drift, but the edge stays on the
    // SAME infinite line. So for line-vs-line we score the PERPENDICULAR distance from the selector's
    // point to the candidate's supporting line — trimming-invariant, yet still distinguishing parallel
    // edges on different lines. Everything else keeps the plain centroid distance.
    const bool line_match = sel_surf && *sel_surf == "line" && c.surf && *c.surf == "line" &&
                            c.axis.has_value();
    double dc;
    if (line_match) {
        Eigen::Vector3d dir = *c.axis;
        double n = dir.norm();
        Eigen::Vector3d v = s_centroid - c.centroid;
        Eigen::Vector3d perp = (n > 1e-12) ? (v - dir * (v.dot(dir) / (n * n))) : v;
        dc = perp.norm() / std::max(bbox_diag, 1e-12);
    } else {
        dc = (c.centroid - s_centroid).norm() / std::max(bbox_diag, 1e-12);
    }

    // Area/length term — only when both measures exist. SKIPPED for line-vs-line: a trimmed edge's
    // length is unreliable and would penalise the correct base edge (position+direction already pin
    // it down; post-unify there are no distinct collinear edges to separate by length).
    double ar = 0.0;
    bool sel_has_measure = sel.contains("area_or_len") && !sel.at("area_or_len").is_null();
    if (!line_match && sel_has_measure && c.measure.has_value()) {
        double s_measure = sel.at("area_or_len").get<double>();
        double denom = std::max({std::abs(s_measure), std::abs(*c.measure), 1e-12});
        ar = std::abs(*c.measure - s_measure) / denom;
    }

    if (has_axis) {
        return 0.4 * dc + 0.3 * ax + 0.2 * ar + 0.1 * tm;
    }
    return 0.55 * dc + 0.3 * ar + 0.15 * tm;
}

namespace {

// Format a double like Python's f"{x:.4f}" (fixed, 4 decimals) for reason strings.
std::string fmt4(double x) {
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.4f", x);
    return std::string(buf);
}

}  // namespace

ResolveResult resolve(const nlohmann::json& selector,
                      const std::vector<TopoInfo>& candidates, double bbox_diag) {
    const double INF = std::numeric_limits<double>::infinity();

    std::string kind = selector.value("kind", std::string());
    std::optional<std::string> s_owner;
    if (selector.contains("owner_feature") && !selector.at("owner_feature").is_null())
        s_owner = selector.at("owner_feature").get<std::string>();

    // Step 1 — filter to same kind.
    std::vector<const TopoInfo*> same_kind;
    for (const TopoInfo& c : candidates)
        if (c.kind == kind) same_kind.push_back(&c);

    if (same_kind.empty()) {
        return ResolveResult{std::nullopt, INF, INF, "no candidates of matching kind"};
    }

    // Owner-filtered set with fallback to all same-kind (only when owner is non-null).
    std::vector<const TopoInfo*> working;
    if (s_owner) {
        for (const TopoInfo* c : same_kind)
            if (c->owner == s_owner) working.push_back(c);
    }
    if (working.empty()) working = same_kind;

    // Step 2 — score, then sort by (score, hint_index) (the nearest-hint tiebreak).
    std::vector<std::pair<double, const TopoInfo*>> scored;
    scored.reserve(working.size());
    for (const TopoInfo* c : working)
        scored.emplace_back(score_candidate(selector, *c, bbox_diag), c);

    std::sort(scored.begin(), scored.end(),
              [](const std::pair<double, const TopoInfo*>& a,
                 const std::pair<double, const TopoInfo*>& b) {
                  return std::tie(a.first, a.second->hint_index) <
                         std::tie(b.first, b.second->hint_index);
              });

    double best_score = scored[0].first;
    const TopoInfo* best_cand = scored[0].second;
    double second_score = scored.size() > 1 ? scored[1].first : INF;

    // Step 3 — accept/reject.
    double margin = second_score - best_score;
    if (best_score < 0.35 && margin > 0.05) {
        return ResolveResult{best_cand->index, best_score, second_score, std::nullopt};
    }

    // Dangling — explain why (match the Python reason strings).
    std::string reason;
    if (best_score >= 0.35) {
        reason = "best score " + fmt4(best_score) + " >= 0.35";
    } else {
        reason = "margin " + fmt4(margin) + " <= 0.05 (symmetric ambiguity)";
    }
    return ResolveResult{std::nullopt, best_score, second_score, reason};
}

}  // namespace aicad::doc
