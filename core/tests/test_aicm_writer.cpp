// Standalone (OCCT-free) test of the AICM v1 writer geom/aicm.cpp. Hand-builds a
// tiny AicmBody, serializes it, and round-trips through the HOST loader
// (render::parseAicm) to prove byte compatibility without needing the geometry
// kernel. Runs in the default CHUDWARE_WITH_OCCT=OFF build.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cstdint>
#include <string>
#include <vector>

#include "geom/aicm.h"
#include "aicm_loader.h"  // host loader (host/src/render on the include path)

namespace {

// A minimal valid body: a single triangle with one face, one 2-point edge, and
// three B-rep vertices. Exercises every chunk encoder.
aicm::AicmBody tiny_body() {
    aicm::AicmBody b;
    b.body_id = 7;
    b.name = "Tiny";
    b.color[0] = 0.1f;
    b.color[1] = 0.2f;
    b.color[2] = 0.3f;
    b.metallic = 0.25f;
    b.roughness = 0.75f;

    b.pos = {0, 0, 0, 1, 0, 0, 0, 1, 0};
    b.nrm = {0, 0, 1, 0, 0, 1, 0, 0, 1};
    b.idx = {0, 1, 2};

    b.faces.push_back({1, 0, 1, 0});  // face_id 1, tri 0, 1 tri, plane

    b.edges.push_back({1, 0, 2, 0});  // edge_id 1, first_pt 0, 2 pts, line
    b.edge_pts = {0, 0, 0, 1, 0, 0};

    b.verts.push_back({1, {0, 0, 0}});
    b.verts.push_back({2, {1, 0, 0}});
    b.verts.push_back({3, {0, 1, 0}});
    return b;
}

}  // namespace

TEST_CASE("aicm writer round-trips through the host loader") {
    std::vector<aicm::AicmBody> bodies{tiny_body()};
    std::vector<uint8_t> bytes = aicm::write_aicm(bodies);

    render::DocMesh doc;
    std::string err;
    REQUIRE_MESSAGE(render::parseAicm(bytes.data(), bytes.size(), doc, err),
                    "host loader rejected AICM: " << err);

    CHECK(doc.version == 1);
    REQUIRE(doc.bodies.size() == 1);
    const render::BodyMesh& b = doc.bodies[0];

    CHECK(b.body_id == 7);
    CHECK(b.name == "Tiny");
    CHECK(b.color[0] == doctest::Approx(0.1f));
    CHECK(b.color[1] == doctest::Approx(0.2f));
    CHECK(b.color[2] == doctest::Approx(0.3f));
    CHECK(b.metallic == doctest::Approx(0.25f));
    CHECK(b.roughness == doctest::Approx(0.75f));

    CHECK(b.n_verts == 3);
    CHECK(b.n_tris == 1);
    CHECK(b.pos.size() == 9);
    CHECK(b.nrm.size() == 9);
    CHECK(b.idx.size() == 3);

    REQUIRE(b.faces.size() == 1);
    CHECK(b.faces[0].face_id == 1);
    CHECK(b.faces[0].first_tri == 0);
    CHECK(b.faces[0].tri_count == 1);
    CHECK(b.faces[0].surf_type == 0);

    REQUIRE(b.edges.size() == 1);
    CHECK(b.edges[0].edge_id == 1);
    CHECK(b.edges[0].pt_count == 2);
    CHECK(b.edge_pts.size() == 6);

    REQUIRE(b.brep_verts.size() == 3);
    CHECK(b.brep_verts[2].vertex_id == 3);
    CHECK(b.brep_verts[2].xyz[1] == doctest::Approx(1.0f));
}

TEST_CASE("aicm writer is deterministic") {
    std::vector<aicm::AicmBody> bodies{tiny_body()};
    CHECK(aicm::write_aicm(bodies) == aicm::write_aicm(bodies));
}

TEST_CASE("multiple bodies produce 4 chunks each") {
    std::vector<aicm::AicmBody> bodies{tiny_body(), tiny_body()};
    bodies[1].body_id = 9;
    bodies[1].name = "Tiny2";
    std::vector<uint8_t> bytes = aicm::write_aicm(bodies);

    render::DocMesh doc;
    std::string err;
    REQUIRE(render::parseAicm(bytes.data(), bytes.size(), doc, err));
    REQUIRE(doc.bodies.size() == 2);
    CHECK(doc.bodies[0].body_id == 7);
    CHECK(doc.bodies[1].body_id == 9);
    CHECK(doc.bodies[1].name == "Tiny2");
}
