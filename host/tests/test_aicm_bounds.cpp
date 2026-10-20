// Consumer tests for the shipped native/wasm-compatible AICM byte parser.
#include "render/aicm_loader.h"

#include <chrono>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <limits>
#include <string>
#include <utility>
#include <vector>

namespace {
#ifdef __EMSCRIPTEN__
static_assert(sizeof(void*) == 4 && sizeof(size_t) == 4 && sizeof(std::ptrdiff_t) == 4,
              "AICM WebAssembly boundary proof requires actual wasm32");
#endif

using Bytes = std::vector<uint8_t>;
struct Chunk { const char* name; Bytes payload; };
int failures = 0;
#define CHECK(condition) do { if (!(condition)) { \
    std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); \
    ++failures; } } while (0)

void u8(Bytes& b, uint8_t v) { b.push_back(v); }
void u16(Bytes& b, uint16_t v) { for (int i = 0; i < 2; ++i) u8(b, uint8_t(v >> (8 * i))); }
void u32(Bytes& b, uint32_t v) { for (int i = 0; i < 4; ++i) u8(b, uint8_t(v >> (8 * i))); }
void u64(Bytes& b, uint64_t v) { for (int i = 0; i < 8; ++i) u8(b, uint8_t(v >> (8 * i))); }
void f32(Bytes& b, float v) { uint32_t bits; std::memcpy(&bits, &v, 4); u32(b, bits); }
void str(Bytes& b, const std::string& s) { u16(b, uint16_t(s.size())); b.insert(b.end(), s.begin(), s.end()); }
void set32(Bytes& b, size_t offset, uint32_t v) {
    for (int i = 0; i < 4; ++i) b.at(offset + i) = uint8_t(v >> (8 * i));
}
void set64(Bytes& b, size_t offset, uint64_t v) {
    for (int i = 0; i < 8; ++i) b.at(offset + i) = uint8_t(v >> (8 * i));
}
Bytes file(const std::vector<Chunk>& chunks) {
    Bytes b{'A', 'I', 'C', 'M'}; u32(b, 1); u32(b, uint32_t(chunks.size()));
    for (const auto& chunk : chunks) {
        b.insert(b.end(), chunk.name, chunk.name + 4);
        u64(b, chunk.payload.size());
        b.insert(b.end(), chunk.payload.begin(), chunk.payload.end());
    }
    return b;
}
Bytes body(bool material = true) {
    Bytes b; u32(b, 1); str(b, "tri");
    for (float v : {0.2f, 0.4f, 0.6f}) f32(b, v);
    u32(b, 3); u32(b, 1);
    for (float v : {0.f, 0.f, 0.f, 1.f, 0.f, 0.f, 0.f, 1.f, 0.f}) f32(b, v);
    for (int i = 0; i < 3; ++i) { f32(b, 0); f32(b, 0); f32(b, 1); }
    u32(b, 0); u32(b, 1); u32(b, 2);
    if (material) { f32(b, 0.75f); f32(b, 0.25f); }
    return b;
}
Bytes face() { Bytes b; u32(b, 1); u32(b, 1); u32(b, 0); u32(b, 1); u8(b, 0); return b; }
Bytes edge() {
    Bytes b; u32(b, 1); u32(b, 1); u32(b, 0); u32(b, 2); u8(b, 0); u32(b, 2);
    for (float v : {0.f, 0.f, 0.f, 1.f, 0.f, 0.f}) f32(b, v);
    return b;
}
Bytes vert() { Bytes b; u32(b, 1); u32(b, 1); f32(b, 0); f32(b, 0); f32(b, 0); return b; }
Bytes appearance() {
    Bytes b; u32(b, 1); u32(b, 1); u32(b, 1); u32(b, 1);
    for (float v : {0.1f, 0.3f, 0.5f, 0.4f, 0.8f}) f32(b, v);
    return b;
}
Bytes scalar() {
    Bytes b; u32(b, 1); str(b, "stress"); f32(b, 0); f32(b, 2); u32(b, 3);
    f32(b, 0); f32(b, 1); f32(b, 2); return b;
}
Bytes displacement() {
    Bytes b; u32(b, 1); f32(b, 1); u32(b, 3);
    for (int i = 0; i < 9; ++i) f32(b, float(i) / 10);
    return b;
}
Bytes lines() {
    Bytes b; str(b, "path"); u32(b, 1); u32(b, 2); u32(b, 2);
    for (float v : {0.f, 0.f, 0.f, 1.f, 0.f, 0.f}) f32(b, v);
    u8(b, 1); f32(b, 0); f32(b, 1); return b;
}
std::vector<Chunk> valid() {
    return {{"BODY", body()}, {"FACE", face()}, {"EDGE", edge()}, {"VERT", vert()},
            {"APRG", appearance()}, {"SCLR", scalar()}, {"DISP", displacement()}, {"LINS", lines()}};
}
void rejects(const Bytes& bytes, const char* expected_error = nullptr) {
    render::DocMesh mesh; mesh.bodies.emplace_back();
    std::string error;
    CHECK(!render::loadAicmBytes(bytes.data(), bytes.size(), mesh, error));
    CHECK(!error.empty());
    if (expected_error) CHECK(error == expected_error);
    CHECK(mesh.bodies.empty() && mesh.scalars.empty() && mesh.displacements.empty() && mesh.lines.empty());
}
void test_geometry_and_materials() {
    auto bytes = file(valid()); render::DocMesh mesh; std::string error = "stale error";
    CHECK(render::loadAicmBytes(bytes.data(), bytes.size(), mesh, error));
    CHECK(error.empty());
    if (mesh.bodies.size() != 1) { CHECK(false); return; }
    const auto& b = mesh.bodies[0];
    CHECK(b.n_verts == 3 && b.pos.size() == 9 && b.nrm.size() == 9);
    CHECK(b.n_tris == 1 && b.idx == std::vector<uint32_t>({0, 1, 2}));
    CHECK(b.pos[3] == 1 && b.pos[7] == 1 && b.nrm[2] == 1);
    CHECK(b.color[0] == 0.2f && b.color[1] == 0.4f && b.color[2] == 0.6f);
    CHECK(b.metallic == 0.75f && b.roughness == 0.25f);
    CHECK(b.faces.size() == 1 && b.faces[0].tri_count == 1);
    CHECK(b.edges.size() == 1 && b.edge_pts.size() == 6 && b.brep_verts.size() == 1);
    if (b.appearance_regions.size() == 1) {
        const auto& a = b.appearance_regions[0];
        CHECK(a.first_face == 1 && a.face_count == 1 && a.color[2] == 0.5f);
        CHECK(a.metallic == 0.4f && a.roughness == 0.8f);
    } else CHECK(false);
    CHECK(mesh.scalars.size() == 1 && mesh.scalars[0].values == std::vector<float>({0, 1, 2}));
    CHECK(mesh.displacements.size() == 1 && mesh.displacements[0].vec.size() == 9);
    CHECK(mesh.lines.size() == 1 && mesh.lines[0].pts.size() == 6 && mesh.lines[0].scalar.size() == 2);
    bytes = file({{"BODY", body(false)}});
    CHECK(render::parseAicm(bytes.data(), bytes.size(), mesh, error));
    CHECK(mesh.bodies.size() == 1 && mesh.bodies[0].metallic == 0 && mesh.bodies[0].roughness == 0.5f);
    auto chunks = valid(); chunks.insert(chunks.begin(), {"FUTR", {1, 2, 3}});
    bytes = file(chunks); bytes.push_back(0xaa); // Unknown chunks and trailing bytes still work.
    CHECK(render::parseAicm(bytes.data(), bytes.size(), mesh, error));
    CHECK(mesh.bodies.size() == 1 && mesh.bodies[0].appearance_regions.size() == 1);
}
void test_header_and_section_bounds() {
    const auto bytes = file(valid());
    for (size_t n = 0; n < bytes.size(); ++n) rejects(Bytes(bytes.begin(), bytes.begin() + n));
    auto corrupt = bytes; set32(corrupt, 4, 2); rejects(corrupt);
    corrupt = bytes; set32(corrupt, 8, UINT32_MAX); rejects(corrupt);
    corrupt = bytes; set64(corrupt, 16, UINT64_MAX); rejects(corrupt);
    // A premature wasm32 narrowing would turn these hostile envelopes into
    // exactly the available small payload, including an unknown chunk to skip.
    corrupt = bytes;
    set64(corrupt, 16, (uint64_t(1) << 32) + valid()[0].payload.size());
    rejects(corrupt, "chunk payload overruns file");
    corrupt = file({{"FUTR", {1, 2, 3}}, {"BODY", body()}});
    set64(corrupt, 16, (uint64_t(1) << 32) + 3);
    rejects(corrupt, "chunk payload overruns file");
    for (size_t i = 0; i < valid().size(); ++i) {
        auto chunks = valid(); chunks[i].payload.pop_back(); rejects(file(chunks));
        chunks = valid(); chunks[i].payload.push_back(0); rejects(file(chunks));
    }
    render::DocMesh mesh; std::string error;
    CHECK(!render::parseAicm(nullptr, 0, mesh, error));
    CHECK(!render::parseAicm(bytes.data(), std::numeric_limits<size_t>::max(), mesh, error));
    CHECK(!render::loadAicmBytes(bytes.data(),
        size_t(std::numeric_limits<std::ptrdiff_t>::max()) + 1, mesh, error));
    CHECK(error == "invalid AICM buffer size" && mesh.empty());
    for (const char* name : {"FACE", "EDGE", "VERT"}) rejects(file({{name, {0, 0, 0, 0}}}));
}
void test_wide_counts_and_ranges() {
    auto chunks = valid();
    // This tiny payload used to decode as two coordinates on wasm32.
    auto b = body(false); b.resize(29); set32(b, 21, 0x55555556u); set32(b, 25, 1);
    for (int i = 0; i < 4; ++i) f32(b, 0);
    u32(b, 0); u32(b, 1); u32(b, 0);
    CHECK(b.size() == 57); // Only the wrapped two coordinates per array exist.
    rejects(file({{"BODY", b}, {"FACE", face()}}), "invalid BODY payload length or count");
    chunks = valid(); set32(chunks[0].payload, 21, UINT32_MAX); rejects(file(chunks));
    chunks = valid(); set32(chunks[0].payload, 25, UINT32_MAX); rejects(file(chunks));
    chunks = valid(); set32(chunks[0].payload, 29 + 18 * 4, 3); rejects(file(chunks));
    chunks = valid(); set32(chunks[1].payload, 8, UINT32_MAX); rejects(file(chunks));
    chunks = valid(); set32(chunks[1].payload, 12, UINT32_MAX); rejects(file(chunks));
    chunks = valid(); set32(chunks[2].payload, 8, UINT32_MAX); rejects(file(chunks));
    chunks = valid(); set32(chunks[2].payload, 12, UINT32_MAX); rejects(file(chunks));
    chunks = valid(); set32(chunks[2].payload, 17, 0x55555556u); rejects(file(chunks));
    for (size_t i : {size_t(1), size_t(2), size_t(3)}) {
        chunks = valid(); set32(chunks[i].payload, 0, UINT32_MAX); rejects(file(chunks));
    }
    chunks = valid(); set32(chunks[5].payload, 20, UINT32_MAX); rejects(file(chunks));
    chunks = valid(); set32(chunks[6].payload, 8, 0x55555556u); rejects(file(chunks));
    chunks = valid(); set32(chunks[7].payload, 6, UINT32_MAX); rejects(file(chunks));
    chunks = valid(); set32(chunks[7].payload, 10, UINT32_MAX); rejects(file(chunks));
    chunks = valid(); set32(chunks[7].payload, 14, 0x55555556u); rejects(file(chunks));
    chunks = valid(); chunks[7].payload[42] = 2; rejects(file(chunks));
}
void test_file_consumer(const char* fixture) {
    render::DocMesh mesh; std::string error;
#ifdef __EMSCRIPTEN__
    // The real fixture is preloaded into Emscripten's filesystem by the proof run.
    // Exercise the shipped web byte consumer, not a native-sized simulation or
    // a substitute HTTP implementation.
    std::ifstream stream(fixture, std::ios::binary);
    CHECK(bool(stream));
    const Bytes bytes((std::istreambuf_iterator<char>(stream)), std::istreambuf_iterator<char>());
    CHECK(render::loadAicmBytes(bytes.data(), bytes.size(), mesh, error));
#else
    CHECK(render::loadAicmFile(fixture, mesh, error));
#endif
    if (mesh.bodies.size() != 1) { CHECK(false); return; }
    const auto& body = mesh.bodies[0];
    CHECK(body.n_verts == 24 && body.pos.size() == 72 && body.n_tris == 12 && body.idx.size() == 36);
    CHECK(body.faces.size() == 6 && body.edges.size() == 12 && body.brep_verts.size() == 8);
    CHECK(body.metallic == 0 && body.roughness == 0.5f);
#ifndef __EMSCRIPTEN__
    const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
    const auto root = std::filesystem::temp_directory_path() /
                      ("chudware-aicm-bounds-" + std::to_string(stamp));
    std::error_code failure;
    if (!std::filesystem::create_directory(root, failure) || failure) { CHECK(false); return; }
    const auto path = root / "inert-corrupt.aicm";
    auto chunks = valid(); set32(chunks[0].payload, 21, 0x55555556u);
    const auto corrupt = file(chunks);
    {
        std::ofstream stream(path, std::ios::binary);
        stream.write(reinterpret_cast<const char*>(corrupt.data()), std::streamsize(corrupt.size()));
        CHECK(bool(stream));
    }
    CHECK(!render::loadAicmFile(path.u8string(), mesh, error));
    CHECK(!error.empty() && mesh.empty());
    std::filesystem::remove_all(root, failure);
    CHECK(!failure);
#endif
}
} // namespace

int main(int argc, char** argv) {
#ifdef __EMSCRIPTEN__
    std::puts("test_aicm_bounds: actual wasm32 (size_t/ptrdiff_t/pointer = 32 bits)");
#endif
    test_geometry_and_materials();
    test_header_and_section_bounds();
    test_wide_counts_and_ranges();
    if (argc == 2) test_file_consumer(argv[1]);
    else { std::fprintf(stderr, "expected golden box.aicm fixture path\n"); ++failures; }
    if (failures) { std::fprintf(stderr, "test_aicm_bounds: %d failure(s)\n", failures); return 1; }
    std::puts("test_aicm_bounds: all checks passed");
    return 0;
}
