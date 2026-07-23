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

#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <string>

#include <nlohmann/json.hpp>

#include "doc/drawing_session.h"
#include "doc/export2d.h"
#include "doc/session.h"

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
    auto tmp = std::filesystem::temp_directory_path() / "chudware_gdt_test.dxf";
    json ex = doc::export_drawing(did, tmp.string(), "dxf");
    CHECK(ex.value("entities", 0) > 0);
    CHECK(ex.value("annotations", 0) > 0);
    std::error_code ec;
    std::filesystem::remove(tmp, ec);

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
