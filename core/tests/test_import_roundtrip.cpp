// Import / save round-trip suite — locks the round-3 file-plumbing fixes:
//   N-8: a saved .step+ BAKES imported geometry in, so it survives the source file being
//        deleted (the "assembly reopened as just the plate" P0).
//   N-5: a multi-solid STEP imports as SEPARATE bodies, not one merged compound.
//   N-4: doc_import_file accepts the native .step+ extension (reopens it as a doc).
//   manual import: doc_open on a NON-.step+ file imports it as a fresh part doc (the
//        "nothing happens when I open a .step" bug).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>
#include <algorithm>
#include <array>
#include <map>

#include <filesystem>
#include <string>
#include <fstream>
#include <cstdint>
#include <cmath>
#include <cstring>
#include <cstdlib>
#ifndef _WIN32
#include <sys/stat.h>
#endif
#include <vector>
#include <miniz.h>

#include <BRepAlgoAPI_Cut.hxx>
#include <BRepBuilderAPI_MakeEdge.hxx>
#include <BRepBuilderAPI_MakeFace.hxx>
#include <BRepBuilderAPI_MakeVertex.hxx>
#include <BRepBuilderAPI_MakeWire.hxx>
#include <BRepCheck_Analyzer.hxx>
#include <BRepBndLib.hxx>
#include <BRepGProp.hxx>
#include <Bnd_Box.hxx>
#include <gp_Ax2.hxx>
#include <gp_Circ.hxx>
#include <BRepPrimAPI_MakeCylinder.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRep_Builder.hxx>
#include <BRep_Tool.hxx>
#include <GProp_GProps.hxx>
#include <IGESControl_Writer.hxx>
#include <Poly_Triangulation.hxx>
#include <TopExp.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
#include <TopTools_IndexedMapOfShape.hxx>
#include <TopTools_MapOfShape.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Compound.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <TopoDS_Shell.hxx>
#include <gp_Dir.hxx>
#include <TopoDS_Vertex.hxx>
#include <gp_Pnt.hxx>
#include <gp_Pln.hxx>
#include <gp_Vec.hxx>

#include "doc/dwg.h"
#include "doc/formats.h"
#include "doc/session.h"
#ifdef CHUDWARE_WITH_POLYMESH
#include "sim/mesher.h"
#endif

using nlohmann::json;
namespace fs = std::filesystem;
constexpr double kPiTest = 3.14159265358979323846;

namespace {
double vol(const TopoDS_Shape& s) {
    GProp_GProps g;
    BRepGProp::VolumeProperties(s, g);
    return g.Mass();
}
int body_count(const json& resp) {
    if (resp.contains("tess") && resp["tess"].contains("bodies") &&
        resp["tess"]["bodies"].is_array())
        return static_cast<int>(resp["tess"]["bodies"].size());
    return -1;
}
fs::path tmp(const std::string& name) {
    return fs::temp_directory_path() / ("chudware_imp_" + name);
}

TopoDS_Compound compound(const std::vector<TopoDS_Shape>& shapes) {
    BRep_Builder builder;
    TopoDS_Compound result;
    builder.MakeCompound(result);
    for (const auto& shape : shapes) builder.Add(result, shape);
    return result;
}

void check_unique_topology(
    const std::vector<std::pair<std::string, TopoDS_Shape>>& bodies,
    TopAbs_ShapeEnum kind, int expected) {
    TopTools_MapOfShape seen;
    int count = 0;
    for (const auto& body : bodies) {
        TopTools_IndexedMapOfShape members;
        TopExp::MapShapes(body.second, kind, members);
        count += members.Extent();
        for (int i = 1; i <= members.Extent(); ++i)
            CHECK(seen.Add(members(i)));
    }
    CHECK(count == expected);
}

void append_u32(std::string& out, std::uint32_t value) {
    for (int i = 0; i < 4; ++i)
        out.push_back(static_cast<char>((value >> (8 * i)) & 0xffu));
}
void append_f32(std::string& out, float value) {
    std::uint32_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    append_u32(out, bits);
}
std::string raw_deflate(const std::string& input) {
    mz_stream stream{};
    REQUIRE(mz_deflateInit2(&stream, MZ_BEST_COMPRESSION, MZ_DEFLATED,
                            -MZ_DEFAULT_WINDOW_BITS, 8, MZ_DEFAULT_STRATEGY) == MZ_OK);
    std::string out(mz_compressBound(static_cast<mz_ulong>(input.size())), '\0');
    stream.next_in = reinterpret_cast<const unsigned char*>(input.data());
    stream.avail_in = static_cast<unsigned>(input.size());
    stream.next_out = reinterpret_cast<unsigned char*>(out.data());
    stream.avail_out = static_cast<unsigned>(out.size());
    const int result = mz_deflate(&stream, MZ_FINISH);
    const std::size_t size = stream.total_out;
    mz_deflateEnd(&stream);
    REQUIRE(result == MZ_STREAM_END);
    out.resize(size);
    return out;
}
void append_face(std::string& out, const std::array<std::array<float, 3>, 4>& points) {
    // One quad triangle strip of length four.
    for (std::uint32_t v : {4u, 8u, 2u, 1u, 4u}) append_u32(out, v);
    const auto append_array = [&](const auto& values) {
        for (std::uint32_t v : {12u, 100u, 2u, 4u}) append_u32(out, v);
        for (const auto& point : values)
            for (float coordinate : point) append_f32(out, coordinate);
    };
    append_array(points);
    const std::array<std::array<float, 3>, 4> normals = {{
        {{0.0f, 0.0f, 1.0f}}, {{0.0f, 0.0f, 1.0f}},
        {{0.0f, 0.0f, 1.0f}}, {{0.0f, 0.0f, 1.0f}}}};
    append_array(normals);
    for (int unused = 0; unused < 2; ++unused)
        for (std::uint32_t v : {12u, 100u, 2u, 0u}) append_u32(out, v);
}
std::string box_display_list() {
    constexpr float s = 0.01f;  // SolidWorks display coordinates are metres: 10 mm.
    const std::array<float, 3> p000 = {0, 0, 0};
    const std::array<float, 3> p100 = {s, 0, 0};
    const std::array<float, 3> p010 = {0, s, 0};
    const std::array<float, 3> p110 = {s, s, 0};
    const std::array<float, 3> p001 = {0, 0, s};
    const std::array<float, 3> p101 = {s, 0, s};
    const std::array<float, 3> p011 = {0, s, s};
    const std::array<float, 3> p111 = {s, s, s};
    std::string out;
    append_face(out, {p000, p010, p100, p110});  // bottom (-Z)
    append_face(out, {p001, p101, p011, p111});  // top (+Z)
    append_face(out, {p000, p100, p001, p101});  // front (-Y)
    append_face(out, {p010, p011, p110, p111});  // back (+Y)
    append_face(out, {p000, p001, p010, p011});  // left (-X)
    append_face(out, {p100, p110, p101, p111});  // right (+X)
    return out;
}
std::string assembly_face_block() {
    std::string out;
    append_u32(out, 0);                  // block ordinal
    out.append("BOX--ID-", 8);           // persistent model id
    append_u32(out, 6);                  // face count
    append_u32(out, 0);
    append_u32(out, 0);
    append_u32(out, 1);
    append_u32(out, 0);
    append_u32(out, 0);
    append_u32(out, 0x00201896u);        // saved-display format stamp
    for (int i = 0; i < 4; ++i) append_u32(out, 0);
    REQUIRE(out.size() == 56);
    out += box_display_list();
    return out;
}
void append_modern_chunk(std::string& file, const std::string& name, const std::string& payload) {
    const std::string compressed = raw_deflate(payload);
    append_u32(file, 0);
    file.append("\x14\x00\x06\x00\x08\x00", 6);
    file.append("\xfd\x00\x00\x00", 4);
    append_u32(file, 65536);
    append_u32(file, static_cast<std::uint32_t>(compressed.size()));
    append_u32(file, static_cast<std::uint32_t>(payload.size()));
    append_u32(file, static_cast<std::uint32_t>(name.size()));
    // Test container key is four, and ROL/ROR by four are identical.
    for (unsigned char c : name)
        file.push_back(static_cast<char>((c << 4u) | (c >> 4u)));
    file += compressed;
}
fs::path write_modern_solidworks(const std::string& name, bool assembly) {
    std::string file(15, '\0');
    file[7] = 4;
    if (assembly) {
        append_modern_chunk(file, "FaceTessellations/000-000-001", assembly_face_block());
        const std::string xml =
            "<?xml version=\"1.0\"?><swSolidWorks><swHeader>"
            "<swFile id=\"pfile\" swDocType=\"PART\"/>"
            "<swFile id=\"afile\" swDocType=\"ASSEMBLY\"/></swHeader><swModelList>"
            "<swModel id=\"part\" swName=\"Tetra\" swFileRef=\"pfile\"></swModel>"
            "<swModel id=\"root\" swName=\"Fixture\" swFileRef=\"afile\">"
            "<swReference swName=\"Tetra\" swReferenceNumber=\"1\" swModelRef=\"part\" "
            "swSuppressed=\"NO\" swTransform=\"1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1\"/>"
            "<swReference swName=\"Tetra\" swReferenceNumber=\"2\" swModelRef=\"part\" "
            "swSuppressed=\"NO\" swTransform=\"1 0 0 0 0 1 0 0 0 0 1 0 0.02 0 0 1\"/>"
            "</swModel></swModelList><swConfiguration swName=\"Default\" swModelRef=\"root\" "
            "swMostRecentConfiguration=\"YES\"/></swSolidWorks>";
        append_modern_chunk(file, "swXmlContents/COMPINSTANCETREE", xml);
    } else {
        append_modern_chunk(file, "Contents/DisplayLists", box_display_list());
    }
    const fs::path path = tmp(name + (assembly ? ".SLDASM" : ".SLDPRT"));
    std::ofstream(path, std::ios::binary | std::ios::trunc).write(file.data(), file.size());
    return path;
}
}  // namespace

#ifndef _WIN32
struct PathGuard {
    std::string old = std::getenv("PATH") ? std::getenv("PATH") : "";
    explicit PathGuard(const fs::path& prepend) {
        const std::string value = prepend.string() + ":" + old;
        ::setenv("PATH", value.c_str(), 1);
    }
    ~PathGuard() { ::setenv("PATH", old.c_str(), 1); }
};
#endif

// --- N-8 mechanism: the BREP base64 round-trip is lossless (volume preserved). --------
TEST_CASE("shape <-> brep base64 round-trips exactly") {
    TopoDS_Shape box = BRepPrimAPI_MakeBox(10.0, 20.0, 30.0).Shape();
    std::string b64 = chudware::doc::shape_to_brep_b64(box);
    REQUIRE_FALSE(b64.empty());
    TopoDS_Shape back = chudware::doc::brep_b64_to_shape(b64);
    REQUIRE_FALSE(back.IsNull());
    CHECK(vol(back) == doctest::Approx(vol(box)).epsilon(1e-9));
}

#ifndef _WIN32
TEST_CASE("DWG converter receives hostile path literally without shell execution") {
    const fs::path dir = tmp("dwg_argv_test");
    fs::create_directories(dir);
    const fs::path converter = dir / "dwg2dxf";
    {
        std::ofstream script(converter);
        script << "#!/bin/sh\n"
                  "test \"$1\" = -y || exit 2\n"
                  "test \"$2\" = -o || exit 2\n"
                  "printf '0\\nSECTION\\n2\\nENTITIES\\n0\\nLINE\\n8\\n0\\n10\\n0\\n20\\n0\\n11\\n1\\n21\\n1\\n0\\nENDSEC\\n0\\nEOF\\n' > \"$3\"\n"
                  "printf '%s' \"$4\" > \"$3.argv\"\n";
    }
    REQUIRE(::chmod(converter.c_str(), 0700) == 0);
    PathGuard path_guard(dir);
    const fs::path marker = dir / "INJECTION_MARKER";
    const fs::path hostile = dir / "drawing;touch INJECTION_MARKER.dwg";
    std::ofstream(hostile).put('\0');

    const fs::path converted = chudware::doc::dwg_to_temp_dxf(hostile.string());
    CHECK_FALSE(fs::exists(marker));
    std::ifstream argv_file(converted.string() + ".argv");
    const std::string received((std::istreambuf_iterator<char>(argv_file)),
                               std::istreambuf_iterator<char>());
    CHECK(received == hostile.string());

    std::error_code ec;
    fs::remove(converted, ec);
    fs::remove(converted.string() + ".argv", ec);
    fs::remove_all(dir, ec);
}
#endif

namespace {
Bnd_Box bounds(const TopoDS_Shape& s) {
    Bnd_Box box;
    BRepBndLib::Add(s, box);
    return box;
}

double face_area(const TopoDS_Shape& s) {
    GProp_GProps g;
    BRepGProp::SurfaceProperties(s, g);
    return g.Mass();
}

// A 10 mm cube plus a planar r=3 disc profile, the two kinds of body DXF/DWG export writes.
std::vector<TopoDS_Shape> cube_and_disc() {
    const TopoDS_Edge rim = BRepBuilderAPI_MakeEdge(
        gp_Circ(gp_Ax2(gp_Pnt(30, 0, 0), gp_Dir(0, 0, 1)), 3.0)).Edge();
    const TopoDS_Face disc = BRepBuilderAPI_MakeFace(BRepBuilderAPI_MakeWire(rim).Wire()).Face();
    return {BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), disc};
}

// Export → import keeps the cube an exact solid (ACIS 3DSOLID: six planar faces, not a
// triangle mesh) and the disc a circular profile, each on its own layer.
void check_cube_and_disc_round_trip(const std::string& fmt) {
    const fs::path path = tmp("cube_disc." + fmt);
    chudware::doc::export_bodies(cube_and_disc(), path.string(), fmt);
    const auto bodies = chudware::doc::import_bodies(path.string(), fmt);
    REQUIRE(bodies.size() == 2);
    CHECK(bodies[0].first == "chudware_imp_cube_disc");  // profile: BODY2's circle
    CHECK(face_area(bodies[0].second) == doctest::Approx(kPiTest * 9.0).epsilon(1e-6));
    CHECK(bodies[1].first == "chudware_imp_cube_disc_solid1");
    CHECK(vol(bodies[1].second) == doctest::Approx(1000.0).epsilon(1e-9));
    int faces = 0;
    for (TopExp_Explorer ex(bodies[1].second, TopAbs_FACE); ex.More(); ex.Next()) ++faces;
    CHECK(faces == 6);
    fs::remove(path);
}
}  // namespace

TEST_CASE("DXF part export round-trips solids as ACIS and profiles as curves") {
    check_cube_and_disc_round_trip("dxf");
}

TEST_CASE("DWG part export round-trips solids as ACIS") {
    if (chudware::doc::libredwg_tool("dwg2dxf").empty()) {
        MESSAGE("LibreDWG dwg2dxf not installed (scripts/provision_libredwg.py); skipped");
        return;
    }
    check_cube_and_disc_round_trip("dwg");
}

TEST_CASE("Inch export writes inch coordinates that re-import at the same size") {
    using namespace chudware::doc;
    const fs::path path = tmp("cube_inch.sat");
    TessOptions tess;
    tess.units = parse_units("in");
    export_bodies({BRepPrimAPI_MakeBox(25.4, 50.8, 12.7).Shape()}, path.string(), "sat", tess);
    std::ifstream in(path);
    const std::string text((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
    CHECK(text.find("25.4") != std::string::npos);  // header: millimetres per unit
    int units = 0;
    const auto bodies = import_bodies(path.string(), "sat", nullptr, &units);
    CHECK(units == 1);
    REQUIRE(bodies.size() == 1);
    CHECK(vol(bodies[0].second) == doctest::Approx(25.4 * 50.8 * 12.7).epsilon(1e-9));
    const Bnd_Box box = bounds(bodies[0].second);
    double x0, y0, z0, x1, y1, z1;
    box.Get(x0, y0, z0, x1, y1, z1);
    CHECK(x1 - x0 == doctest::Approx(25.4).epsilon(1e-5));  // bbox includes vertex tolerance
    fs::remove(path);
}

TEST_CASE("DWG drawing text keeps a usable width and polylines stay closed") {
    using namespace chudware::doc;
    if (libredwg_tool("dwg2dxf").empty()) {
        MESSAGE("LibreDWG dwg2dxf not installed (scripts/provision_libredwg.py); skipped");
        return;
    }
    DxfDocument doc;
    doc.add_layer("VISIBLE", 7);
    doc.polylines.push_back({"VISIBLE", {{0, 0}, {10, 0}, {10, 5}, {0, 5}, {0, 0}}, 0.5, ""});
    doc.texts.push_back({"VISIBLE", {5.0, 2.0}, 3.5, "SCALE 1:1", 1, 2});
    const fs::path dwg = tmp("sheet_bits.dwg"), back = tmp("sheet_bits_back.dxf");
    write_dwg(dwg.string(), doc);
    REQUIRE(run_libredwg("dwg2dxf", {"-y", "-o", back.string(), dwg.string()}));
    // Walk the (code, value) pairs of the read-back entities.
    std::ifstream in(back);
    std::string code, value, entity;
    std::map<std::string, std::map<int, std::string>> seen;
    while (std::getline(in, code) && std::getline(in, value)) {
        auto trim = [](std::string s) {
            s.erase(0, s.find_first_not_of(" \r"));
            s.erase(s.find_last_not_of(" \r") + 1);
            return s;
        };
        const int c = std::stoi(trim(code));
        if (c == 0) entity = trim(value);
        else if (entity == "TEXT" || entity == "LWPOLYLINE") seen[entity].emplace(c, trim(value));
    }
    REQUIRE(seen.count("TEXT") == 1);
    // Width factor 41 defaults to 1 when absent (0 would draw zero-width glyphs).
    CHECK((seen["TEXT"].count(41) == 0 || std::stod(seen["TEXT"][41]) == 1.0));
    CHECK(seen["TEXT"][1] == "SCALE 1:1");
    // Centre/middle alignment keeps (5, 2) as the alignment point; the insertion point is
    // the derived left baseline, left of and below it.
    CHECK(seen["TEXT"][72] == "1");
    CHECK(seen["TEXT"][73] == "2");
    CHECK(std::stod(seen["TEXT"][11]) == doctest::Approx(5.0));
    CHECK(std::stod(seen["TEXT"][21]) == doctest::Approx(2.0));
    CHECK(std::stod(seen["TEXT"][10]) < 5.0);
    CHECK(std::stod(seen["TEXT"][20]) == doctest::Approx(2.0 - 3.5 * 0.5));
    REQUIRE(seen.count("LWPOLYLINE") == 1);
    CHECK((std::stoi(seen["LWPOLYLINE"][70]) & 1) == 1);  // closed
    CHECK(std::stod(seen["LWPOLYLINE"][43]) == doctest::Approx(0.5));
    CHECK(seen["LWPOLYLINE"][8] == "VISIBLE");
    fs::remove(dwg);
    fs::remove(back);
}

TEST_CASE("DXF import places INSERT blocks, honours OCS and keeps 3D meshes") {
    const fs::path path = tmp("blocks_ocs.dxf");
    {
        std::ofstream dxf(path);
        dxf << "0\nSECTION\n2\nBLOCKS\n"
               // Block B, base (1,0): a 2 mm line and an r=1 circle.
               "0\nBLOCK\n2\nB\n10\n1\n20\n0\n30\n0\n"
               "0\nLINE\n8\n0\n10\n1\n20\n0\n30\n0\n11\n3\n21\n0\n31\n0\n"
               "0\nCIRCLE\n8\n0\n10\n2\n20\n2\n30\n0\n40\n1\n"
               "0\nENDBLK\n0\nENDSEC\n"
               "0\nSECTION\n2\nENTITIES\n"
               // Placed at (10,0), scale 2, rotated 90 deg: line -> (10,0)-(10,4),
               // circle -> centre (6,2) r=2.
               "0\nINSERT\n8\nPARTS\n2\nB\n10\n10\n20\n0\n30\n0\n41\n2\n42\n2\n43\n2\n50\n90\n"
               // Extrusion -Z flips the OCS X axis: centre (5,0) is world (-5,0), and the
               // 0..90 deg arc runs from (-6,0) to (-5,1).
               "0\nARC\n8\n0\n10\n5\n20\n0\n30\n0\n40\n1\n50\n0\n51\n90\n210\n0\n220\n0\n230\n-1\n"
               // A closed tetrahedron of 3DFACEs on layer MESH.
               "0\n3DFACE\n8\nMESH\n10\n0\n20\n0\n30\n20\n11\n0\n21\n10\n31\n20\n12\n10\n22\n0\n32\n20\n13\n10\n23\n0\n33\n20\n"
               "0\n3DFACE\n8\nMESH\n10\n0\n20\n0\n30\n20\n11\n10\n21\n0\n31\n20\n12\n0\n22\n0\n32\n30\n13\n0\n23\n0\n33\n30\n"
               "0\n3DFACE\n8\nMESH\n10\n10\n20\n0\n30\n20\n11\n0\n21\n10\n31\n20\n12\n0\n22\n0\n32\n30\n13\n0\n23\n0\n33\n30\n"
               "0\n3DFACE\n8\nMESH\n10\n0\n20\n10\n30\n20\n11\n0\n21\n0\n31\n20\n12\n0\n22\n0\n32\n30\n13\n0\n23\n0\n33\n30\n"
               "0\n3DSOLID\n8\n0\n"
               "0\nENDSEC\n0\nEOF\n";
    }
    std::vector<std::string> warnings;
    const auto bodies = chudware::doc::import_bodies(path.string(), "dxf", &warnings);
    REQUIRE(bodies.size() == 2);
    const Bnd_Box box = bounds(bodies[0].second);
    double x0, y0, z0, x1, y1, z1;
    box.Get(x0, y0, z0, x1, y1, z1);
    CHECK(x0 == doctest::Approx(-6.0).epsilon(1e-6));
    CHECK(x1 == doctest::Approx(10.0).epsilon(1e-6));
    CHECK(y0 == doctest::Approx(0.0).epsilon(1e-6));
    CHECK(y1 == doctest::Approx(4.0).epsilon(1e-6));
    CHECK(bodies[1].first == "chudware_imp_blocks_ocs_MESH");
    CHECK(vol(bodies[1].second) == doctest::Approx(1000.0 / 6.0).epsilon(1e-6));
    // A data-less 3DSOLID is named, never silently dropped.
    REQUIRE(warnings.size() == 1);
    CHECK(warnings[0].find("3DSOLID carries no ACIS data") != std::string::npos);
    fs::remove(path);
}

// --- N-5: a two-solid STEP imports as TWO separate bodies. -----------------------------
TEST_CASE("multi-solid STEP imports as separate bodies") {
    using chudware::doc::doc_import_file;
    fs::path step = tmp("two_solids.step");
    std::vector<TopoDS_Shape> bodies = {
        BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape(),
        BRepPrimAPI_MakeBox(gp_Pnt(50, 0, 0), 10.0, 10.0, 10.0).Shape()};
    chudware::doc::export_bodies(bodies, step.string(), "step");

    json r = doc_import_file(step.string(), "", false, "");
    CHECK(body_count(r) == 2);  // the bug merged them into one compound body
    fs::remove(step);
}

// A STEP that carries construction wires beside a solid (e.g. an AutoCAD drawing exported
// with its profile lines) must open: the free wire is reference geometry, not an empty body.
TEST_CASE("STEP with free construction wires opens with every body") {
    using chudware::doc::doc_import_file;
    const fs::path step = tmp("solid_and_wire.step");
    const TopoDS_Shape wire = BRepBuilderAPI_MakeWire(
        BRepBuilderAPI_MakeEdge(gp_Pnt(40, 0, 0), gp_Pnt(60, 0, 0)).Edge()).Shape();
    chudware::doc::export_bodies({compound({BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape(), wire})},
                                 step.string(), "step");
    const json r = doc_import_file(step.string(), "", false, "");
    REQUIRE(r.contains("rebuild"));
    for (const json& st : r["rebuild"]["feature_status"])
        CHECK_MESSAGE(st.value("ok", false), st.dump());
    CHECK(r.value("volume", 0.0) == doctest::Approx(1000.0).epsilon(1e-6));
    fs::remove(step);
}

TEST_CASE("mixed STEP and BREP preserve surface bodies after stable solid ordinals") {
    const TopoDS_Shape first = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape();
    const TopoDS_Shape second =
        BRepPrimAPI_MakeBox(gp_Pnt(20, 0, 0), 2.0, 4.0, 6.0).Shape();
    const TopoDS_Face shell_face =
        BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(40, 0, 0), gp_Dir(0, 0, 1)),
                                0.0, 5.0, 0.0, 6.0).Face();
    TopoDS_Shell shell;
    BRep_Builder builder;
    builder.MakeShell(shell);
    builder.Add(shell, shell_face);
    const TopoDS_Face face =
        BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(70, 0, 0), gp_Dir(0, 0, 1)),
                                0.0, 2.0, 0.0, 3.0).Face();
    // Surfaces deliberately precede/interleave solids: existing solid body IDs
    // must not shift when the previously discarded surfaces become visible.
    const TopoDS_Compound mixed = compound({shell, first, face, second});
    for (const std::string format : {"step", "brep"}) {
        CAPTURE(format);
        const fs::path path = tmp("mixed_surfaces." + format);
        chudware::doc::export_bodies({mixed}, path.string(), format);
        const auto bodies = chudware::doc::import_bodies(path.string(), format);
        REQUIRE(bodies.size() == 4);
        for (std::size_t i = 0; i < bodies.size(); ++i)
            CHECK(bodies[i].first == "Body" + std::to_string(i + 1));
        CHECK(bodies[0].second.ShapeType() == TopAbs_SOLID);
        CHECK(bodies[1].second.ShapeType() == TopAbs_SOLID);
        CHECK(vol(bodies[0].second) == doctest::Approx(1000.0));
        CHECK(vol(bodies[1].second) == doctest::Approx(48.0));
        GProp_GProps center;
        BRepGProp::VolumeProperties(bodies[1].second, center);
        CHECK(center.CentreOfMass().X() == doctest::Approx(21.0));
        double surface_area = 0.0;
        for (std::size_t i = 2; i < bodies.size(); ++i) {
            CHECK_FALSE(TopExp_Explorer(bodies[i].second, TopAbs_SOLID).More());
            GProp_GProps area;
            BRepGProp::SurfaceProperties(bodies[i].second, area);
            surface_area += area.Mass();
        }
        CHECK(surface_area == doctest::Approx(36.0));
        check_unique_topology(bodies, TopAbs_FACE, 14);
        check_unique_topology(bodies, TopAbs_EDGE, 32);
        const json imported =
            chudware::doc::doc_import_file(path.string(), "", false, format);
        auto check_document = [](const json& response) {
            REQUIRE(body_count(response) == 4);
            const auto& rows = response.at("tess").at("bodies");
            CHECK(rows[0].at("kind") == "solid");
            CHECK(rows[1].at("kind") == "solid");
            CHECK(rows[2].at("kind") == "surface");
            CHECK(rows[3].at("kind") == "surface");
            CHECK(response.at("volume").get<double>() == doctest::Approx(1048.0));
            const json census = chudware::doc::doc_eval_model_integrity(
                response.at("doc_id").get<std::string>(), {{"include_nearest", false}});
            REQUIRE(census.at("solids").size() == 2);
            REQUIRE(census.at("non_solids").size() == 2);
            CHECK(census.at("solids")[0].at("volume_mm3").get<double>() ==
                  doctest::Approx(1000.0));
            CHECK(census.at("solids")[1].at("volume_mm3").get<double>() ==
                  doctest::Approx(48.0));
            CHECK(census.at("non_solids")[0].at("body_id") == 3);
            CHECK(census.at("non_solids")[1].at("body_id") == 4);
            CHECK_FALSE(census.at("complete").get<bool>());
        };
        check_document(imported);
        const fs::path native = tmp("mixed_surfaces_" + format + ".step+");
        chudware::doc::doc_save(imported.at("doc_id").get<std::string>(), native.string());
        fs::remove(path);
        const json reopened = chudware::doc::doc_open(native.string(), false);
        check_document(reopened);
        const json destination = chudware::doc::doc_new("part", "MixedNativeImport");
        const json reimported = chudware::doc::doc_import_file(
            native.string(), destination.at("doc_id").get<std::string>(), false, "");
        check_document(reimported);
        chudware::doc::doc_close(imported.at("doc_id").get<std::string>());
        chudware::doc::doc_close(reopened.at("doc_id").get<std::string>());
        chudware::doc::doc_close(destination.at("doc_id").get<std::string>());
        fs::remove(native);
    }
}

TEST_CASE("mixed BREP keeps free curves and points without duplicating owned topology") {
    const TopoDS_Shape solid = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape();
    const TopoDS_Face face =
        BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(40, 0, 0), gp_Dir(0, 0, 1)),
                                0.0, 5.0, 0.0, 6.0).Face();
    TopoDS_Shell shell;
    BRep_Builder builder;
    builder.MakeShell(shell);
    builder.Add(shell, face);
    const TopoDS_Shape wire = BRepBuilderAPI_MakeWire(
        BRepBuilderAPI_MakeEdge(gp_Pnt(60, 0, 0), gp_Pnt(61, 0, 0)).Edge()).Shape();
    const TopoDS_Shape edge =
        BRepBuilderAPI_MakeEdge(gp_Pnt(70, 0, 0), gp_Pnt(71, 0, 0)).Shape();
    const TopoDS_Shape vertex = BRepBuilderAPI_MakeVertex(gp_Pnt(80, 0, 0)).Shape();
    // New containers around existing members are not new authored geometry.
    TopoDS_Shell overlapping_shell;
    builder.MakeShell(overlapping_shell);
    builder.Add(overlapping_shell, TopExp_Explorer(solid, TopAbs_FACE).Current());
    builder.Add(overlapping_shell, face);
    const TopoDS_Shape overlapping_wire = BRepBuilderAPI_MakeWire(
        TopoDS::Edge(TopExp_Explorer(solid, TopAbs_EDGE).Current())).Shape();
    const TopoDS_Compound mixed = compound({
        overlapping_shell, overlapping_wire, face, wire, solid, shell, edge, vertex,
        TopExp_Explorer(solid, TopAbs_FACE).Current(),
        TopExp_Explorer(solid, TopAbs_SHELL).Current(),
        TopExp_Explorer(face, TopAbs_WIRE).Current(),
        TopExp_Explorer(wire, TopAbs_EDGE).Current(),
        TopExp_Explorer(edge, TopAbs_VERTEX).Current()});
    const fs::path path = tmp("mixed_aliases.brep");
    chudware::doc::export_bodies({mixed}, path.string(), "brep");
    const auto bodies = chudware::doc::import_bodies(path.string(), "brep");
    REQUIRE(bodies.size() == 5);
    CHECK(bodies[0].second.ShapeType() == TopAbs_SOLID);
    CHECK(bodies[1].second.ShapeType() == TopAbs_SHELL);
    CHECK(bodies[2].second.ShapeType() == TopAbs_WIRE);
    CHECK(bodies[3].second.ShapeType() == TopAbs_EDGE);
    CHECK(bodies[4].second.ShapeType() == TopAbs_VERTEX);
    check_unique_topology(bodies, TopAbs_FACE, 7);
    check_unique_topology(bodies, TopAbs_EDGE, 18);
    check_unique_topology(bodies, TopAbs_VERTEX, 17);
    fs::remove(path);

    const fs::path surface_path = tmp("surfaces_only.brep");
    chudware::doc::export_bodies({compound({shell, wire, edge, vertex})},
                                surface_path.string(), "brep");
    const auto surfaces =
        chudware::doc::import_bodies(surface_path.string(), "brep");
    REQUIRE(surfaces.size() == 1);
    CHECK(surfaces.front().second.ShapeType() == TopAbs_COMPOUND);
    CHECK_FALSE(TopExp_Explorer(surfaces.front().second, TopAbs_SOLID).More());
    check_unique_topology(surfaces, TopAbs_FACE, 1);
    check_unique_topology(surfaces, TopAbs_EDGE, 6);
    fs::remove(surface_path);
}

TEST_CASE("IGES shell promotion retains every closed shell and detached surface") {
    const fs::path path = tmp("multiple_shells.iges");
    const TopoDS_Shape first = BRepPrimAPI_MakeBox(10.0, 10.0, 10.0).Shape();
    const TopoDS_Shape second =
        BRepPrimAPI_MakeBox(gp_Pnt(20, 0, 0), 2.0, 4.0, 6.0).Shape();
    const TopoDS_Shape face =
        BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(40, 0, 0), gp_Dir(0, 0, 1)),
                                0.0, 5.0, 0.0, 6.0).Shape();
    // Face-mode output exercises import sewing/promotion, not a prebuilt solid.
    IGESControl_Writer writer("MM", 0);
    REQUIRE(writer.AddShape(TopExp_Explorer(first, TopAbs_SHELL).Current()));
    REQUIRE(writer.AddShape(TopExp_Explorer(second, TopAbs_SHELL).Current()));
    REQUIRE(writer.AddShape(face));
    writer.ComputeModel();
    REQUIRE(writer.Write(path.string().c_str()));
    const auto bodies = chudware::doc::import_bodies(path.string(), "iges");
    REQUIRE(bodies.size() == 3);
    CHECK(bodies[0].second.ShapeType() == TopAbs_SOLID);
    CHECK(bodies[1].second.ShapeType() == TopAbs_SOLID);
    CHECK(vol(bodies[0].second) + vol(bodies[1].second) == doctest::Approx(1048.0));
    CHECK_FALSE(TopExp_Explorer(bodies[2].second, TopAbs_SOLID).More());
    GProp_GProps area;
    BRepGProp::SurfaceProperties(bodies[2].second, area);
    CHECK(area.Mass() == doctest::Approx(30.0));
    check_unique_topology(bodies, TopAbs_FACE, 13);
    check_unique_topology(bodies, TopAbs_EDGE, 28);
    fs::remove(path);
}

// --- N-8 + manual import + N-4 round-trip. ---------------------------------------------
TEST_CASE("saved .step+ survives source deletion; open/import route by extension") {
    using chudware::doc::doc_import_file;
    using chudware::doc::doc_open;
    using chudware::doc::doc_save;

    fs::path step = tmp("one_solid.step");
    std::vector<TopoDS_Shape> one = {BRepPrimAPI_MakeBox(12.0, 12.0, 12.0).Shape()};
    chudware::doc::export_bodies(one, step.string(), "step");

    // Manual import: doc_open on a NON-.step+ file imports it as a fresh part doc.
    json opened = doc_open(step.string(), false);
    CHECK(body_count(opened) == 1);

    // Import + bake + save as native .step+.
    json imp = doc_import_file(step.string(), "", false, "");
    const std::string did = imp.at("doc_id").get<std::string>();
    CHECK(body_count(imp) == 1);
    fs::path native = tmp("baked.step+");
    doc_save(did, native.string());

    // DELETE the source STEP — the saved .step+ must still rebuild the body from its
    // embedded geometry (N-8). Reopen and confirm the body is still there.
    fs::remove(step);
    json reopened = doc_open(native.string(), false);
    CHECK(body_count(reopened) == 1);

    // N-4: doc_import_file on a .step+ routes to doc_open (reopens the doc) rather than
    // failing the format sniffer.
    json reimported = doc_import_file(native.string(), "", false, "");
    CHECK(body_count(reimported) == 1);

    fs::remove(native);
}

TEST_CASE("modern SolidWorks part becomes native geometry and meshes with Polyhedral-FEA") {
    using chudware::doc::doc_import_file;
    using chudware::doc::doc_open;
    using chudware::doc::doc_save;
    using chudware::doc::export_bodies;
    using chudware::doc::import_bodies;

    const fs::path source = write_modern_solidworks("portable_part", false);
    const auto bodies = import_bodies(source.string(), "sldprt");
    REQUIRE(bodies.size() == 1);
    CHECK(vol(bodies.front().second) == doctest::Approx(1000.0).epsilon(1e-5));

    // The sewn B-rep must reach the display tessellator with a normal-bearing
    // Poly_Triangulation (without it the box renders from recomputed flat normals, and a
    // curved import renders as discrete facets). The normals are DERIVED from the soup, not
    // read from the file: the fixture authors +Z on every face, which is wrong on four of
    // the six, and a 40 deg crease angle keeps a box's faces flat. Composed with the face
    // orientation exactly as core/geom/tessellate.cpp does, every normal must therefore
    // point OUT of the 10 mm box — which is also the winding/orientation contract.
    const gp_Pnt center(5.0, 5.0, 5.0);
    int faces_with_normals = 0;
    for (TopExp_Explorer fx(bodies.front().second, TopAbs_FACE); fx.More(); fx.Next()) {
        const TopoDS_Face face = TopoDS::Face(fx.Current());
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
        REQUIRE(!tri.IsNull());
        REQUIRE(tri->HasNormals());
        ++faces_with_normals;
        const double sign = (face.Orientation() == TopAbs_REVERSED) ? -1.0 : 1.0;
        for (int ni = 1; ni <= tri->NbNodes(); ++ni) {
            gp_Pnt node = tri->Node(ni);
            gp_Dir nv = tri->Normal(ni);
            if (!loc.IsIdentity()) {
                node.Transform(loc.Transformation());
                nv.Transform(loc.Transformation());
            }
            const gp_Vec outward(center, node);
            CHECK(sign * gp_Vec(nv).Dot(outward) > 0.0);
            // A box has only 90 deg creases: nothing smooths, so each normal is axis-aligned.
            const double ax = std::abs(nv.X()), ay = std::abs(nv.Y()), az = std::abs(nv.Z());
            CHECK(std::max({ax, ay, az}) == doctest::Approx(1.0).epsilon(1e-6));
        }
    }
    CHECK(faces_with_normals == 6);  // six quads, each one planar face of two triangles

    json imported = doc_import_file(source.string(), "", false, "sldprt");
    REQUIRE(body_count(imported) == 1);
    CHECK(imported.value("import_fidelity", std::string()) == "saved_display_tessellation");
    CHECK(imported.value("editable_as", std::string()) == "baked_brep");
    const std::string doc_id = imported.at("doc_id").get<std::string>();
    const fs::path native = tmp("portable_part.step+");
    doc_save(doc_id, native.string());

    // The native document carries baked B-rep geometry; the proprietary source is provenance,
    // not a runtime dependency.
    fs::remove(source);
    json reopened = doc_open(native.string(), false);
    CHECK(body_count(reopened) == 1);

#ifdef CHUDWARE_WITH_POLYMESH
  const fs::path step = tmp("portable_part.step");
  std::vector<TopoDS_Shape> shapes = {bodies.front().second};
  export_bodies(shapes, step.string(), "step");
  chudware::sim::MeshOptions options;
  options.fineness = 0.4;
  options.order = 1;
  const chudware::sim::MeshOut mesh =
      chudware::sim::mesh_step(step.string(), options);
  REQUIRE_MESSAGE(mesh.ok, mesh.error);
  CHECK(mesh.n_elems > 0);
  CHECK(mesh.n_nodes > 0);
  CHECK(mesh.mesh_type_used == "tet4");
#endif

  fs::remove(native);
#ifdef CHUDWARE_WITH_POLYMESH
  fs::remove(step);
#endif
}

TEST_CASE("modern SolidWorks assembly preserves instances and transforms") {
    const fs::path source = write_modern_solidworks("portable_assembly", true);
    const auto bodies = chudware::doc::import_bodies(source.string(), "sldasm");
    REQUIRE(bodies.size() == 2);
    CHECK(bodies[0].first == "Tetra-1");
    CHECK(bodies[1].first == "Tetra-2");

    std::array<double, 2> center_x{};
    for (std::size_t i = 0; i < bodies.size(); ++i) {
        GProp_GProps props;
        BRepGProp::VolumeProperties(bodies[i].second, props);
        CHECK(props.Mass() == doctest::Approx(1000.0).epsilon(1e-5));
        center_x[i] = props.CentreOfMass().X();
    }
    json imported = chudware::doc::doc_import_file(source.string(), "", false, "sldasm");
    CHECK(imported.value("component_names", json::array()) ==
          json::array({"Tetra-1", "Tetra-2"}));
    CHECK(center_x[1] - center_x[0] == doctest::Approx(20.0).epsilon(1e-9));
    fs::remove(source);
}

// A faceted cylinder wound INSIDE-OUT — exactly the shape a SolidWorks saved-display import
// arrived in (signed volume -374963 mm³ on a 375017 mm³ spacer, so eval_mass was 28% wrong).
// mesh_to_shape must hand back a solid with POSITIVE volume, merge each coplanar facet set
// into ONE planar face (each wall quad's two triangles, each cap's 48-triangle fan — a cap
// left as 48 slivers is how a topology result's clamped end face came back as a 3.6 mm²
// pick), and give the curved wall smooth per-vertex normals while keeping the 90 deg
// wall/cap crease flat.
TEST_CASE("mesh_to_shape re-orients an inverted soup, merges coplanar facets, smooths its wall") {
    using chudware::doc::MeshData;
    using chudware::doc::mesh_to_shape;

    constexpr int kSides = 48;
    constexpr double kR = 10.0, kH = 20.0, kPi = 3.14159265358979323846;
    MeshData mesh;
    // Rim vertices (bottom 0..N-1, top N..2N-1) plus the two cap centres.
    for (int ring = 0; ring < 2; ++ring)
        for (int i = 0; i < kSides; ++i) {
            const double a = 2.0 * kPi * i / kSides;
            mesh.verts.insert(mesh.verts.end(),
                              {kR * std::cos(a), kR * std::sin(a), ring ? kH : 0.0});
        }
    const std::uint32_t c_bot = 2 * kSides, c_top = 2 * kSides + 1;
    mesh.verts.insert(mesh.verts.end(), {0.0, 0.0, 0.0});
    mesh.verts.insert(mesh.verts.end(), {0.0, 0.0, kH});
    // INWARD winding throughout (the outward version of each triangle has b/c swapped).
    for (int i = 0; i < kSides; ++i) {
        const std::uint32_t b0 = i, b1 = (i + 1) % kSides;
        const std::uint32_t t0 = kSides + b0, t1 = kSides + b1;
        mesh.tris.insert(mesh.tris.end(), {b0, t0, b1});
        mesh.tris.insert(mesh.tris.end(), {b1, t0, t1});
        mesh.tris.insert(mesh.tris.end(), {c_bot, b0, b1});
        mesh.tris.insert(mesh.tris.end(), {c_top, t1, t0});
    }

    const TopoDS_Shape shape = mesh_to_shape(mesh);
    REQUIRE(!shape.IsNull());
    CHECK(TopExp_Explorer(shape, TopAbs_SOLID).More());
    CHECK(BRepCheck_Analyzer(shape).IsValid());
    const double expect = 0.5 * kSides * std::sin(2.0 * kPi / kSides) * kR * kR * kH;
    CHECK(vol(shape) == doctest::Approx(expect).epsilon(1e-6));  // POSITIVE: re-oriented

    // Normals, composed with the face orientation the way core/geom/tessellate.cpp does.
    // Wall faces: horizontal, radial to within one facet pitch, CONTINUOUS across the facet
    // boundary (one normal per rim position, shared by both adjacent facets — that C0
    // normal field IS smooth shading) and genuinely averaged rather than flat. Cap faces:
    // the 90 deg crease kept them purely axial, so a box-like edge stays sharp.
    const double pitch = 2.0 * kPi / kSides;
    using PosKey = std::array<long long, 3>;
    std::map<PosKey, gp_Vec> wall_normal;  // welded wall position -> its one shared normal
    int wall_faces = 0, cap_faces = 0, triangles = 0;
    double max_tilt = 0.0;  // biggest angle between a node normal and its own facet normal
    for (TopExp_Explorer fx(shape, TopAbs_FACE); fx.More(); fx.Next()) {
        const TopoDS_Face face = TopoDS::Face(fx.Current());
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
        REQUIRE(!tri.IsNull());
        REQUIRE(tri->HasNormals());
        const double sign = (face.Orientation() == TopAbs_REVERSED) ? -1.0 : 1.0;
        bool wall = true;
        for (int ni = 1; ni <= tri->NbNodes(); ++ni)
            wall = wall && std::abs(tri->Normal(ni).Z()) < 1e-6;
        wall ? ++wall_faces : ++cap_faces;
        for (int ti = 1; ti <= tri->NbTriangles(); ++ti) {
            ++triangles;
            int idx[3];
            tri->Triangle(ti).Get(idx[0], idx[1], idx[2]);
            gp_Pnt p[3];
            gp_Vec n[3];
            for (int k = 0; k < 3; ++k) {
                p[k] = tri->Node(idx[k]);
                gp_Dir nv = tri->Normal(idx[k]);
                if (!loc.IsIdentity()) {
                    p[k].Transform(loc.Transformation());
                    nv.Transform(loc.Transformation());
                }
                n[k] = gp_Vec(sign * nv.X(), sign * nv.Y(), sign * nv.Z());
                // Outward: away from the body centre.
                CHECK(n[k].Dot(gp_Vec(gp_Pnt(0.0, 0.0, kH / 2.0), p[k])) > 1e-9);
            }
            // The facet's own flat normal, under the same winding the tessellator emits.
            gp_Vec flat = gp_Vec(p[0], p[1]).Crossed(gp_Vec(p[0], p[2])) * sign;
            flat.Normalize();
            for (int k = 0; k < 3; ++k) max_tilt = std::max(max_tilt, n[k].Angle(flat));
            if (!wall) {
                for (int k = 0; k < 3; ++k)
                    CHECK(std::abs(n[k].Z()) == doctest::Approx(1.0).epsilon(1e-6));
                continue;
            }
            for (int k = 0; k < 3; ++k) {
                const double r = std::hypot(p[k].X(), p[k].Y());
                CHECK(r == doctest::Approx(kR).epsilon(1e-9));
                CHECK(n[k].Dot(gp_Vec(p[k].X() / r, p[k].Y() / r, 0.0)) > std::cos(pitch));
                const PosKey key{std::llround(p[k].X() * 1e6), std::llround(p[k].Y() * 1e6),
                                 std::llround(p[k].Z() * 1e6)};
                const auto seen = wall_normal.emplace(key, n[k]);
                if (!seen.second) CHECK(seen.first->second.Angle(n[k]) < 1e-6);
            }
        }
    }
    CHECK(wall_faces == kSides);  // one planar face per wall quad
    CHECK(cap_faces == 2);        // one planar face per cap fan
    CHECK(triangles == 4 * kSides);  // every source triangle still displayed
    // Both rim rings, each welded position carrying exactly ONE normal.
    CHECK(static_cast<int>(wall_normal.size()) == 2 * kSides);
    CHECK(max_tilt > 0.1 * pitch);

    // The import stays editable: a through bore cut across both merged caps leaves a valid
    // solid with exactly the bore's volume removed, and it survives the embedded-BREP blob a
    // saved .step+ rebuilds the import from.
    const TopoDS_Shape bore =
        BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0.0, 0.0, -1.0), gp_Dir(0.0, 0.0, 1.0)), 4.0,
                                 kH + 2.0)
            .Shape();
    BRepAlgoAPI_Cut cut(shape, bore);
    REQUIRE(cut.IsDone());
    const TopoDS_Shape bored = cut.Shape();
    CHECK(BRepCheck_Analyzer(bored).IsValid());
    CHECK(vol(bored) == doctest::Approx(expect - kPi * 16.0 * kH).epsilon(1e-6));
    const TopoDS_Shape reopened =
        chudware::doc::brep_b64_to_shape(chudware::doc::shape_to_brep_b64(bored));
    REQUIRE(!reopened.IsNull());
    CHECK(vol(reopened) == doctest::Approx(vol(bored)).epsilon(1e-12));
    TopTools_IndexedMapOfShape bored_faces, reopened_faces;
    TopExp::MapShapes(bored, TopAbs_FACE, bored_faces);
    TopExp::MapShapes(reopened, TopAbs_FACE, reopened_faces);
    CHECK(reopened_faces.Extent() == bored_faces.Extent());

    // An open soup (one triangle missing) keeps the sewing lane: a shell, never a solid.
    MeshData open = mesh;
    open.tris.resize(open.tris.size() - 3);
    const TopoDS_Shape shell = mesh_to_shape(open);
    REQUIRE(!shell.IsNull());
    CHECK_FALSE(TopExp_Explorer(shell, TopAbs_SOLID).More());
}

TEST_CASE("DXF HEADER units scale imported geometry into millimetres") {
    for (const auto [units, expected] : std::vector<std::pair<int, double>>{
             {0, 2.0}, {1, 50.8}, {6, 2000.0}}) {
        const fs::path path = tmp("units_" + std::to_string(units) + ".dxf");
        {
            std::ofstream dxf(path);
            dxf << "0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n" << units
                << "\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n"
                   "0\nLINE\n10\n3\n20\n0\n11\n5\n21\n0\n"
                   "0\nENDSEC\n0\nEOF\n";
        }
        const auto bodies = chudware::doc::import_bodies(path.string(), "dxf");
        REQUIRE(bodies.size() == 1);
        const auto edges = TopExp_Explorer(bodies.front().second, TopAbs_EDGE);
        REQUIRE(edges.More());
        TopoDS_Vertex a, b;
        TopExp::Vertices(TopoDS::Edge(edges.Current()), a, b);
        CHECK(BRep_Tool::Pnt(a).Distance(BRep_Tool::Pnt(b)) ==
              doctest::Approx(expected).epsilon(1e-6));
        fs::remove(path);
    }
}

TEST_CASE("mesh exports refuse only wholly empty triangle sets") {
    const TopoDS_Shape wire =
        BRepBuilderAPI_MakeEdge(gp_Pnt(0, 0, 0), gp_Pnt(10, 0, 0)).Shape();
    const TopoDS_Shape box = BRepPrimAPI_MakeBox(10, 10, 10).Shape();
    for (const std::string& fmt : {"obj", "ply", "gltf", "glb"}) {
        const fs::path path = tmp("wire_export." + fmt);
        fs::remove(path);
        try {
            chudware::doc::export_bodies({wire}, path.string(), fmt);
            FAIL("zero-triangle export succeeded");
        } catch (const chudware::doc::OpError& e) {
            CHECK(e.code == "export_failed");
            CHECK_FALSE(fs::exists(path));
        }
        chudware::doc::export_bodies({box, wire}, path.string(), fmt);
        const auto mesh = chudware::doc::read_mesh(path.string(), fmt);
        CHECK(mesh.triangle_count() > 0);
        fs::remove(path);
        if (fmt == "gltf") {
            fs::path bin = path;
            fs::remove(bin.replace_extension(".bin"));
        }
    }
}

TEST_CASE("glTF reader converts metres to mm and writer converts mm to metres") {
    const fs::path external = tmp("external_meters.gltf");
    {
        std::ofstream os(external);
        os << R"GLTF({"asset":{"version":"2.0"},"scene":0,"scenes":[{"nodes":[0]}],
            "nodes":[{"mesh":0}],"meshes":[{"primitives":[{"attributes":{"POSITION":0},"indices":1}]}],
            "buffers":[{"byteLength":42,"uri":"data:application/octet-stream;base64,AAAAAAAAAAAAAAAAzczMPQAAAAAAAAAAAAAAAM3MzD0AAAAAAAABAAIA"}],
            "bufferViews":[{"buffer":0,"byteOffset":0,"byteLength":36,"target":34962},
                           {"buffer":0,"byteOffset":36,"byteLength":6,"target":34963}],
            "accessors":[{"bufferView":0,"componentType":5126,"count":3,"type":"VEC3",
                          "min":[0,0,0],"max":[0.1,0.1,0]},
                         {"bufferView":1,"componentType":5123,"count":3,"type":"SCALAR"}]})GLTF";
    }
    const auto external_mesh = chudware::doc::read_mesh(external.string(), "gltf");
    REQUIRE(external_mesh.triangle_count() == 1);
    CHECK(*std::max_element(external_mesh.verts.begin(), external_mesh.verts.end()) ==
          doctest::Approx(100.0).epsilon(1e-5));
    fs::remove(external);

    const TopoDS_Shape box = BRepPrimAPI_MakeBox(100.0, 40.0, 20.0).Shape();
    for (const std::string& fmt : {"gltf", "glb"}) {
        const fs::path path = tmp("meters_export." + fmt);
        chudware::doc::export_bodies({box}, path.string(), fmt);
        const auto mesh = chudware::doc::read_mesh(path.string(), fmt);
        REQUIRE(mesh.triangle_count() > 0);
        CHECK(*std::max_element(mesh.verts.begin(), mesh.verts.end()) ==
              doctest::Approx(100.0).epsilon(1e-5));
        fs::remove(path);
        if (fmt == "gltf") {
            fs::path bin = path;
            fs::remove(bin.replace_extension(".bin"));
        }
}
}

#ifdef CHUDWARE_WITH_LIB3MF
TEST_CASE("3MF model units scale the raw mesh to document millimetres") {
    const fs::path path = tmp("inch_model.3mf");
    const std::string model =
        R"XML(<?xml version="1.0" encoding="UTF-8"?>
<model unit="inch" xml:lang="en-US"
 xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
 <resources><object id="1" type="model" name="Inch tetra"><mesh>
  <vertices><vertex x="0" y="0" z="0"/><vertex x="2" y="0" z="0"/>
    <vertex x="0" y="2" z="0"/><vertex x="0" y="0" z="2"/></vertices>
  <triangles><triangle v1="0" v2="2" v3="1"/><triangle v1="0" v2="1" v3="3"/>
    <triangle v1="0" v2="3" v3="2"/><triangle v1="1" v2="2" v3="3"/></triangles>
 </mesh></object></resources><build><item objectid="1"/></build>
</model>)XML";
    const std::string types =
        R"XML(<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
</Types>)XML";
    const std::string rels =
        R"XML(<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel0"
  Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>)XML";
    mz_zip_archive zip{};
    REQUIRE(mz_zip_writer_init_file(&zip, path.string().c_str(), 0));
    for (const auto& [name, data] :
         std::vector<std::pair<std::string, std::string>>{
             {"[Content_Types].xml", types},
             {"_rels/.rels", rels},
             {"3D/3dmodel.model", model}}) {
        REQUIRE(mz_zip_writer_add_mem(
            &zip, name.c_str(), data.data(), data.size(), MZ_DEFAULT_COMPRESSION));
    }
    REQUIRE(mz_zip_writer_finalize_archive(&zip));
    REQUIRE(mz_zip_writer_end(&zip));
    const auto meshes = chudware::doc::read_mesh_raw(path.string(), "3mf");
    REQUIRE(meshes.size() == 1);
    CHECK(meshes[0].second.triangle_count() == 4);
    CHECK(*std::max_element(meshes[0].second.verts.begin(),
                            meshes[0].second.verts.end()) ==
          doctest::Approx(50.8).epsilon(1e-6));
    fs::remove(path);
}
#endif
