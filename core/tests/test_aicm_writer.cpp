// Standalone (OCCT-free) test of the AICM v1 writer geom/aicm.cpp. Hand-builds a
// tiny AicmBody, serializes it, and round-trips through the shared viewport loader
// (render::parseAicm) to prove byte compatibility without needing the geometry
// kernel. Runs in the default CHUDWARE_WITH_OCCT=OFF build.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <cstring>
#include <limits>
#include <cstdint>
#include <string>
#include <vector>

#include "geom/aicm.h"
#include "aicm_loader.h"
#include "util/aicm_handoff.h"

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

uint64_t read_le(const std::vector<uint8_t>& bytes, size_t offset, size_t width) {
    uint64_t value = 0;
    for (size_t i = 0; i < width; ++i) value |= uint64_t(bytes.at(offset + i)) << (8 * i);
    return value;
}

void set_le(std::vector<uint8_t>& bytes, size_t offset, uint64_t value, size_t width) {
    for (size_t i = 0; i < width; ++i) bytes.at(offset + i) = uint8_t(value >> (8 * i));
}

std::vector<uint8_t> chunk_named(const std::vector<uint8_t>& bytes, const char* fourcc) {
    for (size_t offset = 12; offset + 12 <= bytes.size();) {
        const size_t end = offset + 12 + static_cast<size_t>(read_le(bytes, offset + 4, 8));
        REQUIRE(end <= bytes.size());
        if (std::memcmp(bytes.data() + offset, fourcc, 4) == 0)
            return {bytes.begin() + offset, bytes.begin() + end};
        offset = end;
    }
    FAIL("missing chunk " << fourcc);
    return {};
}

aicm::AicmBody region_body() {
    auto body = tiny_body();
    body.idx = {0, 1, 2, 2, 1, 0};
    // An untessellated face still owns its stable ID and material.
    body.faces = {{1, 0, 1, 0}, {2, 1, 0, 0}, {3, 1, 1, 0}};
    body.appearance_regions = {
        {1, 2, {0.8f, 0.1f, 0.2f}, 0.9f, 0.15f},
        {3, 1, {0.2f, 0.6f, 0.9f}, 0.0f, 0.8f},
    };
    return body;
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
    CHECK(b.appearance_regions.empty());

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

TEST_CASE("multiple bodies retain their identities") {
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

TEST_CASE("appearance regions round-trip without changing topology or body fallback") {
    auto body = region_body();
    auto plain = tiny_body();
    plain.body_id = 42;
    const auto bytes = aicm::write_aicm({body, plain});
    render::DocMesh doc;
    std::string err;
    REQUIRE_MESSAGE(render::parseAicm(bytes.data(), bytes.size(), doc, err), err);
    REQUIRE(doc.bodies.size() == 2);
    const auto& decoded = doc.bodies[0];
    CHECK(decoded.body_id == body.body_id);
    CHECK(decoded.idx == body.idx.vec());
    REQUIRE(decoded.faces.size() == body.faces.size());
    for (size_t i = 0; i < body.faces.size(); ++i) {
        CHECK(decoded.faces[i].face_id == body.faces[i].face_id);
        CHECK(decoded.faces[i].first_tri == body.faces[i].first_tri);
        CHECK(decoded.faces[i].tri_count == body.faces[i].tri_count);
    }
    REQUIRE(decoded.appearance_regions.size() == body.appearance_regions.size());
    for (size_t i = 0; i < body.appearance_regions.size(); ++i) {
        const auto& got = decoded.appearance_regions[i];
        const auto& expected = body.appearance_regions[i];
        CHECK(got.first_face == expected.first_face);
        CHECK(got.face_count == expected.face_count);
        for (size_t c = 0; c < 3; ++c) CHECK(got.color[c] == expected.color[c]);
        CHECK(got.metallic == expected.metallic);
        CHECK(got.roughness == expected.roughness);
    }
    for (size_t c = 0; c < 3; ++c) CHECK(decoded.color[c] == body.color[c]);
    CHECK(decoded.metallic == body.metallic);
    CHECK(decoded.roughness == body.roughness);
    CHECK(doc.bodies[1].body_id == 42);
    CHECK(doc.bodies[1].appearance_regions.empty());

    // Older readers can skip APRG and consume precisely the original BODY payload.
    body.appearance_regions.clear();
    CHECK(chunk_named(bytes, "BODY") == chunk_named(aicm::write_aicm({body}), "BODY"));
}

TEST_CASE("appearance chunks bind by body ID independent of chunk order") {
    auto body = region_body();
    body.appearance_regions.erase(body.appearance_regions.begin()); // partial coverage is valid
    auto bytes = aicm::write_aicm({body});
    const auto region = chunk_named(bytes, "APRG");
    std::rotate(bytes.begin() + 12, bytes.end() - region.size(), bytes.end());
    // A future unknown chunk must not disturb body association.
    std::vector<uint8_t> unknown{'F', 'U', 'T', 'R', 3, 0, 0, 0, 0, 0, 0, 0, 9, 8, 7};
    bytes.insert(bytes.end(), unknown.begin(), unknown.end());
    set_le(bytes, 8, read_le(bytes, 8, 4) + 1, 4);
    render::DocMesh doc;
    std::string err;
    REQUIRE_MESSAGE(render::parseAicm(bytes.data(), bytes.size(), doc, err), err);
    REQUIRE(doc.bodies.size() == 1);
    REQUIRE(doc.bodies[0].appearance_regions.size() == 1);
    CHECK(doc.bodies[0].appearance_regions[0].first_face == 3);
    CHECK(doc.bodies[0].color[0] == body.color[0]);
    CHECK(doc.bodies[0].metallic == body.metallic);
}

TEST_CASE("malformed appearance payloads are refused") {
    auto body = region_body();
    auto chunk = chunk_named(aicm::write_aicm({body}), "APRG");
    body.appearance_regions.clear();
    std::vector<std::vector<uint8_t>> extras;

    SUBCASE("truncated chunk header") { chunk.resize(10); }
    SUBCASE("truncated region header") { chunk.resize(16); set_le(chunk, 4, 4, 8); }
    SUBCASE("record overruns its chunk despite later file bytes") {
        chunk.pop_back();
        set_le(chunk, 4, chunk.size() - 12, 8);
        extras.push_back(std::vector<uint8_t>(128, 0));
    }
    SUBCASE("unbounded record count") { set_le(chunk, 16, UINT32_MAX, 4); }
    SUBCASE("overflowing chunk length") { set_le(chunk, 4, UINT64_MAX, 8); }
    SUBCASE("extra record bytes") {
        chunk.push_back(0);
        set_le(chunk, 4, chunk.size() - 12, 8);
    }
    SUBCASE("zero first face") { set_le(chunk, 20, 0, 4); }
    SUBCASE("empty face span") { set_le(chunk, 24, 0, 4); }
    SUBCASE("overflowing face span") { set_le(chunk, 20, UINT32_MAX, 4); }
    SUBCASE("span outside body faces") { set_le(chunk, 24, 4, 4); }
    SUBCASE("missing face inside span") { body.faces[1].face_id = 4; }
    SUBCASE("unknown body") { set_le(chunk, 12, 999, 4); }
    SUBCASE("overlapping regions") { set_le(chunk, 48, 2, 4); }
    SUBCASE("duplicate region chunk") { extras.push_back(chunk); }
    SUBCASE("duplicate face identity") { body.faces[1].face_id = 1; }

    extras.insert(extras.begin(), chunk);
    const auto bytes = aicm::write_aicm({body}, extras);
    render::DocMesh doc;
    std::string err;
    CHECK_FALSE(render::parseAicm(bytes.data(), bytes.size(), doc, err));
}

TEST_CASE("appearance material channels reject nonfinite and out-of-range values") {
    auto body = region_body();
    const auto valid_chunk = chunk_named(aicm::write_aicm({body}), "APRG");
    body.appearance_regions.clear();
    for (size_t channel = 0; channel < 5; ++channel) {
        for (float value : {-0.01f, 1.01f, std::numeric_limits<float>::infinity(),
                            std::numeric_limits<float>::quiet_NaN()}) {
            auto chunk = valid_chunk;
            uint32_t bits;
            std::memcpy(&bits, &value, sizeof(bits));
            set_le(chunk, 28 + 4 * channel, bits, 4);
            const auto bytes = aicm::write_aicm({body}, {chunk});
            render::DocMesh doc;
            std::string err;
            CHECK_FALSE(render::parseAicm(bytes.data(), bytes.size(), doc, err));
        }
    }
}

TEST_CASE("mapped mesh survives replacement and release of its publication") {
    using namespace chudware::aicm_handoff;
    const std::string key = "test_aicm_retained_reader";
    const auto original = aicm::write_aicm({tiny_body()});
    auto replacement_body = tiny_body();
    replacement_body.name = "Replacement";
    const auto replacement = aicm::write_aicm({replacement_body});
    struct Reader {
        Mapping mapping;
        ~Reader() { unmap(mapping); }
    } old_reader, new_reader;
    struct Cleanup {
        std::string key;
        ~Cleanup() { release(key); }
    } cleanup{key};
    std::string error;
    REQUIRE(map(publish(key, original), old_reader.mapping, error));
    REQUIRE(map(publish(key, replacement), new_reader.mapping, error));
    release(key);
    render::DocMesh old_mesh, new_mesh;
    REQUIRE(render::parseAicm(old_reader.mapping.data, old_reader.mapping.size, old_mesh, error));
    REQUIRE(render::parseAicm(new_reader.mapping.data, new_reader.mapping.size, new_mesh, error));
    REQUIRE(old_mesh.bodies.size() == 1);
    REQUIRE(new_mesh.bodies.size() == 1);
    CHECK(old_mesh.bodies[0].name == "Tiny");
    CHECK(new_mesh.bodies[0].name == "Replacement");
}
