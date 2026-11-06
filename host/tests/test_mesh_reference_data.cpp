// GL-free regression for the exact page assembler used by the viewport.
#include "render/mesh_reference_data.h"

#include <cmath>
#include <cstdio>
#include <limits>

using nlohmann::json;

static int failures = 0;
#define CHECK(condition) do { if (!(condition)) { \
    std::fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #condition); ++failures; \
} } while (0)

int main() {
    const json transform = {{"mm_per_unit", 2}, {"rotation", {{1,0,0},{0,1,0},{0,0,1}}},
                            {"translation_mm", {0,0,0}}};
    const json ref = {{"sha256", "abc"}, {"transform", transform}};
    json moved = ref;
    moved["transform"]["translation_mm"] = {5, 0, 0};
    CHECK(render::meshReferenceKey(ref) != render::meshReferenceKey(moved));
    CHECK(render::meshReferenceKey({{"transform", transform}}).empty());

    json first = {{"vertices_mm", {0,0,0, 2,0,0, 0,2,0}}, {"indices", {0,1,2}},
                  {"offset_tris", 0}, {"total_tris", 2}, {"next_offset_tris", 1}};
    json second = {{"vertices_mm", {5,0,0, 5,2,0, 5,0,2}}, {"indices", {0,1,2}},
                   {"offset_tris", 1}, {"total_tris", 2}, {"next_offset_tris", nullptr}};
    Mesh mesh;
    size_t offset = 0;
    render::appendMeshReferencePage(mesh, first, offset, 2);
    CHECK(offset == 1 && mesh.indices == std::vector<uint32_t>({0,1,2}));
    CHECK(mesh.normals.size() == 9 && std::fabs(mesh.normals[2] - 1.0f) < 1e-6f);
    render::appendMeshReferencePage(mesh, second, offset, 2);
    CHECK(offset == 2 && mesh.indices == std::vector<uint32_t>({0,1,2,3,4,5}));
    CHECK(mesh.positions[9] == 5.0f && mesh.normals.size() == mesh.positions.size());
    CHECK(std::fabs(mesh.normals[9] - 1.0f) < 1e-6f);

    auto rejects = [&](json page) {
        Mesh result;
        size_t start = 0;
        bool threw = false;
        try { render::appendMeshReferencePage(result, page, start, 2); }
        catch (const std::exception&) { threw = true; }
        CHECK(threw && start == 0 && result.positions.empty() && result.indices.empty());
    };
    json bad = first;
    bad["indices"][2] = 7;
    rejects(bad);
    bad = first;
    bad["next_offset_tris"] = nullptr;
    rejects(bad);
    bad = first;
    bad["offset_tris"] = 1;
    rejects(bad);
    bad = first;
    bad["vertices_mm"][0] = std::numeric_limits<float>::infinity();
    rejects(bad);
    return failures ? 1 : 0;
}
