// Task 03: on-request labelled views. Exercises the new doc_render_view / doc_render_contact_sheet
// session entries against a real session doc (an asymmetric box, so front/right/top silhouettes
// genuinely differ). We can't pixel-diff with a vision model in CI, so instead we assert the camera
// actually changed by proving the encoded PNGs differ between distinct views and are byte-identical
// for equivalent / repeated specs — i.e. the view spec drives the render deterministically.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

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
