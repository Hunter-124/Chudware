// Wave 2 verification: OCCT shape -> geom::tessellate_body -> aicm::write_aicm ->
// render::parseAicm round-trip through the loader the GUI ships
// (host/src/render/aicm_loader): if it accepts our bytes and the geometry invariants
// hold, the C++ emitter is byte-compatible with the format the host consumes.
//
// Invariants are OCCT-version-INDEPENDENT (counts/ranges/coverage/bbox), NOT a byte
// compare against the hand-authored contracts/fixtures/box.aicm.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <limits>
#include <string>
#include <vector>

#include <BRepAlgoAPI_Cut.hxx>
#include <BRepBuilderAPI_Copy.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <IMeshTools_Parameters.hxx>
#include <Poly_PolygonOnTriangulation.hxx>
#include <Poly_Triangulation.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopTools_ListIteratorOfListOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <gp_Ax2.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

#include "geom/aicm.h"
#include "geom/features.h"
#include "geom/tessellate.h"
#include "aicm_loader.h"  // host loader (host/src/render on the include path)

namespace {

// Tessellate `shape` -> AICM bytes -> parse with the HOST loader. Asserts the host
// accepts it and yields exactly one body; returns the parsed DocMesh + raw bytes.
struct Loaded {
    render::DocMesh doc;
    std::vector<uint8_t> bytes;
};

Loaded roundtrip(const TopoDS_Shape& shape, const std::string& name,
                 const float color[3]) {
    aicm::AicmBody body = geom::tessellate_body(shape, 1, name, color);
    std::vector<aicm::AicmBody> bodies{body};
    std::vector<uint8_t> bytes = aicm::write_aicm(bodies);

    render::DocMesh doc;
    std::string err;
    bool ok = render::parseAicm(bytes.data(), bytes.size(), doc, err);
    REQUIRE_MESSAGE(ok, "host loader rejected AICM: " << err);
    return {std::move(doc), std::move(bytes)};
}

// Structural invariants shared by every body (independent of the source shape).
void check_common(const render::BodyMesh& b) {
    // Geometry array sizes are self-consistent.
    CHECK(b.pos.size() == 3ull * b.n_verts);
    CHECK(b.nrm.size() == 3ull * b.n_verts);
    CHECK(b.idx.size() == 3ull * b.n_tris);

    // Every triangle index is in range.
    for (uint32_t i : b.idx) CHECK(i < b.n_verts);

    // Face ranges are contiguous, in order, and cover exactly all triangles.
    uint32_t running = 0;
    for (const render::FaceRange& f : b.faces) {
        CHECK(f.first_tri == running);
        running += f.tri_count;
    }
    CHECK(running == b.n_tris);

    // Every edge polyline stays within the edge_pts array.
    const uint32_t total_pts = static_cast<uint32_t>(b.edge_pts.size() / 3);
    for (const render::EdgeSegment& e : b.edges) {
        CHECK(e.first_pt + e.pt_count <= total_pts);
    }
}

// Axis-aligned bbox of the triangle vertices, expected to equal the requested dims.
void bbox(const render::BodyMesh& b, double lo[3], double hi[3]) {
    for (int k = 0; k < 3; ++k) {
        lo[k] = std::numeric_limits<double>::max();
        hi[k] = -std::numeric_limits<double>::max();
    }
    for (uint32_t v = 0; v < b.n_verts; ++v) {
        for (int k = 0; k < 3; ++k) {
            double c = b.pos[3ull * v + k];
            if (c < lo[k]) lo[k] = c;
            if (c > hi[k]) hi[k] = c;
        }
    }
}

}  // namespace

TEST_CASE("box tessellation round-trips through the host AICM loader") {
    const double DX = 20.0, DY = 10.0, DZ = 6.0;
    const float color[3] = {0.71f, 0.76f, 0.82f};
    Loaded ld = roundtrip(geom::make_box(DX, DY, DZ), "Body1", color);

    CHECK(ld.doc.version == 1);
    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& b = ld.doc.bodies[0];

    // Header / identity round-trip.
    CHECK(b.body_id == 1);
    CHECK(b.name == "Body1");
    CHECK(b.color[0] == doctest::Approx(0.71f));
    CHECK(b.color[1] == doctest::Approx(0.76f));
    CHECK(b.color[2] == doctest::Approx(0.82f));

    check_common(b);

    // Report the exact OCCT 7.9.3 tessellation stats for this box.
    uint32_t total_edge_pts = static_cast<uint32_t>(b.edge_pts.size() / 3);
    MESSAGE("box stats: n_verts=" << b.n_verts << " n_tris=" << b.n_tris
            << " faces=" << b.faces.size() << " edges=" << b.edges.size()
            << " brep_verts=" << b.brep_verts.size()
            << " total_edge_pts=" << total_edge_pts);

    // A box: 6 planar faces, 12 edges, 8 B-rep vertices, >= 12 tris.
    CHECK(b.faces.size() == 6);
    for (const render::FaceRange& f : b.faces) CHECK(f.surf_type == 0);  // plane
    CHECK(b.edges.size() == 12);
    for (const render::EdgeSegment& e : b.edges) CHECK(e.pt_count >= 2);
    CHECK(b.brep_verts.size() == 8);
    CHECK(b.n_tris >= 12);

    // Watertight bbox equals the requested dims (min corner at origin).
    double lo[3], hi[3];
    bbox(b, lo, hi);
    CHECK(lo[0] == doctest::Approx(0.0).epsilon(1e-4));
    CHECK(lo[1] == doctest::Approx(0.0).epsilon(1e-4));
    CHECK(lo[2] == doctest::Approx(0.0).epsilon(1e-4));
    CHECK((hi[0] - lo[0]) == doctest::Approx(DX).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(DY).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(DZ).epsilon(1e-4));

    // Every per-vertex normal is unit length.
    for (uint32_t v = 0; v < b.n_verts; ++v) {
        double nx = b.nrm[3ull * v + 0];
        double ny = b.nrm[3ull * v + 1];
        double nz = b.nrm[3ull * v + 2];
        CHECK(std::sqrt(nx * nx + ny * ny + nz * nz) == doctest::Approx(1.0).epsilon(1e-3));
    }
}

TEST_CASE("extruded rectangle round-trips through the host AICM loader") {
    const double W = 12.0, H = 8.0, D = 5.0;
    const float color[3] = {0.4f, 0.5f, 0.6f};
    Loaded ld = roundtrip(geom::extrude_rect(W, H, D), "Extrude1", color);

    REQUIRE(ld.doc.bodies.size() == 1);
    const render::BodyMesh& b = ld.doc.bodies[0];

    CHECK(b.name == "Extrude1");
    check_common(b);

    // A prism of a rectangle is a box: 6 faces, 12 edges, 8 vertices.
    CHECK(b.faces.size() == 6);
    for (const render::FaceRange& f : b.faces) CHECK(f.surf_type == 0);  // plane
    CHECK(b.edges.size() == 12);
    CHECK(b.brep_verts.size() == 8);
    CHECK(b.n_tris >= 12);

    double lo[3], hi[3];
    bbox(b, lo, hi);
    CHECK((hi[0] - lo[0]) == doctest::Approx(W).epsilon(1e-4));
    CHECK((hi[1] - lo[1]) == doctest::Approx(H).epsilon(1e-4));
    CHECK((hi[2] - lo[2]) == doctest::Approx(D).epsilon(1e-4));
}

TEST_CASE("write_aicm is deterministic (byte-identical on repeat)") {
    const float color[3] = {0.71f, 0.76f, 0.82f};
    aicm::AicmBody body =
        geom::tessellate_body(geom::make_box(20.0, 10.0, 6.0), 1, "Body1", color);
    std::vector<aicm::AicmBody> bodies{body};

    std::vector<uint8_t> a = aicm::write_aicm(bodies);
    std::vector<uint8_t> c = aicm::write_aicm(bodies);
    CHECK(a == c);
    CHECK(!a.empty());
}

namespace {

struct MeshCounts {
    long faces = 0, unmeshed = 0, nodes = 0, tris = 0;
    double area = 0.0;
};

MeshCounts mesh_counts(const TopoDS_Shape& shape) {
    MeshCounts c;
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
        ++c.faces;
        TopLoc_Location loc;
        const Handle(Poly_Triangulation) tri =
            BRep_Tool::Triangulation(TopoDS::Face(ex.Current()), loc);
        if (tri.IsNull()) { ++c.unmeshed; continue; }
        c.nodes += tri->NbNodes();
        c.tris += tri->NbTriangles();
        for (int i = 1; i <= tri->NbTriangles(); ++i) {
            int n1, n2, n3;
            tri->Triangle(i).Get(n1, n2, n3);
            c.area += 0.5 * gp_Vec(tri->Node(n1), tri->Node(n2))
                               .Crossed(gp_Vec(tri->Node(n1), tri->Node(n3))).Magnitude();
        }
    }
    return c;
}

// Largest distance between the nodes two faces hold for one shared edge; `broken`
// counts the edges whose faces disagree on the node count or hold no polygon at all.
double shared_edge_gap(const TopoDS_Shape& shape, int& broken) {
    TopTools_IndexedDataMapOfShapeListOfShape edge_faces;
    TopExp::MapShapesAndAncestors(shape, TopAbs_EDGE, TopAbs_FACE, edge_faces);
    double gap = 0.0;
    broken = 0;
    for (int i = 1; i <= edge_faces.Extent(); ++i) {
        const TopoDS_Edge edge = TopoDS::Edge(edge_faces.FindKey(i));
        if (BRep_Tool::Degenerated(edge)) continue;
        std::vector<std::vector<gp_Pnt>> sides;
        for (TopTools_ListIteratorOfListOfShape it(edge_faces.FindFromIndex(i)); it.More(); it.Next()) {
            TopLoc_Location loc;
            const Handle(Poly_Triangulation) tri =
                BRep_Tool::Triangulation(TopoDS::Face(it.Value()), loc);
            const Handle(Poly_PolygonOnTriangulation) poly =
                tri.IsNull() ? Handle(Poly_PolygonOnTriangulation)()
                             : BRep_Tool::PolygonOnTriangulation(edge, tri, loc);
            if (poly.IsNull()) { ++broken; continue; }
            std::vector<gp_Pnt> pts;
            for (int k = 1; k <= poly->NbNodes(); ++k)
                pts.push_back(tri->Node(poly->Node(k)).Transformed(loc.Transformation()));
            sides.push_back(std::move(pts));
        }
        for (std::size_t s = 1; s < sides.size(); ++s) {
            if (sides[s].size() != sides[0].size()) { ++broken; continue; }
            for (std::size_t k = 0; k < sides[s].size(); ++k)
                gap = std::max(gap, sides[s][k].Distance(sides[0][k]));
        }
    }
    return gap;
}

// `count` through holes of diameter `dia` on a pitch circle, as one cutting tool.
TopoDS_Shape hole_ring(double pcd, int count, double dia) {
    BRep_Builder builder;
    TopoDS_Compound tools;
    builder.MakeCompound(tools);
    const double pi = std::acos(-1.0);
    for (int k = 0; k < count; ++k) {
        const double a = 2.0 * pi * k / count;
        const gp_Pnt at(100.0 + 0.5 * pcd * std::cos(a), 100.0 + 0.5 * pcd * std::sin(a), -5.0);
        builder.Add(tools, BRepPrimAPI_MakeCylinder(gp_Ax2(at, gp::DZ()), 0.5 * dia, 30.0).Shape());
    }
    return tools;
}

}  // namespace

// A plate face bounded by a few thousand nodes takes the planar fast path. It must be
// indistinguishable from BRepMesh's own result in everything but triangle connectivity:
// same nodes, same triangle count, same area, and edges shared with the hole walls
// carrying the same nodes on both sides.
TEST_CASE("a perforated plate meshes like BRepMesh, and an edit re-meshes only what changed") {
    TopoDS_Shape plate = geom::make_box(200.0, 200.0, 20.0);
    for (int ring = 0; ring < 4; ++ring) {
        plate = BRepAlgoAPI_Cut(plate, hole_ring(40.0 + 30.0 * ring, 10 + 4 * ring, 4.0)).Shape();
        // Mesh after every cut, as each edit of a document does.
        const geom::DisplayDeflections d =
            geom::display_deflections(geom::DisplayQuality::Fine, plate);
        geom::mesh_body(plate, d.lin, d.ang);
    }
    const geom::DisplayDeflections d = geom::display_deflections(geom::DisplayQuality::Fine, plate);

    TopoDS_Shape reference = BRepBuilderAPI_Copy(plate, Standard_True, Standard_False).Shape();
    IMeshTools_Parameters params;
    params.Deflection = d.lin;
    params.Angle = d.ang;
    BRepMesh_IncrementalMesh mesher(reference, params);

    const MeshCounts got = mesh_counts(plate), want = mesh_counts(reference);
    CHECK(got.faces == 6 + 64);
    CHECK(got.unmeshed == 0);
    CHECK(got.nodes == want.nodes);
    CHECK(got.tris == want.tris);
    CHECK(got.area == doctest::Approx(want.area).epsilon(1e-9));
    int broken = 0;
    CHECK(shared_edge_gap(plate, broken) < 1e-9);
    CHECK(broken == 0);

    // The top and bottom faces did take the fast path: only it stores normals.
    int with_normals = 0;
    std::vector<Handle(Poly_Triangulation)> before;
    for (TopExp_Explorer ex(plate, TopAbs_FACE); ex.More(); ex.Next()) {
        TopLoc_Location loc;
        before.push_back(BRep_Tool::Triangulation(TopoDS::Face(ex.Current()), loc));
        if (before.back()->HasNormals()) ++with_normals;
    }
    CHECK(with_normals == 2);

    // Meshing the same body again keeps every triangulation.
    geom::mesh_body(plate, d.lin, d.ang);
    std::size_t at = 0;
    for (TopExp_Explorer ex(plate, TopAbs_FACE); ex.More(); ex.Next(), ++at) {
        TopLoc_Location loc;
        CHECK(BRep_Tool::Triangulation(TopoDS::Face(ex.Current()), loc) == before[at]);
    }

    // One more ring: the hole walls of the earlier rings keep theirs.
    TopoDS_Shape edited = BRepAlgoAPI_Cut(plate, hole_ring(160.0, 26, 4.0)).Shape();
    const geom::DisplayDeflections de =
        geom::display_deflections(geom::DisplayQuality::Fine, edited);
    geom::mesh_body(edited, de.lin, de.ang);
    int kept = 0;
    for (TopExp_Explorer ex(edited, TopAbs_FACE); ex.More(); ex.Next()) {
        TopLoc_Location loc;
        const Handle(Poly_Triangulation) tri =
            BRep_Tool::Triangulation(TopoDS::Face(ex.Current()), loc);
        REQUIRE(!tri.IsNull());
        for (const Handle(Poly_Triangulation)& old : before)
            if (old == tri) { ++kept; break; }
    }
    CHECK(kept >= 64);
    CHECK(shared_edge_gap(edited, broken) < 1e-9);
    CHECK(broken == 0);

    // The display packet built over those faces is one the host loader accepts.
    const float color[3] = {0.71f, 0.76f, 0.82f};
    Loaded ld = roundtrip(edited, "Plate", color);
    REQUIRE(ld.doc.bodies.size() == 1);
    check_common(ld.doc.bodies[0]);
    CHECK(ld.doc.bodies[0].faces.size() == 6 + 64 + 26);
}