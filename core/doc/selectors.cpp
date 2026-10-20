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
#include "geom/face_normal.h"

#include <algorithm>
#include <cmath>
#include <functional>
#include <limits>
#include <string>
#include <tuple>

#include <Bnd_Box.hxx>
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepBndLib.hxx>
#include <BRepGProp.hxx>
#include <BRepLProp_SLProps.hxx>
#include <BRep_Tool.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopAbs.hxx>
#include <gp_Ax1.hxx>
#include <gp_Vec.hxx>
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
#include <gp_Ax3.hxx>
#include <gp_Torus.hxx>

namespace chudware::doc {

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

// Normalise an Eigen vector, nullopt if degenerate.
std::optional<Eigen::Vector3d> unit_vec(const Eigen::Vector3d& v) {
    double n = v.norm();
    if (n > 1e-12) return v / n;
    return std::nullopt;
}

// I3 — the body's axis-aligned bbox CENTER (used as the outward-direction origin for
// sphere/torus/bspline faces that have no kernel axis). Computed lazily per describe/pick.
Eigen::Vector3d bbox_center(const TopoDS_Shape& shape) {
    Bnd_Box box;
    BRepBndLib::Add(shape, box);
    if (box.IsVoid()) return Eigen::Vector3d(0, 0, 0);
    double xmin, ymin, zmin, xmax, ymax, zmax;
    box.Get(xmin, ymin, zmin, xmax, ymax, zmax);
    return Eigen::Vector3d((xmin + xmax) * 0.5, (ymin + ymax) * 0.5, (zmin + zmax) * 0.5);
}

// I3 — REPRESENTATIVE OUTWARD DIRECTION at a face's centroid, for EVERY surface type:
//   plane            : the signed outward normal `plane_axis` (already orientation-flipped).
//   cylinder / cone  : radial-outward at the centroid = centroid minus its projection onto
//                      the surface axis (the axis passes through `axis_loc`); falls back to
//                      the surface axis if the centroid sits on the axis (degenerate).
//   sphere/torus/bspline (no `surf_axis`): (centroid - body bbox center) — a coarse but
//                      stable outward proxy for a convex-ish curved cap.
// Returns nullopt only if every fallback is degenerate.
std::optional<Eigen::Vector3d> representative_dir(
    GeomAbs_SurfaceType st, const Eigen::Vector3d& centroid,
    const std::optional<Eigen::Vector3d>& plane_axis,
    const std::optional<Eigen::Vector3d>& surf_axis,
    const std::optional<Eigen::Vector3d>& axis_loc,
    const std::optional<Eigen::Vector3d>& body_center) {
    if (st == GeomAbs_Plane) return plane_axis;  // already the signed outward normal
    if ((st == GeomAbs_Cylinder || st == GeomAbs_Cone) && surf_axis && axis_loc) {
        const Eigen::Vector3d& d = *surf_axis;       // unit axis direction
        Eigen::Vector3d ap = centroid - *axis_loc;   // axis-origin -> centroid
        Eigen::Vector3d radial = ap - d * ap.dot(d); // strip the axial component
        if (auto r = unit_vec(radial)) return r;
        return surf_axis;  // centroid on the axis (degenerate) -> fall back to the axis
    }
    // sphere / torus / bspline / other: outward from the lazily computed body center.
    return unit_vec(centroid - *body_center);
}

}  // namespace

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

std::optional<TopoInfo::Torus> face_torus(const TopoDS_Face& f) {
    try {
        BRepAdaptor_Surface sa(f);
        if (sa.GetType() != GeomAbs_Torus) return std::nullopt;
        const gp_Torus t = sa.Torus();
        const gp_Ax3& pos = t.Position();
        const auto axis = unit_axis(pos.Direction());
        if (!axis) return std::nullopt;
        TopoInfo::Torus out;
        out.major_radius = t.MajorRadius();
        out.minor_radius = t.MinorRadius();
        out.axis = *axis;
        out.center = Eigen::Vector3d(pos.Location().X(), pos.Location().Y(), pos.Location().Z());
        return out;
    } catch (const Standard_Failure&) {
        return std::nullopt;
    }
}
namespace {

// Exact one-face descriptor shared by full-body enumeration and viewport picks.
// The body center is cached only when a face needs the curved-surface fallback.
TopoInfo face_info(const TopoDS_Face& face, int idx, const TopoDS_Shape& shape,
                   std::optional<Eigen::Vector3d>& body_center) {
    BRepAdaptor_Surface adaptor(face);
    GeomAbs_SurfaceType st = adaptor.GetType();

    // Centroid + area via surface mass properties (props.CentreOfMass()).
    GProp_GProps props;
    BRepGProp::SurfaceProperties(face, props);
    double area = props.Mass();
    gp_Pnt cm = props.CentreOfMass();
    Eigen::Vector3d centroid(cm.X(), cm.Y(), cm.Z());

    // Axis — plane normal, cylinder/cone axis only (matches the Python oracle).
    // ORIENTATION FIX (Z2): OCCT's Plane().Axis().Direction() is the plane FRAME's Z,
    // not the face's outward material normal: the face's topological orientation flips
    // it (a box's REVERSED bottom face), and so does a LEFT-handed plane frame (every
    // revolve end face — the x=0 end of a revolved shaft read +X). geom/face_normal.h
    // applies both, so the stored axis is the SIGNED outward normal: top=[0,0,+1] and
    // bottom=[0,0,-1] for a Z-up plate. Without it the top and bottom faces of a thin
    // plate both stored axis=[0,0,1] and the signed-normal tie-break in resolve() could
    // not distinguish them → "symmetric ambiguity" when fixturing the bottom face for
    // FEA. The resolution score uses |dot(sel_axis, cand_axis)| (sign-agnostic), so the
    // sign does NOT change the score — it only arms the signed-dot tie-break that fires
    // when two parallel faces otherwise score identically.
    std::optional<Eigen::Vector3d> axis;
    std::optional<Eigen::Vector3d> plane_axis;  // I3: signed outward plane normal
    std::optional<Eigen::Vector3d> axis_loc;    // I3: a point ON the cyl/cone axis
    std::optional<double> radius;               // GSL Stage 0: round-geometry size (mm)
    std::optional<bool> concave;
    std::optional<Eigen::Vector3d> axis_point;  // cyl/cone: centroid's foot on the axis
    try {
        if (st == GeomAbs_Plane) {
            axis = unit_axis(::chudware::geom::plane_outward_normal(face, adaptor.Plane()));
            plane_axis = axis;
        } else if (st == GeomAbs_Cylinder) {
            gp_Ax1 ax = adaptor.Cylinder().Axis();
            axis = unit_axis(ax.Direction());
            axis_loc = Eigen::Vector3d(ax.Location().X(), ax.Location().Y(),
                                       ax.Location().Z());
            radius = adaptor.Cylinder().Radius();
            concave = cylinder_face_concave(face);
        } else if (st == GeomAbs_Cone) {
            gp_Ax1 ax = adaptor.Cone().Axis();
            axis = unit_axis(ax.Direction());
            axis_loc = Eigen::Vector3d(ax.Location().X(), ax.Location().Y(),
                                       ax.Location().Z());
            radius = adaptor.Cone().RefRadius();
        } else if (st == GeomAbs_Sphere) {
            radius = adaptor.Sphere().Radius();
        }
    } catch (const Standard_Failure&) {
        axis = std::nullopt;
        plane_axis = std::nullopt;
        axis_loc = std::nullopt;
        radius = std::nullopt;
    }
    if (axis && axis_loc)
        axis_point = *axis_loc + *axis * (centroid - *axis_loc).dot(*axis);

    // Only the final representative_dir fallback reads the whole-body bounds.
    if (st != GeomAbs_Plane &&
        !((st == GeomAbs_Cylinder || st == GeomAbs_Cone) && axis && axis_loc) &&
        !body_center)
        body_center = bbox_center(shape);

    // I3 — representative outward direction for EVERY face (plane normal / cyl-cone
    // radial / outward-from-bbox-center). Wrapped in its own try so a kernel quirk on
    // an exotic surface can't drop the whole descriptor.
    std::optional<Eigen::Vector3d> rep_dir;
    try {
        rep_dir = representative_dir(st, centroid, plane_axis, axis, axis_loc, body_center);
    } catch (const Standard_Failure&) {
        rep_dir = std::nullopt;
    }

    TopoInfo info;
    info.kind = "face";
    info.index = idx;
    info.surf = surf_type_string(st);
    info.centroid = centroid;
    info.axis = axis;
    info.measure = area;
    info.hint_index = idx;
    info.rep_dir = rep_dir;
    info.radius = radius;
    info.concave = concave;
    info.axis_point = axis_point;
    info.torus = (st == GeomAbs_Torus) ? face_torus(face) : std::nullopt;
    return info;
}

}  // namespace

// --------------------------------------------------------------------------- describe_faces

std::vector<TopoInfo> describe_faces(const TopoDS_Shape& shape,
                                     const std::map<int, std::string>& owners) {
    std::vector<TopoInfo> result;
    std::optional<Eigen::Vector3d> body_center;
    int idx = 0;
    for (TopExp_Explorer exp(shape, TopAbs_FACE); exp.More(); exp.Next(), ++idx) {
        TopoInfo info = face_info(TopoDS::Face(exp.Current()), idx, shape, body_center);
        auto it = owners.find(idx);
        if (it != owners.end()) info.owner = it->second;
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
    std::optional<double> radius;  // GSL Stage 0: circle-edge radius (mm) for the read-back
    try {
        if (ct == GeomAbs_Line) {
            axis = unit_axis(adaptor.Line().Direction());
        } else if (ct == GeomAbs_Circle) {
            axis = unit_axis(adaptor.Circle().Axis().Direction());
            radius = adaptor.Circle().Radius();
        }
    } catch (const Standard_Failure&) {
        axis = std::nullopt;
        radius = std::nullopt;
    }

    TopoInfo info;
    info.kind = "edge";
    info.index = idx;
    info.surf = curve_type_string(ct);
    info.centroid = centroid;
    info.axis = axis;
    info.measure = length;
    info.hint_index = idx;
    info.radius = radius;
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

namespace {

// GSL Stage 0 — kill the float noise the LLM read-back used to carry (599.9999999999999,
// e-16 centroid dust): snap sub-nano magnitudes to 0, round the rest to 1e-6. This is far
// below the resolve() accept tolerance (best < 0.35 of bbox_diag), so it cannot change a
// resolution outcome — it only de-noises the persisted/serialised value.
double round_clean(double x) {
    if (!std::isfinite(x)) return x;
    if (std::abs(x) < 1e-9) return 0.0;
    return std::round(x * 1e6) / 1e6;
}

nlohmann::json round_vec(const Eigen::Vector3d& v) {
    return nlohmann::json::array({round_clean(v.x()), round_clean(v.y()), round_clean(v.z())});
}

// Map a signed outward direction to the nearest principal SIDE word (Z-up convention, mirrors
// picks.cpp parse_side: +Z top, -Z bottom, +X right, -X left, -Y front, +Y back). Returns
// nullopt for an oblique face where no axis clearly dominates, so an angled wall does not
// falsely claim a side. This is the plain-word label the LLM addresses geometry by, finally
// echoed back on the OUTPUT side (the docstring used to promise it and never delivered).
std::optional<std::string> side_word(const std::optional<Eigen::Vector3d>& dir) {
    if (!dir) return std::nullopt;
    Eigen::Vector3d v = *dir;
    const double n = v.norm();
    if (n < 1e-9) return std::nullopt;
    v /= n;
    const double ax = std::abs(v.x()), ay = std::abs(v.y()), az = std::abs(v.z());
    const double m = std::max({ax, ay, az});
    // Match orient_tint (0.92): only label faces strongly aligned to a principal axis.
    if (m < 0.92) return std::nullopt;
    if (az >= ax && az >= ay) return v.z() > 0 ? "top" : "bottom";
    if (ax >= ay && ax >= az) return v.x() > 0 ? "right" : "left";
    return v.y() > 0 ? "back" : "front";
}

// GSL Stage 1c (pragmatic W1) — a stable geometry-fingerprint id so the LLM can name a face/edge
// once and refer back to it. It hashes the rounded shape + position (0.1 mm grid) + role, so two
// IDENTICAL faces at different places get DIFFERENT ids (no collision), and the same face is the
// same id across a rebuild that does not move it. (A downstream edit that MOVES the face yields a
// new id — honest: a moved face is a new handle. The owner-scoped version is future work.)
std::string stable_sid(const TopoInfo& info) {
    auto q = [](double x) -> long long { return std::llround(x * 10.0); };  // 0.1 mm grid
    std::string k = info.kind + "|" + (info.surf ? *info.surf : std::string("null")) + "|" +
                    std::to_string(q(info.centroid.x())) + "," +
                    std::to_string(q(info.centroid.y())) + "," +
                    std::to_string(q(info.centroid.z()));
    if (info.radius) k += "|r" + std::to_string(q(*info.radius));
    if (info.concave) k += *info.concave ? "|bore" : "|boss";
    unsigned v = static_cast<unsigned>(std::hash<std::string>{}(k) & 0xFFFFFFu);
    const char* hexd = "0123456789abcdef";
    std::string out(7, '0');
    out[0] = info.kind == "edge" ? 'E' : (info.kind == "vertex" ? 'V' : 'F');
    for (int i = 6; i >= 1; --i) { out[i] = hexd[v & 0xF]; v >>= 4; }
    return out;
}

}  // namespace

nlohmann::json make_selector(const TopoInfo& info) {
    nlohmann::json sel;
    sel["kind"] = info.kind;
    sel["surf"] = info.surf ? nlohmann::json(*info.surf) : nlohmann::json(nullptr);
    // Existing scored-fingerprint fields (resolve() reads these) — now rounded clean.
    sel["centroid"] = round_vec(info.centroid);
    sel["axis"] = info.axis ? round_vec(*info.axis) : nlohmann::json(nullptr);
    sel["area_or_len"] =
        info.measure ? nlohmann::json(round_clean(*info.measure)) : nlohmann::json(nullptr);
    sel["owner_feature"] = info.owner ? nlohmann::json(*info.owner) : nlohmann::json(nullptr);
    sel["hint_index"] = info.hint_index;  // load-bearing: resolve() tiebreak (contract §3)
    // GSL Stage 0 — self-describing semantics the kernel already computes (resolve() IGNORES
    // these; the scored fingerprint above is unchanged). `normal` is the SIGNED outward
    // direction (unlike unsigned `axis`, which collides on opposite faces); `side` is its
    // plain-word label; `concavity` separates a bore/hole from a boss/fillet; `radius` sizes
    // round geometry; `axis_point` is a point ON a cylinder/cone axis (the centroid's foot —
    // `centroid` itself is off-axis on a fillet). nulls where N/A (e.g. an oblique face).
    sel["normal"] = info.rep_dir ? round_vec(*info.rep_dir) : nlohmann::json(nullptr);
    const auto sw = side_word(info.rep_dir);
    sel["side"] = sw ? nlohmann::json(*sw) : nlohmann::json(nullptr);
    sel["concavity"] = info.concave ? nlohmann::json(*info.concave ? "bore" : "boss")
                                    : nlohmann::json(nullptr);
    sel["radius"] =
        info.radius ? nlohmann::json(round_clean(*info.radius)) : nlohmann::json(nullptr);
    sel["axis_point"] =
        info.axis_point ? round_vec(*info.axis_point) : nlohmann::json(nullptr);
    // Torus faces only (sweep bends): the analytic surface `axis`/`radius` cannot carry
    // without changing the scored fingerprint and the stable SID.
    if (info.torus) {
        sel["major_radius"] = round_clean(info.torus->major_radius);
        sel["minor_radius"] = round_clean(info.torus->minor_radius);
        sel["torus_axis"] = round_vec(info.torus->axis);
        sel["torus_center"] = round_vec(info.torus->center);
    }
    sel["sid"] = stable_sid(info);  // GSL W1 — a stable handle the LLM can refer back to
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
    // Faces keep their TopExp_Explorer order, but only the requested face is described.
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
        if (topo_id < 1) return nlohmann::json(nullptr);
        TopExp_Explorer exp(shape, TopAbs_FACE);
        for (int idx = 1; exp.More() && idx < topo_id; ++idx) exp.Next();
        if (!exp.More()) return nlohmann::json(nullptr);
        std::optional<Eigen::Vector3d> body_center;
        info = face_info(TopoDS::Face(exp.Current()), topo_id - 1, shape, body_center);
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

    // A slot value that is not a measured topology selector (a bare string, a predicate-only
    // object with no centroid) has nothing to score: name that instead of letting
    // score_candidate's centroid read escape as a raw json exception.
    if (!selector.is_object())
        return ResolveResult{std::nullopt, INF, INF,
                             "not a selector object (expected {\"kind\":…,\"centroid\":[x,y,z]} "
                             "or a geometry query {\"of\":\"face\",…})"};
    const auto kit = selector.find("kind");
    std::string kind = (kit != selector.end() && kit->is_string()) ? kit->get<std::string>()
                                                                    : std::string();
    std::optional<std::string> s_owner;
    if (selector.contains("owner_feature") && selector.at("owner_feature").is_string())
        s_owner = selector.at("owner_feature").get<std::string>();

    // Step 1 — filter to same kind.
    std::vector<const TopoInfo*> same_kind;
    for (const TopoInfo& c : candidates)
        if (c.kind == kind) same_kind.push_back(&c);

    if (same_kind.empty()) {
        return ResolveResult{std::nullopt, INF, INF, "no candidates of matching kind"};
    }
    const auto cit = selector.find("centroid");
    if (cit == selector.end() || !cit->is_array() || cit->size() != 3 ||
        !std::all_of(cit->begin(), cit->end(),
                     [](const nlohmann::json& v) { return v.is_number(); }))
        return ResolveResult{std::nullopt, INF, INF,
                             "a '" + kind + "' selector without a measured centroid [x,y,z] — "
                             "name it with a geometry query ({\"of\":\"" + kind +
                             "\",\"side\":\"top\"}) or a query_geometry readback"};

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

    // ── Exact-snapshot fast path (asm-fea F1) ────────────────────────────────────────
    // A selector minted from a live match (doc_query / resolve_pick carries the face's
    // recorded centroid + area verbatim, make_selector rounds at 1e-6) whose BEST candidate
    // reproduces both to snapshot precision IS that face — accept it regardless of margin.
    // On a multi-body/assembly compound (four identical wheels) the sibling wheels' faces
    // sit inside the 0.05 ambiguity band (same kind/area/axis; the centroid term is diluted
    // by the assembly-sized bbox diagonal), which dangled every re-resolution of a
    // legitimately picked face. Predicate selectors ({"side":"bottom"}) have no centroid
    // and never enter. Two byte-identical faces (coincident duplicate bodies) resolve to
    // the deterministic sort winner — for facesets either is the same set of points.
    if (selector.contains("centroid") && selector.at("centroid").is_array() &&
        selector.at("centroid").size() == 3) {
        const auto& sc = selector.at("centroid");
        if (sc.at(0).is_number() && sc.at(1).is_number() && sc.at(2).is_number()) {
            const Eigen::Vector3d s_c(sc.at(0).get<double>(), sc.at(1).get<double>(),
                                      sc.at(2).get<double>());
            const double cdist = (best_cand->centroid - s_c).norm();
            bool area_ok = true;  // no recorded area -> centroid alone decides
            if (selector.contains("area_or_len") && selector.at("area_or_len").is_number() &&
                best_cand->measure.has_value()) {
                const double sa = selector.at("area_or_len").get<double>();
                const double m = *best_cand->measure;
                area_ok = std::fabs(m - sa) <=
                          1e-4 * std::max(std::max(std::fabs(sa), std::fabs(m)), 1.0);
            }
            if (cdist <= 1e-3 && area_ok)
                return ResolveResult{best_cand->index, best_score, second_score, std::nullopt};
        }
    }

    if (best_score < 0.35 && margin > 0.05) {
        return ResolveResult{best_cand->index, best_score, second_score, std::nullopt};
    }

    // ── Signed-normal tie-break for opposite-facing parallel planes (Bug F5) ──────
    // The score's axis term is |s_ax · c_ax| — SIGN-AGNOSTIC — so two parallel planar
    // faces with OPPOSITE outward normals (a plate's top vs bottom, a tube's two ends)
    // score identically, OR nearly so, and the margin rule dangles them as "symmetric".
    // For a real 100×100×5 plate, top and bottom have identical areas so their scores
    // differ by only ~0.014 — not an exact float tie, but still within the ambiguous band
    // (margin <= 0.05). Their normals' SIGN is a genuine, stable distinction the selector
    // already carries (a "bottom" pick stored axis -Z; the bottom face's outward normal
    // is -Z, the top's +Z). When the margin <= 0.05 band fires and the selector has an
    // axis, deterministically prefer the candidate in that band whose SIGNED axis best
    // matches the selector's — recovering top/bottom choice without guessing on a true
    // tie. This fires ONLY for axis-bearing candidates (planar/round faces, cylinders):
    // the §5 vertex symmetric-ambiguity cases (no axis) are untouched, so the selector
    // contract / torture suite is preserved. A FEA fixture on a symmetric plate's
    // "bottom" now resolves instead of erroring.
    bool sel_has_axis = selector.contains("axis") && !selector.at("axis").is_null();
    if (best_score < 0.35 && sel_has_axis && scored.size() > 1) {
        const auto& sa = selector.at("axis");
        Eigen::Vector3d s_ax(sa.at(0).get<double>(), sa.at(1).get<double>(),
                             sa.at(2).get<double>());
        double sn = s_ax.norm();
        if (sn > 1e-12) {
            s_ax /= sn;
            // Among ALL candidates in the ambiguous band (score within 0.05 of best),
            // find the one whose SIGNED axis aligns best with the selector's axis, and
            // the next-best alignment. The band is exactly the set the main gate rejects
            // (margin <= 0.05), so we consider any candidate that could plausibly be the
            // winner under pure-score comparison. A non-ambiguous winner (margin > 0.05)
            // is already accepted above and never reaches here.
            const TopoInfo* pick = nullptr;
            double best_align = -std::numeric_limits<double>::infinity();
            double second_align = -std::numeric_limits<double>::infinity();
            int n_in_band = 0;
            bool axis_less = false;
            for (const auto& pr : scored) {
                if (pr.first - best_score > 0.05) break;  // outside ambiguous band
                if (!pr.second->axis.has_value()) { axis_less = true; break; }  // bail on axis-less member
                ++n_in_band;
                double align = s_ax.dot(*pr.second->axis);
                if (align > best_align) {
                    second_align = best_align;
                    best_align = align;
                    pick = pr.second;
                } else if (align > second_align) {
                    second_align = align;
                }
            }
            // Only break the tie when there is a CLEAR signed-direction winner:
            //   1. best_align > 0.5 — the winner genuinely faces the queried direction
            //      (|dot| > 0.5 means within 60° of the selector's axis direction).
            //   2. best_align - second_align > 0.5 — the winner clearly beats the runner-
            //      up; a plate top/bottom gives ~+1 vs ~-1 → gap ~2; genuinely-ambiguous
            //      co-directional faces give gap ~0 and correctly stay dangling.
            // This inner safety gate ensures we never resolve a truly-ambiguous selector.
            if (pick && !axis_less && n_in_band >= 2 && best_align > 0.5 &&
                best_align - second_align > 0.5) {
                return ResolveResult{pick->index, best_score, second_score, std::nullopt};
            }
        }
    }

    // ── Area/size tie-break for COAXIAL faces (a bore vs the boss around it) ─────────
    // Coaxial cylinders share an axis AND an on-axis centroid, so neither the score's
    // centroid term nor the signed-axis break above can separate them (same axis → align
    // gap ≈ 0) — they dangle as "symmetric" even though doc_query (resolve_geom_query,
    // which keys on radius/near) resolves the very same selector to ONE face. A FEA load on
    // a bored boss's inner cylinder hit exactly this. When the selector carries the recorded
    // face AREA (area_or_len), prefer the band candidate whose area matches it — the bore's
    // area is distinct from the boss's. Gated to a CLEAR winner so a true area tie (a plate's
    // equal top/bottom) still defers to the axis break / honest ambiguity above.
    bool sel_has_area = selector.contains("area_or_len") &&
                        selector.at("area_or_len").is_number() &&
                        selector.at("area_or_len").get<double>() > 1e-9;
    if (best_score < 0.35 && sel_has_area && scored.size() > 1) {
        const double sel_area = selector.at("area_or_len").get<double>();
        const TopoInfo* pick = nullptr;
        double best_rel = INF, second_rel = INF;
        int n_in_band = 0;
        bool measure_less = false;
        for (const auto& pr : scored) {
            if (pr.first - best_score > 0.05) break;        // outside ambiguous band
            if (!pr.second->measure.has_value()) { measure_less = true; break; }
            ++n_in_band;
            const double m = *pr.second->measure;
            const double rel = std::fabs(m - sel_area) / std::max(std::max(sel_area, m), 1e-9);
            if (rel < best_rel) { second_rel = best_rel; best_rel = rel; pick = pr.second; }
            else if (rel < second_rel) { second_rel = rel; }
        }
        // Winner must MATCH the recorded area (<10%) and clearly beat the runner-up (>10%
        // apart) — so two equal-area faces never resolve here (they fall through to dangle).
        if (pick && !measure_less && n_in_band >= 2 && best_rel < 0.10 &&
            second_rel - best_rel > 0.10) {
            return ResolveResult{pick->index, best_score, second_score, std::nullopt};
        }
    }

    // ── Merged-face fallback for planar faces ─────────────────────────────────────────
    // Booleans unify same-domain faces (fuse_unify / cut_unify), so a face recorded before
    // an edit — or by an older engine that left coplanar splits — may now be one piece of a
    // larger face on the same plane. Its centroid and area moved, so the score dangles, yet
    // the plane, the signed outward normal and "at least as large" still name exactly one
    // face. Accept only a UNIQUE such candidate; two coplanar same-facing faces dangle.
    if (kind == "face" && selector.value("surf", std::string()) == "plane" &&
        selector.contains("centroid") && selector.at("centroid").is_array() &&
        selector.at("centroid").size() == 3 && selector.contains("normal") &&
        selector.at("normal").is_array() && selector.at("normal").size() == 3) {
        const auto& sc = selector.at("centroid");
        const auto& sn = selector.at("normal");
        const Eigen::Vector3d s_c(sc.at(0).get<double>(), sc.at(1).get<double>(),
                                  sc.at(2).get<double>());
        Eigen::Vector3d s_n(sn.at(0).get<double>(), sn.at(1).get<double>(),
                            sn.at(2).get<double>());
        const double s_area = sel_has_area ? selector.at("area_or_len").get<double>() : 0.0;
        const double plane_tol = std::max(1e-3, 1e-6 * bbox_diag);
        if (s_n.norm() > 1e-12) {
            s_n.normalize();
            const TopoInfo* pick = nullptr;
            int n_match = 0;
            for (const TopoInfo* c : same_kind) {
                if (!c->surf || *c->surf != "plane" || !c->rep_dir) continue;
                if (c->rep_dir->dot(s_n) < 1.0 - 1e-6) continue;
                if (std::fabs((c->centroid - s_c).dot(s_n)) > plane_tol) continue;
                if (c->measure && *c->measure < s_area * (1.0 - 1e-6)) continue;
                pick = c;
                ++n_match;
            }
            if (n_match == 1)
                return ResolveResult{pick->index, best_score, second_score, std::nullopt};
        }
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

}  // namespace chudware::doc
