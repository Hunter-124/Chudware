// Pure-CPU unit test for the C++ AICM reader (render/aicm_loader). No GL — parses the golden
// fixture and asserts the contract counts/ranges from mesh_format.md §4. Built as the optional
// `test_aicm` target (see host/CMakeLists.txt). Run: test_aicm <path-to-box.aicm>.
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>

#include "../aicm_loader.h"
#include "../id_pass.h"  // encode/decode id helpers (no GL in these inline functions)

static int g_fail = 0;
#define CHECK(cond, msg)                                              \
    do {                                                             \
        if (!(cond)) { std::printf("FAIL: %s\n", msg); ++g_fail; }   \
        else { std::printf("ok:   %s\n", msg); }                     \
    } while (0)

int main(int argc, char** argv) {
    std::string path = argc > 1 ? argv[1] : "docs/contracts/fixtures/box.aicm";

    render::DocMesh doc;
    std::string err;
    if (!render::loadAicmFile(path, doc, err)) {
        std::printf("FATAL: could not load %s: %s\n", path.c_str(), err.c_str());
        return 2;
    }

    CHECK(doc.version == 1, "version == 1");
    CHECK(doc.bodies.size() == 1, "1 body");
    if (doc.bodies.empty()) return 2;

    const render::BodyMesh& b = doc.bodies.front();
    CHECK(b.body_id == 1, "body_id == 1");
    CHECK(b.name == "Body1", "name == Body1");
    CHECK(b.n_verts == 24, "24 verts");
    CHECK(b.n_tris == 12, "12 tris");
    CHECK(b.pos.size() == 24u * 3, "pos array 24*3");
    CHECK(b.nrm.size() == 24u * 3, "nrm array 24*3");
    CHECK(b.idx.size() == 12u * 3, "idx array 12*3");

    CHECK(b.faces.size() == 6, "6 face ranges");
    // Contiguous 2-tri ranges covering all 12 triangles, ids 1..6.
    bool contiguous = true;
    uint32_t next = 0;
    for (size_t i = 0; i < b.faces.size(); ++i) {
        if (b.faces[i].face_id != i + 1) contiguous = false;
        if (b.faces[i].first_tri != next) contiguous = false;
        if (b.faces[i].tri_count != 2) contiguous = false;
        next += b.faces[i].tri_count;
    }
    CHECK(contiguous, "faces are contiguous 2-tri ranges, ids 1..6");
    CHECK(next == 12, "face ranges cover all 12 triangles");

    CHECK(b.edges.size() == 12, "12 edges");
    // Each edge is a 2-pt polyline → 24 total edge points.
    CHECK(b.edge_pts.size() == 24u * 3, "24 edge points (2 per edge)");
    bool twoPt = true;
    for (const auto& e : b.edges)
        if (e.pt_count != 2) twoPt = false;
    CHECK(twoPt, "every edge is a 2-pt polyline");

    CHECK(b.brep_verts.size() == 8, "8 B-rep vertices");

    // ---- id encode/decode round-trip ----
    using render::PickKind;
    uint32_t enc = render::encodeId(1, PickKind::Face, 5);
    render::PickHit h = render::decodeId(enc);
    CHECK(h.body_id == 1 && h.kind == PickKind::Face && h.topo_id == 5,
          "encode/decode round-trips body=1 face id=5");
    render::PickHit hv = render::decodeId(render::encodeId(3, PickKind::Vertex, 0x3FFFFF));
    CHECK(hv.body_id == 3 && hv.kind == PickKind::Vertex && hv.topo_id == 0x3FFFFF,
          "encode/decode round-trips max 22-bit topo id");
    CHECK(!render::decodeId(0).valid(), "id 0 decodes to invalid (background)");

    if (g_fail) { std::printf("\n%d check(s) FAILED\n", g_fail); return 1; }
    std::printf("\nall checks passed\n");
    return 0;
}
