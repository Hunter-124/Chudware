#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <limits>
#include <string>
#include <vector>

#include <miniz.h>
#include <nlohmann/json.hpp>

#include "api/libchudware.h"

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace {

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
            ("chudware_camera_" + std::string(tag) + "_" +
             std::to_string(sequence++) + ".png"))
        .string();
}

std::vector<unsigned char> read_bytes(const std::string& path) {
    std::ifstream stream(path, std::ios::binary);
    return std::vector<unsigned char>(std::istreambuf_iterator<char>(stream),
                                      std::istreambuf_iterator<char>());
}

uint64_t fnv1a64(const std::vector<unsigned char>& bytes) {
    uint64_t hash = UINT64_C(1469598103934665603);
    for (unsigned char byte : bytes) {
        hash ^= byte;
        hash *= UINT64_C(1099511628211);
    }
    return hash;
}

uint32_t be32(const unsigned char* p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) |
           ((uint32_t)p[2] << 8) | p[3];
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
    if (out.width <= 0 || out.height <= 0 || bit_depth != 8 || color_type != 2)
        return Rgb{};
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

struct BoxDocument {
    std::string id;
    long revision = -1;
};

BoxDocument make_box(double x, double y, double z, const char* name) {
    const json created = rpc("doc_new", json{{"kind", "part"}, {"name", name}});
    const std::string id = created.at("doc_id").get<std::string>();
    const json feature{
        {"op", "add_feature"},
        {"feature",
         {{"type", "box"},
          {"name", "CameraCube"},
          {"params", {{"length", dimension(x)}, {"width", dimension(y)},
                      {"height", dimension(z)}}}}},
    };
    const json applied = rpc(
        "doc_apply_ops",
        json{{"doc_id", id}, {"base_revision", -1}, {"ops", json::array({feature})}});
    REQUIRE(applied.value("volume", 0.0) > 0.0);
    return {id, applied.at("revision").get<long>()};
}

json exact_view(double distance, int size = 800) {
    return json{
        {"camera",
         {{"projection", "perspective"},
          {"position", json::array({0.0, -distance, 0.0})},
          {"target", json::array({0.0, 0.0, 0.0})},
          {"up", json::array({0.0, 0.0, 1.0})},
          {"vertical_fov_deg", 90.0},
          {"near", 0.1},
          {"far", 100.0}}},
        {"width", size},
        {"height", size},
    };
}

json render(const std::string& doc_id, const json& view, const std::string& path,
            bool annotate = false) {
    return rpc("doc_render_view",
               json{{"doc_id", doc_id}, {"out_path", path}, {"view", view},
                    {"annotate", annotate}});
}

struct PixelBounds {
    int min_x = std::numeric_limits<int>::max();
    int min_y = std::numeric_limits<int>::max();
    int max_x = -1;
    int max_y = -1;
    bool valid() const { return max_x >= min_x && max_y >= min_y; }
    int width() const { return max_x - min_x + 1; }
    int height() const { return max_y - min_y + 1; }
};

PixelBounds central_silhouette(const Rgb& image) {
    PixelBounds bounds;
    for (int y = image.height / 4; y < image.height * 3 / 4; ++y) {
        for (int x = image.width / 4; x < image.width * 3 / 4; ++x) {
            const size_t index = (static_cast<size_t>(y) * image.width + x) * 3;
            if (image.pixels[index] == 245 && image.pixels[index + 1] == 245 &&
                image.pixels[index + 2] == 245)
                continue;
            bounds.min_x = std::min(bounds.min_x, x);
            bounds.max_x = std::max(bounds.max_x, x);
            bounds.min_y = std::min(bounds.min_y, y);
            bounds.max_y = std::max(bounds.max_y, y);
        }
    }
    return bounds;
}

json camera_with(const json& overrides) {
    json view = exact_view(10.0, 256);
    for (auto it = overrides.begin(); it != overrides.end(); ++it)
        view["camera"][it.key()] = it.value();
    return view;
}

}  // namespace

TEST_CASE("exact perspective cube silhouette matches hand projection") {
    const BoxDocument cube = make_box(2.0, 2.0, 2.0, "PerspectiveSilhouetteCube");
    const std::string path = tmp_png("silhouette");
    const json response = render(cube.id, exact_view(10.0), path);
    REQUIRE_MESSAGE(response.value("ok", false), response.dump());
    const Rgb image = decode_png(path);
    REQUIRE(image.ok());
    const PixelBounds bounds = central_silhouette(image);
    REQUIRE(bounds.valid());

    // Camera is at y=-10, looking +Y. The nearest cube face is y=-1, depth=9.
    // At vertical FOV 90 and 800 px, focal=400, so x/z=+-1 project to
    // 400+-400/9 = [355.555..., 444.444...]. Pixel-centre coverage is 356..443.
    CHECK(std::abs(bounds.min_x - 356) <= 1);
    CHECK(std::abs(bounds.max_x - 443) <= 1);
    CHECK(std::abs(bounds.min_y - 356) <= 1);
    CHECK(std::abs(bounds.max_y - 443) <= 1);

    const json& frame = response.at("frame");
    CHECK(frame.at("projection") == "perspective");
    CHECK(frame.at("focal_length_px").get<double>() == doctest::Approx(400.0).epsilon(1e-12));
    CHECK(frame.at("principal_x_px").get<double>() == doctest::Approx(400.0));
    CHECK(frame.at("principal_y_px").get<double>() == doctest::Approx(400.0));
    CHECK(frame.at("right") == json::array({1.0, 0.0, 0.0}));
    CHECK(frame.at("up") == json::array({0.0, 0.0, 1.0}));
    CHECK(frame.at("forward") == json::array({0.0, 1.0, 0.0}));
}

TEST_CASE("perspective apparent scale changes with camera distance") {
    const BoxDocument cube = make_box(2.0, 2.0, 2.0, "PerspectiveScaleCube");
    const std::string near_path = tmp_png("distance10");
    const std::string far_path = tmp_png("distance20");
    REQUIRE(render(cube.id, exact_view(10.0), near_path).value("ok", false));
    REQUIRE(render(cube.id, exact_view(20.0), far_path).value("ok", false));
    const PixelBounds near_bounds = central_silhouette(decode_png(near_path));
    const PixelBounds far_bounds = central_silhouette(decode_png(far_path));
    REQUIRE(near_bounds.valid());
    REQUIRE(far_bounds.valid());

    // Nearest-face depths are 9 and 19, so perspective predicts apparent ratio 19/9.
    const double measured = static_cast<double>(near_bounds.width()) / far_bounds.width();
    CHECK(measured == doctest::Approx(19.0 / 9.0).epsilon(0.04));
    CHECK(near_bounds.width() > far_bounds.width() * 2);
}

TEST_CASE("perspective near and far planes clip triangles in camera depth") {
    const BoxDocument cube = make_box(2.0, 2.0, 2.0, "PerspectiveClipCube");
    json full_view = exact_view(10.0);
    const std::string full_path = tmp_png("clip_full");
    REQUIRE(render(cube.id, full_view, full_path).value("ok", false));
    const PixelBounds full = central_silhouette(decode_png(full_path));
    REQUIRE(full.valid());

    // The cube occupies depth [9,11]. Clipping at depth 10 removes the front half and creates
    // new side-face edges at x/z=+-1, which project to 400+-400/10: an 80 px silhouette.
    json half_view = full_view;
    half_view["camera"]["near"] = 10.0;
    const std::string half_path = tmp_png("clip_half");
    REQUIRE(render(cube.id, half_view, half_path).value("ok", false));
    const PixelBounds half = central_silhouette(decode_png(half_path));
    REQUIRE(half.valid());
    CHECK(half.width() >= 78);
    CHECK(half.width() <= 82);
    CHECK(half.width() < full.width());

    json excluded_view = full_view;
    excluded_view["camera"]["far"] = 8.5;
    const json excluded = render(cube.id, excluded_view, tmp_png("clip_excluded"));
    CHECK_FALSE(excluded.value("ok", true));
    CHECK(excluded.value("path", std::string("not-empty")).empty());
}

TEST_CASE("valid extreme projection and scale-equivalent up remain well-defined") {
    const BoxDocument cube = make_box(2.0, 2.0, 2.0, "PerspectiveNumericalCube");

    json tiny_up = exact_view(10.0, 256);
    tiny_up["camera"]["up"] = json::array({0.0, 0.0, 1e-300});
    const json tiny_up_result = render(cube.id, tiny_up, tmp_png("tiny_up"));
    REQUIRE_MESSAGE(tiny_up_result.value("ok", false), tiny_up_result.dump());
    CHECK(tiny_up_result.at("camera").at("up") == tiny_up.at("camera").at("up"));

    json almost_parallel = exact_view(10.0, 256);
    almost_parallel["camera"]["up"] = json::array({0.0, 1.0, 1e-9});
    const json almost_parallel_result =
        render(cube.id, almost_parallel, tmp_png("almost_parallel"));
    REQUIRE_MESSAGE(almost_parallel_result.value("ok", false),
                    almost_parallel_result.dump());

    // The front face is only 1e-7 WCS units from the eye. Its un-clipped projection is billions
    // of pixels wide; side-frustum clipping must bound it before integer raster bounds are formed.
    json huge_projection = exact_view(1.0000001, 256);
    huge_projection["camera"]["near"] = 1e-9;
    const json huge_result = render(cube.id, huge_projection, tmp_png("huge_projection"));
    REQUIRE_MESSAGE(huge_result.value("ok", false), huge_result.dump());
    const Rgb huge_image = decode_png(huge_result.at("path").get<std::string>());
    REQUIRE(huge_image.ok());
    CHECK(huge_image.width == 256);
    CHECK(huge_image.height == 256);
}

TEST_CASE("degenerate exact cameras are refused by stable names") {
    const BoxDocument cube = make_box(2.0, 2.0, 2.0, "PerspectiveRefusalCube");
    auto refused = [&](const json& view, const char* expected) {
        const json response = render(cube.id, view, tmp_png(expected));
        CHECK_FALSE(response.value("ok", true));
        CHECK(response.value("code", std::string()) == expected);
        CHECK(response.value("path", std::string("not-empty")).empty());
    };
    refused(camera_with(json{{"target", json::array({0.0, -10.0, 0.0})}}),
            "camera_zero_direction");
    refused(camera_with(json{{"up", json::array({0.0, 1.0, 0.0})}}),
            "camera_up_parallel");
    refused(camera_with(json{{"vertical_fov_deg", 0.0}}), "camera_fov_invalid");
    refused(camera_with(json{{"vertical_fov_deg", 1e-320}}), "camera_fov_invalid");
    refused(camera_with(json{{"near", 2.0}, {"far", 2.0}}),
            "camera_clip_range_invalid");
}

TEST_CASE("dispatch advertises capability and echoes exact camera plus revision") {
    const json ping = rpc("ping", json{{"probe", "camera"}});
    REQUIRE(ping.at("capabilities").is_array());
    CHECK(std::find(ping.at("capabilities").begin(), ping.at("capabilities").end(),
                    json("doc_render_view.camera_pose.v1")) != ping.at("capabilities").end());

    const BoxDocument cube = make_box(2.0, 2.0, 2.0, "PerspectiveEchoCube");
    const json requested = exact_view(10.0, 320);
    const json response = render(cube.id, requested, tmp_png("echo"));
    REQUIRE(response.value("ok", false));
    CHECK(response.at("capability") == "doc_render_view.camera_pose.v1");
    CHECK(response.at("revision").get<long>() == cube.revision);
    const json& echo = response.at("camera");
    CHECK(echo.at("position") == requested.at("camera").at("position"));
    CHECK(echo.at("target") == requested.at("camera").at("target"));
    CHECK(echo.at("up") == requested.at("camera").at("up"));
    CHECK(echo.at("vertical_fov_deg") == requested.at("camera").at("vertical_fov_deg"));
    CHECK(echo.at("near") == requested.at("camera").at("near"));
    CHECK(echo.at("far") == requested.at("camera").at("far"));
    CHECK(echo.at("image_width_px") == 320);
    CHECK(echo.at("image_height_px") == 320);
    CHECK_FALSE(response.at("image_size_clamped").get<bool>());

    json nested = exact_view(10.0, 800);
    nested["camera"]["width"] = 5000;
    nested["camera"]["height"] = 8;
    const json clamped = render(cube.id, nested, tmp_png("clamped"));
    REQUIRE(clamped.value("ok", false));
    CHECK(clamped.at("camera").at("image_width_px") == 2048);
    CHECK(clamped.at("camera").at("image_height_px") == 64);
    CHECK(clamped.at("image_size_clamped").get<bool>());
    CHECK(clamped.at("requested_image_width_px") == 5000);
    CHECK(clamped.at("requested_image_height_px") == 8);
}

TEST_CASE("pre-existing render specifications retain pre-change byte hashes") {
    const BoxDocument box = make_box(40.0, 24.0, 12.0, "RenderCameraCompatibilityBox");
    struct Case {
        const char* method;
        json view;
        uint64_t pre_change_hash;
    };
    const std::array<Case, 5> cases{{
        {"doc_render_view", "front", UINT64_C(0xe70f29e01f5fc82f)},
        {"doc_render_view", "top", UINT64_C(0x673fc6206b542758)},
        {"doc_render_png", nullptr, UINT64_C(0x9ce9856cd87a133d)},
        {"doc_render_view", json{{"azimuth", 30}, {"elevation", 20}},
         UINT64_C(0xda853692e9a03abc)},
        {"doc_render_view", json{{"dir", json::array({1, 0, 0})}},
         UINT64_C(0xca1061b1bc1f2712)},
    }};
    for (const Case& item : cases) {
        const std::string path = tmp_png("compat");
        json params{{"doc_id", box.id}, {"out_path", path}};
        if (std::string(item.method) == "doc_render_view") params["view"] = item.view;
        const json response = rpc(item.method, params);
        REQUIRE_MESSAGE(response.value("ok", false), response.dump());
        CHECK_MESSAGE(fnv1a64(read_bytes(path)) == item.pre_change_hash,
                      item.method << " view=" << item.view.dump());
    }
}
