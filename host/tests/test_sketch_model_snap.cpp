// test_sketch_model_snap.cpp — ctest for ui::SketchModelSnap / ui::modelref (host/src/ui/
// sketch_model_snap.*), the model-geometry inference behind every sketch tool's snapping.
//
// Regression under test (live, 2026-11-07): drawing in a sketch over a heavy part, the cursor never
// snapped to the solid's vertices, edge midpoints, hole centers or edges, so every relation to the
// model was added by hand afterwards. The sketcher now indexes the tessellation's B-rep topology
// once per (mesh, sketch plane) and binds the snap into the commit batch as an associative external
// reference.
//
// Acceptance:
//   ✓ candidates: B-rep vertices, straight-edge midpoints, circle centers, on-edge points
//   ✓ priority: vertex > midpoint/center > on-edge; reach limits; inference off keeps vertices only
//   ✓ a candidate the id buffer does not show (hidden back face) is skipped for the next one
//   ✓ edge-on edges / circles seen edge-on contribute nothing (they project to no curve)
//   ✓ the index is rebuilt only when the mesh object or the sketch plane changes
//   ✓ model-reference tokens bind into ONE batch: prepended sketch_convert, shifted placeholders,
//     reuse of an already-converted reference, id_map restored for the caller

#include "ui/sketch_model_snap.h"

#include <cmath>
#include <cstdio>
#include <memory>
#include <set>
#include <string>

using nlohmann::json;
using ui::ModelSnapHit;
using ui::ModelSnapKind;
using ui::ModelSnapScreen;
using ui::SketchModelSnap;

static int g_failures = 0;

#define CHECK(cond)                                                              \
    do {                                                                         \
        if (!(cond)) {                                                           \
            std::printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond);          \
            ++g_failures;                                                        \
        }                                                                        \
    } while (0)

static bool near(float a, float b, float tol = 1e-3f) { return std::fabs(a - b) <= tol; }

// A 100 x 40 x 10 box (x 0..100, y 0..40, z 0..10) with a hole-like circle edge on the top face
// and a circle edge on the front face (seen edge-on from the top).
static std::shared_ptr<const render::DocMesh> boxMesh() {
    auto mesh = std::make_shared<render::DocMesh>();
    render::BodyMesh b;
    b.body_id = 1;
    const float c[8][3] = {{0, 0, 0}, {100, 0, 0}, {100, 40, 0}, {0, 40, 0},
                           {0, 0, 10}, {100, 0, 10}, {100, 40, 10}, {0, 40, 10}};
    for (uint32_t i = 0; i < 8; ++i) {
        render::BrepVertex v;
        v.vertex_id = i + 1;
        for (int k = 0; k < 3; ++k) v.xyz[k] = c[i][k];
        b.brep_verts.push_back(v);
    }
    auto addEdge = [&](uint32_t id, uint8_t curve, const std::vector<std::array<float, 3>>& pts) {
        render::EdgeSegment e;
        e.edge_id = id; e.curve_type = curve;
        e.first_pt = uint32_t(b.edge_pts.size() / 3); e.pt_count = uint32_t(pts.size());
        for (const auto& p : pts) b.edge_pts.insert(b.edge_pts.end(), {p[0], p[1], p[2]});
        b.edges.push_back(e);
    };
    const int pairs[12][2] = {{0, 1}, {1, 2}, {2, 3}, {3, 0}, {4, 5}, {5, 6},
                              {6, 7}, {7, 4}, {0, 4}, {1, 5}, {2, 6}, {3, 7}};
    for (uint32_t i = 0; i < 12; ++i)
        addEdge(i + 1, 0, {{c[pairs[i][0]][0], c[pairs[i][0]][1], c[pairs[i][0]][2]},
                           {c[pairs[i][1]][0], c[pairs[i][1]][1], c[pairs[i][1]][2]}});
    std::vector<std::array<float, 3>> top, front;
    for (int i = 0; i <= 48; ++i) {
        const float a = float(i) / 48.0f * 6.2831853f;
        top.push_back({50.0f + 5.0f * std::cos(a), 20.0f + 5.0f * std::sin(a), 10.0f});
        front.push_back({30.0f + 3.0f * std::cos(a), 0.0f, 5.0f + 3.0f * std::sin(a)});
    }
    addEdge(13, 1, top);     // circle in the sketch plane
    addEdge(14, 1, front);   // circle perpendicular to the sketch plane
    mesh->bodies.push_back(std::move(b));
    return mesh;
}

// Top-face sketch plane: origin (0,0,10), u = +X, v = +Y, normal +Z (row-major 4x4).
static const float kTop[16] = {1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 10, 0, 0, 0, 1};

// 4 px per mm, sketch (0,0) at screen (100, 400), +v up the screen.
static ModelSnapScreen screen() {
    ModelSnapScreen s;
    s.ox = 100; s.oy = 400; s.ux = 4; s.uy = 0; s.vx = 0; s.vy = -4;
    return s;
}

static bool query(const SketchModelSnap& idx, float u, float v, ModelSnapHit& hit,
                  bool inference = true, bool onEdge = true, float reach = 8.0f,
                  const SketchModelSnap::Visible& visible =
                      [](const ModelSnapHit&) { return true; }) {
    const ModelSnapScreen s = screen();
    return idx.query(s, s.x(u, v), s.y(u, v), reach, inference, onEdge, visible, hit);
}

int main() {
    const auto mesh = boxMesh();
    SketchModelSnap idx;
    idx.sync(mesh, kTop);
    // 8 vertices + 8 midpoints (the four vertical edges project to a point) + the in-plane circle.
    CHECK(idx.keyCount() == 17);
    // 8 horizontal edges + the in-plane circle; the vertical edges and the edge-on circle are gone.
    CHECK(idx.edgeCount() == 9);

    ModelSnapHit hit;
    // Vertex: the top and bottom corner project to the same point; the visible one wins.
    CHECK(query(idx, 100.1f, 40.1f, hit));
    CHECK(hit.kind == ModelSnapKind::Vertex && near(hit.u, 100) && near(hit.v, 40));
    const auto topOnly = [](const ModelSnapHit& h) { return h.probe[0][2] > 5.0f; };
    CHECK(query(idx, 100.1f, 40.1f, hit, true, true, 8.0f, topOnly));
    CHECK(hit.kind == ModelSnapKind::Vertex && hit.topo == 7);   // vertex 7 = (100,40,10)
    CHECK(!query(idx, 100.1f, 40.1f, hit, true, true, 8.0f, [](const ModelSnapHit&) { return false; }));

    // Midpoint of the front top edge (edge 5, (0,0,10) -> (100,0,10)).
    CHECK(query(idx, 50.0f, 0.1f, hit, true, false));
    CHECK(hit.kind == ModelSnapKind::Midpoint && near(hit.u, 50) && near(hit.v, 0));
    CHECK(!query(idx, 50.0f, 0.1f, hit, false, true));           // inference off: vertices only

    // Circle center (the hole-center snap): no edge is under the cursor there.
    CHECK(query(idx, 50.1f, 20.0f, hit));
    CHECK(hit.kind == ModelSnapKind::Center && hit.topo == 13 && hit.probes == 4);
    CHECK(near(hit.u, 50, 1e-2f) && near(hit.v, 20, 1e-2f));

    // On-edge: 2 px off the front top edge, away from every key point.
    CHECK(query(idx, 25.0f, 0.5f, hit));
    CHECK(hit.kind == ModelSnapKind::OnEdge && near(hit.u, 25) && near(hit.v, 0));
    CHECK(!query(idx, 25.0f, 0.5f, hit, true, false));           // caller asked for key points only
    // The edge-on circle (center (30,0)) must not appear: the snap there is the edge it overlaps.
    CHECK(query(idx, 30.0f, 0.2f, hit));
    CHECK(hit.kind == ModelSnapKind::OnEdge && hit.topo != 14);
    // Out of reach.
    CHECK(!query(idx, 25.0f, 5.0f, hit));

    // Priority: a vertex 5.6 px away beats a midpoint 0.4 px away.
    {
        auto tiny = std::make_shared<render::DocMesh>();
        render::BodyMesh b;
        b.body_id = 2;
        render::BrepVertex v0; v0.vertex_id = 1; v0.xyz[2] = 10;
        render::BrepVertex v1; v1.vertex_id = 2; v1.xyz[0] = 3; v1.xyz[2] = 10;
        b.brep_verts = {v0, v1};
        render::EdgeSegment e; e.edge_id = 1; e.first_pt = 0; e.pt_count = 2; e.curve_type = 0;
        b.edge_pts = {0, 0, 10, 3, 0, 10};
        b.edges = {e};
        tiny->bodies.push_back(std::move(b));
        SketchModelSnap small;
        small.sync(tiny, kTop);
        CHECK(query(small, 1.4f, 0.0f, hit));
        CHECK(hit.kind == ModelSnapKind::Vertex && hit.topo == 1);
        CHECK(query(small, 1.5f, 0.0f, hit, true, true, 3.0f));   // vertices out of reach now
        CHECK(hit.kind == ModelSnapKind::Midpoint);
    }

    // The index is rebuilt only for a new mesh object or a moved sketch plane.
    CHECK(idx.builds() == 1);
    idx.sync(mesh, kTop);
    CHECK(idx.builds() == 1);
    float moved[16];
    for (int i = 0; i < 16; ++i) moved[i] = kTop[i];
    moved[11] = 0.0f;   // sketch plane at z = 0
    idx.sync(mesh, moved);
    CHECK(idx.builds() == 2);
    idx.sync(boxMesh(), moved);
    CHECK(idx.builds() == 3);
    idx.sync(nullptr, moved);
    CHECK(idx.keyCount() == 0 && idx.edgeCount() == 0);

    {   // Edges owned by a feature that depends on the sketch are dropped, and a vertex survives
        // only while an allowed edge still ends on it: here the top ring and the verticals go.
        SketchModelSnap filtered;
        const SketchModelSnap::EdgeFilter downstream = [](uint32_t, uint32_t edge) {
            return edge >= 5 && edge <= 12;
        };
        filtered.sync(mesh, kTop, 7, downstream);
        CHECK(filtered.keyCount() == 9);   // 4 bottom vertices + 4 bottom midpoints + the circle center
        CHECK(filtered.edgeCount() == 5);
        CHECK(query(filtered, 100.1f, 40.1f, hit));
        CHECK(hit.kind == ModelSnapKind::Vertex && hit.topo == 3);   // the bottom corner; the top one is gone
        CHECK(filtered.upToDate(mesh, kTop, 7));
        CHECK(!filtered.upToDate(mesh, kTop, 8));   // the filter's inputs changed
    }

    {   // downstream features: referencing a sketch directly or through another feature
        const json tree = json::parse(R"({"features":[
          {"id":"f1","type":"sketch","params":{"entities":[{"id":"e1","note":"f9"}]},"refs":{"plane":"plane.top"}},
          {"id":"f2","type":"extrude","refs":{"profile":["f1"]}},
          {"id":"f3","type":"sketch","refs":{"plane":{"of":"face","owner_feature":"f2"}}},
          {"id":"f4","type":"extrude","refs":{"profile":[{"kind":"region","sketch":"f3"}]}},
          {"id":"f5","type":"fillet","refs":{"edges":[{"owner_feature":"f9"}]}},
          {"id":"f6","type":"box"}]})");
        const std::set<std::string> all = {"f2", "f3", "f4"};
        CHECK(ui::modelref::downstreamOf(tree, "f1") == all);
        CHECK(ui::modelref::downstreamOf(tree, "f3") == std::set<std::string>{"f4"});
        CHECK(ui::modelref::downstreamOf(tree, "f6").empty());
        CHECK(ui::modelref::downstreamOf(tree, "nope").empty());
    }

    // ---- model-reference tokens ----------------------------------------------------------
    CHECK(ui::modelref::snapRecord(ModelSnapKind::Vertex, 3) == "@m3.p");
    CHECK(ui::modelref::snapRecord(ModelSnapKind::Midpoint, 3) == "mid:@m3");
    CHECK(ui::modelref::snapRecord(ModelSnapKind::Center, 3) == "@m3.center");
    CHECK(ui::modelref::snapRecord(ModelSnapKind::OnEdge, 3) == "rim:@m3");
    CHECK(ui::modelref::snapRecord(ModelSnapKind::Center, -1) == "@m.center");
    CHECK(ui::modelref::withRef("rim:@m", 12) == "rim:@m12");
    CHECK(ui::modelref::withRef("e3.p2", 12) == "e3.p2");

    const json refA = {{"kind", "vertex"}, {"selector", {{"sid", "v:a"}}}};
    const json refB = {{"kind", "edge"}, {"selector", {{"sid", "e:b"}}}};
    const json batch = json::parse(R"([
      {"op":"sketch_add_entity","sketch":"f2","entity":{"type":"line"},
       "constraints":[{"type":"coincident","refs":["@m0.p","$self.p1"]},
                      {"type":"midpoint","refs":["$self.p2","@m1"]},
                      {"type":"horizontal","refs":["$self"]}]},
      {"op":"sketch_add_constraint","sketch":"f2",
       "constraint":{"type":"equal","refs":["$0","@m1"]}}])");
    CHECK(ui::modelref::uses(batch));
    CHECK(!ui::modelref::uses(json::parse(R"([{"op":"sketch_add_entity","constraints":[{"refs":["$self.p1","e3.p2"]}]}])")));

    {   // both references are new: converts are prepended and the batch's placeholders shift by 2
        json ops = batch;
        const int shift = ui::modelref::bind(ops, "f2", {refA, refB},
                                             [](const json&) { return std::string(); });
        CHECK(shift == 2 && ops.size() == 4);
        CHECK(ops[0]["op"] == "sketch_convert" && ops[0]["refs"][0] == refA &&
              ops[0]["associative"] == true && ops[0]["fix"] == true && ops[0]["construction"] == true);
        CHECK(ops[1]["op"] == "sketch_convert" && ops[1]["refs"][0] == refB);
        CHECK(ops[2]["constraints"][0]["refs"] == json::parse(R"(["$0.p","$self.p1"])"));
        CHECK(ops[2]["constraints"][1]["refs"] == json::parse(R"(["$self.p2","$1"])"));
        CHECK(ops[2]["constraints"][2]["refs"] == json::parse(R"(["$self"])"));
        CHECK(ops[3]["constraint"]["refs"] == json::parse(R"(["$2","$1"])"));   // "$0" -> "$2"
        CHECK(!ui::modelref::uses(ops));
        const json idMap = {{"$0", "e11"}, {"$1", "e12"}, {"$2", "e13"}, {"$3", "c9"}, {"$2#1", "e14"}};
        const json restored = ui::modelref::restoreIdMap(idMap, shift);
        CHECK(restored == json::parse(R"({"$0":"e13","$1":"c9","$0#1":"e14"})"));
    }
    {   // refA is already a converted external reference on the sketch: bind to its entity, no convert
        json ops = batch;
        const int shift = ui::modelref::bind(ops, "f2", {refA, refB}, [&](const json& r) {
            return r == refA ? std::string("e7") : std::string();
        });
        CHECK(shift == 1 && ops.size() == 3);
        CHECK(ops[0]["op"] == "sketch_convert" && ops[0]["refs"][0] == refB);
        CHECK(ops[1]["constraints"][0]["refs"] == json::parse(R"(["e7.p","$self.p1"])"));
        CHECK(ops[1]["constraints"][1]["refs"] == json::parse(R"(["$self.p2","$0"])"));
        CHECK(ops[2]["constraint"]["refs"] == json::parse(R"(["$1","$0"])"));
    }
    {   // everything already converted: literal ids, batch untouched otherwise
        json ops = batch;
        const int shift = ui::modelref::bind(ops, "f2", {refA, refB}, [&](const json& r) {
            return r == refA ? std::string("e7") : std::string("e8");
        });
        CHECK(shift == 0 && ops.size() == 2);
        CHECK(ops[0]["constraints"][1]["refs"] == json::parse(R"(["$self.p2","e8"])"));
        CHECK(ops[1]["constraint"]["refs"] == json::parse(R"(["$0","e8"])"));
        const json same = {{"$0", "e1"}};
        CHECK(ui::modelref::restoreIdMap(same, 0) == same);
    }

    const json tree = json::parse(R"({"features":[
      {"id":"f1","type":"box"},
      {"id":"f2","type":"sketch","params":{"external_refs":[
        {"id":"x1","source":{"kind":"vertex","selector":{"sid":"v:a"}},"entity_ids":["e3"],"stale":false},
        {"id":"x2","source":{"kind":"edge","selector":{"sid":"e:b"}},"entity_ids":["e4"],"stale":true}]}}]})");
    CHECK(ui::modelref::convertedEntity(tree, "f2", refA) == "e3");
    CHECK(ui::modelref::convertedEntity(tree, "f2", refB).empty());   // stale: re-convert instead
    CHECK(ui::modelref::convertedEntity(tree, "f2", json{{"kind", "vertex"}}).empty());
    CHECK(ui::modelref::convertedEntity(tree, "f9", refA).empty());

    if (g_failures) {
        std::printf("test_sketch_model_snap: %d failure(s)\n", g_failures);
        return 1;
    }
    std::printf("test_sketch_model_snap: ok\n");
    return 0;
}
