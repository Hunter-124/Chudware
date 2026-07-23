// Unit test for the nested call-tree / phase-tree profiler (core/prof/tree.{h,cpp} +
// phases::report_tree). Pure fixture — no sampling backend, no OCCT.
//
// Validates:
//   1. build_tree from a KNOWN folded-stack fixture: parent/child self+total sample counts.
//   2. path-scoped to_json returns only that subtree within a depth cap.
//   3. phases::report_tree nests study -> phase -> subphase with wall-clock rollups.
//   4. flat phases::report() shape is unchanged (additive only).
//
// Prints wall-times (steady_clock) for the tree build / serialize paths — no expected-time
// assertions (honest measurement only).
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <chrono>
#include <cstdio>
#include <string>
#include <vector>

#include "prof/phases.h"
#include "prof/tree.h"

namespace tree = chudware::prof::tree;
namespace phases = chudware::prof::phases;
using nlohmann::json;

namespace {

// Known folded fixture (root->leaf):
//   main;foo;bar  10
//   main;foo;baz   5
//   main;qux       3
//   other;z        2
//
// Expected tree:
//   root total=20
//     main total=18 self=0
//       foo total=15 self=0
//         bar total=10 self=10
//         baz total=5  self=5
//       qux total=3 self=3
//     other total=2 self=0
//       z total=2 self=2
constexpr const char* kFolded =
    "main;foo;bar 10\n"
    "main;foo;baz 5\n"
    "main;qux 3\n"
    "other;z 2\n";

const tree::Node* child_named(const tree::Node& n, const std::string& name) {
    for (const auto& c : n.children)
        if (c.name == name) return &c;
    return nullptr;
}

double ms_since(std::chrono::steady_clock::time_point t0) {
    return std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - t0)
        .count();
}

}  // namespace

TEST_CASE("build_tree: parent/child self+total sample counts from folded fixture") {
    auto t0 = std::chrono::steady_clock::now();
    auto stacks = tree::parse_folded(kFolded);
    CHECK(stacks.size() == 4);

    tree::Node root = tree::build_tree(stacks);
    const double build_ms = ms_since(t0);
    std::printf("test_prof_tree build_tree wall_ms=%.3f\n", build_ms);

    CHECK(root.total_samples == 20);
    CHECK(root.self_samples == 0);
    CHECK(root.children.size() == 2);  // main (hotter), other

    // Hot-first: main (18) before other (2).
    CHECK(root.children[0].name == "main");
    CHECK(root.children[0].total_samples == 18);
    CHECK(root.children[0].self_samples == 0);
    CHECK(root.children[0].pct_of_parent == doctest::Approx(90.0));

    const tree::Node* main = child_named(root, "main");
    REQUIRE(main);
    const tree::Node* foo = child_named(*main, "foo");
    REQUIRE(foo);
    CHECK(foo->total_samples == 15);
    CHECK(foo->self_samples == 0);

    const tree::Node* bar = child_named(*foo, "bar");
    REQUIRE(bar);
    CHECK(bar->total_samples == 10);
    CHECK(bar->self_samples == 10);
    CHECK(bar->children.empty());

    const tree::Node* baz = child_named(*foo, "baz");
    REQUIRE(baz);
    CHECK(baz->total_samples == 5);
    CHECK(baz->self_samples == 5);

    const tree::Node* qux = child_named(*main, "qux");
    REQUIRE(qux);
    CHECK(qux->total_samples == 3);
    CHECK(qux->self_samples == 3);

    const tree::Node* other = child_named(root, "other");
    REQUIRE(other);
    CHECK(other->total_samples == 2);
    const tree::Node* z = child_named(*other, "z");
    REQUIRE(z);
    CHECK(z->self_samples == 2);
}

TEST_CASE("to_json: path-scoped subtree + depth cap") {
    tree::Node root = tree::build_tree_from_folded(kFolded);

    auto t0 = std::chrono::steady_clock::now();
    json full = tree::tree_to_json(root, json{{"depth", -1}, {"top", 0}});
    const double full_ms = ms_since(t0);
    std::printf("test_prof_tree to_json(full) wall_ms=%.3f\n", full_ms);

    REQUIRE(full.value("ok", false));
    CHECK(full["root"]["total_samples"] == 20);
    // Full tree has main -> foo -> bar under root.
    REQUIRE(full["root"]["children"].size() >= 1);
    CHECK(full["root"]["children"][0]["name"] == "main");

    // Path drill: main/foo, depth=0 -> only the foo node, no children.
    t0 = std::chrono::steady_clock::now();
    json scoped = tree::tree_to_json(root, json{{"path", "main/foo"}, {"depth", 0}});
    const double scoped_ms = ms_since(t0);
    std::printf("test_prof_tree to_json(path=main/foo,depth=0) wall_ms=%.3f\n", scoped_ms);

    REQUIRE(scoped.value("ok", false));
    CHECK(scoped["path"] == "main/foo");
    CHECK(scoped["root"]["name"] == "foo");
    CHECK(scoped["root"]["total_samples"] == 15);
    CHECK(scoped["root"]["self_samples"] == 0);
    CHECK(scoped["root"]["path"] == "main/foo");
    CHECK(scoped["root"]["children"].empty());  // depth 0

    // Same path, depth=1 -> bar + baz as direct children, no grandchildren (leaves).
    json d1 = tree::tree_to_json(root, json{{"path", "main/foo"}, {"depth", 1}});
    REQUIRE(d1.value("ok", false));
    CHECK(d1["root"]["children"].size() == 2);
    CHECK(d1["root"]["children"][0]["name"] == "bar");  // hot-first: 10 > 5
    CHECK(d1["root"]["children"][0]["total_samples"] == 10);
    CHECK(d1["root"]["children"][1]["name"] == "baz");
    // Leaves have empty children arrays.
    CHECK(d1["root"]["children"][0]["children"].empty());

    // top=1 at root depth=1 -> only the hottest child (main).
    json top1 = tree::tree_to_json(root, json{{"depth", 1}, {"top", 1}});
    REQUIRE(top1.value("ok", false));
    CHECK(top1["root"]["children"].size() == 1);
    CHECK(top1["root"]["children"][0]["name"] == "main");
    CHECK(top1["root"].value("children_elided", 0) == 1);

    // Missing path.
    json miss = tree::tree_to_json(root, json{{"path", "no/such/path"}});
    CHECK(miss.value("ok", false) == false);
    CHECK(miss.contains("error"));
}

TEST_CASE("from_folded one-shot matches build_tree") {
    auto t0 = std::chrono::steady_clock::now();
    json j = tree::from_folded(kFolded, json{{"path", "main"}, {"depth", 1}});
    std::printf("test_prof_tree from_folded wall_ms=%.3f\n", ms_since(t0));
    REQUIRE(j.value("ok", false));
    CHECK(j["root"]["name"] == "main");
    CHECK(j["root"]["total_samples"] == 18);
    // depth 1: foo + qux, no bar/baz.
    REQUIRE(j["root"]["children"].size() == 2);
    CHECK(j["root"]["children"][0]["name"] == "foo");
    // children of foo must be empty at depth 1 from main.
    CHECK(j["root"]["children"][0]["children"].empty());
}

TEST_CASE("phases report_tree: study -> phase -> subphase rollups; flat unchanged") {
    phases::reset();

    // Deterministic wall times via add() (no sleep — identical every run).
    {
        phases::StudyScope study("unit_tree");
        phases::add("solve.assemble", 40.0, 1);
        phases::add("solve.factorize", 30.0, 1);
        phases::add("solve.backsub", 20.0, 1);
        phases::add("mesh.tet", 10.0, 1);
        // Manual total won't match study wall (StudyScope measures real wall), so we only
        // assert relative structure + phase totals, not study total_ms equality.
    }

    // Flat report shape must still be the classic studies[].phases[] array.
    auto t0 = std::chrono::steady_clock::now();
    json flat = phases::report(false);
    std::printf("test_prof_tree phases::report(flat) wall_ms=%.3f\n", ms_since(t0));
    REQUIRE(flat.value("ok", false));
    REQUIRE(flat["studies"].is_array());
    REQUIRE(flat["studies"].size() == 1);
    CHECK(flat["studies"][0]["domain"] == "unit_tree");
    REQUIRE(flat["studies"][0]["phases"].is_array());
    // Flat still lists dotted names as flat entries (byte-stable contract).
    bool saw_assemble = false;
    for (const auto& p : flat["studies"][0]["phases"]) {
        if (p.value("name", std::string()) == "solve.assemble") {
            saw_assemble = true;
            CHECK(p.value("ms", 0.0) == doctest::Approx(40.0));
        }
    }
    CHECK(saw_assemble);

    t0 = std::chrono::steady_clock::now();
    json nested = phases::report_tree(json{{"depth", -1}});
    std::printf("test_prof_tree phases::report_tree wall_ms=%.3f\n", ms_since(t0));
    REQUIRE(nested.value("ok", false));
    CHECK(nested.value("completed", 0ULL) == 1);
    REQUIRE(nested.contains("root"));
    // Root has one study child "unit_tree".
    REQUIRE(nested["root"]["children"].size() == 1);
    const json& study = nested["root"]["children"][0];
    CHECK(study["name"] == "unit_tree");

    // Under the study: "solve" and "mesh" (dotted split). solve should be hotter.
    REQUIRE(study["children"].size() >= 2);
    const json* solve = nullptr;
    const json* mesh = nullptr;
    for (const auto& c : study["children"]) {
        if (c.value("name", std::string()) == "solve") solve = &c;
        if (c.value("name", std::string()) == "mesh") mesh = &c;
    }
    REQUIRE(solve);
    REQUIRE(mesh);
    CHECK((*solve).value("total_ms", 0.0) == doctest::Approx(90.0));  // 40+30+20
    CHECK((*mesh).value("total_ms", 0.0) == doctest::Approx(10.0));

    // solve children: assemble / factorize / backsub
    bool saw_a = false, saw_f = false, saw_b = false;
    for (const auto& c : (*solve)["children"]) {
        const std::string n = c.value("name", std::string());
        if (n == "assemble") {
            saw_a = true;
            CHECK(c.value("total_ms", 0.0) == doctest::Approx(40.0));
            CHECK(c.value("self_ms", 0.0) == doctest::Approx(40.0));
        }
        if (n == "factorize") {
            saw_f = true;
            CHECK(c.value("total_ms", 0.0) == doctest::Approx(30.0));
        }
        if (n == "backsub") {
            saw_b = true;
            CHECK(c.value("total_ms", 0.0) == doctest::Approx(20.0));
        }
    }
    CHECK(saw_a);
    CHECK(saw_f);
    CHECK(saw_b);

    // Path drill: unit_tree/solve, depth=1.
    json drill = phases::report_tree(json{{"path", "unit_tree/solve"}, {"depth", 1}});
    REQUIRE(drill.value("ok", false));
    CHECK(drill["root"]["name"] == "solve");
    CHECK(drill["root"]["total_ms"] == doctest::Approx(90.0));
    CHECK(drill["root"]["children"].size() == 3);
    // depth 1: children present, their children empty.
    for (const auto& c : drill["root"]["children"]) {
        CHECK(c["children"].empty());
    }

    // Flat report still works after tree report (no accidental reset).
    json flat2 = phases::report(false);
    CHECK(flat2["studies"].size() == 1);
    CHECK(flat2.value("completed", 0ULL) == 1);
}

TEST_CASE("folded export text round-trips through parse_folded unchanged in counts") {
    // Byte-stable contract for the FOLDED FORMAT itself: parse then re-aggregate yields
    // the same totals. (We cannot assert sampler report() folded bytes without a live
    // sample backend + symbols; the format parser is the pure contract under test here.)
    auto stacks = tree::parse_folded(kFolded);
    unsigned long long total = 0;
    for (const auto& s : stacks) total += s.second;
    CHECK(total == 20);

    // Re-serialize to folded and re-parse — counts must match.
    std::string re;
    for (const auto& s : stacks) {
        for (size_t i = 0; i < s.first.size(); ++i) {
            if (i) re += ';';
            re += s.first[i];
        }
        re += ' ';
        re += std::to_string(s.second);
        re += '\n';
    }
    auto stacks2 = tree::parse_folded(re);
    CHECK(stacks2.size() == stacks.size());
    tree::Node a = tree::build_tree(stacks);
    tree::Node b = tree::build_tree(stacks2);
    CHECK(a.total_samples == b.total_samples);
    CHECK(a.children.size() == b.children.size());
    for (size_t i = 0; i < a.children.size(); ++i) {
        CHECK(a.children[i].name == b.children[i].name);
        CHECK(a.children[i].total_samples == b.children[i].total_samples);
        CHECK(a.children[i].self_samples == b.children[i].self_samples);
    }
}
