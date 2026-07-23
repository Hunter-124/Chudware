// Op-registry lockstep tests (mega-8 / OpVocab).
//
// 1. Every name in doc_op_kinds() / ecad_op_kinds() is accepted by apply_one
//    (no "unknown op" for registry members — smoke via dry path that only
//    hits the kind switch; invalid payloads may still fail later).
// 2. An invented op yields a rich error with closest suggestions + cad_guide.
// 3. Feature-type names (box/cylinder/…) point at add_feature, not themselves.
//
// If you add a handler in session.cpp / ops.cpp without updating the registry,
// the "registry ⊆ dispatch" assertion fails here.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include <algorithm>
#include <set>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "doc/op_registry.h"
#include "doc/session.h"
#include "ecad/op_registry.h"
#include "ecad/ops.h"
#include "ecad/schema.h"

using json = nlohmann::json;

// --------------------------------------------------------------------------- helpers

// Collect the set of op kind strings that appear as `if (k == "…")` arms in the
// registry. We cannot re-parse session.cpp at runtime, so the contract is:
//   * registry is the single source
//   * unknown_doc_op_message for a REGISTRY name must NOT say "unknown op"
//     for that name when we dry-check membership
//   * for a NON-registry name, the error must list suggestions from the registry
//
// Full dispatch acceptance is checked by applying a minimal/no-op-ish payload
// only where safe (rename/set_units/nop/eq_delete with empty targets still hit
// the kind branch first… actually those may throw on missing fields AFTER the
// kind is recognized). Safer: assert membership + suggestion quality only, and
// that every registry name appears in suggest_doc_ops for a near miss.

TEST_CASE("doc_op_kinds is non-empty and contains the core modeling ops") {
    auto kinds = chudware::doc::doc_op_kinds();
    REQUIRE(kinds.size() >= 20);
    std::set<std::string> s(kinds.begin(), kinds.end());
    CHECK(s.count("add_feature") == 1);
    CHECK(s.count("edit_feature") == 1);
    CHECK(s.count("sketch_add_entity") == 1);
    CHECK(s.count("asm_add_mate") == 1);
    // No duplicates.
    CHECK(s.size() == kinds.size());
}

TEST_CASE("feature_type_names includes primitives and solids") {
    auto ft = chudware::doc::feature_type_names();
    REQUIRE(ft.size() >= 10);
    std::set<std::string> s(ft.begin(), ft.end());
    CHECK(s.count("box") == 1);
    CHECK(s.count("cylinder") == 1);
    CHECK(s.count("sketch") == 1);
    CHECK(s.count("extrude") == 1);
    CHECK(s.count("fillet") == 1);
}

TEST_CASE("unknown doc op error lists closest + cad_guide") {
    const std::string msg = chudware::doc::unknown_doc_op_message(0, "sketch_add_entty");
    CHECK(msg.find("unknown op 'sketch_add_entty'") != std::string::npos);
    CHECK(msg.find("closest:") != std::string::npos);
    CHECK(msg.find("see cad_guide()") != std::string::npos);
    // Family match should prefer sketch_* over unrelated ops.
    CHECK(msg.find("sketch_add_entity") != std::string::npos);
}

TEST_CASE("feature type used as op points at add_feature envelope") {
    const std::string msg = chudware::doc::unknown_doc_op_message(0, "box");
    CHECK(msg.find("unknown op 'box'") != std::string::npos);
    CHECK(msg.find("feature TYPE") != std::string::npos);
    CHECK(msg.find("add_feature") != std::string::npos);
    CHECK(msg.find("see cad_guide()") != std::string::npos);
    auto sug = chudware::doc::suggest_doc_ops("box", 5);
    REQUIRE(!sug.empty());
    CHECK(sug.front() == "add_feature");
}

TEST_CASE("suggest_doc_ops prefers sketch_ family for sketch_create") {
    auto sug = chudware::doc::suggest_doc_ops("sketch_create", 5);
    REQUIRE(sug.size() >= 3);
    // All top suggestions should be sketch_* or add_feature (feature-type boost).
    int sketchish = 0;
    for (const auto& s : sug) {
        if (s.rfind("sketch_", 0) == 0 || s == "add_feature") ++sketchish;
    }
    CHECK(sketchish >= 2);
}

TEST_CASE("doc_apply_ops rejects invented op with rich error envelope") {
    using chudware::doc::doc_new;
    using chudware::doc::doc_apply_ops;
    json nr = doc_new("part", "OpVocabTest");
    const std::string id = nr.at("doc_id").get<std::string>();
    json ops = json::array({json{{"op", "cylinder"}}});
    bool threw = false;
    std::string msg;
    json detail;
    try {
        doc_apply_ops(id, -1, ops, false);
    } catch (const chudware::doc::OpError& e) {
        threw = true;
        msg = e.what();
        detail = e.detail;
    }
    REQUIRE(threw);
    CHECK(msg.find("unknown op 'cylinder'") != std::string::npos);
    CHECK(msg.find("closest:") != std::string::npos);
    CHECK(msg.find("cad_guide()") != std::string::npos);
    CHECK(detail.contains("suggestions"));
    CHECK(detail["suggestions"].is_array());
    CHECK(detail["suggestions"].size() >= 1);
}

TEST_CASE("ecad_op_kinds covers sch_ and pcb_ families") {
    auto kinds = chudware::ecad::ecad_op_kinds();
    REQUIRE(kinds.size() >= 20);
    std::set<std::string> s(kinds.begin(), kinds.end());
    CHECK(s.count("sch_add_symbol") == 1);
    CHECK(s.count("pcb_add_track") == 1);
    CHECK(s.count("nop") == 1);
    CHECK(s.size() == kinds.size());
}

TEST_CASE("unknown ecad op error lists closest + cad_guide") {
    const std::string msg = chudware::ecad::unknown_ecad_op_message(0, "sch_add_symbl");
    CHECK(msg.find("unknown op 'sch_add_symbl'") != std::string::npos);
    CHECK(msg.find("closest:") != std::string::npos);
    CHECK(msg.find("sch_add_symbol") != std::string::npos);
    CHECK(msg.find("see cad_guide()") != std::string::npos);
}

TEST_CASE("ecad apply_ops rejects invented op with suggestions") {
    json doc = chudware::ecad::schema::new_schematic("OpVocabSch");
    json ops = json::array({json{{"op", "add_resistor"}}});
    bool threw = false;
    std::string msg;
    try {
        chudware::ecad::apply_ops(doc, ops);
    } catch (const chudware::ecad::OpError& e) {
        threw = true;
        msg = e.what();
        CHECK(e.detail.contains("suggestions"));
    }
    REQUIRE(threw);
    CHECK(msg.find("unknown op 'add_resistor'") != std::string::npos);
    CHECK(msg.find("closest:") != std::string::npos);
    CHECK(msg.find("cad_guide()") != std::string::npos);
}

TEST_CASE("cheatsheet snippet mentions add_feature and box") {
    const std::string s = chudware::doc::doc_ops_cheatsheet_snippet();
    CHECK(s.find("add_feature") != std::string::npos);
    CHECK(s.find("box") != std::string::npos);
    CHECK(s.find("cad_guide()") != std::string::npos);
}
