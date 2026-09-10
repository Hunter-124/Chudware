// Task 03: on-request labelled views. Exercises the new doc_render_view / doc_render_contact_sheet
// session entries against a real session doc (an asymmetric box, so front/right/top silhouettes
// genuinely differ). We can't pixel-diff with a vision model in CI, so instead we assert the camera
// actually changed by proving the encoded PNGs differ between distinct views and are byte-identical
// for equivalent / repeated specs — i.e. the view spec drives the render deterministically.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <miniz.h>

#include <nlohmann/json.hpp>

#include "doc/session.h"

using json = nlohmann::json;
namespace fs = std::filesystem;

namespace {

std::vector<unsigned char> read_bytes(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    return std::vector<unsigned char>((std::istreambuf_iterator<char>(f)),
                                      std::istreambuf_iterator<char>());
}

bool is_png(const std::vector<unsigned char>& b) {
    static const unsigned char sig[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'};
    if (b.size() < 8) return false;
    for (int i = 0; i < 8; ++i)
        if (b[i] != sig[i]) return false;
    return true;
}

// A unique temp PNG path per call (no Date.now/rand in core scope, so use a counter).
std::string tmp_png(const char* tag) {
    static int n = 0;
    fs::path p = fs::temp_directory_path() /
                 ("chudware_rv_" + std::string(tag) + "_" + std::to_string(n++) + ".png");
    return p.string();
}

struct Rgb {
    int w = 0, h = 0;
    std::vector<unsigned char> px;
    bool ok() const { return w > 0 && h > 0 && px.size() == (size_t)w * h * 3; }
};

uint32_t be32(const unsigned char* p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

// Decode the exact PNG flavour encode_png_rgb emits: 8-bit RGB with filter-none scanlines.
Rgb decode_png(const std::string& path) {
    Rgb out;
    std::ifstream f(path, std::ios::binary);
    if (!f) return out;
    std::string buf((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (buf.size() < 8) return out;
    const auto* d = reinterpret_cast<const unsigned char*>(buf.data());
    size_t n = buf.size(), off = 8;
    std::string idat;
    int bit_depth = 0, color_type = -1, filter_method = -1;
    while (off + 8 <= n) {
        const uint32_t len = be32(d + off);
        const std::string tag(reinterpret_cast<const char*>(d + off + 4), 4);
        const size_t data_off = off + 8;
        if (data_off + len > n) break;
        if (tag == "IHDR" && len >= 13) {
            out.w = (int)be32(d + data_off);
            out.h = (int)be32(d + data_off + 4);
            bit_depth = d[data_off + 8];
            color_type = d[data_off + 9];
            filter_method = d[data_off + 11];
        } else if (tag == "IDAT") {
            idat.append(reinterpret_cast<const char*>(d + data_off), len);
        } else if (tag == "IEND") {
            break;
        }
        off = data_off + len + 4;  // CRC
    }
    if (out.w <= 0 || out.h <= 0 || bit_depth != 8 || color_type != 2 || filter_method != 0) {
        out.w = out.h = 0;
        return out;
    }
    const size_t stride = 1 + (size_t)out.w * 3;
    std::vector<unsigned char> raw(stride * (size_t)out.h);
    mz_ulong raw_len = (mz_ulong)raw.size();
    if (mz_uncompress(raw.data(), &raw_len, reinterpret_cast<const unsigned char*>(idat.data()),
                      (mz_ulong)idat.size()) != MZ_OK ||
        raw_len != raw.size()) {
        out.w = out.h = 0;
        return out;
    }
    out.px.resize((size_t)out.w * out.h * 3);
    for (int y = 0; y < out.h; ++y) {
        if (raw[(size_t)y * stride] != 0) {
            out.w = out.h = 0;
            out.px.clear();
            return out;
        }
        std::copy(raw.begin() + (size_t)y * stride + 1,
                  raw.begin() + (size_t)y * stride + stride,
                  out.px.begin() + (size_t)y * out.w * 3);
    }
    return out;
}

json dimv(double v) { return json{{"value", v}, {"expr", nullptr}}; }

struct HolePlateDoc {
    std::string id;
    json hole_box;
};

// The plate spans [-60,60] x [-40,40] x [-6,6]. A Ø5 through-hole at face-local
// (-40,0) is intentionally far from the principal-face centroid: tight framing on
// [-42.5,-2.5,-6]..[-37.5,2.5,6] must zoom by geometry, and must cull the TOP label.
HolePlateDoc make_hole_plate_doc() {
    using chudware::doc::doc_apply_ops;
    using chudware::doc::doc_new;
    json nr = doc_new("part", "RenderViewTinyHole");
    std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"},
             {"feature", {{"type", "box"}, {"name", "Plate"},
                          {"params", {{"length", dimv(120)}, {"width", dimv(80)},
                                      {"height", dimv(12)}}}}}},
        json{{"op", "add_feature"},
             {"feature", {{"type", "hole"}, {"name", "OffCentreBore"},
                          {"params", {{"style", "simple"}, {"standard", "custom"},
                                      {"diameter", dimv(5)}, {"end", "through_all"},
                                      {"positions", json::array({json::array({-40.0, 0.0})})}}},
                          {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);
    REQUIRE(resp.value("volume", 0.0) > 114000.0);
    REQUIRE(resp.value("volume", 0.0) < 120.0 * 80.0 * 12.0);
    if (resp.contains("rebuild") && resp.at("rebuild").contains("feature_status")) {
        for (const json& st : resp.at("rebuild").at("feature_status"))
            REQUIRE_MESSAGE(st.value("ok", false), st.dump());
    }
    return HolePlateDoc{
        id,
        json{{"min", json::array({-42.5, -2.5, -6.0})},
             {"max", json::array({-37.5, 2.5, 6.0})}},
    };
}

std::array<double, 3> frame_vec(const json& frame, const char* key) {
    const json& v = frame.at(key);
    return {v.at(0).get<double>(), v.at(1).get<double>(), v.at(2).get<double>()};
}

double dot3(const std::array<double, 3>& a, const std::array<double, 3>& b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}

// Teal identifies a shaded -Z face by hue, not exact RGB. Both green and blue must dominate red,
// the two leading channels must remain neighbours, and max-min >= 45 is the saturation floor.
long count_teal(const Rgb& im) {
    long n = 0;
    for (size_t i = 0; i + 2 < im.px.size(); i += 3) {
        const int r = im.px[i], g = im.px[i + 1], b = im.px[i + 2];
        const int hi = std::max({r, g, b}), lo = std::min({r, g, b});
        if (hi >= 75 && hi - lo >= 45 && g > r + 25 && b > r + 25 &&
            std::abs(g - b) <= 25)
            ++n;
    }
    return n;
}

// The TOP glyph is unlit (90,140,225), whereas the top face only mixes 55% of that tint into
// neutral material and is Lambert-shaded. This strict blue hue band isolates glyph/gizmo pixels
// without exact-RGB matching. The caller's region excludes the fixed bottom-left Z gizmo.
long count_label_blue(const Rgb& im, int x0, int y0, int x1, int y1) {
    long n = 0;
    x0 = std::max(0, x0); y0 = std::max(0, y0);
    x1 = std::min(im.w, x1); y1 = std::min(im.h, y1);
    for (int y = y0; y < y1; ++y)
        for (int x = x0; x < x1; ++x) {
            const size_t i = ((size_t)y * im.w + x) * 3;
            const int r = im.px[i], g = im.px[i + 1], b = im.px[i + 2];
            if (b >= 145 && b - r >= 85 && b - g >= 55) ++n;
        }
    return n;
}

// Build a session part doc holding one asymmetric box (X=40, Y=24, Z=12). Distinct extents on every
// axis => front (X*Z), right (Y*Z) and top (X*Y) projections are all different silhouettes.
std::string make_box_doc() {
    using chudware::doc::doc_new;
    using chudware::doc::doc_apply_ops;
    json nr = doc_new("part", "RenderViewTest");
    std::string id = nr.at("doc_id").get<std::string>();
    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };
    json ops = json::array({
        json{{"op", "add_feature"},
             {"feature", {{"type", "box"}, {"name", "Block"},
                          {"params", {{"length", dimv(40)}, {"width", dimv(24)},
                                      {"height", dimv(12)}}}}}},
    });
    json resp = doc_apply_ops(id, -1, ops, false);
    REQUIRE(resp.value("volume", 0.0) > 0.0);  // a real solid got built
    return id;
}

}  // namespace

TEST_CASE("doc_render_view: named views render labelled PNGs and back-compat holds") {
    std::string id = make_box_doc();

    std::string pf = tmp_png("front");
    json rf = chudware::doc::doc_render_view(id, pf, "front");
    REQUIRE(rf.value("ok", false));
    CHECK(rf.value("view", std::string()) == "FRONT");
    CHECK_FALSE(rf.value("dims", std::string()).empty());  // bbox dims label present
    CHECK(is_png(read_bytes(pf)));

    std::string pt = tmp_png("top");
    json rt = chudware::doc::doc_render_view(id, pt, "top");
    REQUIRE(rt.value("ok", false));
    CHECK(rt.value("view", std::string()) == "TOP");
    CHECK(is_png(read_bytes(pt)));

    // doc_render_png (legacy iso) still works unchanged.
    std::string pi = tmp_png("iso");
    json ri = chudware::doc::doc_render_png(id, pi);
    REQUIRE(ri.value("ok", false));
    CHECK(is_png(read_bytes(pi)));
}

TEST_CASE("doc_render_view: distinct cameras produce distinct images, equivalent specs match") {
    std::string id = make_box_doc();

    auto render = [&](const json& v, const char* tag) {
        std::string p = tmp_png(tag);
        json r = chudware::doc::doc_render_view(id, p, v);
        REQUIRE(r.value("ok", false));
        return read_bytes(p);
    };

    std::vector<unsigned char> front = render("front", "f");
    std::vector<unsigned char> right = render("right", "r");
    std::vector<unsigned char> top   = render("top", "t");

    // The asymmetric box looks different from each principal direction -> the camera really moved.
    CHECK(front != right);
    CHECK(front != top);
    CHECK(right != top);

    // Same spec, two forms / two calls -> deterministic, byte-identical output.
    std::vector<unsigned char> frontObj = render(json{{"view", "front"}}, "f2");
    std::vector<unsigned char> frontAgain = render("front", "f3");
    CHECK(front == frontObj);
    CHECK(front == frontAgain);
}

TEST_CASE("doc_render_view: parametric azimuth/elevation maps to the named-view convention") {
    std::string id = make_box_doc();
    using doctest::Approx;
    // Helper: resolved camera dir (scene -> camera) the renderer used, from the response.
    auto dir = [&](const json& v, const char* tag) {
        json r = chudware::doc::doc_render_view(id, tmp_png(tag), v);
        REQUIRE(r.value("ok", false));
        const json& d = r.at("dir");
        return std::array<double, 3>{d[0].get<double>(), d[1].get<double>(), d[2].get<double>()};
    };

    // az=0,el=0 -> front (0,-1,0); az=90 -> right (+X); el=90 -> top (+Z). Same convention the
    // named views and the vision-review face labels use, so "azimuth 90" == "right".
    auto front = dir(json{{"azimuth", 0}, {"elevation", 0}}, "ae0");
    CHECK(front[0] == Approx(0).epsilon(1e-6));
    CHECK(front[1] == Approx(-1).epsilon(1e-6));
    CHECK(front[2] == Approx(0).epsilon(1e-6));

    auto right = dir(json{{"azimuth", 90}, {"elevation", 0}}, "ae90");
    auto rightNamed = dir("right", "rn");
    CHECK(right[0] == Approx(rightNamed[0]).epsilon(1e-6));
    CHECK(right[1] == Approx(rightNamed[1]).epsilon(1e-6));
    CHECK(right[2] == Approx(rightNamed[2]).epsilon(1e-6));

    auto top = dir(json{{"elevation", 90}}, "ael90");
    CHECK(top[2] == Approx(1).epsilon(1e-6));

    // The header echoes the requested angle, and an explicit camera dir also renders.
    std::string pa = tmp_png("aehdr");
    json ra = chudware::doc::doc_render_view(id, pa, json{{"azimuth", 30}, {"elevation", 20}});
    REQUIRE(ra.value("ok", false));
    CHECK(ra.value("view", std::string()) == "AZ30 EL20");
    CHECK(is_png(read_bytes(pa)));

    std::string pd = tmp_png("dir");
    json rd = chudware::doc::doc_render_view(id, pd, json{{"dir", json::array({1, 0, 0})}});
    REQUIRE(rd.value("ok", false));
    CHECK(is_png(read_bytes(pd)));
}

TEST_CASE("doc_render_view focus: frame echo is orthonormal and reports default viewport") {
    const std::string id = make_box_doc();
    json result = chudware::doc::doc_render_view(id, tmp_png("frame"), "iso");
    REQUIRE(result.value("ok", false));
    REQUIRE(result.contains("frame"));
    const json& frame = result.at("frame");
    CHECK_FALSE(frame.at("focused").get<bool>());
    CHECK_FALSE(frame.at("clip_near").get<bool>());
    CHECK(frame.at("width").get<int>() == 512);
    CHECK(frame.at("height").get<int>() == 400);
    CHECK(frame.at("scale").get<double>() > 0.0);

    const auto right = frame_vec(frame, "right");
    const auto up = frame_vec(frame, "up");
    const auto dir = frame_vec(frame, "dir");
    CHECK(std::abs(dot3(right, right) - 1.0) < 1e-12);
    CHECK(std::abs(dot3(up, up) - 1.0) < 1e-12);
    CHECK(std::abs(dot3(dir, dir) - 1.0) < 1e-12);
    CHECK(std::abs(dot3(right, up)) < 1e-12);
    CHECK(std::abs(dot3(right, dir)) < 1e-12);
    CHECK(std::abs(dot3(up, dir)) < 1e-12);
}

TEST_CASE("doc_render_view focus: tiny off-centre hole is rendered at close-up resolution") {
    const HolePlateDoc plate = make_hole_plate_doc();
    const json dir = json::array({0.6, -0.6, 0.5});
    const std::string whole_path = tmp_png("hole_whole");
    const std::string focus_path = tmp_png("hole_focus");
    json whole = chudware::doc::doc_render_view(
        plate.id, whole_path, json{{"dir", dir}});
    json focused = chudware::doc::doc_render_view(
        plate.id, focus_path, json{{"dir", dir}, {"focus", plate.hole_box}});
    REQUIRE(whole.value("ok", false));
    // This is load-bearing: the old whole-part painted-pixel floor rejected sparse tiny close-ups.
    REQUIRE(focused.value("ok", false));
    REQUIRE(whole.contains("frame"));
    REQUIRE(focused.contains("frame"));
    CHECK_FALSE(whole.at("frame").at("focused").get<bool>());
    CHECK(focused.at("frame").at("focused").get<bool>());

    const double whole_scale = whole.at("frame").at("scale").get<double>();
    const double focus_scale = focused.at("frame").at("scale").get<double>();
    // The Ø5 focus is dwarfed by the 120 mm plate. Even with its default 15% focus margin and
    // the oblique view exposing all 12 mm of thickness, geometry forces well over 5x magnification.
    CHECK_MESSAGE(focus_scale > whole_scale * 5.0,
                  "whole scale=" << whole_scale << " focused scale=" << focus_scale);

    const std::vector<unsigned char> whole_bytes = read_bytes(whole_path);
    const std::vector<unsigned char> focus_bytes = read_bytes(focus_path);
    CHECK(is_png(whole_bytes));
    CHECK(is_png(focus_bytes));
    CHECK(focus_bytes != whole_bytes);  // a separately rasterized close-up, not the whole image
    const Rgb image = decode_png(focus_path);
    REQUIRE(image.ok());
    CHECK(image.w == 512);
    CHECK(image.h == 400);
}

TEST_CASE("doc_render_view focus: requested viewport dimensions are honoured and clamped") {
    const std::string id = make_box_doc();
    auto render_size = [&](int width, int height, const char* tag) {
        const std::string path = tmp_png(tag);
        json result = chudware::doc::doc_render_view(
            id, path, json{{"view", "iso"}, {"width", width}, {"height", height}});
        REQUIRE(result.value("ok", false));
        REQUIRE(result.contains("frame"));
        Rgb image = decode_png(path);
        REQUIRE(image.ok());
        CHECK(image.w == result.at("frame").at("width").get<int>());
        CHECK(image.h == result.at("frame").at("height").get<int>());
        return result.at("frame");
    };

    json requested = render_size(1024, 768, "size_requested");
    CHECK(requested.at("width").get<int>() == 1024);
    CHECK(requested.at("height").get<int>() == 768);

    json high = render_size(5000, 5000, "size_high");
    CHECK(high.at("width").get<int>() == 4096);
    CHECK(high.at("height").get<int>() == 4096);

    json low = render_size(8, 8, "size_low");
    CHECK(low.at("width").get<int>() == 64);
    CHECK(low.at("height").get<int>() == 64);
}

TEST_CASE("doc_render_view focus: center-radius and min-max boxes resolve to the same frame") {
    const HolePlateDoc plate = make_hole_plate_doc();
    json box_focus{
        {"min", json::array({-46.0, -6.0, -6.0})},
        {"max", json::array({-34.0, 6.0, 6.0})},
        {"margin", 0.2},
    };
    json radius_focus{
        {"center", json::array({-40.0, 0.0, 0.0})},
        {"radius", 6.0},
        {"margin", 0.2},
    };
    const json dir = json::array({0.6, -0.6, 0.5});
    json box_result = chudware::doc::doc_render_view(
        plate.id, tmp_png("focus_box"), json{{"dir", dir}, {"focus", box_focus}});
    json radius_result = chudware::doc::doc_render_view(
        plate.id, tmp_png("focus_radius"), json{{"dir", dir}, {"focus", radius_focus}});
    REQUIRE(box_result.value("ok", false));
    REQUIRE(radius_result.value("ok", false));
    const json& a = box_result.at("frame");
    const json& b = radius_result.at("frame");
    CHECK(a.at("focused").get<bool>());
    CHECK(b.at("focused").get<bool>());
    CHECK(a.at("width") == b.at("width"));
    CHECK(a.at("height") == b.at("height"));
    for (const char* key : {"center", "right", "up", "dir"}) {
        const auto av = frame_vec(a, key);
        const auto bv = frame_vec(b, key);
        for (int i = 0; i < 3; ++i)
            CHECK_MESSAGE(std::abs(av[i] - bv[i]) < 1e-9, key << "[" << i << "]");
    }
    for (const char* key : {"scale", "cu", "cv"})
        CHECK_MESSAGE(std::abs(a.at(key).get<double>() - b.at(key).get<double>()) < 1e-9, key);
}

TEST_CASE("doc_render_view focus: clip_near exposes the bore behind the near plate material") {
    const HolePlateDoc plate = make_hole_plate_doc();
    json plain_focus = plate.hole_box;
    json clipped_focus = plate.hole_box;
    clipped_focus["clip_near"] = true;
    const json dir = json::array({0.4, -0.4, 0.82});
    const std::string plain_path = tmp_png("clip_plain");
    const std::string clipped_path = tmp_png("clip_near");
    json plain = chudware::doc::doc_render_view(
        plate.id, plain_path, json{{"dir", dir}, {"focus", plain_focus}});
    json clipped = chudware::doc::doc_render_view(
        plate.id, clipped_path, json{{"dir", dir}, {"focus", clipped_focus}});
    REQUIRE(plain.value("ok", false));
    REQUIRE(clipped.value("ok", false));
    CHECK_FALSE(plain.at("frame").at("clip_near").get<bool>());
    CHECK(clipped.at("frame").at("clip_near").get<bool>());
    Rgb plain_image = decode_png(plain_path);
    Rgb clipped_image = decode_png(clipped_path);
    REQUIRE(plain_image.ok());
    REQUIRE(clipped_image.ok());
    CHECK(read_bytes(plain_path) != read_bytes(clipped_path));

    // This direction sees the top (+Z) face first. Clipping at the front of the hole box removes
    // that near material and reveals materially more of the -Z/bottom face, whose shaded teal hue
    // is stable under Lambert multiplication. The plate still fills the frame, so background count
    // is deliberately not used as a proxy for a cutaway.
    const long plain_teal = count_teal(plain_image);
    const long clipped_teal = count_teal(clipped_image);
    CHECK_MESSAGE(clipped_teal > plain_teal + 100,
                  "unclipped teal=" << plain_teal << " clipped teal=" << clipped_teal);
}

TEST_CASE("doc_render_view focus: off-frame principal face labels are culled") {
    const HolePlateDoc plate = make_hole_plate_doc();
    const std::string whole_path = tmp_png("label_whole");
    const std::string focused_path = tmp_png("label_focus");
    json whole = chudware::doc::doc_render_view(plate.id, whole_path, "top");
    json focused = chudware::doc::doc_render_view(
        plate.id, focused_path, json{{"view", "top"}, {"focus", plate.hole_box}});
    REQUIRE(whole.value("ok", false));
    REQUIRE(focused.value("ok", false));
    Rgb whole_image = decode_png(whole_path);
    Rgb focused_image = decode_png(focused_path);
    REQUIRE(whole_image.ok());
    REQUIRE(focused_image.ok());

    // Ignore the neutral header and the fixed bottom-left orientation gizmo. A whole-plate top
    // render contains the saturated-blue TOP dot/glyph. In the close-up the top-face centroid is
    // thousands of pixels to the right of the hole-centred frame, so that glyph must be absent;
    // the shaded top geometry itself is below this hue band's saturation threshold.
    const long whole_blue = count_label_blue(
        whole_image, 80, 40, whole_image.w, whole_image.h - 80);
    const long focused_blue = count_label_blue(
        focused_image, 80, 40, focused_image.w, focused_image.h - 80);
    CHECK_MESSAGE(whole_blue > 20, "whole-render TOP label pixels=" << whole_blue);
    CHECK_MESSAGE(focused_blue < 10, "focused off-frame TOP label pixels=" << focused_blue);
    CHECK_MESSAGE(whole_blue > focused_blue + 20,
                  "whole blue=" << whole_blue << " focused blue=" << focused_blue);
}

TEST_CASE("doc_render_contact_sheet: tiles multiple labelled views into one PNG") {
    std::string id = make_box_doc();

    // Default set: front + right + top + iso.
    std::string ps = tmp_png("sheet");
    json rs = chudware::doc::doc_render_contact_sheet(id, ps, json::array());
    REQUIRE(rs.value("ok", false));
    REQUIRE(rs.at("views").is_array());
    CHECK(rs.at("views").size() == 4);
    CHECK(rs.value("cols", 0) >= 1);
    CHECK(rs.value("rows", 0) >= 1);
    std::vector<unsigned char> sheet = read_bytes(ps);
    CHECK(is_png(sheet));

    // A contact sheet is several tiles -> meaningfully larger than one single-view PNG.
    std::string p1 = tmp_png("single");
    REQUIRE(chudware::doc::doc_render_view(id, p1, "front").value("ok", false));
    CHECK(sheet.size() > read_bytes(p1).size());

    // Explicit view list is honoured.
    std::string ps2 = tmp_png("sheet2");
    json rs2 = chudware::doc::doc_render_contact_sheet(
        id, ps2, json::array({"front", "top"}));
    REQUIRE(rs2.value("ok", false));
    CHECK(rs2.at("views").size() == 2);
    CHECK(is_png(read_bytes(ps2)));
}

// --- optional [r,g,b] background --------------------------------------------------------
// The engine used to hard-code a 245 light-grey field, so anything wanting a dark plate had
// to colour-key that constant back out of a finished frame. The parameter renders it.
TEST_CASE("doc_render_view: background clears the frame and defaults to the 245 grey") {
    const std::string id = make_box_doc();

    // A corner pixel is background by construction: the camera fits the model to 85% of the
    // frame, so no geometry can reach (0,0).
    const std::string light = tmp_png("bg_default");
    REQUIRE(chudware::doc::doc_render_view(id, light, "front").value("ok", false));
    const Rgb lit = decode_png(light);
    REQUIRE(lit.ok());
    CHECK((int)lit.px[0] == 245);
    CHECK((int)lit.px[1] == 245);
    CHECK((int)lit.px[2] == 245);

    const std::string dark = tmp_png("bg_black");
    REQUIRE(chudware::doc::doc_render_view(id, dark, "front", /*annotate=*/true,
                                          json::array({0, 0, 0}))
                .value("ok", false));
    const Rgb blk = decode_png(dark);
    REQUIRE(blk.ok());
    CHECK((int)blk.px[0] == 0);
    CHECK((int)blk.px[1] == 0);
    CHECK((int)blk.px[2] == 0);

    // A non-grey triple lands per channel (the fast uniform-fill path must not flatten it),
    // and out-of-range components clamp rather than wrap.
    const std::string film = tmp_png("bg_film");
    REQUIRE(chudware::doc::doc_render_view(id, film, "front", /*annotate=*/true,
                                          json::array({16, 17, 300}))
                .value("ok", false));
    const Rgb fm = decode_png(film);
    REQUIRE(fm.ok());
    CHECK((int)fm.px[0] == 16);
    CHECK((int)fm.px[1] == 17);
    CHECK((int)fm.px[2] == 255);

    // The geometry itself is untouched: only the cleared field changed, so the two renders
    // differ on the background and agree on the shaded body.
    REQUIRE(blk.w == lit.w);
    REQUIRE(blk.h == lit.h);
    long same_body = 0;
    for (size_t i = 0; i + 2 < lit.px.size(); i += 3) {
        const bool lit_bg = lit.px[i] == 245 && lit.px[i + 1] == 245 && lit.px[i + 2] == 245;
        if (!lit_bg && lit.px[i] == blk.px[i] && lit.px[i + 1] == blk.px[i + 1] &&
            lit.px[i + 2] == blk.px[i + 2])
            ++same_body;
    }
    CHECK(same_body > (long)(lit.w * lit.h) / 20);  // the shaded part is pixel-identical

    // A malformed value is ignored, not fatal: the render still succeeds on the default grey.
    const std::string bad = tmp_png("bg_bad");
    REQUIRE(chudware::doc::doc_render_view(id, bad, "front", /*annotate=*/true,
                                          json::array({0, 0}))
                .value("ok", false));
    const Rgb bd = decode_png(bad);
    REQUIRE(bd.ok());
    CHECK((int)bd.px[0] == 245);
}

TEST_CASE("doc_render_contact_sheet: background applies to every tile") {
    const std::string id = make_box_doc();
    const std::string sheet = tmp_png("sheet_bg");
    REQUIRE(chudware::doc::doc_render_contact_sheet(id, sheet, json::array({"front", "top"}),
                                                    json::array({0, 0, 0}))
                .value("ok", false));
    const Rgb im = decode_png(sheet);
    REQUIRE(im.ok());
    // Tile 0's corner is (0,0); tile 1 starts at column TW+GAP = 384+4.
    CHECK((int)im.px[0] == 0);
    const size_t tile1 = (size_t)(384 + 4) * 3;
    REQUIRE(tile1 + 2 < im.px.size());
    CHECK((int)im.px[tile1] == 0);
}

TEST_CASE("doc_render_png: background clears the legacy iso render too") {
    const std::string id = make_box_doc();
    const std::string p = tmp_png("iso_bg");
    REQUIRE(chudware::doc::doc_render_png(id, p, /*annotate=*/true,
                                          json::array({10, 20, 30}))
                .value("ok", false));
    const Rgb im = decode_png(p);
    REQUIRE(im.ok());
    CHECK((int)im.px[0] == 10);
    CHECK((int)im.px[1] == 20);
    CHECK((int)im.px[2] == 30);
}

TEST_CASE("doc_render_view: nested multibody appearances shade each face without ordinal tint") {
    using namespace chudware::doc;
    const std::string part = doc_new("part", "RenderInheritedAppearance").at("doc_id");
    json ops = json::array();
    for (int i = 0; i < 2; ++i) {
        ops.push_back({{"op", "add_feature"}, {"feature", {
            {"type", "box"}, {"name", i == 0 ? "Red" : "Blue"},
            {"params", {{"length", 20}, {"width", 20}, {"height", 20},
                        {"at", {i * 40, 0, 0}}, {"mode", "new"}}}}}});
        ops.push_back({{"op", "set_appearance"}, {"id", "b" + std::to_string(i + 1)},
                       {"color", i == 0 ? json{0.9, 0.03, 0.03} : json{0.03, 0.03, 0.9}}});
    }
    doc_apply_ops(part, -1, ops, false);
    const fs::path directory = fs::temp_directory_path() / ("chudware_render_appearance_" + part);
    fs::create_directories(directory);
    struct RemoveDirectory {
        fs::path path;
        ~RemoveDirectory() { std::error_code ec; fs::remove_all(path, ec); }
    } cleanup{directory};
    auto path = directory / "part.step+";
    doc_save(part, path.string());
    std::string current = part;
    for (int depth = 0; depth < 2; ++depth) {
        current = doc_new("assembly", "RenderAppearanceNesting").at("doc_id");
        doc_apply_ops(current, -1, json::array({
            {{"op", "asm_insert_component"}, {"source", {{"path", path.string()}}}, {"fixed", true}},
        }), false);
        path = directory / ("assembly" + std::to_string(depth) + ".step+");
        doc_save(current, path.string());
    }
    auto hue_counts = [](const Rgb& image) {
        std::array<long, 2> counts{};
        for (size_t i = 0; i + 2 < image.px.size(); i += 3) {
            const int r = image.px[i], g = image.px[i + 1], b = image.px[i + 2];
            if (r > 60 && r > 3 * g && r > 3 * b) ++counts[0];
            if (b > 60 && b > 3 * g && b > 3 * r) ++counts[1];
        }
        return counts;
    };
    const auto shot = (directory / "nested.png").string();
    REQUIRE(doc_render_view(current, shot, "front", false).value("ok", false));
    const Rgb inherited = decode_png(shot);
    REQUIRE(inherited.ok());
    const auto colors = hue_counts(inherited);
    CHECK(colors[0] > inherited.w * inherited.h / 20);
    CHECK(colors[1] > inherited.w * inherited.h / 20);
    doc_apply_ops(current, -1, json::array({
        {{"op", "set_appearance"}, {"id", "b1"}, {"color", {0.5, 0.5, 0.5}}},
    }), false);
    REQUIRE(doc_render_view(current, shot, "front", false).value("ok", false));
    const Rgb overridden = decode_png(shot);
    REQUIRE(overridden.ok());
    const auto cleared = hue_counts(overridden);
    CHECK(cleared[0] == 0);
    CHECK(cleared[1] == 0);
}
