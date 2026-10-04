// PLM graphical revision diff (doc_render_diff) — ctest coverage for the engine slice of the
// "git diff for a 3D model" feature. Like test_interference.cpp this file never touches the host
// AICM loader: every assertion is made on the JSON doc_render_diff returns plus the PNG bytes it
// wrote, so there is no mesh-parsing or host-render dependency.
//
// Geometry is built entirely from primitive "box"/"cylinder" features placed with explicit
// params.at + params.centered:false (corner-at-`at` for a box, base-center-at-`at` for a
// cylinder), so every expected added/removed volume is an exact closed-form number.
//
// The PNG is decoded locally (miniz inflate + the filter-none row layout encode_png_rgb writes)
// so the colour-semantics assertions are made on REAL pixels: added must show green-dominant
// pixels, removed must show red-dominant ones. That is the only way to prove the render actually
// drew the diff sets rather than just returning plausible volumes.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <miniz.h>
#include <nlohmann/json.hpp>

#include "doc/session.h"

using json = nlohmann::json;
using chudware::doc::doc_new;
using chudware::doc::doc_apply_ops;
using chudware::doc::doc_render_diff;

namespace {

json prim(const char* type, const char* name, json extra) {
    json params = json::object();
    params["centered"] = false;
    for (auto it = extra.begin(); it != extra.end(); ++it) params[it.key()] = it.value();
    return json{{"op", "add_feature"},
                {"feature", json{{"type", type}, {"name", name}, {"params", params}}}};
}

json box(const char* name, double L, double W, double H, std::vector<double> at,
         json extra = json::object()) {
    json p = extra;
    p["length"] = L;
    p["width"] = W;
    p["height"] = H;
    p["at"] = at;
    return prim("box", name, p);
}

json cyl(const char* name, double radius, double height, std::vector<double> at,
         json extra = json::object()) {
    json p = extra;
    p["radius"] = radius;
    p["height"] = height;
    p["at"] = at;
    return prim("cylinder", name, p);
}

// Apply `ops` to a fresh part doc, REQUIRE every feature ok, return the doc id.
std::string build(const char* name, const json& ops) {
    json nr = doc_new("part", name);
    const std::string id = nr.at("doc_id").get<std::string>();
    json resp = doc_apply_ops(id, -1, ops, false);
    for (const json& st : resp.at("rebuild").at("feature_status")) {
        const std::string err = st.contains("error") && st["error"].is_string()
                                    ? st["error"].get<std::string>() : std::string();
        REQUIRE_MESSAGE(st.value("ok", false),
                        "feature '" << st.value("id", std::string()) << "' failed: " << err);
    }
    return id;
}

// The account's own temp directory (TMPDIR/TEMP), never a literal /tmp: a fixed
// /tmp name written by another account on a shared Mac is unwritable here.
std::string tmp_png(const char* stem) {
    return (std::filesystem::temp_directory_path() /
            (std::string("chudware_render_diff_") + stem + ".png"))
        .string();
}

struct Rgb {
    int w = 0, h = 0;
    std::vector<unsigned char> px;  // w*h*3
    bool ok() const { return w > 0 && h > 0 && px.size() == (size_t)w * h * 3; }
};

uint32_t be32(const unsigned char* p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

// Decode the exact PNG flavour encode_png_rgb emits: 8-bit RGB, colour type 2, filter method 0
// with every scanline using filter "none". Anything else -> !ok(), which the tests treat as a
// failure rather than silently passing.
Rgb decode_png(const std::string& path) {
    Rgb out;
    std::ifstream f(path, std::ios::binary);
    if (!f) return out;
    std::string buf((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
    if (buf.size() < 8) return out;
    const unsigned char* d = (const unsigned char*)buf.data();
    size_t n = buf.size(), off = 8;
    std::string idat;
    int bit_depth = 0, color_type = -1, filter_method = -1;
    while (off + 8 <= n) {
        const uint32_t len = be32(d + off);
        const std::string tag((const char*)d + off + 4, 4);
        const size_t data_off = off + 8;
        if (data_off + len > n) break;
        if (tag == "IHDR" && len >= 13) {
            out.w = (int)be32(d + data_off);
            out.h = (int)be32(d + data_off + 4);
            bit_depth = d[data_off + 8];
            color_type = d[data_off + 9];
            filter_method = d[data_off + 11];
        } else if (tag == "IDAT") {
            idat.append((const char*)d + data_off, len);
        } else if (tag == "IEND") {
            break;
        }
        off = data_off + len + 4;  // + CRC
    }
    if (out.w <= 0 || out.h <= 0 || bit_depth != 8 || color_type != 2 || filter_method != 0) {
        out.w = out.h = 0;
        return out;
    }
    const size_t stride = 1 + (size_t)out.w * 3;
    std::vector<unsigned char> raw(stride * (size_t)out.h);
    mz_ulong rlen = (mz_ulong)raw.size();
    if (mz_uncompress(raw.data(), &rlen, (const unsigned char*)idat.data(),
                      (mz_ulong)idat.size()) != MZ_OK ||
        rlen != raw.size()) {
        out.w = out.h = 0;
        return out;
    }
    out.px.resize((size_t)out.w * out.h * 3);
    for (int y = 0; y < out.h; ++y) {
        if (raw[(size_t)y * stride] != 0) {  // only filter "none" is produced
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

// Count pixels where one channel clearly dominates the other two. The diff colours are shaded by
// a 0.35..1.0 lambert term, which scales all three channels together, so the dominance ordering
// survives shading; the 245-grey background and the ghosted grey bulk are near-neutral and the
// header text is a light blue-white, so neither can register as green- or red-dominant.
long count_dominant(const Rgb& im, int ch, int margin = 25, int floor_v = 55) {
    long n = 0;
    for (size_t i = 0; i + 2 < im.px.size(); i += 3) {
        const int v[3] = {im.px[i], im.px[i + 1], im.px[i + 2]};
        const int a = v[(ch + 1) % 3], b = v[(ch + 2) % 3];
        if (v[ch] >= floor_v && v[ch] > a + margin && v[ch] > b + margin) ++n;
    }
    return n;
}

long file_size(const std::string& path) {
    std::ifstream f(path, std::ios::binary | std::ios::ate);
    return f ? (long)f.tellg() : -1L;
}

const double kBossVol = 3.14159265358979 * 4.0 * 4.0 * 5.0;  // r4 h5 cylinder ~= 251.33 mm^3

// A = plain 30x20x10 block.
json plate_ops() { return json::array({box("Blk", 30, 20, 10, {0, 0, 0})}); }

// B (added case) = the same block plus an r4 h5 boss standing on its top face.
json plate_plus_boss_ops() {
    return json::array({box("Blk", 30, 20, 10, {0, 0, 0}),
                        cyl("Boss", 4, 5, {15, 10, 10})});
}

// B (removed case) = the same block with an r4 h5 pocket sunk into its top face.
json plate_minus_pocket_ops() {
    return json::array({box("Blk", 30, 20, 10, {0, 0, 0}),
                        cyl("Pocket", 4, 5, {15, 10, 5}, json{{"mode", "cut"}})});
}

}  // namespace

// (1) B = A + boss. The boss volume shows up entirely as ADDED, nothing is removed, the pair is
// not identical, and the written PNG really contains green-dominant pixels (proving the green
// added body was rasterized, not just measured).
TEST_CASE("render_diff: an added boss reports added volume and paints green pixels") {
    const std::string a = build("DiffA1", plate_ops());
    const std::string b = build("DiffB1", plate_plus_boss_ops());
    const std::string path = tmp_png("added");
    std::remove(path.c_str());

    json r = doc_render_diff(json{{"doc_a", a}, {"doc_b", b}, {"out_path", path}});
    REQUIRE_MESSAGE(r.value("ok", false), "render_diff failed: " << r.dump());
    CHECK(r.value("degraded", false) == false);
    CHECK(r.value("identical", true) == false);
    CHECK(r.at("added_volume_mm3").get<double>() == doctest::Approx(kBossVol).epsilon(0.02));
    CHECK(r.at("removed_volume_mm3").get<double>() < 1e-6);
    CHECK(r.at("added_regions").get<int>() == 1);
    CHECK(r.at("removed_regions").get<int>() == 0);
    CHECK(r.at("mode").get<std::string>() == "context");
    CHECK(r.at("path").get<std::string>() == path);
    CHECK(file_size(path) > 1000);

    Rgb im = decode_png(path);
    REQUIRE_MESSAGE(im.ok(), "could not decode the diff PNG at " << path);
    CHECK(im.w == 640);
    CHECK(im.h == 480);
    const long green = count_dominant(im, 1);
    CHECK_MESSAGE(green > 20, "expected green-dominant added pixels, got " << green);
    CHECK_MESSAGE(count_dominant(im, 0) == 0, "unexpected red pixels in an added-only diff");
}

// (2) B = A - pocket. Mirror image of (1): the pocket volume is REMOVED (red), nothing added.
TEST_CASE("render_diff: a removed pocket reports removed volume and paints red pixels") {
    const std::string a = build("DiffA2", plate_ops());
    const std::string b = build("DiffB2", plate_minus_pocket_ops());
    const std::string path = tmp_png("removed");
    std::remove(path.c_str());

    json r = doc_render_diff(json{{"doc_a", a}, {"doc_b", b}, {"out_path", path}});
    REQUIRE_MESSAGE(r.value("ok", false), "render_diff failed: " << r.dump());
    CHECK(r.value("identical", true) == false);
    CHECK(r.at("removed_volume_mm3").get<double>() == doctest::Approx(kBossVol).epsilon(0.02));
    CHECK(r.at("added_volume_mm3").get<double>() < 1e-6);
    CHECK(r.at("removed_regions").get<int>() == 1);
    CHECK(r.at("added_regions").get<int>() == 0);

    Rgb im = decode_png(path);
    REQUIRE_MESSAGE(im.ok(), "could not decode the diff PNG at " << path);
    const long red = count_dominant(im, 0);
    CHECK_MESSAGE(red > 20, "expected red-dominant removed pixels, got " << red);
    CHECK_MESSAGE(count_dominant(im, 1) == 0, "unexpected green pixels in a removed-only diff");
}

// (3) Identical geometry built independently in two docs: identical:true, match_pct >= 99.5, and
// a valid PNG (the grey unchanged model) is STILL produced so callers always have an image.
TEST_CASE("render_diff: identical docs report identical:true and still write a PNG") {
    const std::string a = build("DiffA3", plate_ops());
    const std::string b = build("DiffB3", plate_ops());
    const std::string path = tmp_png("identical");
    std::remove(path.c_str());

    json r = doc_render_diff(json{{"doc_a", a}, {"doc_b", b}, {"out_path", path}});
    REQUIRE_MESSAGE(r.value("ok", false), "render_diff failed: " << r.dump());
    CHECK(r.value("identical", false) == true);
    CHECK(r.at("match_pct").get<double>() >= 99.5);
    CHECK(r.at("unchanged_volume_mm3").get<double>() == doctest::Approx(6000.0).epsilon(0.01));
    CHECK(file_size(path) > 1000);
    Rgb im = decode_png(path);
    REQUIRE_MESSAGE(im.ok(), "identical-doc diff produced no decodable PNG");
}

// (4) mode:"changes_only" — the drawn set is JUST the residue, with the camera still framed on
// the full A∪B union, so a genuinely small edit lands on very few pixels. First the plain
// added-boss pair, then a DELIBERATELY tiny change (an r1.5 h2 pin on a 200x150 plate) whose
// painted footprint is below the default W*H/400 floor — that second render can only succeed if
// the lowered min_painted threshold is actually plumbed through render_view_to_rgb.
TEST_CASE("render_diff: changes_only renders a sparse small change without a failed-render report") {
    const std::string a = build("DiffA4", plate_ops());
    const std::string b = build("DiffB4", plate_plus_boss_ops());
    const std::string path = tmp_png("changes_only");
    std::remove(path.c_str());

    json r = doc_render_diff(json{{"doc_a", a}, {"doc_b", b}, {"out_path", path},
                                 {"mode", "changes_only"}, {"width", 640}, {"height", 480}});
    REQUIRE_MESSAGE(r.value("ok", false), "changes_only render_diff failed: " << r.dump());
    CHECK(r.value("degraded", false) == false);
    CHECK(r.at("mode").get<std::string>() == "changes_only");
    Rgb im = decode_png(path);
    REQUIRE_MESSAGE(im.ok(), "changes_only produced no decodable PNG");
    const long green = count_dominant(im, 1);
    CHECK_MESSAGE(green > 20, "expected the added boss drawn in changes_only, got " << green);

    // The min_painted proof: a 3mm pin inside a 200mm frame projects to a footprint well under
    // the default 640*480/400 = 768 px "nothing drawn" guard.
    const std::string big_a = build("DiffA4b", json::array({box("Plate", 200, 150, 20, {0, 0, 0})}));
    const std::string big_b = build("DiffB4b",
                                    json::array({box("Plate", 200, 150, 20, {0, 0, 0}),
                                                 cyl("Pin", 1.5, 2, {100, 75, 20})}));
    const std::string tiny = tmp_png("changes_only_tiny");
    std::remove(tiny.c_str());
    json tr = doc_render_diff(json{{"doc_a", big_a}, {"doc_b", big_b}, {"out_path", tiny},
                                  {"mode", "changes_only"}, {"width", 640}, {"height", 480}});
    REQUIRE_MESSAGE(tr.value("ok", false), "tiny changes_only render failed: " << tr.dump());
    CHECK(tr.at("added_volume_mm3").get<double>() > 0.0);
    Rgb tim = decode_png(tiny);
    REQUIRE_MESSAGE(tim.ok(), "tiny changes_only produced no decodable PNG");
    const long tiny_green = count_dominant(tim, 1);
    CHECK_MESSAGE(tiny_green > 0, "the tiny pin was not drawn at all: " << tiny_green);
    CHECK_MESSAGE(tiny_green < (long)tim.w * tim.h / 400,
                  "tiny pin covered " << tiny_green << " px — no longer below the default "
                  "nothing-drawn floor, so this case stopped exercising min_painted");
}

// (5) Camera registration + geometry-sourced numbers. context and changes_only at the same
// view/size must report the same resolved view and both succeed (shared A∪B framing). And a
// third render from a view where the boss is HIDDEN behind the bulk block still reports the same
// added volume — proving the numbers come from the booleans, not from what the camera can see.
TEST_CASE("render_diff: both modes share one camera and volumes come from geometry, not pixels") {
    const std::string a = build("DiffA5", plate_ops());
    const std::string b = build("DiffB5", plate_plus_boss_ops());

    const json view = json{{"view", "front"}};
    json ctx = doc_render_diff(json{{"doc_a", a}, {"doc_b", b},
                                    {"out_path", tmp_png("reg_ctx")}, {"view", view},
                                    {"width", 400}, {"height", 300}, {"mode", "context"}});
    json chg = doc_render_diff(json{{"doc_a", a}, {"doc_b", b},
                                    {"out_path", tmp_png("reg_chg")}, {"view", view},
                                    {"width", 400}, {"height", 300}, {"mode", "changes_only"}});
    REQUIRE_MESSAGE(ctx.value("ok", false), "context render failed: " << ctx.dump());
    REQUIRE_MESSAGE(chg.value("ok", false), "changes_only render failed: " << chg.dump());
    CHECK(ctx.at("view").get<std::string>() == chg.at("view").get<std::string>());
    CHECK(ctx.at("added_volume_mm3").get<double>() ==
          doctest::Approx(chg.at("added_volume_mm3").get<double>()));

    Rgb ictx = decode_png(tmp_png("reg_ctx"));
    Rgb ichg = decode_png(tmp_png("reg_chg"));
    REQUIRE(ictx.ok());
    REQUIRE(ichg.ok());
    CHECK(ictx.w == 400);
    CHECK(ictx.w == ichg.w);
    CHECK(ictx.h == ichg.h);

    // "bottom" looks straight up at the block's underside: the boss is entirely occluded by the
    // 10mm plate, so no green can be visible from here — yet added_volume_mm3 is unchanged.
    json hidden = doc_render_diff(json{{"doc_a", a}, {"doc_b", b},
                                       {"out_path", tmp_png("reg_hidden")},
                                       {"view", json{{"view", "bottom"}}},
                                       {"mode", "context"}});
    REQUIRE_MESSAGE(hidden.value("ok", false), "hidden-boss render failed: " << hidden.dump());
    CHECK(hidden.at("added_volume_mm3").get<double>() ==
          doctest::Approx(kBossVol).epsilon(0.02));
    CHECK(hidden.at("added_regions").get<int>() == 1);
}
