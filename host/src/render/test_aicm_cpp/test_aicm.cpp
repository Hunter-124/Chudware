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
    // body_id is 8-bit in the packed id — values above 255 truncate (documented contract limit).
    render::PickHit hi = render::decodeId(render::encodeId(300, PickKind::Face, 1));
    CHECK(hi.body_id == 44 && hi.kind == PickKind::Face && hi.topo_id == 1,
          "body_id truncates to 8 bits (300 -> 44)");

    // ---- id-pass frame cache key (task 01: selection lag) ----
    // The whole point of the cache is: identical inputs => render() is skipped; ANY change to the
    // camera, viewport size, clip planes, hidden set, or doc generation => the key differs and the
    // id buffer is re-rendered. Verify each axis flips equality so a stale buffer can't be reused.
    {
        using render::IdPassFrameKey;
        IdPassFrameKey base;  // identity-ish; concrete values below
        for (int i = 0; i < 16; ++i) base.mvp[i] = (float)i * 0.5f;
        base.w = 1280; base.h = 720;
        base.clipCount = 1;
        base.clipN[0] = 0; base.clipN[1] = 0; base.clipN[2] = 1; base.clipOff[0] = 3.5f;
        base.hiddenHash = render::idPassHiddenHash({2, 7});
        base.docGen = 4;

        IdPassFrameKey same = base;
        CHECK(same == base, "frame key: identical inputs compare equal (render is skipped)");

        IdPassFrameKey camMoved = base; camMoved.mvp[5] += 0.001f;
        CHECK(camMoved != base, "frame key: a moved camera invalidates the cache");

        IdPassFrameKey resized = base; resized.w = 1281;
        CHECK(resized != base, "frame key: a viewport resize invalidates the cache");

        IdPassFrameKey clipOffMoved = base; clipOffMoved.clipOff[0] = 4.0f;
        CHECK(clipOffMoved != base, "frame key: a moved section plane invalidates the cache");

        IdPassFrameKey clipAdded = base; clipAdded.clipCount = 0;
        CHECK(clipAdded != base, "frame key: enabling/disabling section clip invalidates the cache");

        IdPassFrameKey docBumped = base; docBumped.docGen = 5;
        CHECK(docBumped != base, "frame key: a rebuilt doc (docGen++) invalidates the cache");

        IdPassFrameKey hid = base; hid.hiddenHash = render::idPassHiddenHash({2});
        CHECK(hid != base, "frame key: toggling body visibility invalidates the cache");

        // The hidden-set digest is order-independent (std::set is sorted) but content-sensitive.
        CHECK(render::idPassHiddenHash({2, 7}) == render::idPassHiddenHash({7, 2}),
              "hidden hash: order-independent over the same set");
        CHECK(render::idPassHiddenHash({}) != render::idPassHiddenHash({1}),
              "hidden hash: empty vs non-empty differ");
        CHECK(render::idPassHiddenHash({1, 2}) != render::idPassHiddenHash({1, 3}),
              "hidden hash: differing members differ");
    }

    if (g_fail) { std::printf("\n%d check(s) FAILED\n", g_fail); return 1; }
    std::printf("\nall checks passed\n");
    return 0;
}
