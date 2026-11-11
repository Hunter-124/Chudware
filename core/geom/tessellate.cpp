#include "geom/tessellate.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <vector>

#include <BRepAdaptor_Curve.hxx>
#include <BRepBndLib.hxx>
#include <Bnd_Box.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepLib_ToolTriangulatedShape.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRepTools_WireExplorer.hxx>
#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <ElSLib.hxx>
#include <GCPnts_TangentialDeflection.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <IMeshTools_Parameters.hxx>
#include <NCollection_DataMap.hxx>
#include <OSD_Parallel.hxx>
#include <Poly_PolygonOnTriangulation.hxx>
#include <Poly_Triangle.hxx>
#include <Poly_Triangulation.hxx>
#include <Poly_TriangulationParameters.hxx>
#include <Precision.hxx>
#include <TColStd_Array1OfInteger.hxx>
#include <TColStd_Array1OfReal.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopTools_MapOfShape.hxx>
#include <TopTools_ShapeMapHasher.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Iterator.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Vertex.hxx>
#include <TopoDS_Wire.hxx>
#include <gp_Circ.hxx>
#include <gp_Dir.hxx>
#include <gp_Elips.hxx>
#include <gp_Pln.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pnt2d.hxx>
#include <gp_Trsf.hxx>

#include "../../extern/mapbox/earcut.hpp"

namespace geom {

// The document's tessellation phase runs on its compiling thread; different
// documents can compile concurrently on different threads.
namespace {
thread_local long long g_tess_mesh_ns = 0;
thread_local long long g_tess_extract_ns = 0;
thread_local long g_tess_bodies = 0;
thread_local long g_tess_faces = 0;
}  // namespace

void tess_timing_reset() {
    g_tess_mesh_ns = 0;
    g_tess_extract_ns = 0;
    g_tess_bodies = 0;
    g_tess_faces = 0;
}

TessTiming tess_timing_read() {
    TessTiming t;
    t.mesh_ms = g_tess_mesh_ns / 1e6;
    t.extract_ms = g_tess_extract_ns / 1e6;
    t.bodies = g_tess_bodies;
    t.faces = g_tess_faces;
    return t;
}

namespace {

// Display-quality ceiling on BRepMesh's angular deflection (max normal deviation between
// adjacent facets, radians). The historical render default fed straight through was 0.35 rad
// (~20deg), which leaves curved faces — cylinders especially — visibly faceted in the viewport.
// Clamping the angular term to ~0.20 rad (~11.5deg) roughly +75% facets around a curve (smooth)
// while staying BOUNDED: it is INERT on planar faces (no extra triangles there) and never lets
// a coarse caller value coarsen the display. A FINER (smaller) request is always honoured.
constexpr double kDisplayMaxAngularDeflection = 0.20;

// Surface/curve GeomAbs enum -> AICM code (mesh_format.md §2). Anything else -> "other".
uint8_t surf_code(GeomAbs_SurfaceType t) {
    switch (t) {
        case GeomAbs_Plane: return 0;
        case GeomAbs_Cylinder: return 1;
        case GeomAbs_Cone: return 2;
        case GeomAbs_Sphere: return 3;
        case GeomAbs_Torus: return 4;
        case GeomAbs_BSplineSurface: return 5;
        default: return 6;
    }
}

uint8_t curve_code(GeomAbs_CurveType t) {
    switch (t) {
        case GeomAbs_Line: return 0;
        case GeomAbs_Circle: return 1;
        case GeomAbs_Ellipse: return 2;
        case GeomAbs_BSplineCurve: return 3;
        default: return 4;
    }
}

// Seam (closed-on-a-face) or degenerate (zero-length, e.g. cone apex) edges are
// real B-rep topology but not visible model edges — same rule as _is_hidden_edge in
// services/sim/tessellate.py.
bool is_hidden_edge(const TopoDS_Edge& edge,
                    const TopTools_IndexedDataMapOfShapeListOfShape& anc_map) {
    if (BRep_Tool::Degenerated(edge)) return true;
    if (!anc_map.Contains(edge)) return false;
    const TopTools_ListOfShape& faces = anc_map.FindFromKey(edge);
    for (TopTools_ListIteratorOfListOfShape it(faces); it.More(); it.Next()) {
        TopoDS_Face f = TopoDS::Face(it.Value());
        if (BRep_Tool::IsClosed(edge, f)) return true;
    }
    return false;
}

// clamp(bbox_diag * rel, floor, ceil), falling back to `fallback` when the shape
// has no measurable bbox. Shared by auto_linear_deflection and every display
// quality tier, which differ only in the four constants.
double bbox_relative_deflection(const TopoDS_Shape& shape, double rel, double floor_mm,
                                double ceil_mm, double fallback) {
    try {
        Bnd_Box bb;
        BRepBndLib::Add(shape, bb);
        if (!bb.IsVoid()) {
            double x0, y0, z0, x1, y1, z1;
            bb.Get(x0, y0, z0, x1, y1, z1);
            const double dx = x1 - x0, dy = y1 - y0, dz = z1 - z0;
            const double diag = std::sqrt(dx * dx + dy * dy + dz * dz);
            if (std::isfinite(diag) && diag > 0.0)
                return std::min(ceil_mm, std::max(floor_mm, diag * rel));
        }
    } catch (...) { /* fall through to the fallback */ }
    return fallback;
}

// OCCT's default minimum edge size is one tenth of the linear deflection.
// Highly segmented spline surfaces can make angular refinement repeatedly
// split near-coincident nodes until Delaunay insertion dominates the entire
// build. A quarter of the same deflection is still below the requested chord
// tolerance, but avoids most of that runaway refinement.
//
// Leave analytic-only shapes exactly as before. On mixed/spline shapes, cap
// the minimum to half the angular step of any circular or elliptical edge;
// otherwise a coarse chord tolerance could visibly facet a small round detail.
// AdjustMinSize below also lets OCCT lower it for short edges.
double spline_mesh_min_size(const TopoDS_Shape& shape, double lin, double ang) {
    try {
        bool has_spline = false;
        for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
            if (BRepAdaptor_Surface(TopoDS::Face(ex.Current())).GetType() ==
                GeomAbs_BSplineSurface) {
                has_spline = true;
                break;
            }
        }
        if (!has_spline) return 0.0;

        double min_size = 0.25 * lin;
        const double default_min = 0.1 * lin;
        for (TopExp_Explorer ex(shape, TopAbs_EDGE); ex.More(); ex.Next()) {
            const TopoDS_Edge edge = TopoDS::Edge(ex.Current());
            if (BRep_Tool::Degenerated(edge)) continue;
            const BRepAdaptor_Curve curve(edge);
            const auto type = curve.GetType();
            if (type == GeomAbs_Circle)
                min_size = std::min(min_size, 0.5 * ang * curve.Circle().Radius());
            else if (type == GeomAbs_Ellipse)
                min_size = std::min(min_size, 0.5 * ang * curve.Ellipse().MinorRadius());
            if (min_size <= default_min) return 0.0;
        }
        return min_size;
    } catch (...) {
        // Preserve the original mesher behavior for malformed/unsupported edges.
        return 0.0;
    }
}

// ---- planar fast path ---------------------------------------------------------
// BRepMesh's Delaunay degrades badly on a planar face bounded by thousands of nodes
// (a plate with a few hundred holes: seconds per face, superlinear in the hole count),
// and every edit that touches such a face re-meshes it from scratch. A plane needs no
// interior nodes, so those faces are ear-clipped here over exactly the boundary nodes
// their edges carry on the neighbouring faces, and attached the way BRepMesh attaches
// its own result: node and triangle counts are what BRepMesh produces, shared edges stay
// watertight, and BRepMesh treats the face as already meshed.

// Below this many estimated boundary nodes BRepMesh is fast and its output is kept.
constexpr int kPlanarFastPathMinNodes = 256;

struct EdgeNodes {
    std::vector<gp_Pnt> pts;  // world frame, in the edge's forward parameter order
    std::vector<double> prm;
};
using EdgeNodeCache = NCollection_DataMap<TopoDS_Shape, EdgeNodes, TopTools_ShapeMapHasher>;

// One planar face between gathering its boundary and attaching its triangulation.
struct PlanarFace {
    struct LoopEdge {
        TopoDS_Edge edge;
        std::vector<int> nodes;  // node index per polygon point, forward parameter order
        std::vector<double> prm;
    };
    TopoDS_Face face;
    gp_Pln plane;
    gp_Trsf to_local;                    // world -> the frame the face's TShape is meshed in
    std::vector<gp_Pnt> nodes;           // world frame
    std::vector<std::vector<int>> loops; // node indices per wire
    std::vector<LoopEdge> edges;
    Handle(Poly_Triangulation) tri;
};

// Was `tri` produced by this request? Such a face needs no second look. The recorded
// deflection is never the requested one to the last digit: BRepMesh records a per-face
// value derived from it, and the size-relative request itself moves in the fourth digit
// once a body's bounding box is measured off its mesh.
bool meshed_as_requested(const Handle(Poly_Triangulation)& tri, const IMeshTools_Parameters& params) {
    if (tri.IsNull()) return false;
    const Handle(Poly_TriangulationParameters)& made = tri->Parameters();
    return !made.IsNull() &&
           std::abs(made->Deflection() - params.Deflection) <= 0.01 * params.Deflection &&
           std::abs(made->Angle() - params.Angle) <= 1.0e-9;
}

// Boundary-node estimate of a face, without discretizing anything. BRepMesh steps a
// circular edge by half the angular deflection.
int estimate_boundary_nodes(const TopoDS_Face& face, double ang) {
    int n = 0;
    for (TopExp_Explorer ex(face, TopAbs_EDGE); ex.More(); ex.Next()) {
        const BRepAdaptor_Curve c(TopoDS::Edge(ex.Current()));
        switch (c.GetType()) {
            case GeomAbs_Line: n += 1; break;
            case GeomAbs_Circle:
            case GeomAbs_Ellipse:
                n += static_cast<int>(std::abs(c.LastParameter() - c.FirstParameter()) /
                                      (0.5 * ang)) + 1;
                break;
            default: n += 16; break;
        }
    }
    return n;
}

enum class Attached { None, Found, Unusable };

// The nodes a meshed neighbouring face already gave `edge`. Unusable: a neighbour is
// meshed but its nodes for this edge cannot be read back, so this face could not be
// made to share them. `pending` are the fast-path faces themselves, whose current
// triangulation (if any) is about to be replaced.
Attached attached_edge_nodes(const TopoDS_Edge& edge,
                             const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces,
                             const TopTools_MapOfShape& pending, EdgeNodes& out) {
    if (!edge_faces.Contains(edge)) return Attached::None;
    bool meshed_neighbour = false;
    for (TopTools_ListIteratorOfListOfShape it(edge_faces.FindFromKey(edge)); it.More(); it.Next()) {
        if (pending.Contains(it.Value())) continue;
        TopLoc_Location loc;
        const Handle(Poly_Triangulation) tri =
            BRep_Tool::Triangulation(TopoDS::Face(it.Value()), loc);
        if (tri.IsNull()) continue;
        meshed_neighbour = true;
        const Handle(Poly_PolygonOnTriangulation) poly =
            BRep_Tool::PolygonOnTriangulation(edge, tri, loc);
        if (poly.IsNull() || !poly->HasParameters() || poly->NbNodes() < 2) continue;
        const gp_Trsf trsf = loc.Transformation();
        out.pts.clear();
        out.prm.clear();
        for (int i = 1; i <= poly->NbNodes(); ++i) {
            out.pts.push_back(tri->Node(poly->Node(i)).Transformed(trsf));
            out.prm.push_back(poly->Parameters()->Value(i));
        }
        return Attached::Found;
    }
    return meshed_neighbour ? Attached::Unusable : Attached::None;
}

// Collect a planar face's boundary as closed loops of shared nodes. False leaves the
// face to BRepMesh: anything but simple closed wires of once-used edges. `fresh` holds
// the edges discretized here, so two fast-path faces sharing one agree on its nodes.
bool gather_planar_face(const TopoDS_Face& face,
                        const TopTools_IndexedDataMapOfShapeListOfShape& edge_faces,
                        const TopTools_MapOfShape& pending, EdgeNodeCache& fresh,
                        const IMeshTools_Parameters& params, PlanarFace& out) {
    const gp_Trsf to_world = face.Location().Transformation();
    if (to_world.IsNegative() || std::abs(std::abs(to_world.ScaleFactor()) - 1.0) > 1.0e-12)
        return false;
    const BRepAdaptor_Surface surf(face, Standard_False);
    if (surf.GetType() != GeomAbs_Plane) return false;
    out.face = face;
    out.plane = surf.Plane();
    out.to_local = to_world.Inverted();

    TopTools_MapOfShape seen;
    for (TopoDS_Iterator wi(face); wi.More(); wi.Next()) {
        if (wi.Value().ShapeType() != TopAbs_WIRE) return false;
        const TopAbs_Orientation wo = wi.Value().Orientation();
        if (wo != TopAbs_FORWARD && wo != TopAbs_REVERSED) return false;
        const TopoDS_Wire wire = TopoDS::Wire(wi.Value());
        int n_edges = 0;
        for (TopoDS_Iterator ei(wire); ei.More(); ei.Next()) ++n_edges;

        std::vector<int> loop;
        const std::size_t first_edge = out.edges.size();
        int walked = 0;
        for (BRepTools_WireExplorer we(wire, face); we.More(); we.Next(), ++walked) {
            const TopoDS_Edge edge = we.Current();
            if (BRep_Tool::Degenerated(edge)) return false;
            const TopoDS_Shape key = edge.Oriented(TopAbs_FORWARD);
            if (!seen.Add(key)) return false;  // seam or slit: both sides in one face
            EdgeNodes en;
            const Attached attached = attached_edge_nodes(edge, edge_faces, pending, en);
            if (attached == Attached::Unusable) return false;
            if (attached == Attached::None) {
                if (const EdgeNodes* known = fresh.Seek(key)) {
                    en = *known;
                } else {
                    const BRepAdaptor_Curve curve(edge);
                    const GCPnts_TangentialDeflection disc(
                        curve, 0.5 * params.Angle, params.Deflection, 2, Precision::PConfusion(),
                        params.MinSize > 0.0 ? params.MinSize : 1.0e-7);
                    if (disc.NbPoints() < 2) return false;
                    for (int i = 1; i <= disc.NbPoints(); ++i) {
                        en.pts.push_back(disc.Value(i));
                        en.prm.push_back(disc.Parameter(i));
                    }
                    // End nodes sit on the vertices, as BRepMesh places them.
                    TopoDS_Vertex vf, vl;
                    TopExp::Vertices(TopoDS::Edge(key), vf, vl);
                    if (vf.IsNull() || vl.IsNull()) return false;
                    en.pts.front() = BRep_Tool::Pnt(vf);
                    en.pts.back() = BRep_Tool::Pnt(vl);
                    fresh.Bind(key, en);
                }
            }
            const bool reversed = edge.Orientation() == TopAbs_REVERSED;
            const std::size_t m = en.pts.size();
            PlanarFace::LoopEdge le;
            le.edge = edge;
            le.nodes.assign(m, 0);
            le.prm = en.prm;
            // Walk the edge in wire direction; its first node is the previous edge's last.
            for (std::size_t k = 0; k < m; ++k) {
                const std::size_t src = reversed ? m - 1 - k : k;
                int idx = 0;
                if (k == 0 && !loop.empty()) {
                    idx = loop.back();
                    const double tol =
                        BRep_Tool::Tolerance(we.CurrentVertex()) + Precision::Confusion();
                    if (out.nodes[static_cast<std::size_t>(idx)].Distance(en.pts[src]) > 2.0 * tol)
                        return false;
                } else {
                    out.nodes.push_back(en.pts[src]);
                    idx = static_cast<int>(out.nodes.size()) - 1;
                    loop.push_back(idx);
                }
                le.nodes[src] = idx;
            }
            out.edges.push_back(std::move(le));
        }
        if (walked != n_edges || loop.size() < 4) return false;
        // Close the wire: its last node, the one appended last, repeats its first.
        const int last = loop.back(), first = loop.front();
        if (out.nodes[static_cast<std::size_t>(last)].Distance(
                out.nodes[static_cast<std::size_t>(first)]) >
            2.0 * (BRep_Tool::Tolerance(face) + Precision::Confusion()))
            return false;
        loop.pop_back();
        out.nodes.pop_back();
        for (std::size_t k = first_edge; k < out.edges.size(); ++k)
            for (int& idx : out.edges[k].nodes)
                if (idx == last) idx = first;
        out.loops.push_back(std::move(loop));
    }
    return !out.loops.empty();
}

// Ear-clip the gathered boundary. False (no triangulation) when the result does not
// cover the face exactly with every node used: BRepMesh then meshes the face.
bool triangulate_planar_face(PlanarFace& pf, const IMeshTools_Parameters& params) {
    const std::size_t n = pf.nodes.size();
    std::vector<gp_Pnt2d> uv(n);
    for (std::size_t i = 0; i < n; ++i) {
        double u = 0.0, v = 0.0;
        ElSLib::Parameters(pf.plane, pf.nodes[i], u, v);
        uv[i].SetCoord(u, v);
    }
    // The wire enclosing the largest area is the outer boundary.
    std::vector<double> areas(pf.loops.size());
    std::size_t outer = 0;
    for (std::size_t k = 0; k < pf.loops.size(); ++k) {
        const std::vector<int>& loop = pf.loops[k];
        double a = 0.0;
        for (std::size_t j = 0; j < loop.size(); ++j) {
            const gp_Pnt2d& p = uv[static_cast<std::size_t>(loop[j])];
            const gp_Pnt2d& q = uv[static_cast<std::size_t>(loop[(j + 1) % loop.size()])];
            a += p.X() * q.Y() - q.X() * p.Y();
        }
        areas[k] = std::abs(0.5 * a);
        if (areas[k] > areas[outer]) outer = k;
    }
    double expected = areas[outer];
    for (std::size_t k = 0; k < pf.loops.size(); ++k)
        if (k != outer) expected -= areas[k];
    if (!(expected > 0.0)) return false;

    // earcut numbers the rings' points consecutively, outer ring first.
    std::vector<std::vector<std::array<double, 2>>> rings;
    std::vector<int> ring_node;
    ring_node.reserve(n);
    auto add_ring = [&](const std::vector<int>& loop) {
        std::vector<std::array<double, 2>> ring;
        ring.reserve(loop.size());
        for (const int idx : loop) {
            const gp_Pnt2d& p = uv[static_cast<std::size_t>(idx)];
            ring.push_back({p.X(), p.Y()});
            ring_node.push_back(idx);
        }
        rings.push_back(std::move(ring));
    };
    add_ring(pf.loops[outer]);
    for (std::size_t k = 0; k < pf.loops.size(); ++k)
        if (k != outer) add_ring(pf.loops[k]);
    const std::vector<uint32_t> ears = mapbox::earcut<uint32_t>(rings);
    const std::size_t n_tris = ears.size() / 3;
    // A polygon with h holes has exactly n - 2 + 2h triangles over all of its n nodes.
    if (n_tris != n - 2 + 2 * (pf.loops.size() - 1)) return false;

    Handle(Poly_Triangulation) tri =
        new Poly_Triangulation(static_cast<int>(n), static_cast<int>(n_tris), Standard_True);
    double covered = 0.0;
    for (std::size_t t = 0; t < n_tris; ++t) {
        const int a = ring_node[ears[3 * t]];
        int b = ring_node[ears[3 * t + 1]], c = ring_node[ears[3 * t + 2]];
        const gp_Pnt2d& pa = uv[static_cast<std::size_t>(a)];
        const gp_Pnt2d& pb = uv[static_cast<std::size_t>(b)];
        const gp_Pnt2d& pc = uv[static_cast<std::size_t>(c)];
        const double area2 = (pb.X() - pa.X()) * (pc.Y() - pa.Y()) -
                             (pc.X() - pa.X()) * (pb.Y() - pa.Y());
        if (area2 < 0.0) std::swap(b, c);  // counter-clockwise in the surface's (u, v)
        covered += 0.5 * std::abs(area2);
        tri->SetTriangle(static_cast<int>(t) + 1, Poly_Triangle(a + 1, b + 1, c + 1));
    }
    if (std::abs(covered - expected) > 1.0e-9 * areas[outer]) return false;

    // Nodes and normals in the TShape's frame; the normal is the surface's own
    // (u x v), which extraction flips for a reversed face.
    const gp_Dir normal = pf.plane.Position().XDirection()
                              .Crossed(pf.plane.Position().YDirection())
                              .Transformed(pf.to_local);
    tri->AddNormals();
    for (std::size_t i = 0; i < n; ++i) {
        const int at = static_cast<int>(i) + 1;
        tri->SetNode(at, pf.nodes[i].Transformed(pf.to_local));
        tri->SetUVNode(at, uv[i]);
        tri->SetNormal(at, normal);
    }
    tri->Deflection(params.Deflection);
    tri->Parameters(new Poly_TriangulationParameters(params.Deflection, params.Angle,
                                                     params.MinSize));
    pf.tri = tri;
    return true;
}

// Replace the face's triangulation and give its edges their polygons on it.
void attach_planar_face(const PlanarFace& pf, double lin) {
    BRep_Builder builder;
    TopLoc_Location loc;
    const Handle(Poly_Triangulation) old = BRep_Tool::Triangulation(pf.face, loc);
    if (!old.IsNull())
        for (const PlanarFace::LoopEdge& le : pf.edges)
            builder.UpdateEdge(le.edge, Handle(Poly_PolygonOnTriangulation)(), old, loc);
    builder.UpdateFace(pf.face, pf.tri);
    for (const PlanarFace::LoopEdge& le : pf.edges) {
        const int m = static_cast<int>(le.nodes.size());
        TColStd_Array1OfInteger idx(1, m);
        TColStd_Array1OfReal prm(1, m);
        for (int k = 0; k < m; ++k) {
            idx.SetValue(k + 1, le.nodes[static_cast<std::size_t>(k)] + 1);
            prm.SetValue(k + 1, le.prm[static_cast<std::size_t>(k)]);
        }
        Handle(Poly_PolygonOnTriangulation) poly = new Poly_PolygonOnTriangulation(idx, prm);
        poly->Deflection(lin);
        builder.UpdateEdge(le.edge, poly, pf.tri, pf.face.Location());
    }
}

// Mesh `shape` for display, doing work only for the faces that need it. BRepMesh keeps
// a triangulation that already matches, but still rebuilds its model of every face and
// edge to find that out; on a body with hundreds of faces that walk alone costs as much
// as an edit. So faces meshed by this exact request are left alone, the heavy planar
// ones take the fast path, and BRepMesh gets the rest together with the already-meshed
// faces they share an edge with (whose edge nodes it then reuses).
void mesh_changed_faces(const TopoDS_Shape& shape, const IMeshTools_Parameters& params) {
    std::vector<TopoDS_Face> heavy, rest;
    TopTools_MapOfShape visited, current;
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
        if (!visited.Add(ex.Current())) continue;
        const TopoDS_Face face = TopoDS::Face(ex.Current());
        TopLoc_Location loc;
        if (meshed_as_requested(BRep_Tool::Triangulation(face, loc), params))
            current.Add(face);
        else if (BRepAdaptor_Surface(face, Standard_False).GetType() == GeomAbs_Plane &&
                 estimate_boundary_nodes(face, params.Angle) >= kPlanarFastPathMinNodes)
            heavy.push_back(face);
        else
            rest.push_back(face);
    }
    if (heavy.empty() && current.IsEmpty()) {
        BRepMesh_IncrementalMesh mesher(shape, params);
        return;
    }

    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);
    auto brepmesh = [&](const std::vector<TopoDS_Face>& faces, bool free_edges) {
        BRep_Builder builder;
        TopoDS_Compound part;
        builder.MakeCompound(part);
        bool any = false;
        TopTools_MapOfShape neighbours;
        for (const TopoDS_Face& face : faces) {
            builder.Add(part, face);
            any = true;
            for (TopExp_Explorer ex(face, TopAbs_EDGE); ex.More(); ex.Next()) {
                if (!edge_faces.Contains(ex.Current())) continue;
                for (TopTools_ListIteratorOfListOfShape it(edge_faces.FindFromKey(ex.Current()));
                     it.More(); it.Next())
                    if (current.Contains(it.Value()) && neighbours.Add(it.Value()))
                        builder.Add(part, it.Value());
            }
        }
        // Edges outside any face (imported reference curves) carry their own polygon.
        for (TopExp_Explorer ex(shape, TopAbs_EDGE, TopAbs_FACE); free_edges && ex.More(); ex.Next()) {
            TopLoc_Location loc;
            if (!BRep_Tool::Polygon3D(TopoDS::Edge(ex.Current()), loc).IsNull()) continue;
            builder.Add(part, ex.Current());
            any = true;
        }
        if (any) BRepMesh_IncrementalMesh mesher(part, params);
    };
    // The other faces first: they fix the nodes of the edges the planar faces share.
    brepmesh(rest, true);
    if (heavy.empty()) return;

    std::vector<PlanarFace> planar(heavy.size());
    std::vector<char> ready(heavy.size(), 0);
    EdgeNodeCache fresh;
    TopTools_MapOfShape pending;
    for (const TopoDS_Face& face : heavy) pending.Add(face);
    for (std::size_t i = 0; i < heavy.size(); ++i) {
        try {
            ready[i] = gather_planar_face(heavy[i], edge_faces, pending, fresh, params, planar[i]);
        } catch (const Standard_Failure&) {
        }
    }
    OSD_Parallel::For(0, static_cast<int>(heavy.size()), [&](int i) {
        const std::size_t at = static_cast<std::size_t>(i);
        if (!ready[at]) return;
        try {
            ready[at] = triangulate_planar_face(planar[at], params);
        } catch (...) {
            ready[at] = 0;
        }
    }, heavy.size() < 2);
    std::vector<TopoDS_Face> declined;
    for (std::size_t i = 0; i < heavy.size(); ++i) {
        if (ready[i]) {
            attach_planar_face(planar[i], params.Deflection);
            current.Add(heavy[i]);
        } else {
            declined.push_back(heavy[i]);
        }
    }
    brepmesh(declined, false);
}

}  // namespace

// Size-relative default linear deflection. A fixed
// absolute 0.05 mm meshed multi-metre imports at millions of triangles; scale the
// chord tolerance with the part instead:
//   lin = clamp(bbox_diag * kAutoRel, kAutoFloor, kAutoCeil)
//     kAutoRel   = 6e-4  — a ~50 mm part (diag ~86.6 mm) yields ~0.052 mm, i.e.
//                          essentially the historical 0.05 mm default, so small
//                          and medium parts keep their prior visual quality
//     kAutoFloor = 0.02 mm — bounds the density gain on tiny parts
//     kAutoCeil  = 1.0 mm  — caps facet chord error on huge parts (a 2 m part
//                            meshes at 1.0 mm, ~20x fewer curved-face tris)
// Falls back to the historical 0.05 mm when the bbox is void/degenerate.
double auto_linear_deflection(const TopoDS_Shape& shape) {
    return bbox_relative_deflection(shape, 6e-4, 0.02, 1.0, 0.05);
}

// ---- display quality tiers (see tessellate.h) ---------------------------------
DisplayDeflections display_deflections(DisplayQuality q, const TopoDS_Shape& shape) {
    switch (q) {
        case DisplayQuality::Draft:
            return {bbox_relative_deflection(shape, 6.0e-4, 0.02, 1.0, 0.05), 0.35};
        case DisplayQuality::Standard:
            return {bbox_relative_deflection(shape, 3.0e-4, 0.01, 0.5, 0.025), 0.20};
        case DisplayQuality::Fine:
        default:
            return {bbox_relative_deflection(shape, 1.5e-4, 0.005, 0.25, 0.0125), 0.10};
    }
}

const char* display_quality_name(DisplayQuality q) {
    switch (q) {
        case DisplayQuality::Draft: return "draft";
        case DisplayQuality::Standard: return "standard";
        case DisplayQuality::Fine:
        default: return "fine";
    }
}

bool display_quality_parse(const std::string& name, DisplayQuality& out) {
    if (name == "draft") { out = DisplayQuality::Draft; return true; }
    if (name == "standard") { out = DisplayQuality::Standard; return true; }
    if (name == "fine") { out = DisplayQuality::Fine; return true; }
    return false;
}

DisplayQuality default_display_quality() {
    // Read once: every document in this process (headless render, hostd, GUI) must
    // agree on the default, and the env cannot change under a running engine.
    static const DisplayQuality q = []() {
        DisplayQuality parsed = DisplayQuality::Fine;
        const char* v = std::getenv("CHUDWARE_DISPLAY_QUALITY");
        if (v && *v) {
            std::string s(v);
            for (char& c : s) c = static_cast<char>(std::tolower((unsigned char)c));
            display_quality_parse(s, parsed);  // invalid name -> Fine
        }
        return parsed;
    }();
    return q;
}

DisplayDeflections mesh_body(const TopoDS_Shape& shape, double lin_deflection, double ang_deflection) {
    // Use exactly the display mesher settings for both standalone parts and
    // assembly components. In particular, retain the smooth-render angular clamp.
    const double eff_ang = (ang_deflection > 0.0)
                               ? std::min(ang_deflection, kDisplayMaxAngularDeflection)
                               : kDisplayMaxAngularDeflection;
    const double eff_lin =
        (lin_deflection > 0.0) ? lin_deflection : auto_linear_deflection(shape);
    IMeshTools_Parameters params;
    params.Deflection = eff_lin;
    params.Angle = eff_ang;
    params.InParallel = Standard_True;
    const double min_size = spline_mesh_min_size(shape, eff_lin, eff_ang);
    if (min_size > 0.0) {
        params.MinSize = min_size;
        params.AdjustMinSize = Standard_True;
    }
    const auto start = std::chrono::steady_clock::now();
    mesh_changed_faces(shape, params);
    g_tess_mesh_ns += std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now() - start).count();
    ++g_tess_bodies;
    return {eff_lin, eff_ang};
}

aicm::AicmBody tessellate_body(const TopoDS_Shape& shape, uint32_t body_id,
                               const std::string& name, const float color[3],
                               double lin_deflection, double ang_deflection,
                               float metallic, float roughness) {
    aicm::AicmBody body;
    body.body_id = body_id;
    body.name = name;
    body.color[0] = color[0];
    body.color[1] = color[1];
    body.color[2] = color[2];
    body.metallic = metallic;
    body.roughness = roughness;

    const auto deflections = mesh_body(shape, lin_deflection, ang_deflection);

    // Everything past meshing — the topology index maps + the triangle/edge/vertex
    // extraction — is the "extract" cost (distinct from BRepMesh above).
    const auto _extract_t0 = std::chrono::steady_clock::now();

    // Stable 1-based index maps.
    TopTools_IndexedMapOfShape face_map, edge_map, vert_map;
    TopExp::MapShapes(shape, TopAbs_FACE, face_map);
    TopExp::MapShapes(shape, TopAbs_EDGE, edge_map);
    TopExp::MapShapes(shape, TopAbs_VERTEX, vert_map);

    TopTools_IndexedDataMapOfShapeListOfShape anc_map;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc_map);

    g_tess_faces += face_map.Extent();

    // ----- Per-face geometry: triangles grouped per face (contiguous ranges) -----
    for (int fi = 1; fi <= face_map.Extent(); ++fi) {
        TopoDS_Face face = TopoDS::Face(face_map.FindKey(fi));
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);

        uint32_t first_tri = body.n_tris();
        if (tri.IsNull()) {
            body.faces.push_back({static_cast<uint32_t>(fi), first_tri, 0, 0});
            continue;
        }

        // Authored per-vertex normals win. A face whose triangulation already carries
        // normals came from a reader that recovered them (the SolidWorks saved-display
        // lane, one planar face per triangle); ComputeNormals would overwrite them with the
        // face's single flat normal and render the part as discrete facets. BRepMesh above
        // is face-incremental, so an untouched face still has them here.
        if (!tri->HasNormals()) BRepLib_ToolTriangulatedShape::ComputeNormals(face, tri);
        const bool is_reversed = (face.Orientation() == TopAbs_REVERSED);
        const gp_Trsf trsf = loc.Transformation();
        const bool is_identity = loc.IsIdentity();

        const uint32_t base_vert = body.n_verts();
        for (int ni = 1; ni <= tri->NbNodes(); ++ni) {
            gp_Pnt nd = tri->Node(ni);
            if (!is_identity) nd.Transform(trsf);
            body.pos.push_back(static_cast<float>(nd.X()));
            body.pos.push_back(static_cast<float>(nd.Y()));
            body.pos.push_back(static_cast<float>(nd.Z()));

            gp_Dir nv = tri->Normal(ni);
            // Normals live in the triangulation's (i.e. the face location's) frame, exactly
            // like the nodes above — rotate them into the body frame for a located face.
            if (!is_identity) nv.Transform(trsf);
            const double s = is_reversed ? -1.0 : 1.0;
            body.nrm.push_back(static_cast<float>(s * nv.X()));
            body.nrm.push_back(static_cast<float>(s * nv.Y()));
            body.nrm.push_back(static_cast<float>(s * nv.Z()));
        }

        for (int ti = 1; ti <= tri->NbTriangles(); ++ti) {
            int n1, n2, n3;
            tri->Triangle(ti).Get(n1, n2, n3);
            // OCCT triangle node indices are 1-based; rebase to our global buffer.
            const uint32_t a = base_vert + static_cast<uint32_t>(n1 - 1);
            const uint32_t b = base_vert + static_cast<uint32_t>(n2 - 1);
            const uint32_t c = base_vert + static_cast<uint32_t>(n3 - 1);
            if (is_reversed) {
                body.idx.push_back(a);
                body.idx.push_back(c);
                body.idx.push_back(b);
            } else {
                body.idx.push_back(a);
                body.idx.push_back(b);
                body.idx.push_back(c);
            }
        }

        const uint32_t tri_count = body.n_tris() - first_tri;
        const uint8_t st = surf_code(BRepAdaptor_Surface(face).GetType());
        body.faces.push_back(
            {static_cast<uint32_t>(fi), first_tri, tri_count, st});
    }

    // ----- Edge polylines: PolygonOnTriangulation preferred, GCPnts fallback -----
    for (int ei = 1; ei <= edge_map.Extent(); ++ei) {
        TopoDS_Edge edge = TopoDS::Edge(edge_map.FindKey(ei));
        const uint32_t first_pt = body.total_edge_pts();

        if (is_hidden_edge(edge, anc_map)) {
            body.edges.push_back({static_cast<uint32_t>(ei), first_pt, 0, 4});
            continue;
        }

        std::vector<float> pts;  // flat xyz triples for this edge

        // Try PolygonOnTriangulation against an adjacent face's triangulation.
        if (anc_map.Contains(edge)) {
            const TopTools_ListOfShape& faces = anc_map.FindFromKey(edge);
            if (!faces.IsEmpty()) {
                TopoDS_Face adj_face = TopoDS::Face(faces.First());
                TopLoc_Location loc_e;
                Handle(Poly_Triangulation) adj_tri =
                    BRep_Tool::Triangulation(adj_face, loc_e);
                if (!adj_tri.IsNull()) {
                    const Handle(Poly_PolygonOnTriangulation)& poly =
                        BRep_Tool::PolygonOnTriangulation(edge, adj_tri, loc_e);
                    if (!poly.IsNull()) {
                        const gp_Trsf trsf_e = loc_e.Transformation();
                        const bool is_id_e = loc_e.IsIdentity();
                        for (int ni = 1; ni <= poly->NbNodes(); ++ni) {
                            const int node_idx = poly->Node(ni);
                            gp_Pnt nd = adj_tri->Node(node_idx);
                            if (!is_id_e) nd.Transform(trsf_e);
                            pts.push_back(static_cast<float>(nd.X()));
                            pts.push_back(static_cast<float>(nd.Y()));
                            pts.push_back(static_cast<float>(nd.Z()));
                        }
                    }
                }
            }
        }

        // Fallback: tangential-deflection discretization of the 3D curve.
        if (pts.empty()) {
            try {
                BRepAdaptor_Curve crv(edge);
                GCPnts_TangentialDeflection disc(crv, deflections.lin, deflections.ang);
                for (int ni = 1; ni <= disc.NbPoints(); ++ni) {
                    gp_Pnt p = disc.Value(ni);
                    pts.push_back(static_cast<float>(p.X()));
                    pts.push_back(static_cast<float>(p.Y()));
                    pts.push_back(static_cast<float>(p.Z()));
                }
            } catch (const Standard_Failure&) {
                // leave pts empty -> zero-point edge record below
            }
        }

        if (!pts.empty()) {
            const uint8_t ct = curve_code(BRepAdaptor_Curve(edge).GetType());
            const uint32_t pt_count = static_cast<uint32_t>(pts.size() / 3);
            body.edges.push_back({static_cast<uint32_t>(ei), first_pt, pt_count, ct});
            body.edge_pts.insert_end(pts.begin(), pts.end());
        } else {
            body.edges.push_back({static_cast<uint32_t>(ei), first_pt, 0, 4});
        }
    }

    // ----- B-rep vertices -----
    for (int vi = 1; vi <= vert_map.Extent(); ++vi) {
        TopoDS_Vertex v = TopoDS::Vertex(vert_map.FindKey(vi));
        gp_Pnt p = BRep_Tool::Pnt(v);
        aicm::VertRec vr;
        vr.vertex_id = static_cast<uint32_t>(vi);
        vr.xyz[0] = static_cast<float>(p.X());
        vr.xyz[1] = static_cast<float>(p.Y());
        vr.xyz[2] = static_cast<float>(p.Z());
        body.verts.push_back(vr);
    }

    g_tess_extract_ns += std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now() - _extract_t0).count();
    return body;
}

void transform_body_rigid(aicm::AicmBody& body, const gp_Trsf& t) {
    // 3x4 row-major from gp_Trsf (Value is 1-based: rows 1..3, cols 1..4).
    double m[3][4];
    for (int r = 0; r < 3; ++r)
        for (int c = 0; c < 4; ++c) m[r][c] = t.Value(r + 1, c + 1);
    auto xform_points = [&](aicm::CowVec<float>& a) {
        const std::size_t n = a.size();
        for (std::size_t i = 0; i + 2 < n; i += 3) {
            const double x = a[i], y = a[i + 1], z = a[i + 2];
            a[i]     = static_cast<float>(m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3]);
            a[i + 1] = static_cast<float>(m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3]);
            a[i + 2] = static_cast<float>(m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3]);
        }
    };
    xform_points(body.pos);
    xform_points(body.edge_pts);
    // Normals: rotation only (rigid => inverse-transpose == rotation).
    {
        aicm::CowVec<float>& a = body.nrm;
        const std::size_t n = a.size();
        for (std::size_t i = 0; i + 2 < n; i += 3) {
            const double x = a[i], y = a[i + 1], z = a[i + 2];
            a[i]     = static_cast<float>(m[0][0] * x + m[0][1] * y + m[0][2] * z);
            a[i + 1] = static_cast<float>(m[1][0] * x + m[1][1] * y + m[1][2] * z);
            a[i + 2] = static_cast<float>(m[2][0] * x + m[2][1] * y + m[2][2] * z);
        }
    }
    for (aicm::VertRec& vr : body.verts) {
        const double x = vr.xyz[0], y = vr.xyz[1], z = vr.xyz[2];
        vr.xyz[0] = static_cast<float>(m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3]);
        vr.xyz[1] = static_cast<float>(m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3]);
        vr.xyz[2] = static_cast<float>(m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3]);
    }
}

}  // namespace geom
