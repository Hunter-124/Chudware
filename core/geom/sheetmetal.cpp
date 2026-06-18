// Sheet-metal feature geometry — faithful C++ port of engine/doc/features/
// sheetmetal.py (+ sheetmetal_flat.py). See sheetmetal.h for the frame convention.
//
// PARITY DISCIPLINE: every analytic construction (arc segmentation count, polygon
// vertex order, dedup tolerance, prism direction) mirrors the oracle EXACTLY so the
// resulting BRep volume matches to the differential gate's 1e-6 relative tolerance.
#include "geom/sheetmetal.h"

#include <algorithm>
#include <cmath>
#include <limits>
#include <string>
#include <vector>

#include "geom/booleans.h"

#include <BRep_Tool.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepBndLib.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <GProp_GProps.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <Bnd_Box.hxx>
#include <gp_Dir.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

namespace geom {

namespace {

constexpr double PI = 3.14159265358979323846;

inline double radians(double deg) { return deg * PI / 180.0; }

// A planar face's geometric normal (unit) at its centre, via BRepAdaptor. Matches
// build123d Face.normal_at() up to orientation (the oracle re-orients separately).
bool face_plane_normal(const TopoDS_Face& f, Eigen::Vector3d& n_out,
                       Eigen::Vector3d& c_out) {
    BRepAdaptor_Surface surf(f);
    if (surf.GetType() != GeomAbs_Plane) return false;
    gp_Pln pln = surf.Plane();
    gp_Dir d = pln.Axis().Direction();
    n_out = Eigen::Vector3d(d.X(), d.Y(), d.Z());
    // build123d normal_at() flips the geometric normal when the face is REVERSED.
    if (f.Orientation() == TopAbs_REVERSED) n_out = -n_out;
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

// Drop consecutive near-duplicate points + the closing duplicate (oracle's cleanup
// before _make_face_from_pts). tol = 1e-7.
std::vector<Eigen::Vector3d> clean_loop(const std::vector<Eigen::Vector3d>& pts) {
    std::vector<Eigen::Vector3d> cleaned;
    for (const Eigen::Vector3d& v : pts) {
        if (cleaned.empty() || (v - cleaned.back()).norm() > 1e-7) cleaned.push_back(v);
    }
    if (cleaned.size() >= 2 && (cleaned.front() - cleaned.back()).norm() <= 1e-7)
        cleaned.pop_back();
    return cleaned;
}

}  // namespace

// --------------------------------------------------------------------------- bend math

double sm_bend_allowance(double angle_deg, double inner_radius, double thickness,
                         double k_factor) {
    return radians(angle_deg) * (inner_radius + k_factor * thickness);
}

double sm_bend_deduction(double angle_deg, double inner_radius, double thickness,
                         double k_factor) {
    double ba = sm_bend_allowance(angle_deg, inner_radius, thickness, k_factor);
    double ossb = (inner_radius + thickness) * std::tan(radians(angle_deg) / 2.0);
    return 2.0 * ossb - ba;
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

    const double cx = x0;
    const double cy = R;
    const double a_start = -PI / 2.0;
    const int nseg = std::max(8, static_cast<int>(angle_deg / 6.0));

    std::vector<std::pair<double, double>> inner_pts, outer_pts;
    inner_pts.reserve(nseg + 1);
    outer_pts.reserve(nseg + 1);
    for (int i = 0; i <= nseg; ++i) {
        double a = a_start + ang * (static_cast<double>(i) / nseg);
        inner_pts.emplace_back(cx + R * std::cos(a), cy + R * std::sin(a));
        outer_pts.emplace_back(cx + (R + T) * std::cos(a), cy + (R + T) * std::sin(a));
    }

    const double a_end = a_start + ang;
    const double radx = std::cos(a_end), rady = std::sin(a_end);
    const double tangx = -rady, tangy = radx;
    const double inner_end_x = cx + R * radx, inner_end_y = cy + R * rady;
    const double outer_end_x = cx + (R + T) * radx, outer_end_y = cy + (R + T) * rady;
    const std::pair<double, double> wall_inner_far{inner_end_x + tangx * wall_length,
                                                   inner_end_y + tangy * wall_length};
    const std::pair<double, double> wall_outer_far{outer_end_x + tangx * wall_length,
                                                   outer_end_y + tangy * wall_length};

    std::vector<std::pair<double, double>> poly2d;
    for (const auto& p : inner_pts) poly2d.push_back(p);
    poly2d.push_back(wall_inner_far);
    poly2d.push_back(wall_outer_far);
    for (auto it = outer_pts.rbegin(); it != outer_pts.rend(); ++it) poly2d.push_back(*it);

    std::vector<Eigen::Vector3d> pts3d;
    pts3d.reserve(poly2d.size());
    for (const auto& p : poly2d) pts3d.push_back(xy(p.first, p.second));
    std::vector<Eigen::Vector3d> cleaned = clean_loop(pts3d);

    TopoDS_Shape section = make_face_from_pts(cleaned);
    return prism_face(section, e_dir * edge_length);
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
    std::vector<std::pair<double, double>> inner, outer;

    auto emit = [&](double x, double y, double h) {
        double nx = -std::sin(h), ny = std::cos(h);
        outer.emplace_back(x + nx * half, y + ny * half);
        inner.emplace_back(x - nx * half, y - ny * half);
    };

    emit(px, py, heading);
    for (const StripOp& op : ops) {
        if (op.kind == StripOp::Run) {
            double L = op.value;
            px += std::cos(heading) * L;
            py += std::sin(heading) * L;
            emit(px, py, heading);
        } else {
            double ang = radians(op.value);
            double Rc = op.r_center;
            double sign = op.ccw ? 1.0 : -1.0;
            double cnx = -std::sin(heading), cny = std::cos(heading);
            double cx = px + sign * cnx * Rc;
            double cy = py + sign * cny * Rc;
            double a0 = std::atan2(py - cy, px - cx);
            int nseg = std::max(8, static_cast<int>(op.value / 6.0));
            for (int i = 1; i <= nseg; ++i) {
                double a = a0 + sign * ang * (static_cast<double>(i) / nseg);
                double cpx = cx + Rc * std::cos(a);
                double cpy = cy + Rc * std::sin(a);
                double h = heading + sign * ang * (static_cast<double>(i) / nseg);
                emit(cpx, cpy, h);
            }
            px = cx + Rc * std::cos(a0 + sign * ang);
            py = cy + Rc * std::sin(a0 + sign * ang);
            heading += sign * ang;
        }
    }

    std::vector<std::pair<double, double>> poly2d = outer;
    for (auto it = inner.rbegin(); it != inner.rend(); ++it) poly2d.push_back(*it);

    std::vector<Eigen::Vector3d> pts3d;
    pts3d.reserve(poly2d.size());
    for (const auto& p : poly2d) pts3d.push_back(xy(p.first, p.second));
    std::vector<Eigen::Vector3d> cleaned = clean_loop(pts3d);
    if (cleaned.size() < 3) return TopoDS_Shape();

    TopoDS_Shape section = make_face_from_pts(cleaned);
    return prism_face(section, e_dir * edge_length);
}

// --------------------------------------------------------------------------- flat pattern

namespace {

double setback(const std::string& position, double R, double T) {
    if (position == "material_inside") return -(R + T);
    if (position == "material_outside") return -R;
    return 0.0;  // bend_outside
}

// The flat base block: re-extrude the largest broad face (normal ∥ n_base) by
// thickness INTO the solid (oracle _flat_base_block). Returns the block + the into
// direction + a point on the picked broad-face plane.
TopoDS_Shape flat_base_block(const TopoDS_Shape& body, const Eigen::Vector3d& n_base,
                             double thickness, Eigen::Vector3d& into_dir,
                             Eigen::Vector3d& face_pt) {
    TopoDS_Face broad;
    bool have = false;
    double best_area = -1.0;
    for (TopExp_Explorer fx(body, TopAbs_FACE); fx.More(); fx.Next()) {
        TopoDS_Face f = TopoDS::Face(fx.Current());
        Eigen::Vector3d n, c;
        if (!face_plane_normal(f, n, c)) continue;
        n.normalize();
        if (std::abs(std::abs(n.dot(n_base)) - 1.0) >= 1e-4) continue;
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
    Eigen::Vector3d body_c = body_bbox_center(body);
    into_dir = (face_n.dot(body_c - face_pt) > 0) ? face_n : -face_n;
    into_dir.normalize();

    return prism_face(broad, into_dir * thickness);
}

}  // namespace

TopoDS_Shape sm_flat_pattern_solid(const TopoDS_Shape& body, double thickness,
                                   const std::vector<BendRecord>& bends) {
    if (bends.empty()) return TopoDS_Shape();

    // Base-plane frame from the first bend.
    Eigen::Vector3d e0 = bends[0].e_dir.normalized();
    Eigen::Vector3d l0 = bends[0].length_dir.normalized();
    Eigen::Vector3d n_base = e0.cross(l0);
    n_base.normalize();

    Eigen::Vector3d into_dir, face_pt;
    TopoDS_Shape flat = flat_base_block(body, n_base, thickness, into_dir, face_pt);
    if (flat.IsNull()) return TopoDS_Shape();

    auto to_face_plane = [&](const Eigen::Vector3d& pt) -> Eigen::Vector3d {
        return pt - n_base * ((pt - face_pt).dot(n_base));
    };

    for (const BendRecord& rec : bends) {
        const double R = rec.radius, T = rec.thickness;
        const double ba = rec.bend_allowance, wall = rec.wall_length;
        Eigen::Vector3d e_dir = rec.e_dir.normalized();
        Eigen::Vector3d length_dir = rec.length_dir.normalized();
        const Eigen::Vector3d p0 = rec.p_start;
        const double edge_len = (rec.p_end - rec.p_start).norm();

        const double x0 = setback(rec.position, R, T);
        Eigen::Vector3d raw_tangent = p0 + length_dir * x0;
        Eigen::Vector3d tangent_start = to_face_plane(raw_tangent);

        const double dev_len = ba + wall;  // bend develops to BA; wall keeps its length
        Eigen::Vector3d c0 = tangent_start;
        Eigen::Vector3d c1 = tangent_start + length_dir * dev_len;
        Eigen::Vector3d c2 = c1 + e_dir * edge_len;
        Eigen::Vector3d c3 = c0 + e_dir * edge_len;

        std::vector<Eigen::Vector3d> quad{c0, c1, c2, c3};
        TopoDS_Shape strip = prism_face(make_face_from_pts(quad), into_dir * T);
        if (strip.IsNull()) return TopoDS_Shape();
        TopoDS_Shape fused = fuse(flat, strip);
        if (fused.IsNull()) return TopoDS_Shape();
        flat = fused;
    }
    return flat;
}

}  // namespace geom
