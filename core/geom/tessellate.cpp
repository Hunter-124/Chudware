#include "geom/tessellate.h"

#include <algorithm>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdlib>

#include <BRepAdaptor_Curve.hxx>
#include <BRepBndLib.hxx>
#include <Bnd_Box.hxx>
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
    // BRepMesh is face-incremental: already matching triangulations are retained.
    // The shape-taking constructor performs the mesh; a second Perform repeats
    // topology/deflection validation even when every triangulation is reusable.
    const auto start = std::chrono::steady_clock::now();
    BRepMesh_IncrementalMesh mesher(shape, eff_lin, Standard_False,
                                    eff_ang, Standard_True);
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
