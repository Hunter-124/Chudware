// Sheet-metal solid construction and development.
#include "geom/sheetmetal.h"

#include <algorithm>
#include <cmath>
#include <exception>
#include <limits>
#include <stdexcept>
#include <string>
#include <vector>

#include "geom/booleans.h"
#include "geom/face_normal.h"

#include <BRep_Tool.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepAlgoAPI_Common.hxx>
#include <BRepBndLib.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepClass3d_SolidClassifier.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepTools.hxx>
#include <BRepTools_WireExplorer.hxx>
#include <GProp_GProps.hxx>
#include <GC_MakeArcOfCircle.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <Geom2dConvert.hxx>
#include <Geom2d_BSplineCurve.hxx>
#include <Geom2d_Curve.hxx>
#include <Geom2d_TrimmedCurve.hxx>
#include <Geom_BSplineCurve.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TColgp_Array1OfPnt.hxx>
#include <TColStd_Array1OfReal.hxx>
#include <Standard_Failure.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <Bnd_Box.hxx>
#include <gp_Ax3.hxx>
#include <gp_Dir.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pnt2d.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

namespace geom {

namespace {

constexpr double PI = 3.14159265358979323846;

inline double radians(double deg) { return deg * PI / 180.0; }

// A planar face's outward unit normal (build123d Face.normal_at(): du×dv, flipped when the
// face is REVERSED — geom/face_normal.h, which also honours left-handed plane frames).
bool face_plane_normal(const TopoDS_Face& f, Eigen::Vector3d& n_out,
                       Eigen::Vector3d& c_out) {
    BRepAdaptor_Surface surf(f);
    if (surf.GetType() != GeomAbs_Plane) return false;
    gp_Pln pln = surf.Plane();
    const gp_Dir d = ::chudware::geom::plane_outward_normal(f, pln);
    n_out = Eigen::Vector3d(d.X(), d.Y(), d.Z());
    gp_Pnt loc = pln.Location();
    c_out = Eigen::Vector3d(loc.X(), loc.Y(), loc.Z());
    return true;
}

// Face centre = centre of mass of the surface (build123d Face.center()).
Eigen::Vector3d face_center(const TopoDS_Face& f) {
    GProp_GProps props;
    BRepGProp::SurfaceProperties(f, props);
    gp_Pnt c = props.CentreOfMass();
    return Eigen::Vector3d(c.X(), c.Y(), c.Z());
}

bool is_planar(const TopoDS_Face& f) {
    BRepAdaptor_Surface surf(f);
    return surf.GetType() == GeomAbs_Plane;
}

// Body centre = centre of the volume bounding box midpoint (build123d
// body.bounding_box() centre). The oracle uses the BBOX centre, not mass centre.
Eigen::Vector3d body_bbox_center(const TopoDS_Shape& body) {
    Bnd_Box bb;
    BRepBndLib::Add(body, bb);
    double xmn, ymn, zmn, xmx, ymx, zmx;
    bb.Get(xmn, ymn, zmn, xmx, ymx, zmx);
    return Eigen::Vector3d((xmn + xmx) / 2.0, (ymn + ymx) / 2.0, (zmn + zmx) / 2.0);
}

// build123d Edge.center() = centre of the edge (linear centre of mass for a line).
Eigen::Vector3d edge_center(const TopoDS_Edge& e) {
    GProp_GProps props;
    BRepGProp::LinearProperties(e, props);
    gp_Pnt c = props.CentreOfMass();
    return Eigen::Vector3d(c.X(), c.Y(), c.Z());
}

double edge_length(const TopoDS_Edge& e) {
    GProp_GProps props;
    BRepGProp::LinearProperties(e, props);
    return props.Mass();
}

// Ordered vertices of an edge (TopExp::Vertices gives first/last in parametric
// order — the same the build123d Edge.vertices() list yields for a line).
void edge_vertices(const TopoDS_Edge& e, Eigen::Vector3d& v0, Eigen::Vector3d& v1) {
    TopoDS_Vertex a, b;
    TopExp::Vertices(e, a, b);
    gp_Pnt pa = BRep_Tool::Pnt(a), pb = BRep_Tool::Pnt(b);
    v0 = Eigen::Vector3d(pa.X(), pa.Y(), pa.Z());
    v1 = Eigen::Vector3d(pb.X(), pb.Y(), pb.Z());
}

// Outward normal of a face: its geometric normal flipped to point AWAY from the
// body's bbox centre (oracle _outward_normal).
Eigen::Vector3d outward_normal(const TopoDS_Face& f, const TopoDS_Shape& body) {
    Eigen::Vector3d n, c;
    if (!face_plane_normal(f, n, c)) return Eigen::Vector3d(0, 0, 1);
    n.normalize();
    Eigen::Vector3d fc = face_center(f);
    Eigen::Vector3d bc = body_bbox_center(body);
    if (n.dot(fc - bc) < 0) n = -n;
    return n;
}

// Extent of `f` measured perpendicular to `e_dir`, within the face plane (oracle
// _face_extent_perp_to_edge). Returns -1 when undefined.
double face_extent_perp_to_edge(const TopoDS_Face& f, const Eigen::Vector3d& e_dir) {
    Eigen::Vector3d n, c;
    if (!face_plane_normal(f, n, c)) return -1.0;
    Eigen::Vector3d perp = e_dir.cross(n);
    if (perp.norm() < 1e-9) return -1.0;
    perp.normalize();
    double mn = std::numeric_limits<double>::max();
    double mx = -std::numeric_limits<double>::max();
    for (TopExp_Explorer ex(f, TopAbs_VERTEX); ex.More(); ex.Next()) {
        gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(ex.Current()));
        double pr = perp.dot(Eigen::Vector3d(p.X(), p.Y(), p.Z()));
        mn = std::min(mn, pr);
        mx = std::max(mx, pr);
    }
    if (mx < mn) return -1.0;
    return mx - mn;
}

// Model faces that share `edge` (oracle _faces_adjacent_to_edge): same edge centre
// + parallel direction. We iterate the body's faces and their edges.
std::vector<TopoDS_Face> faces_adjacent_to_edge(const TopoDS_Shape& body,
                                                const TopoDS_Edge& edge,
                                                const Eigen::Vector3d& target_mid,
                                                const Eigen::Vector3d& target_dir) {
    std::vector<TopoDS_Face> out;
    for (TopExp_Explorer fx(body, TopAbs_FACE); fx.More(); fx.Next()) {
        TopoDS_Face f = TopoDS::Face(fx.Current());
        bool matched = false;
        for (TopExp_Explorer ex(f, TopAbs_EDGE); ex.More() && !matched; ex.Next()) {
            TopoDS_Edge fe = TopoDS::Edge(ex.Current());
            double L = edge_length(fe);
            if (L <= 1e-9) continue;
            Eigen::Vector3d fec = edge_center(fe);
            if ((fec - target_mid).norm() > 1e-5) continue;
            Eigen::Vector3d a, b;
            edge_vertices(fe, a, b);
            Eigen::Vector3d d = b - a;
            if (d.norm() < 1e-9) continue;
            d.normalize();
            if (std::abs(std::abs(d.dot(target_dir)) - 1.0) < 1e-6) {
                out.push_back(f);
                matched = true;
            }
        }
    }
    return out;
}

// ---- 2-D polygon -> planar face -> prism (oracle _make_face_from_pts + _prism) ----
TopoDS_Shape make_face_from_pts(const std::vector<Eigen::Vector3d>& pts) {
    if (pts.size() < 3) return TopoDS_Shape();
    BRepBuilderAPI_MakeWire mw;
    const std::size_t n = pts.size();
    for (std::size_t i = 0; i < n; ++i) {
        const Eigen::Vector3d& a = pts[i];
        const Eigen::Vector3d& b = pts[(i + 1) % n];
        BRepBuilderAPI_MakeEdge me(gp_Pnt(a.x(), a.y(), a.z()),
                                   gp_Pnt(b.x(), b.y(), b.z()));
        if (!me.IsDone()) return TopoDS_Shape();
        mw.Add(me.Edge());
    }
    if (!mw.IsDone()) return TopoDS_Shape();
    BRepBuilderAPI_MakeFace mf(mw.Wire(), Standard_True);
    if (!mf.IsDone()) return TopoDS_Shape();
    return mf.Face();
}

TopoDS_Shape prism_face(const TopoDS_Shape& face, const Eigen::Vector3d& vec) {
    if (face.IsNull()) return TopoDS_Shape();
    BRepPrimAPI_MakePrism mk(face, gp_Vec(vec.x(), vec.y(), vec.z()), Standard_False,
                             Standard_True);
    if (!mk.IsDone()) return TopoDS_Shape();
    return mk.Shape();
}

// Three-point arcs retain the intended sweep, including hems over 180 degrees.
TopoDS_Edge section_arc(const Eigen::Vector3d& a, const Eigen::Vector3d& m,
                        const Eigen::Vector3d& b) {
    GC_MakeArcOfCircle arc(gp_Pnt(a.x(), a.y(), a.z()),
                           gp_Pnt(m.x(), m.y(), m.z()),
                           gp_Pnt(b.x(), b.y(), b.z()));
    if (!arc.IsDone()) return TopoDS_Edge();
    BRepBuilderAPI_MakeEdge edge(arc.Value());
    return edge.IsDone() ? edge.Edge() : TopoDS_Edge();
}

TopoDS_Edge section_line(const Eigen::Vector3d& a, const Eigen::Vector3d& b) {
    if ((b - a).norm() < 1e-9) return TopoDS_Edge();
    BRepBuilderAPI_MakeEdge edge(gp_Pnt(a.x(), a.y(), a.z()),
                                 gp_Pnt(b.x(), b.y(), b.z()));
    return edge.IsDone() ? edge.Edge() : TopoDS_Edge();
}

TopoDS_Shape section_prism(const std::vector<TopoDS_Edge>& left,
                           const std::vector<TopoDS_Edge>& right,
                           const Eigen::Vector3d& left_start,
                           const Eigen::Vector3d& right_start,
                           const Eigen::Vector3d& left_end,
                           const Eigen::Vector3d& right_end,
                           const Eigen::Vector3d& prism) {
    if (left.empty() || right.empty()) return TopoDS_Shape();
    BRepBuilderAPI_MakeWire wire;
    for (const auto& edge : left) {
        if (edge.IsNull()) return TopoDS_Shape();
        wire.Add(edge);
    }
    TopoDS_Edge end = section_line(left_end, right_end);
    TopoDS_Edge start = section_line(right_start, left_start);
    if (end.IsNull() || start.IsNull()) return TopoDS_Shape();
    wire.Add(end);
    for (auto it = right.rbegin(); it != right.rend(); ++it) {
        if (it->IsNull()) return TopoDS_Shape();
        wire.Add(TopoDS::Edge(it->Reversed()));
    }
    wire.Add(start);
    if (!wire.IsDone()) return TopoDS_Shape();
    BRepBuilderAPI_MakeFace face(wire.Wire(), Standard_True);
    if (!face.IsDone()) return TopoDS_Shape();
    return prism_face(face.Face(), prism);
}

}  // namespace

// --------------------------------------------------------------------------- bend math

double sm_bend_allowance(double angle_deg, double inner_radius, double thickness,
                         double k_factor) {
    return radians(angle_deg) * (inner_radius + k_factor * thickness);
}

// --------------------------------------------------------------------------- bend frame

BendFrame sm_bend_frame(const TopoDS_Shape& body, const TopoDS_Edge& edge,
                        double thickness) {
    BendFrame fr;

    // _edge_dir_and_endpoints: ordered vertices, chord direction, straightness check.
    Eigen::Vector3d p0, p1;
    edge_vertices(edge, p0, p1);
    Eigen::Vector3d chord = p1 - p0;
    double length = chord.norm();
    if (length < 1e-9) {
        fr.error = "sm_edge_flange: bend edge has zero length";
        return fr;
    }
    double arclen = edge_length(edge);
    if (std::abs(arclen - length) > 1e-4 * std::max(length, 1.0)) {
        fr.error = "sm_edge_flange: bend edge must be a straight line";
        return fr;
    }
    Eigen::Vector3d e_dir = chord * (1.0 / length);

    // _classify_bend_edge: the two planar faces meeting at the edge.
    Eigen::Vector3d mid = edge_center(edge);
    std::vector<TopoDS_Face> faces = faces_adjacent_to_edge(body, edge, mid, e_dir);
    std::vector<TopoDS_Face> planar;
    for (const TopoDS_Face& f : faces)
        if (is_planar(f)) planar.push_back(f);
    if (planar.size() < 2) {
        fr.error = "sm_edge_flange: bend edge is not shared by two planar faces";
        return fr;
    }
    // Thickness face: a planar face ~thickness wide perpendicular to the edge.
    bool have_thick = false, have_broad = false;
    TopoDS_Face thick_face, broad_face;
    for (const TopoDS_Face& f : planar) {
        double perp = face_extent_perp_to_edge(f, e_dir);
        if (perp >= 0.0 && std::abs(perp - thickness) < 0.05 * thickness + 1e-4) {
            thick_face = f;
            have_thick = true;
        } else {
            broad_face = f;
            have_broad = true;
        }
    }
    if (!have_thick) {
        fr.error = "sm_edge_flange: the picked edge is not on a thickness face of the sheet";
        return fr;
    }
    if (!have_broad) {
        fr.error = "sm_edge_flange: could not identify the parent sheet face for this edge";
        return fr;
    }

    Eigen::Vector3d n_broad = outward_normal(broad_face, body);
    Eigen::Vector3d n_thick = outward_normal(thick_face, body);

    // length_dir = n_thick projected into the broad-face plane.
    Eigen::Vector3d length_dir = n_thick - n_broad * n_thick.dot(n_broad);
    if (length_dir.norm() < 1e-9) length_dir = e_dir.cross(n_broad);
    length_dir.normalize();

    fr.ok = true;
    fr.p_start = p0;
    fr.p_end = p1;
    fr.e_dir = e_dir;
    fr.length_dir = length_dir;
    fr.n_broad = n_broad * (1.0 / std::max(n_broad.norm(), 1e-12));
    fr.edge_length = length;
    return fr;
}

// --------------------------------------------------------------------------- edge flange

TopoDS_Shape sm_edge_flange_solid(const Eigen::Vector3d& p_start,
                                  const Eigen::Vector3d& e_dir,
                                  const Eigen::Vector3d& length_dir,
                                  const Eigen::Vector3d& fold_dir,
                                  double edge_length, double thickness,
                                  double inner_radius, double angle_deg,
                                  double wall_length, const std::string& position) {
    const double T = thickness;
    const double R = inner_radius;
    const double ang = radians(angle_deg);

    auto xy = [&](double x, double y) -> Eigen::Vector3d {
        return p_start + length_dir * x + fold_dir * y;
    };

    double x0;
    if (position == "material_inside")       x0 = -(R + T);
    else if (position == "material_outside") x0 = -R;
    else                                     x0 = 0.0;  // bend_outside

    const double a0 = -PI / 2.0, a1 = a0 + ang;
    auto point = [&](double r, double a) {
        return xy(x0 + r * std::cos(a), R + r * std::sin(a));
    };
    const Eigen::Vector3d inner_start = point(R, a0);
    const Eigen::Vector3d outer_start = point(R + T, a0);
    const Eigen::Vector3d inner_end = point(R, a1);
    const Eigen::Vector3d outer_end = point(R + T, a1);
    const Eigen::Vector3d tangent =
        length_dir * (-std::sin(a1)) + fold_dir * std::cos(a1);
    const Eigen::Vector3d inner_far = inner_end + tangent * wall_length;
    const Eigen::Vector3d outer_far = outer_end + tangent * wall_length;
    std::vector<TopoDS_Edge> inner{
        section_arc(inner_start, point(R, (a0 + a1) / 2), inner_end),
        section_line(inner_end, inner_far)};
    std::vector<TopoDS_Edge> outer{
        section_arc(outer_start, point(R + T, (a0 + a1) / 2), outer_end),
        section_line(outer_end, outer_far)};
    return section_prism(inner, outer, inner_start, outer_start,
                         inner_far, outer_far, e_dir * edge_length);
}

// --------------------------------------------------------------------------- strip

TopoDS_Shape sm_strip_from_centerline(const Eigen::Vector3d& p_start,
                                      const Eigen::Vector3d& e_dir,
                                      const Eigen::Vector3d& length_dir,
                                      const Eigen::Vector3d& fold_dir,
                                      double edge_length, double thickness,
                                      const std::vector<StripOp>& ops,
                                      double start_x, double start_y) {
    const double half = thickness / 2.0;

    auto xy = [&](double x, double y) -> Eigen::Vector3d {
        return p_start + length_dir * x + fold_dir * y;
    };

    double px = start_x, py = start_y, heading = 0.0;
    std::vector<TopoDS_Edge> left, right;
    auto side = [&](double x, double y, double h, double sign) {
        return xy(x - sign * half * std::sin(h),
                  y + sign * half * std::cos(h));
    };
    const Eigen::Vector3d left_start = side(px, py, heading, 1);
    const Eigen::Vector3d right_start = side(px, py, heading, -1);
    for (const StripOp& op : ops) {
        if (op.kind == StripOp::Run) {
            if (std::abs(op.value) < 1e-9) continue;
            const double x = px + std::cos(heading) * op.value;
            const double y = py + std::sin(heading) * op.value;
            left.push_back(section_line(side(px, py, heading, 1), side(x, y, heading, 1)));
            right.push_back(section_line(side(px, py, heading, -1), side(x, y, heading, -1)));
            px = x; py = y;
        } else {
            const double sweep = (op.ccw ? 1.0 : -1.0) * radians(op.value);
            if (std::abs(sweep) < 1e-9 || op.r_center <= half) return TopoDS_Shape();
            const double turn = op.ccw ? 1.0 : -1.0;
            const double cx = px - turn * std::sin(heading) * op.r_center;
            const double cy = py + turn * std::cos(heading) * op.r_center;
            const double a0 = std::atan2(py - cy, px - cx);
            auto arc_point = [&](double fraction, double side_sign) {
                const double a = a0 + sweep * fraction;
                const double h = heading + sweep * fraction;
                return side(cx + op.r_center * std::cos(a),
                            cy + op.r_center * std::sin(a), h, side_sign);
            };
            left.push_back(section_arc(arc_point(0, 1), arc_point(0.5, 1), arc_point(1, 1)));
            right.push_back(section_arc(arc_point(0, -1), arc_point(0.5, -1), arc_point(1, -1)));
            px = cx + op.r_center * std::cos(a0 + sweep);
            py = cy + op.r_center * std::sin(a0 + sweep);
            heading += sweep;
        }
    }
    return section_prism(left, right, left_start, right_start,
                         side(px, py, heading, 1), side(px, py, heading, -1),
                         e_dir * edge_length);
}

// --------------------------------------------------------------------------- flat pattern

namespace {

double setback(const std::string& position, double R, double T) {
    if (position == "material_inside") return -(R + T);
    if (position == "material_outside") return -R;
    return 0.0;  // bend_outside
}

// A face strictly between the two sheet skins is a partial-depth pocket. A
// simple extruded flat face would turn it into a through-cut or erase it.
bool has_partial_depth_face(const TopoDS_Shape& body, const TopoDS_Shape& footprint,
                            const Eigen::Vector3d& origin, const Eigen::Vector3d& into,
                            double thickness) {
    for (TopExp_Explorer fx(body, TopAbs_FACE); fx.More(); fx.Next()) {
        const TopoDS_Face face = TopoDS::Face(fx.Current());
        Eigen::Vector3d normal, point;
        if (!face_plane_normal(face, normal, point) ||
            std::abs(std::abs(normal.dot(into)) - 1.0) > 1e-5)
            continue;
        const double depth = (point - origin).dot(into);
        if (depth <= 1e-5 || depth >= thickness - 1e-5) continue;
        gp_Trsf shift;
        shift.SetTranslation(gp_Vec(-into.x() * depth, -into.y() * depth, -into.z() * depth));
        const TopoDS_Shape projected = BRepBuilderAPI_Transform(face, shift, false).Shape();
        TopTools_ListOfShape args, tools;
        args.Append(projected);
        tools.Append(footprint);
        BRepAlgoAPI_Common op;
        op.SetArguments(args);
        op.SetTools(tools);
        op.SetNonDestructive(Standard_True);
        op.Build();
        if (!op.IsDone()) throw std::runtime_error("partial-depth cut check failed");
        if (op.Shape().IsNull()) continue;
        GProp_GProps area;
        BRepGProp::SurfaceProperties(op.Shape(), area);
        if (area.Mass() > 1e-8) return true;
    }
    return false;
}

// Clip every surviving coplanar base face to the original base footprint.
// This retains holes and disconnected portions split by later relief cuts
// without mistaking a large parallel flange for the base.
TopoDS_Shape flat_base_block(const TopoDS_Shape& body, const TopoDS_Shape& base,
                             const Eigen::Vector3d& n_base, const Eigen::Vector3d& anchor,
                             double thickness,
                             Eigen::Vector3d& into_dir,
                             Eigen::Vector3d& face_pt) {
    TopoDS_Face broad;
    bool have = false;
    double best_area = -1.0;
    for (TopExp_Explorer fx(base, TopAbs_FACE); fx.More(); fx.Next()) {
        TopoDS_Face f = TopoDS::Face(fx.Current());
        Eigen::Vector3d n, c;
        if (!face_plane_normal(f, n, c)) continue;
        n.normalize();
        if (std::abs(std::abs(n.dot(n_base)) - 1.0) >= 1e-4) continue;
        if (std::abs((anchor - c).dot(n)) > 1e-5) continue;
        GProp_GProps props;
        BRepGProp::SurfaceProperties(f, props);
        double area = props.Mass();
        if (area > best_area) {
            best_area = area;
            broad = f;
            have = true;
        }
    }
    if (!have) return TopoDS_Shape();

    Eigen::Vector3d face_n, c;
    face_plane_normal(broad, face_n, c);
    face_n.normalize();
    face_pt = face_center(broad);
    into_dir = -face_n;
    if (has_partial_depth_face(body, broad, face_pt, into_dir, thickness))
        throw std::runtime_error("partial-depth base cut cannot be developed without changing it");

    TopoDS_Shape result;
    for (TopExp_Explorer fx(body, TopAbs_FACE); fx.More(); fx.Next()) {
        const TopoDS_Face face = TopoDS::Face(fx.Current());
        Eigen::Vector3d n, p;
        if (!face_plane_normal(face, n, p) ||
            std::abs(std::abs(n.dot(face_n)) - 1.0) > 1e-5 ||
            std::abs((p - face_pt).dot(face_n)) > 1e-5)
            continue;
        const TopoDS_Shape clipped = common(face, broad);
        if (clipped.IsNull()) continue;
        for (TopExp_Explorer px(clipped, TopAbs_FACE); px.More(); px.Next()) {
            TopoDS_Face part = TopoDS::Face(px.Current());
            GProp_GProps area;
            BRepGProp::SurfaceProperties(part, area);
            if (area.Mass() <= 1e-8) continue;
            Eigen::Vector3d part_n, unused;
            if (!face_plane_normal(part, part_n, unused)) return TopoDS_Shape();
            if (part_n.dot(face_n) < 0) part.Reverse();
            TopoDS_Shape block = prism_face(part, into_dir * thickness);
            if (block.IsNull()) return TopoDS_Shape();
            result = result.IsNull() ? block : fuse(result, block);
            if (result.IsNull()) return TopoDS_Shape();
        }
    }
    return result;
}

// Older checkpoints lack fold_dir. Recover it by testing the two possible
// folded wall interiors against the saved B-rep; never guess on a tie.
Eigen::Vector3d recovered_fold(const TopoDS_Shape& body, const BendRecord& rec) {
    if (rec.fold_dir.norm() > 1e-9) return rec.fold_dir.normalized();
    TopExp_Explorer solids(body, TopAbs_SOLID);
    if (!solids.More()) return Eigen::Vector3d::Zero();
    BRepClass3d_SolidClassifier classifier(solids.Current());
    const Eigen::Vector3d e = rec.e_dir.normalized();
    const Eigen::Vector3d l = rec.length_dir.normalized();
    const Eigen::Vector3d normal = e.cross(l).normalized();
    const double theta = radians(rec.angle);
    const double x0 = setback(rec.position, rec.radius, rec.thickness);
    const double width = (rec.p_end - rec.p_start).norm();
    int scores[2] = {0, 0};
    for (int k = 0; k < 2; ++k) {
        Eigen::Vector3d f = normal;
        if (k) f = -f;
        const Eigen::Vector3d wall_dir = l * std::cos(theta) + f * std::sin(theta);
        const Eigen::Vector3d radial = l * std::sin(theta) - f * std::cos(theta);
        const Eigen::Vector3d tangent = rec.p_start + l * (x0 + rec.radius * std::sin(theta))
            + f * (rec.radius * (1.0 - std::cos(theta)));
        for (double a : {0.25, 0.5, 0.75})
            for (double b : {0.25, 0.5, 0.75}) {
                const Eigen::Vector3d p = tangent + e * (width * a)
                    + wall_dir * (rec.wall_length * b) + radial * (rec.thickness / 2);
                classifier.Perform(gp_Pnt(p.x(), p.y(), p.z()), 1e-7);
                if (classifier.State() == TopAbs_IN) ++scores[k];
            }
    }
    if ((scores[0] == 0) == (scores[1] == 0)) return Eigen::Vector3d::Zero();
    if (scores[0] > scores[1]) return normal;
    return -normal;
}

template <typename Map>
bool develop_trimmed_face(const TopoDS_Face& face, const Map& map_uv,
                          const Eigen::Vector3d& start, const Eigen::Vector3d& flat_along,
                          double allowance, const Eigen::Vector3d& into_dir, double thickness,
                          std::vector<TopoDS_Shape>& patches,
                          std::vector<std::array<Eigen::Vector3d, 2>>& seams) {
    const TopoDS_Wire outer = BRepTools::OuterWire(face);
    if (outer.IsNull()) return false;
    TopoDS_Wire developed_outer;
    std::vector<TopoDS_Wire> holes;
    for (TopExp_Explorer wx(face, TopAbs_WIRE); wx.More(); wx.Next()) {
        const TopoDS_Wire wire = TopoDS::Wire(wx.Current());
        BRepBuilderAPI_MakeWire developed;
        for (BRepTools_WireExplorer ex(wire, face); ex.More(); ex.Next()) {
            const TopoDS_Edge edge = ex.Current();
            if (BRep_Tool::Degenerated(edge)) continue;
            double first, last;
            opencascade::handle<Geom2d_Curve> pc =
                BRep_Tool::CurveOnSurface(edge, face, first, last);
            if (pc.IsNull() || !(last > first)) return false;
            opencascade::handle<Geom2d_TrimmedCurve> trimmed =
                new Geom2d_TrimmedCurve(pc, first, last);
            opencascade::handle<Geom2d_BSplineCurve> spline =
                Geom2dConvert::CurveToBSplineCurve(trimmed);
            if (spline.IsNull()) return false;
            TColgp_Array1OfPnt poles(1, spline->NbPoles());
            for (int i = 1; i <= spline->NbPoles(); ++i) {
                const Eigen::Vector3d p = map_uv(spline->Pole(i));
                poles.SetValue(i, gp_Pnt(p.x(), p.y(), p.z()));
            }
            opencascade::handle<Geom_BSplineCurve> curve;
            if (spline->IsRational()) {
                TColStd_Array1OfReal weights(1, spline->NbPoles());
                for (int i = 1; i <= spline->NbPoles(); ++i)
                    weights.SetValue(i, spline->Weight(i));
                curve = new Geom_BSplineCurve(poles, weights, spline->Knots(),
                                              spline->Multiplicities(), spline->Degree(),
                                              spline->IsPeriodic());
            } else {
                curve = new Geom_BSplineCurve(poles, spline->Knots(),
                                              spline->Multiplicities(), spline->Degree(),
                                              spline->IsPeriodic());
            }
            BRepBuilderAPI_MakeEdge mk(curve);
            if (!mk.IsDone()) return false;
            TopoDS_Edge mapped = mk.Edge();
            if (edge.Orientation() == TopAbs_REVERSED) mapped.Reverse();
            developed.Add(mapped);
            const Eigen::Vector3d a = map_uv(pc->Value(first));
            const Eigen::Vector3d b = map_uv(pc->Value(last));
            const Eigen::Vector3d m = map_uv(pc->Value((first + last) / 2));
            for (double seam : {0.0, allowance})
                if (std::abs((a - start).dot(flat_along) - seam) < 1e-6 &&
                    std::abs((b - start).dot(flat_along) - seam) < 1e-6 &&
                    std::abs((m - start).dot(flat_along) - seam) < 1e-6 &&
                    (a - b).norm() > 1e-7)
                    seams.push_back({a, b});
        }
        if (!developed.IsDone()) return false;
        if (wire.IsSame(outer)) developed_outer = developed.Wire();
        else holes.push_back(developed.Wire());
    }
    if (developed_outer.IsNull()) return false;
    BRepBuilderAPI_MakeFace builder(developed_outer, Standard_True);
    if (!builder.IsDone()) return false;
    for (const TopoDS_Wire& hole : holes) builder.Add(hole);
    if (!builder.IsDone()) return false;
    TopoDS_Face developed_face = builder.Face();
    Eigen::Vector3d normal, point;
    if (!BRepCheck_Analyzer(developed_face).IsValid() ||
        !face_plane_normal(developed_face, normal, point)) return false;
    if (normal.dot(-into_dir) < 0) developed_face.Reverse();
    TopoDS_Shape patch = prism_face(developed_face, into_dir * thickness);
    if (patch.IsNull()) return false;
    patches.push_back(patch);
    return true;
}

// Old saved bends used straight chords. Develop each surviving chord face from
// its actual pcurves, preserving its cuts, without re-solving the cached part.
bool develop_legacy_bend(const TopoDS_Shape& body, const BendRecord& rec,
                         const Eigen::Vector3d& fold, const Eigen::Vector3d& start,
                         const Eigen::Vector3d& flat_edge, const Eigen::Vector3d& flat_along,
                         const Eigen::Vector3d& into_dir, TopoDS_Shape& flat,
                         std::vector<std::array<Eigen::Vector3d, 2>>& bend_lines) {
    const Eigen::Vector3d edge = rec.e_dir.normalized();
    const Eigen::Vector3d along = rec.length_dir.normalized();
    const Eigen::Vector3d center = rec.p_start
        + along * setback(rec.position, rec.radius, rec.thickness) + fold * rec.radius;
    const double width = (rec.p_end - rec.p_start).norm();
    const double theta = radians(rec.angle);
    const double allowance = std::abs(rec.bend_allowance);
    auto fraction = [&](const Eigen::Vector3d& p) {
        const Eigen::Vector3d radial = p - center;
        double a = std::atan2(radial.dot(along), -radial.dot(fold));
        if (theta > 0 && a < -1e-7) a += 2 * PI;
        if (theta < 0 && a > 1e-7) a -= 2 * PI;
        return a / theta;
    };
    for (double radius : {rec.radius, rec.radius + rec.thickness}) {
        std::vector<TopoDS_Shape> patches;
        std::vector<std::array<Eigen::Vector3d, 2>> seams;
        for (TopExp_Explorer fx(body, TopAbs_FACE); fx.More(); fx.Next()) {
            const TopoDS_Face face = TopoDS::Face(fx.Current());
            Eigen::Vector3d n, location;
            if (!face_plane_normal(face, n, location) || std::abs(n.dot(edge)) > 1e-6)
                continue;
            n.normalize();
            const double h = (location - center).dot(n);
            if (std::abs(h) >= radius - 1e-10 ||
                std::abs(h) < radius * std::cos(radians(3.5)) - 1e-5)
                continue;
            const double half_chord = std::sqrt(std::max(0.0, radius * radius - h * h));
            const Eigen::Vector3d chord_dir = edge.cross(n).normalized();
            const Eigen::Vector3d a = center + n * h - chord_dir * half_chord;
            const Eigen::Vector3d b = center + n * h + chord_dir * half_chord;
            const Eigen::Vector3d chord = b - a;
            const double ta = fraction(a), tb = fraction(b);
            if (ta < -1e-5 || ta > 1.0 + 1e-5 || tb < -1e-5 || tb > 1.0 + 1e-5 ||
                std::abs((tb - ta) * theta) > radians(7.0) + 1e-5)
                continue;
            bool inside = true;
            int vertices = 0;
            for (TopExp_Explorer vx(face, TopAbs_VERTEX); vx.More(); vx.Next()) {
                const gp_Pnt p = BRep_Tool::Pnt(TopoDS::Vertex(vx.Current()));
                const Eigen::Vector3d q(p.X(), p.Y(), p.Z());
                const double axial = (q - rec.p_start).dot(edge);
                const double t = (q - a).dot(chord) / chord.squaredNorm();
                if (axial < -1e-5 || axial > width + 1e-5 ||
                    t < -1e-5 || t > 1.0 + 1e-5) inside = false;
                ++vertices;
            }
            if (!inside || vertices < 2) continue;
            BRepAdaptor_Surface surface(face);
            const gp_Pnt p0 = surface.Value(0, 0);
            const gp_Pnt pu = surface.Value(1, 0), pv = surface.Value(0, 1);
            const Eigen::Vector3d origin(p0.X(), p0.Y(), p0.Z());
            const Eigen::Vector3d u(pu.X() - p0.X(), pu.Y() - p0.Y(), pu.Z() - p0.Z());
            const Eigen::Vector3d v(pv.X() - p0.X(), pv.Y() - p0.Y(), pv.Z() - p0.Z());
            auto map_uv = [&](const gp_Pnt2d& uv) -> Eigen::Vector3d {
                const Eigen::Vector3d p = origin + u * uv.X() + v * uv.Y();
                const double t = (p - a).dot(chord) / chord.squaredNorm();
                return start + flat_edge * ((p - rec.p_start).dot(edge))
                    + flat_along * (allowance * (ta + (tb - ta) * t));
            };
            if (!develop_trimmed_face(face, map_uv, start, flat_along, allowance,
                                      into_dir, rec.thickness, patches, seams))
                return false;
        }
        if (patches.empty()) continue;
        for (const TopoDS_Shape& patch : patches) {
            flat = fuse(flat, patch);
            if (flat.IsNull()) return false;
        }
        bend_lines.insert(bend_lines.end(), seams.begin(), seams.end());
        return true;
    }
    return false;
}

// Flatten the actual trimmed cylindrical faces, including their inner wires.
// Cylinder (u,v) -> developed (neutral arc length, axial distance) is affine,
// so transforming a pcurve's B-spline poles preserves even non-rectangular cuts.
bool develop_bend_faces(const TopoDS_Shape& body, const BendRecord& rec,
                        const Eigen::Vector3d& fold, const Eigen::Vector3d& start,
                        const Eigen::Vector3d& flat_edge, const Eigen::Vector3d& flat_along,
                        const Eigen::Vector3d& into_dir, TopoDS_Shape& flat,
                        std::vector<std::array<Eigen::Vector3d, 2>>& bend_lines) {
    const Eigen::Vector3d e = rec.e_dir.normalized();
    const Eigen::Vector3d l = rec.length_dir.normalized();
    const Eigen::Vector3d center = rec.p_start
        + l * setback(rec.position, rec.radius, rec.thickness) + fold * rec.radius;
    const double width = (rec.p_end - rec.p_start).norm();
    const double theta = radians(rec.angle);
    const double ba = std::abs(rec.bend_allowance);
    for (double radius : {rec.radius, rec.radius + rec.thickness}) {
        std::vector<TopoDS_Face> faces;
        for (TopExp_Explorer fx(body, TopAbs_FACE); fx.More(); fx.Next()) {
            TopoDS_Face face = TopoDS::Face(fx.Current());
            BRepAdaptor_Surface surf(face);
            if (surf.GetType() != GeomAbs_Cylinder) continue;
            const gp_Cylinder cyl = surf.Cylinder();
            if (std::abs(cyl.Radius() - radius) > 1e-5) continue;
            const gp_Dir axis = cyl.Axis().Direction();
            const Eigen::Vector3d d(axis.X(), axis.Y(), axis.Z());
            if (std::abs(std::abs(d.dot(e)) - 1.0) > 1e-6) continue;
            const gp_Pnt loc = cyl.Axis().Location();
            const Eigen::Vector3d delta = Eigen::Vector3d(loc.X(), loc.Y(), loc.Z()) - center;
            if ((delta - e * delta.dot(e)).norm() <= 1e-5) faces.push_back(face);
        }
        if (faces.empty()) continue;
        std::vector<TopoDS_Shape> patches;
        std::vector<std::array<Eigen::Vector3d, 2>> seams;
        for (const TopoDS_Face& face : faces) {
            BRepAdaptor_Surface surface(face);
            const gp_Cylinder cylinder = surface.Cylinder();
            const gp_Ax3 frame = cylinder.Position();
            const gp_Dir dx = frame.XDirection(), dy = frame.YDirection(), dz = frame.Direction();
            const Eigen::Vector3d x(dx.X(), dx.Y(), dx.Z());
            const Eigen::Vector3d y(dy.X(), dy.Y(), dy.Z());
            const Eigen::Vector3d axis(dz.X(), dz.Y(), dz.Z());
            const gp_Pnt location = frame.Location();
            const Eigen::Vector3d origin(location.X(), location.Y(), location.Z());
            double u_start = std::atan2((-fold).dot(y), (-fold).dot(x));
            const double sign = (-x * std::sin(u_start) + y * std::cos(u_start)).dot(l) >= 0
                                    ? 1.0 : -1.0;
            double u0, u1, v0, v1;
            BRepTools::UVBounds(face, u0, u1, v0, v1);
            u_start += 2 * PI * std::round(((u0 + u1) / 2 - u_start - sign * theta / 2) / (2 * PI));
            const double begin = std::min(u_start, u_start + sign * theta);
            const double end = std::max(u_start, u_start + sign * theta);
            const double axial_origin = (origin - rec.p_start).dot(e);
            const double axial_sign = axis.dot(e);
            const double axial0 = axial_origin + axial_sign * v0;
            const double axial1 = axial_origin + axial_sign * v1;
            if (u0 < begin - 1e-5 || u1 > end + 1e-5 ||
                std::min(axial0, axial1) < -1e-5 ||
                std::max(axial0, axial1) > width + 1e-5)
                return false;  // The face belongs to a larger/ambiguous bend.
            auto map_uv = [&](const gp_Pnt2d& uv) -> Eigen::Vector3d {
                return start + flat_edge * (axial_origin + axial_sign * uv.Y())
                    + flat_along * (ba * sign * (uv.X() - u_start) / theta);
            };
            if (!develop_trimmed_face(face, map_uv, start, flat_along, ba, into_dir,
                                      rec.thickness, patches, seams))
                return false;
        }
        for (const TopoDS_Shape& patch : patches) {
            flat = fuse(flat, patch);
            if (flat.IsNull()) return false;
        }
        bend_lines.insert(bend_lines.end(), seams.begin(), seams.end());
        return !patches.empty();
    }
    return rec.fold_dir.norm() < 1e-9 &&
        develop_legacy_bend(body, rec, fold, start, flat_edge, flat_along,
                            into_dir, flat, bend_lines);
}

}  // namespace

FlatPattern sm_flat_pattern(const TopoDS_Shape& body, const TopoDS_Shape& base,
                            double thickness, const std::vector<BendRecord>& bends) {
    FlatPattern fp;
    auto fail = [&](const std::string& message) {
        fp.solid.Nullify();
        fp.error = message;
        return fp;
    };
    if (bends.empty()) return fail("no bends to develop");
    if (bends[0].e_dir.norm() < 1e-9 || bends[0].length_dir.norm() < 1e-9)
        return fail("base bend frame is degenerate");
    const Eigen::Vector3d e0 = bends[0].e_dir.normalized();
    const Eigen::Vector3d l0 = bends[0].length_dir.normalized();
    Eigen::Vector3d n_base = e0.cross(l0);
    if (n_base.norm() < 1e-9) return fail("base bend frame is degenerate");
    n_base.normalize();
    fp.n_base = n_base;
    Eigen::Vector3d into_dir, face_pt;
    TopoDS_Shape flat;
    try {
        flat = flat_base_block(body, base, n_base, bends[0].p_start,
                               thickness, into_dir, face_pt);
    } catch (const Standard_Failure& exc) {
        const char* message = exc.GetMessageString();
        return fail(std::string("base development failed: ") + (message ? message : "OCCT error"));
    } catch (const std::exception& exc) {
        return fail(std::string("base development failed: ") + exc.what());
    }
    if (flat.IsNull()) return fail("base broad face could not be developed");

    // Each wall maps from its folded frame to the one common developed frame.
    struct Panel {
        Eigen::Vector3d origin, edge, along;
        double width, length;
        Eigen::Matrix3d rotation;
        Eigen::Vector3d translation;
    };
    std::vector<Panel> panels;
    const Eigen::Matrix3d identity = Eigen::Matrix3d::Identity();
    auto on_flat = [&](const Eigen::Vector3d& p) {
        return Eigen::Vector3d(p - n_base * ((p - face_pt).dot(n_base)));
    };
    for (std::size_t index = 0; index < bends.size(); ++index) {
        const BendRecord& rec = bends[index];
        const std::string step = "bend " + std::to_string(index + 1) + ": ";
        const double R = rec.radius, T = rec.thickness;
        const double ba = std::abs(rec.bend_allowance), wall = rec.wall_length;
        const double width = (rec.p_end - rec.p_start).norm();
        if (!(R > 0 && T > 0 && ba > 0 && wall >= 0 && width > 0) ||
            rec.e_dir.norm() < 1e-9 || rec.length_dir.norm() < 1e-9)
            return fail(step + "invalid recorded frame or dimensions");
        const Eigen::Vector3d edge = rec.e_dir.normalized();
        const Eigen::Vector3d along = rec.length_dir.normalized();
        const Eigen::Vector3d fold = recovered_fold(body, rec);
        if (fold.norm() < 1e-9 || std::abs(fold.dot(along)) > 1e-5)
            return fail(step + "fold direction could not be recovered unambiguously");
        const double theta = radians(rec.angle);
        const double x0 = setback(rec.position, R, T);

        const Panel* parent = nullptr;
        auto on_panel = [&](const Panel& panel) {
            const Eigen::Vector3d d = rec.p_start - panel.origin;
            const Eigen::Vector3d normal = panel.edge.cross(panel.along).normalized();
            const double u = d.dot(panel.edge), v = d.dot(panel.along);
            return std::abs(d.dot(normal)) <= T + 1e-5 &&
                u >= -T - 1e-5 && u <= panel.width + T + 1e-5 &&
                v >= -T - 1e-5 && v <= panel.length + T + 1e-5;
        };
        const bool on_base = std::abs((rec.p_start - face_pt).dot(n_base)) <= T + 1e-5;
        if (rec.parent_index >= 0) {
            if (static_cast<std::size_t>(rec.parent_index) >= panels.size() ||
                !on_panel(panels[static_cast<std::size_t>(rec.parent_index)]))
                return fail(step + "recorded parent wall is unavailable");
            parent = &panels[static_cast<std::size_t>(rec.parent_index)];
        } else if (rec.parent_index == -1) {
            if (!on_base) return fail(step + "recorded base parent is unavailable");
        } else if (rec.parent_index == -2) {
            int matches = on_base ? 1 : 0;
            for (const Panel& panel : panels)
                if (on_panel(panel)) {
                    parent = &panel;
                    ++matches;
                }
            if (matches != 1) return fail(step + "parent wall is missing or ambiguous");
        } else {
            return fail(step + "invalid parent bend index");
        }
        const Eigen::Matrix3d rotation = parent ? parent->rotation : identity;
        Eigen::Vector3d translation = Eigen::Vector3d::Zero();
        if (parent) translation = parent->translation;
        const Eigen::Vector3d flat_edge = (rotation * edge).normalized();
        const Eigen::Vector3d flat_along = (rotation * along).normalized();
        if (std::abs(flat_edge.dot(n_base)) > 1e-5 ||
            std::abs(flat_along.dot(n_base)) > 1e-5)
            return fail(step + "parent frame does not map to the base plane");
        const Eigen::Vector3d start = on_flat(rotation * (rec.p_start + along * x0) + translation);
        const Eigen::Vector3d wall_start = start + flat_along * ba;

        try {
            if (!develop_bend_faces(body, rec, fold, start, flat_edge, flat_along,
                                    into_dir, flat, fp.bend_lines))
                return fail(step + "trimmed bend surface could not be developed safely");
        } catch (const Standard_Failure& exc) {
            const char* message = exc.GetMessageString();
            return fail(step + "bend development failed: " + (message ? message : "OCCT error"));
        } catch (const std::exception& exc) {
            return fail(step + "bend development failed: " + exc.what());
        }

        const Eigen::Vector3d folded_wall_start = rec.p_start
            + along * (x0 + R * std::sin(theta)) + fold * (R * (1.0 - std::cos(theta)));
        const Eigen::Vector3d wall_dir = (along * std::cos(theta) + fold * std::sin(theta)).normalized();
        Eigen::Matrix3d source_basis, flat_basis;
        source_basis.col(0) = edge; source_basis.col(1) = wall_dir;
        source_basis.col(2) = edge.cross(wall_dir).normalized();
        flat_basis.col(0) = flat_edge; flat_basis.col(1) = flat_along;
        flat_basis.col(2) = flat_edge.cross(flat_along).normalized();
        const Eigen::Matrix3d wall_rotation = flat_basis * source_basis.transpose();
        const Eigen::Vector3d wall_translation = wall_start - wall_rotation * folded_wall_start;
        if (wall <= 1e-9) {
            panels.push_back({folded_wall_start, edge, wall_dir, width, wall,
                              wall_rotation, wall_translation});
            continue;
        }
        try {
            gp_Trsf transform;
            transform.SetValues(wall_rotation(0, 0), wall_rotation(0, 1), wall_rotation(0, 2), wall_translation.x(),
                                wall_rotation(1, 0), wall_rotation(1, 1), wall_rotation(1, 2), wall_translation.y(),
                                wall_rotation(2, 0), wall_rotation(2, 1), wall_rotation(2, 2), wall_translation.z());
            const Eigen::Vector3d wall_end = folded_wall_start + wall_dir * wall;
            TopoDS_Shape window = make_face_from_pts(
                {folded_wall_start, folded_wall_start + edge * width, wall_end + edge * width, wall_end});
            if (window.IsNull()) return fail(step + "wall window construction failed");
            const Eigen::Vector3d wall_into = along * std::sin(theta) - fold * std::cos(theta);
            if (has_partial_depth_face(body, window, folded_wall_start, wall_into, T))
                return fail(step + "partial-depth wall cut cannot be developed without changing it");
            const Eigen::Vector3d wall_normal = edge.cross(wall_dir).normalized();
            int faces = 0;
            for (TopExp_Explorer fx(body, TopAbs_FACE); fx.More(); fx.Next()) {
                const TopoDS_Face source = TopoDS::Face(fx.Current());
                Eigen::Vector3d normal, point;
                if (!face_plane_normal(source, normal, point) ||
                    std::abs(std::abs(normal.dot(wall_normal)) - 1.0) > 1e-5 ||
                    std::abs((point - folded_wall_start).dot(wall_normal)) > 1e-5)
                    continue;
                const TopoDS_Shape clipped = common(source, window);
                if (clipped.IsNull()) continue;
                for (TopExp_Explorer px(clipped, TopAbs_FACE); px.More(); px.Next()) {
                    TopoDS_Face face = TopoDS::Face(px.Current());
                    GProp_GProps area;
                    BRepGProp::SurfaceProperties(face, area);
                    if (area.Mass() <= 1e-8) continue;
                    face = TopoDS::Face(BRepBuilderAPI_Transform(face, transform, true).Shape());
                    Eigen::Vector3d mapped_normal, unused;
                    if (!face_plane_normal(face, mapped_normal, unused))
                        return fail(step + "wall face transformation failed");
                    if (mapped_normal.dot(-into_dir) < 0) face.Reverse();
                    TopoDS_Shape patch = prism_face(face, into_dir * T);
                    if (patch.IsNull()) return fail(step + "wall extrusion failed");
                    flat = fuse(flat, patch);
                    if (flat.IsNull()) return fail(step + "wall fuse failed");
                    ++faces;
                }
            }
            if (!faces) return fail(step + "wall face could not be found");
        } catch (const Standard_Failure& exc) {
            const char* message = exc.GetMessageString();
            return fail(step + "wall development failed: " + (message ? message : "OCCT error"));
        } catch (const std::exception& exc) {
            return fail(step + "wall development failed: " + exc.what());
        }
        panels.push_back({folded_wall_start, edge, wall_dir, width, wall,
                          wall_rotation, wall_translation});
    }
    flat = unify_same_domain(flat);
    int solids = 0;
    for (TopExp_Explorer sx(flat, TopAbs_SOLID); sx.More(); sx.Next()) ++solids;
    if (solids != 1 || !BRepCheck_Analyzer(flat).IsValid())
        return fail("developed blank is not one valid connected solid");
    fp.solid = flat;
    return fp;
}

}  // namespace geom
