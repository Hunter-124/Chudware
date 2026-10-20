// Standalone parser regressions: no daemon, sewing, providers, or production limit override.
// Including the implementation gives these tiny fixtures private, lowered admission budgets.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>
#include "../doc/solidworks_tessellation.cpp"

#include <chrono>

namespace {
using namespace chudware::doc;

void u32(std::string& out, std::uint32_t value) {
    for (int i = 0; i < 4; ++i) out.push_back(static_cast<char>(value >> (i * 8)));
}

std::string chunk(const std::string& name, const std::string& raw,
                  std::optional<std::uint32_t> claim = std::nullopt) {
    mz_stream stream{};
    REQUIRE(mz_deflateInit2(&stream, MZ_BEST_COMPRESSION, MZ_DEFLATED,
                           -MZ_DEFAULT_WINDOW_BITS, 8, MZ_DEFAULT_STRATEGY) == MZ_OK);
    std::string compressed(mz_compressBound(static_cast<mz_ulong>(raw.size())), '\0');
    stream.next_in = reinterpret_cast<const unsigned char*>(raw.data());
    stream.avail_in = static_cast<unsigned>(raw.size());
    stream.next_out = reinterpret_cast<unsigned char*>(compressed.data());
    stream.avail_out = static_cast<unsigned>(compressed.size());
    const int result = mz_deflate(&stream, MZ_FINISH);
    const auto size = stream.total_out;
    mz_deflateEnd(&stream);
    REQUIRE(result == MZ_STREAM_END);
    compressed.resize(size);
    std::string out(4, '\0');
    out.append("\x14\0\x06\0\x08\0", 6);
    out.append("\xfd\0\0\0", 4);
    u32(out, 65536);
    u32(out, static_cast<std::uint32_t>(compressed.size()));
    u32(out, claim.value_or(static_cast<std::uint32_t>(raw.size())));
    u32(out, static_cast<std::uint32_t>(name.size()));
    for (unsigned char c : name) out.push_back(static_cast<char>((c << 4) | (c >> 4)));
    out += compressed;
    return out;
}

std::string container(const std::string& chunks) {
    std::string out(15, '\0');
    out[7] = 4;
    return out + chunks;
}

struct Fixture {
    std::filesystem::path directory;
    std::filesystem::path path;
    explicit Fixture(const std::string& data, bool assembly = true) {
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        directory = std::filesystem::temp_directory_path() /
                    ("chudware-solidworks-bounds-" + std::to_string(stamp));
        REQUIRE(std::filesystem::create_directory(directory));
        path = directory / (assembly ? "fixture.sldasm" : "fixture.sldprt");
        std::ofstream out(path, std::ios::binary);
        out.write(data.data(), static_cast<std::streamsize>(data.size()));
        REQUIRE(out.good());
    }
    ~Fixture() {
        std::error_code ignored;
        std::filesystem::remove_all(directory, ignored);
    }
};

std::string tetra() {
    constexpr float s = 0.01f;
    const std::array<float, 3> a{0, 0, 0}, b{s, 0, 0}, c{0, s, 0}, d{0, 0, s};
    const std::array<std::array<std::array<float, 3>, 3>, 4> faces{{
        {{a, c, b}}, {{a, b, d}}, {{a, d, c}}, {{b, c, d}}}};
    std::string out;
    for (const auto& face : faces) {
        for (unsigned value : {4u, 8u, 2u, 1u, 3u}) u32(out, value);
        for (int array = 0; array < 2; ++array) {
            for (unsigned value : {12u, 100u, 2u, 3u}) u32(out, value);
            for (const auto& point : face) {
                for (float value : point) {
                    std::uint32_t bits = 0;
                    std::memcpy(&bits, &value, sizeof(bits));
                    u32(out, bits);
                }
            }
        }
        for (int array = 0; array < 2; ++array)
            for (unsigned value : {12u, 100u, 2u, 0u}) u32(out, value);
    }
    return out;
}

std::string block() {
    std::string out;
    u32(out, 0);
    out += "TETRA-ID";
    for (unsigned value : {4u, 0u, 0u, 1u, 0u, 0u, 0x00201896u, 0u, 0u, 0u, 0u})
        u32(out, value);
    return out + tetra();
}

std::string reference(const std::string& target, int number, bool shifted = false,
                      bool suppressed = false) {
    return "<swReference swName=\"Tetra\" swReferenceNumber=\"" + std::to_string(number) +
           "\" swModelRef=\"" + target + "\" swSuppressed=\"" + (suppressed ? "YES" : "NO") +
           "\" swTransform=\"1 0 0 0 0 1 0 0 0 0 1 0 " + (shifted ? "0.02" : "0") +
           " 0 0 1\"/>";
}

std::string graph_xml(int levels, bool branching = true, bool cycle = false,
                      bool suppressed = false) {
    std::string xml = "<swSolidWorks><swHeader><swFile id=\"pfile\" swDocType=\"PART\"/>"
                      "<swFile id=\"afile\" swDocType=\"ASSEMBLY\"/></swHeader><swModelList>"
                      "<swModel id=\"part\" swFileRef=\"pfile\"/>";
    for (int i = 0; i < levels; ++i) {
        const std::string next = i + 1 < levels ? "a" + std::to_string(i + 1)
                                               : cycle ? "a0" : "part";
        xml += "<swModel id=\"a" + std::to_string(i) + "\" swFileRef=\"afile\">";
        xml += reference(next, 1);
        if (branching) xml += reference(next, 2, true, suppressed);
        xml += "</swModel>";
    }
    return xml + "</swModelList><swConfiguration swModelRef=\"a0\" "
                 "swMostRecentConfiguration=\"YES\"/></swSolidWorks>";
}

std::string assembly_data(int levels, bool branching = true, bool cycle = false,
                          bool suppressed = false) {
    return container(chunk("FaceTessellations/000", block()) +
                     chunk("swXmlContents/COMPINSTANCETREE",
                           graph_xml(levels, branching, cycle, suppressed)));
}
}  // namespace

TEST_CASE("SolidWorks stream admission uses aggregate bytes and counts") {
    Fixture source(container(chunk("FaceTessellations/one", std::string(64, 'a')) +
                             chunk("FaceTessellations/two", std::string(64, 'b'))));
    Limits limits;
    limits.stream_bytes = 128;
    limits.streams = 2;
    const auto streams = read_modern_streams(source.path.string(), true, limits);
    REQUIRE(streams.size() == 2);
    CHECK(streams.at("FaceTessellations/one").size() == 64);
    CHECK(streams.at("FaceTessellations/two").size() == 64);
    limits.stream_bytes = 127;
    CHECK_THROWS_WITH(read_modern_streams(source.path.string(), true, limits),
                      "SolidWorks aggregate expanded byte budget exceeded");
    limits.stream_bytes = 128;
    limits.streams = 1;
    CHECK_THROWS_WITH(read_modern_streams(source.path.string(), true, limits),
                      "SolidWorks stream count budget exceeded");
    limits.streams = 2;
    limits.file_bytes = std::filesystem::file_size(source.path);
    CHECK_NOTHROW(read_modern_streams(source.path.string(), true, limits));
    --limits.file_bytes;
    CHECK_THROWS_WITH(read_modern_streams(source.path.string(), true, limits),
                      "SolidWorks input file byte budget exceeded");
}

TEST_CASE("SolidWorks false expanded sizes never inflate without a destination bound") {
    for (std::uint32_t claim : {0u, 1u, 4095u, 4097u}) {
        Fixture source(container(chunk("Contents/DisplayLists", std::string(4096, 'A'), claim)), false);
        CHECK_THROWS(read_modern_streams(source.path.string(), false, Limits{}));
    }
    Fixture valid(container(chunk("Contents/DisplayLists", std::string(4096, 'A'))), false);
    const auto streams = read_modern_streams(valid.path.string(), false, Limits{});
    CHECK(streams.at("Contents/DisplayLists").size() == 4096);
}


TEST_CASE("SolidWorks bounded inflation preserves chunk-sized valid output") {
    for (std::size_t size : {4096u, 65535u, 65536u, 65537u, 131073u}) {
        std::string payload(size, '\0');
        for (std::size_t i = 0; i < size; ++i) payload[i] = static_cast<char>(i % 256);
        Fixture source(container(chunk("Contents/DisplayLists", payload)), false);
        const auto streams = read_modern_streams(source.path.string(), false, Limits{});
        const auto& raw = streams.at("Contents/DisplayLists");
        CHECK(std::string(raw.begin(), raw.end()) == payload);
    }
}
TEST_CASE("SolidWorks duplicate streams and empty streams cannot bypass admission") {
    const std::string one = chunk("FaceTessellations/one", std::string(64, 'a'));
    Fixture duplicate(container(one + one));
    Limits limits;
    limits.streams = 1;
    limits.stream_bytes = 64;
    CHECK(read_modern_streams(duplicate.path.string(), true, limits).size() == 1);
    // Valid inline placeholders have no compressed payload, but still occupy a retained slot.
    std::string empty = chunk("FaceTessellations/empty", "");
    empty.resize(30 + std::string("FaceTessellations/empty").size());
    std::fill(empty.begin() + 18, empty.begin() + 22, '\0');
    Fixture placeholder(container(one + empty));
    CHECK_THROWS_WITH(read_modern_streams(placeholder.path.string(), true, limits),
                      "SolidWorks stream count budget exceeded");
}

TEST_CASE("SolidWorks branching assembly observes exact placed geometry boundaries") {
    Fixture source(assembly_data(3));  // Eight leaves, fourteen scanned references.
    Limits limits;
    limits.instances = 8;
    limits.work = 14;
    limits.vertices = 96;
    limits.triangles = 32;
    limits.mesh_bytes = 96 * 24 + 32 * 12;
    const auto bodies = read_solidworks_tessellation_impl(source.path.string(), true, limits);
    REQUIRE(bodies.size() == 8);
    CHECK(bodies.front().mesh->vertex_count() == 12);
    CHECK(bodies.front().mesh->triangle_count() == 4);
    CHECK(bodies.front().mesh == bodies.back().mesh);
    CHECK(bodies.front().transform[12] == doctest::Approx(0.0));
    CHECK(bodies.back().transform[12] == doctest::Approx(0.06));
    CHECK(bodies.front().name == "Tetra-1/Tetra-1/Tetra-1");
    SUBCASE("instance count") {
        limits.instances = 7;
        CHECK_THROWS_WITH(read_solidworks_tessellation_impl(source.path.string(), true, limits),
                          "SolidWorks instance count budget exceeded");
    }
    SUBCASE("work count") {
        limits.work = 13;
        CHECK_THROWS_WITH(read_solidworks_tessellation_impl(source.path.string(), true, limits),
                          "SolidWorks instance work budget exceeded");
    }
    SUBCASE("aggregate vertices") {
        limits.vertices = 95;
        CHECK_THROWS_WITH(read_solidworks_tessellation_impl(source.path.string(), true, limits),
                          "SolidWorks aggregate vertex budget exceeded");
    }
    SUBCASE("aggregate triangles") {
        limits.triangles = 31;
        CHECK_THROWS_WITH(read_solidworks_tessellation_impl(source.path.string(), true, limits),
                          "SolidWorks aggregate triangle budget exceeded");
    }
    SUBCASE("aggregate mesh bytes") {
        --limits.mesh_bytes;
        CHECK_THROWS_WITH(read_solidworks_tessellation_impl(source.path.string(), true, limits),
                          "SolidWorks aggregate mesh byte budget exceeded");
    }
}

TEST_CASE("SolidWorks compact excessive DAG fails before materializing its leaves") {
    Fixture source(assembly_data(20));  // Tiny source, over a million paths; never expand it.
    CHECK_THROWS_WITH(read_solidworks_tessellation(source.path.string(), true),
                      "SolidWorks instance count budget exceeded");
}

TEST_CASE("SolidWorks cycle depth and suppression guards survive DAG admission") {
    Fixture cyclic(assembly_data(2, false, true));
    CHECK_THROWS_WITH(read_solidworks_tessellation(cyclic.path.string(), true),
                      "SolidWorks assembly component graph contains a cycle");
    Fixture deep(assembly_data(66, false));
    CHECK_THROWS_WITH(read_solidworks_tessellation(deep.path.string(), true),
                      "SolidWorks assembly nesting exceeds 64 levels");
    Fixture boundary(assembly_data(65, false));
    CHECK(read_solidworks_tessellation(boundary.path.string(), true).size() == 1);
    Fixture suppressed(assembly_data(3, true, false, true));
    CHECK(read_solidworks_tessellation(suppressed.path.string(), true).size() == 1);
}

TEST_CASE("SolidWorks memoized models retain the depth guard on longer paths") {
    std::string xml = graph_xml(65, false);
    const std::string configuration = "<swConfiguration swModelRef=\"a0\"";
    xml.replace(xml.find(configuration), configuration.size(),
                "<swConfiguration swModelRef=\"root\"");
    const std::string root = "<swModel id=\"root\" swFileRef=\"afile\">" +
                             reference("a60", 1) + reference("a0", 2) + "</swModel>";
    xml.insert(xml.find("</swModelList>"), root);
    Fixture source(container(chunk("FaceTessellations/000", block()) +
                             chunk("swXmlContents/COMPINSTANCETREE", xml)));
    CHECK_THROWS_WITH(read_solidworks_tessellation(source.path.string(), true),
                      "SolidWorks assembly nesting exceeds 64 levels");
}

TEST_CASE("SolidWorks public portable import preserves bounded branching placements") {
    Fixture part_source(container(chunk("Contents/DisplayLists", tetra())), false);
    const auto part = read_solidworks_tessellation(part_source.path.string(), false);
    REQUIRE(part.size() == 1);
    CHECK(part.front().mesh->vertex_count() == 12);
    CHECK(part.front().mesh->triangle_count() == 4);
    Fixture assembly_source(assembly_data(3));
    const auto bodies = read_solidworks_tessellation(assembly_source.path.string(), true);
    REQUIRE(bodies.size() == 8);
    CHECK(bodies.front().name == "Tetra-1/Tetra-1/Tetra-1");
    CHECK(bodies.front().mesh->triangle_count() == 4);
    CHECK(bodies.back().transform[12] == doctest::Approx(0.06));
    CHECK(bodies.front().mesh == bodies.back().mesh);
}

TEST_CASE("SolidWorks distinct source meshes share the aggregate geometry budget") {
    std::string second = block();
    second[0] = 1;
    const std::string xml = "<swSolidWorks><swFile id=\"pfile\" swDocType=\"PART\"/>"
        "<swFile id=\"afile\" swDocType=\"ASSEMBLY\"/>"
        "<swModel id=\"p1\" swFileRef=\"pfile\"/><swModel id=\"p2\" swFileRef=\"pfile\"/>"
        "<swModel id=\"root\" swFileRef=\"afile\">" + reference("p1", 1) + reference("p2", 2) +
        "</swModel><swConfiguration swModelRef=\"root\"/></swSolidWorks>";
    std::string face_chunks;
    SUBCASE("two blocks in one stream") {
        face_chunks = chunk("FaceTessellations/000", block() + second);
    }
    SUBCASE("two independent streams") {
        face_chunks = chunk("FaceTessellations/000", block()) +
                      chunk("FaceTessellations/001", block());
    }
    Fixture source(container(face_chunks + chunk("swXmlContents/COMPINSTANCETREE", xml)));
    Limits limits;
    limits.vertices = 24;
    CHECK(read_solidworks_tessellation_impl(source.path.string(), true, limits).size() == 2);
    limits.vertices = 23;
    CHECK_THROWS_WITH(read_solidworks_tessellation_impl(source.path.string(), true, limits),
                      "SolidWorks aggregate vertex budget exceeded");
}

TEST_CASE("SolidWorks audit witnesses reject within small aggregate and DAG budgets") {
    Fixture streams(container(chunk("FaceTessellations/one", std::string(1024 * 1024, 'a')) +
                              chunk("FaceTessellations/two", std::string(1024 * 1024, 'b'))));
    Limits limits;
    limits.stream_bytes = 2 * 1024 * 1024;
    const auto admitted = read_modern_streams(streams.path.string(), true, limits);
    REQUIRE(admitted.size() == 2);
    CHECK(admitted.at("FaceTessellations/one").size() +
          admitted.at("FaceTessellations/two").size() == limits.stream_bytes);
    --limits.stream_bytes;
    CHECK_THROWS_WITH(read_modern_streams(streams.path.string(), true, limits),
                      "SolidWorks aggregate expanded byte budget exceeded");

    Fixture dag(assembly_data(8));
    limits = Limits{};
    limits.instances = 256;
    CHECK(read_solidworks_tessellation_impl(dag.path.string(), true, limits).size() == 256);
    --limits.instances;
    CHECK_THROWS_WITH(read_solidworks_tessellation_impl(dag.path.string(), true, limits),
                      "SolidWorks instance count budget exceeded");
}
