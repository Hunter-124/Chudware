// sketch3d document-layer tests: wire chaining (geom::wires_from_sketch3d), the
// sketch3d feature compile, a sweep whose SPINE is a sketch3d wire (line + arc +
// spline chain), a sweep whose GUIDE is a sketch3d line, and the sketch3d_health /
// sketch3d_drag RPCs through the session layer. Mirrors tests/test_document.cpp's
// analytic-invariant discipline (volumes / statuses, not screenshots).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include <BRepCheck_Analyzer.hxx>
#include <TopExp_Explorer.hxx>
#include <TopoDS_Wire.hxx>

#include "doc/document.h"
#include "doc/session.h"
#include "doc/selectors.h"
#include "doc/picks.h"
#include "geom/profile3d.h"

using json = nlohmann::json;
using chudware::doc::CompileResult;
using chudware::doc::compile_document;

namespace {

// ---- 2D sketch DSL (profile sketches) --------------------------------------
json circle2(const char* id, double cx, double cy, double r) {
    return json{{"id", id}, {"type", "circle"}, {"construction", false},
                {"center", json::array({cx, cy})}, {"radius", r}};
}
json rel(const char* id, const char* type, std::vector<const char*> refs) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r}};
}
json dim(const char* id, const char* type, std::vector<const char*> refs, double v) {
    json r = json::array();
    for (auto s : refs) r.push_back(s);
    return json{{"id", id}, {"type", type}, {"refs", r},
                {"value", json{{"value", v}, {"expr", nullptr}}}, {"driven", false}};
}

// ---- 3D sketch DSL ----------------------------------------------------------
json line3(const char* id, std::vector<double> p1, std::vector<double> p2) {
    return json{{"id", id}, {"type", "line"}, {"construction", false},
                {"p1", p1}, {"p2", p2}};
}
json arc3(const char* id, std::vector<double> p1, std::vector<double> pm,
          std::vector<double> p2) {
    return json{{"id", id}, {"type", "arc"}, {"construction", false},
                {"p1", p1}, {"pm", pm}, {"p2", p2}};
}
json spline3(const char* id, std::vector<std::vector<double>> pts) {
    json arr = json::array();
    for (auto& p : pts) arr.push_back(p);
    return json{{"id", id}, {"type", "spline"}, {"construction", false},
                {"points", arr}};
}

// A fully-fixed circle profile of radius R at the origin on Top (XY, normal +Z).
json prof_sketch(double R) {
    json p;
    p["id"] = "prof"; p["type"] = "sketch"; p["plane"] = "top";
    p["entities"] = json::array({circle2("e1", 0, 0, R)});
    p["constraints"] = json::array({rel("c1", "fix", {"e1.center"}),
                                    dim("c2", "radius", {"e1"}, R)});
    return p;
}

constexpr double kPi = 3.14159265358979323846;

}  // namespace

// 1. Wire chaining: a contiguous line+arc+spline chain becomes ONE wire; a
//    disconnected extra line becomes a SECOND wire; construction geometry and
//    points never chain. Wire 0 = the chain of the earliest-authored entity.
TEST_CASE("wires_from_sketch3d chains contiguous edges, splits disconnected ones") {
    // Solved-entity map form (id -> dict), as Sketch3DSolution::entities emits.
    json ents = json::object();
    ents["e1"] = json{{"type", "line"}, {"construction", false},
                      {"p1", json::array({0, 0, 0})}, {"p2", json::array({0, 0, 20})}};
    // Quarter arc in XZ: (0,0,20) -> (10,0,30), center (10,0,20).
    ents["e2"] = json{{"type", "arc"}, {"construction", false},
                      {"p1", json::array({0, 0, 20})},
                      {"pm", json::array({10.0 - 10.0 / std::sqrt(2.0), 0,
                                          20.0 + 10.0 / std::sqrt(2.0)})},
                      {"p2", json::array({10, 0, 30})}};
    ents["e3"] = json{{"type", "spline"}, {"construction", false},
                      {"points", json::array({json::array({10, 0, 30}),
                                              json::array({15, 2, 31}),
                                              json::array({20, 0, 32})})}};
    // Disconnected second chain + a construction line + a bare point.
    ents["e4"] = json{{"type", "line"}, {"construction", false},
                      {"p1", json::array({100, 0, 0})}, {"p2", json::array({120, 0, 0})}};
    ents["e5"] = json{{"type", "line"}, {"construction", true},
                      {"p1", json::array({0, 0, 0})}, {"p2", json::array({100, 0, 0})}};
    ents["e6"] = json{{"type", "point"}, {"construction", false},
                      {"p", json::array({50, 50, 50})}};

    std::vector<TopoDS_Wire> wires = chudware::geom::wires_from_sketch3d(ents);
    REQUIRE(wires.size() == 2);
    int n0 = 0, n1 = 0;
    for (TopExp_Explorer ex(wires[0], TopAbs_EDGE); ex.More(); ex.Next()) ++n0;
    for (TopExp_Explorer ex(wires[1], TopAbs_EDGE); ex.More(); ex.Next()) ++n1;
    CHECK(n0 == 3);  // line + arc + spline chained (wire 0: earliest entity e1)
    CHECK(n1 == 1);  // the disconnected e4
    CHECK(!wires[0].Closed());
}

// 2. End-to-end: a sweep whose SPINE is a sketch3d line+arc+spline chain, with a
//    2D circle profile at the spine start, builds a valid solid whose volume is
//    at least the straight-segment tube (analytic lower bound) and close to
//    pi R^2 * total path length.
TEST_CASE("sweep along a sketch3d spine (line+arc+spline chain) -> valid solid") {
    const double R = 3.0;
    json sk3;
    sk3["id"] = "spine"; sk3["type"] = "sketch3d";
    sk3["entities"] = json::array({
        line3("e1", {0, 0, 0}, {0, 0, 20}),
        arc3("e2", {0, 0, 20},
             {10.0 - 10.0 / std::sqrt(2.0), 0, 20.0 + 10.0 / std::sqrt(2.0)},
             {10, 0, 30}),
        spline3("e3", {{10, 0, 30}, {15, 2, 31}, {20, 0, 32}}),
    });
    sk3["constraints"] = json::array({rel("c1", "fix", {"e1"}), rel("c2", "fix", {"e2"}),
                                      rel("c3", "fix", {"e3"})});

    json doc;
    doc["features"] = json::array({
        prof_sketch(R), sk3,
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "spine"}},
    });
    CompileResult r = compile_document(doc, json::object());
    for (const auto& s : r.statuses)
        if (!s.ok) MESSAGE("feat " << s.id << ": " << s.error);
    REQUIRE(r.ok);
    REQUIRE(r.body_count == 1);
    // Path length: 20 (line) + 2*pi*10/4 (quarter arc) + >= ~10.6 (spline chord).
    const double min_len = 20.0 + kPi * 5.0;  // line + arc only (spline extra)
    MESSAGE("sketch3d-spine sweep volume=" << r.volume);
    CHECK(r.volume > kPi * R * R * min_len * 0.85);
    CHECK(r.volume < kPi * R * R * (min_len + 15.0) * 1.15);
}

// 2b. The {"feature":fid,"wire":k} ref form selects a chain explicitly, and a
//     multi-chain sketch3d referenced by bare id is rejected loudly.
TEST_CASE("sketch3d multi-wire path ref: explicit wire index required") {
    const double R = 2.0;
    json sk3;
    sk3["id"] = "spine"; sk3["type"] = "sketch3d";
    sk3["entities"] = json::array({line3("e1", {0, 0, 0}, {0, 0, 15}),
                                   line3("e2", {100, 0, 0}, {100, 0, 25})});
    sk3["constraints"] = json::array();

    // Bare-id ref with two chains -> loud error naming the fix.
    json doc;
    doc["features"] = json::array({
        prof_sketch(R), sk3,
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "spine"}}});
    CompileResult r = compile_document(doc, json::object());
    CHECK_FALSE(r.ok);
    bool saw = false;
    for (const auto& s : r.statuses)
        if (s.id == "sw") {
            saw = true;
            CHECK(s.error.find("wire") != std::string::npos);
        }
    CHECK(saw);

    // Explicit {"feature":"spine","wire":0} -> sweeps the first chain (len 15).
    json doc2;
    doc2["features"] = json::array({
        prof_sketch(R), sk3,
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"},
             {"path", json{{"feature", "spine"}, {"wire", 0}}}}});
    CompileResult r2 = compile_document(doc2, json::object());
    for (const auto& s : r2.statuses)
        if (!s.ok) MESSAGE("feat " << s.id << ": " << s.error);
    REQUIRE(r2.ok);
    CHECK(r2.volume == doctest::Approx(kPi * R * R * 15.0).epsilon(5e-2));
}

// 3. Sweep with a sketch3d GUIDE: same profile+2D path that sweeps a plain
//    cylinder; a sketch3d guide line diverging from the path scales the section
//    up, so the guided volume clearly exceeds the plain sweep (the same
//    invariant as the 2D guide test — proves the guides[] ref consumes a
//    sketch3d wire end-to-end).
TEST_CASE("sketch3d guide curve scales a swept profile end-to-end") {
    const double R = 4.0, Hh = 20.0;
    auto path2d = [&] {
        json p;
        p["id"] = "path"; p["type"] = "sketch"; p["plane"] = "front";
        p["entities"] = json::array(
            {json{{"id", "e1"}, {"type", "line"}, {"construction", false},
                  {"p1", json::array({0, 0})}, {"p2", json::array({0, Hh})}}});
        p["constraints"] = json::array({rel("c1", "fix", {"e1.p1"}),
                                        rel("c2", "vertical", {"e1"}),
                                        dim("c3", "distance", {"e1.p1", "e1.p2"}, Hh)});
        return p;
    };
    json base;
    base["features"] = json::array({prof_sketch(R), path2d(),
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "path"}}});
    CompileResult rb = compile_document(base, json::object());
    REQUIRE(rb.ok);

    // Guide in WORLD coords: starts on the profile rim (R,0,0), rises to (2R,0,Hh).
    json guide;
    guide["id"] = "guide"; guide["type"] = "sketch3d";
    guide["entities"] = json::array({line3("e1", {R, 0, 0}, {2 * R, 0, Hh})});
    guide["constraints"] = json::array({rel("c1", "fix", {"e1"})});

    json g;
    g["features"] = json::array({prof_sketch(R), path2d(), guide,
        json{{"id", "sw"}, {"type", "sweep"}, {"profile", "prof"}, {"path", "path"},
             {"guides", json::array({json{{"feature", "guide"}, {"wire", 0}}})}}});
    CompileResult rg = compile_document(g, json::object());
    for (const auto& s : rg.statuses)
        if (!s.ok) MESSAGE("feat " << s.id << ": " << s.error);
    REQUIRE(rg.ok);
    REQUIRE(rg.body_count == 1);
    MESSAGE("sketch3d-guided vol=" << rg.volume << " plain vol=" << rb.volume);
    CHECK(rg.volume > 1.4 * rb.volume);
}

// 4. sketch3d_health + sketch3d_drag through the SESSION layer (the RPC surface
//    the GUI consumes): create a doc via ops, add a sketch3d, read health, drag
//    a handle, commit via edit_feature, re-read health.
TEST_CASE("sketch3d health + drag RPCs through the session layer") {
    json newdoc = chudware::doc::doc_new("part", "sketch3d-rpc-test");
    const std::string doc_id = newdoc["doc_id"].get<std::string>();

    json sk3 = json{{"type", "sketch3d"},
                    {"params", json{{"entities", json::array({
                                         line3("e1", {0, 0, 0}, {10, 0, 0})})},
                                    {"constraints", json::array({
                                         rel("c1", "fix", {"e1.p1"})})}}}};
    json ops = json::array({json{{"op", "add_feature"}, {"feature", sk3}}});
    json r = chudware::doc::doc_apply_ops(doc_id, -1, ops, false);
    const std::string fid = r["id_map"]["$0"].get<std::string>();
    REQUIRE(!fid.empty());

    json h = chudware::doc::doc_sketch3d_health(doc_id, fid);
    CHECK(h["ok"].get<bool>());
    CHECK(h["converged"].get<bool>());
    CHECK(h["dof"].get<int>() == 3);  // e1.p2 free in x/y/z
    CHECK(h["status"] == "under");
    REQUIRE(h["wires"].size() == 1);
    CHECK(h["wires"][0]["n_edges"].get<int>() == 1);
    CHECK_FALSE(h["wires"][0]["closed"].get<bool>());
    // free vars name the loose params on e1.p2
    bool saw_p2 = false;
    for (const auto& fv : h["free"])
        if (fv["entity"] == "e1" && fv["param"].get<std::string>().rfind("p2", 0) == 0)
            saw_p2 = true;
    CHECK(saw_p2);

    // Drag e1.p2 to (0, 25, 5): p1 stays pinned, p2 follows the target.
    json d = chudware::doc::doc_sketch3d_drag(doc_id, fid, "e1.p2",
                                           json::array({0.0, 25.0, 5.0}), json());
    REQUIRE(d["ok"].get<bool>());
    const json* e1 = nullptr;
    for (const auto& e : d["entities"])
        if (e["id"] == "e1") e1 = &e;
    REQUIRE(e1 != nullptr);
    CHECK((*e1)["p1"][0].get<double>() == doctest::Approx(0.0).epsilon(1e-6));
    CHECK((*e1)["p2"][1].get<double>() == doctest::Approx(25.0).epsilon(1e-2));
    CHECK((*e1)["p2"][2].get<double>() == doctest::Approx(5.0).epsilon(1e-2));

    // Commit = plain edit_feature with the solved entities (the documented flow).
    json solved_entities = json::array();
    for (const auto& e : d["entities"]) solved_entities.push_back(e);
    json commit = json::array({json{{"op", "edit_feature"}, {"id", fid},
                                    {"params", json{{"entities", solved_entities}}}}});
    json rc = chudware::doc::doc_apply_ops(doc_id, -1, commit, false);
    CHECK(rc.contains("revision"));

    json h2 = chudware::doc::doc_sketch3d_health(doc_id, fid);
    CHECK(h2["ok"].get<bool>());
    const json* e1b = nullptr;
    for (const auto& e : h2["entities"])
        if (e["id"] == "e1") e1b = &e;
    REQUIRE(e1b != nullptr);
    CHECK((*e1b)["p2"][1].get<double>() == doctest::Approx(25.0).epsilon(1e-2));

    // A bogus handle is rejected loudly, entities preserved.
    json bad = chudware::doc::doc_sketch3d_drag(doc_id, fid, "e9.p1",
                                             json::array({0.0, 0.0, 0.0}), json());
    CHECK_FALSE(bad["ok"].get<bool>());
    CHECK(bad["entities"].size() == 1);

    chudware::doc::doc_close(doc_id);
}

// ---- sketch3d external vertex references ------------------------------------

TEST_CASE("sketch3d external vertex ref: resolves, grounds a coincident constraint, "
         "follows an upstream edit, goes stale (kept) when the source is deleted") {
    using chudware::doc::doc_new;
    using chudware::doc::doc_apply_ops;
    json nr = doc_new("part", "sketch3d-xref-test");
    const std::string doc_id = nr["doc_id"].get<std::string>();

    auto dimv = [](double v) { return json{{"value", v}, {"expr", nullptr}}; };
    json add_box = json{{"op", "add_feature"}, {"feature", {{"type", "box"}, {"name", "Blk"},
        {"params", {{"length", dimv(20)}, {"width", dimv(20)}, {"height", dimv(20)}}}}}};
    json r1 = doc_apply_ops(doc_id, -1, json::array({add_box}), false);
    REQUIRE(r1.contains("revision"));
    const std::string box_fid = r1["id_map"]["$0"].get<std::string>();
    REQUIRE(!box_fid.empty());

    // Find the box corner at (20,20,20) through the same RPC surface a GUI pick
    // would use (doc_resolve_pick) — scan vertex topo_ids for the one nearest our
    // target corner (doc_resolve_pick needs a topo_id, which we don't know a
    // priori from a plain box feature).
    json best_sel;
    double best_d = 1e18;
    for (int tid = 1; tid <= 64; ++tid) {
        json pick;
        try {
            pick = chudware::doc::doc_resolve_pick(doc_id, 1, "vertex", tid);
        } catch (...) { break; }
        const json& sel = pick["selector"];
        if (!sel.contains("centroid")) continue;
        Eigen::Vector3d c(sel["centroid"][0].get<double>(), sel["centroid"][1].get<double>(),
                          sel["centroid"][2].get<double>());
        double d = (c - Eigen::Vector3d(-10, -10, -10)).norm();
        if (d < best_d) { best_d = d; best_sel = sel; }
    }
    REQUIRE(best_d < 1e-6);

    json sk3 = json{{"type", "sketch3d"},
                    {"params", json{
                        {"entities", json::array({line3("e1", {0, 0, 0}, {5, 5, 5})})},
                        {"constraints", json::array({rel("c1", "fix", {"e1.p1"}),
                                                     rel("c2", "coincident", {"e1.p2", "x1"})})},
                        {"external_refs", json::array({
                            json{{"id", "x1"},
                                {"source", {{"kind", "vertex"}, {"selector", best_sel}}},
                                {"stale", true}}})}}}};
    json ops2 = json::array({json{{"op", "add_feature"}, {"feature", sk3}}});
    json r2 = doc_apply_ops(doc_id, -1, ops2, false);
    const std::string fid = r2["id_map"]["$0"].get<std::string>();
    REQUIRE(!fid.empty());

    json h = chudware::doc::doc_sketch3d_health(doc_id, fid);
    REQUIRE(h["ok"].get<bool>());
    const json* e1 = nullptr;
    for (const auto& e : h["entities"]) if (e["id"] == "e1") e1 = &e;
    REQUIRE(e1 != nullptr);
    // THE demo case: the constrained endpoint follows the resolved corner.
    CHECK((*e1)["p2"][0].get<double>() == doctest::Approx(-10.0).epsilon(1e-4));
    CHECK((*e1)["p2"][1].get<double>() == doctest::Approx(-10.0).epsilon(1e-4));
    CHECK((*e1)["p2"][2].get<double>() == doctest::Approx(-10.0).epsilon(1e-4));

    // Edit the box (height 20 -> 35): the corner moves to (10,10,17.5); re-resolve
    // (any mutating doc_apply_ops batch refreshes external_refs) and re-check —
    // the constrained endpoint follows the moved corner.
    json edit_box = json{{"op", "edit_feature"}, {"id", "f1"},
                         {"params", {{"height", dimv(35)}}}};
    json r3 = doc_apply_ops(doc_id, -1, json::array({edit_box}), false);
    REQUIRE(r3.contains("revision"));
    json h2 = chudware::doc::doc_sketch3d_health(doc_id, fid);
    REQUIRE(h2["ok"].get<bool>());
    const json* e1b = nullptr;
    for (const auto& e : h2["entities"]) if (e["id"] == "e1") e1b = &e;
    REQUIRE(e1b != nullptr);
    CHECK((*e1b)["p2"][2].get<double>() == doctest::Approx(-17.5).epsilon(1e-4));

    // Delete the box: the vertex source can no longer resolve. The ref goes
    // stale but keeps its LAST-GOOD ground point — the sketch keeps solving.
    json del_box = json{{"op", "delete_feature"}, {"id", box_fid}};
    json r4 = doc_apply_ops(doc_id, -1, json::array({del_box}), false);
    REQUIRE(r4.contains("revision"));
    json h3 = chudware::doc::doc_sketch3d_health(doc_id, fid);
    REQUIRE(h3["ok"].get<bool>());
    const json* e1c = nullptr;
    for (const auto& e : h3["entities"]) if (e["id"] == "e1") e1c = &e;
    REQUIRE(e1c != nullptr);
    // Last-good position (from the height=35 state) kept, not silently zeroed.
    CHECK((*e1c)["p2"][2].get<double>() == doctest::Approx(-17.5).epsilon(1e-4));

    chudware::doc::doc_close(doc_id);
}
