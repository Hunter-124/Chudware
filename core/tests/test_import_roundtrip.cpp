// Import / save round-trip suite — locks the round-3 file-plumbing fixes:
//   N-8: a saved .step+ BAKES imported geometry in, so it survives the source file being
//        deleted (the "assembly reopened as just the plate" P0).
//   N-5: a multi-solid STEP imports as SEPARATE bodies, not one merged compound.
//   N-4: doc_import_file accepts the native .step+ extension (reopens it as a doc).
//   manual import: doc_open on a NON-.step+ file imports it as a fresh part doc (the
//        "nothing happens when I open a .step" bug).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>
#include <array>

#include <filesystem>
#include <string>
#include <fstream>
#include <cstdint>
#include <cstring>
#include <cstdlib>
#ifndef _WIN32
#include <sys/stat.h>
#endif
#include <vector>
#include <miniz.h>

#include <BRepGProp.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <GProp_GProps.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>

#include "doc/formats.h"
#include "doc/session.h"
#include "sim/mesher.h"

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

    const fs::path step = tmp("portable_part.step");
    std::vector<TopoDS_Shape> shapes = {bodies.front().second};
    export_bodies(shapes, step.string(), "step");
    if (chudware::sim::mesher_backend_available(chudware::sim::MesherBackend::Polymesh)) {
        chudware::sim::MeshOptions options;
        options.backend = chudware::sim::MesherBackend::Polymesh;
        options.fineness = 0.4;
        options.order = 1;
        const chudware::sim::MeshOut mesh = chudware::sim::mesh_step(step.string(), options);
        REQUIRE_MESSAGE(mesh.ok, mesh.error);
        CHECK(mesh.n_elems > 0);
        CHECK(mesh.n_nodes > 0);
        CHECK(mesh.mesh_type_used == "tet4-polymesh");
    }

    fs::remove(native);
    fs::remove(step);
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
