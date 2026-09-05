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

#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <BRep_Tool.hxx>
#include <GProp_GProps.hxx>
#include <Poly_Triangulation.hxx>
#include <TopExp_Explorer.hxx>
#include <TopLoc_Location.hxx>
#include <TopoDS.hxx>
#include <TopoDS_Face.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Dir.hxx>
#include <gp_Pnt.hxx>
#include <gp_Vec.hxx>

#include "doc/formats.h"
#include "doc/session.h"
#ifdef CHUDWARE_WITH_POLYMESH
#include "sim/mesher.h"
#endif

using nlohmann::json;
namespace fs = std::filesystem;

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
                  "test \"$1\" = -o || exit 2\n"
                  "printf '0\\nSECTION\\n2\\nENTITIES\\n0\\nLINE\\n8\\n0\\n10\\n0\\n20\\n0\\n11\\n1\\n21\\n1\\n0\\nENDSEC\\n0\\nEOF\\n' > \"$2\"\n"
                  "printf '%s' \"$3\" > \"$2.argv\"\n";
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
    CHECK(faces_with_normals == 12);  // six quads, two triangles each

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
// mesh_to_shape must hand back a solid with POSITIVE volume, and it must give the curved
// wall smooth per-vertex normals while keeping the 90 deg wall/cap crease flat.
TEST_CASE("mesh_to_shape re-orients an inverted soup and smooths its curved wall") {
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
    int wall_faces = 0, cap_faces = 0;
    double max_tilt = 0.0;  // biggest angle between a node normal and its own facet normal
    for (TopExp_Explorer fx(shape, TopAbs_FACE); fx.More(); fx.Next()) {
        const TopoDS_Face face = TopoDS::Face(fx.Current());
        TopLoc_Location loc;
        Handle(Poly_Triangulation) tri = BRep_Tool::Triangulation(face, loc);
        REQUIRE(!tri.IsNull());
        REQUIRE(tri->HasNormals());
        REQUIRE(tri->NbNodes() == 3);
        const double sign = (face.Orientation() == TopAbs_REVERSED) ? -1.0 : 1.0;
        gp_Pnt p[3];
        gp_Vec n[3];
        for (int ni = 0; ni < 3; ++ni) {
            p[ni] = tri->Node(ni + 1);
            gp_Dir nv = tri->Normal(ni + 1);
            if (!loc.IsIdentity()) {
                p[ni].Transform(loc.Transformation());
                nv.Transform(loc.Transformation());
            }
            n[ni] = gp_Vec(sign * nv.X(), sign * nv.Y(), sign * nv.Z());
            // Outward: away from the body centre.
            CHECK(n[ni].Dot(gp_Vec(gp_Pnt(0.0, 0.0, kH / 2.0), p[ni])) > 1e-9);
        }
        // The facet's own flat normal, under the same winding the tessellator emits.
        gp_Vec flat = gp_Vec(p[0], p[1]).Crossed(gp_Vec(p[0], p[2])) * sign;
        flat.Normalize();
        for (int ni = 0; ni < 3; ++ni)
            max_tilt = std::max(max_tilt, n[ni].Angle(flat));
        const bool wall = std::abs(n[0].Z()) < 1e-6 && std::abs(n[1].Z()) < 1e-6 &&
                          std::abs(n[2].Z()) < 1e-6;
        if (!wall) {
            for (int ni = 0; ni < 3; ++ni)
                CHECK(std::abs(n[ni].Z()) == doctest::Approx(1.0).epsilon(1e-6));
            ++cap_faces;
            continue;
        }
        ++wall_faces;
        for (int ni = 0; ni < 3; ++ni) {
            const double r = std::hypot(p[ni].X(), p[ni].Y());
            CHECK(r == doctest::Approx(kR).epsilon(1e-9));
            CHECK(n[ni].Dot(gp_Vec(p[ni].X() / r, p[ni].Y() / r, 0.0)) > std::cos(pitch));
            const PosKey key{std::llround(p[ni].X() * 1e6), std::llround(p[ni].Y() * 1e6),
                             std::llround(p[ni].Z() * 1e6)};
            const auto seen = wall_normal.emplace(key, n[ni]);
            if (!seen.second) CHECK(seen.first->second.Angle(n[ni]) < 1e-6);
        }
    }
    CHECK(wall_faces == 2 * kSides);
    CHECK(cap_faces == 2 * kSides);
    // Both rim rings, each welded position carrying exactly ONE normal.
    CHECK(static_cast<int>(wall_normal.size()) == 2 * kSides);
    CHECK(max_tilt > 0.1 * pitch);
}
