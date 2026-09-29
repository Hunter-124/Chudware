// doc_view_metrics (ViewNet shot planner input) + doc_render_view `view.section`, driven through
// the C ABI against a real holed-box session doc. Asserts the invariants the planner consumes:
// facing vs back faces, true visible area against the analytic face areas, principal-axis views
// scoring low normal entropy, B-rep edge visibility/silhouettes, section caps, and that a
// section render changes pixels while an absent/null section leaves them byte-identical.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <map>
#include <string>
#include <utility>
#include <vector>

#include <miniz.h>
#include <nlohmann/json.hpp>

#include "api/libchudware.h"

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace {

constexpr double kPi = 3.14159265358979323846;

json rpc(const char* method, const json& params = json::object()) {
    char* raw = nullptr;
    const std::string encoded = params.dump();
    const int rc = chudware_call(method, encoded.c_str(), &raw);
    INFO("method=" << method << " rc=" << rc << " response=" << (raw ? raw : "<null>"));
    REQUIRE(rc == 0);
    REQUIRE(raw != nullptr);
    json result = json::parse(raw);
    chudware_free(raw);
    return result;
}

std::string tmp_png(const char* tag) {
    static int sequence = 0;
    return (fs::temp_directory_path() /
            ("chudware_view_metrics_" + std::string(tag) + "_" + std::to_string(sequence++) +
             ".png"))
        .string();
}

std::vector<unsigned char> read_bytes(const std::string& path) {
    std::ifstream stream(path, std::ios::binary);
    return std::vector<unsigned char>(std::istreambuf_iterator<char>(stream),
                                      std::istreambuf_iterator<char>());
}

uint32_t be32(const unsigned char* p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

struct Rgb {
    int width = 0;
    int height = 0;
    std::vector<unsigned char> pixels;
    bool ok() const {
        return width > 0 && height > 0 &&
               pixels.size() == static_cast<size_t>(width) * height * 3;
    }
};

Rgb decode_png(const std::string& path) {
    Rgb out;
    const std::vector<unsigned char> bytes = read_bytes(path);
    if (bytes.size() < 8) return out;
    size_t offset = 8;
    std::string idat;
    int bit_depth = 0;
    int color_type = -1;
    while (offset + 8 <= bytes.size()) {
        const uint32_t length = be32(bytes.data() + offset);
        if (offset + 12 + length > bytes.size()) return Rgb{};
        const std::string tag(reinterpret_cast<const char*>(bytes.data() + offset + 4), 4);
        const size_t data_offset = offset + 8;
        if (tag == "IHDR" && length >= 13) {
            out.width = static_cast<int>(be32(bytes.data() + data_offset));
            out.height = static_cast<int>(be32(bytes.data() + data_offset + 4));
            bit_depth = bytes[data_offset + 8];
            color_type = bytes[data_offset + 9];
        } else if (tag == "IDAT") {
            idat.append(reinterpret_cast<const char*>(bytes.data() + data_offset), length);
        } else if (tag == "IEND") {
            break;
        }
        offset = data_offset + length + 4;
    }
    if (out.width <= 0 || out.height <= 0 || bit_depth != 8 || color_type != 2) return Rgb{};
    const size_t stride = 1 + static_cast<size_t>(out.width) * 3;
    std::vector<unsigned char> raw(stride * out.height);
    mz_ulong raw_length = static_cast<mz_ulong>(raw.size());
    if (mz_uncompress(raw.data(), &raw_length,
                      reinterpret_cast<const unsigned char*>(idat.data()),
                      static_cast<mz_ulong>(idat.size())) != MZ_OK ||
        raw_length != raw.size())
        return Rgb{};
    out.pixels.resize(static_cast<size_t>(out.width) * out.height * 3);
    for (int y = 0; y < out.height; ++y) {
        if (raw[static_cast<size_t>(y) * stride] != 0) return Rgb{};
        std::copy(raw.begin() + static_cast<size_t>(y) * stride + 1,
                  raw.begin() + static_cast<size_t>(y) * stride + stride,
                  out.pixels.begin() + static_cast<size_t>(y) * out.width * 3);
    }
    return out;
}

json dimension(double value) { return json{{"value", value}, {"expr", nullptr}}; }

// 40 (X) x 30 (Y) x 20 (Z) box centred on the origin with a Ø8 through-hole at (10, 0) from the
// top face. Visible-area references: +X face 600, -Y face 800, +Z face 1200 - 16π.
constexpr double kHoleRadius = 4.0;
const double kTopArea = 40.0 * 30.0 - kPi * kHoleRadius * kHoleRadius;

std::string make_holed_box(const char* name) {
    const json created = rpc("doc_new", json{{"kind", "part"}, {"name", name}});
    const std::string id = created.at("doc_id").get<std::string>();
    const json ops = json::array({
        json{{"op", "add_feature"},
             {"feature",
              {{"type", "box"},
               {"name", "MetricsBox"},
               {"params",
                {{"length", dimension(40)}, {"width", dimension(30)}, {"height", dimension(20)}}}}}},
        json{{"op", "add_feature"},
             {"feature",
              {{"type", "hole"},
               {"name", "MetricsBore"},
               {"params",
                {{"style", "simple"},
                 {"standard", "custom"},
                 {"diameter", dimension(2 * kHoleRadius)},
                 {"end", "through_all"},
                 {"positions", json::array({json::array({10.0, 0.0})})}}},
               {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
    });
    const json applied =
        rpc("doc_apply_ops", json{{"doc_id", id}, {"base_revision", -1}, {"ops", ops}});
    const double expected = 40.0 * 30.0 * 20.0 - kPi * kHoleRadius * kHoleRadius * 20.0;
    REQUIRE_MESSAGE(applied.value("volume", 0.0) == doctest::Approx(expected).epsilon(0.01),
                    applied.dump());
    return id;
}

json camera_at(double x, double y, double z, json up = json::array({0.0, 0.0, 1.0})) {
    return json{{"projection", "perspective"},
                {"position", json::array({x, y, z})},
                {"target", json::array({0.0, 0.0, 0.0})},
                {"up", std::move(up)},
                {"vertical_fov_deg", 30.0},
                {"near", 1.0},
                {"far", 1000.0}};
}

// Three-quarter camera: sees the +X, -Y and +Z faces.
json three_quarter() { return camera_at(120.0, -120.0, 100.0); }
json straight_down() { return camera_at(0.0, 0.0, 150.0, json::array({0.0, 1.0, 0.0})); }

json middle_section() {
    // Keeps y >= 0: the -Y (front) half is removed, so a front camera looks into the cut.
    return json{{"point", json::array({0.0, 0.0, 0.0})},
                {"normal", json::array({0.0, -1.0, 0.0})}};
}

int planar_face(const json& faces, double nx, double ny, double nz) {
    for (size_t i = 0; i < faces.size(); ++i) {
        const json& f = faces[i];
        if (f.at("surf_type").get<int>() != 0) continue;
        const json& n = f.at("normal");
        const double d = n[0].get<double>() * nx + n[1].get<double>() * ny +
                         n[2].get<double>() * nz;
        if (d > 0.999) return static_cast<int>(i);
    }
    return -1;
}

struct FaceVis {
    long visible_px = 0;
    double visible_area = 0.0;
    double mean_cos = 0.0;
    double projected_px = 0.0;
};

std::map<int, FaceVis> face_vis(const json& view) {
    std::map<int, FaceVis> out;
    for (const json& row : view.at("face_vis"))
        out[row[0].get<int>()] = FaceVis{row[1].get<long>(), row[2].get<double>(),
                                         row[3].get<double>(), row[4].get<double>()};
    return out;
}

FaceVis vis_of(const std::map<int, FaceVis>& table, int face) {
    const auto it = table.find(face);
    return it == table.end() ? FaceVis{} : it->second;
}

json metrics(const std::string& id, const json& cameras, int width, int height,
             const json& extra = json::object()) {
    json params{{"doc_id", id}, {"cameras", cameras}, {"width", width}, {"height", height}};
    for (auto it = extra.begin(); it != extra.end(); ++it) params[it.key()] = it.value();
    const json response = rpc("doc_view_metrics", params);
    REQUIRE_MESSAGE(response.value("ok", false), response.dump());
    REQUIRE(response.at("views").size() == cameras.size());
    return response;
}

}  // namespace

TEST_CASE("view metrics: a face toward the camera is visible, one facing away is not") {
    const std::string id = make_holed_box("MetricsFacing");
    const json response =
        metrics(id, json::array({camera_at(0.0, -150.0, 0.0)}), 320, 240);
    const json& faces = response.at("faces");
    const int front = planar_face(faces, 0, -1, 0);
    const int back = planar_face(faces, 0, 1, 0);
    REQUIRE(front >= 0);
    REQUIRE(back >= 0);
    const std::map<int, FaceVis> vis = face_vis(response.at("views")[0]);
    CHECK(vis_of(vis, front).visible_px > 1000);
    CHECK(vis_of(vis, front).mean_cos > 0.95);
    CHECK(vis_of(vis, back).visible_px == 0);
    CHECK(vis_of(vis, back).projected_px == 0.0);
    // Straight on, the front face is the whole silhouette: its true area is 40 x 20.
    CHECK(vis_of(vis, front).visible_area == doctest::Approx(800.0).epsilon(0.05));
}

TEST_CASE("view metrics: 3/4 view visible area matches the analytic area of the three faces") {
    const std::string id = make_holed_box("MetricsArea");
    const json response = metrics(id, json::array({three_quarter()}), 640, 480);
    const json& faces = response.at("faces");
    const int right = planar_face(faces, 1, 0, 0);
    const int front = planar_face(faces, 0, -1, 0);
    const int top = planar_face(faces, 0, 0, 1);
    const int bottom = planar_face(faces, 0, 0, -1);
    REQUIRE(right >= 0);
    REQUIRE(front >= 0);
    REQUIRE(top >= 0);
    REQUIRE(bottom >= 0);
    CHECK(faces.at(static_cast<size_t>(top)).at("area_mm2").get<double>() ==
          doctest::Approx(kTopArea).epsilon(0.02));

    const std::map<int, FaceVis> vis = face_vis(response.at("views")[0]);
    const double seen = vis_of(vis, right).visible_area + vis_of(vis, front).visible_area +
                        vis_of(vis, top).visible_area;
    CHECK(seen == doctest::Approx(600.0 + 800.0 + kTopArea).epsilon(0.05));
    CHECK(vis_of(vis, right).visible_area == doctest::Approx(600.0).epsilon(0.05));
    CHECK(vis_of(vis, front).visible_area == doctest::Approx(800.0).epsilon(0.05));
    CHECK(vis_of(vis, top).visible_area == doctest::Approx(kTopArea).epsilon(0.05));
    // Unoccluded faces: z-buffer winners match the analytic projected area.
    for (int face : {right, front, top}) {
        const FaceVis v = vis_of(vis, face);
        REQUIRE(v.projected_px > 0.0);
        CHECK(v.visible_px / v.projected_px == doctest::Approx(1.0).epsilon(0.03));
    }
    CHECK(vis_of(vis, bottom).visible_px == 0);

    // Of the four 40 mm X edges, the bottom-back one is hidden, the top-front one is an interior
    // crease, and the top-back / bottom-front ones are silhouettes against the background.
    const json& edges = response.at("edges");
    std::map<int, std::array<double, 4>> edge_rows;
    for (const json& row : response.at("views")[0].at("edge_vis"))
        edge_rows[row[0].get<int>()] = {row[1].get<double>(), row[2].get<double>(),
                                        row[3].get<double>(), row[4].get<double>()};
    int hidden = 0, silhouettes = 0, creases = 0, long_edges = 0;
    for (size_t e = 0; e < edges.size(); ++e) {
        if (std::abs(edges[e].at("length_mm").get<double>() - 40.0) > 0.01) continue;
        ++long_edges;
        const auto it = edge_rows.find(static_cast<int>(e));
        REQUIRE(it != edge_rows.end());
        const auto& row = it->second;
        CHECK(row[1] > 50.0);  // projected length in px
        if (row[0] < 0.15) ++hidden;
        else if (row[0] > 0.9 && row[3] > 0.8) ++silhouettes;
        else if (row[0] > 0.9 && row[3] < 0.2) ++creases;
    }
    CHECK(long_edges == 4);
    CHECK(hidden == 1);
    CHECK(silhouettes == 2);
    CHECK(creases == 1);
}

TEST_CASE("view metrics: straight-down view is principal and low-entropy vs a 3/4 view") {
    const std::string id = make_holed_box("MetricsEntropy");
    const json response =
        metrics(id, json::array({straight_down(), three_quarter()}), 320, 240);
    const json& faces = response.at("faces");
    const json& down = response.at("views")[0];
    const json& oblique = response.at("views")[1];
    const int top = planar_face(faces, 0, 0, 1);
    REQUIRE(top >= 0);

    CHECK(down.at("principal_align").get<double>() == doctest::Approx(1.0).epsilon(1e-6));
    CHECK(oblique.at("principal_align").get<double>() < 0.7);
    const std::map<int, FaceVis> vis = face_vis(down);
    const long fg = down.at("fg_px").get<long>();
    REQUIRE(fg > 0);
    CHECK(vis_of(vis, top).visible_px >= static_cast<long>(0.95 * fg));
    for (const auto& n : std::array<std::array<double, 3>, 5>{
             {{1, 0, 0}, {-1, 0, 0}, {0, 1, 0}, {0, -1, 0}, {0, 0, -1}}}) {
        const int f = planar_face(faces, n[0], n[1], n[2]);
        REQUIRE(f >= 0);
        CHECK(vis_of(vis, f).visible_px == 0);
    }
    CHECK(down.at("normal_entropy").get<double>() < 0.5);
    CHECK(oblique.at("normal_entropy").get<double>() > 1.2);
    CHECK(down.at("fg_px").get<long>() + down.at("bg_px").get<long>() == 320 * 240);
    CHECK(down.at("cap_px").get<long>() == 0);
    CHECK(down.at("depth_edge_px").get<long>() > 0);
}

TEST_CASE("view metrics: a section through the middle yields cap pixels and removes the cut half") {
    const std::string id = make_holed_box("MetricsSection");
    const json cams = json::array({three_quarter(), three_quarter()});
    const json response = metrics(id, cams, 320, 240,
                                  json{{"sections", json::array({nullptr, middle_section()})}});
    const json& faces = response.at("faces");
    const int front = planar_face(faces, 0, -1, 0);
    REQUIRE(front >= 0);
    const json& whole = response.at("views")[0];
    const json& cut = response.at("views")[1];
    CHECK(whole.at("cap_px").get<long>() == 0);
    CHECK(whole.at("section").is_null());
    CHECK(cut.at("cap_px").get<long>() > 500);
    CHECK(cut.at("section").at("normal") == json::array({0.0, -1.0, 0.0}));
    CHECK(vis_of(face_vis(whole), front).visible_px > 0);
    CHECK(vis_of(face_vis(cut), front).visible_px == 0);
    CHECK(vis_of(face_vis(cut), front).projected_px == 0.0);

    // Request-level `section` applies to every camera without a per-camera override.
    const json shared = metrics(id, json::array({three_quarter()}), 320, 240,
                                json{{"section", middle_section()}});
    CHECK(shared.at("views")[0].at("cap_px").get<long>() == cut.at("cap_px").get<long>());
}

TEST_CASE("view metrics: bad input is refused by name") {
    const std::string id = make_holed_box("MetricsRefusal");
    const json degenerate = camera_at(0.0, 0.0, 0.0);
    json r = rpc("doc_view_metrics",
                 json{{"doc_id", id}, {"cameras", json::array({three_quarter(), degenerate})}});
    CHECK_FALSE(r.value("ok", true));
    CHECK(r.value("code", std::string()) == "camera_zero_direction");
    CHECK(r.value("camera_index", -1) == 1);

    r = rpc("doc_view_metrics", json{{"doc_id", id}, {"cameras", json::array()}});
    CHECK_FALSE(r.value("ok", true));
    CHECK(r.value("code", std::string()) == "cameras_invalid");

    r = rpc("doc_view_metrics",
            json{{"doc_id", id},
                 {"cameras", json::array({three_quarter()})},
                 {"section", json{{"point", json::array({0, 0, 0})},
                                  {"normal", json::array({0, 0, 0})}}}});
    CHECK_FALSE(r.value("ok", true));
    CHECK(r.value("code", std::string()) == "section_invalid");

    r = rpc("doc_view_metrics",
            json{{"doc_id", id},
                 {"cameras", json::array({three_quarter()})},
                 {"sections", json::array({nullptr, nullptr})}});
    CHECK_FALSE(r.value("ok", true));
    CHECK(r.value("code", std::string()) == "sections_invalid");
}

TEST_CASE("doc_render_view section: caps and ghosts the cut, absent section stays legacy") {
    const std::string id = make_holed_box("RenderSection");
    auto render = [&](const json& view, const char* tag) {
        const std::string path = tmp_png(tag);
        const json response = rpc("doc_render_view", json{{"doc_id", id},
                                                          {"out_path", path},
                                                          {"view", view},
                                                          {"annotate", false}});
        REQUIRE_MESSAGE(response.value("ok", false), response.dump());
        return std::make_pair(response, read_bytes(path));
    };
    const json exact{{"camera", three_quarter()}, {"width", 320}, {"height", 240}};
    json null_section = exact;
    null_section["section"] = nullptr;
    json sectioned = exact;
    sectioned["section"] = middle_section();
    // Without the ghost the removed half is not drawn over the cap, so its flat colour shows.
    json sectioned_plain = exact;
    sectioned_plain["section"] = middle_section();
    sectioned_plain["section"]["ghost"] = false;

    const auto legacy = render(exact, "legacy");
    const auto with_null = render(null_section, "null");
    const auto with_cut = render(sectioned, "cut");
    const auto with_plain_cut = render(sectioned_plain, "plain_cut");
    CHECK(legacy.second == with_null.second);
    CHECK_FALSE(legacy.first.contains("section"));
    CHECK(legacy.second != with_cut.second);
    CHECK(with_cut.second != with_plain_cut.second);  // the ghost is drawn
    REQUIRE(with_cut.first.contains("section"));
    CHECK(with_cut.first.at("section").at("normal") == json::array({0.0, -1.0, 0.0}));
    CHECK(with_cut.first.at("section").at("ghost") == true);
    CHECK(with_plain_cut.first.at("section").at("ghost") == false);

    const Rgb ghosted = decode_png(with_cut.first.at("path").get<std::string>());
    const Rgb kept = decode_png(with_plain_cut.first.at("path").get<std::string>());
    REQUIRE(ghosted.ok());
    REQUIRE(kept.ok());
    long ghost_region = 0;
    for (size_t p = 0; p + 2 < ghosted.pixels.size(); p += 3) {
        const int delta = std::abs(int(ghosted.pixels[p]) - int(kept.pixels[p])) +
                          std::abs(int(ghosted.pixels[p + 1]) - int(kept.pixels[p + 1])) +
                          std::abs(int(ghosted.pixels[p + 2]) - int(kept.pixels[p + 2]));
        if (delta > 12) ++ghost_region;
    }
    CHECK(ghost_region > 400);  // ghost has readable surface coverage, not just one line

    // Legacy orthographic directions accept the same section.
    const json dir_view{{"dir", json::array({1.0, -1.0, 0.8})}, {"width", 320}, {"height", 240}};
    json dir_cut = dir_view;
    dir_cut["section"] = middle_section();
    dir_cut["section"]["ghost"] = false;
    const auto dir_plain = render(dir_view, "dir_plain");
    const auto dir_sectioned = render(dir_cut, "dir_cut");
    CHECK(dir_plain.second != dir_sectioned.second);

    json bad = exact;
    bad["section"] = json{{"point", json::array({0, 0})}, {"normal", json::array({0, 1, 0})}};
    const json refused = rpc("doc_render_view", json{{"doc_id", id},
                                                     {"out_path", tmp_png("bad")},
                                                     {"view", bad},
                                                     {"annotate", false}});
    CHECK_FALSE(refused.value("ok", true));
    CHECK(refused.value("code", std::string()) == "section_invalid");
}
