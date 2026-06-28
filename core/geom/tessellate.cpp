#include "geom/tessellate.h"

#include <algorithm>

#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepLib_ToolTriangulatedShape.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRep_Tool.hxx>
#include <GCPnts_TangentialDeflection.hxx>
#include <GeomAbs_CurveType.hxx>
#include <GeomAbs_SurfaceType.hxx>
#include <Poly_PolygonOnTriangulation.hxx>
#include <Poly_Triangle.hxx>
#include <Poly_Triangulation.hxx>
#include <TopAbs.hxx>
#include <TopExp.hxx>
#include <TopLoc_Location.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_ListOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Edge.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Vertex.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Trsf.hxx>

namespace geom {

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
// real B-rep topology but not visible model edges — match the Python _is_hidden_edge.
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

}  // namespace

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

    // Smooth-render clamp (display path): never let a curved face facet worse than
    // kDisplayMaxAngularDeflection. A coarse caller angular value (the 0.35 rad default) would
    // otherwise render a faceted cylinder; a FINER request passes through unchanged. Linear
    // deflection is left as the caller set it (0.05 mm by default is already smooth) — the
    // angular term is the one that was binding on curved faces.
    const double eff_ang = (ang_deflection > 0.0)
                               ? std::min(ang_deflection, kDisplayMaxAngularDeflection)
                               : kDisplayMaxAngularDeflection;

    // Mesh the shape (parallel, absolute deflection) — same call as tessellate.py.
    BRepMesh_IncrementalMesh mesher(shape, lin_deflection, Standard_False,
                                    eff_ang, Standard_True);
    mesher.Perform();

    // Stable 1-based index maps.
    TopTools_IndexedMapOfShape face_map, edge_map, vert_map;
    TopExp::MapShapes(shape, TopAbs_FACE, face_map);
    TopExp::MapShapes(shape, TopAbs_EDGE, edge_map);
    TopExp::MapShapes(shape, TopAbs_VERTEX, vert_map);

    TopTools_IndexedDataMapOfShapeListOfShape anc_map;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, anc_map);

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

        BRepLib_ToolTriangulatedShape::ComputeNormals(face, tri);
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
                GCPnts_TangentialDeflection disc(crv, lin_deflection, eff_ang);
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
            body.edge_pts.insert(body.edge_pts.end(), pts.begin(), pts.end());
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

    return body;
}

}  // namespace geom
