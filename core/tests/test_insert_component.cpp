// Assembly component insertion — proves asm_insert_component places a component
// into an assembly document and that it appears in the tree.
//
// Mirrors the host Insert Component flow (new assembly → insert source path) at
// the core/doc layer so the insert contract is locked without a GUI harness.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <filesystem>
#include <string>
#include <vector>

#include <BRepAlgoAPI_Fuse.hxx>
#include <BRepPrimAPI_MakeBox.hxx>
#include <TopoDS_Shape.hxx>
#include <Standard_Version.hxx>
#include <gp_Pnt.hxx>

#include "doc/assembly.h"
#include "doc/formats.h"
#include "doc/session.h"
#include "doc/selectors.h"
#include "doc/stepplus.h"

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

// Grounded-frame drift, at the document layer. `rebuild_assembly` writes the solved
// transforms back over the doc, so whatever the mate solve returns for the GROUNDED
// component is what a save captures. The solver used to hand back the anchor's matrix
// re-derived from (t, exp-map w) — and a merged import's placement is authored from
// MEASURED axes, so its 3x3 block is orthonormal only to within the measurement and the
// round trip is a real re-pose: the rover's frame moved ~0.046 mm at its corners, which
// is what put a 73.7 mm3 frame<->hub overlap into an assembly whose transform-placed
// twin was clean. A fixed component's transform is a hard constraint; the rebuild must
// be a byte-for-byte no-op on it.
TEST_CASE("a grounded component's measured placement survives the rebuild unchanged") {
    fs::path pa = tmp("anchor_a.step");
    fs::path pb = tmp("anchor_b.step");
    TwoComp asm_ = two_component_assembly(pa, pb, "anchor_pose_test");

    // Authored from two measured axes 1e-4 rad off perpendicular, 0.6 m out.
    const json measured = json::array({1.0,   1.0e-4, 0.0, 610.0,
                                       0.0,   1.0,    0.0, -25.0,
                                       0.0,   0.0,    1.0, 40.0,
                                       0.0,   0.0,    0.0, 1.0});
    json edit = json::array({
        {{"op", "asm_edit_component"}, {"id", asm_.cid_a}, {"transform", measured}},
    });
    REQUIRE(!doc::doc_apply_ops(asm_.doc_id, -1, edit, false).contains("code"));

    const std::vector<double> authored = measured.get<std::vector<double>>();
    std::vector<double> ta = comp_transform(asm_.doc_id, asm_.cid_a);
    REQUIRE(ta.size() == 16);
    for (size_t i = 0; i < authored.size(); ++i) CHECK(ta[i] == authored[i]);

    // And it stays put across further rebuilds, so a re-solve cannot walk the anchor.
    json touch = json::array({
        {{"op", "asm_edit_component"}, {"id", asm_.cid_b}, {"transform", pose_tx(60.0)}},
    });
    REQUIRE(!doc::doc_apply_ops(asm_.doc_id, -1, touch, false).contains("code"));
    ta = comp_transform(asm_.doc_id, asm_.cid_a);
    REQUIRE(ta.size() == 16);
    for (size_t i = 0; i < authored.size(); ++i) CHECK(ta[i] == authored[i]);

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
    // …and it now carries the solve's own verdict plus a per-mate residual, which is what
    // the agent-facing reply reads. Before this, an assembly rebuild reported nothing but
    // ok/over — a solve that never converged still said `mates: "ok"`.
    CHECK(rebuild.value("converged", false) == true);
    CHECK(rebuild.value("max_residual", 1.0) <= rebuild.value("tolerance", 0.0));
    CHECK(rebuild["mates"][0].contains("residual"));
    CHECK(rebuild["mates"][0].value("residual", 1.0) <= rebuild.value("tolerance", 0.0));
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

// A network no pose can satisfy must SAY SO, and say which mate. A design
// session once spent hours bisecting a 20-mate rover assembly by hand because the
// rebuild reported only `mates: "ok"` / `"over"` and the poses had been scattered by the
// solver's own restart escape, so there was nothing to read the failure off.
TEST_CASE("an unsatisfiable mate network is reported, named and bounded") {
    fs::path pa = tmp("conflict_a.step");
    fs::path pb = tmp("conflict_b.step");
    TwoComp asm_ = two_component_assembly(pa, pb, "mate_conflict_test");

    auto face_refs = []() {
        return json::array({
            {{"component", ""}, {"selector", {{"kind", "face"},
                                              {"centroid", json::array({5.0, 5.0, 10.0})}}}},
            {{"component", ""}, {"selector", {{"kind", "face"},
                                              {"centroid", json::array({45.0, 5.0, 10.0})}}}},
        });
    };
    auto distance_mate = [&](double value) {
        json refs = face_refs();
        refs[0]["component"] = asm_.cid_a;
        refs[1]["component"] = asm_.cid_b;
        return json{{"op", "asm_add_mate"},
                    {"mate", {{"type", "distance"},
                              {"value", {{"value", value}, {"expr", nullptr}}},
                              {"align", "aligned"},
                              {"suppressed", false},
                              {"refs", refs}}}};
    };
    // Two distances on the SAME face pair, 30 mm apart in what they demand.
    json ops = json::array({distance_mate(30.0), distance_mate(60.0)});
    json resp = doc::doc_apply_ops(asm_.doc_id, -1, ops, false);
    REQUIRE(!resp.contains("code"));

    const json rebuild = resp.value("rebuild", json::object());
    CHECK(rebuild.value("converged", true) == false);
    // The reply names the offender and quantifies it: the two demands split the difference,
    // so each carries half of the 30 mm disagreement.
    const std::string worst = rebuild.value("worst_mate", std::string());
    CHECK(!worst.empty());
    CHECK(rebuild.value("max_residual", 0.0) == doctest::Approx(15.0).epsilon(0.01));
    REQUIRE(rebuild.value("mates", json::array()).size() == 2);
    bool named = false;
    for (const json& ms : rebuild["mates"]) {
        CHECK(ms.value("residual", 0.0) == doctest::Approx(15.0).epsilon(0.01));
        if (ms.value("id", std::string()) == worst) named = true;
    }
    CHECK(named);

    // And the components are still where the document put them: the unsolvable network
    // leaves the assembly readable instead of scattering it.
    const std::vector<double> tb = comp_transform(asm_.doc_id, asm_.cid_b);
    REQUIRE(tb.size() == 16);
    CHECK(std::abs(tb[3]) < 50.0);
    CHECK(std::abs(tb[7]) < 50.0);
    CHECK(std::abs(tb[11]) < 50.0);

    fs::remove(pa);
    fs::remove(pb);
}

TEST_CASE("legacy split-face caches cannot override queried component topology") {
    // A geometry-semantic change can merge coplanar faces without changing the
    // document or OCCT version. Reproduce the old hub-cache disagreement with a
    // simple plate: the old BREP has two top faces; the current feature has one.
    const TopoDS_Shape left =
        BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 2.0).Shape();
    const TopoDS_Shape right =
        BRepPrimAPI_MakeBox(gp_Pnt(10, 0, 0), 10.0, 10.0, 2.0).Shape();
    BRepAlgoAPI_Fuse fuse(left, right);  // deliberately no same-domain unification
    REQUIRE(fuse.IsDone());
    const TopoDS_Shape split = fuse.Shape();
    const json part = {
        {"schema", "aicad.part/1"}, {"id", "legacy-plate"}, {"name", "Plate"},
        {"units", {{"system", "MMGS"}}},
        {"features", json::array({
            {{"id", "f1"}, {"name", "Plate"}, {"type", "box"}, {"suppressed", false},
             {"params", {{"length", 20.0}, {"width", 10.0}, {"height", 2.0},
                         {"centered", false}}},
             {"refs", json::object()}}
        })}};
    const json header = {
        {"engine", "chudware-geomcache/1;occt=" OCC_VERSION_COMPLETE},
        {"doc_hash", doc::stepplus_doc_hash(part)}, {"solved", true}};
    const fs::path path = tmp("legacy_split_faces.step+");
    struct Cleanup {
        fs::path path;
        std::string doc_id;
        ~Cleanup() {
            if (!doc_id.empty()) doc::doc_close(doc_id);
            std::error_code ec;
            fs::remove(path, ec);
        }
    } cleanup{path, {}};
    doc::stepplus_save(part, path.string(),
        {{"cache.json", header.dump()}, {"cache.brep", doc::brep_cache_encode({split})}});

    cleanup.doc_id = doc::doc_open(path.string(), /*as_solid=*/false, /*detach_path=*/true)
                         .at("doc_id").get<std::string>();
    const json query = doc::doc_query(cleanup.doc_id,
        {{"of", "face"}, {"surf", "plane"}, {"side", "top"},
         {"pick", "largest"}, {"count", 1}});
    REQUIRE(query.at("matches").size() == 1);
    const json selector = query.at("matches").at(0);
    REQUIRE(selector.at("area_or_len").get<double>() == doctest::Approx(200.0));
    // Control: it is genuinely ambiguous against the obsolete split topology.
    // The fix must invalidate that snapshot, not relax selector resolution.
    CHECK_FALSE(doc::resolve(selector, doc::describe_faces(split), std::sqrt(504.0)).index);

    const json assembly = {
        {"schema", "aicad.asm/1"},
        {"components", json::array({
            {{"id", "fixed"}, {"name", "Fixed"}, {"fixed", true},
             {"source", {{"path", path.string()}}}, {"transform", pose_tx(0)}},
            {{"id", "moving"}, {"name", "Moving"}, {"fixed", false},
             {"source", {{"path", path.string()}}}, {"transform", pose_tx(40)}}
        })},
        {"mates", json::array({
            {{"id", "lock"}, {"type", "lock"}, {"refs", json::array({
                {{"component", "fixed"}, {"selector", selector}},
                {{"component", "moving"}, {"selector", selector}}
            })}}
        })}};
    const auto result = doc::compile_assembly(assembly);
    REQUIRE(result.component_status.size() == 2);
    for (const auto& component : result.component_status) REQUIRE(component.ok);
    REQUIRE(result.mate_status.size() == 1);
    CHECK(result.mate_status.front().dangling_refs.empty());
    CHECK(result.mate_status.front().status == "ok");
    REQUIRE(result.converged);
    CHECK(result.dof == 0);
    CHECK(result.max_residual <= result.tolerance);
    const auto& moved = result.transforms.at("moving");
    CHECK(std::abs(moved[3] - 40.0) < 1e-7);  // lock preserves the authored relative pose
    CHECK(std::abs(moved[7]) < 1e-7);
    CHECK(std::abs(moved[11]) < 1e-7);
}

namespace {
// The load error of the first failed component after one asm_insert_component op, or ""
// when every component loaded. A thrown rejection counts as the error too.
std::string insert_error(const std::string& did, const std::string& source_path) {
    try {
        const json r = doc::doc_apply_ops(did, -1, json::array({
            {{"op", "asm_insert_component"}, {"source", {{"path", source_path}}}}}), false);
        for (const json& row : r.at("rebuild").at("feature_status"))
            if (!row.value("ok", true))
                return row.value("error", std::string("component failed without an error"));
        return {};
    } catch (const std::exception& e) {
        return e.what();
    }
}
}  // namespace

// 2026-10-10 six-axis trace: sources copied from a SAVED assembly (`parts/redesign/x.step+`,
// relative to its file) into a NEW unsaved assembly resolved against hostd's cwd and
// failed "doc_open: no such file: $HOME/parts/...". An unsaved assembly resolves
// relative sources against the project folder doc_new received, never the process cwd.
TEST_CASE("unsaved assembly resolves relative component sources against its project folder") {
    const fs::path project = tmp("relative_source_project");
    fs::remove_all(project);
    fs::create_directories(project / "parts");
    doc::export_bodies({BRepPrimAPI_MakeBox(gp_Pnt(0, 0, 0), 10.0, 10.0, 10.0).Shape()},
                       (project / "parts/box.step").string(), "step");
    std::vector<std::string> opened;
    struct Cleanup {
        std::vector<std::string>& ids; fs::path dir;
        ~Cleanup() {
            for (const std::string& id : ids) doc::doc_close(id);
            std::error_code ec;
            fs::remove_all(dir, ec);
        }
    } cleanup{opened, project};

    const std::string did = doc::doc_new("assembly", "relative_source_asm", false,
                                         project.string()).at("doc_id").get<std::string>();
    opened.push_back(did);
    const json tree = doc::doc_tree(did);
    CHECK(tree.at("component_source_base") == project.lexically_normal().string());

    // The relative source builds, and the document stores it exactly as given.
    CHECK(insert_error(did, "parts/box.step").empty());
    const json comps = doc::doc_tree(did).at("tree").at("components");
    REQUIRE(comps.size() == 1);
    CHECK(comps[0].at("source").at("path") == "parts/box.step");
    CHECK(doc::doc_bbox(did).at("max").at(0).get<double>() == doctest::Approx(10.0));

    // Save keeps the existing convention: a source under the assembly folder stays relative.
    doc::doc_save(did, (project / "relative_source_asm.step+").string());
    const json saved = doc::stepplus_read_document((project / "relative_source_asm.step+").string());
    REQUIRE(saved.at("components").size() == 1);
    CHECK(saved["components"][0].at("source").at("path") == "parts/box.step");

    // A missing file names the resolved absolute path and the base folder.
    const std::string missing = insert_error(did, "parts/absent.step");
    CHECK(missing.find((project / "parts/absent.step").lexically_normal().string()) !=
          std::string::npos);
    CHECK(missing.find("base folder '" + project.lexically_normal().string() + "'") !=
          std::string::npos);

    // No file and no project folder: refuse by name, never resolve against the cwd.
    const std::string loose = doc::doc_new("assembly", "relative_source_loose")
                                  .at("doc_id").get<std::string>();
    opened.push_back(loose);
    CHECK(doc::doc_tree(loose).at("component_source_base").is_null());
    const std::string refused = insert_error(loose, "parts/box.step");
    CHECK(refused.find("relative path") != std::string::npos);
    CHECK(refused.find(fs::current_path().string() + "/parts") == std::string::npos);

    // A relative project folder is a caller error, not a cwd guess.
    CHECK_THROWS(doc::doc_new("assembly", "relative_source_bad", false, "relative/project"));
}
