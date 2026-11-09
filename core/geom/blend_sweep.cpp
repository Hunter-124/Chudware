#include "geom/blend_sweep.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdio>
#include <limits>
#include <optional>
#include <utility>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Curve2d.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepAlgoAPI_Cut.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepAlgoAPI_Splitter.hxx>
#include <BRepBndLib.hxx>
#include <BRepBuilderAPI_Copy.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_Transform.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepClass3d_SolidClassifier.hxx>
#include <BRepClass_FaceClassifier.hxx>
#include <BRepGProp.hxx>
#include <BRepLib_MakeFace.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepTools.hxx>
#include <BRepTools_History.hxx>
#include <BRep_Tool.hxx>
#include <Bnd_Box.hxx>
#include <ChFi2d_FilletAlgo.hxx>
#include <ElSLib.hxx>
#include <GCPnts_AbscissaPoint.hxx>
#include <GProp_GProps.hxx>
#include <GeomAPI_ExtremaCurveCurve.hxx>
#include <GeomAPI_ProjectPointOnCurve.hxx>
#include <GeomAPI_ProjectPointOnSurf.hxx>
#include <GeomAdaptor_Curve.hxx>
#include <Geom_Circle.hxx>
#include <Geom_Curve.hxx>
#include <Geom_Line.hxx>
#include <Geom_RectangularTrimmedSurface.hxx>
#include <Geom_Surface.hxx>
#include <Geom_SurfaceOfLinearExtrusion.hxx>
#include <Precision.hxx>
#include <ShapeAnalysis_FreeBounds.hxx>
#include <ShapeUpgrade_UnifySameDomain.hxx>
#include <Standard_Failure.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_HSequenceOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Ax1.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <gp_Cone.hxx>
#include <gp_Cylinder.hxx>
#include <gp_Lin.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>
#include <gp_Vec.hxx>

#include "geom/dress.h"
#include "geom/edge_analysis.h"
#include "geom/op_history.h"

namespace geom {
namespace {

using History = Handle(BRepTools_History);

// |n . axis| below this: a plane contains the edge direction. Directions within it: parallel.
constexpr double kAngular = 1e-7;

int solid_count(const TopoDS_Shape& shape) {
    int n = 0;
    for (TopExp_Explorer it(shape, TopAbs_SOLID); it.More(); it.Next()) ++n;
    return n;
}

double volume(const TopoDS_Shape& shape) {
    GProp_GProps props;
    BRepGProp::VolumeProperties(shape, props);
    return props.Mass();
}

std::string point_text(const gp_Pnt& p) {
    char buf[96];
    std::snprintf(buf, sizeof(buf), "(%.3f, %.3f, %.3f)", p.X(), p.Y(), p.Z());
    return buf;
}

bool two_faces(const TopoDS_Edge& edge, const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces,
               std::array<TopoDS_Face, 2>& faces) {
    if (!edge_faces.Contains(edge)) return false;
    int count = 0;
    for (TopTools_ListIteratorOfListOfShape it(edge_faces.FindFromKey(edge)); it.More(); it.Next()) {
        if (count && it.Value().IsSame(faces[0])) continue;
        if (count == 2) return false;
        faces[count++] = TopoDS::Face(it.Value());
    }
    return count == 2;
}

// The orientation of `edge` as `face` traverses it (composed with the face's own).
TopAbs_Orientation orientation_in(const TopoDS_Face& face, const TopoDS_Edge& edge) {
    for (TopExp_Explorer it(face, TopAbs_EDGE); it.More(); it.Next())
        if (it.Current().IsSame(edge)) return it.Current().Orientation();
    return TopAbs_EXTERNAL;
}

bool outward_normal(const TopoDS_Face& face, double u, double v, gp_Dir& normal) {
    BRepAdaptor_Surface surface(face, Standard_False);
    gp_Pnt p;
    gp_Vec du, dv;
    surface.D1(u, v, p, du, dv);
    gp_Vec n = du.Crossed(dv);
    if (n.Magnitude() <= gp::Resolution()) return false;
    if (face.Orientation() == TopAbs_REVERSED) n.Reverse();
    normal = gp_Dir(n);
    return true;
}

// The part of a face a blend rolls on: its section through the corner, in the plane normal
// to the edge there, and the parameter direction along that section that enters the face.
struct Support {
    TopoDS_Face face;
    Handle(Geom_Curve) curve;
    double start = 0.0;
    double sense = 1.0;
    gp_Dir outward;
};

Handle(Geom_SurfaceOfLinearExtrusion) extrusion_of(const TopoDS_Face& face) {
    Handle(Geom_Surface) surface = BRep_Tool::Surface(face);
    for (int depth = 0; depth < 4 && !surface.IsNull(); ++depth) {
        Handle(Geom_SurfaceOfLinearExtrusion) extrusion =
            Handle(Geom_SurfaceOfLinearExtrusion)::DownCast(surface);
        if (!extrusion.IsNull()) return extrusion;
        Handle(Geom_RectangularTrimmedSurface) trimmed =
            Handle(Geom_RectangularTrimmedSurface)::DownCast(surface);
        if (trimmed.IsNull()) break;
        surface = trimmed->BasisSurface();
    }
    return {};
}

// The section of `face` through `corner` in the plane normal to `axis`, when the face is
// constant along `axis`. Null otherwise.
Handle(Geom_Curve) section_curve(const TopoDS_Face& face, const gp_Pnt& corner, const gp_Dir& axis,
                                 double tolerance, double& start) {
    BRepAdaptor_Surface surface(face, Standard_False);
    start = 0.0;
    switch (surface.GetType()) {
        case GeomAbs_Plane: {
            const gp_Dir n = surface.Plane().Axis().Direction();
            if (std::abs(n.Dot(axis)) > kAngular) return {};
            return new Geom_Line(corner, axis.Crossed(n));
        }
        case GeomAbs_Cylinder: {
            const gp_Cylinder cylinder = surface.Cylinder();
            if (!cylinder.Axis().Direction().IsParallel(axis, kAngular)) return {};
            const gp_Lin line(cylinder.Axis());
            const gp_Vec offset(line.Location(), corner);
            const gp_Pnt foot = line.Location().Translated(
                gp_Vec(line.Direction()) * offset.Dot(gp_Vec(line.Direction())));
            const gp_Vec radial(foot, corner);
            if (std::abs(radial.Magnitude() - cylinder.Radius()) > tolerance ||
                radial.Magnitude() <= gp::Resolution())
                return {};
            return new Geom_Circle(gp_Ax2(foot, axis, gp_Dir(radial)), cylinder.Radius());
        }
        case GeomAbs_SurfaceOfExtrusion: {
            if (!surface.Direction().IsParallel(axis, kAngular)) return {};
            const Handle(Geom_SurfaceOfLinearExtrusion) extrusion = extrusion_of(face);
            if (extrusion.IsNull() || extrusion->BasisCurve().IsNull()) return {};
            Handle(Geom_Curve) curve = Handle(Geom_Curve)::DownCast(extrusion->BasisCurve()->Copy());
            if (curve.IsNull()) return {};
            const double f = curve->FirstParameter(), l = curve->LastParameter();
            double sample = 0.0;
            if (f > -1e50) sample = std::max(sample, f);
            if (l < 1e50) sample = std::min(sample, l);
            curve->Translate(gp_Vec(axis) * gp_Vec(curve->Value(sample), corner).Dot(gp_Vec(axis)));
            GeomAPI_ProjectPointOnCurve projection(corner, curve);
            if (projection.NbPoints() == 0 || projection.LowerDistance() > tolerance) return {};
            start = projection.LowerDistanceParameter();
            return curve;
        }
        default:
            return {};
    }
}

// Both supports of a prismatic edge at its midpoint `corner`; false (with `why`) otherwise.
bool supports_of(const TopoDS_Edge& edge, const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces,
                 const gp_Dir& axis, double tolerance, gp_Pnt& corner,
                 std::array<Support, 2>& supports, std::string& why) {
    std::array<TopoDS_Face, 2> faces;
    if (!two_faces(edge, edge_faces, faces)) {
        why = "the edge does not border exactly two faces";
        return false;
    }
    BRepAdaptor_Curve curve(edge);
    const double mid = 0.5 * (curve.FirstParameter() + curve.LastParameter());
    gp_Vec tangent;
    curve.D1(mid, corner, tangent);
    if (tangent.Magnitude() <= gp::Resolution()) {
        why = "the edge has no direction";
        return false;
    }
    for (int i = 0; i < 2; ++i) {
        Support& s = supports[i];
        s.face = faces[i];
        s.curve = section_curve(faces[i], corner, axis, tolerance, s.start);
        if (s.curve.IsNull()) {
            why = "a face of the edge is not constant along it";
            return false;
        }
        const BRepAdaptor_Curve2d pcurve(edge, faces[i]);
        const gp_Pnt2d uv = pcurve.Value(mid);
        if (!outward_normal(faces[i], uv.X(), uv.Y(), s.outward)) {
            why = "a face of the edge has no normal at its midpoint";
            return false;
        }
        const TopAbs_Orientation orientation = orientation_in(faces[i], edge);
        if (orientation != TopAbs_FORWARD && orientation != TopAbs_REVERSED) {
            why = "the edge is not a boundary of its face";
            return false;
        }
        gp_Vec along = tangent.Normalized();
        if (orientation == TopAbs_REVERSED) along.Reverse();
        // The face lies to the left of its boundary seen from outside: n x t points in.
        const gp_Vec inward = gp_Vec(s.outward).Crossed(along);
        gp_Pnt p;
        gp_Vec d;
        s.curve->D1(s.start, p, d);
        if (d.Magnitude() <= gp::Resolution()) {
            why = "a face's section is singular at the edge";
            return false;
        }
        const double cosine = d.Normalized().Dot(inward);
        if (std::abs(cosine) < 0.5) {
            why = "could not tell which way a face leaves the edge";
            return false;
        }
        s.sense = cosine > 0.0 ? 1.0 : -1.0;
    }
    if (gp_Vec(supports[0].outward).Crossed(gp_Vec(supports[1].outward)).Magnitude() <= 1e-6) {
        why = "the faces are tangent along the edge";
        return false;
    }
    return true;
}

// The parameter `length` along the section from the corner into the face (clamped to a
// bounded curve's end; an arc never wraps past half a turn).
std::optional<double> advance(const Support& s, double length) {
    if (s.curve->DynamicType() == STANDARD_TYPE(Geom_Line)) return s.start + s.sense * length;
    if (const Handle(Geom_Circle) circle = Handle(Geom_Circle)::DownCast(s.curve); !circle.IsNull())
        return s.start + s.sense * std::min(length / circle->Radius(), 0.9 * M_PI);
    GeomAdaptor_Curve adaptor(s.curve);
    GCPnts_AbscissaPoint point(adaptor, s.sense * length, s.start);
    if (point.IsDone()) return point.Parameter();
    const double bound = s.sense > 0.0 ? s.curve->LastParameter() : s.curve->FirstParameter();
    if (std::abs(bound) > 1e50 || std::abs(bound - s.start) <= Precision::PConfusion()) return {};
    return bound;
}

TopoDS_Edge segment(const Handle(Geom_Curve)& curve, double a, double b) {
    if (std::abs(a - b) <= Precision::PConfusion()) return {};
    BRepBuilderAPI_MakeEdge maker(curve, std::min(a, b), std::max(a, b));
    return maker.IsDone() ? maker.Edge() : TopoDS_Edge();
}

// The parameter of `p` on the support's section, on the side that enters the face.
std::optional<double> locate(const Support& s, const gp_Pnt& p, double far, double tolerance) {
    const double lo = std::min(s.start, far), hi = std::max(s.start, far);
    GeomAPI_ProjectPointOnCurve projection(p, s.curve, lo, hi);
    if (projection.NbPoints() == 0 || projection.LowerDistance() > tolerance) return {};
    const double u = projection.LowerDistanceParameter();
    if (!(s.sense * (u - s.start) > Precision::PConfusion())) return {};
    return u;
}

TopoDS_Face planar_face(const gp_Pln& plane, const std::vector<TopoDS_Edge>& edges, double tolerance) {
    Handle(TopTools_HSequenceOfShape) input = new TopTools_HSequenceOfShape;
    Handle(TopTools_HSequenceOfShape) wires = new TopTools_HSequenceOfShape;
    for (const TopoDS_Edge& edge : edges) {
        if (edge.IsNull()) return {};
        input->Append(edge);
    }
    ShapeAnalysis_FreeBounds::ConnectEdgesToWires(input, tolerance, Standard_False, wires);
    if (wires->Length() != 1) return {};
    const TopoDS_Wire wire = TopoDS::Wire(wires->Value(1));
    if (!wire.Closed()) return {};
    BRepBuilderAPI_MakeFace maker(plane, wire, Standard_True);
    if (!maker.IsDone()) return {};
    const TopoDS_Face face = maker.Face();
    if (!BRepCheck_Analyzer(face).IsValid()) return {};
    return face;
}

// The blend's cross-section: the region between the corner, the two support sections and
// the arc (or chamfer segment), in the plane through `corner` normal to `axis`.
struct Section {
    TopoDS_Face region;
    std::array<gp_Pnt, 2> contact;  // where the blend leaves each support
    double reach = 0.0;             // farthest distance of the region from the corner
};

bool round_section(const std::array<Support, 2>& supports, const gp_Pnt& corner, const gp_Dir& axis,
                   double radius, double tolerance, Section& out, std::string& why) {
    const gp_Pln plane(corner, axis);
    why = "no arc of this radius is tangent to both faces at the edge";
    for (double window = 2.0 * radius; window <= 64.0 * radius; window *= 2.0) {
        std::array<double, 2> far{};
        std::array<TopoDS_Edge, 2> local;
        bool bounded = true;
        for (int i = 0; i < 2; ++i) {
            const auto end = advance(supports[i], window);
            if (!end) { bounded = false; break; }
            far[i] = *end;
            local[i] = segment(supports[i].curve, supports[i].start, far[i]);
            if (local[i].IsNull()) { bounded = false; break; }
        }
        if (!bounded) break;
        TopoDS_Edge a = local[0], b = local[1];
        ChFi2d_FilletAlgo algo(a, b, plane);
        if (!algo.Perform(radius) || algo.NbResults(corner) < 1) continue;
        const TopoDS_Edge arc = algo.Result(corner, a, b);
        if (arc.IsNull()) continue;
        BRepAdaptor_Curve round(arc);
        if (round.GetType() != GeomAbs_Circle ||
            std::abs(round.Circle().Radius() - radius) > tolerance ||
            !round.Circle().Axis().Direction().IsParallel(axis, 1e-6)) {
            why = "the kernel's arc has an unexpected radius or plane";
            return false;
        }
        const std::array<gp_Pnt, 2> ends{round.Value(round.FirstParameter()),
                                         round.Value(round.LastParameter())};
        const std::array<double, 2> end_par{round.FirstParameter(), round.LastParameter()};
        std::array<double, 2> u{};
        std::array<int, 2> which{-1, -1};
        for (int i = 0; i < 2; ++i) {
            for (int e = 0; e < 2; ++e) {
                if (e == which[0]) continue;
                const auto at = locate(supports[i], ends[e], far[i], tolerance);
                if (!at) continue;
                u[i] = *at;
                which[i] = e;
                break;
            }
            if (which[i] < 0) break;
        }
        if (which[0] < 0 || which[1] < 0) {
            why = "the arc does not end on both faces";
            continue;
        }
        for (int i = 0; i < 2; ++i) {
            gp_Pnt p;
            gp_Vec ds, da;
            supports[i].curve->D1(u[i], p, ds);
            round.D1(end_par[which[i]], p, da);
            if (ds.Magnitude() <= gp::Resolution() || da.Magnitude() <= gp::Resolution() ||
                std::abs(ds.Normalized().Dot(da.Normalized())) < 1.0 - 1e-6) {
                why = "the arc is not tangent to a face";
                return false;
            }
            out.contact[i] = supports[i].curve->Value(u[i]);
        }
        out.region = planar_face(plane, {segment(supports[0].curve, supports[0].start, u[0]), arc,
                                         segment(supports[1].curve, supports[1].start, u[1])},
                                 tolerance);
        if (out.region.IsNull()) {
            why = "could not close the round's cross-section";
            return false;
        }
        out.reach = std::max(corner.Distance(out.contact[0]), corner.Distance(out.contact[1]));
        out.reach = std::max(out.reach, corner.Distance(round.Value(
            0.5 * (round.FirstParameter() + round.LastParameter()))));
        return true;
    }
    return false;
}

std::optional<double> along_by_length(const Support& s, double length) {
    if (!(length > 0.0)) return {};
    GeomAdaptor_Curve adaptor(s.curve);
    GCPnts_AbscissaPoint point(adaptor, s.sense * length, s.start);
    if (!point.IsDone()) return {};
    return point.Parameter();
}

bool chamfer_section(const std::array<Support, 2>& supports, int reference, const gp_Pnt& corner,
                     const gp_Dir& axis, const SweepBlendProfile& profile, double tolerance,
                     Section& out, std::string& why) {
    const gp_Pln plane(corner, axis);
    const Support& first = supports[reference];
    const Support& second = supports[1 - reference];
    const auto u_first = along_by_length(first, profile.size);
    if (!u_first) {
        why = "the chamfer distance runs off the face's section";
        return false;
    }
    const gp_Pnt a = first.curve->Value(*u_first);
    std::optional<double> u_second;
    if (profile.kind == SweepBlendProfile::Kind::chamfer) {
        u_second = along_by_length(second, profile.size2);
    } else {
        // The chamfer leaves the reference face at `angle` to it, toward the other face.
        gp_Pnt p;
        gp_Vec d;
        first.curve->D1(*u_first, p, d);
        if (d.Magnitude() <= gp::Resolution()) {
            why = "the reference face's section is singular at the chamfer";
            return false;
        }
        const gp_Dir back(-first.sense * d);
        const double reach = 64.0 * std::max(profile.size, tolerance);
        const auto far = advance(second, reach);
        double best = std::numeric_limits<double>::infinity();
        if (far) {
            for (double sign : {1.0, -1.0}) {
                const gp_Dir direction = back.Rotated(gp_Ax1(a, axis), sign * profile.size2);
                Handle(Geom_Line) line = new Geom_Line(a, direction);
                GeomAPI_ExtremaCurveCurve extrema(line, second.curve, 0.0, reach,
                                                  std::min(second.start, *far),
                                                  std::max(second.start, *far));
                for (int i = 1; i <= extrema.NbExtrema(); ++i) {
                    if (extrema.Distance(i) > tolerance) continue;
                    double t = 0.0, u = 0.0;
                    extrema.Parameters(i, t, u);
                    if (!(t > tolerance) || !(second.sense * (u - second.start) > Precision::PConfusion()))
                        continue;
                    const double distance = second.curve->Value(u).Distance(corner);
                    if (distance < best) {
                        best = distance;
                        u_second = u;
                    }
                }
            }
        }
    }
    if (!u_second) {
        why = "the chamfer does not reach the other face";
        return false;
    }
    const gp_Pnt b = second.curve->Value(*u_second);
    if (a.Distance(b) <= tolerance) {
        why = "the chamfer collapses to a point";
        return false;
    }
    BRepBuilderAPI_MakeEdge bevel(a, b);
    if (!bevel.IsDone()) {
        why = "could not build the chamfer segment";
        return false;
    }
    out.region = planar_face(plane, {segment(first.curve, first.start, *u_first), bevel.Edge(),
                                     segment(second.curve, second.start, *u_second)},
                             tolerance);
    if (out.region.IsNull()) {
        why = "could not close the chamfer's cross-section";
        return false;
    }
    out.contact[reference] = a;
    out.contact[1 - reference] = b;
    out.reach = std::max(corner.Distance(a), corner.Distance(b));
    return true;
}

// A point strictly inside a planar region (its centroid when that is inside).
std::optional<gp_Pnt> inside_point(const TopoDS_Face& face, double tolerance) {
    GProp_GProps props;
    BRepGProp::SurfaceProperties(face, props);
    const gp_Pnt centroid = props.CentreOfMass();
    BRepClass_FaceClassifier classifier(face, centroid, tolerance);
    if (classifier.State() == TopAbs_IN) return centroid;
    return {};
}

std::optional<gp_Pnt> inside_point_of_solid(const TopoDS_Shape& solid, double tolerance) {
    GProp_GProps props;
    BRepGProp::VolumeProperties(solid, props);
    const gp_Pnt centroid = props.CentreOfMass();
    BRepClass3d_SolidClassifier classifier(solid, centroid, tolerance);
    if (classifier.State() == TopAbs_IN) return centroid;
    // A curved sliver can have its centroid outside: try just inside each face instead.
    for (TopExp_Explorer it(solid, TopAbs_FACE); it.More(); it.Next()) {
        const TopoDS_Face face = TopoDS::Face(it.Current());
        double u0, u1, v0, v1;
        BRepTools::UVBounds(face, u0, u1, v0, v1);
        gp_Dir n;
        const double u = 0.5 * (u0 + u1), v = 0.5 * (v0 + v1);
        if (!outward_normal(face, u, v, n)) continue;
        BRepClass_FaceClassifier on_face(face, gp_Pnt2d(u, v), tolerance);
        if (on_face.State() != TopAbs_IN) continue;
        const gp_Pnt p = BRepAdaptor_Surface(face).Value(u, v).Translated(gp_Vec(n) * (-10.0 * tolerance));
        BRepClass3d_SolidClassifier probe(solid, p, tolerance);
        if (probe.State() == TopAbs_IN) return p;
    }
    return {};
}

// A face beyond the supports at an end of the edge, and the side of its surface the edge
// leaves it on (-1: against the outward normal, into the material the face bounds).
struct EndFace {
    TopoDS_Face face;
    int side = 0;
};

enum class Combine { all, any };

struct EndClip {
    gp_Pnt vertex;
    std::vector<EndFace> faces;
    Combine combine = Combine::all;
};

int side_of(const TopoDS_Face& face, const gp_Pnt& p, double tolerance);

// A sharp edge at `vertex`, outside `chain`, that continues the chain's line: the kernel's
// contour would carry a blend on along it. Null when there is none.
TopoDS_Edge continuation(const TopTools_IndexedMapOfShape& chain, const TopoDS_Vertex& vertex,
                         const gp_Dir& axis, const TopTools_IndexedDataMapOfShapeListOfShape& vertex_faces,
                         const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces) {
    if (!vertex_faces.Contains(vertex)) return {};
    TopTools_IndexedMapOfShape seen;
    for (TopTools_ListIteratorOfListOfShape it(vertex_faces.FindFromKey(vertex)); it.More(); it.Next()) {
        for (TopExp_Explorer e(it.Value(), TopAbs_EDGE); e.More(); e.Next()) {
            const TopoDS_Edge other = TopoDS::Edge(e.Current());
            if (chain.Contains(other) || seen.Contains(other) || BRep_Tool::Degenerated(other)) continue;
            seen.Add(other);
            TopoDS_Vertex a, b;
            TopExp::Vertices(other, a, b);
            if (!a.IsSame(vertex) && !b.IsSame(vertex)) continue;
            BRepAdaptor_Curve curve(other);
            gp_Pnt p;
            gp_Vec tangent;
            curve.D1(BRep_Tool::Parameter(vertex, other), p, tangent);
            if (tangent.Magnitude() <= gp::Resolution() ||
                std::abs(tangent.Normalized().Dot(gp_Vec(axis))) < 1.0 - 1e-6)
                continue;
            std::array<TopoDS_Face, 2> faces;
            if (!two_faces(other, edge_faces, faces)) continue;
            std::array<gp_Dir, 2> normals;
            bool known = true;
            for (int i = 0; i < 2 && known; ++i) {
                const gp_Pnt2d uv = BRep_Tool::Parameters(vertex, faces[i]);
                known = outward_normal(faces[i], uv.X(), uv.Y(), normals[i]);
            }
            if (known && gp_Vec(normals[0]).Crossed(gp_Vec(normals[1])).Magnitude() > 1e-6) return other;
        }
    }
    return {};
}

// `chain_faces`: the supports of every edge of the swept chain. `probe`: how far into the
// edge to look when a face is tangent to it at the vertex.
bool end_clip(const TopTools_IndexedMapOfShape& chain_faces, const TopoDS_Vertex& vertex,
              const gp_Dir& into_edge, const TopTools_IndexedDataMapOfShapeListOfShape& vertex_faces,
              const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces, double probe,
              double tolerance, EndClip& clip, std::string& why) {
    clip.vertex = BRep_Tool::Pnt(vertex);
    if (!vertex_faces.Contains(vertex)) {
        why = "an end vertex of the edge has no faces";
        return false;
    }
    TopTools_IndexedMapOfShape seen;
    for (TopTools_ListIteratorOfListOfShape it(vertex_faces.FindFromKey(vertex)); it.More(); it.Next()) {
        const TopoDS_Face face = TopoDS::Face(it.Value());
        if (chain_faces.Contains(face) || seen.Contains(face)) continue;
        seen.Add(face);
        const gp_Pnt2d uv = BRep_Tool::Parameters(vertex, face);
        gp_Dir n;
        if (!outward_normal(face, uv.X(), uv.Y(), n)) {
            why = "a face ending the edge at " + point_text(clip.vertex) + " has no normal there";
            return false;
        }
        const double s = gp_Vec(into_edge).Dot(gp_Vec(n));
        int side = std::abs(s) > 1e-6 ? (s > 0.0 ? 1 : -1) : 0;
        // A face tangent to the edge direction at the vertex still ends the blend where it
        // curves away from the edge (a round the edge runs into); a plane containing the
        // edge does not.
        if (side == 0)
            side = side_of(face, clip.vertex.Translated(gp_Vec(into_edge) * probe), tolerance);
        if (side == 0) continue;
        clip.faces.push_back({face, side});
    }
    if (clip.faces.empty()) {
        why = "no face ends the edge at " + point_text(clip.vertex);
        return false;
    }
    if (clip.faces.size() == 1) return true;
    // Several faces end the edge: the blend keeps the edge's side of the surface they form
    // together. Where two of them meet, that side is convex (keep the intersection of their
    // sides) or reflex (keep the union), from the convexity of the edge between them.
    int links = 0;
    std::optional<Combine> combine;
    for (std::size_t i = 0; i < clip.faces.size(); ++i) {
        for (std::size_t j = i + 1; j < clip.faces.size(); ++j) {
            TopoDS_Edge shared;
            for (TopExp_Explorer e(clip.faces[i].face, TopAbs_EDGE); e.More() && shared.IsNull(); e.Next()) {
                const TopoDS_Edge candidate = TopoDS::Edge(e.Current());
                TopoDS_Vertex a, b;
                TopExp::Vertices(candidate, a, b);
                if (!a.IsSame(vertex) && !b.IsSame(vertex)) continue;
                if (!edge_faces.Contains(candidate)) continue;
                for (TopTools_ListIteratorOfListOfShape f(edge_faces.FindFromKey(candidate)); f.More(); f.Next())
                    if (f.Value().IsSame(clip.faces[j].face)) { shared = candidate; break; }
            }
            if (shared.IsNull()) continue;
            if (clip.faces[i].side != clip.faces[j].side) {
                why = "the faces ending the edge at " + point_text(clip.vertex) +
                      " do not bound one side of it";
                return false;
            }
            ++links;
            Combine pair = Combine::all;
            const auto frame = chudware::geom::edge_blend_frame(shared, edge_faces);
            if (frame && frame->concave) {
                const bool material_side = clip.faces[i].side < 0;
                const bool convex = !*frame->concave;
                pair = convex == material_side ? Combine::all : Combine::any;
            }
            if (combine && *combine != pair) {
                why = "the faces ending the edge at " + point_text(clip.vertex) +
                      " turn both ways around it";
                return false;
            }
            combine = pair;
        }
    }
    if (links + 1 < static_cast<int>(clip.faces.size()) || !combine) {
        why = "the faces ending the edge at " + point_text(clip.vertex) + " are not connected";
        return false;
    }
    clip.combine = *combine;
    return true;
}

// `face`'s surface, bounded only enough to cross the whole blend tool around `center`.
TopoDS_Face extended_face(const TopoDS_Face& face, const gp_Pnt& center, double size) {
    BRepAdaptor_Surface surface(face, Standard_False);
    double u = 0.0, v = 0.0;
    switch (surface.GetType()) {
        case GeomAbs_Plane: {
            const gp_Pln plane = surface.Plane();
            ElSLib::Parameters(plane, center, u, v);
            BRepBuilderAPI_MakeFace maker(plane, u - size, u + size, v - size, v + size);
            return maker.IsDone() ? maker.Face() : TopoDS_Face();
        }
        case GeomAbs_Cylinder: {
            const gp_Cylinder cylinder = surface.Cylinder();
            ElSLib::Parameters(cylinder, center, u, v);
            BRepBuilderAPI_MakeFace maker(cylinder, 0.0, 2.0 * M_PI, v - size, v + size);
            return maker.IsDone() ? maker.Face() : TopoDS_Face();
        }
        case GeomAbs_Sphere: {
            BRepBuilderAPI_MakeFace maker(surface.Sphere());
            return maker.IsDone() ? maker.Face() : TopoDS_Face();
        }
        case GeomAbs_Torus: {
            BRepBuilderAPI_MakeFace maker(surface.Torus());
            return maker.IsDone() ? maker.Face() : TopoDS_Face();
        }
        case GeomAbs_Cone: {
            const gp_Cone cone = surface.Cone();
            ElSLib::Parameters(cone, center, u, v);
            // The apex is at v = -R / sin(semi-angle); stop short of it.
            const double apex = -cone.RefRadius() / std::sin(cone.SemiAngle());
            double v0 = v - size, v1 = v + size;
            if (apex > v0 && apex < v1) {
                if (v > apex) v0 = apex + 1e-6 * size;
                else v1 = apex - 1e-6 * size;
            }
            BRepBuilderAPI_MakeFace maker(cone, 0.0, 2.0 * M_PI, v0, v1);
            return maker.IsDone() ? maker.Face() : TopoDS_Face();
        }
        default: {
            // A freeform face extends within its surface's own bounds.
            const Handle(Geom_Surface) geometry = BRep_Tool::Surface(face);
            double u0, u1, v0, v1, su0, su1, sv0, sv1;
            BRepTools::UVBounds(face, u0, u1, v0, v1);
            geometry->Bounds(su0, su1, sv0, sv1);
            const double du = u1 - u0, dv = v1 - v0;
            u0 = std::max(su0, u0 - du);
            u1 = std::min(su1, u1 + du);
            v0 = std::max(sv0, v0 - dv);
            v1 = std::min(sv1, v1 + dv);
            BRepLib_MakeFace maker(geometry, u0, u1, v0, v1, Precision::Confusion());
            return maker.IsDone() ? maker.Face() : TopoDS_Face();
        }
    }
}

// Which side of `face`'s (unbounded) surface `p` lies on: +1 along the outward normal.
int side_of(const TopoDS_Face& face, const gp_Pnt& p, double tolerance) {
    GeomAPI_ProjectPointOnSurf projection(p, BRep_Tool::Surface(face));
    if (projection.NbPoints() == 0) return 0;
    double u = 0.0, v = 0.0;
    projection.LowerDistanceParameters(u, v);
    gp_Dir n;
    if (!outward_normal(face, u, v, n)) return 0;
    const double value = gp_Vec(projection.NearestPoint(), p).Dot(gp_Vec(n));
    if (std::abs(value) <= tolerance) return 0;
    return value > 0.0 ? 1 : -1;
}

void map_through(const TopoDS_Shape& shape, const std::vector<History>& chain, TopTools_ListOfShape& out) {
    out.Clear();
    out.Append(shape);
    for (const History& history : chain) {
        TopTools_ListOfShape next;
        for (TopTools_ListIteratorOfListOfShape it(out); it.More(); it.Next()) {
            if (history->IsRemoved(it.Value())) continue;
            const TopTools_ListOfShape& modified = history->Modified(it.Value());
            if (modified.IsEmpty()) next.Append(it.Value());
            else for (TopTools_ListIteratorOfListOfShape m(modified); m.More(); m.Next()) next.Append(m.Value());
        }
        out = next;
    }
}

struct SweepOne {
    TopoDS_Shape shape;
    std::vector<History> histories;
    gp_Pnt from, to;  // the ends of the blended chain
    std::string why;
};

// True when `other`'s two faces carry the same cross-section as `supports` at the corner:
// sampled points of each support's section, slid along the axis to `other`, lie on the face
// of `other` that faces the same way.
bool same_section(const TopoDS_Edge& other, const std::array<Support, 2>& supports,
                  const Section& section, const gp_Pnt& corner, const gp_Dir& axis,
                  const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces, double tolerance,
                  std::array<TopoDS_Face, 2>& faces) {
    if (!two_faces(other, edge_faces, faces)) return false;
    BRepAdaptor_Curve curve(other);
    const gp_Pnt mid = curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()));
    const gp_Vec slide = gp_Vec(axis) * gp_Vec(corner, mid).Dot(gp_Vec(axis));
    std::array<gp_Dir, 2> normals;
    for (int i = 0; i < 2; ++i)
        if (!sweep_blend_face_normal(faces[i], other, normals[i])) return false;
    if (gp_Vec(normals[0]).Dot(gp_Vec(supports[0].outward)) <
        gp_Vec(normals[1]).Dot(gp_Vec(supports[0].outward)))
        std::swap(faces[0], faces[1]), std::swap(normals[0], normals[1]);
    for (int i = 0; i < 2; ++i) {
        if (gp_Vec(normals[i]).Dot(gp_Vec(supports[i].outward)) < 1.0 - 1e-6) return false;
        const gp_Pnt contact = section.contact[i];
        const gp_Pnt halfway((corner.XYZ() + contact.XYZ()) * 0.5);
        for (const gp_Pnt& p : {contact, halfway}) {
            GeomAPI_ProjectPointOnSurf projection(p.Translated(slide), BRep_Tool::Surface(faces[i]));
            if (projection.NbPoints() == 0 || projection.LowerDistance() > tolerance) return false;
        }
    }
    return true;
}

// Merge the new end faces of the blend into the body faces they continue. Only edges between
// a face made from the tool and one of the body are allowed to disappear.
TopoDS_Shape merge_end_faces(const TopoDS_Shape& shape, BRepAlgoAPI_BooleanOperation& boolean,
                             const std::vector<TopoDS_Shape>& tools, double expected_volume,
                             std::vector<History>& histories) {
    try {
        TopTools_IndexedMapOfShape result_faces, tool_faces;
        TopExp::MapShapes(shape, TopAbs_FACE, result_faces);
        for (const TopoDS_Shape& tool : tools)
            for (TopExp_Explorer it(tool, TopAbs_FACE); it.More(); it.Next()) {
                const TopTools_ListOfShape& images = boolean.Modified(it.Current());
                if (images.IsEmpty() && result_faces.Contains(it.Current())) tool_faces.Add(it.Current());
                for (TopTools_ListIteratorOfListOfShape m(images); m.More(); m.Next())
                    if (result_faces.Contains(m.Value())) tool_faces.Add(m.Value());
            }
        BRepBuilderAPI_Copy copier(shape, Standard_False, Standard_False);
        const TopoDS_Shape work = copier.Shape();
        if (work.IsNull()) return {};
        TopTools_IndexedMapOfShape copied_tool_faces;
        for (int i = 1; i <= tool_faces.Extent(); ++i) {
            const TopoDS_Shape& image = copier.ModifiedShape(tool_faces(i));
            if (!image.IsNull()) copied_tool_faces.Add(image);
        }
        TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
        TopExp::MapShapesAndAncestors(work, TopAbs_EDGE, TopAbs_FACE, edge_faces);
        ShapeUpgrade_UnifySameDomain unify(work, Standard_True, Standard_True, Standard_False);
        TopTools_IndexedMapOfShape free_vertices;
        int seams = 0;
        for (int i = 1; i <= edge_faces.Extent(); ++i) {
            int from_tool = 0, count = 0;
            for (TopTools_ListIteratorOfListOfShape f(edge_faces(i)); f.More(); f.Next(), ++count)
                if (copied_tool_faces.Contains(f.Value())) ++from_tool;
            if (count == 2 && from_tool == 1) {
                ++seams;
                TopExp_Explorer v(edge_faces.FindKey(i), TopAbs_VERTEX);
                for (; v.More(); v.Next()) free_vertices.Add(v.Current());
            } else {
                unify.KeepShape(edge_faces.FindKey(i));
            }
        }
        if (seams == 0) return {};
        for (TopExp_Explorer v(work, TopAbs_VERTEX); v.More(); v.Next())
            if (!free_vertices.Contains(v.Current())) unify.KeepShape(v.Current());
        unify.Build();
        const TopoDS_Shape merged = unify.Shape();
        if (merged.IsNull() || solid_count(merged) != 1 || !is_watertight(merged) ||
            !BRepCheck_Analyzer(merged).IsValid() ||
            std::abs(volume(merged) - expected_volume) > 1e-6 * std::max(1.0, std::abs(expected_volume)))
            return {};
        TopTools_ListOfShape args;
        args.Append(shape);
        histories.push_back(new BRepTools_History(args, copier));
        histories.push_back(unify.History());
        return merged;
    } catch (const Standard_Failure&) {
        return {};
    }
}

SweepOne sweep_one(const TopoDS_Shape& shape, const TopoDS_Edge& edge, const SweepBlendProfile& profile) {
    SweepOne out;
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces, vertex_faces;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);
    TopExp::MapShapesAndAncestors(shape, TopAbs_VERTEX, TopAbs_FACE, vertex_faces);
    if (BRepAdaptor_Curve(edge).GetType() != GeomAbs_Line) {
        out.why = "the edge is not straight";
        return out;
    }
    TopoDS_Vertex first, last;
    TopExp::Vertices(edge, first, last);
    if (first.IsNull() || last.IsNull() || first.IsSame(last)) {
        out.why = "the edge has no two distinct ends";
        return out;
    }
    const gp_Pnt e0 = BRep_Tool::Pnt(first), e1 = BRep_Tool::Pnt(last);
    if (e0.Distance(e1) <= Precision::Confusion()) {
        out.why = "the edge is too short";
        return out;
    }
    const gp_Dir axis(gp_Vec(e0, e1));
    std::array<Support, 2> supports;
    gp_Pnt corner;
    const double tolerance = std::max({10.0 * Precision::Confusion(), 10.0 * BRep_Tool::Tolerance(edge),
                                       10.0 * BRep_Tool::Tolerance(first), 10.0 * BRep_Tool::Tolerance(last)});
    if (!supports_of(edge, edge_faces, axis, tolerance, corner, supports, out.why)) return out;

    Section section;
    if (profile.kind == SweepBlendProfile::Kind::round) {
        if (!round_section(supports, corner, axis, profile.size, tolerance, section, out.why)) return out;
    } else {
        int reference = 0;
        if (profile.has_reference)
            reference = gp_Vec(supports[1].outward).Dot(gp_Vec(profile.reference_normal)) >
                        gp_Vec(supports[0].outward).Dot(gp_Vec(profile.reference_normal)) ? 1 : 0;
        if (!chamfer_section(supports, reference, corner, axis, profile, tolerance, section, out.why))
            return out;
    }
    for (int i = 0; i < 2; ++i) {
        BRepClass_FaceClassifier on_face(supports[i].face, section.contact[i], tolerance);
        if (on_face.State() != TopAbs_IN && on_face.State() != TopAbs_ON) {
            out.why = std::string("the ") +
                (profile.kind == SweepBlendProfile::Kind::round ? "round" : "chamfer") +
                " is wider than a face it rests on at " + point_text(section.contact[i]);
            return out;
        }
    }
    const auto inside = inside_point(section.region, tolerance);
    if (!inside) {
        out.why = "could not find the inside of the blend's cross-section";
        return out;
    }
    const TopAbs_State state = BRepClass3d_SolidClassifier(shape, *inside, tolerance).State();
    if (state != TopAbs_IN && state != TopAbs_OUT) {
        out.why = "the blend's cross-section touches the body's boundary";
        return out;
    }
    const bool additive = state == TopAbs_OUT;
    if (const auto frame = chudware::geom::edge_blend_frame(edge, edge_faces);
        frame && frame->concave && *frame->concave != additive) {
        out.why = "the blend's cross-section is on the wrong side of the edge";
        return out;
    }

    // The kernel's contour follows sharp edges that continue this one in a straight line (a
    // long edge split where a face was imprinted). Such an edge between faces with the same
    // cross-section joins the swept chain; one where the faces change cannot be swept.
    TopTools_IndexedMapOfShape chain, chain_faces;
    chain.Add(edge);
    chain_faces.Add(supports[0].face);
    chain_faces.Add(supports[1].face);
    std::array<TopoDS_Vertex, 2> ends{first, last};
    for (int side = 0; side < 2; ++side) {
        for (;;) {
            const TopoDS_Edge next = continuation(chain, ends[side], axis, vertex_faces, edge_faces);
            if (next.IsNull()) break;
            std::array<TopoDS_Face, 2> next_faces;
            if (BRepAdaptor_Curve(next).GetType() != GeomAbs_Line ||
                !same_section(next, supports, section, corner, axis, edge_faces, tolerance, next_faces)) {
                out.why = "the edge continues tangentially into another sharp edge at " +
                          point_text(BRep_Tool::Pnt(ends[side])) +
                          " where its faces change; the blend cannot follow it";
                return out;
            }
            chain.Add(next);
            chain_faces.Add(next_faces[0]);
            chain_faces.Add(next_faces[1]);
            TopoDS_Vertex a, b;
            TopExp::Vertices(next, a, b);
            ends[side] = a.IsSame(ends[side]) ? b : a;
        }
    }
    const gp_Pnt p0 = BRep_Tool::Pnt(ends[0]), p1 = BRep_Tool::Pnt(ends[1]);
    const double length = p0.Distance(p1);
    out.from = p0;
    out.to = p1;

    std::array<EndClip, 2> clips;
    const double probe = std::min(section.reach, 0.5 * length);
    if (!end_clip(chain_faces, ends[0], axis, vertex_faces, edge_faces, probe, tolerance, clips[0],
                  out.why) ||
        !end_clip(chain_faces, ends[1], axis.Reversed(), vertex_faces, edge_faces, probe, tolerance,
                  clips[1], out.why))
        return out;

    // Axial coordinates measured from the section plane.
    const gp_Vec direction(axis);
    const double s0 = gp_Vec(corner, p0).Dot(direction), s1 = gp_Vec(corner, p1).Dot(direction);
    double extension = std::max(4.0 * section.reach, 100.0 * tolerance);
    std::vector<TopoDS_Shape> kept;
    double kept_volume = 0.0;
    for (int attempt = 0; attempt < 3; ++attempt, extension *= 4.0) {
        kept.clear();
        kept_volume = 0.0;
        gp_Trsf shift;
        shift.SetTranslation(direction * (s0 - extension));
        const TopoDS_Shape base = BRepBuilderAPI_Transform(section.region, shift, Standard_True).Shape();
        const TopoDS_Shape prism = BRepPrimAPI_MakePrism(base, direction * (s1 - s0 + 2.0 * extension),
                                                         Standard_False, Standard_True).Shape();
        if (prism.IsNull()) {
            out.why = "could not sweep the blend's cross-section";
            return out;
        }
        Bnd_Box box;
        BRepBndLib::Add(prism, box);
        const double size = 2.0 * std::sqrt(box.SquareExtent()) + 1.0;
        TopTools_ListOfShape args, tools;
        args.Append(prism);
        for (const EndClip& clip : clips)
            for (const EndFace& end : clip.faces) {
                const TopoDS_Face extended = extended_face(end.face, clip.vertex, size);
                if (extended.IsNull()) {
                    out.why = "could not extend a face ending the edge at " + point_text(clip.vertex);
                    return out;
                }
                tools.Append(extended);
            }
        BRepAlgoAPI_Splitter splitter;
        splitter.SetArguments(args);
        splitter.SetTools(tools);
        splitter.SetNonDestructive(Standard_True);
        splitter.Build();
        if (!splitter.IsDone() || splitter.HasErrors()) {
            out.why = "could not trim the blend at the faces ending the edge";
            return out;
        }
        bool reaches_end = false;
        for (TopExp_Explorer it(splitter.Shape(), TopAbs_SOLID); it.More(); it.Next()) {
            const TopoDS_Shape piece = it.Current();
            const auto p = inside_point_of_solid(piece, tolerance);
            if (!p) {
                out.why = "could not classify a piece of the trimmed blend";
                return out;
            }
            bool keep = true;
            for (const EndClip& clip : clips) {
                bool all = true, any = false;
                for (const EndFace& end : clip.faces) {
                    const int side = side_of(end.face, *p, tolerance);
                    if (side == 0) {
                        out.why = "a piece of the blend lies on a face ending the edge at " +
                                  point_text(clip.vertex);
                        return out;
                    }
                    const bool on_edge_side = side == end.side;
                    all = all && on_edge_side;
                    any = any || on_edge_side;
                }
                if (!(clip.combine == Combine::all ? all : any)) { keep = false; break; }
            }
            if (!keep) continue;
            Bnd_Box piece_box;
            BRepBndLib::Add(piece, piece_box);
            double x0, y0, z0, x1, y1, z1;
            piece_box.Get(x0, y0, z0, x1, y1, z1);
            for (double x : {x0, x1}) for (double y : {y0, y1}) for (double z : {z0, z1}) {
                const double s = gp_Vec(corner, gp_Pnt(x, y, z)).Dot(direction);
                if (s < s0 - extension + 2.0 * tolerance || s > s1 + extension - 2.0 * tolerance)
                    reaches_end = true;
            }
            kept.push_back(piece);
            kept_volume += volume(piece);
        }
        // An oblique end face trims the blend farther out than the prism reached: retry longer.
        if (!reaches_end) break;
        if (attempt == 2) {
            out.why = "a face ending the edge is too oblique to trim the blend against";
            return out;
        }
    }
    if (kept.empty()) {
        out.why = "the faces ending the edge leave nothing of the blend";
        return out;
    }
    // The trimmed blend must still run through the middle of the edge.
    const gp_Pnt middle = inside->Translated(direction * (0.5 * (s0 + s1)));
    bool covers_middle = false;
    for (const TopoDS_Shape& piece : kept)
        if (BRepClass3d_SolidClassifier(piece, middle, tolerance).State() == TopAbs_IN) covers_middle = true;
    if (!covers_middle) {
        out.why = "the faces ending the edge cut the blend between its ends";
        return out;
    }

    TopTools_ListOfShape args, tools;
    args.Append(shape);
    for (const TopoDS_Shape& piece : kept) tools.Append(piece);
    std::optional<BRepAlgoAPI_Cut> cut;
    std::optional<BRepAlgoAPI_Fuse> fuse;
    BRepAlgoAPI_BooleanOperation* boolean = nullptr;
    if (additive) boolean = &fuse.emplace();
    else boolean = &cut.emplace();
    boolean->SetArguments(args);
    boolean->SetTools(tools);
    boolean->SetNonDestructive(Standard_True);
    boolean->Build();
    const char* operation = additive ? "fusing" : "cutting";
    if (!boolean->IsDone() || boolean->HasErrors() || boolean->Shape().IsNull()) {
        out.why = std::string(operation) + " the blend failed";
        return out;
    }
    TopoDS_Shape result = boolean->Shape();
    if (solid_count(result) != 1 || !is_watertight(result) || !BRepCheck_Analyzer(result).IsValid()) {
        out.why = std::string(operation) + " the blend did not leave one valid solid";
        return out;
    }
    // The blend must exchange exactly its own volume: anything else means the region was
    // not clear (another wall inside the round, a pocket under the chamfer).
    const double before = volume(shape), after = volume(result);
    const double exchanged = additive ? after - before : before - after;
    if (std::abs(exchanged - kept_volume) > std::max(1e-3 * kept_volume, 1e-9)) {
        out.why = "the blend region is not clear of other material";
        return out;
    }
    out.histories.push_back(boolean->History());
    const TopoDS_Shape merged = merge_end_faces(result, *boolean, kept, after, out.histories);
    if (!merged.IsNull()) result = merged;
    if (op_history_recording())
        for (const History& history : out.histories) op_history_record(history);
    out.shape = result;
    return out;
}

}  // namespace

bool sweep_blend_face_normal(const TopoDS_Face& face, const TopoDS_Edge& edge, gp_Dir& normal) {
    try {
        BRepAdaptor_Curve curve(edge);
        const double mid = 0.5 * (curve.FirstParameter() + curve.LastParameter());
        const gp_Pnt2d uv = BRepAdaptor_Curve2d(edge, face).Value(mid);
        return outward_normal(face, uv.X(), uv.Y(), normal);
    } catch (const Standard_Failure&) {
        return false;
    }
}

bool sweep_blend_prismatic(const TopoDS_Edge& edge,
                           const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces) {
    try {
        if (edge.IsNull() || BRep_Tool::Degenerated(edge) ||
            BRepAdaptor_Curve(edge).GetType() != GeomAbs_Line)
            return false;
        TopoDS_Vertex first, last;
        TopExp::Vertices(edge, first, last);
        if (first.IsNull() || last.IsNull() || first.IsSame(last)) return false;
        const gp_Pnt p0 = BRep_Tool::Pnt(first), p1 = BRep_Tool::Pnt(last);
        if (p0.Distance(p1) <= Precision::Confusion()) return false;
        std::array<Support, 2> supports;
        gp_Pnt corner;
        std::string why;
        const double tolerance = std::max(10.0 * Precision::Confusion(), 10.0 * BRep_Tool::Tolerance(edge));
        return supports_of(edge, edge_faces, gp_Dir(gp_Vec(p0, p1)), tolerance, corner, supports, why);
    } catch (const Standard_Failure&) {
        return false;
    }
}

std::vector<std::size_t> sweep_blend_choose(
    const std::vector<TopoDS_Edge>& edges,
    const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces, SweepBlendScope scope) {
    std::vector<std::size_t> chosen;
    for (std::size_t i = 0; i < edges.size(); ++i) {
        if (!sweep_blend_prismatic(edges[i], edge_faces)) continue;
        if (scope == SweepBlendScope::isolated) {
            TopoDS_Vertex a, b;
            TopExp::Vertices(edges[i], a, b);
            bool shared = false;
            for (std::size_t j = 0; j < edges.size() && !shared; ++j) {
                if (j == i) continue;
                TopoDS_Vertex c, d;
                TopExp::Vertices(edges[j], c, d);
                shared = a.IsSame(c) || a.IsSame(d) || b.IsSame(c) || b.IsSame(d);
            }
            if (shared) continue;
        }
        chosen.push_back(i);
    }
    return chosen;
}

SweepBlendRun sweep_blend_edges(const TopoDS_Shape& shape, const std::vector<TopoDS_Edge>& edges,
                                const std::function<SweepBlendProfile(std::size_t)>& profile,
                                const std::vector<TopoDS_Edge>& others) {
    SweepBlendRun run;
    try {
        if (solid_count(shape) != 1) {
            run.why = "swept blends need a single solid";
            return run;
        }
        TopoDS_Shape current = shape;
        std::vector<History> chain;
        // The straight runs already blended: a selected edge on one was blended with it.
        std::vector<std::pair<gp_Pnt, gp_Pnt>> blended;
        const auto on_blended = [&](const TopoDS_Edge& edge) {
            BRepAdaptor_Curve curve(edge);
            for (const auto& [a, b] : blended) {
                const gp_Vec run_vector(a, b);
                const double length = run_vector.Magnitude();
                if (length <= gp::Resolution()) continue;
                bool inside = true;
                for (double f : {0.0, 0.5, 1.0}) {
                    const gp_Pnt p = curve.Value(curve.FirstParameter() +
                                                 f * (curve.LastParameter() - curve.FirstParameter()));
                    const gp_Vec offset(a, p);
                    const double t = offset.Dot(run_vector) / length;
                    if (t < -1e-6 || t > length + 1e-6 ||
                        offset.Crossed(run_vector).Magnitude() / length > 1e-6)
                        inside = false;
                }
                if (inside) return true;
            }
            return false;
        };
        for (std::size_t i = 0; i < edges.size(); ++i) {
            run.failed = i;
            run.edge_failed = true;
            if (on_blended(edges[i])) {
                run.edge_failed = false;
                continue;
            }
            TopTools_ListOfShape images;
            map_through(edges[i], chain, images);
            TopTools_IndexedMapOfShape current_edges;
            TopExp::MapShapes(current, TopAbs_EDGE, current_edges);
            TopoDS_Edge edge;
            int count = 0;
            for (TopTools_ListIteratorOfListOfShape it(images); it.More(); it.Next())
                if (current_edges.Contains(it.Value())) {
                    edge = TopoDS::Edge(it.Value());
                    ++count;
                }
            if (count != 1) {
                run.why = "an earlier blend of this feature split or consumed it";
                return run;
            }
            SweepOne one = sweep_one(current, edge, profile(i));
            if (one.shape.IsNull()) {
                run.why = std::move(one.why);
                return run;
            }
            run.edge_failed = false;
            current = one.shape;
            blended.emplace_back(one.from, one.to);
            chain.insert(chain.end(), one.histories.begin(), one.histories.end());
        }
        TopTools_IndexedMapOfShape result_edges;
        TopExp::MapShapes(current, TopAbs_EDGE, result_edges);
        for (const TopoDS_Edge& other : others) {
            if (on_blended(other)) continue;
            TopTools_ListOfShape images;
            map_through(other, chain, images);
            bool mapped = false;
            for (TopTools_ListIteratorOfListOfShape it(images); it.More(); it.Next())
                if (result_edges.Contains(it.Value())) {
                    run.remaining.push_back(TopoDS::Edge(it.Value()));
                    mapped = true;
                }
            if (!mapped) {
                run.why = "a swept blend consumed another selected edge";
                run.remaining.clear();
                return run;
            }
        }
        run.shape = current;
    } catch (const Standard_Failure& failure) {
        const char* message = failure.GetMessageString();
        run.why = std::string("swept blend failed: ") + (message && *message ? message : "OCCT failure");
        run.shape.Nullify();
        run.remaining.clear();
    }
    return run;
}

}  // namespace geom
