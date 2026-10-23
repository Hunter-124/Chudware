// Format-registry parity tests (engine_oracle/doc cutover).
//
// The doc_formats RPC payload is a byte-parity port of the Python oracle's
// engine/doc/formats.py::list_formats (FORMAT_REGISTRY). These tests pin the
// C++ payload against the EXACT oracle dump (captured 2026-07-22 via
// `python3 -c "import json; from engine.doc import formats;
//  print(json.dumps(formats.list_formats()))"`) and lock the detect_format
// extension map to the registry. If formats.py's registry changes, regenerate
// the oracle dump below.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cctype>
#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <map>
#include <random>
#include <utility>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/formats.h"
#include "doc/session.h"
#include <BRepAdaptor_Curve.hxx>
#include <BRepAdaptor_Surface.hxx>
#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepFilletAPI_MakeFillet.hxx>
#include <BRepGProp.hxx>
#include <BRepMesh_IncrementalMesh.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRepPrimAPI_MakePrism.hxx>
#include <BRepPrimAPI_MakeSphere.hxx>
#include <BRepTools.hxx>
#include <BRep_Tool.hxx>
#include <GC_MakeArcOfCircle.hxx>
#include <GProp_GProps.hxx>
#include <Poly_PolygonOnTriangulation.hxx>
#include <Poly_Triangulation.hxx>
#include <ShapeUpgrade_UnifySameDomain.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopTools_IndexedDataMapOfShapeListOfShape.hxx>
#include <TopoDS.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

using json = nlohmann::json;

// Oracle dump of engine/doc/formats.py::list_formats() (semantic compare — the
// oracle serializes keys in insertion order, the C++ port in sorted order; the
// envelope content is identical).
static const char* kOracleFormats = R"JSON(
[{"fmt": "step", "label": "STEP", "exts": [".step", ".stp"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "iges", "label": "IGES", "exts": [".iges", ".igs"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "brep", "label": "BREP (OpenCASCADE)", "exts": [".brep"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "stl", "label": "STL", "exts": [".stl"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "3mf", "label": "3MF", "exts": [".3mf"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "obj", "label": "Wavefront OBJ", "exts": [".obj"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "ply", "label": "Stanford PLY", "exts": [".ply"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "gltf", "label": "glTF", "exts": [".gltf"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "glb", "label": "glTF binary", "exts": [".glb"], "lane": "mesh", "can_import": true, "can_export": true}, {"fmt": "3dm", "label": "Rhino", "exts": [".3dm"], "lane": "rhino", "can_import": true, "can_export": true}, {"fmt": "dxf", "label": "AutoCAD DXF", "exts": [".dxf"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "dwg", "label": "AutoCAD DWG", "exts": [".dwg"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "sat", "label": "ACIS SAT", "exts": [".sat", ".sab"], "lane": "brep", "can_import": true, "can_export": true}, {"fmt": "sldprt", "label": "SolidWorks part", "exts": [".sldprt"], "lane": "proprietary", "can_import": true, "can_export": false}, {"fmt": "sldasm", "label": "SolidWorks assembly", "exts": [".sldasm"], "lane": "proprietary", "can_import": true, "can_export": false}, {"fmt": "x_t", "label": "Parasolid (text)", "exts": [".x_t"], "lane": "proprietary", "can_import": true, "can_export": false}, {"fmt": "x_b", "label": "Parasolid (binary)", "exts": [".x_b"], "lane": "proprietary", "can_import": true, "can_export": false}]
)JSON";

TEST_CASE("doc_formats payload matches the Python oracle byte-for-byte (semantic)") {
    const json payload = chudware::doc::doc_formats();
    REQUIRE(payload.is_object());
    REQUIRE(payload.contains("formats"));
    // nlohmann object equality is key-order-insensitive; array order IS compared,
    // so registry order must match the oracle exactly.
    CHECK(payload["formats"] == json::parse(kOracleFormats));
}

TEST_CASE("format_registry exposes the full matrix in oracle order") {
    const auto& reg = chudware::doc::format_registry();
    const json oracle = json::parse(kOracleFormats);
    REQUIRE(reg.size() == oracle.size());
    for (std::size_t i = 0; i < reg.size(); ++i) {
        CAPTURE(i);
        CHECK(reg[i].fmt == oracle[i]["fmt"].get<std::string>());
        CHECK(reg[i].label == oracle[i]["label"].get<std::string>());
        CHECK(reg[i].exts == oracle[i]["exts"].get<std::vector<std::string>>());
        CHECK(reg[i].lane == oracle[i]["lane"].get<std::string>());
        CHECK(reg[i].can_import == oracle[i]["can_import"].get<bool>());
        CHECK(reg[i].can_export == oracle[i]["can_export"].get<bool>());
    }
}

TEST_CASE("detect_format resolves every registry extension to its fmt") {
    // The extension map must be a projection of the registry (formats.py::
    // _EXT_TO_FORMAT is derived from the same registry — drift here means the
    // host offers a format the sniffer then rejects).
    for (const auto& fi : chudware::doc::format_registry()) {
        for (const std::string& ext : fi.exts) {
            CAPTURE(ext);
            CHECK(chudware::doc::detect_format("some/path/Body" + ext) == fi.fmt);
            // Case-insensitive, like formats.py::_detect_format (suffix.lower()).
            std::string upper = ext;
            for (char& c : upper) c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
            CHECK(chudware::doc::detect_format("BODY" + upper) == fi.fmt);
        }
    }
}

TEST_CASE("detect_format rejects an unknown extension like the oracle") {
    bool threw = false;
    try {
        (void)chudware::doc::detect_format("model.xyz");
    } catch (const chudware::doc::OpError& e) {
        threw = true;
        CHECK(e.code == "invalid_op");
    }
    CHECK(threw);
}

namespace {
using StlPoint = std::array<double, 3>;
using StlFacet = std::array<StlPoint, 3>;

struct StlFile {
    std::filesystem::path path = std::filesystem::temp_directory_path() /
        ("chudware_stl_regression_" + std::to_string(std::random_device{}()) + ".stl");
    ~StlFile() { std::error_code ec; std::filesystem::remove(path, ec); }
};

std::uint32_t little_u32(const unsigned char* p) {
    return std::uint32_t(p[0]) | (std::uint32_t(p[1]) << 8) |
           (std::uint32_t(p[2]) << 16) | (std::uint32_t(p[3]) << 24);
}

// Read the consumer-visible facets, without OCCT's reader, welding, or healing.
std::vector<StlFacet> raw_stl(const std::filesystem::path& path, bool ascii) {
    std::ifstream stream(path, std::ios::binary);
    REQUIRE(stream.good());
    std::vector<StlFacet> facets;
    if (ascii) {
        std::string token;
        StlFacet facet{};
        int corner = 0;
        stream >> token;
        REQUIRE(token == "solid");
        while (stream >> token) {
            if (token != "vertex") continue;
            REQUIRE(static_cast<bool>(stream >> facet[corner][0] >> facet[corner][1] >> facet[corner][2]));
            if (++corner == 3) { facets.push_back(facet); corner = 0; }
        }
        REQUIRE(corner == 0);
    } else {
        unsigned char header[84];
        stream.read(reinterpret_cast<char*>(header), sizeof(header));
        REQUIRE(stream.gcount() == sizeof(header));
        const auto count = little_u32(header + 80);
        REQUIRE(std::filesystem::file_size(path) == 84 + std::uint64_t(count) * 50);
        facets.resize(count);
        for (auto& facet : facets) {
            unsigned char record[50];
            stream.read(reinterpret_cast<char*>(record), sizeof(record));
            REQUIRE(stream.gcount() == sizeof(record));
            for (int k = 0; k < 3; ++k) for (int axis = 0; axis < 3; ++axis) {
                const auto bits = little_u32(record + 12 + 12 * k + 4 * axis);
                float coordinate;
                std::memcpy(&coordinate, &bits, sizeof(coordinate));
                facet[k][axis] = coordinate;
            }
        }
    }
    REQUIRE_FALSE(facets.empty());
    return facets;
}

double raw_closed_volume(const std::vector<StlFacet>& facets) {
    std::map<StlPoint, std::size_t> vertices;
    struct Uses { int count = 0, direction = 0; std::size_t face = 0; };
    std::map<std::pair<std::size_t, std::size_t>, Uses> edges;
    std::vector<std::vector<std::size_t>> neighbours(facets.size());
    double volume = 0.0;
    for (std::size_t t = 0; t < facets.size(); ++t) {
        const auto& f = facets[t];
        gp_Pnt p[3];
        std::size_t id[3];
        for (int k = 0; k < 3; ++k) {
            for (double coordinate : f[k]) REQUIRE(std::isfinite(coordinate));
            p[k].SetCoord(f[k][0], f[k][1], f[k][2]);
            id[k] = vertices.emplace(f[k], vertices.size()).first->second;
        }
        REQUIRE(gp_Vec(p[0], p[1]).Crossed(gp_Vec(p[0], p[2])).SquareMagnitude() > 0.0);
        volume += gp_Vec(p[0].XYZ()).Dot(gp_Vec(p[1].XYZ()).Crossed(gp_Vec(p[2].XYZ()))) / 6.0;
        for (int k = 0; k < 3; ++k) {
            const auto a = id[k], b = id[(k + 1) % 3];
            REQUIRE(a != b);
            auto& use = edges[{std::min(a, b), std::max(a, b)}];
            if (use.count++ == 0) use.face = t;
            else { neighbours[t].push_back(use.face); neighbours[use.face].push_back(t); }
            use.direction += a < b ? 1 : -1;
        }
    }
    for (const auto& edge : edges) {
        CHECK(edge.second.count == 2);
        CHECK(edge.second.direction == 0);
    }
    std::vector<bool> seen(facets.size(), false);
    std::vector<std::size_t> pending{0};
    seen[0] = true;
    std::size_t reached = 0;
    while (!pending.empty()) {
        const auto t = pending.back(); pending.pop_back(); ++reached;
        for (auto n : neighbours[t])
            if (!seen[n]) { seen[n] = true; pending.push_back(n); }
    }
    CHECK(reached == facets.size());
    CHECK(volume > 0.0);
    return volume;
}

double source_volume(const TopoDS_Shape& shape) {
    GProp_GProps properties;
    BRepGProp::VolumeProperties(shape, properties);
    return properties.Mass();
}

TopoDS_Shape tangent_crescent() {
    // Two quarter-circle boundaries tangent at (0,3,0), with radii 3 and 1.
    // Equal angular subdivision overlaps their first chords. This is a reduced
    // native cusp, not a baked mesh or a dependency on a user's model.
    const double q = std::sqrt(0.5);
    const gp_Pnt outside(-3, 0, 0), apex(0, 3, 0), inside(-1, 2, 0);
    const auto outer = BRepBuilderAPI_MakeEdge(
        GC_MakeArcOfCircle(outside, gp_Pnt(-3*q, 3*q, 0), apex).Value()).Edge();
    const auto inner = BRepBuilderAPI_MakeEdge(
        GC_MakeArcOfCircle(apex, gp_Pnt(-q, 2+q, 0), inside).Value()).Edge();
    const auto bottom = BRepBuilderAPI_MakeEdge(inside, outside).Edge();
    const auto wire = BRepBuilderAPI_MakeWire(outer, inner, bottom).Wire();
    const TopoDS_Shape solid = BRepPrimAPI_MakePrism(
        BRepBuilderAPI_MakeFace(wire).Face(), gp_Vec(0, 0, 2)).Shape();
    // Prism caps are located instances of the same native face TShape. Keep
    // that sharing in the fixture: cusp refinement must synchronize their edge
    // parameter samples, not just each edge's incident faces.
    std::vector<TopoDS_Shape> faces;
    bool has_located_partner = false;
    for (TopExp_Explorer ex(solid, TopAbs_FACE); ex.More(); ex.Next()) {
        for (const auto& previous : faces)
            has_located_partner |= previous.IsPartner(ex.Current()) && !previous.IsSame(ex.Current());
        faces.push_back(ex.Current());
    }
    REQUIRE(has_located_partner);
    return solid;
}

TopoDS_Edge edge_with_midpoint(const TopoDS_Shape& shape, const gp_Pnt& midpoint) {
    for (TopExp_Explorer ex(shape, TopAbs_EDGE); ex.More(); ex.Next()) {
        const BRepAdaptor_Curve curve(TopoDS::Edge(ex.Current()));
        if (curve.Value(0.5 * (curve.FirstParameter() + curve.LastParameter()))
                .Distance(midpoint) < 1e-6)
            return TopoDS::Edge(ex.Current());
    }
    FAIL("fixture edge not found");
    return {};
}

TopoDS_Shape vanishing_corner_fillet() {
    // An inside corner whose floor fillets (R2 along one wall, R3 along the other)
    // are both tangent to the floor where they cross. Filleting their crossing
    // edge gives a native rational B-spline face that vanishes to a pole on the
    // floor, reduced from the 6-axis turntable/base. Not a baked mesh.
    const auto box = [](gp_Pnt a, gp_Pnt b) { return BRepPrimAPI_MakeBox(a, b).Shape(); };
    const TopoDS_Shape fused = BRepAlgoAPI_Fuse(
        BRepAlgoAPI_Fuse(box(gp_Pnt(0, -40, -10), gp_Pnt(40, 0, 0)),
                         box(gp_Pnt(0, -40, 0), gp_Pnt(14.5, 0, 12.6))).Shape(),
        box(gp_Pnt(0, -21.6, 0), gp_Pnt(40, 0, 12.6))).Shape();
    ShapeUpgrade_UnifySameDomain unify(fused);
    unify.Build();
    BRepFilletAPI_MakeFillet floor(unify.Shape());
    floor.Add(2, edge_with_midpoint(unify.Shape(), gp_Pnt(14.5, -30.8, 0)));
    floor.Add(3, edge_with_midpoint(unify.Shape(), gp_Pnt(27.25, -21.6, 0)));
    floor.Build();
    REQUIRE(floor.IsDone());
    TopTools_IndexedDataMapOfShapeListOfShape owners;
    TopExp::MapShapesAndAncestors(floor.Shape(), TopAbs_EDGE, TopAbs_FACE, owners);
    BRepFilletAPI_MakeFillet corner(floor.Shape());
    int crossings = 0;
    for (int i = 1; i <= owners.Extent(); ++i) {
        int cylinders = 0;
        for (const auto& face : owners(i))
            cylinders += BRepAdaptor_Surface(TopoDS::Face(face)).GetType() == GeomAbs_Cylinder;
        if (cylinders == 2) { corner.Add(0.5, TopoDS::Edge(owners.FindKey(i))); ++crossings; }
    }
    REQUIRE(crossings == 1);
    corner.Build();
    REQUIRE(corner.IsDone());
    return corner.Shape();
}

// Raw BRepMesh edges, with each pole's UV nodes identified, used by more than two
// facets of one face: the folded fins the STL exporter must account for.
int folded_pole_edges(const TopoDS_Shape& shape) {
    BRepTools::Clean(shape);
    BRepMesh_IncrementalMesh(shape, 0.05, Standard_False, 0.35, Standard_True);
    int folded = 0;
    for (TopExp_Explorer ex(shape, TopAbs_FACE); ex.More(); ex.Next()) {
        const TopoDS_Face face = TopoDS::Face(ex.Current());
        TopLoc_Location loc;
        const auto& tri = BRep_Tool::Triangulation(face, loc);
        std::vector<int> id(static_cast<std::size_t>(tri->NbNodes()) + 1);
        for (int i = 1; i <= tri->NbNodes(); ++i) id[i] = i;
        for (TopExp_Explorer ee(face, TopAbs_EDGE); ee.More(); ee.Next()) {
            const TopoDS_Edge edge = TopoDS::Edge(ee.Current());
            const auto& polygon = BRep_Tool::PolygonOnTriangulation(edge, tri, loc);
            if (!BRep_Tool::Degenerated(edge) || polygon.IsNull()) continue;
            for (int i = polygon->Nodes().Lower(); i <= polygon->Nodes().Upper(); ++i)
                id[polygon->Nodes()(i)] = polygon->Nodes()(polygon->Nodes().Lower());
        }
        std::map<std::pair<int, int>, int> uses;
        for (int t = 1; t <= tri->NbTriangles(); ++t) {
            int n[3];
            tri->Triangle(t).Get(n[0], n[1], n[2]);
            for (int k = 0; k < 3; ++k) {
                const int a = id[n[k]], b = id[n[(k + 1) % 3]];
                if (a != b) ++uses[{std::min(a, b), std::max(a, b)}];
            }
        }
        for (const auto& use : uses) folded += use.second > 2;
    }
    return folded;
}
}  // namespace

TEST_CASE("STL exports native tangent cusps and sphere poles as closed oriented raw facets") {
    const double pi = std::acos(-1.0);
    const std::vector<TopoDS_Shape> sources{
        BRepPrimAPI_MakeBox(gp_Pnt(2, 3, 4), 5, 6, 7).Shape(),
        tangent_crescent(),
        BRepPrimAPI_MakeSphere(gp_Pnt(10, 10, 10), 2, 0, pi/2, pi/2).Shape()};
    for (std::size_t index = 0; index < sources.size(); ++index) {
        CAPTURE(index);
        const auto& shape = sources[index];
        REQUIRE(BRepCheck_Analyzer(shape).IsValid());
        const double expected = source_volume(shape);
        REQUIRE(expected > 0);
        for (bool ascii : {false, true}) {
            CAPTURE(ascii);
            StlFile output;
            chudware::doc::TessOptions tess;
            tess.ascii = ascii;
            chudware::doc::export_bodies({shape}, output.path.string(), "stl", tess);
            const double actual = raw_closed_volume(raw_stl(output.path, ascii));
            CHECK(std::abs(actual - expected) / expected < 0.03);
            CHECK(source_volume(shape) == doctest::Approx(expected).epsilon(1e-12));
            CHECK(BRepCheck_Analyzer(shape).IsValid());
        }
    }
}

TEST_CASE("STL exports a vanishing fillet whose pole BRepMesh folds as closed oriented raw facets") {
    const auto shape = vanishing_corner_fillet();
    REQUIRE(BRepCheck_Analyzer(shape).IsValid());
    // The fixture must exercise the fold: the raw face triangulation links both
    // UV ends of the pole to one node column, so pole edges carry 4 facets.
    REQUIRE(folded_pole_edges(shape) > 0);
    const double expected = source_volume(shape);
    REQUIRE(expected > 0);
    // Python export defaults, the slice_part deflections, and a coarse setting.
    for (const auto& setting : {std::pair{0.05, 0.35}, std::pair{0.02, 0.1}, std::pair{0.1, 0.5}}) {
        for (bool ascii : {false, true}) {
            const double lin = setting.first, ang = setting.second;
            CAPTURE(lin);
            CAPTURE(ascii);
            StlFile output;
            chudware::doc::TessOptions tess;
            tess.lin_deflection = lin;
            tess.ang_deflection = ang;
            tess.ascii = ascii;
            chudware::doc::export_bodies({shape}, output.path.string(), "stl", tess);
            const double actual = raw_closed_volume(raw_stl(output.path, ascii));
            CHECK(std::abs(actual - expected) / expected < 0.01);
            CHECK(BRepCheck_Analyzer(shape).IsValid());
        }
    }
}

TEST_CASE("STL refuses output precision collapse rather than deleting source facets") {
    const auto box = BRepPrimAPI_MakeBox(gp_Pnt(1e8, 1e8, 1e8), 1, 1, 1).Shape();
    REQUIRE(BRepCheck_Analyzer(box).IsValid());
    StlFile output;
    try {
        chudware::doc::export_bodies({box}, output.path.string(), "stl");
        FAIL("Binary STL cannot represent this unit box at its source coordinates");
    } catch (const chudware::doc::OpError& error) {
        CHECK(error.code == "export_failed");
    }
    CHECK_FALSE(std::filesystem::exists(output.path));
}

TEST_CASE("STL continues to export ordinary open surface bodies") {
    const auto wire = BRepBuilderAPI_MakeWire(
        BRepBuilderAPI_MakeEdge(gp_Pnt(0,0,0), gp_Pnt(4,0,0)).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(4,0,0), gp_Pnt(4,3,0)).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(4,3,0), gp_Pnt(0,3,0)).Edge(),
        BRepBuilderAPI_MakeEdge(gp_Pnt(0,3,0), gp_Pnt(0,0,0)).Edge()).Wire();
    const auto sheet = BRepBuilderAPI_MakeFace(wire).Face();
    for (bool ascii : {false, true}) {
        StlFile output;
        chudware::doc::TessOptions tess;
        tess.ascii = ascii;
        chudware::doc::export_bodies({sheet}, output.path.string(), "stl", tess);
        CHECK(raw_stl(output.path, ascii).size() == 2);
    }
}
