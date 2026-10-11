// Regressions from the 6-axis robot arm agent session (2026-10-10). Defects 2, 3, 4 of the
// session findings:
//
//   2. FIX ON A SOLVED PROFILE. Sketch f61 (doc-cpp00000016) carried 8 coincident constraints
//      that closed a 4-line / 4-arc outline. The readback showed the SOLVED endpoints 1e-11 mm
//      apart, but the AUTHORED seeds were 0.002..0.030 mm apart. `fix` pins the authored
//      coordinates, so "add fix on every entity" was truly inconsistent and the whole batch
//      rolled back with a bare "sketch did not converge". Fixes: (a) sketch_add_constraint
//      moves the seeds to the solved geometry before it adds a `fix`
//      (sketch::adopt_solved_coordinates), so the fix locks the shape the readback shows;
//      (b) a sketch that still does not converge names the conflicting constraints and the
//      authored gap (sketch::nonconvergence_reason).
//   3. CENTROID-ONLY REGION. validate.cpp refused {kind:region, sketch, centroid} without
//      hint_index, though the centroid alone identifies a region.
//   4. INERT EXTRUDE PARAM. params.reverse:true was silently ignored (the engine reads
//      `flip`); pads built the wrong way with ok:true. The op path now refuses an unknown
//      extrude param by name and teaches `flip`. Saved docs that carry the legacy key still
//      open and accept edits that do not set it.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <cmath>
#include <string>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/session.h"
#include "sketch/sketch.h"

using json = nlohmann::json;
using chudware::doc::doc_apply_ops;
using chudware::doc::doc_new;
using chudware::doc::doc_open_embedded;
using chudware::doc::doc_tree;
using chudware::doc::OpError;

namespace {

constexpr double kPi = 3.14159265358979323846;

json line(const char* id, double x1, double y1, double x2, double y2) {
    return json{{"id", id}, {"type", "line"}, {"p1", {x1, y1}}, {"p2", {x2, y2}},
                {"construction", false}};
}
json arc(const char* id, double r, double a0, double a1) {
    return json{{"id", id}, {"type", "arc"}, {"center", {0.0, 0.0}}, {"radius", r},
                {"start", a0}, {"end", a1}, {"ccw", true}, {"construction", false}};
}

// The AUTHORED f61 seeds, verbatim from the trace payload (turntable.step+ document.json).
json f61_entities() {
    return json::array({
        line("e1", 19.51, -25.11, 19.51, 25.11),     arc("e2", 31.8, 52.1, 62.86),
        line("e3", 14.52, 28.29, -14.52, 28.29),     arc("e4", 31.8, 117.14, 133.8),
        line("e5", -22.01, 22.95, -22.01, -22.95),   arc("e6", 31.8, 226.2, 242.86),
        line("e7", -14.52, -28.29, 14.52, -28.29),   arc("e8", 31.8, 297.14, 307.9),
    });
}
const char* kF61Joins[8][2] = {
    {"e1.p2", "e2.start"}, {"e2.end", "e3.p1"}, {"e3.p2", "e4.start"}, {"e4.end", "e5.p1"},
    {"e5.p2", "e6.start"}, {"e6.end", "e7.p1"}, {"e7.p2", "e8.start"}, {"e8.end", "e1.p1"},
};
json f61_coincidents() {
    json out = json::array();
    for (int i = 0; i < 8; ++i)
        out.push_back(json{{"id", "c" + std::to_string(i + 1)}, {"type", "coincident"},
                           {"refs", {kF61Joins[i][0], kF61Joins[i][1]}}});
    return out;
}
json fix_all(json constraints, int first_id) {
    for (int i = 1; i <= 8; ++i)
        constraints.push_back(json{{"id", "c" + std::to_string(first_id + i - 1)},
                                   {"type", "fix"}, {"refs", {"e" + std::to_string(i)}}});
    return constraints;
}

// Point of a line/arc handle in an entity dict (authored or solved form).
std::pair<double, double> handle_xy(const json& ents, const std::string& h) {
    const std::string eid = h.substr(0, h.find('.'));
    const std::string sub = h.substr(h.find('.') + 1);
    const json& e = ents.at(eid);
    if (e.at("type") == "line") return {e.at(sub)[0].get<double>(), e.at(sub)[1].get<double>()};
    const double a = e.at(sub).get<double>() * kPi / 180.0, r = e.at("radius").get<double>();
    return {e.at("center")[0].get<double>() + r * std::cos(a),
            e.at("center")[1].get<double>() + r * std::sin(a)};
}
json by_id(const json& list) {
    json out = json::object();
    for (const json& e : list) out[e.at("id").get<std::string>()] = e;
    return out;
}

// Every error text an op batch produced: a thrown OpError (what + detail) or the
// per-feature rebuild errors. Empty when the batch applied and every feature built.
std::string batch_errors(const std::string& doc_id, const json& ops, json* resp_out = nullptr) {
    try {
        json r = doc_apply_ops(doc_id, -1, ops, false);
        std::string out;
        if (r.contains("rebuild") && r["rebuild"].contains("feature_status"))
            for (const json& row : r["rebuild"]["feature_status"])
                if (row.contains("error") && row["error"].is_string())
                    out += row["error"].get<std::string>() + "\n";
        if (resp_out) *resp_out = r;
        return out;
    } catch (const OpError& e) {
        return std::string(e.what()) + " " + e.detail.dump();
    }
}
bool has(const std::string& hay, const std::string& needle) {
    return hay.find(needle) != std::string::npos;
}

json sketch_op(const json& entities, const json& constraints) {
    return json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "S"},
             {"params", {{"entities", entities}, {"constraints", constraints}}},
             {"refs", {{"plane", "plane.top"}}}}}};
}
json extrude_op(const json& profile, const json& extra_params = json::object()) {
    json params = {{"mode", "boss"}, {"end", "blind"},
                   {"depth", json{{"value", 2.0}, {"expr", nullptr}}}};
    for (auto it = extra_params.begin(); it != extra_params.end(); ++it)
        params[it.key()] = it.value();
    return json{{"op", "add_feature"}, {"feature",
        json{{"type", "extrude"}, {"name", "E"}, {"params", params},
             {"refs", {{"profile", profile}}}}}};
}
json find_feature(const json& tree, const std::string& fid) {
    for (const json& f : tree.at("features"))
        if (f.value("id", std::string()) == fid) return f;
    return json(nullptr);
}

}  // namespace

// ============================================================================ defect 2

TEST_CASE("defect 2: fix on every entity solves when the coincident seeds agree to 1e-11 mm") {
    // Consistent redundant constraints: authored seeds where every joined pair already
    // agrees to 1e-11 mm. 36 pins + 16 coincident rows over 36 vars must converge.
    json ents = f61_entities();
    const json authored = by_id(ents);
    for (int i = 0; i < 8; ++i) {
        // Move each line endpoint onto its arc endpoint, then add 1e-11 mm of noise.
        std::string lh = kF61Joins[i][0], ah = kF61Joins[i][1];
        if (authored.at(lh.substr(0, 2)).at("type") != "line") std::swap(lh, ah);
        const auto p = handle_xy(authored, ah);
        for (json& e : ents)
            if (e.at("id") == lh.substr(0, 2))
                e[lh.substr(3)] = json::array({p.first + 1e-11, p.second - 1e-11});
    }
    const json sk = {{"entities", ents}, {"constraints", fix_all(f61_coincidents(), 9)}};
    const auto sol = chudware::sketch::solve_sketch(sk, json::object());
    CHECK(sol.converged);
    CHECK(sol.conflicting.empty());
    CHECK(sol.dof == 0);
}

TEST_CASE("defect 2: a truly inconsistent fix names the conflict and the authored gap") {
    // The trace seeds: fix pins them, and they are 0.0297 mm apart at e1.p2 / e2.start.
    const json sk = {{"entities", f61_entities()},
                     {"constraints", fix_all(f61_coincidents(), 9)}};
    const auto sol = chudware::sketch::solve_sketch(sk, json::object());
    REQUIRE_FALSE(sol.converged);
    const std::string msg = chudware::sketch::nonconvergence_reason(sk, sol);
    MESSAGE(msg);
    CHECK(has(msg, "sketch did not converge"));
    CHECK(has(msg, "authored coordinates"));
    CHECK(has(msg, "c1 (e1.p2, e2.start) 0.0297 mm"));
    CHECK_FALSE(sol.conflicting.empty());
    for (const std::string& id : sol.conflicting) CHECK(has(msg, id));
}

TEST_CASE("defect 2: adopt_solved_coordinates moves seeds without moving geometry") {
    json sk = {{"entities", f61_entities()}, {"constraints", f61_coincidents()}};
    const auto before = chudware::sketch::solve_sketch(sk, json::object());
    REQUIRE(before.converged);
    REQUIRE(chudware::sketch::adopt_solved_coordinates(sk, json::object()));
    // The seeds now close the outline, so fixing every entity is consistent.
    sk["constraints"] = fix_all(sk["constraints"], 9);
    const auto after = chudware::sketch::solve_sketch(sk, json::object());
    CHECK(after.converged);
    CHECK(after.conflicting.empty());
    for (int i = 0; i < 8; ++i)
        for (const char* h : kF61Joins[i]) {
            const auto a = handle_xy(before.entities, h), b = handle_xy(after.entities, h);
            CHECK(a.first == doctest::Approx(b.first).epsilon(1e-9));
            CHECK(a.second == doctest::Approx(b.second).epsilon(1e-9));
        }
    // A sketch that does not solve keeps its seeds.
    json bad = {{"entities", f61_entities()},
                {"constraints", fix_all(f61_coincidents(), 9)}};
    const json seeds = bad["entities"];
    CHECK_FALSE(chudware::sketch::adopt_solved_coordinates(bad, json::object()));
    CHECK(bad["entities"] == seeds);
}

TEST_CASE("defect 2: the trace batch (sketch_add_constraint fix on e1..e8) now applies") {
    const std::string id = doc_new("part", "SixAxisF61").at("doc_id").get<std::string>();
    // Batch 1, as the agent sent it: entities + coincidents + a boss on the outline.
    json ops = json::array({json{{"op", "add_feature"}, {"feature",
        json{{"type", "sketch"}, {"name", "CarrierDeckOutline"},
             {"params", {{"entities", json::array()}, {"constraints", json::array()}}},
             {"refs", {{"plane", "plane.top"}}}}}}});
    for (json e : f61_entities()) {
        e.erase("construction");
        ops.push_back(json{{"op", "sketch_add_entity"}, {"sketch", "$0"}, {"entity", e}});
    }
    for (const auto& j : kF61Joins)
        ops.push_back(json{{"op", "sketch_add_constraint"}, {"sketch", "$0"},
                           {"constraint", {{"type", "coincident"}, {"refs", {j[0], j[1]}}}}});
    ops.push_back(extrude_op(json::array({json{{"kind", "region"}, {"sketch", "$0"},
                                               {"centroid", {0, 0}}, {"hint_index", 0}}})));
    json r1;
    const std::string e1 = batch_errors(id, ops, &r1);
    REQUIRE_MESSAGE(e1.empty(), e1);
    const std::string fid = r1.at("id_map").at("$0").get<std::string>();
    const double vol1 = r1.value("volume", 0.0);
    REQUIRE(vol1 > 0.0);

    // Batch 2: fix every entity.
    json fixes = json::array();
    for (int i = 1; i <= 8; ++i)
        fixes.push_back(json{{"op", "sketch_add_constraint"}, {"sketch", fid},
                             {"constraint", {{"type", "fix"}, {"refs", {"e" + std::to_string(i)}}}}});
    json r2;
    const std::string e2 = batch_errors(id, fixes, &r2);
    CHECK_MESSAGE(e2.empty(), e2);
    // The fix locked the shape the readback showed: same volume, solved seeds stored.
    CHECK(r2.value("volume", 0.0) == doctest::Approx(vol1).epsilon(1e-9));
    const json sk = find_feature(doc_tree(id).at("tree"), fid);
    REQUIRE(sk.is_object());
    const json ents = by_id(sk.at("params").at("entities"));
    const auto p2 = handle_xy(ents, "e1.p2");
    const auto st = handle_xy(ents, "e2.start");
    CHECK(std::hypot(p2.first - st.first, p2.second - st.second) < 1e-9);
    CHECK(p2.first == doctest::Approx(19.5212).epsilon(1e-4));
    CHECK(p2.second == doctest::Approx(25.1021).epsilon(1e-4));
}

TEST_CASE("defect 2: an inline inconsistent fix fails with the named conflict") {
    // add_feature with inline constraints does not pass through sketch_add_constraint,
    // so the seeds stay authored and the compile error must explain why.
    const std::string id = doc_new("part", "SixAxisF61Inline").at("doc_id").get<std::string>();
    const std::string err = batch_errors(
        id, json::array({sketch_op(f61_entities(), fix_all(f61_coincidents(), 9))}));
    MESSAGE(err);
    CHECK(has(err, "sketch did not converge"));
    CHECK(has(err, "authored coordinates"));
    CHECK(has(err, "(e1.p2, e2.start) 0.0297 mm"));
}

// ============================================================================ defect 3

namespace {
// Two 10 x 10 squares on plane.top: A centred (5,0), B centred (16,0).
json two_square_ops() {
    auto rect = [](double x1, double y1, double x2, double y2) {
        return json{{"op", "sketch_add_entity"}, {"sketch", "$0"},
                    {"entity", json{{"type", "rectangle"},
                                    {"corner1", json::array({x1, y1})},
                                    {"corner2", json::array({x2, y2})}}}};
    };
    return json::array({sketch_op(json::array(), json::array()), rect(0, -5, 10, 5),
                        rect(11, -5, 21, 5)});
}
}  // namespace

TEST_CASE("defect 3: a region selector with a centroid and no hint_index is valid") {
    const std::string id = doc_new("part", "CentroidOnly").at("doc_id").get<std::string>();
    json ops = two_square_ops();
    ops.push_back(extrude_op(json::array({json{{"kind", "region"}, {"sketch", "$0"},
                                               {"centroid", {16.0, 0.0}}}})));
    json r;
    const std::string err = batch_errors(id, ops, &r);
    CHECK_MESSAGE(err.empty(), err);
    CHECK_FALSE(has(err, "hint_index"));
    // Square B only: 10 x 10 x 2.
    CHECK(r.value("volume", 0.0) == doctest::Approx(200.0).epsilon(1e-6));
}

TEST_CASE("defect 3: a non-numeric hint_index is still refused") {
    const std::string id = doc_new("part", "CentroidBadHint").at("doc_id").get<std::string>();
    json ops = two_square_ops();
    ops.push_back(extrude_op(json::array({json{{"kind", "region"}, {"sketch", "$0"},
                                               {"centroid", {5.0, 0.0}},
                                               {"hint_index", "first"}}})));
    const std::string err = batch_errors(id, ops);
    CHECK(has(err, "hint_index: must be a number"));
}

TEST_CASE("defect 3: an ambiguous centroid without hint_index fails by name") {
    {   // Equidistant from both square centroids (5,0) and (16,0).
        const std::string id = doc_new("part", "CentroidTie").at("doc_id").get<std::string>();
        json ops = two_square_ops();
        ops.push_back(extrude_op(json::array({json{{"kind", "region"}, {"sketch", "$0"},
                                                   {"centroid", {10.5, 0.0}}}})));
        const std::string err = batch_errors(id, ops);
        MESSAGE(err);
        CHECK(has(err, "profile:"));
        CHECK(has(err, "equidistant"));
    }
    {   // Far outside every region.
        const std::string id = doc_new("part", "CentroidFar").at("doc_id").get<std::string>();
        json ops = two_square_ops();
        ops.push_back(extrude_op(json::array({json{{"kind", "region"}, {"sketch", "$0"},
                                                   {"centroid", {100.0, 100.0}}}})));
        const std::string err = batch_errors(id, ops);
        MESSAGE(err);
        CHECK(has(err, "profile:"));
        CHECK(has(err, "outside"));
    }
}

// ============================================================================ defect 4

TEST_CASE("defect 4: an unknown extrude param is refused and names the right key") {
    const std::string id = doc_new("part", "ReverseKey").at("doc_id").get<std::string>();
    json ops = two_square_ops();
    ops.push_back(extrude_op(json::array({"$0"}), json{{"reverse", true}}));
    const std::string err = batch_errors(id, ops);
    MESSAGE(err);
    CHECK(has(err, "params.reverse"));
    CHECK(has(err, "\"flip\": true"));
    // The accepted keys are named.
    CHECK(has(err, "depth"));
    CHECK(has(err, "flip"));

    // A negative depth teaches flip too.
    const std::string id2 = doc_new("part", "NegativeDepth").at("doc_id").get<std::string>();
    json ops2 = two_square_ops();
    json neg = extrude_op(json::array({"$0"}));
    neg["feature"]["params"]["depth"] = json{{"value", -2.0}, {"expr", nullptr}};
    ops2.push_back(neg);
    const std::string err2 = batch_errors(id2, ops2);
    CHECK(has(err2, "depth must be > 0"));
    CHECK(has(err2, "set flip to true"));
}

TEST_CASE("defect 4: every key the extrude compiler reads is accepted") {
    const std::string id = doc_new("part", "KnownKeys").at("doc_id").get<std::string>();
    json ops = two_square_ops();
    ops.push_back(extrude_op(json::array({"$0"}),
                             json{{"flip", true}, {"merge", true}, {"mid_plane", false},
                                  {"draft_outward", false}, {"thin", false},
                                  {"start", "sketch_plane"}, {"end2", "none"},
                                  {"suppressed", false}}));
    json r;
    const std::string err = batch_errors(id, ops, &r);
    CHECK_MESSAGE(err.empty(), err);
    CHECK(r.value("volume", 0.0) == doctest::Approx(400.0).epsilon(1e-6));
}

TEST_CASE("defect 4: a saved doc with a legacy inert key still opens and edits") {
    const std::string id = doc_new("part", "LegacySrc").at("doc_id").get<std::string>();
    json ops = two_square_ops();
    ops.push_back(extrude_op(json::array({"$0"})));
    json r;
    REQUIRE(batch_errors(id, ops, &r).empty());
    const std::string efid = r.at("id_map").at("$3").get<std::string>();
    json tree = doc_tree(id).at("tree");
    for (json& f : tree["features"])
        if (f.value("id", std::string()) == efid) f["params"]["reverse"] = true;
    const std::string legacy = doc_open_embedded(tree).at("doc_id").get<std::string>();

    // An edit that does not set the legacy key applies.
    const json depth_edit = json::array({json{{"op", "edit_feature"}, {"id", efid},
        {"params", {{"depth", json{{"value", 3.0}, {"expr", nullptr}}}}}}});
    json r2;
    const std::string e2 = batch_errors(legacy, depth_edit, &r2);
    CHECK_MESSAGE(e2.empty(), e2);
    CHECK(r2.value("volume", 0.0) == doctest::Approx(600.0).epsilon(1e-6));

    // An edit that sets it is refused by name.
    const std::string e3 = batch_errors(legacy, json::array({json{{"op", "edit_feature"},
        {"id", efid}, {"params", {{"reverse", false}}}}}));
    CHECK(has(e3, "params.reverse"));

    // A null removes it.
    CHECK(batch_errors(legacy, json::array({json{{"op", "edit_feature"}, {"id", efid},
        {"params", {{"reverse", nullptr}}}}})).empty());
    CHECK_FALSE(find_feature(doc_tree(legacy).at("tree"), efid).at("params").contains("reverse"));
}
