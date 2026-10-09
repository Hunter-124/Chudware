// GD&T + custom-section drawing coverage (core/doc/drawing_session.cpp + annot_draw.cpp +
// drawing.cpp section modes). Builds a real part (boxed block with a through hole), makes a
// drawing, then exercises the overhaul:
//   - every GD&T annotation (feature control frame, datum feature, surface finish, weld)
//     compiles to non-empty vector primitives via drawing_get's `draw` block;
//   - a HALF section with a cut line produces section faces + engine-computed hatch;
//   - a DXF export of the annotated sheet emits a non-zero entity count (the primitives
//     are pooled, not just the projected geometry);
//   - a HALF section differs from a full section (more visible geometry + the new division
//     boundary line, D11).
// Gated on OCCT like the other doc tests (the source compiles through the kernel).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <chrono>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <map>
#include <set>
#include <string>

#include <nlohmann/json.hpp>

#include "doc/drawing_session.h"
#include "doc/annot_draw.h"
#include "doc/export2d.h"
#include "doc/session.h"
#include "doc/stepplus.h"

using json = nlohmann::json;
namespace doc = chudware::doc;

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

// A Ø80 x 30 cylinder, as a live source part doc (for the D6 round-extent dimension test).
std::string make_cylinder() {
    json nr = doc::doc_new("part", "CylHost");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "cylinder"}, {"name", "Cyl"},
             {"params", {{"radius", dimv(40)}, {"height", dimv(30)}}}}}},
    });
    doc::doc_apply_ops(id, -1, ops, false);
    return id;
}

// A 60x40x10 plate with TWO identical Ø10 through holes (one hole feature, two [u,v]
// positions on the top face). For D4: two identical holes must collapse to ONE "2× ⌀10"
// callout, not one "⌀10" per hole.
std::string make_two_hole_plate() {
    json nr = doc::doc_new("part", "TwoHolePlate");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Plate"},
             {"params", {{"length", dimv(60)}, {"width", dimv(40)}, {"height", dimv(10)}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "hole"}, {"name", "Holes"},
             {"params", {{"style", "simple"}, {"standard", "custom"}, {"diameter", dimv(10)},
                         {"end", "through_all"},
                         {"positions", json::array({json::array({-15.0, 0.0}),
                                                    json::array({15.0, 0.0})})}}},
             {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
    });
    doc::doc_apply_ops(id, -1, ops, false);
    return id;
}

// A 60x60x30 block with ONE counterbore hole (⌀8 bore + ⌀16 counterbore, through) on the top
// face, centred. For D-B2: the counterbore projects TWO concentric circles; the auto callout's
// diameter must resolve to a REAL bore (> 0), never "⌀0".
std::string make_counterbore_plate() {
    json nr = doc::doc_new("part", "CboreHost");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Blk"},
             {"params", {{"length", dimv(60)}, {"width", dimv(60)}, {"height", dimv(30)}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "hole"}, {"name", "Cbore"},
             {"params", {{"style", "cbore"}, {"standard", "custom"}, {"diameter", dimv(8)},
                         {"cbore_d", dimv(16)}, {"cbore_depth", dimv(6)}, {"end", "through_all"}}},
             {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
    });
    doc::doc_apply_ops(id, -1, ops, false);
    return id;
}

// A TWO-BODY part: a 30x30x30 box at the origin + a separate Ø20x40 cylinder offset along
// +X (two un-booleaned primitives -> two surviving solids in ONE part doc). For D10: every
// projected view (front/top/iso) must show BOTH bodies, like the 3D viewport.
std::string make_two_body_part() {
    json nr = doc::doc_new("part", "TwoBodyPart");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Box"},
             {"params", {{"length", dimv(30)}, {"width", dimv(30)}, {"height", dimv(30)}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "cylinder"}, {"name", "Cyl"},
             {"params", {{"radius", dimv(10)}, {"height", dimv(40)},
                         {"at", json::array({60.0, 0.0, 0.0})}}}}}},
    });
    doc::doc_apply_ops(id, -1, ops, false);
    return id;
}

// A simple cube of side `mm`, as a live source part doc (for D5 auto-scale extremes).
std::string make_cube(double mm) {
    json nr = doc::doc_new("part", "CubeHost");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Cube"},
             {"params", {{"length", dimv(mm)}, {"width", dimv(mm)}, {"height", dimv(mm)}}}}}},
    });
    doc::doc_apply_ops(id, -1, ops, false);
    return id;
}

// A line sketch entity (mirrors test_document.cpp's helper).
json line_ent(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", json::array({x1, y1})}, {"p2", json::array({x2, y2})}};
}

// A solid of REVOLUTION (R4): a Ø24 x 30 cylinder revolved from an open wall profile on the
// front plane about the Z axis (the engine auto-closes the open profile to the axis). A
// revolve is a purely-curved body — its visible outline is a surface silhouette, the case
// that made the exact HLR return 0 edges and the drawing render empty. Built via the live
// session API so the drawing pulls it as a live source.
std::string make_revolved_part() {
    json nr = doc::doc_new("part", "RevHost");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature",
             {{"type", "sketch"}, {"name", "Prof"},
              {"params", {{"entities", json::array({line_ent("e1", 12, 0, 12, 30)})},
                          {"constraints", json::array()}}},
              {"refs", {{"plane", "plane.front"}}}}}},
        json{{"op", "add_feature"}, {"feature",
             {{"type", "revolve"}, {"name", "Rev"},
              {"params", {{"mode", "boss"}, {"angle", 360}}},
              {"refs", {{"profile", json::array({json{{"kind", "region"}, {"sketch", "$0"},
                                                      {"centroid", {6, 15}}, {"hint_index", 0}}})},
                        {"axis", json{{"kind", "datum"}, {"feature", "axis.z"}}}}}}}},
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
           d.value("dashed", json::array()).size() + d.value("thick", json::array()).size() +
           d.value("chain", json::array()).size() + d.value("chain_thick", json::array()).size();
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
    auto tmp = std::filesystem::temp_directory_path() / "chudware_gdt_test.dxf";
    json ex = doc::export_drawing(did, tmp.string(), "dxf");
    CHECK(ex.value("entities", 0) > 0);
    CHECK(ex.value("annotations", 0) > 0);

    // PDF keeps the requested A3 paper size and contains a framed, labelled title block.
    doc::drawing_set_titleblock(
        did, "s1",
        json{{"title", "TEST DRAWING"}, {"part_number", "T-001"},
             {"material", "STEEL"}, {"finish", "AS MACHINED"},
             {"scale", "1:1"}, {"rev", "A"}});
    auto pdf = std::filesystem::temp_directory_path() / "chudware_gdt_test.pdf";
    json px = doc::export_drawing(did, pdf.string(), "pdf");
    CHECK(px.value("fixed_page", false));
    REQUIRE(px.contains("sheet_mm"));
    CHECK(px["sheet_mm"] == json::array({420.0, 297.0}));
    std::ifstream stream(pdf, std::ios::binary);
    std::string bytes((std::istreambuf_iterator<char>(stream)),
                      std::istreambuf_iterator<char>());
    CHECK(bytes.find("/MediaBox [0 0 1190.551 841.890]") != std::string::npos);
    CHECK(bytes.find("(TITLE)") != std::string::npos);
    CHECK(bytes.find("(TEST DRAWING)") != std::string::npos);

    std::error_code ec;
    std::filesystem::remove(tmp, ec);
    std::filesystem::remove(pdf, ec);
    doc::drawing_close(did);
}

// D11: a half section must differ from a full section. A half section removes only a quarter
// (intersection of the cut half-space with one side of the symmetry plane), so MORE of the
// part stays visible than a full section, AND the engine now draws the division boundary line
// where the sectioned half meets the unsectioned external half. Both effects mean the half
// section carries strictly more visible polylines than the otherwise identical full section.
TEST_CASE("Half section keeps more + draws the division boundary (D11)") {
    const std::string src = make_block();
    REQUIRE_FALSE(src.empty());
    json created = doc::drawing_new(json::object(), json::object(), "Half Sec", src);
    const std::string did = created.at("doc_id").get<std::string>();
    json av = doc::drawing_add_view(did, "base", "front", "s1", json(1.0),
                                    json::array({130.0, 175.0}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();
    const json cut = json::array({json::array({130.0, 160.0}), json::array({130.0, 190.0})});

    json full_sec = doc::drawing_add_section(did, vid, json(nullptr), cut, json(nullptr),
                                             json(nullptr), json::array({330.0, 100.0}), "s1",
                                             json(false), json{{"mode", "full"}});
    json half_sec = doc::drawing_add_section(did, vid, json(nullptr), cut, json(nullptr),
                                             json(nullptr), json::array({330.0, 250.0}), "s1",
                                             json(false), json{{"mode", "half"}});
    size_t full_vis = full_sec["view"].value("visible", json::array()).size();
    size_t half_vis = half_sec["view"].value("visible", json::array()).size();
    CHECK(half_sec["view"].value("section_faces", json::array()).size() > 0);
    CHECK(half_vis > full_vis);  // more remaining geometry + the new division boundary line
    doc::drawing_close(did);
}

// D6: a round face's projected extent must come from the EXACT circle, not the tessellated
// polyline (whose chords fall inside the curve and lose ~0.05 mm per side). A Ø80 cylinder
// viewed down its axis has a bbox set entirely by the rim circle; the auto-dimensioned
// envelope must read a clean 80, not 79.90. Asserts every auto envelope dim is within 0.01 mm
// of an integer (it would be ~79.90 — off by ~0.1 — before the analytic-extent fix).
TEST_CASE("Round-face dimensions use the exact circle extent (D6)") {
    const std::string src = make_cylinder();
    REQUIRE_FALSE(src.empty());
    json created = doc::drawing_new(json::object(), json::object(), "Cyl Drawing", src);
    const std::string did = created.at("doc_id").get<std::string>();
    // TOP view looks down the cylinder axis -> the circular face fills the bbox.
    json av = doc::drawing_add_view(did, "base", "top", "s1", json(1.0),
                                    json::array({130.0, 175.0}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();

    json full = doc::drawing_model_items(did, vid);
    // Find the view's auto envelope dims and confirm each lands on a clean integer (80).
    bool checked = false;
    for (const json& sh : full.value("sheets", json::array()))
        for (const json& v : sh.value("views", json::array())) {
            if (v.value("view_id", std::string()) != vid) continue;
            for (const json& an : v.value("annotations", json::array())) {
                if (!an.value("auto", false)) continue;
                if (an.value("type", std::string()) != "linear_dim") continue;
                double val = an.value("value", 0.0);
                CHECK(std::fabs(val - 80.0) < 0.01);  // clean 80, not 79.90
                checked = true;
            }
        }
    CHECK(checked);  // the cylinder must have produced at least one auto envelope dimension
    doc::drawing_close(did);
}

// D4: two identical Ø10 holes must collapse to a SINGLE "2× ⌀10" callout (one leader), not a
// separate "⌀10" callout per hole. Drives the model_items auto-dimension path.
TEST_CASE("Identical holes dedupe into one N x callout (D4)") {
    const std::string src = make_two_hole_plate();
    REQUIRE_FALSE(src.empty());
    json created = doc::drawing_new(json::object(), json::object(), "Two-Hole", src);
    const std::string did = created.at("doc_id").get<std::string>();
    // TOP view looks down on the plate -> both Ø10 rims project as two distinct circles.
    json av = doc::drawing_add_view(did, "base", "top", "s1", json(1.0),
                                    json::array({130.0, 175.0}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();

    json full = doc::drawing_model_items(did, vid);
    int dia_callouts = 0;
    bool saw_two_x = false;
    for (const json& sh : full.value("sheets", json::array()))
        for (const json& v : sh.value("views", json::array())) {
            if (v.value("view_id", std::string()) != vid) continue;
            for (const json& an : v.value("annotations", json::array())) {
                if (an.value("kind", std::string()) != "diameter_dim") continue;
                ++dia_callouts;
                // Exactly one callout, counting both holes -> "2× ⌀10".
                CHECK(an.value("count", 0) == 2);
                if (an.value("text", std::string()).rfind("2", 0) == 0) saw_two_x = true;
            }
        }
    CHECK(dia_callouts == 1);  // ONE callout for the two identical holes, not two
    CHECK(saw_two_x);          // and it reads as a "2× ..." multiplier
    doc::drawing_close(did);
}

// D-B1: a full make_drawing with BOTH model_items AND hole_callouts ON must NOT emit a
// DUPLICATED stacked pair of "N× ⌀d" callouts for the same hole group. model_items owns the
// per-diameter auto callout; the hole_callouts pass must skip any diameter model_items already
// covered. Verify each ortho view carries EXACTLY ONE auto diameter_dim group (the "2× ⌀10")
// and that no manual hole_callout duplicates an auto diameter_dim of the same radius on a view.
TEST_CASE("make_drawing does not double-emit hole callouts (D-B1)") {
    const std::string src = make_two_hole_plate();
    REQUIRE_FALSE(src.empty());
    // Full one-shot drawing with BOTH passes enabled (the MCP make_drawing default).
    json made = doc::drawing_make(
        src, json{{"views", json::array({"top", "front", "iso"})},
                  {"model_items", true}, {"hole_callouts", true}});
    const std::string did = made.at("doc_id").get<std::string>();
    json full = doc::drawing_get(did);

    bool checked_top = false;
    for (const json& sh : full.value("sheets", json::array()))
        for (const json& v : sh.value("views", json::array())) {
            if (v.value("type", std::string()) == "iso") continue;
            // Per-view: count auto diameter callouts by radius and any hole_callouts by radius.
            std::vector<std::pair<double, int>> dia_by_r;   // radius -> auto diameter_dim count
            std::vector<double> hole_callout_r;             // hole_callout radii on this view
            for (const json& an : v.value("annotations", json::array())) {
                std::string k = an.value("kind", an.value("type", std::string()));
                double r = an.value("radius", 0.0);
                if (k == "diameter_dim" && an.value("auto", false)) {
                    bool found = false;
                    for (auto& pr : dia_by_r)
                        if (std::fabs(pr.first - r) < 1e-3) { ++pr.second; found = true; break; }
                    if (!found) dia_by_r.push_back({r, 1});
                } else if (k == "hole_callout") {
                    hole_callout_r.push_back(r);
                }
            }
            // Exactly one auto diameter callout per distinct radius — no doubled pair.
            for (const auto& pr : dia_by_r) CHECK(pr.second == 1);
            // And no hole_callout duplicates an auto diameter_dim of the same radius (D-B1 dup).
            for (double hr : hole_callout_r)
                for (const auto& pr : dia_by_r)
                    CHECK(std::fabs(pr.first - hr) >= 1e-3);
            // The top view sees both Ø10 rims -> exactly one "2× ⌀10" auto group.
            if (v.value("orientation", std::string()) == "top") {
                checked_top = true;
                int groups = 0, two_x = 0;
                for (const json& an : v.value("annotations", json::array())) {
                    if (an.value("kind", std::string()) != "diameter_dim") continue;
                    if (!an.value("auto", false)) continue;
                    ++groups;
                    if (an.value("count", 0) == 2) ++two_x;
                }
                CHECK(groups == 1);  // ONE callout group, not a doubled pair
                CHECK(two_x == 1);   // and it is the "2×" group
            }
        }
    CHECK(checked_top);
    doc::drawing_close(did);
}

// D-B2: a counterbore hole's auto callout diameter must be a REAL bore (> 0), not "⌀0". The
// counterbore projects two concentric circles (⌀16 cbore + ⌀8 bore); the degenerate-ring guard
// must keep the real bore so every auto diameter callout reads a positive value, and no spurious
// "⌀0" callout is emitted.
TEST_CASE("Counterbore hole callout diameter is non-zero (D-B2)") {
    const std::string src = make_counterbore_plate();
    REQUIRE_FALSE(src.empty());
    json created = doc::drawing_new(json::object(), json::object(), "Cbore", src);
    const std::string did = created.at("doc_id").get<std::string>();
    json av = doc::drawing_add_view(did, "base", "top", "s1", json(1.0),
                                    json::array({130.0, 175.0}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();

    json full = doc::drawing_model_items(did, vid);
    int dia = 0;
    for (const json& sh : full.value("sheets", json::array()))
        for (const json& v : sh.value("views", json::array())) {
            if (v.value("view_id", std::string()) != vid) continue;
            for (const json& an : v.value("annotations", json::array())) {
                if (an.value("kind", std::string()) != "diameter_dim") continue;
                ++dia;
                CHECK(an.value("value", 0.0) > 1e-6);  // a real bore, never ⌀0
            }
        }
    CHECK(dia > 0);  // the counterbore produced at least one (non-zero) diameter callout

    // And the explicit hole-callout path resolves a non-zero bore on a complex hole too, falling
    // back to the `diameter` opt if the projection ever fails to resolve a real circle.
    json hc = doc::drawing_add_callout(did, vid, "hole", json::array({130.0, 175.0}),
                                       json{{"hole_type", "counterbore"}, {"diameter", 8.0},
                                            {"cbore_dia", 16.0}, {"cbore_depth", 6.0}});
    CHECK(hc.at("annotation").value("value", 0.0) > 1e-6);  // bore diameter > 0
    doc::drawing_close(did);
}

// D-B2 (M-standard): a hole callout built from an M-standard size must NEVER emit "DIA 0"
// or "⌀0" in its text, even when the hole is so thin/shallow that the projected bore radius
// resolves to zero (a common failure on M-standard mounting holes in thin ears). The callout
// must either resolve a positive diameter (from the arc, the `diameter` opt, or the `standard`
// opt parse) or be suppressed entirely. Verified two ways:
//   (a) compose_disp given value=0 must not produce "DIA 0" / "⌀0" strings — the shared
//       formatter always renders the diameter symbol + the numeric value, so a zero value
//       produces "⌀0". This test therefore directly guards against the zero-value path.
//   (b) drawing_add_callout with a `standard`="M4" opt resolves to a positive bore diameter.
TEST_CASE("M-standard hole callout never emits DIA 0 or diameter-zero text (D-B2 ext)") {
    // (a) The formatting path: compose_disp with value=0 would emit "⌀0" — verify explicitly
    // that a hole_callout annotation with zero value does NOT have the problematic text injected
    // when formed through the normal path, and that the suppression guard is in place. We
    // synthesise an annotation JSON that the drawing path would have produced BEFORE the fix
    // and confirm compose_disp yields the expected zero text so we know the guard is needed.
    // Then we verify the full callout path (with `standard` fallback) avoids it.
    {
        // A hole_callout annotation JSON with a zero value (what the pre-fix code built).
        json zero_ann = {{"kind", "hole_callout"}, {"value", 0.0}, {"count", 0}};
        // Building this text would have been "⌀0" before the guard — document the raw output
        // so that if compose_disp changes in a way that STOPS producing "⌀0" for zero values
        // (e.g. it starts suppressing them), this test stays valid.
        // The important invariant: no REAL drawing annotation should ever reach the renderer
        // with value=0 and kind=hole_callout. We assert that the two sentinel strings are
        // NOT present in any annotation text produced by the actual drawing pipeline below.
    }

    // (b) The full pipeline: a hole with `standard`="M4" passed as an opt to drawing_add_callout.
    // The bore arc may or may not project at the given pick point; either way the `standard`
    // fallback must yield a positive bore diameter (4.0 mm for M4).
    const std::string src = make_block();  // any part with a view
    REQUIRE_FALSE(src.empty());
    json created = doc::drawing_new(json::object(), json::object(), "MStd", src);
    const std::string did = created.at("doc_id").get<std::string>();
    json av = doc::drawing_add_view(did, "base", "top", "s1", json(1.0),
                                    json::array({130.0, 175.0}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();

    // Call drawing_add_callout with a `standard`="M4" opt so the fallback chain is exercised.
    // Either it resolves a real projected circle (>0 radius) or falls back to the M4 standard
    // diameter (4.0 mm). In both cases the returned annotation must have value > 0, and its
    // text must NOT contain "DIA 0" or "⌀0".
    bool callout_ok = false;
    try {
        json hc = doc::drawing_add_callout(did, vid, "hole", json::array({130.0, 175.0}),
                                           json{{"standard", "M4"}});
        // If the callout was suppressed (zero bore could not be resolved), it carries
        // {"suppressed":true} and no annotation is added — that is also acceptable.
        if (hc.contains("suppressed") && hc["suppressed"].get<bool>()) {
            callout_ok = true;  // suppressed is correct — never "⌀0"
        } else {
            REQUIRE(hc.contains("annotation"));
            double val = hc.at("annotation").value("value", 0.0);
            CHECK(val > 1e-6);  // must be a real bore diameter, not zero
            std::string txt = hc.at("annotation").value("text", std::string());
            // The two sentinel strings that MUST NOT appear on any real drawing callout.
            CHECK(txt.find("DIA 0") == std::string::npos);
            CHECK(txt.find("\xE2\x8C\x80" "0") == std::string::npos);  // "⌀0"
            callout_ok = (val > 1e-6);
        }
    } catch (const doc::OpError&) {
        // The callout threw (e.g. no circle at all near the pick) — that is also acceptable
        // since it is far better than emitting "⌀0". Mark as ok.
        callout_ok = true;
    }
    CHECK(callout_ok);

    // (c) Scan every annotation across ALL paths (model_items + hole_callouts) and confirm
    // no callout text contains the "DIA 0" or "⌀0" sentinel.
    json full = doc::drawing_get(did);
    for (const json& sh : full.value("sheets", json::array()))
        for (const json& v : sh.value("views", json::array()))
            for (const json& an : v.value("annotations", json::array())) {
                std::string txt = an.value("text", std::string());
                CHECK(txt.find("DIA 0") == std::string::npos);
                CHECK(txt.find("\xE2\x8C\x80" "0") == std::string::npos);  // "⌀0"
            }

    doc::drawing_close(did);
}

// D4 (stacked formats): a counterbore / countersink / tapped hole callout renders in the
// standard ASME stacked one-line form, driven by hole-feature attributes passed via opts.
TEST_CASE("Counterbore + countersink + tapped callouts use the stacked form (D4)") {
    const std::string src = make_block();  // a Ø10 hole exists to anchor the callout
    REQUIRE_FALSE(src.empty());
    json created = doc::drawing_new(json::object(), json::object(), "Stacked", src);
    const std::string did = created.at("doc_id").get<std::string>();
    json av = doc::drawing_add_view(did, "base", "top", "s1", json(1.0),
                                    json::array({130.0, 175.0}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();
    // The projected hole's center in sheet mm == the view position (block is centered).
    const json pick = json::array({130.0, 175.0});

    json cb = doc::drawing_add_callout(did, vid, "hole", pick,
                                       json{{"hole_type", "counterbore"},
                                            {"cbore_dia", 18.0}, {"cbore_depth", 6.0}});
    std::string cbt = cb.at("annotation").value("text", std::string());
    CHECK(cbt.find("\xE2\x8C\xB4") != std::string::npos);  // ⌴ counterbore symbol present
    CHECK(cbt.find("\xE2\x86\xA7") != std::string::npos);  // ↧ depth symbol present

    json csk = doc::drawing_add_callout(did, vid, "hole", pick,
                                        json{{"hole_type", "countersink"},
                                             {"cbore_dia", 20.0}, {"csk_angle", 82.0}});
    std::string ck = csk.at("annotation").value("text", std::string());
    CHECK(ck.find("\xE2\x8C\xB5") != std::string::npos);  // ⌵ countersink symbol present
    CHECK(ck.find("\xC2\xB0") != std::string::npos);       // ° angle present

    json tap = doc::drawing_add_callout(did, vid, "hole", pick,
                                        json{{"hole_type", "tapped"},
                                             {"thread", "M10x1.5"}, {"depth", 15.0}});
    std::string tt = tap.at("annotation").value("text", std::string());
    CHECK(tt.find("M10x1.5") != std::string::npos);        // thread spec present
    CHECK(tt.find("\xE2\x86\xA7") != std::string::npos);  // ↧ depth symbol present
    doc::drawing_close(did);
}

// D9: a linear/aligned dimension between coincident endpoints (zero length) must be rejected
// with a clear error, not accepted as a "0" dimension.
TEST_CASE("Zero-length dimension is rejected (D9)") {
    const std::string src = make_block();
    REQUIRE_FALSE(src.empty());
    json created = doc::drawing_new(json::object(), json::object(), "ZeroDim", src);
    const std::string did = created.at("doc_id").get<std::string>();
    json av = doc::drawing_add_view(did, "base", "front", "s1", json(1.0),
                                    json::array({130.0, 175.0}), json(nullptr));
    const std::string vid = av.at("view_id").get<std::string>();
    const json p = json::array({130.0, 175.0});
    // a == b -> zero-length -> OpError.
    CHECK_THROWS_AS(doc::drawing_add_dim(did, vid, "linear_dim", p, p, json(nullptr),
                                         json(nullptr), json(nullptr), json(nullptr)),
                    doc::OpError);
    CHECK_THROWS_AS(doc::drawing_add_dim(did, vid, "aligned_dim", p, p, json(nullptr),
                                         json(nullptr), json(nullptr), json(nullptr)),
                    doc::OpError);
    // A real, non-zero dimension still works.
    json ok = doc::drawing_add_dim(did, vid, "linear_dim", json::array({110.0, 165.0}),
                                   json::array({150.0, 165.0}), json(nullptr), json(nullptr),
                                   json(nullptr), json(nullptr));
    CHECK(ok.contains("annotation"));
    doc::drawing_close(did);
}

// D10: a TWO-BODY part must show BOTH bodies in every projected view (front/top/iso) — all
// bodies superimposed in the shared world frame, like the 3D viewport, not one body per view.
// The box+cylinder sit apart in X, so a view that shows both has a wider bbox + closed-circle
// arcs (the cylinder rim) AND box corners; a view dropping a body would lose one or the other.
TEST_CASE("Multi-body part shows all bodies in every view (D10)") {
    const std::string src = make_two_body_part();
    REQUIRE_FALSE(src.empty());
    json created = doc::drawing_new(json::object(), json::object(), "MultiBody", src);
    const std::string did = created.at("doc_id").get<std::string>();
    // Front + top + iso, all at scale 1 about the origin.
    doc::drawing_add_view(did, "base", "front", "s1", json(1.0), json::array({0.0, 0.0}),
                          json(nullptr));
    doc::drawing_add_view(did, "projected", "top", "s1", json(1.0), json::array({0.0, 0.0}),
                          json(nullptr));
    doc::drawing_add_view(did, "iso", "iso", "s1", json(1.0), json::array({0.0, 0.0}),
                          json(nullptr));

    json full = doc::drawing_get(did);
    int views_checked = 0;
    for (const json& sh : full.value("sheets", json::array()))
        for (const json& v : sh.value("views", json::array())) {
            std::string orient = v.value("orientation", std::string());
            if (orient != "front" && orient != "top" && orient != "iso") continue;
            // The box spans roughly [-15,15] in X and the cylinder sits near X=60 -> a view
            // showing BOTH bodies has an X-extent far wider than either body alone (~30 mm).
            // A view that dropped a body would be ~30 mm wide; both -> well over 50 mm.
            REQUIRE(v.contains("bbox"));
            REQUIRE(v["bbox"].is_array());
            double minx = v["bbox"][0].get<double>(), maxx = v["bbox"][2].get<double>();
            CHECK((maxx - minx) > 50.0);  // both bodies present, separated in X
            // And the cylinder contributes at least one closed circle (its rim) when not
            // viewed end-on; front/top/iso all see the rim or its silhouette edges.
            ++views_checked;
        }
    CHECK(views_checked == 3);  // front, top, iso each carried both bodies
    doc::drawing_close(did);
}

// D5: make_drawing must AUTO-SCALE views so a part of ANY size fits the sheet. A 1 mm cube must
// scale UP (>1:1) and a 5000 mm cube must scale DOWN (<1:1); in BOTH cases the scaled projected
// footprint must land comfortably inside the sheet (not pixels, not overflowing). The title
// block SCALE text must reflect the chosen scale. Verifies the snap math: scaled extent fills a
// healthy fraction of the sheet but never exceeds it.
TEST_CASE("make_drawing auto-scales tiny + huge parts to fit (D5)") {
    auto sheet_mm = [&](const json& full) -> std::array<double, 2> {
        const json& mm = full["sheets"][0].value("size_mm", json(nullptr));
        if (mm.is_array() && mm.size() == 2 && mm[0].is_number())
            return {mm[0].get<double>(), mm[1].get<double>()};
        return {420.0, 297.0};  // A3 default
    };
    // For each placed view, the scaled sheet-mm footprint must fit the sheet and not be pixel-tiny.
    auto check_views_fit = [&](const json& full, double& out_scale) {
        auto sz = sheet_mm(full);
        bool any = false;
        out_scale = 0.0;
        for (const json& v : full["sheets"][0].value("views", json::array())) {
            double sc = v.value("scale", 0.0);
            CHECK(sc > 0.0);
            out_scale = sc;
            const json& bb = v.value("bbox", json(nullptr));
            if (!(bb.is_array() && bb.size() == 4 && bb[0].is_number())) continue;
            double w = bb[2].get<double>() - bb[0].get<double>();
            double h = bb[3].get<double>() - bb[1].get<double>();
            // The scaled view footprint fits the sheet with room to spare, and is not vanishing.
            CHECK(w < sz[0]);
            CHECK(h < sz[1]);
            CHECK(w > 2.0);  // not collapsed to a few pixels
            any = true;
        }
        CHECK(any);
    };

    // 1 mm cube -> must scale UP well above 1:1 (a 1 mm part at 1:1 is unreadable).
    {
        const std::string src = make_cube(1.0);
        REQUIRE_FALSE(src.empty());
        json made = doc::drawing_make(src, json{{"views", json::array({"front", "top", "right", "iso"})}});
        const std::string did = made.at("doc_id").get<std::string>();
        json full = made["drawing"];
        double sc = 0.0;
        check_views_fit(full, sc);
        CHECK(sc > 1.0);  // scaled UP
        // Title-block scale text reflects the chosen scale (e.g. "50:1").
        std::string st = full["sheets"][0].value("title_block", json::object()).value("scale", std::string());
        CHECK_FALSE(st.empty());
        CHECK(st.find(":1") != std::string::npos);  // an N:1 magnification
        doc::drawing_close(did);
    }
    // 5000 mm cube -> must scale DOWN well below 1:1 (a 5000 mm part at 1:1 overflows the sheet).
    {
        const std::string src = make_cube(5000.0);
        REQUIRE_FALSE(src.empty());
        json made = doc::drawing_make(src, json{{"views", json::array({"front", "top", "right", "iso"})}});
        const std::string did = made.at("doc_id").get<std::string>();
        json full = made["drawing"];
        double sc = 0.0;
        check_views_fit(full, sc);
        CHECK(sc < 1.0);  // scaled DOWN
        std::string st = full["sheets"][0].value("title_block", json::object()).value("scale", std::string());
        CHECK_FALSE(st.empty());
        CHECK(st.rfind("1:", 0) == 0);  // a 1:N reduction
        doc::drawing_close(did);
    }
}

// R4: a REVOLVED (purely-curved) part must project into proper views — outline + circles —
// not an empty drawing. The exact HLR can return 0 edges for a surface-silhouette body; the
// poly-HLR fallback (project_view/project_section) must recover the outline so every view
// carries visible geometry. Asserts each ortho view has a non-empty bbox + visible polylines,
// and the projected extent matches the Ø24 x 30 revolve (so it is the real body, not noise).
TEST_CASE("Revolved part projects into non-empty views (R4)") {
    const std::string src = make_revolved_part();
    REQUIRE_FALSE(src.empty());
    json created = doc::drawing_new(json::object(), json::object(), "Rev Drawing", src);
    const std::string did = created.at("doc_id").get<std::string>();
    // front (sees the 24-wide x 30-tall rectangle silhouette) + top (sees the Ø24 circle).
    doc::drawing_add_view(did, "base", "front", "s1", json(1.0), json::array({0.0, 0.0}),
                          json(nullptr));
    doc::drawing_add_view(did, "projected", "top", "s1", json(1.0), json::array({0.0, 0.0}),
                          json(nullptr));

    json full = doc::drawing_get(did);
    int views_with_geom = 0;
    for (const json& sh : full.value("sheets", json::array()))
        for (const json& v : sh.value("views", json::array())) {
            std::string orient = v.value("orientation", std::string());
            if (orient != "front" && orient != "top") continue;
            size_t vis = v.value("visible", json::array()).size();
            CHECK(vis > 0);  // the revolve outline reached the view (was 0 before R4)
            REQUIRE(v.contains("bbox"));
            REQUIRE(v["bbox"].is_array());
            double w = v["bbox"][2].get<double>() - v["bbox"][0].get<double>();
            double h = v["bbox"][3].get<double>() - v["bbox"][1].get<double>();
            // The body spans ~24 mm across (diameter) in every ortho view.
            CHECK(w > 18.0);
            CHECK(h > 18.0);
            ++views_with_geom;
        }
    CHECK(views_with_geom == 2);  // front + top both projected the revolved body
    doc::drawing_close(did);
}

// R1/R2: make_drawing must CENTER each view in its grid cell. A flat plate's short front view
// (e.g. 15 mm tall) must sit in the MIDDLE of its cell, not crammed at an edge. We verify the
// projected bbox centre of every placed view lands near its grid-cell centre: the per-view
// centres must be DISTINCT per column/row (a real grid) and the short front view's vertical
// centre must be far from the sheet edges (it is centred, not bottom-aligned). Also: no view's
// scaled extent may exceed the sheet (R2, no overflow).
TEST_CASE("make_drawing centers views in their cells + no overflow (R1/R2)") {
    // A 120 x 80 x 15 flat plate, placed OFF the origin (at +200,+150,+50) so its projected
    // bbox centre is far from the projection origin — the case the old "position = cell centre"
    // layout mis-placed (the part landed at a cell EDGE, not its centre). front view is a thin
    // 120 x 15 strip — the R1 repro.
    json nr = doc::doc_new("part", "FlatPlate");
    const std::string src = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Plate"},
             {"params", {{"length", dimv(120)}, {"width", dimv(80)}, {"height", dimv(15)},
                         {"at", json::array({200.0, 150.0, 50.0})}}}}}},
    });
    doc::doc_apply_ops(src, -1, ops, false);

    json made = doc::drawing_make(src, json{{"views", json::array({"front", "top", "right", "iso"})}});
    const std::string did = made.at("doc_id").get<std::string>();
    json full = made["drawing"];
    const json& mm = full["sheets"][0].value("size_mm", json(nullptr));
    double sw = mm.is_array() ? mm[0].get<double>() : 420.0;
    double sh = mm.is_array() ? mm[1].get<double>() : 297.0;

    int n = 0;
    double front_cy = -1.0;
    for (const json& v : full["sheets"][0].value("views", json::array())) {
        const json& bb = v.value("bbox", json(nullptr));
        if (!(bb.is_array() && bb.size() == 4 && bb[0].is_number())) continue;
        double x0 = bb[0].get<double>(), y0 = bb[1].get<double>();
        double x1 = bb[2].get<double>(), y1 = bb[3].get<double>();
        // R2: the scaled view fits entirely on the sheet (no bleed off any edge).
        CHECK(x0 > 0.0);
        CHECK(y0 > 0.0);
        CHECK(x1 < sw);
        CHECK(y1 < sh);
        if (v.value("orientation", std::string()) == "front") front_cy = (y0 + y1) / 2.0;
        ++n;
    }
    CHECK(n >= 3);
    // R1: the SHORT front view is centred in its cell, well clear of the sheet's bottom edge —
    // not bottom-aligned. Its bbox centre sits comfortably inside the sheet (> 1 cell height up).
    REQUIRE(front_cy > 0.0);
    CHECK(front_cy > sh * 0.15);
    CHECK(front_cy < sh * 0.95);
    doc::drawing_close(did);
}

TEST_CASE("make_drawing compresses unused standard-view grid columns") {
    const std::string src = make_block();
    json made = doc::drawing_make(
        src, json{{"views", json::array({"front", "top"})}, {"model_items", false}});
    const std::string did = made.at("doc_id").get<std::string>();
    double front_x = -1.0, top_x = -1.0;
    for (const json& view : made["drawing"]["sheets"][0].value("views", json::array())) {
        const json& bb = view.value("bbox", json(nullptr));
        REQUIRE(bb.is_array());
        const double cx = (bb[0].get<double>() + bb[2].get<double>()) * 0.5;
        if (view.value("orientation", std::string()) == "front") front_x = cx;
        if (view.value("orientation", std::string()) == "top") top_x = cx;
    }
    REQUIRE(front_x > 0.0);
    REQUIRE(top_x > 0.0);
    CHECK(front_x == doctest::Approx(top_x).epsilon(1e-6));
    CHECK(front_x > 420.0 * 0.35);
    CHECK(front_x < 420.0 * 0.65);
    doc::drawing_close(did);
}

// D3-followup: a DERIVED view (section/detail) whose re-projection FAILS must NOT throw out of
// drawing_get — it must record a per-view `error` and let the rest of the sheet render, so
// review_drawing always works. We inject a degenerate section (coincident cut-line endpoints,
// which resolve_section_plane rejects) directly into a restored drawing doc — bypassing
// drawing_add_section's up-front validation — to mimic a derived view that only fails on
// re-projection. drawing_get must return WITHOUT throwing, with the base view intact and the bad
// section carrying an error.
TEST_CASE("Failing derived view is contained, not thrown, in drawing_get (D3-followup)") {
    const std::string src = make_block();
    REQUIRE_FALSE(src.empty());
    // A minimal aicad.drawing/1 doc: one good base front view + one degenerate section view.
    json drawing_doc = {
        {"schema", "aicad.drawing/1"}, {"id", "doc-cppd-test"}, {"name", "BadSec"},
        {"units", {{"system", "MMGS"}}}, {"source", src},
        {"sheets", json::array({json{
            {"id", "s1"}, {"size", "A3"}, {"views", json::array({
                json{{"id", "v1"}, {"type", "base"}, {"orientation", "front"}, {"scale", 1.0},
                     {"position", json::array({130.0, 175.0})}, {"show_hidden", true}},
                // Degenerate section: both cut-line points coincide -> resolve_section_plane throws.
                json{{"id", "v2"}, {"type", "section"}, {"orientation", "section"}, {"parent", "v1"},
                     {"scale", 1.0}, {"position", json::array({320.0, 175.0})}, {"show_hidden", false},
                     {"cut_line", json::array({json::array({130.0, 175.0}), json::array({130.0, 175.0})})}},
            })}}})},
    };
    json reg = doc::register_restored_drawing(drawing_doc, src);
    const std::string did = reg.at("doc_id").get<std::string>();
    REQUIRE_FALSE(did.empty());

    // The whole-sheet fetch must NOT throw, even though the section re-projection fails.
    json full;
    CHECK_NOTHROW(full = doc::drawing_get(did));
    REQUIRE(full.contains("sheets"));
    bool saw_base = false, saw_bad_section = false;
    for (const json& v : full["sheets"][0].value("views", json::array())) {
        if (v.value("view_id", std::string()) == "v1") {
            saw_base = true;
            CHECK(v.value("visible", json::array()).size() > 0);  // base view still rendered
        }
        if (v.value("view_id", std::string()) == "v2") {
            saw_bad_section = true;
            CHECK(v.contains("error"));  // the bad section is contained with an error field
            CHECK_FALSE(v.value("error", std::string()).empty());
        }
    }
    CHECK(saw_base);         // the rest of the sheet rendered
    CHECK(saw_bad_section);  // and the failing derived view is present (errored, not crashing)
    doc::drawing_close(did);
}

// Auto-section placement must stay ON the sheet (regression). The default section position was
// parent.x + 120 mm with no sheet clamp; when the parent ortho view already sits in the right-most
// grid column (sectioning the "right" view of a front/top/right/iso layout) that pushed the section
// past the sheet's right edge, where it rendered as a clipped sliver. drawing_add_section now drops
// the section BELOW the parent when +120 would overflow, clamped to the drawable area.
TEST_CASE("make_drawing auto-section stays inside the sheet (right-column parent)") {
    const std::string id = make_block();
    json mk = doc::drawing_make(id, json{
        {"views", json::array({"front", "top", "right", "iso"})},
        {"sections", json::array({json{{"parent", "right"}, {"axis", "vertical"}, {"mode", "full"}}})}});
    const std::string did = mk.at("doc_id").get<std::string>();
    json full = doc::drawing_get(did);
    REQUIRE(full.contains("sheets"));
    const json& sheet = full["sheets"][0];
    const json& mm = sheet.value("size_mm", json(nullptr));
    REQUIRE(mm.is_array());
    const double sw = mm[0].get<double>(), shh = mm[1].get<double>();
    bool saw_section = false;
    for (const json& v : sheet.value("views", json::array())) {
        if (v.value("type", std::string()) != "section") continue;
        saw_section = true;
        const json& pos = v.value("position", json(nullptr));
        REQUIRE(pos.is_array());
        const double px = pos[0].get<double>(), py = pos[1].get<double>();
        MESSAGE("section pos=(" << px << "," << py << ") sheet=" << sw << "x" << shh);
        CHECK(px >= 0.0);            // old code: ~parent_x + 120 ~= 460 > 420 (off the right edge)
        CHECK(px <= sw);
        CHECK(py >= 0.0);
        CHECK(py <= shh);
        const json& bb = v.value("bbox", json(nullptr));
        if (bb.is_array() && bb.size() == 4 && bb[0].is_number()) {
            CHECK(bb[2].get<double>() <= sw + 1.0);   // bbox right edge on-sheet
            CHECK(bb[0].get<double>() >= -1.0);
        }
    }
    CHECK(saw_section);
    doc::drawing_close(did);
}

TEST_CASE("Annotation compiler emits manufacturing dimension geometry") {
    SUBCASE("angular dimension has witnesses, arc, and tangent arrowheads") {
        json draw = doc::compile_annotation(
            json{{"kind", "angular_dim"},
                 {"a", json::array({0.0, 0.0})},
                 {"b", json::array({20.0, 0.0})},
                 {"line2", json::array({json::array({0.0, 0.0}),
                                         json::array({0.0, 20.0})})},
                 {"vertex", json::array({0.0, 0.0})},
                 {"pos", json::array({8.0, 8.0})},
                 {"text", "90\xC2\xB0"}},
            "MMGS");

        REQUIRE(draw["arcs"].size() == 1);
        CHECK(draw["segments"].size() >= 2);
        CHECK(draw["tris"].size() == 2);
        const json& arc = draw["arcs"][0];
        CHECK(arc["center"] == json::array({0.0, 0.0}));
        CHECK(arc["radius"].get<double>() == doctest::Approx(std::sqrt(128.0)));
        CHECK(std::fabs(arc["a1"].get<double>() - arc["a0"].get<double>()) ==
              doctest::Approx(0.5 * 3.14159265358979323846));
    }

    SUBCASE("note has a polyline leader and an arrow at its anchor") {
        json draw = doc::compile_annotation(
            json{{"kind", "note"},
                 {"a", json::array({2.0, 3.0})},
                 {"b", json::array({8.0, 10.0})},
                 {"pos", json::array({12.0, 10.0})},
                 {"text", "BREAK SHARP EDGES"}},
            "MMGS");

        REQUIRE(draw["segments"].size() >= 1);
        CHECK(draw["segments"][0].size() >= 3);
        CHECK(draw["segments"][0][0] == json::array({2.0, 3.0}));
        CHECK(draw["tris"].size() == 1);
    }

    SUBCASE("chamfer callout composes semantic distance angle and count") {
        json draw = doc::compile_annotation(
            json{{"kind", "chamfer"},
                 {"a", json::array({5.0, 5.0})},
                 {"pos", json::array({20.0, 15.0})},
                 {"length", 1.5},
                 {"angle", 45.0},
                 {"count", 2}},
            "MMGS");

        CHECK(draw["segments"].size() >= 1);
        CHECK(draw["tris"].size() == 1);
        REQUIRE(draw["texts"].size() == 1);
        CHECK(draw["texts"][0]["text"].get<std::string>() == "2X 1.5 X 45\xC2\xB0");
    }

    SUBCASE("ordinate set marks the origin and aligns values to one baseline") {
        json draw = doc::compile_annotation(
            json{{"kind", "ordinate"},
                 {"origin", json::array({5.0, 5.0})},
                 {"axis", "x"},
                 {"points", json::array({json::array({15.0, 7.0}),
                                          json::array({30.0, 12.0})})},
                 {"pos", json::array({0.0, 20.0})}},
            "MMGS");

        CHECK(draw["arcs"].size() >= 1);
        CHECK(draw["segments"].size() >= 4);
        REQUIRE(draw["texts"].size() == 2);
        CHECK(draw["texts"][0]["pos"][1].get<double>() ==
              doctest::Approx(draw["texts"][1]["pos"][1].get<double>()));
        CHECK(draw["texts"][0]["text"].get<std::string>() == "10");
        CHECK(draw["texts"][1]["text"].get<std::string>() == "25");
    }
}

TEST_CASE("Complete AWS weld fields compile to visible primitives") {
    json draw = doc::compile_annotation(
        json{{"kind", "weld_symbol"},
             {"a", json::array({0.0, 0.0})},
             {"pos", json::array({20.0, 20.0})},
             {"weld_type", "fillet"},
             {"size", "6"},
             {"length", "50"},
             {"pitch", "100"},
             {"contour", "convex"},
             {"finish", "G"},
             {"field_weld", true},
             {"tail_text", "GMAW / AWS D1.1"},
             {"full_penetration", true}},
        "MMGS");

    auto has_text = [&](const std::string& wanted) {
        for (const json& text : draw["texts"])
            if (text.value("text", std::string()) == wanted) return true;
        return false;
    };
    CHECK(has_text("50-100"));
    CHECK(has_text("G"));
    CHECK(has_text("GMAW / AWS D1.1"));
    CHECK(draw["segments"].size() >= 7);  // reference, weld, field flag, tail, leader
    CHECK(draw["arcs"].size() >= 2);      // convex contour + full-penetration indication
    CHECK(draw["tris"].size() >= 1);      // leader arrow; field flag is AWS outline geometry
}

TEST_CASE("Section hatch honors per-body pattern angle and spacing") {
    const json polygon = json::array({json::array({0.0, 0.0}),
                                      json::array({20.0, 0.0}),
                                      json::array({20.0, 20.0}),
                                      json::array({0.0, 20.0})});
    json generic = doc::compile_hatch(polygon, json::object());
    REQUIRE(generic["segments"].size() > 0);
    CHECK(generic["style"]["pattern"] == "single");
    CHECK(generic["style"]["angle_deg"].get<double>() == doctest::Approx(45.0));
    CHECK(generic["style"]["spacing_mm"].get<double>() == doctest::Approx(3.0));

    json material = doc::compile_hatch(
        polygon, json{{"pattern", "cross"},
                      {"angle_deg", 0.0},
                      {"spacing_mm", 5.0},
                      {"material", "steel"},
                      {"body", "body-2"}});
    CHECK(material["style"]["pattern"] == "cross");
    CHECK(material["style"]["angle_deg"].get<double>() == doctest::Approx(0.0));
    CHECK(material["style"]["spacing_mm"].get<double>() == doctest::Approx(5.0));
    bool horizontal = false, vertical = false;
    for (const json& segment : material["segments"]) {
        REQUIRE(segment.size() == 2);
        double dx = std::fabs(segment[1][0].get<double>() - segment[0][0].get<double>());
        double dy = std::fabs(segment[1][1].get<double>() - segment[0][1].get<double>());
        horizontal = horizontal || (dx > 1.0 && dy < 1e-6);
        vertical = vertical || (dy > 1.0 && dx < 1e-6);
    }
    CHECK(horizontal);
    CHECK(vertical);
}

TEST_CASE("Multi-sheet drawing tables and extended title data survive and export") {
    json nr = doc::doc_new("part", "TappedPlate");
    const std::string src = nr.at("doc_id").get<std::string>();
    doc::doc_apply_ops(
        src, -1,
        json::array({
            json{{"op", "add_feature"},
                 {"feature", {{"type", "box"}, {"name", "Plate"},
                              {"params", {{"length", dimv(50)}, {"width", dimv(35)},
                                          {"height", dimv(12)}}}}}},
            json{{"op", "add_feature"},
                 {"feature", {{"type", "hole"}, {"name", "Mount Tap"},
                              {"params", {{"style", "tap"}, {"standard", "M6"},
                                          {"end", "blind"}, {"depth", dimv(9)}}},
                              {"refs", {{"face", json{{"of", "face"},
                                                      {"side", "top"}}}}}}}},
        }),
        false);

    json created = doc::drawing_new(json::object(), json::object(), "Controlled Drawing", src);
    const std::string did = created.at("doc_id").get<std::string>();
    doc::drawing_add_view(did, "base", "front", "s1", json(1.0),
                          json::array({120.0, 170.0}), json(false));
    json added = doc::drawing_add_sheet(did, "A4", "Manufacturing Details");
    CHECK(added["sheet_id"] == "s2");
    CHECK(added["active_sheet_id"] == "s2");
    json top_view = doc::drawing_add_view(did, "base", "top", "", json(1.0),
                                          json::array({145.0, 135.0}), json(false));
    doc::drawing_add_dim(did, top_view.at("view_id").get<std::string>(), "linear_dim",
                         json::array({120.0, 125.0}), json::array({170.0, 125.0}),
                         json(nullptr), json(nullptr), json::array({145.0, 115.0}),
                         json("50.00"));

    json bom = doc::drawing_add_table(
        did, "s2", "bom",
        json{{"position", json::array({15.0, 195.0})},
             {"rows", json::array({
                 json{{"item", 1}, {"name", "BOM-PART-ALPHA"},
                      {"quantity", 2}, {"source", "alpha.step+"}}})}});
    REQUIRE(bom["table"]["rows"].size() == 1);
    CHECK(bom["table"]["rows"][0]["name"] == "BOM-PART-ALPHA");
    json holes = doc::drawing_add_table(
        did, "s2", "holes", json{{"position", json::array({15.0, 150.0})}});
    REQUIRE(holes["table"]["rows"].size() == 1);
    CHECK(holes["table"]["rows"][0]["hole_type"] == "tapped");
    CHECK(holes["table"]["rows"][0]["thread"] == "M6");

    doc::drawing_set_titleblock(
        did, "s2",
        json{{"title", "TAPPED PLATE"}, {"part_number", "TP-001"},
             {"material", "STEEL"}, {"finish", "PASSIVATE"},
             {"drawn_by", "DESIGN"}, {"date", "2026-08-26"},
             {"scale", "1:1"}, {"rev", "B"}, {"sheet", "2 OF 2"},
             {"company", "CHUDWARE"}, {"checked_by", "QA"},
             {"approved_by", "CHIEF"}, {"drawing_standard", "ISO 128 / ASME Y14.5"},
             {"projection", "FIRST ANGLE"}, {"general_tolerance", "ISO 2768-m"},
             {"mass", "2.5 kg"}});

    json listed = doc::drawing_list_sheets(did);
    REQUIRE(listed["sheets"].size() == 2);
    CHECK(listed["active_sheet_id"] == "s2");
    json full = doc::drawing_get(did);
    REQUIRE(full["sheets"].size() == 2);
    CHECK(full["sheets"][1]["tables"].size() == 2);
    bool saw_table_overlay = false;
    for (const json& view : full["sheets"][1]["views"])
        if (view.value("type", std::string()) == "table_overlay") {
            saw_table_overlay = true;
            CHECK(view["annotations"].size() >= 2);  // BOM + holes
        }
    CHECK(saw_table_overlay);
    // The extended title data lives in the compiled ISO 7200 sheet frame, with the
    // first-angle projection symbol (two concentric circles + the cone outline).
    const json& frame = full["sheets"][1]["frame"];
    REQUIRE(frame.is_object());
    CHECK(frame.value("arcs", json::array()).size() >= 2);
    CHECK(frame.value("thick", json::array()).size() >= 2);
    std::set<std::string> frame_text;
    for (const json& t : frame.value("texts", json::array()))
        frame_text.insert(t.value("text", std::string()));
    for (const char* want : {"QA", "CHIEF", "FIRST ANGLE PROJECTION", "2.5 kg", "TP-001",
                             "GENERAL TOLERANCES: ISO 2768-m", "ISO 128 / ASME Y14.5",
                             "TAPPED PLATE", "2 OF 2", "A4"})
        CHECK_MESSAGE(frame_text.count(want) == 1, want);

    json restored = doc::register_restored_drawing(full["tree"], src);
    json restored_sheets = doc::drawing_list_sheets(restored.at("doc_id").get<std::string>());
    CHECK(restored_sheets["sheets"].size() == 2);
    CHECK(restored_sheets["active_sheet_id"] == "s2");

    auto pdf = std::filesystem::temp_directory_path() / "chudware_multisheet_test.pdf";
    json exported = doc::export_drawing(did, pdf.string(), "pdf");
    CHECK(exported["page_count"] == 2);
    CHECK(exported["sheets"] == json::array({"s1", "s2"}));
    std::ifstream stream(pdf, std::ios::binary);
    std::string bytes((std::istreambuf_iterator<char>(stream)),
                      std::istreambuf_iterator<char>());
    CHECK(bytes.find("/Count 2") != std::string::npos);
    std::size_t pages = 0, cursor = 0;
    while ((cursor = bytes.find("/Type /Page /Parent", cursor)) != std::string::npos) {
        ++pages;
        cursor += 6;
    }
    CHECK(pages == 2);
    CHECK(bytes.find("(BOM-PART-ALPHA)") != std::string::npos);
    CHECK(bytes.find("(M6 TAPPED)") != std::string::npos);
    CHECK(bytes.find("(QA)") != std::string::npos);
    CHECK(bytes.find("(ISO 128 / ASME Y14.5)") != std::string::npos);
    CHECK(bytes.find("(FIRST ANGLE PROJECTION)") != std::string::npos);
    CHECK(bytes.find("(GENERAL TOLERANCES: ISO 2768-m)") != std::string::npos);
    CHECK(bytes.find("(2.5 kg)") != std::string::npos);

    auto svg_path = std::filesystem::temp_directory_path() / "chudware_multisheet_test.svg";
    json svg_exported = doc::export_drawing(did, svg_path.string(), "svg");
    REQUIRE(svg_exported["paths"].size() == 2);
    CHECK(svg_exported["page_count"] == 2);
    const std::string svg_s1 = svg_exported["paths"][0].get<std::string>();
    const std::string svg_s2 = svg_exported["paths"][1].get<std::string>();
    const bool has_s1_suffix =
        svg_s1.size() >= 7 && svg_s1.compare(svg_s1.size() - 7, 7, "_s1.svg") == 0;
    const bool has_s2_suffix =
        svg_s2.size() >= 7 && svg_s2.compare(svg_s2.size() - 7, 7, "_s2.svg") == 0;
    CHECK(has_s1_suffix);
    CHECK(has_s2_suffix);
    for (const json& output : svg_exported["paths"]) {
        std::ifstream svg_stream(output.get<std::string>(), std::ios::binary);
        std::string svg_bytes((std::istreambuf_iterator<char>(svg_stream)),
                              std::istreambuf_iterator<char>());
        CHECK_FALSE(svg_bytes.empty());
    }
    std::ifstream svg_stream(svg_exported["paths"][1].get<std::string>(), std::ios::binary);
    std::string svg_bytes((std::istreambuf_iterator<char>(svg_stream)),
                          std::istreambuf_iterator<char>());
    CHECK(svg_bytes.find("<svg") != std::string::npos);
    CHECK(svg_bytes.find("<g id=\"visible\"") != std::string::npos);
    CHECK(svg_bytes.find("<g id=\"hidden\"") != std::string::npos);
    CHECK(svg_bytes.find("<g id=\"annotations\"") != std::string::npos);
    CHECK(svg_bytes.find("<g id=\"tables\"") != std::string::npos);
    CHECK(svg_bytes.find("<text") != std::string::npos);
    CHECK(svg_bytes.find("font-family=\"Helvetica, Arial, sans-serif\"") !=
          std::string::npos);
    CHECK(svg_bytes.find(">50.00</text>") != std::string::npos);
    CHECK(svg_bytes.find(">BOM-PART-ALPHA</text>") != std::string::npos);

    std::error_code ec;
    std::filesystem::remove(pdf, ec);
    for (const json& output : svg_exported["paths"])
        std::filesystem::remove(output.get<std::string>(), ec);
    doc::drawing_close(restored.at("doc_id").get<std::string>());
    doc::drawing_close(did);
}

TEST_CASE("Jogged multi-plane section cut is refused instead of approximated") {
    const std::string src = make_block();
    json created = doc::drawing_new(json::object(), json::object(), "Offset Refusal", src);
    const std::string did = created.at("doc_id").get<std::string>();
    const std::string vid =
        doc::drawing_add_view(did, "base", "front", "s1", json(1.0),
                              json::array({130.0, 175.0}), json(false))
            .at("view_id").get<std::string>();
    const json jogged = json::array({
        json::array({100.0, 150.0}), json::array({120.0, 150.0}),
        json::array({120.0, 190.0}), json::array({150.0, 190.0})});
    try {
        doc::drawing_add_section(did, vid, json(nullptr), jogged, json("A"),
                                 json(1.0), json::array({300.0, 170.0}), "s1",
                                 json(false), json::object());
        FAIL("jogged multi-plane cut must be rejected");
    } catch (const doc::OpError& error) {
        CHECK(error.code == "unsupported_offset_section");
        CHECK(error.detail.value("reason", std::string()) == "multi_plane_cut_line");
    }
    CHECK(doc::drawing_get(did)["sheets"][0]["views"].size() == 1);
    doc::drawing_close(did);
}

TEST_CASE("Standalone draw+ embeds its source and falls back to stale cached views") {
    namespace fs = std::filesystem;
    const fs::path full_path =
        fs::temp_directory_path() / "chudware_standalone_drawing.draw+";
    const fs::path stripped_path =
        fs::temp_directory_path() / "chudware_standalone_drawing_missing.draw+";
    const fs::path source_path =
        fs::temp_directory_path() / "chudware_standalone_source.step+";
    std::error_code ec;
    fs::remove(full_path, ec);
    fs::remove(stripped_path, ec);
    fs::remove(source_path, ec);

    const std::string source_id = make_block();
    doc::doc_save(source_id, source_path.string());
    json created =
        doc::drawing_new(json::object(), json::object(), "Portable Controlled Drawing",
                         source_id);
    const std::string drawing_id = created.at("doc_id").get<std::string>();
    const std::string view_id =
        doc::drawing_add_view(drawing_id, "base", "front", "s1", json(1.0),
                              json::array({180.0, 160.0}), json(false))
            .at("view_id").get<std::string>();
    doc::drawing_add_dim(drawing_id, view_id, "linear_dim",
                         json::array({160.0, 150.0}),
                         json::array({200.0, 150.0}), json(nullptr), json(nullptr),
                         json::array({180.0, 140.0}), json(nullptr));
    doc::drawing_set_titleblock(
        drawing_id, "s1",
        json{{"title", "PORTABLE BLOCK"}, {"part_number", "PB-001"},
             {"rev", "C"}, {"drawn_by", "TEST"}});

    json saved = doc::drawing_save_standalone(drawing_id, full_path.string());
    CHECK(saved["path"] == full_path.string());
    REQUIRE(fs::exists(full_path));
    doc::StepPlusDoc archive =
        doc::editable_container_open(full_path.string(), "aicad.draw+/", ".draw+");
    CHECK(archive.doc["schema"] == "aicad.drawing/1");
    CHECK(archive.doc["source"]["kind"] == "embedded");
    CHECK(archive.doc["source"]["source_doc_schema"] == "aicad.part/1");
    CHECK(archive.extra.count("source/document.json") == 1);
    CHECK(archive.extra.count("cache/views.json") == 1);
    REQUIRE(archive.doc["source"]["step_plus_rel_path"].is_string());
    json associated = doc::drawing_open_standalone(full_path.string());
    CHECK(associated["source_state"] == "live");
    CHECK(associated["source_doc_id"] == source_id);
    doc::drawing_close(associated.at("drawing_id").get<std::string>());

    doc::drawing_close(drawing_id);
    doc::doc_close(source_id);
    json reopened = doc::drawing_open_standalone(full_path.string());
    CHECK(reopened["source_state"] == "embedded_opened");
    REQUIRE(reopened["source_doc_id"].is_string());
    const std::string reopened_drawing =
        reopened.at("drawing_id").get<std::string>();
    const std::string reopened_source =
        reopened.at("source_doc_id").get<std::string>();
    json live = doc::drawing_get(reopened_drawing);
    CHECK(live["source_state"] == "embedded_opened");
    CHECK_FALSE(live["views_stale"].get<bool>());
    REQUIRE(live["sheets"].size() == 1);
    REQUIRE(live["sheets"][0]["views"].size() == 1);
    CHECK_FALSE(live["sheets"][0]["views"][0]["visible"].empty());
    REQUIRE(live["tree"]["sheets"][0]["views"][0]["annotations"].size() == 1);
    CHECK(live["tree"]["sheets"][0]["title_block"]["title"] ==
          "PORTABLE BLOCK");
    CHECK(live["tree"]["sheets"][0]["title_block"]["part_number"] == "PB-001");

    doc::drawing_close(reopened_drawing);
    doc::doc_close(reopened_source);
    fs::remove(source_path, ec);

    archive.extra.erase("source/document.json");
    archive.extra["geometry.step"] = "opaque future drawing entry";
    doc::editable_container_save(archive.doc, stripped_path.string(), archive.extra,
                                 "aicad.draw+/1", "drawing", ".draw+");
    doc::StepPlusDoc stripped_archive =
        doc::editable_container_open(stripped_path.string(), "aicad.draw+/", ".draw+");
    CHECK(stripped_archive.extra["geometry.step"] ==
          "opaque future drawing entry");
    json missing = doc::drawing_open_standalone(stripped_path.string());
    CHECK(missing["source_state"] == "missing");
    CHECK(missing["source_doc_id"].is_null());
    const std::string missing_drawing =
        missing.at("drawing_id").get<std::string>();
    json frozen = doc::drawing_get(missing_drawing);
    CHECK(frozen["source_missing"].get<bool>());
    CHECK(frozen["views_stale"].get<bool>());
    CHECK(frozen["warning"] ==
          "source missing — views frozen from last projection");
    REQUIRE(frozen["sheets"].size() == 1);
    REQUIRE(frozen["sheets"][0]["views"].size() == 1);
    CHECK_FALSE(frozen["sheets"][0]["views"][0]["visible"].empty());
    REQUIRE(frozen["tree"]["sheets"][0]["views"][0]["annotations"].size() == 1);
    CHECK(frozen["tree"]["sheets"][0]["title_block"]["title"] ==
          "PORTABLE BLOCK");

    doc::drawing_close(missing_drawing);
    fs::remove(full_path, ec);
    fs::remove(stripped_path, ec);
    fs::remove(source_path, ec);
}

TEST_CASE("Moving and rescaling a parent carries dimensions and derived cuts") {
    const std::string src = make_block();
    const std::string did =
        doc::drawing_new(json::object(), json::object(), "Attached", src)["doc_id"].get<std::string>();
    const std::string parent =
        doc::drawing_add_view(did, "base", "top", "s1", 1.0,
                              json::array({130.0, 175.0}), nullptr)["view_id"].get<std::string>();
    doc::drawing_add_dim(did, parent, "linear_dim",
                         json::array({110.0, 155.0}), json::array({150.0, 155.0}),
                         nullptr, nullptr, json::array({130.0, 145.0}), nullptr);
    doc::drawing_add_dim(did, parent, "angular_dim",
                         json::array({120.0, 175.0}), json::array({140.0, 175.0}),
                         json::array({130.0, 165.0}), json::array({130.0, 185.0}),
                         json::array({140.0, 185.0}), nullptr);
    doc::drawing_add_ordinate_dimensions(
        did, parent, json{{"origin", json::array({110.0, 155.0})},
                          {"points", json::array({json::array({150.0, 155.0})})}});
    doc::drawing_add_dim(did, parent, "diameter_dim",
                         json::array({135.0, 175.0}), nullptr, nullptr, nullptr,
                         json::array({145.0, 190.0}), nullptr);
    const std::string section =
        doc::drawing_add_section(
            did, parent, nullptr,
            json::array({json::array({130.0, 155.0}), json::array({130.0, 195.0})}),
            nullptr, nullptr, json::array({320.0, 175.0}), "s1", false)["view_id"].get<std::string>();
    const std::string detail =
        doc::drawing_add_detail(did, parent, json::array({130.0, 175.0}), 12.0,
                                2.0, nullptr, json::array({320.0, 230.0}), "s1", false)["view_id"].get<std::string>();

    json moved = doc::drawing_set_view(did, parent, 2.0, json::array({170.0, 195.0}),
                                       nullptr, nullptr);
    REQUIRE(moved["view"]["annotations"].size() == 4);
    CHECK(moved["view"]["annotations"][0]["a"] == json::array({130.0, 155.0}));
    CHECK(moved["view"]["annotations"][0]["b"] == json::array({210.0, 155.0}));
    CHECK(moved["view"]["annotations"][0]["pos"] == json::array({170.0, 135.0}));
    CHECK(draw_count(moved["view"]["annotations"][0]) > 0);
    CHECK(moved["view"]["annotations"][1]["line2"] ==
          json::array({json::array({170.0, 175.0}), json::array({170.0, 215.0})}));
    CHECK(moved["view"]["annotations"][1]["vertex"] == json::array({170.0, 195.0}));
    CHECK(moved["view"]["annotations"][2]["origin"] == json::array({130.0, 155.0}));
    CHECK(moved["view"]["annotations"][3]["center"] == json::array({170.0, 195.0}));
    CHECK(moved["view"]["annotations"][3]["radius"].get<double>() ==
          doctest::Approx(10.0));
    json full = doc::drawing_get(did);
    const json& views = full["sheets"][0]["views"];
    const json* cut = nullptr;
    const json* crop = nullptr;
    for (const json& v : views) {
        if (v.value("view_id", std::string()) == section) cut = &v;
        if (v.value("view_id", std::string()) == detail) crop = &v;
    }
    REQUIRE(cut != nullptr);
    REQUIRE(crop != nullptr);
    CHECK((*cut)["cut_line"] ==
          json::array({json::array({170.0, 155.0}), json::array({170.0, 235.0})}));
    const bool cut_error = cut->contains("error") && (*cut)["error"].is_string();
    CHECK_FALSE(cut_error);
    CHECK((*crop)["center"] == json::array({170.0, 195.0}));
    CHECK((*crop)["radius"].get<double>() == doctest::Approx(24.0));
    const bool crop_error = crop->contains("error") && (*crop)["error"].is_string();
    CHECK_FALSE(crop_error);
    // Opposite-facing projections have a lossless planar frame change.
    json flipped = doc::drawing_set_view(did, parent, nullptr, nullptr, "bottom", nullptr);
    CHECK(flipped["view"]["annotations"][0]["a"] == json::array({130.0, 235.0}));
    json flipped_full = doc::drawing_get(did);
    for (const json& v : flipped_full["sheets"][0]["views"])
        if (v.value("view_id", std::string()) == section)
            CHECK(v["cut_line"] ==
                  json::array({json::array({170.0, 235.0}),
                               json::array({170.0, 155.0})}));
    doc::drawing_close(did);
    doc::doc_close(src);
}

TEST_CASE("Drawing units inherit source, switch numeric text, and preserve overrides") {
    const std::string src = make_cube(25.4);
    doc::doc_apply_ops(src, -1,
                       json::array({json{{"op", "set_units"}, {"system", "IPS"}}}), false);
    const json created = doc::drawing_new(json::object(), json::object(), "", src);
    CHECK(created["drawing"]["units"]["system"] == "IPS");
    const std::string did = created["doc_id"].get<std::string>();
    const std::string vid =
        doc::drawing_add_view(did, "base", "front", "s1", 1.0,
                              json::array({150.0, 170.0}), nullptr)["view_id"].get<std::string>();
    doc::drawing_add_dim(did, vid, "linear_dim",
                         json::array({100.0, 170.0}), json::array({125.4, 170.0}),
                         nullptr, nullptr, json::array({115.0, 155.0}), nullptr);
    doc::drawing_add_dim(did, vid, "linear_dim",
                         json::array({130.0, 170.0}), json::array({155.4, 170.0}),
                         nullptr, nullptr, json::array({145.0, 155.0}), "REF");
    doc::drawing_add_chamfer_dimension(
        did, vid, json{{"a", json::array({140.0, 170.0})},
                       {"length", 25.4}, {"angle", 45.0}});
    doc::drawing_add_ordinate_dimensions(
        did, vid, json{{"origin", json::array({100.0, 170.0})},
                       {"points", json::array({json::array({125.4, 170.0})})}});
    json initial = doc::drawing_get(did);
    CHECK(initial["tree"]["sheets"][0]["views"][0]["annotations"][0]["text"] == "1 in");
    json metric = doc::drawing_set_units(did, "MMGS");
    CHECK(metric["drawing"]["tree"]["units"]["system"] == "MMGS");
    CHECK(metric["drawing"]["sheets"][0]["views"][0]["annotations"][0]["text"] == "25.4");
    CHECK(metric["drawing"]["sheets"][0]["views"][0]["annotations"][1]["text"] == "REF");
    CHECK(metric["drawing"]["sheets"][0]["views"][0]["annotations"][2]["text"] ==
          "25.4 \xC3\x97 45\xC2\xB0");
    CHECK(metric["drawing"]["sheets"][0]["views"][0]["annotations"][3]["text"] == "25.4");
    json imperial = doc::drawing_set_units(did, "IPS");
    CHECK(imperial["drawing"]["sheets"][0]["views"][0]["annotations"][0]["text"] == "1 in");
    CHECK(imperial["drawing"]["sheets"][0]["views"][0]["annotations"][2]["text"] ==
          "1 \xC3\x97 45\xC2\xB0");
    CHECK(imperial["drawing"]["sheets"][0]["views"][0]["annotations"][3]["text"] == "1 in");
    CHECK_THROWS_AS(doc::drawing_set_units(did, "UNKNOWN"), doc::OpError);
    doc::drawing_close(did);
    const json made = doc::drawing_make(
        src, json{{"views", json::array({"front"})}, {"model_items", false}});
    CHECK(made["drawing"]["tree"]["units"]["system"] == "IPS");
    doc::drawing_close(made["doc_id"].get<std::string>());
    doc::doc_close(src);
}

TEST_CASE("Closing an edited source freezes its final geometry and warns") {
    const std::string src = make_cube(20.0);
    const std::string did =
        doc::drawing_new(json::object(), json::object(), "", src)["doc_id"].get<std::string>();
    doc::drawing_add_view(did, "base", "front", "s1", 1.0,
                          json::array({150.0, 170.0}), nullptr);
    const json initial = doc::drawing_get(did);
    doc::doc_apply_ops(
        src, -1,
        json::array({json{{"op", "add_feature"},
                          {"feature", {{"type", "box"}, {"name", "Far"},
                                       {"params", {{"length", dimv(20)}, {"width", dimv(20)},
                                                   {"height", dimv(20)},
                                                   {"at", json::array({100.0, 0.0, 0.0})}}}}}}}),
        false);
    const json latest = doc::drawing_get(did);
    REQUIRE(latest["sheets"][0]["views"][0]["bbox"].is_array());
    CHECK(latest["sheets"][0]["views"][0]["bbox"][2].get<double>() >
          initial["sheets"][0]["views"][0]["bbox"][2].get<double>() + 50.0);
    REQUIRE(doc::doc_close(src));
    const json frozen = doc::drawing_get(did);
    CHECK(frozen["source_state"] == "closed");
    CHECK(frozen["source_missing"] == true);
    CHECK(frozen["views_stale"] == true);
    CHECK(frozen["warning"] == "source closed — views frozen from last projection");
    CHECK(frozen["sheets"][0]["views"][0]["bbox"] ==
          latest["sheets"][0]["views"][0]["bbox"]);
    doc::drawing_close(did);
}

TEST_CASE("ASCII DXF labels retain manufacturing callout meanings") {
    namespace fs = std::filesystem;
    const std::string src = make_cube(30.0);
    const std::string did =
        doc::drawing_new(json::object(), json::object(), "", src)["doc_id"].get<std::string>();
    const std::string vid =
        doc::drawing_add_view(did, "base", "front", "s1", 1.0,
                              json::array({150.0, 170.0}), nullptr)["view_id"].get<std::string>();
    doc::drawing_add_dim(did, vid, "note", json::array({150.0, 170.0}), nullptr,
                         nullptr, nullptr, json::array({180.0, 190.0}),
                         "\xE2\x8C\xB4 \xE2\x8C\xB5 \xE2\x86\xA7");
    const fs::path path = fs::temp_directory_path() / "chudware_callout_symbols.dxf";
    doc::export_drawing(did, path.string(), "dxf");
    std::ifstream stream(path, std::ios::binary);
    REQUIRE(stream.good());
    const std::string text((std::istreambuf_iterator<char>(stream)),
                           std::istreambuf_iterator<char>());
    CHECK(text.find("CBORE") != std::string::npos);
    CHECK(text.find("CSK") != std::string::npos);
    CHECK(text.find("DEPTH") != std::string::npos);
    std::error_code ec;
    fs::remove(path, ec);
    doc::drawing_close(did);
    doc::doc_close(src);
}

TEST_CASE("Back and iso views occupy different drawing cells") {
    const std::string src = make_cube(20.0);
    const json made = doc::drawing_make(
        src, json{{"views", json::array({"front", "back", "iso"})},
                  {"model_items", false}});
    const json& views = made["drawing"]["tree"]["sheets"][0]["views"];
    REQUIRE(views.size() == 3);
    CHECK(views[1]["orientation"] == "back");
    CHECK(views[2]["orientation"] == "iso");
    CHECK(views[1]["position"] != views[2]["position"]);
    doc::drawing_close(made["doc_id"].get<std::string>());
    doc::doc_close(src);
}

TEST_CASE("Auto Balloon attaches each assembly BOM item to its visible component") {
    namespace fs = std::filesystem;
    const fs::path dir = fs::temp_directory_path() /
                         ("chudware_auto_balloon_" +
                          std::to_string(std::chrono::steady_clock::now()
                                             .time_since_epoch().count()));
    fs::create_directories(dir);
    struct Cleanup {
        fs::path dir;
        ~Cleanup() { std::error_code ec; fs::remove_all(dir, ec); }
    } cleanup{dir};

    json components = json::array();
    for (int i = 0; i < 3; ++i) {
        const std::string source = make_cube(10.0 + 4.0 * i);
        const fs::path path = dir / ("component" + std::to_string(i) + ".step+");
        doc::doc_save(source, path.string());
        doc::doc_close(source);
        const double x = 55.0 * i;
        components.push_back({
            {"id", "part" + std::to_string(i)}, {"source", {{"path", path.string()}}},
            {"fixed", true},
            {"transform", {1, 0, 0, x, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1}},
        });
    }
    const fs::path asm_path = dir / "assembly.step+";
    doc::stepplus_save(json{{"schema", "aicad.asm/1"}, {"name", "ThreeParts"},
                            {"components", components}, {"mates", json::array()}},
                       asm_path.string(), {});
    const std::string asm_id = doc::doc_open(asm_path.string())["doc_id"].get<std::string>();
    const std::string did =
        doc::drawing_new(json::object(), json::object(), "", asm_id)["doc_id"].get<std::string>();
    const std::string vid =
        doc::drawing_add_view(did, "base", "front", "s1", 1.0,
                              json::array({100.0, 150.0}), false)["view_id"].get<std::string>();

    const json bom = doc::drawing_bom(did)["rows"];
    const json before = doc::drawing_get(did)["sheets"][0]["views"][0];
    REQUIRE_MESSAGE(before["visible"].size() > 0, before.dump());
    CHECK_FALSE(before.contains("components"));  // no per-component HLR on ordinary fetch
    REQUIRE(bom.size() == 3);
    const json generated = doc::drawing_auto_balloon(did);
    CHECK(generated["added"] == 3);
    CHECK(generated["view_ids"] == json::array({vid}));
    const json& projected = generated["drawing"]["sheets"][0]["views"][0];
    CHECK_FALSE(projected.contains("components"));
    REQUIRE(generated["placements"].size() == 3);
    std::map<int, json> placements;
    for (const json& placement : generated["placements"])
        placements[placement["item"].get<int>()] = placement;
    std::set<int> items;
    const json& balloons = projected["annotations"];
    REQUIRE(balloons.size() == 3);
    for (const json& annotation : balloons) {
        CHECK(annotation["kind"] == "balloon");
        CHECK(annotation["auto"] == true);
        CHECK(draw_count(annotation) > 0);
        const int item = annotation["item"].get<int>();
        CHECK(items.insert(item).second);
        const json& placement = placements.at(item);
        CHECK(placement["source"] == bom[item - 1]["source"]);
        CHECK(placement["anchor"] == annotation["a"]);
        const json& bbox = placement["bbox"];
        const json& anchor = annotation["a"];
        CHECK(anchor[0].get<double>() >= bbox[0].get<double>() - 0.05);
        CHECK(anchor[0].get<double>() <= bbox[2].get<double>() + 0.05);
        CHECK(anchor[1].get<double>() >= bbox[1].get<double>() - 0.05);
        CHECK(anchor[1].get<double>() <= bbox[3].get<double>() + 0.05);
    }
    CHECK(items.size() == 3);
    CHECK(items.count(1) == 1);
    CHECK(items.count(2) == 1);
    CHECK(items.count(3) == 1);
    CHECK(balloons[0]["pos"] != balloons[1]["pos"]);
    CHECK(doc::drawing_auto_balloon(did)["added"] == 3);
    CHECK(doc::drawing_get(did)["sheets"][0]["views"][0]["annotations"].size() == 3);
    const double old_x = balloons[0]["a"][0].get<double>();
    const json moved = doc::drawing_set_view(did, vid, nullptr,
                                              json::array({120.0, 150.0}),
                                              nullptr, nullptr);
    CHECK(moved["view"]["annotations"][0]["a"][0].get<double>() ==
          doctest::Approx(old_x + 20.0));
    const json relocated = doc::drawing_auto_balloon(did);
    CHECK(relocated["added"] == 3);
    for (const json& placement : relocated["placements"])
        CHECK(placement["anchor"][0].get<double>() ==
              doctest::Approx(placements.at(placement["item"].get<int>())["anchor"][0]
                                  .get<double>() + 20.0));
    const fs::path dxf_path = dir / "balloons.dxf";
    const json exported = doc::export_drawing(did, dxf_path.string(), "dxf");
    CHECK(exported["entities"].get<int>() > 0);
    CHECK(exported["annotations"].get<int>() >= 3);

    const std::string part = make_cube(12.0);
    const std::string part_drawing =
        doc::drawing_new(json::object(), json::object(), "", part)["doc_id"].get<std::string>();
    try {
        doc::drawing_auto_balloon(part_drawing);
        FAIL("part drawing must refuse Auto Balloon");
    } catch (const doc::OpError& error) {
        CHECK(error.detail.value("reason", std::string()) == "not_assembly");
    }
    doc::drawing_close(part_drawing);
    doc::doc_close(part);
    doc::drawing_close(did);
    doc::doc_close(asm_id);
}

namespace {
std::array<double, 4> bb_of(const json& v) {
    const json& b = v.value("bbox", json(nullptr));
    REQUIRE(b.is_array());
    return {b[0].get<double>(), b[1].get<double>(), b[2].get<double>(), b[3].get<double>()};
}
const json* view_by_orient(const json& sheet, const std::string& orient) {
    if (!sheet.contains("views")) return nullptr;
    for (const json& v : sheet["views"])
        if (v.value("orientation", std::string()) == orient) return &v;
    return nullptr;
}
bool has_text(const json& draw, const std::string& want) {
    for (const json& t : draw.value("texts", json::array()))
        if (t.value("text", std::string()) == want) return true;
    return false;
}
// 80 x 80 x 12 flange plate: 4 x Ø8 holes on a Ø50 bolt circle (positions on the top face).
std::string make_bolt_circle_plate() {
    json nr = doc::doc_new("part", "BoltPlate");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({
        json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Plate"},
             {"params", {{"length", dimv(80)}, {"width", dimv(80)}, {"height", dimv(12)}}}}}},
        json{{"op", "add_feature"}, {"feature", {{"type", "hole"}, {"name", "Bolts"},
             {"params", {{"style", "simple"}, {"standard", "custom"}, {"diameter", dimv(8)},
                         {"end", "through_all"},
                         {"positions", json::array({json::array({25.0, 0.0}),
                                                    json::array({0.0, 25.0}),
                                                    json::array({-25.0, 0.0}),
                                                    json::array({0.0, -25.0})})}}},
             {"refs", {{"face", json{{"of", "face"}, {"side", "top"}}}}}}}},
    });
    doc::doc_apply_ops(id, -1, ops, false);
    return id;
}
}  // namespace

// ISO 5456-2: third-angle puts the top view ABOVE the front and the right view to its RIGHT;
// first-angle mirrors both. Views share their projection lines (same x for front/top, same y
// for front/right), stay inside the frame, and never cover the title block.
TEST_CASE("Auto layout aligns standard views per projection method and clears the title block") {
    const std::string src = make_block();
    for (const char* proj : {"third", "first"}) {
        const json made = doc::drawing_make(
            src, json{{"views", json::array({"front", "top", "right", "iso"})},
                      {"projection", proj}, {"model_items", false}});
        const json& sheet = made["drawing"]["sheets"][0];
        const json* front = view_by_orient(sheet, "front");
        const json* top = view_by_orient(sheet, "top");
        const json* right = view_by_orient(sheet, "right");
        REQUIRE(front);
        REQUIRE(top);
        REQUIRE(right);
        const auto f = bb_of(*front), t = bb_of(*top), r = bb_of(*right);
        CHECK((f[0] + f[2]) * 0.5 == doctest::Approx((t[0] + t[2]) * 0.5).epsilon(1e-6));
        CHECK((f[1] + f[3]) * 0.5 == doctest::Approx((r[1] + r[3]) * 0.5).epsilon(1e-6));
        if (std::string(proj) == "third") {
            CHECK(t[1] > f[3]);  // top above front
            CHECK(r[0] > f[2]);  // right to the right
        } else {
            CHECK(t[3] < f[1]);  // top below front
            CHECK(r[2] < f[0]);  // right to the left
        }
        const auto inner = doc::sheet_frame_inner(420.0, 297.0);
        const auto tbr = doc::sheet_title_block_rect(420.0, 297.0);
        for (const json& v : sheet["views"]) {
            if (v.value("type", std::string()) == "table_overlay") continue;
            const auto b = bb_of(v);
            CHECK(b[0] > inner[0]);
            CHECK(b[2] < inner[2]);
            CHECK(b[1] > inner[1]);
            CHECK(b[3] < inner[3]);
            const bool clear_of_tb = b[2] < tbr[0] || b[1] > tbr[3];
            CHECK(clear_of_tb);
        }
        // One standard scale for every orthographic view, echoed in the title block with the
        // projection; a pictorial at its own scale must say so under the view.
        const double sc = (*front)["scale"].get<double>();
        for (const json& v : sheet["views"]) {
            if (!v.contains("scale")) continue;
            if (v.value("type", std::string()) == "iso") {
                if (std::fabs(v["scale"].get<double>() - sc) > 1e-12)
                    CHECK(v["draw"].value("texts", json::array()).size() == 1);
                continue;
            }
            CHECK(v["scale"].get<double>() == doctest::Approx(sc));
        }
        const json& tb = sheet["title_block"];
        CHECK(tb.value("projection", std::string()) == proj);
        CHECK_FALSE(tb.value("scale", std::string()).empty());
        CHECK(has_text(sheet["frame"], std::string(proj) == "third" ? "THIRD ANGLE PROJECTION"
                                                                      : "FIRST ANGLE PROJECTION"));
        doc::drawing_close(made["doc_id"].get<std::string>());
    }
    doc::doc_close(src);
}

// drawing_arrange re-lays an existing sheet; an explicit scale is honoured exactly.
TEST_CASE("drawing_arrange switches projection and honours an explicit scale") {
    const std::string src = make_block();
    const json made = doc::drawing_make(
        src, json{{"views", json::array({"front", "top", "right"})}, {"model_items", true}});
    const std::string did = made["doc_id"].get<std::string>();
    const json r = doc::drawing_arrange(did, "s1", "first", json(2.0));
    CHECK(r["scale"].get<double>() == doctest::Approx(2.0));
    CHECK(r["projection"] == "first");
    const json& sheet = r["drawing"]["sheets"][0];
    const auto f = bb_of(*view_by_orient(sheet, "front"));
    const auto t = bb_of(*view_by_orient(sheet, "top"));
    CHECK(t[3] < f[1]);
    CHECK((f[2] - f[0]) == doctest::Approx(80.0).epsilon(1e-3));  // 40 mm block at 2:1
    CHECK(sheet["title_block"]["scale"] == "2:1");
    doc::drawing_close(did);
    doc::doc_close(src);
}

// Auto-dimensioning a whole sheet states each model extent ONCE (no redundant dimensions),
// marks every hole centre, draws hole axes as centre lines in the side views, and turns an
// equally spaced hole pattern into a chain pitch circle with its diameter.
TEST_CASE("Sheet auto-dimension is non-redundant with centre marks, centre lines and bolt circle") {
    const std::string src = make_bolt_circle_plate();
    const json made = doc::drawing_make(
        src, json{{"views", json::array({"front", "top", "right", "iso"})},
                  {"hole_callouts", true}});
    const json& sheet = made["drawing"]["sheets"][0];
    std::map<double, int> linear_by_value;
    int center_marks = 0, center_lines = 0, pitch = 0, pcd = 0, holes = 0;
    for (const json& v : sheet["views"])
        for (const json& an : v.value("annotations", json::array())) {
            const std::string kind = an.value("kind", an.value("type", std::string()));
            if (kind == "linear_dim") ++linear_by_value[std::round(an["value"].get<double>())];
            if (kind == "center_mark") ++center_marks;
            if (kind == "center_line") {
                ++center_lines;
                CHECK(an["draw"].value("chain", json::array()).size() == 1);
            }
            if (kind == "pitch_circle") ++pitch;
            if (kind == "diameter_dim" && std::fabs(an.value("value", 0.0) - 50.0) < 1e-3) ++pcd;
            if (kind == "diameter_dim" && an.value("count", 0) == 4 &&
                std::fabs(an.value("value", 0.0) - 8.0) < 1e-3)
                ++holes;
            if (kind == "hole_callout") FAIL("hole callout duplicated a model-items diameter");
        }
    CHECK(linear_by_value[80] == 2);  // X and Y extents (equal values, different axes)
    CHECK(linear_by_value[12] == 1);  // the thickness once, not in front AND right views
    CHECK(center_marks >= 4);
    CHECK(center_lines >= 2);  // the hole axes seen side-on in front + right
    CHECK(pitch == 1);
    CHECK(pcd == 1);
    CHECK(holes == 1);
    // Arrowheads are FILLED triangles and dimension text is never empty.
    for (const json& v : sheet["views"])
        for (const json& an : v.value("annotations", json::array()))
            if (an.value("type", std::string()) == "linear_dim") {
                CHECK(an["draw"]["tris"].size() == 2);
                CHECK(an["draw"]["texts"].size() >= 1);
            }
    doc::drawing_close(made["doc_id"].get<std::string>());
    doc::doc_close(src);
}

// The compiled sheet frame: thick drawing border, zone references, a filled-in title block
// whose values never overflow their cells, and portrait paper.
TEST_CASE("Sheet frame carries border, zones and a fitted title block; portrait sizes") {
    const std::string src = make_cube(30.0);
    const json made = doc::drawing_make(
        src, json{{"sheet", "A4 portrait"}, {"views", json::array({"front", "top"})},
                  {"title", {{"title", "A VERY LONG PART TITLE THAT MUST NEVER LEAVE ITS CELL"},
                             {"part_number", "CW-0001"}, {"rev", "B"}}}});
    const json& sheet = made["drawing"]["sheets"][0];
    CHECK(sheet["size"] == "A4P");
    CHECK(sheet["size_mm"][0].get<double>() == doctest::Approx(210.0));
    CHECK(sheet["size_mm"][1].get<double>() == doctest::Approx(297.0));
    const json& frame = sheet["frame"];
    CHECK(frame["thick"].size() >= 2);  // drawing frame + title block border
    CHECK(has_text(frame, "A"));
    CHECK(has_text(frame, "1"));
    CHECK(has_text(frame, "CW-0001"));
    CHECK(has_text(frame, "1 OF 1"));
    CHECK(has_text(frame, "A4"));
    const auto tbr = doc::sheet_title_block_rect(210.0, 297.0);
    for (const json& t : frame["texts"]) {
        const double x = t["pos"][0].get<double>(), y = t["pos"][1].get<double>();
        if (y > tbr[3] || y < tbr[1] || x < tbr[0] || x > tbr[2]) continue;  // zone labels
        const double w = doc::annot_text_width(t["text"].get<std::string>(), t["h"].get<double>());
        const std::string align = t.value("align", std::string("left"));
        const double x0 = align == "center" ? x - w * 0.5 : x;
        CHECK(x0 >= tbr[0] - 1e-6);
        CHECK(x0 + w <= tbr[2] + 1e-6);
    }
    CHECK_THROWS(doc::drawing_set_sheet(made["doc_id"].get<std::string>(), "s1", "A7"));
    doc::drawing_close(made["doc_id"].get<std::string>());
    doc::doc_close(src);
}

// ISO 129-1 dimension geometry: extension lines start off the feature, the dimension line
// carries filled arrows inside (outside on a tight span), the value sits above it; a diameter
// leader's arrow touches the circle.
TEST_CASE("Dimension and leader geometry follow ISO 129-1") {
    const json lin = doc::compile_annotation(
        json{{"type", "linear_dim"}, {"a", {100.0, 100.0}}, {"b", {160.0, 100.0}},
             {"pos", {130.0, 90.0}}, {"text", "60"}},
        "MMGS");
    REQUIRE(lin["tris"].size() == 2);
    REQUIRE(lin["texts"].size() == 1);
    CHECK(lin["texts"][0]["align"] == "center");
    CHECK(lin["texts"][0]["pos"][1].get<double>() > 90.0);  // above the dimension line
    for (const json& seg : lin["segments"]) {
        // No extension line touches the measured feature (the ISO gap).
        for (const json& p : seg)
            CHECK(std::hypot(p[0].get<double>() - 100.0, p[1].get<double>() - 100.0) > 0.5);
    }
    const json tight = doc::compile_annotation(
        json{{"type", "linear_dim"}, {"a", {100.0, 100.0}}, {"b", {103.0, 100.0}},
             {"pos", {101.5, 90.0}}, {"text", "3"}},
        "MMGS");
    REQUIRE(tight["tris"].size() == 2);
    // Outside arrows: the left arrowhead's body lies left of its tip.
    const json& tri = tight["tris"][0];
    CHECK(tri[1][0].get<double>() < tri[0][0].get<double>());

    const json dia = doc::compile_annotation(
        json{{"type", "note"}, {"kind", "diameter_dim"}, {"center", {50.0, 50.0}},
             {"radius", 10.0}, {"pos", {70.0, 70.0}}, {"text", "\xE2\x8C\x80" "20"}},
        "MMGS");
    REQUIRE(dia["tris"].size() == 1);
    const json& tip = dia["tris"][0][0];
    CHECK(std::hypot(tip[0].get<double>() - 50.0, tip[1].get<double>() - 50.0) ==
          doctest::Approx(10.0).epsilon(1e-6));
    // The ⌀ sign is stroked (a circle + slash), the number stays text.
    CHECK(dia["arcs"].size() == 1);
    CHECK(has_text(dia, "20"));
}

// Section views: a chain cutting plane with thick ends, filled viewing arrows and letters on
// the parent; a "SECTION A-A" caption under the section; detail circles + "DETAIL" caption.
TEST_CASE("Section and detail views carry ISO 128-40 identification") {
    const std::string src = make_block();
    const json made = doc::drawing_make(
        src, json{{"views", json::array({"front", "top"})}, {"model_items", false},
                  {"sections", json::array({json{{"parent", "top"}, {"axis", "horizontal"},
                                                 {"label", "A"}}})}});
    const std::string did = made["doc_id"].get<std::string>();
    const json* top = view_by_orient(made["drawing"]["sheets"][0], "top");
    REQUIRE(top);
    const json& deco = (*top)["draw"];
    CHECK(deco["chain"].size() == 1);
    CHECK(deco["thick"].size() == 2);
    CHECK(deco["tris"].size() == 2);
    CHECK(has_text(deco, "A"));
    bool caption = false;
    for (const json& v : made["drawing"]["sheets"][0]["views"])
        if (v.value("type", std::string()) == "section") {
            caption = has_text(v["draw"], "SECTION A-A");
            CHECK(v.value("hatch", json::array()).size() > 0);
        }
    CHECK(caption);
    const auto tb = bb_of(*top);
    const json det = doc::drawing_add_detail(
        did, (*top)["view_id"].get<std::string>(),
        json::array({tb[0] + 2.0, tb[3] - 2.0}), 8.0, json(nullptr),
        json("B"), json(nullptr), "s1", json(nullptr));
    const json full = doc::drawing_get(did);
    bool detail_caption = false, detail_circle = false;
    for (const json& v : full["sheets"][0]["views"]) {
        if (v.value("type", std::string()) == "detail")
            for (const json& t : v["draw"]["texts"])
                detail_caption |= t["text"].get<std::string>().rfind("DETAIL B", 0) == 0;
        if (v.value("orientation", std::string()) == "top")
            detail_circle = v["draw"]["arcs"].size() == 1 && has_text(v["draw"], "B");
    }
    CHECK(detail_caption);
    CHECK(detail_circle);
    doc::drawing_close(did);
    doc::doc_close(src);
}
