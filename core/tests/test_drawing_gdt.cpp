// GD&T + custom-section drawing coverage (core/doc/drawing_session.cpp + annot_draw.cpp +
// drawing.cpp section modes). Builds a real part (boxed block with a through hole), makes a
// drawing, then exercises the overhaul:
//   - every GD&T annotation (feature control frame, datum feature, surface finish, weld)
//     compiles to non-empty vector primitives via drawing_get's `draw` block;
//   - a HALF section with a cut line produces section faces + engine-computed hatch;
//   - a DXF export of the annotated sheet emits a non-zero entity count (the primitives
//     are pooled, not just the projected geometry).
// Gated on OCCT like the other doc tests (the source compiles through the kernel).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cstdlib>
#include <filesystem>
#include <string>

#include <nlohmann/json.hpp>

#include "doc/drawing_session.h"
#include "doc/export2d.h"
#include "doc/session.h"

using json = nlohmann::json;
namespace doc = aicad::doc;

namespace {
json dimv(double v) { return json{{"value", v}, {"expr", nullptr}}; }

// A 40x40x20 block with a Ø10 through hole, as a live source part doc.
std::string make_block() {
    json nr = doc::doc_new("part", "GdtHost");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Blk"},
             {"params", {{"length", dimv(40)}, {"width", dimv(40)}, {"height", dimv(20)}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "hole"}, {"name", "Bore"},
             {"params", {{"style", "simple"}, {"standard", "custom"}, {"diameter", dimv(10)},
                         {"end", "through_all"}}},
             {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
    });
    doc::doc_apply_ops(id, -1, ops, false);
    return id;
}

// Count draw primitives on an annotation's compiled `draw` block.
size_t draw_count(const json& ann) {
    if (!ann.contains("draw") || !ann["draw"].is_object()) return 0;
    const json& d = ann["draw"];
    return d.value("segments", json::array()).size() + d.value("arcs", json::array()).size() +
           d.value("tris", json::array()).size() + d.value("texts", json::array()).size() +
           d.value("dashed", json::array()).size();
}
}  // namespace

TEST_CASE("GD&T annotations compile to draw primitives + export") {
    const std::string src = make_block();
    REQUIRE_FALSE(src.empty());

    json created = doc::drawing_new(json::object(), json::object(), "GDT Drawing", src);
    const std::string did = created.at("doc_id").get<std::string>();
    REQUIRE_FALSE(did.empty());

    // Base front view at (130,175).
    json av = doc::drawing_add_view(did, "base", "front", "s1", json(1.0),
                                    json::array({130.0, 175.0}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();
    REQUIRE_FALSE(vid.empty());

    // Feature control frame: ⌀0.1 position to datum A (MMC).
    doc::drawing_add_gdt(did, vid,
                         json{{"symbol", "position"}, {"tol_value", 0.1}, {"tol_diameter", true},
                              {"tol_modifier", "mmc"},
                              {"datums", json::array({json{{"ref", "A"}}})},
                              {"a", json::array({130.0, 175.0})},
                              {"pos", json::array({150.0, 190.0})}});
    doc::drawing_add_datum(did, vid,
                           json{{"label", "A"}, {"a", json::array({110.0, 165.0})},
                                {"pos", json::array({110.0, 150.0})}});
    doc::drawing_add_surface_finish(did, vid,
                                    json{{"ra", 1.6}, {"machining", "required"},
                                         {"a", json::array({150.0, 175.0})},
                                         {"pos", json::array({160.0, 185.0})}});
    doc::drawing_add_weld(did, vid,
                          json{{"weld_type", "fillet"}, {"size", "6"},
                               {"a", json::array({120.0, 165.0})},
                               {"pos", json::array({120.0, 200.0})}});

    // A HALF section across the block.
    doc::drawing_add_section(did, vid, json(nullptr),
                             json::array({json::array({130.0, 160.0}), json::array({130.0, 190.0})}),
                             json(nullptr), json(nullptr), json::array({320.0, 175.0}), "s1",
                             json(false), json{{"mode", "half"}});

    json full = doc::drawing_get(did);
    REQUIRE(full.contains("sheets"));
    const json& views = full["sheets"][0]["views"];

    // Every GD&T annotation on the base view compiled to primitives.
    bool saw_gdt = false, saw_datum = false, saw_surf = false, saw_weld = false;
    bool saw_section_faces = false, saw_hatch = false;
    for (const json& v : views) {
        if (v.value("view_id", std::string()) == vid)
            for (const json& an : v.value("annotations", json::array())) {
                std::string k = an.value("kind", std::string());
                if (k == "gdt_frame") { saw_gdt = true; CHECK(draw_count(an) > 0); }
                if (k == "datum_feature") { saw_datum = true; CHECK(draw_count(an) > 0); }
                if (k == "surface_finish") { saw_surf = true; CHECK(draw_count(an) > 0); }
                if (k == "weld_symbol") { saw_weld = true; CHECK(draw_count(an) > 0); }
            }
        if (v.value("type", std::string()) == "section") {
            if (v.value("section_faces", json::array()).size() > 0) saw_section_faces = true;
            if (v.value("hatch", json::array()).size() > 0) saw_hatch = true;
        }
    }
    CHECK(saw_gdt);
    CHECK(saw_datum);
    CHECK(saw_surf);
    CHECK(saw_weld);
    CHECK(saw_section_faces);
    CHECK(saw_hatch);

    // Export the annotated sheet to DXF -> non-zero entities including the GD&T strokes.
    auto tmp = std::filesystem::temp_directory_path() / "aicad_gdt_test.dxf";
    json ex = doc::export_drawing(did, tmp.string(), "dxf");
    CHECK(ex.value("entities", 0) > 0);
    CHECK(ex.value("annotations", 0) > 0);
    std::error_code ec;
    std::filesystem::remove(tmp, ec);

    doc::drawing_close(did);
}
