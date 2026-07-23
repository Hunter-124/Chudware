// Assembly component insertion — proves asm_insert_component places a component
// into an assembly document and that it appears in the tree.
//
// Mirrors the host Insert Component flow (new assembly → insert source path) at
// the core/doc layer so the insert contract is locked without a GUI harness.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <filesystem>
#include <string>

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
