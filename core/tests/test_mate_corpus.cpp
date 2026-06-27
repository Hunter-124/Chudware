// Smoke-load every core/mate/corpus/*.json fixture through solve_mates_json.
// Python parity is checked by the differential harness; here we only prove the C++
// solver accepts the full fixture set without throwing and returns the expected schema.
#define DOCTEST_CONFIG_IMPLEMENT_WITH_MAIN
#include <doctest/doctest.h>

#include "mate/mate.h"

#include <filesystem>
#include <fstream>
#include <string>

using namespace aicad::mate;
using json = nlohmann::json;

namespace {

std::filesystem::path corpus_dir() {
    return std::filesystem::path(__FILE__).parent_path().parent_path() / "mate" / "corpus";
}

json load_fixture(const std::filesystem::path& p) {
    std::ifstream in(p);
    REQUIRE(in.good());
    json j;
    in >> j;
    return j;
}

bool has_status(const json& out, const std::string& status) {
    if (!out.contains("mate_status") || !out["mate_status"].is_array()) return false;
    for (const auto& s : out["mate_status"])
        if (s.value("status", std::string()) == status) return true;
    return false;
}

void check_schema(const json& out) {
    CHECK(out.contains("converged"));
    CHECK(out.contains("dof"));
    CHECK(out.contains("transforms"));
    CHECK(out.contains("mate_status"));
    CHECK(out.contains("over_defined"));
    CHECK(out.contains("max_residual"));
}

}  // namespace

TEST_CASE("mate corpus fixtures load and return schema") {
    const auto dir = corpus_dir();
    REQUIRE(std::filesystem::is_directory(dir));
    int n = 0;
    for (const auto& ent : std::filesystem::directory_iterator(dir)) {
        if (ent.path().extension() != ".json") continue;
        json input = load_fixture(ent.path());
        json out = solve_mates_json(input);
        check_schema(out);
        ++n;
    }
    CHECK(n >= 30);  // guard against an empty/mis-pointed corpus dir
}

TEST_CASE("mate corpus spot checks") {
    SUBCASE("peg_in_hole_aligned: 1 DOF, converged") {
        json out = solve_mates_json(load_fixture(corpus_dir() / "peg_in_hole_aligned.json"));
        check_schema(out);
        CHECK(out["converged"].get<bool>());
        CHECK(out["dof"].get<int>() == 1);
    }
    SUBCASE("chain_coincident_4: coupled chain converges") {
        json out = solve_mates_json(load_fixture(corpus_dir() / "chain_coincident_4.json"));
        check_schema(out);
        CHECK(out["converged"].get<bool>());
    }
    SUBCASE("over_conflicting_distances: flagged over-defined") {
        json out = solve_mates_json(load_fixture(corpus_dir() / "over_conflicting_distances.json"));
        check_schema(out);
        CHECK(out["over_defined"].get<bool>());
        CHECK(has_status(out, "conflicting"));
    }
    SUBCASE("over_redundant_concentric: redundant but not over-defined") {
        json out = solve_mates_json(load_fixture(corpus_dir() / "over_redundant_concentric.json"));
        check_schema(out);
        CHECK_FALSE(out["over_defined"].get<bool>());
        CHECK(has_status(out, "redundant"));
    }
    SUBCASE("suppressed_and_unresolved: suppressed/unresolved statuses present") {
        json out = solve_mates_json(load_fixture(corpus_dir() / "suppressed_and_unresolved.json"));
        check_schema(out);
        CHECK((has_status(out, "suppressed") || has_status(out, "unresolved")));
    }
}
