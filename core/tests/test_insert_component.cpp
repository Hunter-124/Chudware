// Assembly component insertion — proves asm_insert_component places a component
// into an assembly document and that it appears in the tree.
//
// Mirrors the host Insert Component flow (new assembly → insert source path) at
// the core/doc layer so the insert contract is locked without a GUI harness.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <filesystem>
#include <string>
#include <vector>

#include <BRepPrimAPI_MakeBox.hxx>
#include <TopoDS_Shape.hxx>
#include <gp_Pnt.hxx>

#include "doc/formats.h"
#include "doc/session.h"

using nlohmann::json;
namespace fs = std::filesystem;
namespace doc = chudware::doc;

namespace {
fs::path tmp(const std::string& name) {
    return fs::temp_directory_path() / ("chudware_asmins_" + name);
}

// 16-value row-major identity with a translation on X.
json pose_tx(double tx) {
    return json::array({1.0, 0.0, 0.0, tx,
                        0.0, 1.0, 0.0, 0.0,
                        0.0, 0.0, 1.0, 0.0,
                        0.0, 0.0, 0.0, 1.0});
}

// A component's live 16-value transform out of the document tree.
std::vector<double> comp_transform(const std::string& did, const std::string& cid) {
    json resp = doc::doc_tree(did);
    const json& body = resp.contains("tree") ? resp["tree"] : resp;
    REQUIRE(body.is_object());
    REQUIRE(body.contains("components"));
    for (const json& c : body["components"])
        if (c.value("id", std::string()) == cid) {
            REQUIRE(c.contains("transform"));
            REQUIRE(c["transform"].is_array());
            return c["transform"].get<std::vector<double>>();
        }
    REQUIRE_MESSAGE(false, "component not found in tree: ", cid);
    return {};
}

// Two 10-mm boxes inserted into a fresh assembly. Returns {doc_id, cid_a (fixed), cid_b}.
struct TwoComp { std::string doc_id, cid_a, cid_b; };

TwoComp two_component_assembly(const fs::path& a, const fs::path& b, const std::string& name) {
    doc::export_bodies({BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()},
                       a.string(), "step");
    doc::export_bodies({BRepPrimAPI_MakeBox(gp_Pnt(40, 0, 0), 10.0, 10.0, 10.0).Shape()},
                       b.string(), "step");
    json nresp = doc::doc_new("assembly", name);
    const std::string did = nresp.at("doc_id").get<std::string>();
    json ops = json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", a.string()}}}},
        {{"op", "asm_insert_component"}, {"source", {{"path", b.string()}}}},
    });
    json r = doc::doc_apply_ops(did, -1, ops, false);
    REQUIRE(!r.contains("code"));
    const json id_map = r.value("id_map", json::object());
    return {did, id_map.value("$0", std::string("k1")), id_map.value("$1", std::string("k2"))};
}
}  // namespace

// --- Insert a single component into a fresh assembly and verify it appears. ---
TEST_CASE("asm_insert_component places a component into the assembly tree") {
    fs::path part_step = tmp("part.step");
    doc::export_bodies({BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 20.0, 15.0, 10.0).Shape()},
                       part_step.string(), "step");

    json nresp = doc::doc_new("assembly", "insert_comp_test");
    REQUIRE(nresp.contains("doc_id"));
    const std::string did = nresp.at("doc_id").get<std::string>();
    REQUIRE(!did.empty());

    // Empty assembly has zero components.
    {
        json tree = doc::doc_tree(did);
        REQUIRE(tree.is_object());
        // doc_tree returns {revision, tree, ...}; tolerate either shape.
        const json& body = tree.contains("tree") ? tree["tree"] : tree;
        if (body.is_object() && body.contains("components") && body["components"].is_array())
            CHECK(body["components"].empty());
    }

    // Insert the part — same op the host Insert Component dialog emits.
    json ops = json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", part_step.string()}}}},
    });
    json r = doc::doc_apply_ops(did, -1, ops, false);
    REQUIRE(!r.contains("code"));  // no error envelope
    const bool hasTreeOrRev = r.contains("tree") || r.contains("revision");
    REQUIRE(hasTreeOrRev);

    // Component must appear in the assembly tree with the source path preserved.
    json treeResp = doc::doc_tree(did);
    REQUIRE(treeResp.is_object());
    const json& body = treeResp.contains("tree") ? treeResp["tree"] : treeResp;
    REQUIRE(body.is_object());
    REQUIRE(body.contains("components"));
    REQUIRE(body["components"].is_array());
    REQUIRE(body["components"].size() == 1);

    const json& comp = body["components"][0];
    CHECK(comp.contains("id"));
    CHECK(!comp.value("id", std::string()).empty());
    CHECK(comp.value("fixed", false) == true);  // first component is auto-fixed
    REQUIRE(comp.contains("source"));
    REQUIRE(comp["source"].is_object());
    CHECK(comp["source"].value("path", std::string()) == part_step.string());
    // Name defaults to the source file stem.
    CHECK(comp.value("name", std::string()) == part_step.stem().string());

    // id_map from apply_ops should map $0 → the new component id.
    if (r.contains("id_map") && r["id_map"].is_object()) {
        const std::string mapped = r["id_map"].value("$0", std::string());
        if (!mapped.empty())
            CHECK(mapped == comp.value("id", std::string()));
    }

    // A second insert grows the component list (not fixed).
    fs::path part2 = tmp("peg.step");
    doc::export_bodies({BRepPrimAPI_MakeBox(gp_Pnt(30, 0, 0), 8.0, 8.0, 20.0).Shape()},
                       part2.string(), "step");
    json ops2 = json::array({
        {{"op", "asm_insert_component"}, {"source", {{"path", part2.string()}}}},
    });
    json r2 = doc::doc_apply_ops(did, -1, ops2, false);
    REQUIRE(!r2.contains("code"));

    json tree2 = doc::doc_tree(did);
    const json& body2 = tree2.contains("tree") ? tree2["tree"] : tree2;
    REQUIRE(body2["components"].is_array());
    REQUIRE(body2["components"].size() == 2);
    CHECK(body2["components"][1].value("fixed", true) == false);
    CHECK(body2["components"][1].value("name", std::string()) == part2.stem().string());

    fs::remove(part_step);
    fs::remove(part2);
}

// Re-posing an EXISTING component must stick. Every doc_apply_ops on an assembly runs
// rebuild_assembly -> mate::solve_mates, which seeds each component from the session's
// warm map (the previous solve's params) in preference to components[].transform and then
// writes the solved transforms back over the doc — so before the warm entry was
// invalidated, an authored transform was discarded by the very rebuild its own op
// triggered (inserts survived only because the new id was not in the map yet).
TEST_CASE("asm_edit_component transform survives the rebuild it triggers") {
    fs::path pa = tmp("edit_a.step");
    fs::path pb = tmp("edit_b.step");
    TwoComp asm_ = two_component_assembly(pa, pb, "edit_comp_test");

    // The floating component: authored pose, no mates to own it.
    json edit = json::array({
        {{"op", "asm_edit_component"}, {"id", asm_.cid_b}, {"transform", pose_tx(111.0)}},
    });
    json er = doc::doc_apply_ops(asm_.doc_id, -1, edit, false);
    REQUIRE(!er.contains("code"));
    std::vector<double> tb = comp_transform(asm_.doc_id, asm_.cid_b);
    REQUIRE(tb.size() == 16);
    CHECK(std::abs(tb[3] - 111.0) < 1e-9);

    // A later, unrelated batch re-solves again — the pose must not revert.
    json rename = json::array({
        {{"op", "asm_edit_component"}, {"id", asm_.cid_b}, {"name", "Lid"}},
    });
    REQUIRE(!doc::doc_apply_ops(asm_.doc_id, -1, rename, false).contains("code"));
    tb = comp_transform(asm_.doc_id, asm_.cid_b);
    CHECK(std::abs(tb[3] - 111.0) < 1e-9);

    // The FIXED anchor too: its pose is authored, never solved (mate.cpp keeps a fixed
    // frame off the warm map entirely).
    json edit_anchor = json::array({
        {{"op", "asm_edit_component"}, {"id", asm_.cid_a}, {"transform", pose_tx(-25.0)}},
    });
    REQUIRE(!doc::doc_apply_ops(asm_.doc_id, -1, edit_anchor, false).contains("code"));
    std::vector<double> ta = comp_transform(asm_.doc_id, asm_.cid_a);
    REQUIRE(ta.size() == 16);
    CHECK(std::abs(ta[3] + 25.0) < 1e-9);

    fs::remove(pa);
    fs::remove(pb);
}

// The other half of the contract: for a MATED, unfixed component the transform is only a
// solver seed, so authoring it is an engine-level no-op — the component stays exactly on
// the mate solution. The warm entry is deliberately NOT dropped here: re-seeding the mate
// solve from an off-solution pose lands it in a different, tilted minimum rather than
// snapping back, so only a fixed or unmated component gets its seed invalidated.
TEST_CASE("a mated component's transform edit stays solver-owned") {
    fs::path pa = tmp("mated_a.step");
    fs::path pb = tmp("mated_b.step");
    TwoComp asm_ = two_component_assembly(pa, pb, "mated_edit_test");

    // Plane-distance mate between the two +Z faces (selectors are in each component's
    // own local geometry, so the second box's centroid is at x=45).
    json refs = json::array({
        {{"component", asm_.cid_a},
         {"selector", {{"kind", "face"}, {"centroid", json::array({5.0, 5.0, 10.0})}}}},
        {{"component", asm_.cid_b},
         {"selector", {{"kind", "face"}, {"centroid", json::array({45.0, 5.0, 10.0})}}}},
    });
    json add_mate = json::array({
        {{"op", "asm_add_mate"},
         {"mate", {{"type", "distance"},
                   {"value", {{"value", 30.0}, {"expr", nullptr}}},
                   {"align", "aligned"},
                   {"suppressed", false},
                   {"refs", refs}}}},
    });
    REQUIRE(!doc::doc_apply_ops(asm_.doc_id, -1, add_mate, false).contains("code"));

    const std::vector<double> solved = comp_transform(asm_.doc_id, asm_.cid_b);
    REQUIRE(solved.size() == 16);

    // Author the solved pose displaced 7 mm along Z — the one direction the mate owns.
    std::vector<double> off = solved;
    off[11] += 7.0;
    json edit = json::array({
        {{"op", "asm_edit_component"}, {"id", asm_.cid_b}, {"transform", off}},
    });
    json resp = doc::doc_apply_ops(asm_.doc_id, -1, edit, false);
    REQUIRE(!resp.contains("code"));

    // Diagnostic: the rebuild that consumed the edit must itself report a healthy solve —
    // a pose that merely "looks unchanged" out of a conflicting/unresolved solve is not
    // the contract. The assembly rebuild reports per-mate status plus the doc-level roll-up.
    const json rebuild = resp.value("rebuild", json::object());
    CHECK(rebuild.value("over_defined", true) == false);
    CHECK(rebuild.value("doc_status", json::object()).value("mates", std::string()) == "ok");
    REQUIRE(rebuild.value("mates", json::array()).size() == 1);
    for (const json& ms : rebuild["mates"]) CHECK(ms.value("status", std::string()) == "ok");
    for (const json& cs : rebuild.value("components", json::array()))
        CHECK(cs.value("ok", false) == true);

    const std::vector<double> after = comp_transform(asm_.doc_id, asm_.cid_b);
    REQUIRE(after.size() == 16);
    // Back on the mate solution, not at the authored offset.
    CHECK(std::abs(after[11] - solved[11]) < 1e-4);
    CHECK(std::abs(after[11] - off[11]) > 1.0);
    for (int i = 0; i < 16; ++i) CHECK(std::abs(after[i] - solved[i]) < 1e-4);

    fs::remove(pa);
    fs::remove(pb);
}
